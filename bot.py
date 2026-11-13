import os
import re
import sqlite3
import logging
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from telegram import Update, InputFile
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

BOT_TOKEN = os.environ["BOT_TOKEN"]
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
        conn.commit()
    logger.info("SQLite database ready at %s", DB_PATH)


def clean_url(raw_url: str):
    url = raw_url.strip().rstrip(".,;:!?)]}»”'")
    if not url:
        return None
    try:
        parts = urlsplit(url)
        if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
            return None
        host = parts.hostname.lower().rstrip(".")
        if not host.endswith(".com"):
            return None
        if parts.username or parts.password:
            return None
        netloc = host
        if parts.port:
            netloc = f"{host}:{parts.port}"
        normalized = urlunsplit((
            parts.scheme.lower(), netloc, parts.path, parts.query, parts.fragment
        ))
        return normalized, url
    except (ValueError, TypeError):
        return None


def extract_links(text: str):
    # Some text sources encode line breaks as the two literal characters: backslash+n.
    # Convert those to real line breaks before extracting URLs.
    text = (text or "").replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\r", "\n")
    results = []
    seen_in_batch = set()
    for match in URL_PATTERN.findall(text):
        cleaned = clean_url(match)
        if cleaned:
            normalized, original = cleaned
            if normalized not in seen_in_batch:
                seen_in_batch.add(normalized)
                results.append(cleaned)
    return results


def filter_and_save(links):
    new_links = []
    duplicates = 0
    with get_connection() as conn:
        cur = conn.cursor()
        for normalized, original in links:
            cur.execute(
                "INSERT OR IGNORE INTO saved_links (normalized_url, original_url) VALUES (?, ?)",
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
        return conn.execute("SELECT COUNT(*) FROM saved_links").fetchone()[0]


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Welcome to Permanent Link Filter Bot!\n\n"
        "Send .com links directly or upload a TXT file.\n"
        "Only new links are saved and returned, one link per line.\n\n"
        "/stats - Total unique links saved\n"
        "/done - Confirm automatic processing"
    )


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"📦 Total unique links saved: {get_total_count():,}")


async def done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Every message and TXT upload is processed automatically.")


async def process_submission(update: Update, raw_text: str):
    if len(raw_text) > MAX_TEXT_CHARS:
        await update.message.reply_text("Text is too large. Please split it into smaller TXT files.")
        return

    links = extract_links(raw_text)
    if not links:
        await update.message.reply_text("No valid .com links found.")
        return

    new_links, duplicates = filter_and_save(links)
    total_saved = get_total_count()
    summary = (
        "✅ Filtering complete\n"
        f"Links found: {len(links)}\n"
        f"🆕 New links saved: {len(new_links)}\n"
        f"♻️ Duplicates skipped: {duplicates}\n"
        f"📦 Total database links: {total_saved:,}"
    )

    if new_links:
        # Actual newline characters ensure one URL per line in the TXT output.
        output = "\n".join(f"{index}. {url}" for index, url in enumerate(new_links, start=1))
        await update.message.reply_document(
            document=InputFile(output.encode("utf-8"), filename="new_links.txt"),
            caption=summary,
        )
    else:
        await update.message.reply_text(summary + "\n\nNo new links in this submission.")


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
        await update.effective_message.reply_text("Something went wrong. Please try again.")


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
