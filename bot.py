# -*- coding: utf-8 -*-
"""
QISQA VIDEO YUKLAB OLUVCHI BOT
Instagram Reels, TikTok, YouTube Shorts havolasini yuborsangiz,
bot videoni yuklab olib, to'g'ridan-to'g'ri Telegram'ga jo'natadi.

MUHIM CHEKLOV:
Oddiy Telegram bot orqali yuborilishi mumkin bo'lgan fayl hajmi
50 MB bilan chegaralangan (bu Telegram'ning o'zi qo'ygan chegara,
serverga bog'liq emas). Uzun/katta videolar (masalan to'liq YouTube
videolari) yubora olmaydi, xato xabarini qaytaradi.

Shaxsiy foydalanish uchun mo'ljallangan — boshqalarning kontentini
ruxsatsiz ommaviy tarqatishdan saqlaning.
"""

import asyncio
import logging
import os
import sqlite3
import tempfile
import uuid
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import Message, FSInputFile, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest
import yt_dlp

BOT_TOKEN = os.environ["BOT_TOKEN"]

# Faqat /stats buyrug'ini ko'ra oladigan shaxslar (vergul bilan ID kiriting).
# Botning o'zi (video yuklab olish) hammaga ochiq — bu ro'yxatga bog'liq emas.
_owner_ids_raw = os.environ.get("OWNER_CHAT_IDS", "").strip()
OWNER_CHAT_IDS = [int(x.strip()) for x in _owner_ids_raw.split(",") if x.strip()]

# Majburiy obuna: bot ishlatilishi uchun foydalanuvchi shu kanalga a'zo bo'lishi kerak.
# @kanal_username shaklida kiriting (masalan @allsave_channel). Bo'sh qoldirsangiz,
# obuna talab qilinmaydi. MUHIM: bot shu kanalga ADMIN sifatida qo'shilgan bo'lishi shart.
REQUIRED_CHANNEL = os.environ.get("REQUIRED_CHANNEL", "").strip()
BOT_USERNAME_TAG = os.environ.get("BOT_USERNAME_TAG", "@AllSaveUz_Bot").strip()

MAX_TELEGRAM_MB = 50
DB_PATH = os.environ.get("DB_PATH", "users.db")

# Video sifatini pasaytirib, fayl hajmini kichraytiradi — tezroq yuklanadi,
# 50 MB chegarasiga kamroq tegadi. Railway'da MAX_VIDEO_HEIGHT o'zgaruvchisi
# orqali sozlash mumkin (masalan 360, 480, 720). Standart: 480p.
MAX_VIDEO_HEIGHT = os.environ.get("MAX_VIDEO_HEIGHT", "480")

# Bir vaqtning o'zida nechta video yuklab olish mumkinligini cheklaydi —
# serverning protsessori/tarmog'i tiqilib qolmasligi uchun. Kerak bo'lsa
# Railway'da MAX_CONCURRENT_DOWNLOADS o'zgaruvchisi orqali oshirish/kamaytirish mumkin.
MAX_CONCURRENT_DOWNLOADS = int(os.environ.get("MAX_CONCURRENT_DOWNLOADS", "8"))
download_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("video-bot")

router = Router()


async def safe_edit(msg: Message, text: str, **kwargs):
    """edit_text'ni xavfsiz chaqiradi — agar matn eskisi bilan bir xil bo'lsa
    Telegram beradigan 'message is not modified' xatosini e'tiborsiz qoldiradi,
    boshqa har qanday xatoni esa qayta chiqaradi."""
    try:
        await msg.edit_text(text, **kwargs)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_seen TEXT
        )
    """)
    return conn


def track_user(user_id: int, username: str):
    conn = db()
    conn.execute(
        "INSERT OR IGNORE INTO users (user_id, username, first_seen) VALUES (?, ?, ?)",
        (user_id, username or "", datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()


def count_users() -> int:
    conn = db()
    n = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    conn.close()
    return n


async def is_subscribed(bot: Bot, user_id: int) -> bool:
    if not REQUIRED_CHANNEL:
        return True  # majburiy obuna sozlanmagan
    try:
        member = await bot.get_chat_member(chat_id=REQUIRED_CHANNEL, user_id=user_id)
        return member.status not in ("left", "kicked")
    except Exception as e:
        log.warning(f"Obuna tekshirishda xato: {e}")
        # Tekshira olmasak, ehtiyot bo'lib "obuna emas" deb hisoblaymiz
        return False


def subscribe_keyboard() -> InlineKeyboardMarkup:
    channel_url = f"https://t.me/{REQUIRED_CHANNEL.lstrip('@')}"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="\U0001F4E2 Kanalga o'tish", url=channel_url)],
        [InlineKeyboardButton(text="\u2705 A'zo bo'ldim, tekshirish", callback_data="check_sub")],
    ])


SUBSCRIBE_TEXT = (
    "\u26D4 Botdan foydalanish uchun avval quyidagi kanalga a'zo bo'ling, "
    "so'ng \"A'zo bo'ldim, tekshirish\" tugmasini bosing."
)

START_TEXT = (
    "Salom! Quyidagi platformalardan video havolasini yuboring — "
    "yuklab, sizga jo'nataman:\n\n"
    "\U0001F4F8 Instagram (Reels, postlar)\n"
    "\U0001F3B5 TikTok\n"
    "\u25B6\uFE0F YouTube (Shorts)\n"
    "\U0001F535 VK\n"
    "\U0001F537 Facebook\n"
    "\u274C Twitter/X\n"
    "\U0001F4CC Pinterest\n"
    "\U0001F47E Twitch (clip'lar)\n"
    "\U0001F536 Reddit\n\n"
    f"Eslatma: Telegram cheklovi tufayli faqat {MAX_TELEGRAM_MB} MB'gacha "
    "bo'lgan videolarni yubora olaman."
)


@router.message(F.text == "/start")
async def cmd_start(message: Message, bot: Bot):
    track_user(message.from_user.id, message.from_user.username)
    if not await is_subscribed(bot, message.from_user.id):
        await message.answer(SUBSCRIBE_TEXT, reply_markup=subscribe_keyboard())
        return
    await message.answer(START_TEXT)


@router.message(F.text == "/stats")
async def cmd_stats(message: Message):
    if not OWNER_CHAT_IDS or message.from_user.id not in OWNER_CHAT_IDS:
        return  # sozlanmagan yoki ruxsatsiz — jim turadi
    await message.answer(f"\U0001F465 Botdan foydalangan jami odamlar: {count_users()} kishi")


@router.message(F.text.startswith("/xabar"))
async def cmd_broadcast(message: Message, bot: Bot):
    if not OWNER_CHAT_IDS or message.from_user.id not in OWNER_CHAT_IDS:
        return

    parts = message.text.split("\n", 1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer(
            "Foydalanish: birinchi qatorga /xabar deb yozing, "
            "Shift+Enter bosib yangi qatorga o'ting, so'ng yubormoqchi "
            "bo'lgan matnni yozib, hammasini BITTA xabar sifatida yuboring."
        )
        return

    broadcast_text = parts[1]

    conn = db()
    user_ids = [row[0] for row in conn.execute("SELECT user_id FROM users").fetchall()]
    conn.close()

    if not user_ids:
        await message.answer("Hali hech kim ro'yxatda yo'q.")
        return

    progress = await message.answer(f"\u23F3 Yuborilmoqda... (0/{len(user_ids)})")
    sent, failed = 0, 0

    for i, uid in enumerate(user_ids, start=1):
        try:
            await bot.send_message(uid, broadcast_text)
            sent += 1
        except Exception as e:
            failed += 1
            log.warning(f"Xabar yuborilmadi ({uid}): {e}")
        if i % 25 == 0:
            try:
                await safe_edit(progress, f"\u23F3 Yuborilmoqda... ({i}/{len(user_ids)})")
            except Exception:
                pass
        await asyncio.sleep(0.05)  # Telegram limitiga urilib qolmaslik uchun

    await safe_edit(
        progress,
        f"\u2705 Tugadi!\nYuborildi: {sent} ta\nYetib bormadi (bloklagan/o'chirgan): {failed} ta"
    )


@router.callback_query(F.data == "check_sub")
async def cb_check_sub(callback: CallbackQuery, bot: Bot):
    if await is_subscribed(bot, callback.from_user.id):
        await safe_edit(callback.message, "\u2705 Rahmat! Endi videolarni yuborishingiz mumkin.")
        await callback.answer()
    else:
        await callback.answer("Hali kanalga a'zo bo'lmagansiz.", show_alert=True)


def _download_video_sync(url: str, ydl_opts: dict) -> dict:
    """Bloklaydigan (sinxron) yuklab olish — alohida threadda ishga tushiriladi,
    shunda bot boshqa foydalanuvchilarga bir vaqtda javob bera oladi.
    Video fayli bilan birga asl izohini (caption/description) ham qaytaradi."""
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        return {
            "path": ydl.prepare_filename(info),
            "title": (info.get("title") or "").strip(),
            "description": (info.get("description") or "").strip(),
            "uploader": (info.get("uploader") or info.get("uploader_id") or "").strip(),
        }


async def _download_with_retry(url: str, ydl_opts: dict, attempts: int = 2) -> dict:
    """Vaqtinchalik tarmoq/bloklanish xatolarida bir necha marta qayta urinadi
    (kutish bilan), doimiy xatolarda (masalan noto'g'ri havola) darhol
    yt_dlp.utils.DownloadError'ni yuqoriga uzatadi."""
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            return await asyncio.to_thread(_download_video_sync, url, ydl_opts)
        except yt_dlp.utils.DownloadError as e:
            last_error = e
            if attempt < attempts:
                log.warning(f"Yuklashda xato (urinish {attempt}/{attempts}), qayta urinilmoqda: {e}")
                await asyncio.sleep(2 * attempt)
            else:
                raise
    raise last_error


def _build_caption(result: dict) -> str:
    """Video bilan birga yuboriladigan izohni tayyorlaydi: asl izoh (agar bo'lsa)
    + botning o'z reklamasi. Telegram caption chegarasi (1024 belgi)dan oshmaydi."""
    izoh = result.get("description") or result.get("title") or ""

    footer = f"\n\nBu video {BOT_USERNAME_TAG} orqali yuklab olindi \U0001F4E5"
    max_izoh_len = 1024 - len(footer) - 5  # kichik zaxira

    if izoh and max_izoh_len > 10:
        if len(izoh) > max_izoh_len:
            izoh = izoh[:max_izoh_len].rstrip() + "..."
        body = izoh
    else:
        body = ""

    return (body + footer)[:1024]


@router.message(F.text.startswith("http"))
async def handle_link(message: Message, bot: Bot):
    track_user(message.from_user.id, message.from_user.username)

    if not await is_subscribed(bot, message.from_user.id):
        await message.answer(SUBSCRIBE_TEXT, reply_markup=subscribe_keyboard())
        return

    url = message.text.strip()
    status = await message.answer("\u23F3 Video yuklab olinmoqda...")

    tmp_dir = tempfile.mkdtemp()
    out_template = os.path.join(tmp_dir, f"{uuid.uuid4().hex}.%(ext)s")

    ydl_opts = {
        "outtmpl": out_template,
        "format": (
            f"best[height<={MAX_VIDEO_HEIGHT}][ext=mp4]/"
            f"best[height<={MAX_VIDEO_HEIGHT}]/"
            "best[ext=mp4]/best"
        ),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "max_filesize": MAX_TELEGRAM_MB * 1024 * 1024,
    }

    downloaded_path = None
    try:
        was_queued = download_semaphore.locked()
        if was_queued:
            await safe_edit(status, 
                "\u23F3 Hozir juda ko'p odam video yuklamoqda, navbatingizni kutmoqdaman..."
            )

        async with download_semaphore:
            if was_queued:
                await safe_edit(status, "\u23F3 Video yuklab olinmoqda...")
            result = await _download_with_retry(url, ydl_opts)
            downloaded_path = result.get("path")

        if not downloaded_path or not os.path.exists(downloaded_path):
            await safe_edit(status, 
                "\u274C Video topilmadi yoki yuklab bo'lmadi. "
                "Havola to'g'riligini tekshiring."
            )
            return

        size_mb = os.path.getsize(downloaded_path) / (1024 * 1024)
        if size_mb > MAX_TELEGRAM_MB:
            await safe_edit(status, 
                f"\u274C Video {size_mb:.1f} MB — bu {MAX_TELEGRAM_MB} MB "
                "Telegram chegarasidan katta, yubora olmayman."
            )
            return

        await safe_edit(status, "\u2705 Yuklandi, yuborilmoqda...")
        await bot.send_video(
            chat_id=message.chat.id,
            video=FSInputFile(downloaded_path),
            caption=_build_caption(result),
        )
        await status.delete()

    except yt_dlp.utils.DownloadError as e:
        log.warning(f"Download xato: {e}")
        await safe_edit(status, 
            "\u274C Videoni yuklab bo'lmadi. Havola noto'g'ri, video "
            "o'chirilgan yoki maxfiy bo'lishi mumkin."
        )
    except Exception as e:
        log.error(f"Kutilmagan xato: {e}")
        await safe_edit(status, "\u274C Xatolik yuz berdi, birozdan keyin qayta urinib ko'ring.")
    finally:
        # Vaqtinchalik fayllarni tozalash — serverda joy to'lib qolmasligi uchun
        try:
            if downloaded_path and os.path.exists(downloaded_path):
                os.remove(downloaded_path)
            os.rmdir(tmp_dir)
        except OSError:
            pass


async def main():
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=None))
    dp = Dispatcher()
    dp.include_router(router)
    log.info("Video bot ishga tushmoqda...")
    await dp.start_polling(bot, allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    asyncio.run(main())
