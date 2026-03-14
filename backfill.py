"""
HDFC Email Backfill Script
───────────────────────────
Scans your ENTIRE Gmail history for HDFC Bank transaction alerts
and backfills them into the expenses database.

Run this ONCE before starting the bot to import all historical data.

Usage:
  export EMAIL_ADDRESS="your_email@gmail.com"
  export EMAIL_PASSWORD="your_gmail_app_password"
  python backfill.py [options]

Options:
  --since YYYY-MM-DD    Only import emails after this date (default: all time)
  --until YYYY-MM-DD    Only import emails before this date (default: today)
  --dry-run             Parse and show results without writing to DB
  --verbose             Show every parsed transaction
  --user-id NUMBER      Telegram user ID to associate (default: 0 for backfill)
  --db PATH             Path to SQLite database (default: expenses.db)
  --batch-size NUMBER   IMAP fetch batch size (default: 50)
"""

import os
import sys
import argparse
import imaplib
import email
import sqlite3
import hashlib
import logging
import time
from email.header import decode_header
from datetime import datetime
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

# ─── Reuse parsers from bot.py ───────────────────────────────
from bot import parse_hdfc_email, init_db, auto_categorize

# ──────────────────────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────────────────────
IMAP_SERVER   = os.environ.get("IMAP_SERVER", "imap.gmail.com")
IMAP_PORT     = int(os.environ.get("IMAP_PORT", "993"))
EMAIL_ADDRESS = os.environ.get("EMAIL_ADDRESS", "your_email@gmail.com")
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD", "your_app_password")

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────
# DEDUP: fingerprint each transaction
# ──────────────────────────────────────────────────────────────
def txn_fingerprint(body: str) -> str:
    """
    Hash of the raw email body text — pure function, no LLM dependency.
    Same email always produces the same fingerprint.
    """
    return hashlib.sha256(body.encode()).hexdigest()[:16]


def fingerprint_exists(cursor, fingerprint):
    cursor.execute(
        "SELECT 1 FROM transactions WHERE fingerprint = ? LIMIT 1",
        (fingerprint,)
    )
    return cursor.fetchone() is not None


# ──────────────────────────────────────────────────────────────
# DB MIGRATION: add fingerprint column if missing
# ──────────────────────────────────────────────────────────────
def ensure_mode_column(db_path):
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute("PRAGMA table_info(transactions)")
    columns = [row[1] for row in c.fetchall()]
    if "mode" not in columns:
        logger.info("Adding 'mode' column to transactions table...")
        c.execute("ALTER TABLE transactions ADD COLUMN mode TEXT")
        conn.commit()
    conn.close()


def ensure_fingerprint_column(db_path):
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute("PRAGMA table_info(transactions)")
    columns = [row[1] for row in c.fetchall()]
    if "fingerprint" not in columns:
        logger.info("Adding 'fingerprint' column to transactions table...")
        c.execute("ALTER TABLE transactions ADD COLUMN fingerprint TEXT")
        c.execute("CREATE INDEX IF NOT EXISTS idx_fingerprint ON transactions(fingerprint)")
        conn.commit()
    conn.close()


# ──────────────────────────────────────────────────────────────
# EMAIL HELPERS
# ──────────────────────────────────────────────────────────────
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
    """Extract plain text body from email message."""
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            content_disposition = str(part.get("Content-Disposition", ""))
            if content_type == "text/plain" and "attachment" not in content_disposition:
                charset = part.get_content_charset() or "utf-8"
                return part.get_payload(decode=True).decode(charset, errors="replace")
        # Fallback to HTML
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                charset = part.get_content_charset() or "utf-8"
                raw = part.get_payload(decode=True).decode(charset, errors="replace")
                import re
                text = re.sub(r"<[^>]+>", " ", raw)
                text = re.sub(r"\s+", " ", text).strip()
                return text
    else:
        charset = msg.get_content_charset() or "utf-8"
        payload = msg.get_payload(decode=True)
        if payload:
            return payload.decode(charset, errors="replace")
    return ""


def get_email_date(msg) -> str | None:
    """Extract date from email headers."""
    date_str = msg.get("Date", "")
    if not date_str:
        return None
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(date_str)
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return None


# ──────────────────────────────────────────────────────────────
# MAIN BACKFILL LOGIC
# ──────────────────────────────────────────────────────────────
def build_search_criteria(since=None, until=None):
    """
    Build IMAP search query using Gmail's X-GM-RAW for partial sender matching.
    """
    gmail_query = "from:hdfcbank"

    if since:
        d = datetime.strptime(since, "%Y-%m-%d")
        gmail_query += f" after:{d.strftime('%Y/%m/%d')}"

    if until:
        d = datetime.strptime(until, "%Y-%m-%d")
        gmail_query += f" before:{d.strftime('%Y/%m/%d')}"

    return f'(X-GM-RAW "{gmail_query}")'


def backfill(args):
    db_path = args.db
    user_id = args.user_id

    # Init DB and ensure schema
    os.environ["DB_PATH"] = db_path
    init_db()
    ensure_fingerprint_column(db_path)
    ensure_mode_column(db_path)

    # Connect to IMAP
    logger.info(f"Connecting to {IMAP_SERVER}:{IMAP_PORT} as {EMAIL_ADDRESS}...")
    try:
        mail = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
        mail.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
    except imaplib.IMAP4.error as e:
        logger.error(f"IMAP login failed: {e}")
        logger.error("Make sure you're using an App Password for Gmail.")
        sys.exit(1)

    # Select All Mail for Gmail (catches archived emails too)
    # Fall back to INBOX for other providers
    try:
        status, _ = mail.select('"[Gmail]/All Mail"', readonly=True)
        if status != "OK":
            raise Exception("Not Gmail")
        mailbox = "[Gmail]/All Mail"
    except Exception:
        mail.select("INBOX", readonly=True)
        mailbox = "INBOX"
    logger.info(f"Searching in: {mailbox}")

    # Search
    criteria = build_search_criteria(args.since, args.until)
    logger.info(f"IMAP search: {criteria}")
    status, message_ids = mail.search(None, criteria)

    if status != "OK" or not message_ids[0]:
        logger.info("No HDFC emails found matching criteria.")
        mail.logout()
        return

    all_ids = message_ids[0].split()
    total_emails = len(all_ids)
    logger.info(f"Found {total_emails} HDFC emails to process")

    # Stats
    stats = Counter()
    stats["total_emails"] = total_emails

    conn = sqlite3.connect(db_path)
    c = conn.cursor()

    # Process in batches
    batch_size = args.batch_size
    start_time = time.time()

    for batch_start in range(0, total_emails, batch_size):
        batch_end = min(batch_start + batch_size, total_emails)
        batch_ids = all_ids[batch_start:batch_end]

        # Fetch batch — use UID for stability
        id_range = b",".join(batch_ids)
        status, msg_data = mail.fetch(id_range, "(RFC822)")

        if status != "OK":
            logger.warning(f"Failed to fetch batch {batch_start}-{batch_end}")
            stats["fetch_errors"] += 1
            continue

        # ── Step 1: Extract email bodies (fast, sequential) ──────
        emails_to_parse = []
        for i in range(0, len(msg_data)):
            item = msg_data[i]
            if not isinstance(item, tuple):
                continue
            try:
                msg = email.message_from_bytes(item[1])
                sender = msg.get("From", "").lower()
                if "alerts@hdfcbank" not in sender:
                    stats["skipped_promo"] += 1
                    continue
                subject = decode_mime_words(msg.get("Subject", ""))
                body = get_email_body(msg)
                email_date = get_email_date(msg)
                if not body:
                    stats["empty_body"] += 1
                    continue
                emails_to_parse.append((subject, body, email_date))
            except Exception as e:
                stats["errors"] += 1
                logger.warning(f"  Error reading email: {e}")

        # ── Step 2: Parse in parallel + write as results arrive ──
        def parse_one(subject_body_date):
            subject, body, email_date = subject_body_date
            try:
                result = parse_hdfc_email(body)
                return subject, body, email_date, result
            except Exception:
                return subject, body, email_date, None

        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = {executor.submit(parse_one, e): e for e in emails_to_parse}
            for future in as_completed(futures):
                subject, body, email_date, result = future.result()
                try:
                    if not result:
                        stats["unparseable"] += 1
                        if args.verbose:
                            logger.debug(f"  Could not parse: {subject[:60]}")
                        continue

                    if email_date:
                        result["txn_date"] = email_date

                    fp = txn_fingerprint(body)

                    if fingerprint_exists(c, fp):
                        stats["duplicates"] += 1
                        continue

                    # Secondary dedup: same amount + type + merchant + date (catches stale fingerprints)
                    c.execute(
                        "SELECT 1 FROM transactions WHERE user_id=? AND amount=? AND txn_type=? AND merchant=? AND txn_date=? LIMIT 1",
                        (user_id, result["amount"], result["txn_type"], result["merchant"], result["txn_date"])
                    )
                    if c.fetchone():
                        stats["duplicates"] += 1
                        continue

                    if args.dry_run:
                        stats["would_import"] += 1
                        if args.verbose:
                            logger.info(
                                f"  [DRY RUN] {result['txn_date']} | "
                                f"{result['txn_type']:6s} | {result.get('mode',''):12s} | "
                                f"₹{result['amount']:>10,.2f} | "
                                f"{result['merchant'] or 'Unknown':30s}"
                            )
                            if not result["merchant"] and args.debug:
                                logger.info(f"  [EMAIL BODY]:\n{body[:600]}\n")
                        continue

                    category = auto_categorize(c, user_id, result["merchant"])
                    c.execute("""
                        INSERT INTO transactions
                            (user_id, amount, txn_type, mode, merchant, category,
                             card_last4, ref_no, txn_date, fingerprint)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        user_id,
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
                    stats["imported"] += 1

                    if args.verbose:
                        logger.info(
                            f"  ✅ {result['txn_date']} | "
                            f"{result['txn_type']:6s} | {result.get('mode',''):12s} | "
                            f"₹{result['amount']:>10,.2f} | "
                            f"{result['merchant'] or 'Unknown':30s} [{category}]"
                        )

                except Exception as e:
                    stats["errors"] += 1
                    logger.warning(f"  Error processing email: {e}")
                    continue

        # Commit each batch
        if not args.dry_run:
            conn.commit()

        # Progress
        processed = batch_end
        elapsed = time.time() - start_time
        rate = processed / elapsed if elapsed > 0 else 0
        remaining = (total_emails - processed) / rate if rate > 0 else 0
        logger.info(
            f"  Progress: {processed}/{total_emails} "
            f"({processed/total_emails*100:.0f}%) | "
            f"{rate:.1f} emails/sec | "
            f"~{remaining:.0f}s remaining"
        )

    conn.close()
    mail.logout()

    # ── Summary ──────────────────────────────────────────────
    elapsed = time.time() - start_time

    print("\n" + "═" * 55)
    print("  BACKFILL COMPLETE")
    print("═" * 55)
    print(f"  Total HDFC emails found:    {stats['total_emails']:>8,}")
    print(f"  Skipped (promotional):      {stats['skipped_promo']:>8,}")
    print(f"  Skipped (empty body):       {stats['empty_body']:>8,}")
    print(f"  Could not parse:            {stats['unparseable']:>8,}")
    print(f"  Duplicates skipped:         {stats['duplicates']:>8,}")
    print(f"  Errors:                     {stats['errors']:>8,}")
    print("  " + "─" * 53)
    if args.dry_run:
        print(f"  Would import:               {stats['would_import']:>8,}")
        print(f"  (re-run without --dry-run to actually import)")
    else:
        print(f"  ✅ Imported:                 {stats['imported']:>8,}")
    print(f"  Time elapsed:               {elapsed:>7.1f}s")
    print("═" * 55)

    if not args.dry_run and stats["imported"] > 0:
        # Quick summary of imported data
        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        c.execute("""
            SELECT
                MIN(txn_date) as earliest,
                MAX(txn_date) as latest,
                COUNT(*) as count,
                SUM(amount) as total,
                COUNT(DISTINCT category) as categories
            FROM transactions WHERE user_id = ?
        """, (user_id,))
        row = c.fetchone()
        conn.close()

        if row and row[0]:
            print(f"\n  📊 Your expense data now spans:")
            print(f"     {row[0]}  →  {row[1]}")
            print(f"     {row[2]:,} transactions totalling ₹{row[3]:,.2f}")
            print(f"     across {row[4]} categories")
            print(f"\n  Start the bot and use /summary or /monthly to explore!")


# ──────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="Backfill HDFC Bank transaction emails into expense tracker",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Dry run — see what would be imported
  python backfill.py --dry-run --verbose

  # Import everything
  python backfill.py

  # Import only last 6 months
  python backfill.py --since 2025-09-01

  # Import a specific date range
  python backfill.py --since 2024-01-01 --until 2024-12-31 --verbose
        """
    )
    parser.add_argument("--since", type=str, default=None,
                        help="Only import emails after YYYY-MM-DD")
    parser.add_argument("--until", type=str, default=None,
                        help="Only import emails before YYYY-MM-DD")
    parser.add_argument("--dry-run", action="store_true",
                        help="Parse and display without writing to DB")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Show every transaction as it's processed")
    parser.add_argument("--user-id", type=int, default=0,
                        help="Telegram user ID to associate (default: 0)")
    parser.add_argument("--db", type=str, default="expenses.db",
                        help="SQLite database path (default: expenses.db)")
    parser.add_argument("--batch-size", type=int, default=50,
                        help="IMAP fetch batch size (default: 50)")
    parser.add_argument("--workers", type=int, default=2,
                        help="Parallel LLM workers (default: 2)")
    parser.add_argument("--debug", action="store_true",
                        help="Print email body when merchant is Unknown")

    args = parser.parse_args()

    # Validate dates
    for date_arg, name in [(args.since, "--since"), (args.until, "--until")]:
        if date_arg:
            try:
                datetime.strptime(date_arg, "%Y-%m-%d")
            except ValueError:
                logger.error(f"{name} must be in YYYY-MM-DD format")
                sys.exit(1)

    if EMAIL_ADDRESS == "your_email@gmail.com":
        print("⚠️  Set your email credentials first:")
        print("   export EMAIL_ADDRESS='your_email@gmail.com'")
        print("   export EMAIL_PASSWORD='your_gmail_app_password'")
        sys.exit(1)

    backfill(args)


if __name__ == "__main__":
    main()
