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
  if (!r.ok) console.error(`${method} failed:`, await r.text());
  return r;
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

const REVIEW_KB = {
  inline_keyboard: [[
    { text: "✅ Post", callback_data: "post" },
    { text: "❌ Skip", callback_data: "skip" },
  ]],
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
        generationConfig: { temperature: 0.4 },
        "thinkingConfig": {"thinkingLevel": "low"},
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
Link: ${deal.link}
Temperature: ${deal.temperature ?? "n/a"}
Description: ${deal.summary ?? ""}
Why it was selected: ${deal.reason ?? ""}
</deal>
${editBlock}
Rules:
- English block first, then the separator line, then the full Persian translation.
- Persian numerals in the Persian block. Keep links in Latin script.
- If a detail is not in the deal data, leave that line out. Never invent prices,
  dates, or discount percentages.
- Output ONLY the post text. No preamble, no markdown fences.
- Use Telegram HTML: <b>, <i>, <a href="">. No other tags.`;
}

async function sendDraft(env, chatId, deal, text, edits) {
  if (text.length > TG_LIMIT) {
    await tg(env, "sendMessage", {
      chat_id: chatId,
      text: `Draft is ${text.length} chars, over Telegram's ${TG_LIMIT} limit. Ask for "shorter".`,
    });
    return;
  }
  const r = await tg(env, "sendMessage", {
    chat_id: chatId,
    text,
    parse_mode: "HTML",
    link_preview_options: { is_disabled: true },
    reply_markup: DRAFT_KB,
  });
  const body = await r.json().catch(() => null);
  const mid = body?.result?.message_id;
  if (mid) {
    await env.DEALBOT.put(
      `draft:${mid}`,
      JSON.stringify({ deal, text, edits }),
      { expirationTtl: 60 * 60 * 24 * 7 }
    );
  }
}

// ---------- handlers ----------

async function handleCallback(env, cb) {
  const chatId = cb.message.chat.id;
  const mid = cb.message.message_id;
  const action = cb.data;

  await tg(env, "answerCallbackQuery", { callback_query_id: cb.id });

  // --- review stage: post or skip a candidate ---
  if (action === "post" || action === "skip") {
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
      model_reason: deal.reason ?? "",
    });

    if (action === "post") {
      await tg(env, "sendMessage", { chat_id: chatId, text: "Writing a draft…" });
      try {
        const text = await generate(env, draftPrompt(env, deal, []));
        await sendDraft(env, chatId, deal, text, []);
      } catch (e) {
        await tg(env, "sendMessage", { chat_id: chatId, text: `Draft failed: ${e.message}` });
      }
      await env.DEALBOT.delete(`pending:${mid}`);
    } else {
      await env.DEALBOT.put(`awaiting:${chatId}`, String(mid), { expirationTtl: 3600 });
      await tg(env, "sendMessage", {
        chat_id: chatId,
        text: "Skipped. Reply here with a reason if you want (optional).",
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
      model_reason: "",
    });
    if (action === "batch_down") {
      await env.DEALBOT.put(`awaiting:${chatId}`, String(mid), { expirationTtl: 3600 });
    }
    return;
  }

  // --- draft stage: send, edit, discard ---
  const draft = await kvJson(env, `draft:${mid}`, null);
  if (!draft) return;

  if (action === "send") {
    await tg(env, "sendMessage", {
      chat_id: env.TELEGRAM_CHANNEL_ID,
      text: draft.text,
      parse_mode: "HTML",
      link_preview_options: { is_disabled: true },
    });
    await tg(env, "editMessageReplyMarkup", {
      chat_id: chatId, message_id: mid, reply_markup: { inline_keyboard: [] },
    });
    await tg(env, "sendMessage", { chat_id: chatId, text: "Posted ✅" });
    await logDecision(env, {
      decision: "sent",
      title: draft.deal.title,
      link: draft.deal.link,
      sent_text: draft.text,
      edit_rounds: draft.edits.length,
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
    if (draft.edits.length >= MAX_EDIT_ROUNDS) {
      await tg(env, "sendMessage", {
        chat_id: chatId,
        text: `${MAX_EDIT_ROUNDS} edit rounds reached. Send it, discard it, or start over.`,
      });
      return;
    }
    await env.DEALBOT.put(`awaiting:${chatId}`, `draft:${mid}`, { expirationTtl: 3600 });
    await tg(env, "sendMessage", {
      chat_id: chatId,
      text: 'What should change? Reply in plain language, e.g. "shorter" or "add the TCO math".',
    });
  }
}

async function handleMessage(env, msg) {
  const chatId = String(msg.chat.id);
  if (chatId !== String(env.TELEGRAM_ADMIN_CHAT_ID)) return;
  const text = (msg.text || "").trim();
  if (!text) return;

  // /batch [n] — raw deals for rule training, no scoring, no posting
  if (text.startsWith("/batch")) {
    const n = Math.min(parseInt(text.split(/\s+/)[1], 10) || 20, 50);
    await tg(env, "sendMessage", { chat_id: chatId, text: `Fetching ${n} fresh deals…` });
    try {
      const sent = await sendBatch(env, chatId, n);
      await tg(env, "sendMessage", { chat_id: chatId, text: `Sent ${sent}.` });
    } catch (e) {
      await tg(env, "sendMessage", { chat_id: chatId, text: `Batch failed: ${e.message}` });
    }
    return;
  }

  if (text === "/status") {
    const stats = await kvJson(env, "stats", {});
    const decisions = await kvJson(env, "decisions", []);
    await tg(env, "sendMessage", {
      chat_id: chatId,
      text: `Updates handled: ${stats.updateCount ?? 0}\nUnsynced decisions: ${decisions.length}\nLast error: ${stats.lastError ?? "none"}`,
    });
    return;
  }

  // Anything else: is it an answer to something I asked?
  const awaiting = await env.DEALBOT.get(`awaiting:${chatId}`);
  if (!awaiting) return;
  await env.DEALBOT.delete(`awaiting:${chatId}`);

  // Edit instruction for a draft
  if (awaiting.startsWith("draft:")) {
    const draft = await kvJson(env, awaiting, null);
    if (!draft) return;
    const edits = [...draft.edits, text];
    await tg(env, "sendMessage", { chat_id: chatId, text: "Revising…" });
    try {
      const newText = await generate(env, draftPrompt(env, draft.deal, edits));
      await sendDraft(env, chatId, draft.deal, newText, edits);
      await env.DEALBOT.delete(awaiting);
    } catch (e) {
      await tg(env, "sendMessage", { chat_id: chatId, text: `Revision failed: ${e.message}` });
    }
    return;
  }

  // Reason for a skip or a 👎
  const deal = await kvJson(env, `pending:${awaiting}`, null);
  await logDecision(env, {
    decision: "reason",
    title: deal?.title ?? "",
    link: deal?.link ?? "",
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
        summary: pick("description").slice(0, 300),
        temperature: tempMatch ? tempMatch[1].replace(/\./g, "") : null,
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
