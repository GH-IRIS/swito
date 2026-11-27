# Swito — Zomato & Swiggy Deal Alert Bot (GitHub Actions Live)

Cloud-first Telegram alert bot for **BTM 2nd Stage, Bangalore** (and configurable areas). Runs on **GitHub Actions** on a 5-minute schedule with zero local PC dependencies—alerts continue even when your computer is turned off.

## Features
- **Live Zomato Delivery Deals:** Polls BTM, Koramangala, HSR, and JP Nagar delivery offers (`bulkOffers` & `gold`) without stale browser cookies.
- **Live Swiggy Offers:** Polls Swiggy `offers-near-me` + `restaurants/list/v5` fallback.
- **Smart Deduplication:** Persists alerted deals (`12h` TTL) and Telegram `last_update_id` in GitHub Actions cache (`state/bot_state.json`).
- **Telegram Commands:** `/deals`, `/zomato`, `/swiggy`, `/status`, `/help`, `/test_rescue`, `/test_swiggy`.

## Enable Live Alerts on GitHub (`GH-IRIS/swito`)

1. **Make sure this repository is Private** (recommended).
2. **Add Repository Secrets** under **Settings → Secrets and variables → Actions → New repository secret**:
   - `TELEGRAM_BOT_TOKEN` — Your bot token from `@BotFather`
   - `TELEGRAM_CHAT_ID` — Your Telegram numeric chat ID (e.g., from `@userinfobot`)
   - *(Optional)* `PROXY_URL` — If GitHub's cloud runner IPs are ever rate-limited by Zomato/Swiggy
3. **Optional Repository Variables** (**Settings → Secrets and variables → Actions → Variables**):
   - `TARGET_LOCATION` (default: `BTM 2nd Stage, Bangalore`)
   - `MIN_PERCENT_OFF` (default: `40`)
   - `MIN_FLAT_OFF` (default: `100`)
4. **Trigger & Verify:**
   - Open the **Actions** tab → **Food Deals & Rescue Alerts** → **Run workflow**.
   - After that, GitHub Actions runs automatically every ~5 minutes (`cron: "*/5 * * * *"`).

## Local Testing (Optional)

```powershell
$env:TELEGRAM_BOT_TOKEN="<your-token>"
$env:TELEGRAM_CHAT_ID="<your-chat-id>"
python -m unittest -v test_bot.py
python bot.py --once
```
