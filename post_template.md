# Telegram post template

The draft generator uses this as a few-shot example. Edit it freely — this file
is the source of truth for how posts look.

Note: this is also pasted into the Worker's `POST_TEMPLATE` secret. When you
change this file, re-run `npx wrangler secret put POST_TEMPLATE`.

---

## Structure

```
{EMOJI} {HEADLINE IN CAPS} {EMOJI}

{one line: what it is, old price -> new price, % off}

{optional one line: why it matters right now}

🎯 How to get it:
✅ {step}
✅ {step}
✅ {step}

💡 {optional: worked example, or the total-cost math}

⚠️ {optional: the catch, e.g. cancel before auto-renewal}

⏰ Valid until: {date, time}

{#hashtags}
@berliner_deals
━━━━━━━━━━━━━━━━━━━━━━

{SAME POST IN PERSIAN}

@berliner_deals
```

## Rules

- English block first, separator line, then the full Persian translation.
- Persian block uses Persian numerals. Links stay in Latin script.
- Telegram HTML only: `<b>`, `<i>`, `<a href="">`. No markdown.
- Never invent a price, date, or discount percentage. Omit the line instead.
- For mobile contracts, always show the total-cost arithmetic explicitly.

---

## Example 1 — subscription offer

```
📦 AMAZON PRIME: 3 MONTHS FOR €4.99 📦

Get 3 months of Prime for €4.99 instead of €26.97, over 80% off.

🚀 Good timing for Prime Day: free shipping plus Prime-only deals.

🎯 How to get it:
✅ Go to amazon.de
✅ Click "Discover Prime" in the top menu
✅ Claim the €4.99 promo in the popup

⏰ Valid until: June 26, 23:59

#Amazon #PrimeDay
@berliner_deals
━━━━━━━━━━━━━━━━━━━━━━

📦 ۳ ماه آمازون پرایم فقط ۴.۹۹ یورو 📦

۳ ماه اشتراک پرایم با ۴.۹۹ یورو به جای ۲۶.۹۷ یورو، بیش از ۸۰٪ تخفیف.

🚀 زمان مناسب برای پرایم‌دی: ارسال رایگان و تخفیف‌های ویژه.

🎯 مراحل دریافت:
✅ ورود به amazon.de
✅ کلیک روی «Discover Prime» در منوی بالا
✅ فعال‌سازی آفر ۴.۹۹ یورویی در پاپ‌آپ

⏰ مهلت: تا ۲۶ ژوئن، ساعت ۲۳:۵۹

#آمازون #پرایم
@berliner_deals
```

## Example 2 — mobile contract, with the TCO math

```
🔥 iPHONE 17 PRO + TELEKOM UNLIMITED 🍎

📱 iPhone 17 Pro 256GB with unlimited 5G

💰 The math:
• €49.95/month × 24 = €1,198.80
• Upfront: €399
• Minus €350 in bonus and cashback
• Total: €1,247.80

📊 Why it's good:
The phone alone retails at €1,199. You get it plus 24 months of unlimited data
for €48.80 more.

📶 Includes:
• ✅ Allnet and SMS flat
• ✅ Unlimited 5G
• ✅ Roaming in 🇨🇭

⚠️ Cancel before the contract auto-renews.

@berliner_deals
━━━━━━━━━━━━━━━━━━━━━━

🔥 آیفون ۱۷ پرو + تلکوم نامحدود 🍎

📱 آیفون ۱۷ پرو ۲۵۶ گیگابایت با اینترنت نامحدود ۵G

💰 محاسبه هزینه:
• ۴۹.۹۵€ × ۲۴ = ۱٬۱۹۸.۸۰€
• پیش‌پرداخت: ۳۹۹€
• منهای ۳۵۰€ بونوس و کش‌بک
• جمع کل: ۱٬۲۴۷.۸۰€

📊 چرا خوبه:
خود گوشی ۱٬۱۹۹€ قیمت داره. با فقط ۴۸.۸۰€ بیشتر، گوشی به‌علاوه ۲۴ ماه
اینترنت نامحدود می‌گیرید.

📶 شامل:
• ✅ تماس و پیامک نامحدود
• ✅ اینترنت نامحدود ۵G
• ✅ رومینگ در سوئیس 🇨🇭

⚠️ قبل از تمدید خودکار، قرارداد را لغو کنید.

@berliner_deals
```
