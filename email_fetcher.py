"""
HDFC Email Fetcher
──────────────────
Polls Gmail via IMAP for new HDFC Bank transaction alerts,
parses them, and saves transactions directly to the SQLite DB.

Run this as a background service alongside bot.py.
"""

import os
import sys
import time
import imaplib
import email
import sqlite3
from email.header import decode_header
from email.utils import parsedate_to_datetime
from datetime import datetime, timedelta
import logging

from bot import parse_hdfc_email, init_db, auto_categorize
from backfill import txn_fingerprint, fingerprint_exists, ensure_fingerprint_column, ensure_mode_column

# ──────────────────────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────────────────────
IMAP_SERVER    = os.environ.get("IMAP_SERVER", "imap.gmail.com")
IMAP_PORT      = int(os.environ.get("IMAP_PORT", "993"))
EMAIL_ADDRESS  = os.environ.get("EMAIL_ADDRESS", "your_email@gmail.com")
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD", "your_app_password")
CHAT_ID        = int(os.environ.get("CHAT_ID", "0"))
DB_PATH        = os.environ.get("DB_PATH", "expenses.db")
POLL_INTERVAL  = int(os.environ.get("POLL_INTERVAL", "60"))  # seconds

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)


def decode_mime_words(s):
    if not s:
        return ""
    decoded = decode_header(s)
    parts = []
    for part, charset in decoded:
        if isinstance(part, bytes):
            parts.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            parts.append(part)
    return " ".join(parts)


def get_email_body(msg) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            cd = str(part.get("Content-Disposition", ""))
            if ct == "text/plain" and "attachment" not in cd:
                charset = part.get_content_charset() or "utf-8"
                return part.get_payload(decode=True).decode(charset, errors="replace")
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                charset = part.get_content_charset() or "utf-8"
                raw = part.get_payload(decode=True).decode(charset, errors="replace")
                import re
                raw = re.sub(r"<style[^>]*>.*?</style>", " ", raw, flags=re.DOTALL)
                raw = re.sub(r"<[^>]+>", " ", raw)
                return re.sub(r"\s+", " ", raw).strip()
    else:
        charset = msg.get_content_charset() or "utf-8"
        payload = msg.get_payload(decode=True)
        if payload:
            return payload.decode(charset, errors="replace")
    return ""


def get_email_date(msg) -> str:
    date_str = msg.get("Date", "")
    if not date_str:
        return datetime.now().strftime("%Y-%m-%d")
    try:
        return parsedate_to_datetime(date_str).strftime("%Y-%m-%d")
    except Exception:
        return datetime.now().strftime("%Y-%m-%d")


def fetch_and_save():
    """Fetch unseen HDFC emails and save transactions to DB."""
    try:
        mail = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
        mail.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
        mail.select("inbox")

        since = (datetime.now() - timedelta(days=2)).strftime("%Y/%m/%d")
        status, message_ids = mail.search(None, f'(X-GM-RAW "from:hdfcbank.bank.in after:{since}")')
        if status != "OK" or not message_ids[0]:
            logger.debug("No new HDFC emails")
            mail.logout()
            return

        ids = message_ids[0].split()
        logger.info(f"Found {len(ids)} new HDFC email(s)")

        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        saved = 0

        for mid in ids:
            status, msg_data = mail.fetch(mid, "(RFC822)")
            if status != "OK":
                continue

            try:
                msg = email.message_from_bytes(msg_data[0][1])
                subject = decode_mime_words(msg.get("Subject", ""))
                body = get_email_body(msg)
                email_date = get_email_date(msg)

                if not body:
                    continue

                result = parse_hdfc_email(body)
                if not result:
                    logger.debug(f"Could not parse: {subject[:60]}")
                    continue

                # Always use the email Date: header as ground truth
                if email_date:
                    result["txn_date"] = email_date

                fp = txn_fingerprint(body)

                if fingerprint_exists(c, fp):
                    logger.debug(f"Duplicate skipped: {subject[:60]}")
                    continue

                # Secondary dedup by ref_no (strongest signal)
                if result.get("ref_no"):
                    c.execute("SELECT 1 FROM transactions WHERE ref_no=? LIMIT 1", (result["ref_no"],))
                    if c.fetchone():
                        logger.debug(f"Duplicate skipped (ref_no match): {subject[:60]}")
                        continue

                # Tertiary dedup: same amount + type + merchant + date
                c.execute(
                    "SELECT 1 FROM transactions WHERE user_id=? AND amount=? AND txn_type=? AND merchant=? AND txn_date=? LIMIT 1",
                    (CHAT_ID, result["amount"], result["txn_type"], result["merchant"], result["txn_date"])
                )
                if c.fetchone():
                    logger.debug(f"Duplicate skipped (content match): {subject[:60]}")
                    continue

                category = auto_categorize(c, CHAT_ID, result["merchant"])
                c.execute("""
                    INSERT INTO transactions
                        (user_id, amount, txn_type, mode, merchant, category,
                         card_last4, ref_no, txn_date, fingerprint)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    CHAT_ID,
                    result["amount"],
                    result["txn_type"],
                    result.get("mode"),
                    result["merchant"],
                    category,
                    result["card_last4"],
                    result["ref_no"],
                    result["txn_date"],
                    fp,
                ))
                saved += 1
                logger.info(
                    f"Saved: {result['txn_date']} | "
                    f"{result['txn_type']} | {result.get('mode', 'Unknown')} | "
                    f"Rs{result['amount']:,.2f} | "
                    f"{result['merchant'] or 'Unknown'} [{category}]"
                )

            except Exception as e:
                logger.error(f"Error processing email: {e}")
                continue

        conn.commit()
        conn.close()
        mail.logout()

        if saved:
            logger.info(f"Saved {saved} new transaction(s) to DB")

    except imaplib.IMAP4.error as e:
        logger.error(f"IMAP error: {e}")
    except Exception as e:
        logger.error(f"Unexpected error: {e}")


def main():
    if EMAIL_ADDRESS == "your_email@gmail.com":
        print("Set EMAIL_ADDRESS, EMAIL_PASSWORD, CHAT_ID env vars first.")
        sys.exit(1)

    os.environ["DB_PATH"] = DB_PATH
    init_db()
    ensure_fingerprint_column(DB_PATH)
    ensure_mode_column(DB_PATH)

    logger.info(f"Email fetcher started — polling every {POLL_INTERVAL}s")
    logger.info(f"Monitoring: {EMAIL_ADDRESS}")

    while True:
        fetch_and_save()
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
