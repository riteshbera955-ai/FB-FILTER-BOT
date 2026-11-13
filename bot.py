import os
import re
import sqlite3
import logging
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from telegram import Update, InputFile
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters
)

BOT_TOKEN = os.environ["BOT_TOKEN"]
# Render Persistent Disk must be mounted at /var/data.
# For local testing, you can override DB_PATH.
DB_PATH = Path(os.environ.get("DB_PATH", "/var/data/links.db"))
MAX_TEXT_CHARS = 500_000
MAX_TXT_BYTES = 10 * 1024 * 1024

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

URL_PATTERN = re.compile(r'https?://[^\s<>"\']+', re.IGNORECASE)


def get_connection():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def init_database():
    with get_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS saved_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                normalized_url TEXT NOT NULL UNIQUE,
                original_url TEXT NOT NULL,
                first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_saved_links_first_seen
            ON saved_links(first_seen_at)
        """)
        conn.commit()
    logger.info("SQLite database ready at %s", DB_PATH)


def clean_url(raw_url: str):
    # Remove punctuation commonly placed after URLs in sentences.
    url = raw_url.strip().rstrip(".,;:!?)]}»”'")
    if not url:
        return None

    try:
        parts = urlsplit(url)
        if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
            return None

        host = parts.hostname.lower().rstrip(".")
        # Accept .com domains and their subdomains only.
        if not host.endswith(".com"):
            return None

        if parts.username or parts.password:
            return None

        netloc = host
        if parts.port:
            netloc = f"{host}:{parts.port}"

        # Exact full URL matching: path, query parameters, and fragment are retained.
        normalized = urlunsplit((
            parts.scheme.lower(),
            netloc,
            parts.path,
            parts.query,
            parts.fragment,
        ))
        return normalized, url
    except (ValueError, TypeError):
        return None


def extract_links(text: str):
    links = []
    for match in URL_PATTERN.findall(text or ""):
        cleaned = clean_url(match)
        if cleaned:
            links.append(cleaned)
    return links


def filter_and_save(links):
    new_links = []
    duplicates = 0

    # A single transaction keeps each submitted batch consistent.
    with get_connection() as conn:
        cur = conn.cursor()
        for normalized, original in links:
            cur.execute(
                """
                INSERT OR IGNORE INTO saved_links (normalized_url, original_url)
                VALUES (?, ?)
                """,
                (normalized, original),
            )
            if cur.rowcount == 1:
                new_links.append(original)
            else:
                duplicates += 1
        conn.commit()

    return new_links, duplicates


def get_total_count():
    with get_connection() as conn:
        row = conn.execute("SELECT COUNT(*) FROM saved_links").fetchone()
        return row[0]


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Welcome to Permanent Link Filter Bot!\\n\\n"
        "Send links directly in a message or upload a .txt file.\\n"
        "I accept http/https links on .com domains only.\\n"
        "New links are saved permanently on the Render Persistent Disk.\\n\\n"
        "Commands:\\n"
        "/start - Show help\\n"
        "/stats - Show total saved links\\n"
        "/done - Confirm that submissions are processed automatically"
    )


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"📦 Total unique links saved: {get_total_count():,}"
    )


async def done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Every message and TXT upload is processed automatically. "
        "There is no pending batch."
    )


async def process_submission(update: Update, raw_text: str):
    if len(raw_text) > MAX_TEXT_CHARS:
        await update.message.reply_text(
            "This text is too large. Please split it into smaller TXT files."
        )
        return

    links = extract_links(raw_text)
    if not links:
        await update.message.reply_text(
            "No valid .com links found. Send links beginning with http:// or https://."
        )
        return

    new_links, duplicates = filter_and_save(links)
    total_saved = get_total_count()

    summary = (
        "✅ Filtering complete\\n"
        f"Links found: {len(links)}\\n"
        f"🆕 New links saved: {len(new_links)}\\n"
        f"♻️ Duplicates skipped: {duplicates}\\n"
        f"📦 Total database links: {total_saved:,}"
    )

    if new_links:
        output = "\\n".join(new_links).encode("utf-8")
        await update.message.reply_document(
            document=InputFile(output, filename="new_links.txt"),
            caption=summary,
        )
    else:
        await update.message.reply_text(
            summary + "\\n\\nNo new links in this submission."
        )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await process_submission(update, update.message.text or "")


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if not doc.file_name or not doc.file_name.lower().endswith(".txt"):
        await update.message.reply_text("Please upload a .txt file.")
        return

    if doc.file_size and doc.file_size > MAX_TXT_BYTES:
        await update.message.reply_text("TXT file size limit is 10 MB.")
        return

    telegram_file = await context.bot.get_file(doc.file_id)
    content = await telegram_file.download_as_bytearray()

    try:
        text = bytes(content).decode("utf-8-sig")
    except UnicodeDecodeError:
        text = bytes(content).decode("utf-8", errors="replace")

    await process_submission(update, text)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.exception("Unhandled bot error", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text(
            "Something went wrong. Please try this submission again."
        )


def main():
    init_database()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("done", done))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_error_handler(error_handler)

    logger.info("Permanent Link Filter Bot is starting...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
      
