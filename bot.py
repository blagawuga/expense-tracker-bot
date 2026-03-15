"""
HDFC Expense Tracker Telegram Bot
──────────────────────────────────
Parses forwarded HDFC Bank email alerts (UPI, Debit Card, Credit Card),
stores transactions in SQLite, and provides dashboarding commands.

Setup:
  1. Create a bot via @BotFather on Telegram → get your BOT_TOKEN
  2. Set up email forwarding (see README.md)
  3. pip install python-telegram-bot matplotlib pandas
  4. python bot.py
"""

import json
import os
import re
import sqlite3
import logging
import threading
from datetime import datetime, timedelta
from io import BytesIO
from functools import wraps

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import pandas as pd

from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, filters
)

# ──────────────────────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────────────────────
BOT_TOKEN   = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
DB_PATH     = os.environ.get("DB_PATH", "expenses.db")
ALLOWED_UID = int(os.environ.get("CHAT_ID", "0"))

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────
# AUTH
# ──────────────────────────────────────────────────────────────

def restricted(func):
    @wraps(func)
    async def wrapper(update, context, *args, **kwargs):
        if update.effective_user.id != ALLOWED_UID:
            logger.warning(f"Blocked unauthorised user {update.effective_user.id}")
            return
        return await func(update, context, *args, **kwargs)
    return wrapper


# ──────────────────────────────────────────────────────────────
# DATABASE
# ──────────────────────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            amount      REAL NOT NULL,
            txn_type    TEXT NOT NULL,       -- DEBIT / CREDIT
            mode        TEXT,               -- Credit Card / Debit Card / UPI / UPI Mandate / NEFT / IMPS / Auto Debit
            merchant    TEXT,
            category    TEXT DEFAULT 'Uncategorized',
            card_last4  TEXT,
            ref_no      TEXT,
            txn_date    TEXT NOT NULL,       -- ISO date
            fingerprint TEXT,               -- dedup hash
            created_at  TEXT DEFAULT (datetime('now'))
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_fingerprint ON transactions(fingerprint)")
    c.execute("""
        CREATE TABLE IF NOT EXISTS category_rules (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            keyword     TEXT NOT NULL,
            category    TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()

def _resolve_date_range(args: list[str], now: datetime) -> tuple[str, str, str] | None:
    """
    Parse date range from command args. Supports:
      today                     → today only
      yesterday                 → yesterday only
      week                      → last 7 days
      month                     → this calendar month
      year                      → this calendar year
      YYYY-MM-DD                → from that date to today
      YYYY-MM-DD  YYYY-MM-DD    → explicit range
    Returns (date_from, date_to, label) or None on parse error.
    """
    today     = now.strftime("%Y-%m-%d")
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")

    TAGS = {
        "today":     (today,                          today,  "Today"),
        "yesterday": (yesterday,                      yesterday, "Yesterday"),
        "week":      ((now - timedelta(days=6)).strftime("%Y-%m-%d"), today, "Last 7 days"),
        "month":     (now.strftime("%Y-%m-01"),       today,  now.strftime("%B %Y")),
        "year":      (now.strftime("%Y-01-01"),       today,  now.strftime("%Y")),
    }

    if not args:
        return now.strftime("%Y-%m-01"), today, now.strftime("%B %Y")

    if len(args) == 1:
        tag = args[0].lower()
        if tag in TAGS:
            return TAGS[tag]
        try:
            d = datetime.strptime(args[0], "%Y-%m-%d").strftime("%Y-%m-%d")
            return d, today, f"{args[0]} → today"
        except ValueError:
            return None

    if len(args) == 2:
        def resolve(s):
            t = s.lower()
            if t in TAGS:
                return TAGS[t][0]   # use the "from" date of the tag
            return datetime.strptime(s, "%Y-%m-%d").strftime("%Y-%m-%d")
        try:
            d_from = resolve(args[0])
            d_to   = resolve(args[1])
            return d_from, d_to, f"{d_from} → {d_to}"
        except ValueError:
            return None

    return None


async def _send_long(update, text: str, parse_mode="Markdown", limit=4096):
    """Split text on newlines and send in ≤limit char chunks."""
    lines = text.split("\n")
    chunk = ""
    for line in lines:
        candidate = chunk + ("\n" if chunk else "") + line
        if len(candidate) > limit:
            await update.message.reply_text(chunk, parse_mode=parse_mode)
            chunk = line
        else:
            chunk = candidate
    if chunk:
        await update.message.reply_text(chunk, parse_mode=parse_mode)


def _md(text: str) -> str:
    """Escape special characters for Telegram legacy Markdown."""
    return str(text).replace("_", "\\_").replace("*", "\\*").replace("`", "\\`").replace("[", "\\[")


def _txn_fingerprint(body_text: str) -> str:
    """Hash of the raw email body — pure function, no LLM dependency."""
    import hashlib
    return hashlib.sha256(body_text.encode()).hexdigest()[:16]

def add_transaction(user_id, amount, txn_type, mode, merchant, card_last4, ref_no, txn_date, body_text):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # Dedup check
    fp = _txn_fingerprint(body_text)
    c.execute("SELECT id FROM transactions WHERE fingerprint = ? LIMIT 1", (fp,))
    if c.fetchone():
        conn.close()
        return None, None  # duplicate

    # Auto-categorize
    category = auto_categorize(c, user_id, merchant)
    c.execute("""
        INSERT INTO transactions (user_id, amount, txn_type, mode, merchant, category, card_last4, ref_no, txn_date, fingerprint)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (user_id, amount, txn_type, mode, merchant, category, card_last4, ref_no, txn_date, fp))
    conn.commit()
    txn_id = c.lastrowid
    conn.close()
    return txn_id, category

def auto_categorize(cursor, user_id, merchant):
    if not merchant:
        return "Uncategorized"
    cursor.execute(
        "SELECT keyword, category FROM category_rules WHERE user_id = ? ORDER BY id",
        (user_id,)
    )
    rules = cursor.fetchall()
    merchant_lower = merchant.lower()

    # Built-in rules
    builtin = {
        "swiggy": "Food & Dining", "zomato": "Food & Dining",
        "uber": "Transport", "ola": "Transport", "rapido": "Transport",
        "amazon": "Shopping", "flipkart": "Shopping", "myntra": "Shopping",
        "bigbasket": "Groceries", "blinkit": "Groceries", "zepto": "Groceries",
        "jiomart": "Groceries", "dmart": "Groceries",
        "netflix": "Entertainment", "hotstar": "Entertainment", "spotify": "Entertainment",
        "airtel": "Utilities", "jio": "Utilities", "vi ": "Utilities",
        "electricity": "Utilities", "water bill": "Utilities", "gas bill": "Utilities",
        "petrol": "Fuel", "fuel": "Fuel", "indian oil": "Fuel", "hp ": "Fuel", "bpcl": "Fuel",
        "pharmacy": "Health", "apollo": "Health", "medplus": "Health",
        "irctc": "Travel", "makemytrip": "Travel", "goibibo": "Travel",
    }

    # User rules first (higher priority)
    for kw, cat in rules:
        if kw.lower() in merchant_lower:
            return cat

    for kw, cat in builtin.items():
        if kw in merchant_lower:
            return cat

    return "Uncategorized"


# ──────────────────────────────────────────────────────────────
# HDFC EMAIL PARSER — LLM-powered (Ollama or Groq)
# Set LLM_PROVIDER=ollama (backfill on Mac)
#     LLM_PROVIDER=groq  (email fetcher on Oracle Cloud)
# ──────────────────────────────────────────────────────────────

LLM_PROVIDER   = os.environ.get("LLM_PROVIDER", "groq")   # "ollama" or "groq"
OLLAMA_HOST    = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL   = os.environ.get("OLLAMA_MODEL", "llama3.2:3b")
GROQ_API_KEY   = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL     = os.environ.get("GROQ_MODEL", "llama-3.1-8b-instant")

PARSE_PROMPT = """You are a bank transaction email parser for HDFC Bank India.

Extract transaction details from the email text and return a JSON object.

Rules:
- amount: transaction amount as a number. null if not a transaction.
- txn_type: "DEBIT" if money left the account, "CREDIT" if money came in.
- mode: payment method — one of: "Credit Card", "Debit Card", "UPI", "UPI Mandate", "NEFT", "IMPS", "Auto Debit". null if unknown.
- merchant: short clean merchant/person name. For UPI, extract the READABLE NAME that appears AFTER the VPA handle (after the @domain part). E.g. "q889129699@ybl TAMANNA KHATUN" → merchant is "Tamanna Khatun". For cards, use the merchant name. null if truly unknown.
- card_last4: last 4 digits of card/account if mentioned, else null.
- ref_no: transaction reference number if present, else null.
- txn_date: date in YYYY-MM-DD format. Dates like "10-03-26" mean 2026-03-10. null if not found.
- is_transaction: true only for real money movements. false for OTP, login alerts, promo, mandate setup.

Return ONLY valid JSON, no explanation, no markdown.

Examples:
Input: "Rs.150.00 has been debited from account 0139 to VPA ibkpos.ep191406@icici M S ABC OUTMEDIA on 10-03-26. UPI ref 600661287781"
Output: {"amount": 150.0, "txn_type": "DEBIT", "mode": "UPI", "merchant": "M S ABC Outmedia", "card_last4": "0139", "ref_no": "600661287781", "txn_date": "2026-03-10", "is_transaction": true}

Input: "Thank you for using your HDFC Bank Credit Card ending 9407 for Rs 963.00 at SWIGGY on 04-10-2022"
Output: {"amount": 963.0, "txn_type": "DEBIT", "mode": "Credit Card", "merchant": "Swiggy", "card_last4": "9407", "ref_no": null, "txn_date": "2022-10-04", "is_transaction": true}

Input: "Rs.120.00 has been debited from account 0139 to VPA q889129699@ybl TAMANNA KHATUN on 11-03-26. UPI ref 398758306583"
Output: {"amount": 120.0, "txn_type": "DEBIT", "mode": "UPI", "merchant": "Tamanna Khatun", "card_last4": "0139", "ref_no": "398758306583", "txn_date": "2026-03-11", "is_transaction": true}

Email:
{email_text}"""


def _build_prompt(email_snippet: str) -> str:
    return PARSE_PROMPT.replace("{email_text}", email_snippet)


def _call_ollama(email_snippet: str) -> str:
    import requests as req
    response = req.post(
        f"{OLLAMA_HOST}/api/chat",
        json={
            "model": OLLAMA_MODEL,
            "messages": [{"role": "user", "content": _build_prompt(email_snippet)}],
            "stream": False,
            "format": "json",
        },
        timeout=60,
    )
    response.raise_for_status()
    return response.json()["message"]["content"]


_groq_local = threading.local()

def _get_groq_client():
    if not hasattr(_groq_local, "client"):
        from groq import Groq
        _groq_local.client = Groq()
    return _groq_local.client


def _call_groq(email_snippet: str) -> str:
    client = _get_groq_client()
    response = client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[{"role": "user", "content": _build_prompt(email_snippet)}],
        max_completion_tokens=256,
        temperature=0,
        stream=False,
        response_format={"type": "json_object"},
    )
    return response.choices[0].message.content


def _clean_email(text: str) -> str:
    """Strip CSS/HTML noise and extract only human-readable content."""
    import re
    # Remove everything before Dear (CSS, media queries etc.)
    m = re.search(r"(Dear\s+(?:Customer|Card\s+Member|Cardholder).*)", text, re.IGNORECASE | re.DOTALL)
    if m:
        text = m.group(1)
    # Strip any remaining HTML tags
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def parse_hdfc_email(text: str) -> dict | None:
    """
    Parse HDFC Bank email using LLM (Ollama locally or Groq on cloud).
    Returns dict with: amount, txn_type, merchant, card_last4, ref_no, txn_date
    or None if not a transaction email.
    """
    try:
        email_snippet = _clean_email(text)[:800]
        if LLM_PROVIDER == "ollama":
            raw = _call_ollama(email_snippet)
        else:
            raw = _call_groq(email_snippet)
        # Strip markdown code fences if model ignores format instruction
        if raw.startswith("```"):
            raw = re.sub(r"^```[a-z]*\n?", "", raw)
            raw = re.sub(r"\n?```$", "", raw)

        data = json.loads(raw)

        if not isinstance(data, dict):
            logger.error(f"LLM returned non-dict: {raw[:200]}")
            return None
        if not data.get("is_transaction"):
            return None
        if not data.get("amount"):
            return None
        return {
            "amount": float(str(data["amount"]).replace(",", "")),
            "txn_type": data.get("txn_type", "DEBIT"),
            "mode": data.get("mode"),
            "merchant": data.get("merchant"),
            "card_last4": data.get("card_last4"),
            "ref_no": data.get("ref_no"),
            "txn_date": data.get("txn_date") or datetime.now().strftime("%Y-%m-%d"),
        }

    except Exception as e:
        logger.error(f"LLM parser error ({LLM_PROVIDER}): {e}")
        try:
            logger.error(f"  Raw response was: {raw[:300]}")
        except Exception:
            pass
        return None


# ──────────────────────────────────────────────────────────────
# CHART HELPERS
# ──────────────────────────────────────────────────────────────

MODE_EMOJI = {
    "Credit Card": "💳",
    "Debit Card":  "🏦",
    "UPI":         "📱",
    "UPI Mandate": "🔁",
    "NEFT":        "🏛",
    "IMPS":        "⚡",
    "Auto Debit":  "🔄",
}

CHART_COLORS = [
    "#6C5CE7", "#00B894", "#FD79A8", "#FDCB6E",
    "#0984E3", "#E17055", "#00CEC9", "#D63031",
    "#A29BFE", "#55EFC4", "#FAB1A0", "#74B9FF",
]

def _styled_fig(figsize=(8, 5)):
    fig, ax = plt.subplots(figsize=figsize, facecolor="#1a1a2e")
    ax.set_facecolor("#16213e")
    ax.tick_params(colors="#e0e0e0", labelsize=9)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_color("#333")
    ax.spines["bottom"].set_color("#333")
    ax.xaxis.label.set_color("#e0e0e0")
    ax.yaxis.label.set_color("#e0e0e0")
    ax.title.set_color("#ffffff")
    return fig, ax

def _fig_to_bytes(fig) -> BytesIO:
    buf = BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight",
                facecolor=fig.get_facecolor(), edgecolor="none")
    plt.close(fig)
    buf.seek(0)
    return buf


# ──────────────────────────────────────────────────────────────
# BOT HANDLERS
# ──────────────────────────────────────────────────────────────

@restricted
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "💰 *HDFC Expense Tracker Bot*\n\n"
        "Forward your HDFC Bank email alerts here and I'll track everything!\n\n"
        "*Commands:*\n"
        "/summary — This month's overview\n"
        "/monthly — Month-by-month breakdown\n"
        "/category — Spending by category\n"
        "/daily — Daily spending chart (last 30 days)\n"
        "/recent — Last 10 transactions\n"
        "/search `keyword` — Search transactions\n"
        "/drilldown `category` — Pareto breakdown of a category\n"
        "/addrule `keyword` `Category` — Auto-categorize\n"
        "/export — Download CSV\n"
        "/help — Show this message",
        parse_mode="Markdown"
    )


@restricted
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, context)


@restricted
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Parse forwarded HDFC email alerts."""
    text = update.message.text or ""
    if not text.strip():
        return

    result = parse_hdfc_email(text)
    if not result:
        await update.message.reply_text(
            "🤔 Couldn't parse that as an HDFC alert.\n"
            "Try forwarding the *full email text* from HDFC Bank.",
            parse_mode="Markdown"
        )
        return

    user_id = update.effective_user.id
    txn_id, category = add_transaction(
        user_id=user_id,
        amount=result["amount"],
        txn_type=result["txn_type"],
        mode=result.get("mode"),
        merchant=result["merchant"],
        card_last4=result["card_last4"],
        ref_no=result["ref_no"],
        txn_date=result["txn_date"],
        body_text=text,
    )

    if txn_id is None:
        await update.message.reply_text(
            "⚠️ *Duplicate detected* — this transaction is already recorded.",
            parse_mode="Markdown"
        )
        return

    emoji = {"UPI": "📱", "DEBIT": "💳", "CREDIT": "💳"}.get(result["txn_type"], "💸")
    card_info = f" (xx{result['card_last4']})" if result["card_last4"] else ""

    await update.message.reply_text(
        f"✅ *Transaction #{txn_id} recorded*\n\n"
        f"{emoji} *Type:* {result['txn_type']}{card_info}\n"
        f"💵 *Amount:* ₹{result['amount']:,.2f}\n"
        f"🏪 *Merchant:* {result['merchant'] or 'Unknown'}\n"
        f"🏷 *Category:* {category}\n"
        f"📅 *Date:* {result['txn_date']}",
        parse_mode="Markdown"
    )


@restricted
async def cmd_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    now = datetime.now()
    args = context.args

    parsed = _resolve_date_range(args, now)
    if parsed is None:
        await update.message.reply_text(
            "Usage: /summary [from] [to]\n"
            "  /summary                        — this month\n"
            "  /summary today\n"
            "  /summary yesterday\n"
            "  /summary week                   — last 7 days\n"
            "  /summary month\n"
            "  /summary year\n"
            "  /summary 2025-01-01             — from date to today\n"
            "  /summary 2025-01-01 2025-03-31  — custom range"
        )
        return
    date_from, date_to, label = parsed

    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query(
        "SELECT * FROM transactions WHERE user_id = ? AND txn_date >= ? AND txn_date <= ? ORDER BY amount DESC",
        conn, params=(user_id, date_from, date_to)
    )
    conn.close()

    if df.empty:
        await update.message.reply_text(f"No transactions found for {label}.")
        return

    debits  = df[df["txn_type"] == "DEBIT"]
    credits = df[df["txn_type"] == "CREDIT"]

    total_debit  = debits["amount"].sum()
    total_credit = credits["amount"].sum()
    net          = total_debit - total_credit

    # ── Message 1: Overview ───────────────────────────────────────
    lines = [f"📊 *{label}*\n"]
    lines.append(f"💸 Debited:   ₹{total_debit:>12,.2f}  ({len(debits)} txns)")
    lines.append(f"💰 Credited:  ₹{total_credit:>12,.2f}  ({len(credits)} txns)")
    lines.append(f"📉 Net outflow: ₹{net:>10,.2f}")

    # By mode (debits only — what you actually spent)
    if not debits.empty and "mode" in debits.columns:
        by_mode = debits.groupby("mode", dropna=False)["amount"].agg(["sum", "count"]).sort_values("sum", ascending=False)
        lines.append("\n*By Payment Mode (debits):*")
        for mode, row in by_mode.iterrows():
            pct   = (row["sum"] / total_debit * 100) if total_debit else 0
            emoji = MODE_EMOJI.get(str(mode), "💲")
            lines.append(f"  {emoji} {mode or 'Unknown':14s}  ₹{row['sum']:>10,.2f}  {pct:4.0f}%  ({int(row['count'])} txns)")

    # By category (debits)
    by_cat = debits.groupby("category")["amount"].sum().sort_values(ascending=False)
    lines.append("\n*By Category (debits):*")
    for cat, amt in by_cat.items():
        pct = (amt / total_debit * 100) if total_debit else 0
        bar = "█" * int(pct / 5)
        lines.append(f"  {_md(cat):20s}  ₹{amt:>10,.2f}  {pct:4.0f}%  {bar}")

    await _send_long(update, "\n".join(lines))

    # ── Message 2: Top 20 transactions by amount ──────────────────
    top = df.nlargest(20, "amount")
    txn_lines = [f"*Top {len(top)} transactions ({label}):*\n"]
    for _, r in top.iterrows():
        emoji    = MODE_EMOJI.get(str(r.get("mode", "")), "💲")
        arrow    = "↑" if r["txn_type"] == "CREDIT" else "↓"
        merchant = _md(r["merchant"] or "Unknown")[:22]
        mode_str = _md(str(r.get("mode") or ""))[:12]
        txn_lines.append(
            f"{arrow}{emoji} ₹{r['amount']:>9,.2f}  {merchant:<22s}  {mode_str:<12s}  {r['txn_date']}"
        )

    await _send_long(update, "\n".join(txn_lines))

    # ── Message 3: Per-mode top transactions (debit modes with >1 txn) ──
    if "mode" in debits.columns:
        for mode, group in debits.groupby("mode", dropna=False):
            if len(group) < 2:
                continue
            emoji   = MODE_EMOJI.get(str(mode), "💲")
            top_grp = group.nlargest(10, "amount")
            mlines  = [f"{emoji} *{_md(str(mode))} — ₹{group['amount'].sum():,.2f} ({len(group)} txns):*\n"]
            for _, r in top_grp.iterrows():
                merchant = _md(r["merchant"] or "Unknown")[:28]
                mlines.append(f"  ₹{r['amount']:>9,.2f}  {merchant:<28s}  {r['txn_date']}")
            await _send_long(update, "\n".join(mlines))


@restricted
async def cmd_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conn = sqlite3.connect(DB_PATH)
    now = datetime.now()
    month_start = now.strftime("%Y-%m-01")

    df = pd.read_sql_query(
        "SELECT category, SUM(amount) as total FROM transactions "
        "WHERE user_id = ? AND txn_date >= ? GROUP BY category ORDER BY total DESC",
        conn, params=(user_id, month_start)
    )
    conn.close()

    if df.empty:
        await update.message.reply_text("No transactions this month.")
        return

    # Pie chart
    fig, ax = _styled_fig(figsize=(7, 7))
    colors = CHART_COLORS[:len(df)]
    wedges, texts, autotexts = ax.pie(
        df["total"], labels=df["category"], autopct="%1.0f%%",
        colors=colors, textprops={"color": "#e0e0e0", "fontsize": 10},
        pctdistance=0.8, startangle=140
    )
    for t in autotexts:
        t.set_fontsize(9)
        t.set_color("#ffffff")
    ax.set_title(f"Spending by Category — {now.strftime('%B %Y')}", fontsize=14, pad=20)

    buf = _fig_to_bytes(fig)
    await update.message.reply_photo(photo=buf, caption="🏷 Category breakdown for this month")


@restricted
async def cmd_daily(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conn = sqlite3.connect(DB_PATH)
    cutoff = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")

    df = pd.read_sql_query(
        "SELECT txn_date, SUM(amount) as total FROM transactions "
        "WHERE user_id = ? AND txn_date >= ? GROUP BY txn_date ORDER BY txn_date",
        conn, params=(user_id, cutoff)
    )
    conn.close()

    if df.empty:
        await update.message.reply_text("No transactions in the last 30 days.")
        return

    df["txn_date"] = pd.to_datetime(df["txn_date"])
    fig, ax = _styled_fig()
    ax.bar(df["txn_date"], df["total"], color="#6C5CE7", width=0.8, edgecolor="#1a1a2e")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))
    ax.xaxis.set_major_locator(mdates.WeekdayLocator(interval=1))
    plt.xticks(rotation=45)
    ax.set_ylabel("Amount (₹)")
    ax.set_title("Daily Spending — Last 30 Days", fontsize=13, pad=15)
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"₹{x:,.0f}"))

    buf = _fig_to_bytes(fig)
    await update.message.reply_photo(photo=buf, caption="📅 Daily spending over the last 30 days")


@restricted
async def cmd_monthly(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conn = sqlite3.connect(DB_PATH)

    df = pd.read_sql_query(
        "SELECT substr(txn_date,1,7) as month, txn_type, SUM(amount) as total "
        "FROM transactions WHERE user_id = ? GROUP BY month, txn_type ORDER BY month",
        conn, params=(user_id,)
    )
    conn.close()

    if df.empty:
        await update.message.reply_text("No transactions found.")
        return

    pivot = df.pivot_table(index="month", columns="txn_type", values="total", fill_value=0)
    fig, ax = _styled_fig()

    x = range(len(pivot))
    bottom = [0] * len(pivot)
    color_map = {"UPI": "#6C5CE7", "DEBIT": "#00B894", "CREDIT": "#FD79A8"}

    for col in pivot.columns:
        color = color_map.get(col, "#FDCB6E")
        ax.bar(x, pivot[col], bottom=bottom, label=col, color=color, edgecolor="#1a1a2e")
        bottom = [b + v for b, v in zip(bottom, pivot[col])]

    ax.set_xticks(x)
    ax.set_xticklabels(pivot.index, rotation=45)
    ax.set_ylabel("Amount (₹)")
    ax.set_title("Monthly Spending by Type", fontsize=13, pad=15)
    ax.legend(facecolor="#16213e", edgecolor="#333", labelcolor="#e0e0e0")
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"₹{x:,.0f}"))

    buf = _fig_to_bytes(fig)
    await update.message.reply_photo(photo=buf, caption="📊 Monthly spending breakdown")


@restricted
async def cmd_recent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT id, amount, txn_type, merchant, category, txn_date "
        "FROM transactions WHERE user_id = ? ORDER BY txn_date DESC, id DESC LIMIT 10",
        (user_id,)
    )
    rows = c.fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("No transactions yet. Forward an HDFC email to start!")
        return

    lines = ["📋 *Last 10 Transactions*\n"]
    for r in rows:
        tid, amt, ttype, merchant, cat, tdate = r
        emoji = {"UPI": "📱", "DEBIT": "💳", "CREDIT": "💳"}.get(ttype, "💸")
        lines.append(f"{emoji} `#{tid}` ₹{amt:,.2f} — {_md(merchant or 'Unknown')} [{_md(cat)}] _{tdate}_")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


@restricted
async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /search `keyword`", parse_mode="Markdown")
        return

    keyword = " ".join(context.args)
    user_id = update.effective_user.id
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT id, amount, txn_type, merchant, category, txn_date "
        "FROM transactions WHERE user_id = ? AND (merchant LIKE ? OR category LIKE ?) "
        "ORDER BY txn_date DESC LIMIT 15",
        (user_id, f"%{keyword}%", f"%{keyword}%")
    )
    rows = c.fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text(f"No transactions matching '{keyword}'.")
        return

    total = sum(r[1] for r in rows)
    lines = [f"🔍 *Search: '{_md(keyword)}'* — {len(rows)} results, ₹{total:,.2f} total\n"]
    for r in rows:
        tid, amt, ttype, merchant, cat, tdate = r
        lines.append(f"  `#{tid}` ₹{amt:,.2f} — {_md(merchant or 'Unknown')} [{_md(cat)}] _{tdate}_")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


@restricted
async def cmd_addrule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage: /addrule `keyword` `Category Name`\n"
            "Example: /addrule starbucks Food & Dining",
            parse_mode="Markdown"
        )
        return

    keyword = context.args[0]
    category = " ".join(context.args[1:])
    user_id = update.effective_user.id

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT INTO category_rules (user_id, keyword, category) VALUES (?, ?, ?)",
        (user_id, keyword, category)
    )
    # Update existing uncategorized transactions
    c.execute(
        "UPDATE transactions SET category = ? WHERE user_id = ? AND category = 'Uncategorized' AND merchant LIKE ?",
        (category, user_id, f"%{keyword}%")
    )
    updated = c.rowcount
    conn.commit()
    conn.close()

    msg = f"✅ Rule added: *{keyword}* → *{category}*"
    if updated:
        msg += f"\n🔄 Updated {updated} existing transaction(s)."
    await update.message.reply_text(msg, parse_mode="Markdown")


@restricted
async def cmd_drilldown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /drilldown <category> [from] [to]

    Pareto breakdown of a category: which merchants are responsible
    for the bulk of spend, and every transaction within them.
    """
    user_id = update.effective_user.id
    now = datetime.now()

    if not context.args:
        await update.message.reply_text(
            "Usage: /drilldown <category> [from] [to]\n"
            "  /drilldown Uncategorized\n"
            "  /drilldown Uncategorized week\n"
            "  /drilldown Uncategorized month\n"
            "  /drilldown Uncategorized year\n"
            "  /drilldown Uncategorized 2025-01-01\n"
            "  /drilldown Uncategorized 2025-01-01 2025-03-31"
        )
        return

    # Split trailing date args from the category name.
    # Dates/tags are the last 1 or 2 tokens if they look like dates or known tags.
    DATE_TAGS = {"today", "yesterday", "week", "month", "year"}
    raw = context.args

    def _is_date_token(s):
        if s.lower() in DATE_TAGS:
            return True
        try:
            datetime.strptime(s, "%Y-%m-%d")
            return True
        except ValueError:
            return False

    # Peel off trailing date tokens (up to 2)
    date_tokens = []
    cat_tokens  = list(raw)
    for _ in range(2):
        if cat_tokens and _is_date_token(cat_tokens[-1]):
            date_tokens.insert(0, cat_tokens.pop())
        else:
            break

    category = " ".join(cat_tokens)
    if not category:
        await update.message.reply_text("Please specify a category name.")
        return

    parsed = _resolve_date_range(date_tokens, now)
    if parsed is None:
        await update.message.reply_text("Invalid date. Use: today, yesterday, week, month, year, or YYYY-MM-DD.")
        return
    date_from, date_to, label_date = parsed

    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query(
        "SELECT * FROM transactions "
        "WHERE user_id = ? AND txn_date >= ? AND txn_date <= ? AND txn_type = 'DEBIT' "
        "AND category LIKE ? "
        "ORDER BY amount DESC",
        conn, params=(user_id, date_from, date_to, category)
    )
    conn.close()

    if df.empty:
        await update.message.reply_text(f"No debit transactions in '{category}' for {label_date}.")
        return

    total = df["amount"].sum()
    count = len(df)

    # ── Message 1: Merchant Pareto ────────────────────────────────
    by_merchant = (
        df.groupby("merchant", dropna=False)["amount"]
        .agg(["sum", "count"])
        .sort_values("sum", ascending=False)
    )

    lines = [f"🔍 *{_md(category)}* — {label_date}\n"]
    lines.append(f"Total: ₹{total:,.2f}  ({count} txns)\n")
    lines.append("*Merchants (Pareto):*")

    cumulative = 0.0
    for merchant, row in by_merchant.iterrows():
        pct        = (row["sum"] / total * 100) if total else 0
        cumulative += pct
        bar        = "█" * max(1, int(pct / 4))
        name       = _md(str(merchant) if merchant else "Unknown")[:24]

        # Summarise modes + txn_types for this merchant's transactions
        txns = df[df["merchant"] == merchant]
        mode_counts = txns["mode"].value_counts()
        type_counts = txns["txn_type"].value_counts()
        mode_str = "  ".join(
            f"{MODE_EMOJI.get(str(m), '💲')} {m} ×{c}" if c > 1 else f"{MODE_EMOJI.get(str(m), '💲')} {m}"
            for m, c in mode_counts.items() if m and str(m) != "nan"
        ) or "Unknown"
        type_str = "  ".join(
            f"{'↓' if t == 'DEBIT' else '↑'}{t}" for t in type_counts.index
        )

        lines.append(
            f"  {name:<24s}  ₹{row['sum']:>10,.2f}  {pct:4.0f}%  {bar}"
        )
        lines.append(
            f"  {'':24s}  {int(row['count'])} txn{'s' if row['count'] > 1 else ''}  cumulative {cumulative:.0f}%"
        )
        lines.append(
            f"  {'':24s}  {mode_str}  {type_str}"
        )
        if cumulative >= 80:
            remaining = len(by_merchant) - list(by_merchant.index).index(merchant) - 1
            if remaining > 0:
                tail_amt = by_merchant.iloc[list(by_merchant.index).index(merchant)+1:]["sum"].sum()
                lines.append(f"\n  ↳ remaining {remaining} merchant(s): ₹{tail_amt:,.2f} ({100-cumulative:.0f}%)")
            break

    await _send_long(update, "\n".join(lines))

    # ── Message 2: All transactions, grouped by merchant ─────────
    for merchant, group in by_merchant.iterrows():
        txns = df[df["merchant"] == merchant].sort_values("amount", ascending=False)
        if txns.empty:
            continue
        name    = _md(str(merchant) if merchant else "Unknown")[:30]
        mlines  = [f"*{name}* — ₹{group['sum']:,.2f}\n"]
        for _, r in txns.iterrows():
            mode_str = (str(r.get("mode") or ""))[:12]
            mlines.append(f"  ₹{r['amount']:>9,.2f}  {mode_str:<12s}  {r['txn_date']}")
        await _send_long(update, "\n".join(mlines))


@restricted
async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query(
        "SELECT txn_date, amount, txn_type, merchant, category, card_last4, ref_no "
        "FROM transactions WHERE user_id = ? ORDER BY txn_date DESC",
        conn, params=(user_id,)
    )
    conn.close()

    if df.empty:
        await update.message.reply_text("No transactions to export.")
        return

    buf = BytesIO()
    df.to_csv(buf, index=False)
    buf.seek(0)
    buf.name = f"hdfc_expenses_{datetime.now().strftime('%Y%m%d')}.csv"

    await update.message.reply_document(document=buf, caption="📎 Your expense data as CSV")


# ──────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────

def main():
    init_db()
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("summary", cmd_summary))
    app.add_handler(CommandHandler("monthly", cmd_monthly))
    app.add_handler(CommandHandler("category", cmd_category))
    app.add_handler(CommandHandler("daily", cmd_daily))
    app.add_handler(CommandHandler("recent", cmd_recent))
    app.add_handler(CommandHandler("search", cmd_search))
    app.add_handler(CommandHandler("addrule", cmd_addrule))
    app.add_handler(CommandHandler("drilldown", cmd_drilldown))
    app.add_handler(CommandHandler("export", cmd_export))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot started! Listening for messages...")
    app.run_polling()


if __name__ == "__main__":
    main()
