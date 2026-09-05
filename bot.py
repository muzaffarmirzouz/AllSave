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
import base64
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

# Instagram Stories kabi ba'zi kontentni yuklash uchun "cookies" (sessiya) kerak
# bo'lishi mumkin. IG_COOKIES_B64 — Netscape formatidagi cookies.txt faylining
# base64 kodlangan matni (Railway Variables'ga shu ko'rinishda qo'yiladi, hech
# qachon GitHub'ga yuklanmaydi). Bo'sh qoldirilsa, bot avvalgidek ishlaydi —
# faqat cookies talab qiladigan kontent (masalan ba'zi Stories) yuklanmasligi mumkin.
_ig_cookies_b64 = os.environ.get("IG_COOKIES_B64", "").strip()
IG_COOKIES_FILE = None
if _ig_cookies_b64:
    try:
        _cookies_path = os.path.join(tempfile.gettempdir(), "ig_cookies.txt")
        with open(_cookies_path, "wb") as _f:
            _f.write(base64.b64decode(_ig_cookies_b64))
        IG_COOKIES_FILE = _cookies_path
    except Exception as _e:
        logging.getLogger("video-bot").warning(f"IG_COOKIES_B64'ni o'qishda xato: {_e}")

# Video'larga pastki-markazga qo'yiladigan animatsion GIF logo (watermark).
# LOGO_PATH — doimiy (Railway Volume'dagi) fayl manzili, DB_PATH bilan bir xil
# papkada saqlanadi, shuning uchun qayta deploy/restart'da HAM YO'QOLMAYDI.
# Botga to'g'ridan-to'g'ri /setlogo orqali yangi GIF yuborib, uni istalgan
# vaqt almashtirish mumkin — Railway Variables'ga qayta kirish shart emas.
LOGO_PATH = os.environ.get("LOGO_PATH", os.path.join(os.path.dirname(DB_PATH) or ".", "logo.gif"))
LOGO_GIF_FILE = LOGO_PATH if os.path.exists(LOGO_PATH) else None

# LOGO_GIF_B64 — ixtiyoriy, FAQAT birinchi marta (hali hech qanday logo
# saqlanmagan bo'lsa) boshlang'ich qiymat sifatida ishlatiladi.
if not LOGO_GIF_FILE:
    _logo_gif_b64 = os.environ.get("LOGO_GIF_B64", "").strip()
    if _logo_gif_b64:
        try:
            os.makedirs(os.path.dirname(LOGO_PATH) or ".", exist_ok=True)
            with open(LOGO_PATH, "wb") as _f:
                _f.write(base64.b64decode(_logo_gif_b64))
            LOGO_GIF_FILE = LOGO_PATH
        except Exception as _e:
            logging.getLogger("video-bot").warning(f"LOGO_GIF_B64'ni o'qishda xato: {_e}")

# Logo qayerga qo'yilishini belgilaydi. Doimiy joyda (LOGO_PATH bilan bir xil
# papkada) kichik matn fayl sifatida saqlanadi, shuning uchun /setposition
# orqali tanlangan joy ham qayta deploy/restart'dan keyin ham eslab qolinadi.
LOGO_POSITION_PATH = os.path.join(os.path.dirname(LOGO_PATH) or ".", "logo_position.txt")

LOGO_POSITIONS = {
    "top_left": ("Chap yuqori", "20:20"),
    "top_center": ("Yuqori markaz", "(main_w-overlay_w)/2:20"),
    "top_right": ("O'ng yuqori", "main_w-overlay_w-20:20"),
    "center": ("Markaz", "(main_w-overlay_w)/2:(main_h-overlay_h)/2"),
    "bottom_left": ("Chap pastki", "20:main_h-overlay_h-20"),
    "bottom_center": ("Pastki markaz", "(main_w-overlay_w)/2:main_h-overlay_h-20"),
    "bottom_right": ("O'ng pastki", "main_w-overlay_w-20:main_h-overlay_h-20"),
}

LOGO_POSITION = "bottom_center"
if os.path.exists(LOGO_POSITION_PATH):
    try:
        with open(LOGO_POSITION_PATH, "r") as _f:
            _saved_pos = _f.read().strip()
        if _saved_pos in LOGO_POSITIONS:
            LOGO_POSITION = _saved_pos
    except Exception:
        pass

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


def _add_watermark_sync(input_path: str, output_path: str) -> None:
    """FFmpeg orqali videoga (joriy LOGO_POSITION sozlamasi bo'yicha)
    animatsion GIF logo qo'yadi. GIF butun video davomiyligiga yetguncha
    aylantiriladi (loop). Bloklaydigan (sinxron) funksiya — alohida
    threadda ishga tushiriladi."""
    import subprocess

    _, xy = LOGO_POSITIONS[LOGO_POSITION]
    cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-stream_loop", "-1", "-i", LOGO_GIF_FILE,
        "-filter_complex",
        "[1:v]scale=150:-1[logo];"
        f"[0:v][logo]overlay={xy}:shortest=1",
        "-c:a", "copy",
        output_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg xato: {result.stderr[-500:]}")


# Kim /setlogo buyrug'ini yuborib, hozir yangi logo GIF yuborishini kutayotganini
# saqlaydi (bitta oddiy to'plam — alohida FSM kutubxonasi shart emas).
_awaiting_logo_from: set = set()


@router.message(F.text == "/setlogo")
async def cmd_setlogo(message: Message):
    """Bot egasi yangi logo (watermark) GIF'ini o'rnatishni boshlaydi."""
    if not OWNER_CHAT_IDS or message.from_user.id not in OWNER_CHAT_IDS:
        return
    _awaiting_logo_from.add(message.from_user.id)
    await message.answer(
        "\U0001F3A8 Yangi logo sifatida ishlatiladigan GIF'ni hozir menga yuboring.\n\n"
        "(GIF'ni Telegram orqali oddiy yuborsangiz yetarli \u2014 alohida buyruq kerak emas.)"
    )


@router.message(F.text == "/setposition")
async def cmd_setposition(message: Message):
    """Bot egasi logo videoning qaysi qismida chiqishini tugmalar orqali tanlaydi."""
    if not OWNER_CHAT_IDS or message.from_user.id not in OWNER_CHAT_IDS:
        return
    buttons = []
    row = []
    for key, (label, _) in LOGO_POSITIONS.items():
        mark = "\u2705 " if key == LOGO_POSITION else ""
        row.append(InlineKeyboardButton(text=f"{mark}{label}", callback_data=f"logopos:{key}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    await message.answer(
        "\U0001F4CD Logo videoning qaysi qismida chiqsin?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@router.callback_query(F.data.startswith("logopos:"))
async def cb_set_position(callback: CallbackQuery):
    if not OWNER_CHAT_IDS or callback.from_user.id not in OWNER_CHAT_IDS:
        await callback.answer()
        return
    global LOGO_POSITION
    key = callback.data.split(":", 1)[1]
    if key not in LOGO_POSITIONS:
        await callback.answer("Noma'lum joy.")
        return
    LOGO_POSITION = key
    try:
        with open(LOGO_POSITION_PATH, "w") as _f:
            _f.write(key)
    except Exception as e:
        log.warning(f"LOGO_POSITION saqlashda xato: {e}")
    label = LOGO_POSITIONS[key][0]
    await callback.message.edit_text(f"\u2705 Logo joyi o'zgartirildi: {label}")
    await callback.answer()


@router.message(F.animation | F.document)
async def handle_logo_upload(message: Message, bot: Bot):
    """/setlogo buyrug'idan keyin yuborilgan GIF'ni doimiy joyga saqlaydi."""
    if not OWNER_CHAT_IDS or message.from_user.id not in OWNER_CHAT_IDS:
        return
    if message.from_user.id not in _awaiting_logo_from:
        return
    _awaiting_logo_from.discard(message.from_user.id)

    file_id = message.animation.file_id if message.animation else message.document.file_id
    try:
        file_info = await bot.get_file(file_id)
        os.makedirs(os.path.dirname(LOGO_PATH) or ".", exist_ok=True)
        await bot.download_file(file_info.file_path, destination=LOGO_PATH)
        global LOGO_GIF_FILE
        LOGO_GIF_FILE = LOGO_PATH
        await message.answer("\u2705 Yangi logo saqlandi! Endi shu GIF video'larga qo'yiladi.")
    except Exception as e:
        log.error(f"Logo saqlashda xato: {e}")
        await message.answer("\u274C Logo saqlashda xatolik yuz berdi, qayta urinib ko'ring.")


@router.message(F.video)
async def handle_owner_video(message: Message, bot: Bot):
    """Har qanday foydalanuvchi video yuborsa, unga GIF logo (watermark)
    qo'yib qaytaradi. Boshqa buyruqlar (link yuklab olish) kabi majburiy
    obunani ham talab qiladi."""
    if not LOGO_GIF_FILE:
        return  # logo hali sozlanmagan — jim o'tkazib yuboriladi
    track_user(message.from_user.id, message.from_user.username)

    if not await is_subscribed(bot, message.from_user.id):
        await message.answer(SUBSCRIBE_TEXT, reply_markup=subscribe_keyboard())
        return

    status = await message.answer("\U0001F3A8 Logo qo'yilmoqda...")
    input_path = None
    output_path = None
    try:
        file_info = await bot.get_file(message.video.file_id)
        input_path = os.path.join(tempfile.gettempdir(), f"{uuid.uuid4()}.mp4")
        output_path = os.path.join(tempfile.gettempdir(), f"{uuid.uuid4()}_logo.mp4")
        await bot.download_file(file_info.file_path, destination=input_path)

        await asyncio.wait_for(
            asyncio.to_thread(_add_watermark_sync, input_path, output_path),
            timeout=180,
        )

        await bot.send_video(chat_id=message.chat.id, video=FSInputFile(output_path))
        await status.delete()
    except asyncio.TimeoutError:
        await safe_edit(status, "\u274C Vaqt tugadi (video juda uzun bo'lishi mumkin).")
    except Exception as e:
        log.error(f"Watermark xatosi: {e}")
        await safe_edit(status, "\u274C Logo qo'yishda xatolik yuz berdi.")
    finally:
        for p in (input_path, output_path):
            if p and os.path.exists(p):
                os.remove(p)


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
    if IG_COOKIES_FILE and "instagram.com" in url:
        ydl_opts["cookiefile"] = IG_COOKIES_FILE

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
        err_text = str(e).lower()
        if "login" in err_text or "rate-limit" in err_text or "restricted" in err_text:
            await safe_edit(status,
                "\u274C Bu kontentni yuklab bo'lmadi \u2014 Instagram bunday havolalar uchun "
                "\"tizimga kirgan\" holatni talab qiladi. Agar bu takrorlansa, bot egasiga xabar bering."
            )
        else:
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
