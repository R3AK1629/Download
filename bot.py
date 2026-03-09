import asyncio
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlparse

from dotenv import load_dotenv
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputMediaVideo,
    Update,
)
from telegram.constants import ChatAction
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
import yt_dlp


# =========================
# ENV / CONFIG
# =========================
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing in .env")

ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}

MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "48"))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024

COOKIES_FILE = os.getenv("COOKIES_FILE", "").strip()

URL_RE = re.compile(r"(https?://[^\s]+)", re.IGNORECASE)

SITE_NAMES = {
    "tiktok.com": "TikTok",
    "vt.tiktok.com": "TikTok",
    "vm.tiktok.com": "TikTok",
    "douyin.com": "Douyin",
    "facebook.com": "Facebook",
    "fb.watch": "Facebook",
    "instagram.com": "Instagram",
    "youtube.com": "YouTube",
    "youtu.be": "YouTube",
    "x.com": "X",
    "twitter.com": "X",
    "xiaohongshu.com": "Rednote / Xiaohongshu",
    "rednote.com": "Rednote / Xiaohongshu",
}

# per-user simple memory
USER_STATE = {}


# =========================
# HELPERS
# =========================
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS if ADMIN_IDS else False


def extract_url(text: str) -> Optional[str]:
    if not text:
        return None
    m = URL_RE.search(text)
    return m.group(1) if m else None


def detect_site(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower().replace("www.", "")
        for key, name in SITE_NAMES.items():
            if host == key or host.endswith("." + key):
                return name
        return host or "Unknown"
    except Exception:
        return "Unknown"


def safe_name(text: str) -> str:
    bad = '<>:"/\\|?*\n\r\t'
    for ch in bad:
        text = text.replace(ch, "_")
    return text[:120].strip() or "media"


def file_mime(path: Path) -> str:
    mime, _ = mimetypes.guess_type(str(path))
    return mime or "application/octet-stream"


def is_image(path: Path) -> bool:
    return file_mime(path).startswith("image/")


def is_video(path: Path) -> bool:
    return file_mime(path).startswith("video/")


def is_audio(path: Path) -> bool:
    return file_mime(path).startswith("audio/")


def sort_media_files(paths: List[Path]) -> List[Path]:
    def score(p: Path):
        mime = file_mime(p)
        kind = 0
        if mime.startswith("image/"):
            kind = 3
        elif mime.startswith("video/"):
            kind = 2
        elif mime.startswith("audio/"):
            kind = 1
        return (kind, p.stat().st_size)
    return sorted(paths, key=score, reverse=True)


def list_all_files(folder: Path) -> List[Path]:
    return [p for p in folder.rglob("*") if p.is_file()]


def find_single_best_file(folder: Path, prefer_audio: bool = False) -> Optional[Path]:
    files = list_all_files(folder)
    if not files:
        return None

    def score(p: Path):
        mime = file_mime(p)
        size = p.stat().st_size
        if prefer_audio:
            if mime.startswith("audio/"):
                return (3, size)
            if mime.startswith("video/"):
                return (2, size)
            if mime.startswith("image/"):
                return (1, size)
            return (0, size)
        else:
            if mime.startswith("video/"):
                return (3, size)
            if mime.startswith("image/"):
                return (2, size)
            if mime.startswith("audio/"):
                return (1, size)
            return (0, size)

    files.sort(key=score, reverse=True)
    return files[0]


def find_images(folder: Path) -> List[Path]:
    files = [p for p in list_all_files(folder) if is_image(p)]
    return sort_media_files(files)


def get_caption(info: dict, mode: str, site: str) -> str:
    title = (info.get("title") or "Downloaded media").strip()
    uploader = (info.get("uploader") or info.get("channel") or "").strip()
    lines = [title]
    if uploader:
        lines.append(f"by {uploader}")
    lines.append(f"site: {site}")
    lines.append(f"mode: {mode}")
    return "\n".join(lines)[:1000]


def keyboard_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎬 Video HD", callback_data="dl:video_hd"),
            InlineKeyboardButton("📱 Video 720p", callback_data="dl:video_720"),
        ],
        [
            InlineKeyboardButton("🎵 MP3", callback_data="dl:audio_mp3"),
            InlineKeyboardButton("🖼 Photos", callback_data="dl:photos"),
        ],
        [
            InlineKeyboardButton("⚡ Auto", callback_data="dl:auto"),
            InlineKeyboardButton("🗜 Compress", callback_data="dl:compress"),
        ],
    ])


def keyboard_mode() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Auto", callback_data="mode:auto"),
            InlineKeyboardButton("Video HD", callback_data="mode:video_hd"),
            InlineKeyboardButton("Video 720p", callback_data="mode:video_720"),
        ],
        [
            InlineKeyboardButton("MP3", callback_data="mode:audio_mp3"),
            InlineKeyboardButton("Photos", callback_data="mode:photos"),
            InlineKeyboardButton("Compress", callback_data="mode:compress"),
        ],
    ])


def ydl_base_opts(out_dir: str) -> dict:
    outtmpl = os.path.join(out_dir, "%(playlist_index|)s%(title).100s [%(id)s].%(ext)s")
    opts = {
        "outtmpl": outtmpl,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "windowsfilenames": True,
        "ignoreerrors": False,
        "restrictfilenames": False,
        "nopart": False,
        "concurrent_fragment_downloads": 4,
    }
    if COOKIES_FILE and os.path.exists(COOKIES_FILE):
        opts["cookiefile"] = COOKIES_FILE
    return opts


def ydl_opts_for_mode(mode: str, out_dir: str) -> dict:
    opts = ydl_base_opts(out_dir)

    if mode == "video_hd":
        opts.update({
            "format": "bv*+ba/b",
            "merge_output_format": "mp4",
        })
    elif mode == "video_720":
        opts.update({
            "format": "mp4[height<=720]/best[height<=720]/bv*[height<=720]+ba/b[height<=720]/b",
            "merge_output_format": "mp4",
        })
    elif mode == "audio_mp3":
        opts.update({
            "format": "bestaudio/best",
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }
            ],
        })
    elif mode == "photos":
        opts.update({
            "format": "best",
            "writethumbnail": False,
        })
    elif mode == "compress":
        opts.update({
            "format": "mp4[height<=720]/best[height<=720]/bv*[height<=720]+ba/b",
            "merge_output_format": "mp4",
        })
    else:  # auto
        opts.update({
            "format": "bestvideo*+bestaudio/best",
            "merge_output_format": "mp4",
        })

    return opts


def run_yt_dlp_download(url: str, mode: str, out_dir: str) -> dict:
    opts = ydl_opts_for_mode(mode, out_dir)
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        return info or {}


def ffmpeg_installed() -> bool:
    try:
        result = subprocess.run(
            ["ffmpeg", "-version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        return result.returncode == 0
    except Exception:
        return False


def compress_video_for_telegram(input_file: Path, output_file: Path) -> bool:
    """
    Conservative compression for Telegram-friendly delivery.
    """
    cmd = [
        "ffmpeg",
        "-y",
        "-i", str(input_file),
        "-vf", "scale='min(1280,iw)':-2",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "30",
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        str(output_file),
    ]
    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    return result.returncode == 0 and output_file.exists()


async def tg_send_album(chat_id: int, context: ContextTypes.DEFAULT_TYPE, paths: List[Path], caption: str):
    """
    Telegram media groups support 2-10 items per group.
    """
    chunk_size = 10
    for i in range(0, len(paths), chunk_size):
        chunk = paths[i:i + chunk_size]
        media_group = []
        open_files = []

        try:
            for idx, path in enumerate(chunk):
                f = open(path, "rb")
                open_files.append(f)

                item_caption = caption if idx == 0 else None
                if is_video(path):
                    media_group.append(InputMediaVideo(media=f, caption=item_caption))
                else:
                    media_group.append(InputMediaPhoto(media=f, caption=item_caption))

            await context.bot.send_media_group(chat_id=chat_id, media=media_group)
        finally:
            for f in open_files:
                try:
                    f.close()
                except Exception:
                    pass


async def run_download_async(url: str, mode: str, out_dir: str) -> dict:
    return await asyncio.to_thread(run_yt_dlp_download, url, mode, out_dir)


async def send_status(chat_id: int, context: ContextTypes.DEFAULT_TYPE, text: str):
    await context.bot.send_message(chat_id=chat_id, text=text)


# =========================
# COMMANDS
# =========================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Send a public social media link.\n\n"
        "Modes:\n"
        "🎬 Video HD\n"
        "📱 Video 720p\n"
        "🎵 MP3\n"
        "🖼 Photos\n"
        "⚡ Auto\n"
        "🗜 Compress\n\n"
        "Commands:\n"
        "/mode - set default mode\n"
        "/status - show config\n"
        "/cancel - clear saved link\n"
        "/help - help"
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Usage:\n"
        "1. Send link\n"
        "2. Press a button\n"
        "3. Bot downloads and sends file\n\n"
        "Tips:\n"
        "- Add cookies.txt for harder public pages\n"
        "- MP3 and Compress need ffmpeg\n"
        "- Photos mode is best for Instagram/Xiaohongshu multi-image posts"
    )


async def mode_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Choose your default mode:",
        reply_markup=keyboard_mode()
    )


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    USER_STATE.pop(uid, None)
    await update.message.reply_text("Cleared saved link and mode cache.")


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    state = USER_STATE.get(uid, {})
    await update.message.reply_text(
        f"ffmpeg: {'OK' if ffmpeg_installed() else 'NOT FOUND'}\n"
        f"default_mode: {state.get('default_mode', 'not set')}\n"
        f"last_url: {state.get('last_url', 'none')}\n"
        f"max_upload_mb: {MAX_UPLOAD_MB}\n"
        f"cookies_file: {'YES' if COOKIES_FILE and os.path.exists(COOKIES_FILE) else 'NO'}"
    )


# =========================
# CALLBACKS
# =========================
async def mode_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    uid = query.from_user.id
    USER_STATE.setdefault(uid, {})
    mode = query.data.split(":", 1)[1]
    USER_STATE[uid]["default_mode"] = mode

    await query.edit_message_text(f"Default mode set to: {mode}")


async def download_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    uid = query.from_user.id
    state = USER_STATE.get(uid, {})
    url = state.get("last_url")

    if not url:
        await query.edit_message_text("No link saved. Send a link first.")
        return

    mode = query.data.split(":", 1)[1]
    await query.edit_message_text(f"Downloading with mode: {mode}")
    await process_download(update, context, url, mode)


# =========================
# MAIN LINK HANDLER
# =========================
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    url = extract_url(update.message.text)
    if not url:
        await update.message.reply_text("Please send a valid social media URL.")
        return

    uid = update.effective_user.id
    USER_STATE.setdefault(uid, {})
    USER_STATE[uid]["last_url"] = url

    site = detect_site(url)
    default_mode = USER_STATE[uid].get("default_mode")

    if default_mode:
        await update.message.reply_text(
            f"Detected site: {site}\nUsing default mode: {default_mode}"
        )
        await process_download(update, context, url, default_mode)
        return

    await update.message.reply_text(
        f"Detected site: {site}\nChoose download type:",
        reply_markup=keyboard_main()
    )


# =========================
# DOWNLOAD + SEND
# =========================
async def process_download(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str, mode: str):
    chat_id = update.effective_chat.id
    site = detect_site(url)
    tmp_dir = tempfile.mkdtemp(prefix="ultra_bot_")
    tmp_path = Path(tmp_dir)

    try:
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        t0 = time.time()

        if mode in {"audio_mp3", "compress"} and not ffmpeg_installed():
            await send_status(chat_id, context, "ffmpeg is not installed or not in PATH.")
            return

        info = await run_download_async(url, mode, tmp_dir)
        caption = get_caption(info, mode, site)

        # Photos mode: try sending multiple images/videos as album
        if mode == "photos":
            images = [p for p in list_all_files(tmp_path) if is_image(p) or is_video(p)]
            images = sort_media_files(images)

            if not images:
                await send_status(chat_id, context, "No photo/video items found for this post.")
                return

            # Filter too-large items out of album
            sendable = [p for p in images if p.stat().st_size <= MAX_UPLOAD_BYTES]
            if not sendable:
                await send_status(chat_id, context, "All media items are too large to send.")
                return

            await tg_send_album(chat_id, context, sendable[:20], caption)
            elapsed = round(time.time() - t0, 1)
            await send_status(chat_id, context, f"Done in {elapsed}s")
            return

        prefer_audio = mode == "audio_mp3"
        media = find_single_best_file(tmp_path, prefer_audio=prefer_audio)
        if not media:
            await send_status(chat_id, context, "Download failed. No media file found.")
            return

        # Compress if requested or if auto mode file is too large and ffmpeg is available
        final_file = media
        if mode == "compress" and is_video(media):
            compressed = tmp_path / f"{media.stem}_compressed.mp4"
            ok = await asyncio.to_thread(compress_video_for_telegram, media, compressed)
            if ok:
                final_file = compressed

        elif mode == "auto" and is_video(media) and media.stat().st_size > MAX_UPLOAD_BYTES and ffmpeg_installed():
            compressed = tmp_path / f"{media.stem}_compressed.mp4"
            ok = await asyncio.to_thread(compress_video_for_telegram, media, compressed)
            if ok and compressed.stat().st_size < media.stat().st_size:
                final_file = compressed

        size = final_file.stat().st_size
        if size > MAX_UPLOAD_BYTES:
            mb = round(size / (1024 * 1024), 2)
            await send_status(
                chat_id,
                context,
                f"Downloaded, but file is still too large to send.\nSize: {mb} MB\nLimit: {MAX_UPLOAD_MB} MB"
            )
            return

        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_DOCUMENT)

        with open(final_file, "rb") as f:
            if is_audio(final_file):
                await context.bot.send_audio(
                    chat_id=chat_id,
                    audio=f,
                    caption=caption,
                    title=final_file.stem,
                )
            elif is_image(final_file):
                await context.bot.send_photo(
                    chat_id=chat_id,
                    photo=f,
                    caption=caption,
                )
            elif is_video(final_file):
                await context.bot.send_video(
                    chat_id=chat_id,
                    video=f,
                    caption=caption,
                    supports_streaming=True,
                )
            else:
                await context.bot.send_document(
                    chat_id=chat_id,
                    document=f,
                    caption=caption,
                )

        elapsed = round(time.time() - t0, 1)
        await send_status(chat_id, context, f"Done in {elapsed}s")

    except yt_dlp.utils.DownloadError as e:
        await send_status(
            chat_id,
            context,
            "Download failed.\n"
            "Possible reasons:\n"
            "- post is private\n"
            "- login/cookies required\n"
            "- extractor/site changed\n"
            "- media restricted\n"
            f"\nError: {str(e)[:500]}"
        )
    except Exception as e:
        await send_status(chat_id, context, f"Error: {str(e)[:700]}")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# =========================
# ADMIN
# =========================
async def admin_broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("Admin only.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /broadcast your message")
        return

    text = " ".join(context.args)
    users = list(USER_STATE.keys())
    sent = 0

    for uid in users:
        try:
            await context.bot.send_message(chat_id=uid, text=text)
            sent += 1
        except Exception:
            pass

    await update.message.reply_text(f"Broadcast sent to {sent} users.")


# =========================
# START APP
# =========================
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("mode", mode_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("broadcast", admin_broadcast_cmd))

    app.add_handler(CallbackQueryHandler(mode_callback, pattern=r"^mode:"))
    app.add_handler(CallbackQueryHandler(download_callback, pattern=r"^dl:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    print("ULTRA BOT RUNNING...")
    app.run_polling()


if __name__ == "__main__":
    main()