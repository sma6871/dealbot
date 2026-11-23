/**
 * dealbot webhook — instant Telegram callback handling.
 *
 * Replaces the review.yml polling job. Handles button taps, the draft edit
 * loop, and /batch. State lives in Cloudflare KV.
 *
 * Routes:
 *   POST /webhook   Telegram updates (verified via secret header)
 *   GET  /health    status JSON, polled by GitHub Actions
 *
 * Commands:
 *   /draft <text>   draft a post from free text (non-mydealz deals)
 *   /missed <url>   log a deal the filter should have caught
 *   /batch [n]      raw deals for rule training, never posts
 *   /status         worker health
 *
 * KV keys:
 *   pending:<message_id>   a deal awaiting my approve/skip
 *   draft:<message_id>     { deal, text, edits[] } awaiting send/edit/discard
 *   awaiting:<chat_id>     message_id I'm currently being asked to comment on
 *   decisions             JSON array, drained into log.csv by GitHub Actions
 *   seen                  JSON array of deal ids
 *   stats                 { lastUpdate, updateCount, lastError }
 */

const MAX_EDIT_ROUNDS = 10;
const TG_LIMIT = 4096;

// ---------- helpers ----------

const esc = (s) =>
  String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

async function tg(env, method, payload) {
  const r = await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/${method}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!r.ok) console.error(`${method} failed:`, await r.clone().text());
  return r;
}

const URL_RE = /https?:\/\/[^\s<>"']+/;

function parseLinkLine(text) {
  // "link: https://..." or "url: https://..." anywhere in the message
  const m = (text || "").match(/^\s*(?:link|url)\s*:\s*(\S+)/im);
  if (m && URL_RE.test(m[1])) return m[1];
  return null;
}

function parseImageLine(text) {
  // "image: https://..." or "img: https://..." or "photo: https://..."
  const m = (text || "").match(/^\s*(?:image|img|photo)\s*:\s*(\S+)/im);
  if (m && URL_RE.test(m[1])) return m[1];
  return null;
}

function isRemoveImage(text) {
  return /^\s*(?:remove|drop|delete|no)\s*(?:image|photo|pic|img)\b/i.test(text || "");
}

function shortenPrompt(env, text) {
  return `Condense this bilingual Telegram deal post so its TOTAL length is strictly under 1,000 characters (Telegram photo captions cannot exceed 1,024 characters).

Current text (${text.length} characters):
${text}

Rules:
- Keep the bilingual structure: English section first, separator line (━━━━━━━━━━━━━━━━━━━━━━), then Persian section.
- Keep all crucial offer facts: product/deal name, price, original price, merchant/shop name, voucher code if any, and core steps.
- Persian numerals in the Persian block.
- Tighten sentences, omit non-essential commentary, and keep explanations brief and punchy.
- Return ONLY the condensed post with valid Telegram HTML tags (<b>, <i>, <a href="...">). No markdown fences, no preamble.`;
}

async function notifyOwner(env, text) {
  await tg(env, "sendMessage", {
    chat_id: env.TELEGRAM_ADMIN_CHAT_ID,
    text: text.slice(0, 3500),
  });
}

async function kvJson(env, key, fallback) {
  const v = await env.DEALBOT.get(key);
  if (!v) return fallback;
  try {
    return JSON.parse(v);
  } catch {
    return fallback;
  }
}

async function logDecision(env, row) {
  const decisions = await kvJson(env, "decisions", []);
  decisions.push({ timestamp: new Date().toISOString(), ...row });
  // Keep bounded; GitHub Actions drains this regularly.
  await env.DEALBOT.put("decisions", JSON.stringify(decisions.slice(-1000)));
}

// Four-way feedback. "good" and "meh" distinguish "fair pick, wrong moment"
// from "borderline" — binary skip collapsed those and lost the signal.
const REVIEW_KB = {
  inline_keyboard: [
    [
      { text: "🔥 Post", callback_data: "hot" },
      { text: "👍 Good but no", callback_data: "good" },
    ],
    [
      { text: "😐 Meh", callback_data: "meh" },
      { text: "❌ Never", callback_data: "never" },
    ],
  ],
};

const DRAFT_KB = {
  inline_keyboard: [[
    { text: "✅ Send", callback_data: "send" },
    { text: "✏️ Edit", callback_data: "edit" },
    { text: "❌ Discard", callback_data: "discard" },
  ]],
};

const BATCH_KB = {
  inline_keyboard: [[
    { text: "👍", callback_data: "batch_up" },
    { text: "👎", callback_data: "batch_down" },
  ]],
};

// ---------- LLM ----------

async function callGemini(env, prompt) {
  const model = env.GEMINI_MODEL || "gemini-flash-latest";
  const r = await fetch(
    `https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent`,
    {
      method: "POST",
      headers: { "x-goog-api-key": env.GEMINI_API_KEY, "Content-Type": "application/json" },
      body: JSON.stringify({
        contents: [{ parts: [{ text: prompt }] }],
        // MUST be nested inside generationConfig or it's silently ignored.
        generationConfig: {
          temperature: 0.4,
          thinkingConfig: { thinkingLevel: "minimal" },
        },
      }),
    }
  );
  if (!r.ok) throw new Error(`gemini ${r.status}: ${await r.text()}`);
  const d = await r.json();
  return d?.candidates?.[0]?.content?.parts?.[0]?.text ?? "";
}

async function callGroq(env, prompt) {
  const r = await fetch("https://api.groq.com/openai/v1/chat/completions", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${env.GROQ_API_KEY}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      model: env.GROQ_MODEL || "llama-3.3-70b-versatile",
      messages: [{ role: "user", content: prompt }],
      temperature: 0.4,
    }),
  });
  if (!r.ok) throw new Error(`groq ${r.status}: ${await r.text()}`);
  const d = await r.json();
  return d?.choices?.[0]?.message?.content ?? "";
}

async function generate(env, prompt) {
  const errors = [];
  for (const [name, fn, key] of [
    ["gemini", callGemini, env.GEMINI_API_KEY],
    ["groq", callGroq, env.GROQ_API_KEY],
  ]) {
    if (!key) continue;
    try {
      const text = await fn(env, prompt);
      if (text.trim()) return text.trim();
      errors.push(`${name}: empty response`);
    } catch (e) {
      errors.push(`${name}: ${e.message}`);
    }
  }
  throw new Error(`all providers failed — ${errors.join(" | ")}`);
}

function draftPrompt(env, deal, edits) {
  const editBlock = edits.length
    ? `\nApply these revision instructions, in order. Later ones win:\n${edits
        .map((e, i) => `${i + 1}. ${e}`)
        .join("\n")}\n`
    : "";

  return `Write a Telegram post for this deal, following the template exactly.

<template>
${env.POST_TEMPLATE}
</template>

<deal>
Title: ${deal.title}
Shop: ${deal.merchant || deal.link_host || "unknown"}
Price: ${deal.price ?? "n/a"}
Was: ${deal.next_best_price ?? "n/a"}
Temperature: ${deal.temperature ?? "n/a"}
Voucher code: ${deal.voucher_code || "none"}
Description: ${deal.description ?? deal.summary ?? ""}
Why it was selected: ${deal.reason ?? ""}
</deal>
${editBlock}
Rules:
- English block first, then the separator line, then the full Persian translation.
- Persian numerals in the Persian block. Keep links in Latin script.
- If a detail is not in the deal data, leave that line out. Never invent prices,
  dates, or discount percentages.
- NEVER include a mydealz.de link or mention mydealz. The channel does not
  disclose its source. Name the shop instead (e.g. "at amazon.de").
- Do not put the deal URL in the text at all. It is attached as a button.
- Output ONLY the post text. No preamble, no markdown fences.
- Use Telegram HTML: <b>, <i>, <a href="">. No other tags.`;
}

function makeDraftKb(deal, dealUrl) {
  const kbRows = [];
  if (dealUrl) {
    let label = "🛒 Deal Link";
    try {
      label = `🛒 Link: ${new URL(dealUrl).hostname.replace(/^www\./, "")}`;
    } catch {}
    kbRows.push([{ text: label, url: dealUrl }]);
  } else {
    const shop = deal?.merchant || deal?.link_host || null;
    kbRows.push([{
      text: `⚠️ No deal link yet${shop ? ` (${shop})` : ""}`,
      callback_data: "edit",
    }]);
  }
  kbRows.push([
    { text: "✅ Send", callback_data: "send" },
    { text: "✏️ Edit", callback_data: "edit" },
    { text: "❌ Discard", callback_data: "discard" },
  ]);
  return { inline_keyboard: kbRows };
}

async function sendDraft(env, chatId, deal, text, edits, dealUrl, image = null) {
  if (text.length > TG_LIMIT) {
    await tg(env, "sendMessage", {
      chat_id: chatId,
      text: `Draft is ${text.length} chars, over Telegram's ${TG_LIMIT} limit. Ask for "shorter".`,
    });
    return;
  }

  const shop = deal?.merchant || deal?.link_host || null;
  // No angle brackets here — Telegram's HTML parser rejects unknown tags.
  const status = dealUrl
    ? `🛒 Link set: ${esc(dealUrl)}`
    : `⚠️ No deal link yet.${shop ? ` Shop is ${esc(shop)}.` : ""} Reply with "link: https://..." to add a button.`;

  const draftKb = makeDraftKb(deal, dealUrl);
  const hasPhoto = Boolean(image && (image.photoFileId || image.imageUrl) && text.length <= 1024);

  let r;
  if (hasPhoto) {
    const payload = {
      chat_id: chatId,
      photo: image.photoFileId || image.imageUrl,
      caption: text,
      parse_mode: "HTML",
      reply_markup: draftKb,
    };
    r = await tg(env, "sendPhoto", payload);
    if (!r.ok) {
      console.error("HTML draft photo failed, retrying plain:", await r.clone().text());
      const plain = { ...payload };
      delete plain.parse_mode;
      r = await tg(env, "sendPhoto", plain);
    }
  } else {
    const payload = {
      chat_id: chatId,
      text: `${text}\n\n— ${status}`,
      parse_mode: "HTML",
      link_preview_options: { is_disabled: true },
      reply_markup: draftKb,
    };
    r = await tg(env, "sendMessage", payload);
    if (!r.ok) {
      console.error("HTML draft failed, retrying plain:", await r.clone().text());
      const plain = { ...payload };
      delete plain.parse_mode;
      r = await tg(env, "sendMessage", plain);
    }
  }

  if (!r.ok) {
    await notifyOwner(
      env,
      `❌ Could not send the draft:\n\n${(await r.text()).slice(0, 600)}`
    );
    return;
  }

  const body = await r.json().catch(() => null);
  const mid = body?.result?.message_id;
  if (mid) {
    await env.DEALBOT.put(
      `draft:${mid}`,
      JSON.stringify({
        deal,
        text,
        edits,
        dealUrl: dealUrl || null,
        image: image || null,
      }),
      { expirationTtl: 60 * 60 * 24 * 7 }
    );
  } else {
    await notifyOwner(env, "Draft sent but couldn't be saved — the buttons won't work. Try again.");
  }
}

// ---------- handlers ----------

async function generateAndSendDraft(env, chatId, deal, edits, dealUrl) {
  await tg(env, "sendMessage", { chat_id: chatId, text: "Writing a draft…" });
  try {
    const text = await generate(env, draftPrompt(env, deal, edits));
    await sendDraft(env, chatId, deal, text, edits, dealUrl, null);
  } catch (e) {
    await notifyOwner(env, `❌ Draft failed: ${e.message}`);
  }
}

const REVIEW_ACTIONS = {
  hot: { decision: "hot", note: "🔥 Posting", askReason: false },
  good: { decision: "good_but_no", note: "👍 Good pick, not now", askReason: true },
  meh: { decision: "meh", note: "😐 Borderline", askReason: true },
  never: { decision: "never", note: "❌ Should never have surfaced", askReason: true },
};

async function handleCallback(env, cb) {
  const chatId = cb.message.chat.id;
  const mid = cb.message.message_id;
  const action = cb.data;

  await tg(env, "answerCallbackQuery", { callback_query_id: cb.id });

  // --- review stage: four-way feedback ---
  if (REVIEW_ACTIONS[action]) {
    const { decision, note, askReason } = REVIEW_ACTIONS[action];
    const deal = await kvJson(env, `pending:${mid}`, null);

    await tg(env, "editMessageReplyMarkup", {
      chat_id: chatId, message_id: mid, reply_markup: { inline_keyboard: [] },
    });

    if (!deal) {
      // Never fail silently — the owner sees the buttons vanish otherwise.
      await notifyOwner(env, "That deal expired or wasn't found in storage, so nothing was logged.");
      return;
    }

    await logDecision(env, {
      decision,
      title: deal.title,
      link: deal.link,
      temperature: deal.temperature ?? "",
      shop: deal.merchant || deal.link_host || "",
      model_reason: deal.reason ?? "",
    });

    await tg(env, "editMessageText", {
      chat_id: chatId,
      message_id: mid,
      text: `${cb.message.text || ""}\n\n— ${note}`,
      link_preview_options: { is_disabled: true },
    });

    if (action === "hot") {
      await generateAndSendDraft(env, chatId, deal, [], null);
      await env.DEALBOT.delete(`pending:${mid}`);
    } else if (askReason) {
      await env.DEALBOT.put(`awaiting:${chatId}`, String(mid), { expirationTtl: 3600 });
      await tg(env, "sendMessage", {
        chat_id: chatId,
        text: "Reply with a reason if you have one (optional, but valuable when you disagree with the model).",
      });
    }
    return;
  }

  // --- batch stage: thumbs for rule training only, never posts ---
  if (action === "batch_up" || action === "batch_down") {
    const deal = await kvJson(env, `pending:${mid}`, null);
    await tg(env, "editMessageReplyMarkup", {
      chat_id: chatId, message_id: mid, reply_markup: { inline_keyboard: [] },
    });
    if (!deal) return;
    await logDecision(env, {
      decision: action,
      title: deal.title,
      link: deal.link,
      temperature: deal.temperature ?? "",
      shop: deal.merchant || deal.link_host || "",
    });
    if (action === "batch_down") {
      await env.DEALBOT.put(`awaiting:${chatId}`, String(mid), { expirationTtl: 3600 });
    }
    return;
  }

  // --- draft shortening / image dropping callbacks ---
  if (action.startsWith("shorten:")) {
    const targetMid = action.split(":")[1];
    const draft = await kvJson(env, `draft:${targetMid}`, null);
    await tg(env, "editMessageReplyMarkup", {
      chat_id: chatId,
      message_id: mid,
      reply_markup: { inline_keyboard: [] },
    });
    if (!draft) {
      await notifyOwner(env, "That draft expired or wasn't found.");
      return;
    }
    await tg(env, "sendMessage", { chat_id: chatId, text: "Condensing post to < 1024 chars…" });
    try {
      const shortened = await generate(env, shortenPrompt(env, draft.text));
      const image = draft.pendingImage || draft.image || null;
      delete draft.pendingImage;
      await env.DEALBOT.delete(`draft:${targetMid}`);
      await sendDraft(
        env,
        chatId,
        draft.deal,
        shortened,
        [...(draft.edits || []), "condensed < 1024 chars"],
        draft.dealUrl,
        image
      );
    } catch (e) {
      await notifyOwner(env, `❌ Condensing failed: ${e.message}`);
    }
    return;
  }

  if (action.startsWith("drop_img:")) {
    const targetMid = action.split(":")[1];
    const draft = await kvJson(env, `draft:${targetMid}`, null);
    await tg(env, "editMessageReplyMarkup", {
      chat_id: chatId,
      message_id: mid,
      reply_markup: { inline_keyboard: [] },
    });
    if (draft) {
      delete draft.pendingImage;
      delete draft.image;
      await env.DEALBOT.put(`draft:${targetMid}`, JSON.stringify(draft), { expirationTtl: 60 * 60 * 24 * 7 });
    }
    await tg(env, "sendMessage", { chat_id: chatId, text: "Kept original text. Image was dropped." });
    return;
  }

  // --- draft stage: send, edit, discard ---
  const draft = await kvJson(env, `draft:${mid}`, null);
  if (!draft) {
    await notifyOwner(env, "That draft expired or wasn't found. Nothing was sent.");
    return;
  }

  if (action === "send") {
    const markup = draft.dealUrl
      ? { inline_keyboard: [[{ text: "🛒 Get the deal · دریافت آفر", url: draft.dealUrl }]] }
      : undefined;

    const hasPhoto = Boolean(draft.image && (draft.image.photoFileId || draft.image.imageUrl));
    let r;

    if (hasPhoto) {
      if (draft.text.length > 1024) {
        await notifyOwner(
          env,
          `❌ Cannot post with image: caption is ${draft.text.length} chars (Telegram max 1024). Tap Edit and shorten it.`
        );
        return;
      }
      const payload = {
        chat_id: env.TELEGRAM_CHANNEL_ID,
        photo: draft.image.photoFileId || draft.image.imageUrl,
        caption: draft.text,
        parse_mode: "HTML",
      };
      if (markup) payload.reply_markup = markup;

      r = await tg(env, "sendPhoto", payload);
      if (!r.ok) {
        const firstErr = await r.text();
        console.error("HTML sendPhoto failed, retrying plain:", firstErr);
        const plain = { ...payload };
        delete plain.parse_mode;
        r = await tg(env, "sendPhoto", plain);
        if (!r.ok) {
          await notifyOwner(
            env,
            `❌ Post failed, draft kept. Tap Send again after fixing.\n\n${(await r.text()).slice(0, 600)}`
          );
          return;
        }
      }
    } else {
      const payload = {
        chat_id: env.TELEGRAM_CHANNEL_ID,
        text: draft.text,
        parse_mode: "HTML",
        link_preview_options: { is_disabled: true },
      };
      if (markup) payload.reply_markup = markup;

      r = await tg(env, "sendMessage", payload);
      if (!r.ok) {
        const firstErr = await r.text();
        console.error("HTML post failed, retrying plain:", firstErr);
        const plain = { ...payload };
        delete plain.parse_mode;
        r = await tg(env, "sendMessage", plain);
        if (!r.ok) {
          await notifyOwner(
            env,
            `❌ Post failed, draft kept. Tap Send again after fixing.\n\n${(await r.text()).slice(0, 600)}`
          );
          return;
        }
      }
    }

    await tg(env, "editMessageReplyMarkup", {
      chat_id: chatId, message_id: mid, reply_markup: { inline_keyboard: [] },
    });
    await tg(env, "sendMessage", { chat_id: chatId, text: "Posted ✅" });
    await logDecision(env, {
      decision: "sent",
      title: draft.deal?.title || "",
      link: draft.deal?.link || "",
      shop: draft.deal ? (draft.deal.merchant || draft.deal.link_host || "") : "",
      deal_url: draft.dealUrl || "",
      sent_text: draft.text,
      has_image: hasPhoto,
      edit_rounds: draft.edits?.length || 0,
    });
    await env.DEALBOT.delete(`draft:${mid}`);
    return;
  }

  if (action === "discard") {
    await tg(env, "editMessageReplyMarkup", {
      chat_id: chatId, message_id: mid, reply_markup: { inline_keyboard: [] },
    });
    await tg(env, "sendMessage", { chat_id: chatId, text: "Discarded." });
    await env.DEALBOT.delete(`draft:${mid}`);
    return;
  }

  if (action === "edit") {
    if ((draft.edits || []).length >= MAX_EDIT_ROUNDS) {
      await tg(env, "sendMessage", {
        chat_id: chatId,
        text: `${MAX_EDIT_ROUNDS} edit rounds reached. Send it, discard it, or start over.`,
      });
      return;
    }
    await env.DEALBOT.put(`awaiting:${chatId}`, `draft:${mid}`, { expirationTtl: 3600 });
    await tg(env, "sendMessage", {
      chat_id: chatId,
      text:
        'What should change? Reply in plain language, e.g. "shorter" or "add the TCO math".\n\n' +
        '• To attach deal button: reply "link: https://..."\n' +
        '• To attach an image: send a photo directly, or reply "image: https://..."\n' +
        (draft.image ? '• To remove attached image: reply "remove image"\n' : ""),
    });
  }
}

async function handleDraftCommand(env, chatId, raw) {
  const body = raw.replace(/^\/draft(?:@\S+)?\s*/i, "").trim();
  if (!body) {
    await tg(env, "sendMessage", {
      chat_id: chatId,
      text:
        "Describe the deal and I'll write a post.\n\n" +
        "Example:\n/draft Amazon Prime 3 months for 4.99 instead of 26.97, " +
        "claim via the Discover Prime popup, ends June 26\nlink: https://www.amazon.de",
    });
    return;
  }

  const dealUrl = parseLinkLine(body);
  // Strip the link line so it doesn't end up in the prose.
  const description = body.replace(/^\s*(?:link|url)\s*:\s*\S+\s*$/im, "").trim();

  const deal = {
    title: description.split("\n")[0].slice(0, 200),
    description,
    link: "",
    merchant: null,
    link_host: dealUrl ? (() => { try { return new URL(dealUrl).hostname; } catch { return null; } })() : null,
    temperature: null,
    price: null,
    next_best_price: null,
    voucher_code: null,
    reason: "manual /draft",
  };

  await generateAndSendDraft(env, chatId, deal, [], dealUrl);
}

async function handleMissedCommand(env, chatId, raw) {
  const body = raw.replace(/^\/missed(?:@\S+)?\s*/i, "").trim();
  const m = body.match(URL_RE);

  if (!body) {
    await tg(env, "sendMessage", {
      chat_id: chatId,
      text:
        "Log a deal the filter should have caught.\n\n" +
        "/missed <url> <why it should have been picked>\n\n" +
        "These are the most valuable entries in the log — they're the only way " +
        "to see what the rules are wrongly rejecting.",
    });
    return;
  }

  const url = m ? m[0] : "";
  const reason = body.replace(URL_RE, "").replace(/^[\s—–-]+/, "").trim();

  await logDecision(env, {
    decision: "missed",
    title: "",
    link: url,
    my_reason: reason,
  });

  await tg(env, "sendMessage", {
    chat_id: chatId,
    text: reason
      ? "Logged as missed 📌 — this one will shape the next rules update."
      : "Logged as missed 📌. A reason would make it much more useful — send /missed again with one if you can.",
  });
}

async function handleMessage(env, msg) {
  const chatId = String(msg.chat.id);
  if (chatId !== String(env.TELEGRAM_ADMIN_CHAT_ID)) return;

  const hasPhoto = Array.isArray(msg.photo) && msg.photo.length > 0;
  const text = (msg.text || msg.caption || "").trim();
  if (!text && !hasPhoto) return;

  if (/^\/draft\b/i.test(text)) {
    await handleDraftCommand(env, chatId, text);
    return;
  }

  if (/^\/missed\b/i.test(text)) {
    await handleMissedCommand(env, chatId, text);
    return;
  }

  // /batch [n] — raw deals for rule training, no scoring, no posting
  if (/^\/batch\b/i.test(text)) {
    const n = Math.min(parseInt(text.split(/\s+/)[1], 10) || 20, 50);
    await tg(env, "sendMessage", { chat_id: chatId, text: `Fetching ${n} fresh deals…` });
    try {
      const sent = await sendBatch(env, chatId, n);
      await tg(env, "sendMessage", { chat_id: chatId, text: `Sent ${sent}.` });
    } catch (e) {
      await notifyOwner(env, `❌ Batch failed: ${e.message}`);
    }
    return;
  }

  if (/^\/status\b/i.test(text)) {
    const stats = await kvJson(env, "stats", {});
    const decisions = await kvJson(env, "decisions", []);
    await tg(env, "sendMessage", {
      chat_id: chatId,
      text:
        `Updates handled: ${stats.updateCount ?? 0}\n` +
        `Unsynced decisions: ${decisions.length}\n` +
        `Last update: ${stats.lastUpdate ?? "never"}\n` +
        `Last error: ${stats.lastError ?? "none"}`,
    });
    return;
  }

  if (/^\/help\b/i.test(text) || /^\/start\b/i.test(text)) {
    await tg(env, "sendMessage", {
      chat_id: chatId,
      text:
        "/draft <description> — write a post from free text\n" +
        "  add a line \"link: https://...\" for a Zum Deal button\n" +
        "/missed <url> <why> — log a deal the filter wrongly rejected\n" +
        "/batch [n] — raw deals with 👍/👎 for rule training (never posts)\n" +
        "/status — worker health",
    });
    return;
  }

  // Anything else: is it an answer to something I asked?
  const awaiting = await env.DEALBOT.get(`awaiting:${chatId}`);
  if (!awaiting) return;

  // Edit instruction, image, or link for a draft
  if (awaiting.startsWith("draft:")) {
    const draft = await kvJson(env, awaiting, null);
    if (!draft) {
      await env.DEALBOT.delete(`awaiting:${chatId}`);
      await notifyOwner(env, "That draft expired. Nothing was changed.");
      return;
    }

    const draftMid = awaiting.replace("draft:", "");

    // Text confirmation to shorten when pendingImage exists
    if (draft.pendingImage && /^\s*(?:shorten|condense|yes|ok)\b/i.test(text)) {
      await env.DEALBOT.delete(`awaiting:${chatId}`);
      await tg(env, "sendMessage", { chat_id: chatId, text: "Condensing post to < 1024 chars…" });
      try {
        const shortened = await generate(env, shortenPrompt(env, draft.text));
        const image = draft.pendingImage;
        delete draft.pendingImage;
        await env.DEALBOT.delete(awaiting);
        await tg(env, "editMessageReplyMarkup", {
          chat_id: chatId, message_id: draftMid, reply_markup: { inline_keyboard: [] },
        }).catch(() => {});
        await sendDraft(
          env,
          chatId,
          draft.deal,
          shortened,
          [...(draft.edits || []), "condensed < 1024 chars"],
          draft.dealUrl,
          image
        );
      } catch (e) {
        await notifyOwner(env, `❌ Condensing failed: ${e.message}`);
      }
      return;
    }

    // Command to remove image
    if (isRemoveImage(text)) {
      await env.DEALBOT.delete(`awaiting:${chatId}`);
      await env.DEALBOT.delete(awaiting);
      await tg(env, "editMessageReplyMarkup", {
        chat_id: chatId, message_id: draftMid, reply_markup: { inline_keyboard: [] },
      }).catch(() => {});
      await sendDraft(env, chatId, draft.deal, draft.text, draft.edits, draft.dealUrl, null);
      await tg(env, "sendMessage", { chat_id: chatId, text: "Image removed 🗑️" });
      return;
    }

    // Check for photo attachment (direct photo upload or URL)
    let photoFileId = null;
    if (hasPhoto) {
      const best = msg.photo[msg.photo.length - 1];
      photoFileId = best.file_id;
    }
    const imageUrl = parseImageLine(text);
    const newImage = photoFileId ? { photoFileId } : (imageUrl ? { imageUrl } : null);

    // Check for link button
    const link = parseLinkLine(text);
    if (link && !newImage) {
      await env.DEALBOT.delete(`awaiting:${chatId}`);
      await env.DEALBOT.delete(awaiting);
      await tg(env, "editMessageReplyMarkup", {
        chat_id: chatId, message_id: draftMid, reply_markup: { inline_keyboard: [] },
      }).catch(() => {});
      await sendDraft(env, chatId, draft.deal, draft.text, draft.edits, link, draft.image);
      return;
    }

    if (newImage) {
      let instruction = text;
      if (imageUrl) {
        instruction = text.replace(/^\s*(?:image|img|photo)\s*:\s*\S+/im, "").trim();
      }

      let currentText = draft.text;
      let currentEdits = draft.edits || [];

      if (instruction) {
        await tg(env, "sendMessage", { chat_id: chatId, text: "Revising text & attaching image…" });
        currentEdits = [...currentEdits, instruction];
        try {
          currentText = await generate(env, draftPrompt(env, draft.deal, currentEdits));
        } catch (e) {
          await notifyOwner(env, `❌ Revision failed: ${e.message}`);
          return;
        }
      }

      // Check caption length against Telegram 1024 limit
      if (currentText.length <= 1024) {
        await env.DEALBOT.delete(`awaiting:${chatId}`);
        await env.DEALBOT.delete(awaiting);
        await tg(env, "editMessageReplyMarkup", {
          chat_id: chatId, message_id: draftMid, reply_markup: { inline_keyboard: [] },
        }).catch(() => {});
        await sendDraft(
          env,
          chatId,
          draft.deal,
          currentText,
          currentEdits,
          draft.dealUrl,
          newImage
        );
        return;
      } else {
        // Exceeds 1024: store pendingImage and prompt owner to confirm shortening
        draft.text = currentText;
        draft.edits = currentEdits;
        draft.pendingImage = newImage;
        await env.DEALBOT.put(awaiting, JSON.stringify(draft), { expirationTtl: 60 * 60 * 24 * 7 });

        await tg(env, "sendMessage", {
          chat_id: chatId,
          text:
            `⚠️ <b>Post is too long for a photo caption</b> (${currentText.length} characters; Telegram caption limit is 1,024).\n\n` +
            `Would you like me to condense it to fit under 1,024 characters?`,
          parse_mode: "HTML",
          reply_markup: {
            inline_keyboard: [
              [
                { text: "✂️ Shorten to < 1024 chars", callback_data: `shorten:${draftMid}` },
                { text: "Keep text (drop image)", callback_data: `drop_img:${draftMid}` },
              ],
            ],
          },
        });
        return;
      }
    }

    // Plain text revision (no image provided this turn)
    if (text) {
      await env.DEALBOT.delete(`awaiting:${chatId}`);
      const edits = [...(draft.edits || []), text];
      await tg(env, "sendMessage", { chat_id: chatId, text: "Revising…" });
      try {
        const newText = await generate(env, draftPrompt(env, draft.deal, edits));

        // If draft previously had an image attached and the revised text now exceeds 1024
        if (draft.image && newText.length > 1024) {
          draft.text = newText;
          draft.edits = edits;
          draft.pendingImage = draft.image;
          delete draft.image;
          await env.DEALBOT.put(awaiting, JSON.stringify(draft), { expirationTtl: 60 * 60 * 24 * 7 });

          await tg(env, "sendMessage", {
            chat_id: chatId,
            text:
              `⚠️ <b>Revised post is too long for the attached photo</b> (${newText.length} characters; Telegram caption limit is 1,024).\n\n` +
              `Would you like me to condense it to fit under 1,024 characters?`,
            parse_mode: "HTML",
            reply_markup: {
              inline_keyboard: [
                [
                  { text: "✂️ Shorten to < 1024 chars", callback_data: `shorten:${draftMid}` },
                  { text: "Keep text (drop image)", callback_data: `drop_img:${draftMid}` },
                ],
              ],
            },
          });
          return;
        }

        await env.DEALBOT.delete(awaiting);
        await tg(env, "editMessageReplyMarkup", {
          chat_id: chatId, message_id: draftMid, reply_markup: { inline_keyboard: [] },
        }).catch(() => {});
        await sendDraft(
          env,
          chatId,
          draft.deal,
          newText,
          edits,
          draft.dealUrl,
          draft.image
        );
      } catch (e) {
        await notifyOwner(env, `❌ Revision failed, draft kept: ${e.message}`);
      }
      return;
    }
  }

  // Reason for a review decision or a batch 👎
  await env.DEALBOT.delete(`awaiting:${chatId}`);
  const deal = await kvJson(env, `pending:${awaiting}`, null);
  await logDecision(env, {
    decision: "reason",
    title: deal?.title ?? "",
    link: deal?.link ?? "",
    shop: deal ? (deal.merchant || deal.link_host || "") : "",
    my_reason: text,
  });
  await tg(env, "sendMessage", { chat_id: chatId, text: "Noted 👍" });
  await env.DEALBOT.delete(`pending:${awaiting}`);
}

// ---------- batch ----------

const stripTags = (s) =>
  (s || "").replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();

async function sendBatch(env, chatId, n) {
  const feeds = (env.FEED_URLS || "https://www.mydealz.de/rss/hot")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);

  const seen = new Set(await kvJson(env, "seen", []));
  const deals = [];
  const ids = new Set();

  for (const url of feeds) {
    const r = await fetch(url, { headers: { "User-Agent": "dealbot/1.0" } });
    if (!r.ok) continue;
    const xml = await r.text();

    for (const m of xml.matchAll(/<item>([\s\S]*?)<\/item>/g)) {
      const item = m[1];
      const pick = (tag) => {
        const t = item.match(new RegExp(`<${tag}[^>]*>([\\s\\S]*?)</${tag}>`));
        return t ? stripTags(t[1].replace(/<!\[CDATA\[|\]\]>/g, "")) : "";
      };
      const link = pick("link");
      const guid = pick("guid") || link;
      if (!guid || seen.has(guid) || ids.has(guid)) continue;
      ids.add(guid);

      const title = pick("title");
      if (!title) continue;
      const tempMatch = title.match(/(\d[\d.]*)\s*°/);

      deals.push({
        id: guid,
        title,
        link,
        description: pick("description").slice(0, 300),
        temperature: tempMatch ? tempMatch[1].replace(/\./g, "") : null,
        merchant: null,
        link_host: null,
      });
      if (deals.length >= n) break;
    }
    if (deals.length >= n) break;
  }

  for (const deal of deals) {
    const r = await tg(env, "sendMessage", {
      chat_id: chatId,
      text: `<b>${esc(deal.title)}</b>\n🌡 ${deal.temperature ?? "n/a"}\n${esc(deal.link)}`,
      parse_mode: "HTML",
      link_preview_options: { is_disabled: true },
      reply_markup: BATCH_KB,
    });
    const mid = (await r.json().catch(() => null))?.result?.message_id;
    if (mid) {
      await env.DEALBOT.put(`pending:${mid}`, JSON.stringify(deal), {
        expirationTtl: 60 * 60 * 24 * 3,
      });
    }
    seen.add(deal.id);
  }

  await env.DEALBOT.put("seen", JSON.stringify([...seen].slice(-3000)));
  return deals.length;
}

// ---------- entry ----------

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (url.pathname === "/health") {
      const stats = await kvJson(env, "stats", {});
      const decisions = await kvJson(env, "decisions", []);
      return Response.json({
        ok: true,
        lastUpdate: stats.lastUpdate ?? null,
        updateCount: stats.updateCount ?? 0,
        unsyncedDecisions: decisions.length,
        lastError: stats.lastError ?? null,
      });
    }

    if (url.pathname === "/webhook" && request.method === "POST") {
      if (
        request.headers.get("X-Telegram-Bot-Api-Secret-Token") !== env.WEBHOOK_SECRET
      ) {
        return new Response("forbidden", { status: 403 });
      }

      const update = await request.json().catch(() => null);
      // Always 200 quickly — Telegram retries non-200 and will double-process.
      if (!update) return new Response("ok");

      try {
        if (update.callback_query) {
          if (
            String(update.callback_query.from.id) === String(env.TELEGRAM_ADMIN_CHAT_ID)
          ) {
            await handleCallback(env, update.callback_query);
          }
        } else if (update.message) {
          await handleMessage(env, update.message);
        }
        const stats = await kvJson(env, "stats", {});
        await env.DEALBOT.put(
          "stats",
          JSON.stringify({
            lastUpdate: new Date().toISOString(),
            updateCount: (stats.updateCount ?? 0) + 1,
            lastError: stats.lastError ?? null,
          })
        );
      } catch (e) {
        console.error(e);
        const stats = await kvJson(env, "stats", {});
        await env.DEALBOT.put(
          "stats",
          JSON.stringify({ ...stats, lastError: `${new Date().toISOString()} ${e.message}` })
        );
      }

      return new Response("ok");
    }

    return new Response("not found", { status: 404 });
  },
};
