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
import re
import shutil
import sqlite3
import tempfile
import uuid
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import Message, FSInputFile, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, InputMediaPhoto
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
import yt_dlp

# /caption rejimi — video/link'ga o'zbekcha titr (hardsub) qo'shadi.
# MUHIM: bu quyidagi `router`dan OLDIN include qilinishi shart (pastdagi
# main() funksiyasiga qarang).
from caption_mode import caption_router


class WatermarkStates(StatesGroup):
    # "Videoga Logo qo'yish" tugmasi bosilgandan keyin, FAQAT keyingi bitta
    # video shaxsiy logo bilan qaytadi — shundan keyin holat avtomatik
    # tozalanadi (caption rejimi bilan bir xil, izchil xatti-harakat uchun).
    waiting_video = State()

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

# Doimiy (Railway Volume'dagi) papka — DB_PATH bilan bir xil joyda, shuning
# uchun qayta deploy/restart'da fayllar YO'QOLMAYDI.
PERSIST_DIR = os.path.dirname(DB_PATH) or "."


def _find_existing_persist_file(name: str) -> str:
    p = os.path.join(PERSIST_DIR, name)
    return p if os.path.exists(p) else None


# Instagram Stories kabi ba'zi kontentni yuklash uchun "cookies" (sessiya) kerak
# bo'lishi mumkin. Botga to'g'ridan-to'g'ri /setcookies_ig orqali yangi
# cookies.txt yuborib, uni istalgan vaqt yangilash mumkin (Railway Variables'ga
# kirish shart emas). IG_COOKIES_B64 — FAQAT birinchi marta (hali hech qanday
# fayl saqlanmagan bo'lsa) boshlang'ich qiymat sifatida ishlatiladi.
IG_COOKIES_FILE = _find_existing_persist_file("ig_cookies.txt")
if not IG_COOKIES_FILE:
    _ig_cookies_b64 = os.environ.get("IG_COOKIES_B64", "").strip()
    if _ig_cookies_b64:
        try:
            os.makedirs(PERSIST_DIR, exist_ok=True)
            _cookies_path = os.path.join(PERSIST_DIR, "ig_cookies.txt")
            with open(_cookies_path, "wb") as _f:
                _f.write(base64.b64decode(_ig_cookies_b64))
            IG_COOKIES_FILE = _cookies_path
        except Exception as _e:
            logging.getLogger("video-bot").warning(f"IG_COOKIES_B64'ni o'qishda xato: {_e}")

# YouTube "Sign in to confirm you're not a bot" tekshiruvini chetlab o'tish
# uchun ham xuddi shunday cookies kerak bo'lishi mumkin. Botga /setcookies_yt
# orqali yangilanadi. YT_COOKIES_B64 — faqat boshlang'ich qiymat.
YT_COOKIES_FILE = _find_existing_persist_file("yt_cookies.txt")
if not YT_COOKIES_FILE:
    _yt_cookies_b64 = os.environ.get("YT_COOKIES_B64", "").strip()
    if _yt_cookies_b64:
        try:
            os.makedirs(PERSIST_DIR, exist_ok=True)
            _yt_cookies_path = os.path.join(PERSIST_DIR, "yt_cookies.txt")
            with open(_yt_cookies_path, "wb") as _f:
                _f.write(base64.b64decode(_yt_cookies_b64))
            YT_COOKIES_FILE = _yt_cookies_path
        except Exception as _e:
            logging.getLogger("video-bot").warning(f"YT_COOKIES_B64'ni o'qishda xato: {_e}")

# YouTube PO Token provider xizmatining (bgutil-ytdlp-pot-provider) ichki
# manzili — Railway'da alohida servis sifatida joylashtiriladi. Masalan:
# "http://bgutil-pot-provider.railway.internal:4416"
POT_PROVIDER_URL = os.environ.get("POT_PROVIDER_URL", "").strip()

# Ixtiyoriy proxy — sozlansa, barcha yt-dlp so'rovlari (video yuklab olish,
# Instagram/YouTube bilan bog'lanish) shu proxy orqali o'tadi. Bu Instagram
# cookie sessiyasining tezroq eskirib qolishining oldini olish uchun foydali
# (statik rezidensial/ISP proxy Instagram'ga "oddiy uy interneti"dek
# ko'rinadi, Railway'ning datacenter IP'sidan farqli). Format:
# "http://user:pass@host:port" yoki "socks5://user:pass@host:port".
# Bo'sh qoldirilsa (standart), hech narsa o'zgarmaydi.
PROXY_URL = os.environ.get("PROXY_URL", "").strip()

# Video'larga pastki-markazga qo'yiladigan animatsion GIF logo (watermark).
# LOGO_PATH — doimiy (Railway Volume'dagi) fayl manzili, DB_PATH bilan bir xil
# papkada saqlanadi, shuning uchun qayta deploy/restart'da HAM YO'QOLMAYDI.
# Botga to'g'ridan-to'g'ri /setlogo orqali yangi GIF yuborib, uni istalgan
# vaqt almashtirish mumkin — Railway Variables'ga qayta kirish shart emas.
# Video'larga pastki-markazga qo'yiladigan animatsion logo (watermark) —
# GIF yoki WEBM (Telegram video-stiker) bo'lishi mumkin. LOGO_DIR — doimiy
# (Railway Volume'dagi) papka, DB_PATH bilan bir xil joyda, shuning uchun
# qayta deploy/restart'da HAM YO'QOLMAYDI. Botga to'g'ridan-to'g'ri /setlogo
# orqali yangi fayl yuborib, uni istalgan vaqt almashtirish mumkin.
LOGO_DIR = os.environ.get("LOGO_DIR", PERSIST_DIR)
USER_LOGOS_DIR = os.path.join(LOGO_DIR, "users")


def _find_existing_logo() -> str:
    """LOGO_DIR ichida logo.gif yoki logo.webm bor-yo'qligini tekshiradi
    (bu — bot egasining ASOSIY/standart logotipi, hech kim shaxsiy logo
    o'rnatmagan bo'lsa ishlatiladi)."""
    for ext in (".webm", ".apng", ".gif"):
        p = os.path.join(LOGO_DIR, f"logo{ext}")
        if os.path.exists(p):
            return p
    return None


def _find_user_logo(user_id: int) -> str:
    """Ma'lum bir foydalanuvchining SHAXSIY logotipini qidiradi."""
    d = os.path.join(USER_LOGOS_DIR, str(user_id))
    for ext in (".webm", ".apng", ".gif"):
        p = os.path.join(d, f"logo{ext}")
        if os.path.exists(p):
            return p
    return None


LOGO_GIF_FILE = _find_existing_logo()  # bot egasining standart logotipi

# LOGO_GIF_B64 — ixtiyoriy, FAQAT birinchi marta (hali hech qanday logo
# saqlanmagan bo'lsa) boshlang'ich qiymat sifatida ishlatiladi (GIF sifatida).
if not LOGO_GIF_FILE:
    _logo_gif_b64 = os.environ.get("LOGO_GIF_B64", "").strip()
    if _logo_gif_b64:
        try:
            os.makedirs(LOGO_DIR, exist_ok=True)
            _seed_path = os.path.join(LOGO_DIR, "logo.gif")
            with open(_seed_path, "wb") as _f:
                _f.write(base64.b64decode(_logo_gif_b64))
            LOGO_GIF_FILE = _seed_path
        except Exception as _e:
            logging.getLogger("video-bot").warning(f"LOGO_GIF_B64'ni o'qishda xato: {_e}")

LOGO_POSITIONS = {
    "top_left": "20:20",
    "top_center": "(main_w-overlay_w)/2:20",
    "top_right": "main_w-overlay_w-20:20",
    "center": "(main_w-overlay_w)/2:(main_h-overlay_h)/2",
    "bottom_left": "20:main_h-overlay_h-main_h*0.15",
    "bottom_center": "(main_w-overlay_w)/2:main_h-overlay_h-main_h*0.25",
    "bottom_right": "main_w-overlay_w-20:main_h-overlay_h-main_h*0.15",
}

POSITION_LABELS = {
    "uz": {
        "top_left": "Chap yuqori", "top_center": "Yuqori markaz", "top_right": "O'ng yuqori",
        "center": "Markaz", "bottom_left": "Chap pastki", "bottom_center": "Pastki markaz",
        "bottom_right": "O'ng pastki",
    },
    "ru": {
        "top_left": "Верхний левый", "top_center": "Верхний центр", "top_right": "Верхний правый",
        "center": "Центр", "bottom_left": "Нижний левый", "bottom_center": "Нижний центр",
        "bottom_right": "Нижний правый",
    },
    "en": {
        "top_left": "Top left", "top_center": "Top center", "top_right": "Top right",
        "center": "Center", "bottom_left": "Bottom left", "bottom_center": "Bottom center",
        "bottom_right": "Bottom right",
    },
}

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
            first_seen TEXT,
            lang TEXT,
            logo_position TEXT
        )
    """)
    # Eski bazalarda ba'zi ustunlar bo'lmasligi mumkin — xavfsiz qo'shamiz.
    for col in ("lang", "logo_position"):
        try:
            conn.execute(f"ALTER TABLE users ADD COLUMN {col} TEXT")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # ustun allaqachon bor
    return conn


def track_user(user_id: int, username: str):
    conn = db()
    conn.execute(
        "INSERT OR IGNORE INTO users (user_id, username, first_seen) VALUES (?, ?, ?)",
        (user_id, username or "", datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()


def get_user_lang(user_id: int) -> str:
    """Foydalanuvchining saqlangan tilini qaytaradi, yoki None (hali tanlamagan)."""
    conn = db()
    row = conn.execute("SELECT lang FROM users WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    return row[0] if row and row[0] else None


def set_user_lang(user_id: int, lang: str):
    conn = db()
    conn.execute("UPDATE users SET lang = ? WHERE user_id = ?", (lang, user_id))
    conn.commit()
    conn.close()


def get_user_logo_position(user_id: int) -> str:
    conn = db()
    row = conn.execute("SELECT logo_position FROM users WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    pos = row[0] if row and row[0] else None
    return pos if pos in LOGO_POSITIONS else "bottom_center"


def set_user_logo_position(user_id: int, position: str):
    conn = db()
    conn.execute("UPDATE users SET logo_position = ? WHERE user_id = ?", (position, user_id))
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


LANGUAGES = {"uz": "\U0001F1FA\U0001F1FF O'zbekcha", "ru": "\U0001F1F7\U0001F1FA Русский", "en": "\U0001F1EC\U0001F1E7 English"}

TEXTS = {
    "uz": {
        "choose_lang": "Tilni tanlang / Выберите язык / Choose language:",
        "lang_saved": "\u2705 Til o'zbekcha qilib saqlandi.",
        "start": (
            "\U0001F44B Salom! Men video va rasm yuklab beruvchi botman.\n\n"
            "\U0001F4E5 Quyidagi platformalardan video/rasm havolasini yuboring — "
            "yuklab, sizga jo'nataman:\n\n"
            "\U0001F4F8 Instagram (Reels, postlar, rasmlar)\n"
            "\U0001F3B5 TikTok\n"
            "▶️ YouTube (Shorts va oddiy videolar)\n"
            "\U0001F535 VK\n"
            "\U0001F537 Facebook\n"
            "\u274C Twitter/X\n"
            "\U0001F4CC Pinterest\n"
            "\U0001F47E Twitch (clip'lar)\n"
            "\U0001F536 Reddit\n\n"
            f"(Video hajmi {MAX_TELEGRAM_MB}MB'dan oshmasligi kerak)\n\n"
            "\u2728 Qo'shimcha xizmatlar:\n"
            "\U0001F3A8 Videoga shaxsiy logo (watermark) qo'yish\n"
            "\U0001F4DD Videoga o'zbekcha titr (subtitr) yozish\n\n"
            "Pastdagi tugmalardan foydalaning \U0001F447"
        ),
        "subscribe": (
            "\u26D4 Botdan foydalanish uchun avval quyidagi kanalga a'zo bo'ling, "
            "so'ng \"A'zo bo'ldim, tekshirish\" tugmasini bosing."
        ),
        "btn_channel": "\U0001F4E2 Kanalga o'tish",
        "btn_check": "\u2705 A'zo bo'ldim, tekshirish",
        "subscribed_thanks": "\u2705 Rahmat! Endi videolarni yuborishingiz mumkin.",
        "not_subscribed_yet": "Hali kanalga a'zo bo'lmagansiz.",
        "youtube_unavailable": (
            "\U0001F6E0\uFE0F YouTube hozircha vaqtincha ishlamayapti \u2014 "
            "tez orada tuzatamiz!\n\n"
            "Hozircha Instagram, TikTok, Facebook va boshqa havolalar bilan "
            "urinib ko'rishingiz mumkin \U0001F60A"
        ),
        "downloading": "\u23F3 Video yuklab olinmoqda...",
        "queued": "\u23F3 Hozir juda ko'p odam video yuklamoqda, navbatingizni kutmoqdaman...",
        "not_found": "\u274C Video topilmadi yoki yuklab bo'lmadi. Havola to'g'riligini tekshiring.",
        "too_large": "\u274C Video {size:.1f} MB — bu {max} MB Telegram chegarasidan katta, yubora olmayman.",
        "uploading": "\u2705 Yuklandi, yuborilmoqda...",
        "login_required": (
            "\u274C Bu kontentni yuklab bo'lmadi \u2014 Instagram bunday havolalar uchun "
            "\"tizimga kirgan\" holatni talab qiladi. Agar bu takrorlansa, bot egasiga xabar bering."
        ),
        "generic_error": "\u274C Videoni yuklab bo'lmadi. Havola noto'g'ri, video o'chirilgan yoki maxfiy bo'lishi mumkin.",
        "unexpected_error": "\u274C Xatolik yuz berdi, birozdan keyin qayta urinib ko'ring.",
        "no_personal_logo": (
            "\U0001F4A1 Sizda hali shaxsiy logo sozlanmagan. "
            "/setlogo orqali o'rnatib, keyin qayta urinib ko'ring."
        ),
        "setlogo_prompt": (
            "\U0001F3A8 Yangi (shaxsiy) logo sifatida ishlatiladigan GIF yoki "
            "istalgan turdagi Telegram stikerini (video-stiker yoki animatsion "
            "stiker) hozir menga yuboring.\n\n"
            "(Oddiy yuborsangiz yetarli \u2014 alohida buyruq kerak emas.)\n\n"
            "\u26A0\uFE0F Eski logotipingiz shu yangisi bilan avtomatik "
            "almashtiriladi.\n\n"
            "Shundan keyin menga video HAVOLASI yuborsangiz ham, natija shu logo "
            "bilan qaytadi!"
        ),
        "logo_exists_choice": "\U0001F3A8 Sizda allaqachon shaxsiy logo bor. Nima qilmoqchisiz?",
        "btn_logo_continue": "\u2705 Shu logo bilan davom etish",
        "btn_logo_replace": "\U0001F504 Yangi logo yuklash",
        "btn_logo_off": "\U0001F6AB Logoni o'chirish",
        "setposition_prompt": "\U0001F4CD Logo videoning qaysi qismida chiqsin?",
        "position_changed": "\u2705 Logo joyi o'zgartirildi: {label}",
        "unknown_position": "Noma'lum joy.",
        "tgs_convert_failed": (
            "\u274C Bu stikerni o'girib bo'lmadi. Iltimos, boshqa stiker "
            "sinab ko'ring, yoki https://ezgif.com/tgs-to-gif saytida "
            "GIF'ga o'girib, o'sha GIF'ni yuboring."
        ),
        "logo_saved": (
            "\u2705 <b>Logotip muvaffaqiyatli o'rnatildi!</b>\n\n"
            "Endi bemalol videolarni o'z logoyingiz bilan yuklab olishingiz "
            "mumkin \u2014 menga yuboradigan HAR BIR video (fayl yoki havola) "
            "shu logo bilan avtomatik qaytadi.\n\n"
            "Oddiy (logosiz) rejimga qaytish uchun /start bosing."
        ),
        "logo_save_error": "\u274C Logo saqlashda xatolik yuz berdi, qayta urinib ko'ring.",
        "logo_removed": "\u2705 Shaxsiy logo o'chirildi. Endi videolar logosiz qaytadi.",
        "no_logo_to_remove": "\U0001F4A1 Sizda hozir o'chiriladigan logo yo'q.",
        "send_video_for_logo": (
            "\u2705 <b>Logo rejimi yoqildi!</b>\n\n"
            "Endi bemalol videolarni o'z logoyingiz bilan yuklab olishingiz "
            "mumkin \u2014 menga yuboradigan HAR BIR video (fayl yoki havola) "
            "shaxsiy logotipingiz bilan avtomatik qaytadi.\n\n"
            "Oddiy (logosiz) rejimga qaytish uchun /start bosing."
        ),
    },
    "ru": {
        "choose_lang": "Tilni tanlang / Выберите язык / Choose language:",
        "lang_saved": "\u2705 Язык сохранён: русский.",
        "start": (
            "\U0001F44B Привет! Я бот для скачивания видео и фото.\n\n"
            "\U0001F4E5 Отправьте ссылку на видео/фото с одной из платформ — "
            "скачаю и пришлю вам:\n\n"
            "\U0001F4F8 Instagram (Reels, посты, фото)\n"
            "\U0001F3B5 TikTok\n"
            "▶️ YouTube (Shorts и обычные видео)\n"
            "\U0001F535 VK\n"
            "\U0001F537 Facebook\n"
            "\u274C Twitter/X\n"
            "\U0001F4CC Pinterest\n"
            "\U0001F47E Twitch (клипы)\n"
            "\U0001F536 Reddit\n\n"
            f"(Видео не должно превышать {MAX_TELEGRAM_MB}МБ)\n\n"
            "\u2728 Дополнительные услуги:\n"
            "\U0001F3A8 Добавление личного логотипа (водяного знака) на видео\n"
            "\U0001F4DD Добавление узбекских субтитров на видео\n\n"
            "Используйте кнопки ниже \U0001F447"
        ),
        "subscribe": (
            "\u26D4 Чтобы пользоваться ботом, сначала подпишитесь на канал ниже, "
            "затем нажмите \"Я подписался, проверить\"."
        ),
        "btn_channel": "\U0001F4E2 Перейти в канал",
        "btn_check": "\u2705 Я подписался, проверить",
        "subscribed_thanks": "\u2705 Спасибо! Теперь можете отправлять видео.",
        "not_subscribed_yet": "Вы ещё не подписаны на канал.",
        "youtube_unavailable": (
            "\U0001F6E0\uFE0F YouTube временно не работает \u2014 "
            "скоро исправим!\n\n"
            "А пока можете попробовать ссылки с Instagram, TikTok, Facebook "
            "и других платформ \U0001F60A"
        ),
        "downloading": "\u23F3 Скачиваю видео...",
        "queued": "\u23F3 Сейчас много людей скачивают видео, жду своей очереди...",
        "not_found": "\u274C Видео не найдено или не удалось скачать. Проверьте ссылку.",
        "too_large": "\u274C Видео {size:.1f} МБ — это больше лимита Telegram в {max} МБ, не могу отправить.",
        "uploading": "\u2705 Скачано, отправляю...",
        "login_required": (
            "\u274C Не удалось скачать этот контент \u2014 Instagram требует "
            "\"авторизации\" для таких ссылок. Если это повторяется, сообщите владельцу бота."
        ),
        "generic_error": "\u274C Не удалось скачать видео. Ссылка неверна, видео удалено или приватно.",
        "unexpected_error": "\u274C Произошла ошибка, попробуйте ещё раз чуть позже.",
        "no_personal_logo": (
            "\U0001F4A1 У вас ещё не настроен личный логотип. "
            "Настройте через /setlogo и попробуйте снова."
        ),
        "setlogo_prompt": (
            "\U0001F3A8 Отправьте мне GIF или любой стикер Telegram "
            "(видео-стикер или анимированный стикер), который будет "
            "использоваться как ваш личный логотип.\n\n"
            "(Просто отправьте — отдельная команда не нужна.)\n\n"
            "\u26A0\uFE0F Ваш старый логотип будет автоматически заменён новым.\n\n"
            "После этого, если вы отправите мне ССЫЛКУ на видео, результат "
            "тоже вернётся с этим логотипом!"
        ),
        "logo_exists_choice": "\U0001F3A8 У вас уже есть личный логотип. Что хотите сделать?",
        "btn_logo_continue": "\u2705 Продолжить с этим логотипом",
        "btn_logo_replace": "\U0001F504 Загрузить новый логотип",
        "btn_logo_off": "\U0001F6AB Удалить логотип",
        "setposition_prompt": "\U0001F4CD В какой части видео должен появляться логотип?",
        "position_changed": "\u2705 Позиция логотипа изменена: {label}",
        "unknown_position": "Неизвестная позиция.",
        "tgs_convert_failed": (
            "\u274C Не удалось конвертировать этот стикер. Попробуйте другой "
            "стикер, или конвертируйте его в GIF на https://ezgif.com/tgs-to-gif "
            "и отправьте этот GIF."
        ),
        "logo_saved": (
            "\u2705 <b>Логотип успешно установлен!</b>\n\n"
            "Теперь можете свободно скачивать видео со своим логотипом \u2014 "
            "КАЖДОЕ видео (файл или ссылка), которое вы мне отправите, будет "
            "автоматически возвращаться с этим логотипом.\n\n"
            "Чтобы вернуться в обычный (без логотипа) режим, нажмите /start."
        ),
        "logo_save_error": "\u274C Ошибка при сохранении логотипа, попробуйте ещё раз.",
        "logo_removed": "\u2705 Личный логотип удалён. Теперь видео будут без логотипа.",
        "no_logo_to_remove": "\U0001F4A1 У вас сейчас нет логотипа для удаления.",
        "send_video_for_logo": (
            "\u2705 <b>Режим логотипа включён!</b>\n\n"
            "Теперь можете свободно скачивать видео со своим логотипом \u2014 "
            "КАЖДОЕ видео (файл или ссылка), которое вы мне отправите, будет "
            "автоматически возвращаться с вашим личным логотипом.\n\n"
            "Чтобы вернуться в обычный режим (без логотипа), нажмите /start."
        ),
    },
    "en": {
        "choose_lang": "Tilni tanlang / Выберите язык / Choose language:",
        "lang_saved": "\u2705 Language set to English.",
        "start": (
            "\U0001F44B Hi! I'm a bot for downloading videos and photos.\n\n"
            "\U0001F4E5 Send me a video/photo link from one of these platforms "
            "and I'll download it for you:\n\n"
            "\U0001F4F8 Instagram (Reels, posts, photos)\n"
            "\U0001F3B5 TikTok\n"
            "▶️ YouTube (Shorts and regular videos)\n"
            "\U0001F535 VK\n"
            "\U0001F537 Facebook\n"
            "\u274C Twitter/X\n"
            "\U0001F4CC Pinterest\n"
            "\U0001F47E Twitch (clips)\n"
            "\U0001F536 Reddit\n\n"
            f"(Videos must be under {MAX_TELEGRAM_MB}MB)\n\n"
            "\u2728 Additional services:\n"
            "\U0001F3A8 Add your personal logo (watermark) to videos\n"
            "\U0001F4DD Add Uzbek subtitles to videos\n\n"
            "Use the buttons below \U0001F447"
        ),
        "subscribe": (
            "\u26D4 To use this bot, please first subscribe to the channel below, "
            "then tap \"I've subscribed, check\"."
        ),
        "btn_channel": "\U0001F4E2 Go to channel",
        "btn_check": "\u2705 I've subscribed, check",
        "subscribed_thanks": "\u2705 Thanks! You can now send videos.",
        "not_subscribed_yet": "You haven't subscribed to the channel yet.",
        "youtube_unavailable": (
            "\U0001F6E0\uFE0F YouTube is temporarily unavailable \u2014 "
            "we'll fix it soon!\n\n"
            "For now, try links from Instagram, TikTok, Facebook and other "
            "platforms \U0001F60A"
        ),
        "downloading": "\u23F3 Downloading video...",
        "queued": "\u23F3 Lots of people are downloading right now, waiting for your turn...",
        "not_found": "\u274C Video not found or couldn't be downloaded. Please check the link.",
        "too_large": "\u274C The video is {size:.1f} MB — that's over Telegram's {max} MB limit, I can't send it.",
        "uploading": "\u2705 Downloaded, sending...",
        "login_required": (
            "\u274C Couldn't download this content \u2014 Instagram requires a "
            "\"logged in\" session for such links. If this keeps happening, contact the bot owner."
        ),
        "generic_error": "\u274C Couldn't download the video. The link may be wrong, or the video deleted/private.",
        "unexpected_error": "\u274C Something went wrong, please try again in a moment.",
        "no_personal_logo": (
            "\U0001F4A1 You haven't set up a personal logo yet. "
            "Set one up with /setlogo and try again."
        ),
        "setlogo_prompt": (
            "\U0001F3A8 Send me a GIF or any Telegram sticker (video sticker "
            "or animated sticker) to use as your personal logo.\n\n"
            "(Just send it \u2014 no separate command needed.)\n\n"
            "\u26A0\uFE0F Your old logo will be automatically replaced by the new one.\n\n"
            "After that, if you send me a video LINK too, the result will "
            "come back with this logo!"
        ),
        "logo_exists_choice": "\U0001F3A8 You already have a personal logo. What would you like to do?",
        "btn_logo_continue": "\u2705 Continue with this logo",
        "btn_logo_replace": "\U0001F504 Upload a new logo",
        "btn_logo_off": "\U0001F6AB Remove logo",
        "setposition_prompt": "\U0001F4CD Where on the video should the logo appear?",
        "position_changed": "\u2705 Logo position changed: {label}",
        "unknown_position": "Unknown position.",
        "tgs_convert_failed": (
            "\u274C Couldn't convert this sticker. Try a different sticker, "
            "or convert it to GIF at https://ezgif.com/tgs-to-gif and send "
            "that GIF instead."
        ),
        "logo_saved": (
            "\u2705 <b>Logo successfully set!</b>\n\n"
            "You can now freely download videos with your own logo \u2014 "
            "EVERY video (file or link) you send me will automatically come "
            "back with this logo.\n\n"
            "To return to the normal (no logo) mode, press /start."
        ),
        "logo_save_error": "\u274C Error saving the logo, please try again.",
        "logo_removed": "\u2705 Personal logo removed. Videos will now come back without a logo.",
        "no_logo_to_remove": "\U0001F4A1 You don't have a logo set up to remove right now.",
        "send_video_for_logo": (
            "\u2705 <b>Logo mode is on!</b>\n\n"
            "You can now freely download videos with your own logo \u2014 "
            "EVERY video (file or link) you send me will automatically come "
            "back with your personal logo.\n\n"
            "To return to plain mode (no logo), press /start."
        ),
    },
}


def t(key: str, lang: str) -> str:
    lang = lang if lang in TEXTS else "uz"
    return TEXTS[lang].get(key, TEXTS["uz"].get(key, ""))


def language_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=f"setlang:{code}")]
        for code, label in LANGUAGES.items()
    ])


def subscribe_keyboard(lang: str = "uz") -> InlineKeyboardMarkup:
    channel_url = f"https://t.me/{REQUIRED_CHANNEL.lstrip('@')}"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t("btn_channel", lang), url=channel_url)],
        [InlineKeyboardButton(text=t("btn_check", lang), callback_data="check_sub")],
    ])


MAIN_MENU_LABELS = {
    "uz": {"logo": "\U0001F3A8 Videoga Logo qo'yish", "caption": "\U0001F4DD Videoga Text qo'yish", "lang": "\U0001F310 Til"},
    "ru": {"logo": "\U0001F3A8 Добавить логотип на видео", "caption": "\U0001F4DD Добавить текст на видео", "lang": "\U0001F310 Язык"},
    "en": {"logo": "\U0001F3A8 Add logo to video", "caption": "\U0001F4DD Add text to video", "lang": "\U0001F310 Language"},
}


def main_menu_keyboard(lang: str) -> InlineKeyboardMarkup:
    labels = MAIN_MENU_LABELS.get(lang, MAIN_MENU_LABELS["uz"])
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=labels["logo"], callback_data="start_setlogo")],
        [InlineKeyboardButton(text=labels["caption"], callback_data="mode_caption")],
        [InlineKeyboardButton(text=labels["lang"], callback_data="start_til")],
    ])


def logo_choice_keyboard(lang: str) -> InlineKeyboardMarkup:
    """Foydalanuvchida allaqachon shaxsiy logo bo'lganda ko'rsatiladi —
    mavjud logo bilan davom etish, uni yangisiga almashtirish, yoki
    butunlay o'chirish o'rtasida tanlov beradi."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t("btn_logo_continue", lang), callback_data="logo_continue")],
        [InlineKeyboardButton(text=t("btn_logo_replace", lang), callback_data="logo_replace")],
        [InlineKeyboardButton(text=t("btn_logo_off", lang), callback_data="logo_off")],
    ])


@router.message(F.text == "/start")
async def cmd_start(message: Message, bot: Bot, state: FSMContext):
    # Har safar /start bosilganda, foydalanuvchi /caption yoki /setlogo kabi
    # jarayonda qolib ketgan bo'lsa ham, oddiy (logosiz, titrsiz) yuklab olish
    # rejimiga qaytariladi.
    await state.clear()

    track_user(message.from_user.id, message.from_user.username)
    lang = get_user_lang(message.from_user.id)
    if not lang:
        await message.answer(TEXTS["uz"]["choose_lang"], reply_markup=language_keyboard())
        return
    if not await is_subscribed(bot, message.from_user.id):
        await message.answer(t("subscribe", lang), reply_markup=subscribe_keyboard(lang))
        return
    await message.answer(t("start", lang), reply_markup=main_menu_keyboard(lang))


@router.message(F.text == "/til")
async def cmd_til(message: Message):
    await message.answer(TEXTS["uz"]["choose_lang"], reply_markup=language_keyboard())


@router.callback_query(F.data.startswith("setlang:"))
async def cb_set_lang(callback: CallbackQuery, bot: Bot):
    lang = callback.data.split(":", 1)[1]
    if lang not in TEXTS:
        await callback.answer()
        return
    set_user_lang(callback.from_user.id, lang)
    await safe_edit(callback.message, t("lang_saved", lang))
    await callback.answer()
    if not await is_subscribed(bot, callback.from_user.id):
        await callback.message.answer(t("subscribe", lang), reply_markup=subscribe_keyboard(lang))
    else:
        await callback.message.answer(t("start", lang))


@router.message(F.text == "/stats")
async def cmd_stats(message: Message):
    if not OWNER_CHAT_IDS or message.from_user.id not in OWNER_CHAT_IDS:
        return  # sozlanmagan yoki ruxsatsiz — jim turadi
    await message.answer(f"\U0001F465 Botdan foydalangan jami odamlar: {count_users()} kishi")


def parse_broadcast_sections(text: str) -> dict:
    """/xabar matnini tilga bo'lib ajratadi, agar admin UZ:/RU:/EN:
    belgilarini alohida qatorlarda ishlatgan bo'lsa. Aks holda, butun
    matnni HAMMA til uchun bir xil (orqaga moslik) qaytaradi."""
    pattern = re.compile(r"^(UZ|RU|EN):\s*$", re.IGNORECASE | re.MULTILINE)
    matches = list(pattern.finditer(text))
    if not matches:
        whole = text.strip()
        return {"uz": whole, "ru": whole, "en": whole}

    sections = {}
    for i, m in enumerate(matches):
        lang = m.group(1).lower()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        sections[lang] = text[start:end].strip()

    # Belgilanmagan tillar uchun UZ bo'limi (yoki topilgan birinchisi)
    # zaxira sifatida ishlatiladi.
    fallback = sections.get("uz") or next(iter(sections.values()), "")
    for lang in ("uz", "ru", "en"):
        sections.setdefault(lang, fallback)
    return sections


@router.message(F.text.startswith("/xabar"))
async def cmd_broadcast(message: Message, bot: Bot):
    if not OWNER_CHAT_IDS or message.from_user.id not in OWNER_CHAT_IDS:
        return

    parts = message.text.split("\n", 1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer(
            "Foydalanish: birinchi qatorga /xabar deb yozing, Shift+Enter bosib "
            "yangi qatorga o'ting, so'ng matningizni yozib, hammasini BITTA xabar "
            "sifatida yuboring.\n\n"
            "• Bitta til uchun: shunchaki matnni yozing — hammaga bir xil boradi.\n\n"
            "• Har bir tilga ALOHIDA matn (foydalanuvchining saqlangan tiliga "
            "qarab avtomatik tanlanadi) uchun quyidagi formatda yozing:\n\n"
            "UZ:\n<o'zbekcha matn>\n\n"
            "RU:\n<ruscha matn>\n\n"
            "EN:\n<inglizcha matn>"
        )
        return

    sections = parse_broadcast_sections(parts[1])

    conn = db()
    rows = conn.execute("SELECT user_id, lang FROM users").fetchall()
    conn.close()

    if not rows:
        await message.answer("Hali hech kim ro'yxatda yo'q.")
        return

    progress = await message.answer(f"\u23F3 Yuborilmoqda... (0/{len(rows)})")
    sent, failed = 0, 0

    for i, (uid, user_lang) in enumerate(rows, start=1):
        lang = user_lang if user_lang in sections else "uz"
        text_to_send = sections.get(lang) or sections.get("uz", "")
        try:
            await bot.send_message(uid, text_to_send)
            sent += 1
        except Exception as e:
            failed += 1
            log.warning(f"Xabar yuborilmadi ({uid}): {e}")
        if i % 25 == 0:
            try:
                await safe_edit(progress, f"\u23F3 Yuborilmoqda... ({i}/{len(rows)})")
            except Exception:
                pass
        await asyncio.sleep(0.05)  # Telegram limitiga urilib qolmaslik uchun

    await safe_edit(
        progress,
        f"\u2705 Tugadi!\nYuborildi: {sent} ta\nYetib bormadi (bloklagan/o'chirgan): {failed} ta"
    )


@router.callback_query(F.data == "check_sub")
async def cb_check_sub(callback: CallbackQuery, bot: Bot):
    lang = get_user_lang(callback.from_user.id) or "uz"
    if await is_subscribed(bot, callback.from_user.id):
        await safe_edit(callback.message, t("subscribed_thanks", lang))
        await callback.answer()
    else:
        await callback.answer(t("not_subscribed_yet", lang), show_alert=True)


@router.callback_query(F.data == "start_setlogo")
async def cb_start_setlogo(callback: CallbackQuery, state: FSMContext):
    """Asosiy menyudagi '🎨 Videoga Logo qo'yish' tugmasi.

    Agar foydalanuvchida allaqachon shaxsiy logo bo'lsa — mavjud logo
    bilan davom etish yoki yangisiga almashtirish o'rtasida tanlov
    beradi. Agar logo hali sozlanmagan bo'lsa — to'g'ridan-to'g'ri uni
    sozlashni so'raydi."""
    lang = get_user_lang(callback.from_user.id) or "uz"
    await callback.answer()
    if _find_user_logo(callback.from_user.id):
        await callback.message.answer(t("logo_exists_choice", lang), reply_markup=logo_choice_keyboard(lang))
    else:
        _awaiting_logo_from.add(callback.from_user.id)
        await callback.message.answer(t("setlogo_prompt", lang))


def logo_active_keyboard(lang: str) -> InlineKeyboardMarkup:
    """Logo rejimi FAOL bo'lgan paytda ko'rsatiladi — tez o'chirish
    imkoniyati uchun."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t("btn_logo_off", lang), callback_data="logo_off")],
    ])


@router.callback_query(F.data == "logo_continue")
async def cb_logo_continue(callback: CallbackQuery, state: FSMContext):
    """Mavjud logo bilan davom etish tanlandi — logo rejimini yoqadi."""
    lang = get_user_lang(callback.from_user.id) or "uz"
    await callback.answer()
    await state.set_state(WatermarkStates.waiting_video)
    await callback.message.answer(
        t("send_video_for_logo", lang), parse_mode="HTML", reply_markup=logo_active_keyboard(lang)
    )


@router.callback_query(F.data == "logo_replace")
async def cb_logo_replace(callback: CallbackQuery):
    """Yangi logo yuklash tanlandi — eskisi keyingi fayl yuborilganda
    avtomatik almashtiriladi (handle_logo_upload'da)."""
    lang = get_user_lang(callback.from_user.id) or "uz"
    await callback.answer()
    _awaiting_logo_from.add(callback.from_user.id)
    await callback.message.answer(t("setlogo_prompt", lang))


@router.callback_query(F.data == "start_til")
async def cb_start_til(callback: CallbackQuery):
    """Asosiy menyudagi '🌐 Til' tugmasi — /til bilan bir xil."""
    await callback.answer()
    await callback.message.answer(TEXTS["uz"]["choose_lang"], reply_markup=language_keyboard())


def _download_images_gallery_dl_sync(url: str, output_dir: str) -> list:
    """gallery-dl orqali Instagram rasm(lar)ini (yt-dlp yuklay olmaydigan
    surat post/karusel) yuklab oladi. yt-dlp — video, gallery-dl — rasm
    uchun mos vositalar. Yuklangan rasm fayllari yo'llarini (tartiblangan)
    ro'yxat qilib qaytaradi. Bloklaydigan (sinxron) funksiya — alohida
    threadda ishga tushiriladi."""
    import subprocess

    cmd = ["gallery-dl", "-D", output_dir, "--no-mtime"]
    if IG_COOKIES_FILE:
        cmd += ["--cookies", IG_COOKIES_FILE]
    cmd.append(url)

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise RuntimeError(f"gallery-dl xato: {result.stderr[-500:]}")

    image_exts = (".jpg", ".jpeg", ".png", ".webp", ".heic")
    files = []
    for root, _dirs, filenames in os.walk(output_dir):
        for fn in filenames:
            if fn.lower().endswith(image_exts):
                files.append(os.path.join(root, fn))
    files.sort()
    return files


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


async def _download_with_retry(url: str, ydl_opts: dict, attempts: int = 3) -> dict:
    """Vaqtinchalik tarmoq/bloklanish xatolarida bir necha marta qayta urinadi
    (kutish bilan), doimiy xatolarda (masalan noto'g'ri havola) darhol
    yt_dlp.utils.DownloadError'ni yuqoriga uzatadi.

    Instagram ba'zan bir zumga bo'sh javob (JSON parse xatosi) qaytaradi —
    bu odatda vaqtinchalik bo'ladi, shuning uchun urinishlar sonini oshirib
    va orasidagi kutish vaqtini uzaytirib qo'ydik (3 ta urinish, 4-8-12 soniya)."""
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            return await asyncio.to_thread(_download_video_sync, url, ydl_opts)
        except yt_dlp.utils.DownloadError as e:
            last_error = e
            if attempt < attempts:
                log.warning(f"Yuklashda xato (urinish {attempt}/{attempts}), qayta urinilmoqda: {e}")
                await asyncio.sleep(4 * attempt)
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


IMAGE_CAPTION = (
    f"Ushbu rasm eng sifatli tarzda {BOT_USERNAME_TAG} orqali yuklab olindi \U0001F4F8"
)


def _add_watermark_sync(input_path: str, output_path: str, logo_file: str, position: str) -> None:
    """FFmpeg orqali videoga berilgan logo faylni, berilgan pozitsiyada
    qo'yadi. GIF/WEBM butun video davomiyligiga yetguncha aylantiriladi
    (loop). Bloklaydigan (sinxron) funksiya — alohida threadda ishga
    tushiriladi."""
    import subprocess

    xy = LOGO_POSITIONS.get(position, LOGO_POSITIONS["bottom_center"])
    cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-stream_loop", "-1", "-i", logo_file,
        "-filter_complex",
        # Logo videoning ENI'ga NISBATAN (85%) o'lchamlanadi.
        "[1:v][0:v]scale2ref=w=main_w*0.85:h=ow/mdar[logo][video];"
        # unpremultiply — rlottie ba'zan "premultiplied alpha" bilan chiqaradi
        # (rang qiymatlari shaffoflik bilan oldindan aralashtirilgan), bu esa
        # so'nish paytida QORA rangga "cho'kib" ketishga sabab bo'ladi; shu
        # filtr buni to'g'irlaydi (to'g'ri manbalarga zarar bermaydi).
        "[logo]format=rgba,unpremultiply=inplace=1[logo2];"
        f"[video][logo2]overlay={xy}:shortest=1",
        # Logo qo'yish videoni QAYTA kodlashni talab qiladi (overlay tufayli),
        # shuning uchun hajm o'sib ketmasligi uchun aniq siqish beramiz.
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
        "-c:a", "copy",
        output_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg xato: {result.stderr[-500:]}")


# Kim /setlogo buyrug'ini yuborib, hozir yangi logo GIF yuborishini kutayotganini
# saqlaydi (bitta oddiy to'plam — alohida FSM kutubxonasi shart emas).
_awaiting_logo_from: set = set()

# Kim /setcookies_ig yoki /setcookies_yt yuborib, hozir yangi cookies.txt
# yuborishini kutayotganini saqlaydi. Qiymati "ig" yoki "yt".
_awaiting_cookies_from: dict = {}


@router.message(F.text == "/setcookies_ig")
async def cmd_setcookies_ig(message: Message):
    """Bot egasi yangi Instagram cookies.txt faylini o'rnatishni boshlaydi."""
    if not OWNER_CHAT_IDS or message.from_user.id not in OWNER_CHAT_IDS:
        return
    _awaiting_cookies_from[message.from_user.id] = "ig"
    await message.answer(
        "\U0001F36A Yangi Instagram cookies.txt faylini hozir menga yuboring "
        "(hujjat/fayl sifatida)."
    )


@router.message(F.text == "/setcookies_yt")
async def cmd_setcookies_yt(message: Message):
    """Bot egasi yangi YouTube cookies.txt faylini o'rnatishni boshlaydi."""
    if not OWNER_CHAT_IDS or message.from_user.id not in OWNER_CHAT_IDS:
        return
    _awaiting_cookies_from[message.from_user.id] = "yt"
    await message.answer(
        "\U0001F36A Yangi YouTube cookies.txt faylini hozir menga yuboring "
        "(hujjat/fayl sifatida)."
    )


async def _handle_cookies_upload(message: Message, bot: Bot, kind: str):
    """/setcookies_ig yoki /setcookies_yt buyrug'idan keyin yuborilgan
    cookies.txt faylini doimiy joyga saqlaydi va darhol ishlatishni boshlaydi."""
    filename = "ig_cookies.txt" if kind == "ig" else "yt_cookies.txt"
    target_path = os.path.join(PERSIST_DIR, filename)
    try:
        file_info = await bot.get_file(message.document.file_id)
        os.makedirs(PERSIST_DIR, exist_ok=True)
        await bot.download_file(file_info.file_path, destination=target_path)
        global IG_COOKIES_FILE, YT_COOKIES_FILE
        if kind == "ig":
            IG_COOKIES_FILE = target_path
        else:
            YT_COOKIES_FILE = target_path
        label = "Instagram" if kind == "ig" else "YouTube"
        await message.answer(f"\u2705 {label} cookies yangilandi! Darhol ishlatiladi.")
    except Exception as e:
        log.error(f"Cookies saqlashda xato: {e}")
        await message.answer("\u274C Cookies saqlashda xatolik yuz berdi, qayta urinib ko'ring.")


async def _remove_user_logo(user_id: int) -> bool:
    """Foydalanuvchining shaxsiy logotipini o'chiradi. Biror narsa
    o'chirilgan bo'lsa True qaytaradi."""
    user_dir = os.path.join(USER_LOGOS_DIR, str(user_id))
    removed = False
    for ext in (".webm", ".apng", ".gif"):
        p = os.path.join(user_dir, f"logo{ext}")
        if os.path.exists(p):
            os.remove(p)
            removed = True
    return removed


async def _do_logooff(user_id: int, lang: str) -> str:
    """Logoni o'chiradi va foydalanuvchiga ko'rsatiladigan matnni qaytaradi
    (/logooff buyrug'i va '🚫 Logoni o'chirish' tugmasi ikkalasi ham shuni
    ishlatadi)."""
    removed = await _remove_user_logo(user_id)
    return t("logo_removed" if removed else "no_logo_to_remove", lang)


@router.message(F.text == "/logooff")
async def cmd_logooff(message: Message, state: FSMContext):
    """Foydalanuvchining shaxsiy logotipini bitta oddiy buyruq bilan
    o'chiradi."""
    lang = get_user_lang(message.from_user.id) or "uz"
    text = await _do_logooff(message.from_user.id, lang)
    await state.clear()
    await message.answer(text)


@router.callback_query(F.data == "logo_off")
async def cb_logo_off(callback: CallbackQuery, state: FSMContext):
    """Logo bo'limidagi '🚫 Logoni o'chirish' tugmasi — /logooff bilan bir xil."""
    lang = get_user_lang(callback.from_user.id) or "uz"
    await callback.answer()
    text = await _do_logooff(callback.from_user.id, lang)
    await state.clear()
    await callback.message.answer(text)


@router.message(F.text == "/setlogo")
async def cmd_setlogo(message: Message):
    """Agar shaxsiy logo allaqachon bor bo'lsa — davom etish yoki
    almashtirish o'rtasida tanlov beradi; bo'lmasa, yangi logo sozlashni
    so'raydi."""
    lang = get_user_lang(message.from_user.id) or "uz"
    if _find_user_logo(message.from_user.id):
        await message.answer(t("logo_exists_choice", lang), reply_markup=logo_choice_keyboard(lang))
    else:
        _awaiting_logo_from.add(message.from_user.id)
        await message.answer(t("setlogo_prompt", lang))


@router.message(F.text == "/setposition")
async def cmd_setposition(message: Message):
    """Foydalanuvchi o'z logotipi videoning qaysi qismida chiqishini
    tugmalar orqali tanlaydi (shaxsiy sozlama)."""
    lang = get_user_lang(message.from_user.id) or "uz"
    labels = POSITION_LABELS.get(lang, POSITION_LABELS["uz"])
    current = get_user_logo_position(message.from_user.id)
    buttons = []
    row = []
    for key in LOGO_POSITIONS:
        mark = "\u2705 " if key == current else ""
        row.append(InlineKeyboardButton(text=f"{mark}{labels[key]}", callback_data=f"logopos:{key}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    await message.answer(
        t("setposition_prompt", lang),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@router.callback_query(F.data.startswith("logopos:"))
async def cb_set_position(callback: CallbackQuery):
    lang = get_user_lang(callback.from_user.id) or "uz"
    key = callback.data.split(":", 1)[1]
    if key not in LOGO_POSITIONS:
        await callback.answer(t("unknown_position", lang))
        return
    set_user_logo_position(callback.from_user.id, key)
    label = POSITION_LABELS.get(lang, POSITION_LABELS["uz"])[key]
    await safe_edit(callback.message, t("position_changed", lang).format(label=label))
    await callback.answer()


def _convert_tgs_to_apng_sync(tgs_path: str, apng_path: str) -> bool:
    """Eski turdagi (.tgs, Lottie/vektor) Telegram stikerini rlottie
    kutubxonasi orqali APNG'ga o'giradi (GIF EMAS — GIF faqat ikkilik
    shaffoflikni qo'llab-quvvatlaydi, shuning uchun so'nib-ketuvchi
    animatsiyalarda orqa fon "qattiq" bo'lib ko'rinib qoladi; APNG esa
    to'liq (256 daraja) alfa-kanalni saqlaydi). Bloklaydigan (sinxron)
    funksiya — alohida threadda ishga tushiriladi."""
    try:
        from rlottie_python import LottieAnimation
        with LottieAnimation.from_tgs(tgs_path) as anim:
            anim.save_animation(apng_path)
        return os.path.exists(apng_path) and os.path.getsize(apng_path) > 0
    except Exception as e:
        log.warning(f"TGS->APNG konvertatsiyasida xato: {e}")
        return False


@router.message(F.animation | F.document | F.sticker)
async def handle_logo_upload(message: Message, bot: Bot, state: FSMContext):
    """/setlogo, /setcookies_ig yoki /setcookies_yt buyrug'idan keyin
    yuborilgan faylni tegishli joyga saqlaydi. GIF/video-stiker/hujjat
    (logo uchun — HAR KIM) va cookies.txt (cookies uchun — FAQAT bot
    egasi) qabul qilinadi. Eski turdagi (.tgs, vektor) stikerlar FFmpeg
    tomonidan o'qib bo'lmagani uchun rad etiladi."""
    # Cookies — faqat bot egasi uchun (bu maxfiy, hisobga oid ma'lumot).
    cookies_kind = _awaiting_cookies_from.get(message.from_user.id)
    if cookies_kind and message.document:
        if not OWNER_CHAT_IDS or message.from_user.id not in OWNER_CHAT_IDS:
            return
        del _awaiting_cookies_from[message.from_user.id]
        await _handle_cookies_upload(message, bot, cookies_kind)
        return

    # Logo — endi HAR KIM o'zi uchun sozlashi mumkin.
    if message.from_user.id not in _awaiting_logo_from:
        return
    lang = get_user_lang(message.from_user.id) or "uz"

    # Stiker bo'lsa: "video-stiker" (.webm) to'g'ridan-to'g'ri, eski turdagi
    # (".tgs", vektor/Lottie) esa rlottie orqali APNG'ga o'girilib olinadi
    # (GIF emas — APNG to'liq alfa-kanalni saqlaydi, so'nish animatsiyalari
    # to'g'ri, shaffof ko'rinishi uchun).
    needs_tgs_convert = False
    if message.sticker:
        if message.sticker.is_video:
            file_id = message.sticker.file_id
            ext = ".webm"
        else:
            file_id = message.sticker.file_id
            ext = ".apng"
            needs_tgs_convert = True
    elif message.animation:
        file_id = message.animation.file_id
        ext = ".gif"
    else:
        file_id = message.document.file_id
        ext = ".webm" if (message.document.mime_type or "").endswith("webm") else ".gif"

    _awaiting_logo_from.discard(message.from_user.id)
    user_dir = os.path.join(USER_LOGOS_DIR, str(message.from_user.id))
    new_path = os.path.join(user_dir, f"logo{ext}")
    try:
        file_info = await bot.get_file(file_id)
        os.makedirs(user_dir, exist_ok=True)

        if needs_tgs_convert:
            tgs_temp = os.path.join(tempfile.gettempdir(), f"{uuid.uuid4()}.tgs")
            await bot.download_file(file_info.file_path, destination=tgs_temp)
            ok = await asyncio.to_thread(_convert_tgs_to_apng_sync, tgs_temp, new_path)
            os.remove(tgs_temp)
            if not ok:
                await message.answer(t("tgs_convert_failed", lang))
                return
        else:
            await bot.download_file(file_info.file_path, destination=new_path)
        # Eski (boshqa kengaytmali) logo faylini tozalab qo'yamiz, chalkashmasin.
        for old_ext in (".gif", ".webm", ".apng"):
            old_path = os.path.join(user_dir, f"logo{old_ext}")
            if old_path != new_path and os.path.exists(old_path):
                os.remove(old_path)
        await message.answer(t("logo_saved", lang), parse_mode="HTML", reply_markup=logo_active_keyboard(lang))
        # Logo yangi sozlandi — darhol KEYINGI video shu logo bilan qaytishi
        # uchun, qayta tugma bosmasdan, video kutish holatiga o'tkazamiz.
        await state.set_state(WatermarkStates.waiting_video)
    except Exception as e:
        log.error(f"Logo saqlashda xato: {e}")
        await message.answer(t("logo_save_error", lang))


@router.message(WatermarkStates.waiting_video, F.video)
async def handle_owner_video(message: Message, bot: Bot):
    """'Videoga Logo qo'yish' tugmasi (yoki /setlogo) bosilgandan keyin,
    logo rejimi YOQILGAN holda qoladi — foydalanuvchi yuborgan HAR BIR
    video shaxsiy logo (watermark) bilan qaytariladi, /start bosilmaguncha
    (holat faqat /start orqali tozalanadi, bu yerda emas). Majburiy
    obunani ham talab qiladi."""
    log.info(f"WMARK: handler boshlandi, user={message.from_user.id}")
    lang = get_user_lang(message.from_user.id) or "uz"
    logo_file = _find_user_logo(message.from_user.id)
    if not logo_file:
        log.info("WMARK: shaxsiy logo yo'q, to'xtatildi (video o'zgarishsiz qoladi)")
        await message.answer(t("no_personal_logo", lang))
        return
    position = get_user_logo_position(message.from_user.id)
    track_user(message.from_user.id, message.from_user.username)

    if not await is_subscribed(bot, message.from_user.id):
        log.info("WMARK: obuna emas, to'xtatildi")
        await message.answer(t("subscribe", lang), reply_markup=subscribe_keyboard(lang))
        return

    status = await message.answer("\U0001F3A8 Logo qo'yilmoqda...")
    log.info("WMARK: status xabari yuborildi, yuklab olish boshlanmoqda")
    input_path = None
    output_path = None
    try:
        file_info = await bot.get_file(message.video.file_id)
        log.info(f"WMARK: file_info olindi, hajmi={message.video.file_size}")
        input_path = os.path.join(tempfile.gettempdir(), f"{uuid.uuid4()}.mp4")
        output_path = os.path.join(tempfile.gettempdir(), f"{uuid.uuid4()}_logo.mp4")
        await bot.download_file(file_info.file_path, destination=input_path)
        log.info("WMARK: video yuklab olindi, ffmpeg boshlanmoqda")

        await asyncio.wait_for(
            asyncio.to_thread(_add_watermark_sync, input_path, output_path, logo_file, position),
            timeout=180,
        )
        log.info("WMARK: ffmpeg tugadi, video yuborilmoqda")

        await bot.send_video(chat_id=message.chat.id, video=FSInputFile(output_path))
        log.info("WMARK: video muvaffaqiyatli yuborildi")
        await status.delete()
    except asyncio.TimeoutError:
        log.error("WMARK: TIMEOUT xatosi")
        await safe_edit(status, "\u274C Vaqt tugadi (video juda uzun bo'lishi mumkin).")
    except Exception as e:
        log.error(f"WMARK: XATO -> {type(e).__name__}: {e}")
        await safe_edit(status, "\u274C Logo qo'yishda xatolik yuz berdi.")
    finally:
        for p in (input_path, output_path):
            if p and os.path.exists(p):
                os.remove(p)


def _ensure_telegram_compatible_sync(path: str) -> str:
    """Video kodeki Telegram bilan mos (H.264) emasligini tekshiradi, va
    FAQAT shunday bo'lsa qayta kodlaydi — mos bo'lsa, TEZLIK uchun
    faylga tegilmaydi (logo qo'yilmagan hollarda tezroq yuboriladi).
    Muammo bo'lsa, ASL faylni qaytaradi (xavfsiz)."""
    import subprocess

    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name", "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=15,
        )
        vcodec = probe.stdout.strip().lower()
    except Exception as e:
        log.warning(f"ffprobe xato (asl fayl qaytariladi): {e}")
        return path

    if vcodec in ("h264",):
        return path  # allaqachon mos — tezlik uchun qayta kodlash shart emas

    fixed_path = path.rsplit(".", 1)[0] + "_fixed.mp4"
    try:
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", path,
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
             "-c:a", "aac", "-movflags", "+faststart", fixed_path],
            capture_output=True, text=True, timeout=180,
        )
        if result.returncode == 0 and os.path.exists(fixed_path):
            os.remove(path)
            return fixed_path
        log.warning(f"Qayta kodlashda xato (asl fayl qaytariladi): {result.stderr[-300:]}")
        return path
    except Exception as e:
        log.warning(f"Qayta kodlashda kutilmagan xato (asl fayl qaytariladi): {e}")
        return path


@router.message(F.text.startswith("http"))
async def handle_link(message: Message, bot: Bot, state: FSMContext):
    track_user(message.from_user.id, message.from_user.username)
    lang = get_user_lang(message.from_user.id) or "uz"

    if not await is_subscribed(bot, message.from_user.id):
        await message.answer(t("subscribe", lang), reply_markup=subscribe_keyboard(lang))
        return

    url = message.text.strip()

    status = await message.answer(t("downloading", lang))

    tmp_dir = tempfile.mkdtemp()
    out_template = os.path.join(tmp_dir, f"{uuid.uuid4().hex}.%(ext)s")

    ydl_opts = {
        "outtmpl": out_template,
        "format": (
            # Telegram VP9/AV1 kabi kodeklarni ba'zan to'g'ri ko'rsatmaydi —
            # shuning uchun avval aynan H.264 (avc1) + AAC (m4a) ni afzal
            # ko'ramiz, faqat topilmasa umumiyroq variantlarga o'tamiz.
            f"bestvideo[vcodec^=avc1][height<={MAX_VIDEO_HEIGHT}]+bestaudio[acodec^=mp4a]/"
            f"bestvideo[vcodec^=avc1][height<={MAX_VIDEO_HEIGHT}]+bestaudio/"
            f"best[vcodec^=avc1][height<={MAX_VIDEO_HEIGHT}]/"
            f"bestvideo[height<={MAX_VIDEO_HEIGHT}]+bestaudio[ext=m4a]/"
            f"bestvideo[height<={MAX_VIDEO_HEIGHT}]+bestaudio/"
            f"best[height<={MAX_VIDEO_HEIGHT}][ext=mp4]/"
            f"best[height<={MAX_VIDEO_HEIGHT}]/"
            "best[ext=mp4]/best"
        ),
        "merge_output_format": "mp4",
        "postprocessor_args": {
            # "faststart" — MP4'ning meta-ma'lumotini (moov atom) fayl BOSHIGA
            # ko'chiradi, shunda Telegram videoni oqim sifatida darhol to'g'ri
            # ko'rsata oladi ("qotgan kadr, faqat ovoz" muammosining yechimi.
            "merger": ["-movflags", "+faststart"],
            "videoconvert": ["-movflags", "+faststart"],
        },
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "max_filesize": MAX_TELEGRAM_MB * 1024 * 1024,
    }
    if IG_COOKIES_FILE and "instagram.com" in url:
        ydl_opts["cookiefile"] = IG_COOKIES_FILE
    if YT_COOKIES_FILE and ("youtube.com" in url or "youtu.be" in url):
        ydl_opts["cookiefile"] = YT_COOKIES_FILE
    # PROXY_URL Railway environment variable orqali sozlansa — barcha
    # so'rovlar shu proxy orqali o'tadi (masalan Instagram cookie sessiyasi
    # tezroq eskirib qolmasligi uchun statik rezidensial/ISP proxy).
    # Format: http://user:pass@host:port yoki socks5://user:pass@host:port
    if PROXY_URL:
        ydl_opts["proxy"] = PROXY_URL
    if POT_PROVIDER_URL and ("youtube.com" in url or "youtu.be" in url):
        # yt-dlp'ning o'zi mos client'ni tanlashiga ruxsat beramiz — faqat
        # POT provider manzilini beramiz, majburlab client tanlamaymiz.
        ydl_opts["extractor_args"] = {
            "youtubepot-bgutilhttp": {"base_url": [POT_PROVIDER_URL]}
        }
    if "instagram.com" in url or "facebook.com" in url or "fb.watch" in url:
        try:
            from yt_dlp.networking.impersonate import ImpersonateTarget
            ydl_opts["impersonate"] = ImpersonateTarget("chrome")
        except Exception as _e:
            log.warning(f"ImpersonateTarget sozlashda xato (o'tkazib yuborildi): {_e}")

    downloaded_path = None
    try:
        was_queued = download_semaphore.locked()
        if was_queued:
            await safe_edit(status, t("queued", lang))

        async with download_semaphore:
            if was_queued:
                await safe_edit(status, t("downloading", lang))
            result = await _download_with_retry(url, ydl_opts)
            downloaded_path = result.get("path")

        if not downloaded_path or not os.path.exists(downloaded_path):
            await safe_edit(status, t("not_found", lang))
            return

        # Agar yuklangan fayl VIDEO emas, RASM bo'lsa (masalan Instagram'dagi
        # oddiy surat post/story) — watermark/kodek ishlovisiz, to'g'ridan-to'g'ri
        # rasm sifatida yuboramiz.
        image_exts = (".jpg", ".jpeg", ".png", ".webp", ".heic")
        if downloaded_path.lower().endswith(image_exts):
            size_mb = os.path.getsize(downloaded_path) / (1024 * 1024)
            if size_mb > MAX_TELEGRAM_MB:
                await safe_edit(status, t("too_large", lang).format(size=size_mb, max=MAX_TELEGRAM_MB))
                return
            await safe_edit(status, t("uploading", lang))
            await bot.send_photo(
                chat_id=message.chat.id,
                photo=FSInputFile(downloaded_path),
                caption=IMAGE_CAPTION,
            )
            await status.delete()
            return

        # Agar foydalanuvchi "Videoga Logo qo'yish" rejimida bo'lsa (va
        # shaxsiy logotipi sozlangan bo'lsa), havola orqali yuklangan
        # videoga ham shuni qo'yamiz. MUHIM: bu ATAYLAB WatermarkStates
        # holatiga bog'langan — /start bosilgach avtomatik o'chadi, aks
        # holda logo har doim (rejimdan tashqarida ham) qo'yilib qolar edi.
        current_state = await state.get_state()
        user_logo = _find_user_logo(message.from_user.id) if current_state == WatermarkStates.waiting_video.state else None
        if user_logo:
            wm_output = downloaded_path.rsplit(".", 1)[0] + "_wm.mp4"
            try:
                position = get_user_logo_position(message.from_user.id)
                await asyncio.to_thread(
                    _add_watermark_sync, downloaded_path, wm_output, user_logo, position
                )
                os.remove(downloaded_path)
                downloaded_path = wm_output
            except Exception as e:
                log.warning(f"Havola-yuklashda shaxsiy logo qo'yishda xato (logosiz yuboriladi): {e}")
                # Logo qo'yilmadi — kodek hamon mos emasligi mumkin, tekshiramiz.
                downloaded_path = await asyncio.to_thread(_ensure_telegram_compatible_sync, downloaded_path)
        else:
            # Logo yo'q — faqat kodek Telegram bilan mosligini tekshirib,
            # kerak bo'lsagina (tezlik uchun) tuzatamiz.
            downloaded_path = await asyncio.to_thread(_ensure_telegram_compatible_sync, downloaded_path)

        size_mb = os.path.getsize(downloaded_path) / (1024 * 1024)
        if size_mb > MAX_TELEGRAM_MB:
            await safe_edit(status, t("too_large", lang).format(size=size_mb, max=MAX_TELEGRAM_MB))
            return

        await safe_edit(status, t("uploading", lang))
        await bot.send_video(
            chat_id=message.chat.id,
            video=FSInputFile(downloaded_path),
            caption=_build_caption(result),
        )
        await status.delete()

    except yt_dlp.utils.DownloadError as e:
        log.warning(f"Download xato: {e}")
        err_text = str(e).lower()

        # RASM post/karusel bo'lsa (Instagram, Twitter/X va h.k.), yt-dlp
        # "no video" turidagi xato beradi (u faqat video uchun mo'ljallangan).
        # Bunday holda gallery-dl orqali (rasm uchun mos vosita) qayta urinamiz.
        no_video_patterns = (
            "no video formats",
            "no video could be found",
            "there is no video in this post",
        )
        if any(p in err_text for p in no_video_patterns):
            try:
                img_dir = tempfile.mkdtemp()
                image_paths = await asyncio.to_thread(
                    _download_images_gallery_dl_sync, url, img_dir
                )
                if image_paths:
                    await safe_edit(status, t("uploading", lang))
                    if len(image_paths) == 1:
                        await bot.send_photo(
                            chat_id=message.chat.id, photo=FSInputFile(image_paths[0]),
                            caption=IMAGE_CAPTION,
                        )
                    else:
                        media = [InputMediaPhoto(media=FSInputFile(p)) for p in image_paths[:10]]
                        media[0].caption = IMAGE_CAPTION
                        await bot.send_media_group(chat_id=message.chat.id, media=media)
                    await status.delete()
                    for p in image_paths:
                        os.remove(p)
                    shutil.rmtree(img_dir, ignore_errors=True)
                    return
                shutil.rmtree(img_dir, ignore_errors=True)
            except Exception as ge:
                log.warning(f"gallery-dl zaxira usuli ham ishlamadi: {ge}")
            await safe_edit(status, t("generic_error", lang))
            return

        if "login" in err_text or "rate-limit" in err_text or "restricted" in err_text:
            await safe_edit(status, t("login_required", lang))
        else:
            await safe_edit(status, t("generic_error", lang))
    except Exception as e:
        import traceback
        log.error(f"Kutilmagan xato: turi={type(e).__name__}, tafsilot={e!r}")
        log.error(traceback.format_exc())
        await safe_edit(status, t("unexpected_error", lang))
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

    # MUHIM: caption_router `router`dan OLDIN include qilinishi shart —
    # shunda /caption va CaptionStates jarayonidagi xabarlar avval
    # caption_mode.py'dagi handlerlarga tekshiriladi.
    dp.include_router(caption_router)
    dp.include_router(router)

    from aiogram.types import BotCommand, BotCommandScopeDefault, BotCommandScopeChat
    default_commands = [
        BotCommand(command="start", description="Botni ishga tushirish / yordam"),
        BotCommand(command="setlogo", description="Watermark uchun yangi GIF logo o'rnatish"),
        BotCommand(command="logooff", description="Shaxsiy logoni o'chirish"),
        BotCommand(command="setposition", description="Logo videoda qayerda chiqishini tanlash"),
        BotCommand(command="caption", description="Videoga o'zbekcha titr qo'shish"),
    ]
    # Hamma foydalanuvchi uchun umumiy (standart) buyruqlar ro'yxati —
    # cookies buyruqlari BU YERDA YO'Q, shuning uchun oddiy foydalanuvchilar
    # menyusida ko'rinmaydi.
    await bot.set_my_commands(default_commands, scope=BotCommandScopeDefault())

    # Faqat OWNER_CHAT_IDS'dagi shaxslar uchun — yuqoridagi ro'yxat +
    # cookies buyruqlari. Har bir admin uchun ALOHIDA (chat-scope) sozlanadi,
    # chunki Telegram bunday "faqat ma'lum shaxslarga" qamrovni global emas,
    # har bir chat_id uchun alohida talab qiladi.
    owner_commands = default_commands + [
        BotCommand(command="setcookies_ig", description="Instagram cookies'ni yangilash"),
        BotCommand(command="setcookies_yt", description="YouTube cookies'ni yangilash"),
    ]
    for owner_id in OWNER_CHAT_IDS:
        try:
            await bot.set_my_commands(owner_commands, scope=BotCommandScopeChat(chat_id=owner_id))
        except Exception as e:
            log.warning(f"Admin ({owner_id}) uchun buyruqlar menyusini sozlashda xato: {e}")

    log.info("Video bot ishga tushmoqda...")
    await dp.start_polling(bot, allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    asyncio.run(main())