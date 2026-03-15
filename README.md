# HDFC Expense Tracker — Telegram Bot

Track all your HDFC Bank expenses (UPI, Debit Card, Credit Card) automatically. HDFC sends you email alerts → this bot parses them with an LLM → you get rich dashboards in Telegram.

```
HDFC Bank Email Alerts
        │
        ▼
┌──────────────────┐
│  email_fetcher.py │ ← Polls Gmail via IMAP every 60s
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│     bot.py        │ ← Telegram bot (commands + message parser)
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│   SQLite DB       │ → /summary, /category, /daily, /monthly, /export
└──────────────────┘
```

---

## Prerequisites

- Docker + Docker Compose
- A Telegram bot token (from [@BotFather](https://t.me/BotFather))
- Your Telegram user ID (from [@userinfobot](https://t.me/userinfobot))
- A Gmail address that receives HDFC Bank alerts
- A Gmail [App Password](https://myaccount.google.com/apppasswords) (requires 2FA)
- A free [Groq API key](https://console.groq.com) (for LLM email parsing)

---

## Quick Start (Docker)

```bash
# 1. Clone and configure
git clone https://github.com/your-username/hdfc-expense-bot.git
cd hdfc-expense-bot
make setup          # creates .env from .env.example

# 2. Edit .env with your credentials
#    Required: BOT_TOKEN, CHAT_ID, EMAIL_ADDRESS, EMAIL_PASSWORD, GROQ_API_KEY

# 3. Start both services
make up
make logs           # watch for errors
```

Both the bot and the email fetcher start automatically and restart on failure.

---

## Backfill Historical Data

Import all your past HDFC emails before using the bot day-to-day:

```bash
# Dry run first — see what would be imported
make backfill -- --dry-run --verbose

# Import everything
make backfill -- --user-id YOUR_TELEGRAM_CHAT_ID

# Import only the last 12 months
make backfill -- --since 2025-03-01 --user-id YOUR_TELEGRAM_CHAT_ID
```

The backfill script scans `[Gmail]/All Mail` (catches archived emails), deduplicates using fingerprints, and is safe to run multiple times.

---

## Bot Commands

| Command | Description |
|---------|-------------|
| `/start` | Welcome message and help |
| `/summary` | This month's overview (totals, by mode, by category, top transactions) |
| `/summary week` | Last 7 days |
| `/summary 2025-01-01 2025-03-31` | Custom date range |
| `/monthly` | Stacked bar chart by month |
| `/category` | Pie chart by category (this month) |
| `/daily` | Daily spending bar chart (last 30 days) |
| `/recent` | Last 10 transactions |
| `/search keyword` | Find transactions by merchant or category |
| `/drilldown category` | Pareto breakdown of a category |
| `/addrule keyword Category` | Add an auto-categorize rule |
| `/export` | Download all data as CSV |

---

## Manual (systemd) Deployment

For VPS deployments without Docker (e.g. Oracle Cloud Free Tier):

```bash
# 1. Run the server setup script (once, as root)
sudo bash deploy/init-server.sh

# 2. Clone the repo and set up Python
git clone https://github.com/your-username/hdfc-expense-bot.git /opt/hdfc-expense-bot
cd /opt/hdfc-expense-bot
python3 -m venv venv
venv/bin/pip install -r requirements.txt
cp .env.example .env
# edit .env with your credentials

# 3. Install and start the systemd services
sudo cp deploy/hdfc-bot.service /etc/systemd/system/
sudo cp deploy/hdfc-fetcher.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hdfc-bot hdfc-fetcher

# Check status
sudo systemctl status hdfc-bot hdfc-fetcher
journalctl -u hdfc-bot -f
```

---

## Configuration Reference

| Variable | Description | Default |
|----------|-------------|---------|
| `BOT_TOKEN` | Telegram bot token from @BotFather | — |
| `CHAT_ID` | Your Telegram user ID | — |
| `EMAIL_ADDRESS` | Gmail address receiving HDFC alerts | — |
| `EMAIL_PASSWORD` | Gmail App Password | — |
| `IMAP_SERVER` | IMAP server hostname | `imap.gmail.com` |
| `IMAP_PORT` | IMAP SSL port | `993` |
| `POLL_INTERVAL` | Email polling interval (seconds) | `60` |
| `LLM_PROVIDER` | `groq` (cloud) or `ollama` (local) | `groq` |
| `GROQ_API_KEY` | Groq Cloud API key | — |
| `GROQ_MODEL` | Groq model for parsing | `llama-3.1-8b-instant` |
| `OLLAMA_HOST` | Ollama server URL | `http://localhost:11434` |
| `OLLAMA_MODEL` | Ollama model name | `llama3.2:3b` |
| `DB_PATH` | SQLite database path | `/data/expenses.db` |

---

## Troubleshooting

**Bot not responding?**
- Check `make logs` — look for errors in the `bot` container
- Verify `BOT_TOKEN` and `CHAT_ID` are set correctly in `.env`

**Email fetcher not picking up emails?**
- Ensure IMAP is enabled in Gmail → Settings → Forwarding and POP/IMAP
- Use an App Password, not your regular Gmail password (requires 2FA)
- Check `make logs` for IMAP login errors

**LLM parsing fails?**
- Verify `GROQ_API_KEY` is valid at [console.groq.com](https://console.groq.com)
- For local use, set `LLM_PROVIDER=ollama` and ensure Ollama is running

**Charts not rendering?**
- The bot uses matplotlib's `Agg` backend — no display needed on servers

**Data not showing in `/summary`?**
- Run backfill with your real `--user-id` (your Telegram Chat ID)
- Transactions stored without a user ID won't appear in bot commands
