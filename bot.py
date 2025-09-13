import asyncio
import logging
import os
import re
from datetime import datetime, timezone
from typing import Dict, List, Optional

from telegram import (
    Update,
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    Chat,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler, 
    ContextTypes,
    filters,
)

# =========================
# Logging
# =========================
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("sort-bot")

# =========================
# UX Copy (Professional, consistent tone)
# =========================
COPY = {
    "brand": "FileSort Bot",
    "emoji_logo": "🗂️",
    "start_title": "Welcome to FileSort Bot",
    "start_body": (
        "I organize your uploads and forward them back in clean, alphabetical order.\n\n"
        "How it works:\n"
        "1) Send /first to start a capture session.\n"
        "2) Upload all your files (photos, docs, videos, etc.).\n"
        "3) Send /last and I’ll sort & forward them by *file name*.\n\n"
        "Need help? Use /help"
    ),
    "help": (
        "*Quick Guide*\n"
        "• /first — start capture mode\n"
        "• Send files — I’ll quietly collect them\n"
        "• /last — stop, sort (A→Z), and forward\n"
        "• /cancel — abort current session\n\n"
        "*Notes*\n"
        "• Sorting uses natural A→Z (so 2 < 10)\n"
        "• If a file has no name (e.g., photos), I assign a sensible one\n"
        "• Works in DMs and groups (I track per user)"
    ),
    "first_started": (
        "Capture started. 🔴\n\n"
        "Now send *all files* you want me to arrange. When you’re done, send /last.\n"
        "_Tip: you can keep adding files in multiple messages._"
    ),
    "already_capturing": (
        "You already have an active capture session.\n"
        "Send more files, or finish with /last. To abort, use /cancel."
    ),
    "not_capturing": (
        "No active session found.\n"
        "Start a new one with /first."
    ),
    "file_received": "Got it: *{name}*",
    "file_received_noname": "Got it (no name, assigned): *{name}*",
    "last_processing": (
        "Wrapping up your session…\n"
        "• Total files captured: *{count}*\n"
        "• Sorting by name (A→Z)…\n"
        "• Forwarding in order…"
    ),
    "last_done": (
        "All set! ✅\n"
        "I forwarded *{count}* files in sorted order.\n\n"
        "Start another round with /first whenever you like."
    ),
    "last_none": (
        "I didn’t receive any files in this session.\n"
        "Start again with /first and upload your files."
    ),
    "cancel_ok": "Session cancelled. Nothing was forwarded.",
    "error_generic": (
        "Something went wrong while processing that. Please try again."
    ),
    "footer_cta": "Need a refresher? Try /help",
}

# =========================
# Session storage
# =========================
class Item:
    def __init__(self, message_id: int, file_name: str, date_iso: str, msg_type: str):
        self.message_id = message_id
        self.file_name = file_name
        self.date_iso = date_iso
        self.msg_type = msg_type

    def __repr__(self):
        return f"Item({self.file_name!r}, message_id={self.message_id}, type={self.msg_type})"

class Session:
    def __init__(self, chat_id: int, user_id: int):
        self.chat_id = chat_id
        self.user_id = user_id
        self.items: List[Item] = []
        self.collecting: bool = False

# In-memory store keyed by (chat_id, user_id)
SESSIONS: Dict[tuple, Session] = {}

# =========================
# Utilities
# =========================

def session_key(chat: Chat, user_id: int) -> tuple:
    return (chat.id, user_id)

def safe_filename(name: str) -> str:
    # Normalize whitespace and strip dangerous chars for sorting display (not used to save a file)
    name = re.sub(r"\s+", " ", name or "").strip()
    if not name:
        name = "unnamed"
    return name

def infer_name_from_message(msg: Message) -> str:
    """
    Derive a 'filename' for sorting if the Telegram payload doesn't include one (e.g., photos).
    Priority: document.file_name, video.file_name, audio.file_name, animation.file_name, else fallback.
    """
    # Documents (PDF, zip, etc.)
    if msg.document and msg.document.file_name:
        return safe_filename(msg.document.file_name)

    # Video
    if msg.video and msg.video.file_name:
        return safe_filename(msg.video.file_name)

    # Audio
    if msg.audio and msg.audio.file_name:
        return safe_filename(msg.audio.file_name)

    # Animation (GIF)
    if msg.animation and msg.animation.file_name:
        return safe_filename(msg.animation.file_name)

    # Voice messages will have no name
    # Photos never have a file_name; prefer caption if present
    base = None
    if msg.caption:
        base = msg.caption
    else:
        # Use a timestamp-based fallback to keep deterministic order pre-sort
        ts = (msg.date or datetime.now(timezone.utc)).strftime("%Y%m%d_%H%M%S")
        # give a generic label by type
        if msg.photo:
            base = f"photo_{ts}"
        elif msg.voice:
            base = f"voice_{ts}"
        else:
            base = f"media_{ts}"

    return safe_filename(base)

def natural_sort_key(s: str):
    """
    Natural sort (case-insensitive): splits numbers and words so "file2" < "file10".
    """
    return [
        int(text) if text.isdigit() else text.lower()
        for text in re.findall(r"\d+|\D+", s)
    ]

def is_supported_media(msg: Message) -> Optional[str]:
    """
    Return a simple type label if message carries a supported file, else None.
    """
    if msg.document:
        return "document"
    if msg.photo:
        return "photo"
    if msg.video:
        return "video"
    if msg.audio:
        return "audio"
    if msg.voice:
        return "voice"
    if msg.animation:
        return "animation"
    return None

# =========================
# Handlers
# =========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup.from_row([
        InlineKeyboardButton("Start Sorting", callback_data="cta_first"),
        InlineKeyboardButton("How to Use", callback_data="cta_help"),
    ])
    text = (
        f"{COPY['emoji_logo']} *{COPY['start_title']}*\n\n"
        f"{COPY['start_body']}\n\n"
        f"— _{COPY['brand']}_"
    )
    await update.effective_message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        COPY["help"] + f"\n\n_{COPY['footer_cta']}_",
        parse_mode=ParseMode.MARKDOWN
    )

async def callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "cta_first":
        # simulate /first
        update._effective_message = query.message  # type: ignore
        await first_cmd(update, context)
    elif query.data == "cta_help":
        await query.message.reply_text(COPY["help"], parse_mode=ParseMode.MARKDOWN)

async def first_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    key = session_key(chat, user.id)
    sess = SESSIONS.get(key)

    if sess and sess.collecting:
        await update.effective_message.reply_text(COPY["already_capturing"])
        return

    SESSIONS[key] = Session(chat_id=chat.id, user_id=user.id)
    SESSIONS[key].collecting = True
    await update.effective_message.reply_text(
        COPY["first_started"],
        parse_mode=ParseMode.MARKDOWN
    )

async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    key = session_key(chat, user.id)
    sess = SESSIONS.get(key)
    if not sess or not sess.collecting:
        await update.effective_message.reply_text(COPY["not_capturing"])
        return
    del SESSIONS[key]
    await update.effective_message.reply_text(COPY["cancel_ok"])

async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    key = session_key(chat, user.id)
    sess = SESSIONS.get(key)

    if not sess or not sess.collecting:
        # Ignore if not collecting, but guide the user
        return

    msg = update.effective_message
    media_type = is_supported_media(msg)
    if not media_type:
        return  # silently skip non-file messages during capture

    try:
        inferred = infer_name_from_message(msg)
        item = Item(
            message_id=msg.message_id,
            file_name=inferred,
            date_iso=(msg.date or datetime.now(timezone.utc)).isoformat(),
            msg_type=media_type,
        )
        sess.items.append(item)

        template = "file_received" if inferred != "unnamed" else "file_received_noname"
        await msg.reply_text(
            COPY[template].format(name=inferred),
            parse_mode=ParseMode.MARKDOWN,
            quote=False,
        )
    except Exception as e:
        log.exception("Error collecting media: %s", e)
        await msg.reply_text(COPY["error_generic"])

async def last_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    key = session_key(chat, user.id)
    sess = SESSIONS.get(key)

    if not sess or not sess.collecting:
        await update.effective_message.reply_text(COPY["not_capturing"])
        return

    count = len(sess.items)
    if count == 0:
        del SESSIONS[key]
        await update.effective_message.reply_text(COPY["last_none"])
        return

    await update.effective_message.reply_text(
        COPY["last_processing"].format(count=count),
        parse_mode=ParseMode.MARKDOWN
    )

    # Sort by filename (natural, case-insensitive)
    items_sorted = sorted(sess.items, key=lambda it: natural_sort_key(it.file_name))

    # Forward (copy) back into the same chat in order
    # Using copy_message preserves captions; forward_message shows "forwarded from"
    for it in items_sorted:
        try:
            await context.bot.copy_message(
                chat_id=chat.id,
                from_chat_id=chat.id,
                message_id=it.message_id,
            )
            # a tiny pause to respect rate limits in large batches
            await asyncio.sleep(0.05)
        except Exception as e:
            log.error("Failed to copy message %s: %s", it.message_id, e)

    # Close session
    del SESSIONS[key]
    await update.effective_message.reply_text(
        COPY["last_done"].format(count=count),
        parse_mode=ParseMode.MARKDOWN
    )

# =========================
# Main
# =========================
def main():
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN env var not set")

    app = Application.builder().token(token).build()

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("first", first_cmd))
    app.add_handler(CommandHandler("last", last_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))

    # Inline button callbacks
    app.add_handler(CallbackQueryHandler(callback_query))

    # Media collector
    media_filter = (
        filters.Document.ALL   # docs
        | filters.PHOTO        # images
        | filters.VIDEO        # videos
        | filters.AUDIO        # audio/music
        | filters.VOICE        # voice notes
        | filters.ANIMATION    # GIFs
    )
    
    app.add_handler(MessageHandler(media_filter, handle_media))


    log.info("Starting bot...")
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
