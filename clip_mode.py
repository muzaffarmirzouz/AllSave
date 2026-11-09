# -*- coding: utf-8 -*-
"""
"/clip" bo'limi — YouTube videodan KERAKLI QISMNI qirqib olish.

Nima qiladi:
  Foydalanuvchi uzun YouTube videoning havolasini va vaqt oralig'ini
  (masalan 34:15-35:15) yuboradi — bot FAQAT shu qismni yuklab, qirqib,
  Telegram'ga jo'natadi. Butun videoni yuklab OLINMAYDI: yt-dlp
  "download_ranges" orqali ffmpeg faqat kerakli bo'lakni oladi, shuning
  uchun 1 soatlik videodan 1 daqiqa olish ham tez va arzon.

Ikki xil ishlatish usuli:
  1) Bir xabarda:   https://youtu.be/XXXX 34:15-35:15
  2) Bosqichma-bosqich: /clip (yoki menyudagi "✂️ Videoni qirqish" tugmasi)
     -> havola yuboriladi -> vaqt oralig'i yoziladi.
  Oddiy YouTube havolasi (vaqtsiz) avvalgidek butun videoni yuklaydi —
  bu modul unga TEGMAYDI.

Vaqt formatlari: 34:15-35:15 | 1:02:30-1:04:00 | 34:15 35:15 |
                 34:15 dan 35:15 gacha | 90-150 (soniya)

Botga ulash (bot.py):
  - `from clip_mode import clip_router`
  - main()da clip_router'ni caption_router'dan OLDIN include qiling
    (u faqat YouTube havola+vaqt yoki /clip jarayonidagi xabarlarni ushlaydi).

Sozlamalar (Railway Variables, hammasi ixtiyoriy):
  CLIP_MAX_SECONDS    — bitta qism eng ko'pi bilan necha soniya (standart 300)
  CLIP_MAX_HEIGHT     — sifat chegarasi, videoning kichik tomoni (standart 720)
  CLIP_MAX_CONCURRENT — bir vaqtda nechta qirqish (standart 2)
YouTube cookies/proxy/PO-token sozlamalari bot.py'dagi bilan BIR XIL.
"""

import asyncio
import glob
import importlib
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from typing import Optional, Tuple

import yt_dlp
from aiogram import Router, F, Bot
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, FSInputFile, InlineKeyboardMarkup, InlineKeyboardButton

logger = logging.getLogger(__name__)

clip_router = Router(name="clip_mode")

# ---------------------------------------------------------------- sozlamalar

CLIP_MAX_SECONDS = int(os.getenv("CLIP_MAX_SECONDS", "300"))
CLIP_MAX_HEIGHT = (os.getenv("CLIP_MAX_HEIGHT", "1080").strip() or "1080")
CLIP_MAX_CONCURRENT = int(os.getenv("CLIP_MAX_CONCURRENT", "2"))
clip_semaphore = asyncio.Semaphore(CLIP_MAX_CONCURRENT)
CLIP_DOWNLOAD_TIMEOUT = int(os.getenv("CLIP_DOWNLOAD_TIMEOUT", "420"))  # bitta yuklash uchun maks. soniya
CLIP_QUEUE_TIMEOUT = int(os.getenv("CLIP_QUEUE_TIMEOUT", "600"))  # navbatda maks. kutish

# Vertikal (9:16) rejim: odam yuzini kuzatib kesadi (vertical.py). 16:9 dan 9:16
# kesilganda kadr kengligi uchdan biriga tushadi, shuning uchun manba sifati
# yuqoriroq (standart 1080p) olinadi. Uzunlik chegarasi — CPU/hajm uchun.
CLIP_VERTICAL_MAX_SECONDS = int(os.getenv("CLIP_VERTICAL_MAX_SECONDS", "120"))
CLIP_VERTICAL_SOURCE_HEIGHT = int(os.getenv("CLIP_VERTICAL_SOURCE_HEIGHT", "1080"))

TELEGRAM_LIMIT_MB = 50
# Telegram chegarasidan biroz pastroq — konteyner qo'shimcha hajmi uchun zaxira.
SAFE_LIMIT_MB = 48

# Tezkor usul (ffmpeg bilan faqat kerakli bo'lakni yuklash) ba'zi serverlarda
# YouTube tomonidan rad etiladi ("ffmpeg exited with code 8" = HTTP 4xx xato).
# Shunda ISHONCHLI, lekin sekinroq zaxira usuli ishlatiladi: past sifatli
# (standart 360p) video odatiy (native) yuklagich bilan to'liq yuklanadi,
# so'ng kerakli qism lokal ffmpeg bilan qirqiladi.
CLIP_FALLBACK = os.getenv("CLIP_FALLBACK", "1").strip().lower() not in ("0", "false", "no")
CLIP_FALLBACK_HEIGHT = (os.getenv("CLIP_FALLBACK_HEIGHT", "360").strip() or "360")
CLIP_FALLBACK_MAX_VIDEO_SECONDS = int(os.getenv("CLIP_FALLBACK_MAX_VIDEO_SECONDS", "7200"))
CLIP_FALLBACK_MAX_MB = int(os.getenv("CLIP_FALLBACK_MAX_MB", "700"))
# Tezkor usul ffmpeg xatosi bilan yiqilsa, shuncha soniya uni urinmay,
# to'g'ridan-to'g'ri zaxira usulga o'tamiz (har so'rovda ~20 soniya yo'qotmaslik uchun).
CLIP_SECTION_RETRY_SECONDS = int(os.getenv("CLIP_SECTION_RETRY_SECONDS", "1800"))
_section_skip_until = 0.0


class ClipStates(StatesGroup):
    waiting_link = State()
    waiting_range = State()


class ClipError(Exception):
    """Foydalanuvchiga ko'rsatiladigan tushunarli xato (CLIP_TEXTS kaliti bilan)."""

    def __init__(self, key: str, **fmt):
        super().__init__(key)
        self.key = key
        self.fmt = fmt


def _bot_mod():
    """bot.py modulini qaytaradi. bot.py `python bot.py` bilan ishga tushganda
    u `__main__` nomi ostida bo'ladi — shuni ishlatamiz, shunda bot.py ikkinchi
    marta import qilinib qayta bajarilmaydi (umumiy holat/cookie/sozlamalar
    bitta nusxada qoladi). Circular import bo'lmasligi uchun faqat funksiya
    ICHIDA chaqiriladi."""
    main = sys.modules.get("__main__")
    if main is not None and hasattr(main, "get_user_lang") and hasattr(main, "BOT_USERNAME_TAG"):
        return main
    return importlib.import_module("bot")


# --------------------------------------------------------------- ko'p tillilik

CLIP_TEXTS = {
    "uz": {
        "ask_link": (
            "✂️ YouTube videodan kerakli qismni qirqib beraman.\n\n"
            "Avval YouTube video havolasini yuboring.\n(Bekor qilish: /cancel)"
        ),
        "ask_range": (
            "⏱ Endi kerakli qismning boshlanish va tugash vaqtini yozing, masalan:\n\n"
            "34:15-35:15\n1:02:30-1:04:00\n\n"
            "Bitta qism eng ko'pi bilan {max} bo'lishi mumkin.\n\n"
            "Formatni (16:9 yoki 9:16) keyin tugma bilan so'rayman.\n(Bekor qilish: /cancel)"
        ),
        "bad_range": "❌ Vaqtni tushunmadim. Shunday yozing: 34:15-35:15 (tugash vaqti boshlanishidan keyin bo'lsin).",
        "range_too_long": "❌ Qism juda uzun. Eng ko'pi bilan {max} bo'lishi mumkin — qisqaroq oraliq yozing.",
        "not_youtube": "❌ Bu YouTube havolasiga o'xshamaydi. Hozircha faqat YouTube videolaridan qirqish mumkin. Havolani qayta yuboring yoki /cancel.",
        "downloading": "✂️ Qirqib olinmoqda, biroz kuting...",
        "queued": "⏳ Hozir boshqa videolar qirqilmoqda, navbatingizni kutmoqdaman...",
        "uploading": "✅ Tayyor, yuborilmoqda...",
        "out_of_range": "❌ Siz yozgan boshlanish vaqti video tugaganidan keyin turibdi. Video uzunligi: {dur}.",
        "live": "❌ Jonli efir (live) videosidan qirqib bo'lmaydi.",
        "download_failed": "❌ Qirqib bo'lmadi. Havola noto'g'ri, video o'chirilgan yoki maxfiy bo'lishi mumkin.",
        "youtube_blocked": "❌ YouTube hozir so'rovni rad etdi. Birozdan keyin qayta urinib ko'ring. Takrorlansa, bot egasiga xabar bering.",
        "too_large": "❌ Qirqilgan qism Telegram chegarasiga ({max} MB) sig'madi. Qisqaroq oraliq tanlang.",
        "unexpected_error": "❌ Xatolik yuz berdi, birozdan keyin qayta urinib ko'ring.",
        "fallback_notice": "⏳ Tezkor usul ishlamadi, boshqa usulda yuklanmoqda — bu bir necha daqiqa olishi mumkin, iltimos kuting...",
        "fallback_too_long": "❌ Bu video juda uzun, shuning uchun qirqib bo'lmadi (zaxira usul {max} gacha bo'lgan videolar uchun ishlaydi).",
        "reframing": "📱 9:16 formatga o'tkazilmoqda (odam yuzi kuzatiladi), biroz kuting...",
        "vertical_too_long": "❌ Vertikal (9:16) rejimda qism eng ko'pi bilan {max} bo'lishi mumkin. Qisqaroq oraliq yozing.",
        "vertical_failed": "⚠️ 9:16 qilib bo'lmadi, oddiy (gorizontal) shaklda yuboriladi...",
        "no_face_note": "ℹ️ Odam yuzi topilmadi — kadr markazdan kesildi.",
        "ask_format": "🎬 Qaysi formatda kerak?\n⏱ {a} – {b}",
        "btn_h": "🖥 16:9 (YouTube)",
        "btn_v": "📱 9:16 (Reels)",
        "chosen_h": "✅ Format: 16:9 (YouTube)\n⏱ {a} – {b}",
        "chosen_v": "✅ Format: 9:16 (Reels)\n⏱ {a} – {b}",
        "format_expired": "Bu so'rov eskirgan. Havola va vaqtni qayta yuboring.",
        "vertical_btn_too_long": "9:16 uchun qism {max} dan oshmasligi kerak. 16:9 ni tanlang yoki qisqaroq oraliq yuboring.",
    },
    "ru": {
        "ask_link": (
            "✂️ Вырежу нужный фрагмент из видео YouTube.\n\n"
            "Сначала пришлите ссылку на видео YouTube.\n(Отмена: /cancel)"
        ),
        "ask_range": (
            "⏱ Теперь напишите время начала и конца нужного фрагмента, например:\n\n"
            "34:15-35:15\n1:02:30-1:04:00\n\n"
            "Один фрагмент — не длиннее {max}.\n\n"
            "Формат (16:9 или 9:16) спрошу потом кнопками.\n(Отмена: /cancel)"
        ),
        "bad_range": "❌ Не понял время. Напишите так: 34:15-35:15 (конец должен быть позже начала).",
        "range_too_long": "❌ Фрагмент слишком длинный. Максимум — {max}, напишите промежуток короче.",
        "not_youtube": "❌ Это не похоже на ссылку YouTube. Пока вырезать можно только из видео YouTube. Отправьте ссылку снова или /cancel.",
        "downloading": "✂️ Вырезаю фрагмент, подождите немного...",
        "queued": "⏳ Сейчас вырезаются другие видео, жду своей очереди...",
        "uploading": "✅ Готово, отправляю...",
        "out_of_range": "❌ Время начала позже конца видео. Длина видео: {dur}.",
        "live": "❌ Из прямого эфира (live) вырезать нельзя.",
        "download_failed": "❌ Не удалось вырезать. Ссылка неверна, видео удалено или приватно.",
        "youtube_blocked": "❌ YouTube сейчас отклонил запрос. Попробуйте чуть позже. Если повторяется — сообщите владельцу бота.",
        "too_large": "❌ Фрагмент не поместился в лимит Telegram ({max} МБ). Выберите промежуток короче.",
        "unexpected_error": "❌ Произошла ошибка, попробуйте ещё раз чуть позже.",
        "fallback_notice": "⏳ Быстрый способ не сработал, скачиваю другим способом — это может занять несколько минут, подождите...",
        "fallback_too_long": "❌ Видео слишком длинное, вырезать не удалось (запасной способ работает для видео до {max}).",
        "reframing": "📱 Перевожу в формат 9:16 (слежу за лицом), подождите немного...",
        "vertical_too_long": "❌ В вертикальном режиме (9:16) фрагмент — не длиннее {max}. Напишите промежуток короче.",
        "vertical_failed": "⚠️ Не удалось сделать 9:16, отправлю обычное (горизонтальное) видео...",
        "no_face_note": "ℹ️ Лицо не найдено — кадр обрезан по центру.",
        "ask_format": "🎬 В каком формате нужно?\n⏱ {a} – {b}",
        "btn_h": "🖥 16:9 (YouTube)",
        "btn_v": "📱 9:16 (Reels)",
        "chosen_h": "✅ Формат: 16:9 (YouTube)\n⏱ {a} – {b}",
        "chosen_v": "✅ Формат: 9:16 (Reels)\n⏱ {a} – {b}",
        "format_expired": "Этот запрос устарел. Отправьте ссылку и время ещё раз.",
        "vertical_btn_too_long": "Для 9:16 фрагмент не должен быть длиннее {max}. Выберите 16:9 или отправьте промежуток короче.",
    },
    "en": {
        "ask_link": (
            "✂️ I'll cut the part you need out of a YouTube video.\n\n"
            "First, send me the YouTube video link.\n(Cancel: /cancel)"
        ),
        "ask_range": (
            "⏱ Now write the start and end time of the part you want, e.g.:\n\n"
            "34:15-35:15\n1:02:30-1:04:00\n\n"
            "A single clip can be at most {max} long.\n\n"
            "I'll ask for the format (16:9 or 9:16) with buttons next.\n(Cancel: /cancel)"
        ),
        "bad_range": "❌ I couldn't understand the time. Write it like: 34:15-35:15 (the end must be after the start).",
        "range_too_long": "❌ That clip is too long. The maximum is {max} — please write a shorter range.",
        "not_youtube": "❌ That doesn't look like a YouTube link. For now I can only cut YouTube videos. Send the link again or /cancel.",
        "downloading": "✂️ Cutting the clip, please wait...",
        "queued": "⏳ Other videos are being cut right now, waiting for your turn...",
        "uploading": "✅ Done, sending...",
        "out_of_range": "❌ The start time is after the end of the video. Video length: {dur}.",
        "live": "❌ I can't cut a clip from a live stream.",
        "download_failed": "❌ Couldn't cut the clip. The link may be wrong, or the video deleted/private.",
        "youtube_blocked": "❌ YouTube rejected the request right now. Please try again in a moment. If it keeps happening, tell the bot owner.",
        "too_large": "❌ The clip didn't fit Telegram's {max} MB limit. Please choose a shorter range.",
        "unexpected_error": "❌ Something went wrong, please try again in a moment.",
        "fallback_notice": "⏳ The fast method didn't work, trying another way — this may take a few minutes, please wait...",
        "fallback_too_long": "❌ This video is too long to cut (the backup method works for videos up to {max}).",
        "reframing": "📱 Converting to 9:16 (following the face), please wait...",
        "vertical_too_long": "❌ In vertical (9:16) mode a clip can be at most {max}. Please write a shorter range.",
        "vertical_failed": "⚠️ Couldn't make it 9:16, sending the regular (horizontal) clip...",
        "no_face_note": "ℹ️ No face found — cropped from the center.",
        "ask_format": "🎬 Which format do you need?\n⏱ {a} – {b}",
        "btn_h": "🖥 16:9 (YouTube)",
        "btn_v": "📱 9:16 (Reels)",
        "chosen_h": "✅ Format: 16:9 (YouTube)\n⏱ {a} – {b}",
        "chosen_v": "✅ Format: 9:16 (Reels)\n⏱ {a} – {b}",
        "format_expired": "This request has expired. Please send the link and time again.",
        "vertical_btn_too_long": "For 9:16 the clip must be at most {max}. Choose 16:9 or send a shorter range.",
    },
}


def ct(key: str, lang: str, **kwargs) -> str:
    lang = lang if lang in CLIP_TEXTS else "uz"
    text = CLIP_TEXTS[lang].get(key) or CLIP_TEXTS["uz"].get(key, "")
    return text.format(**kwargs) if kwargs else text


def _lang(user_id: int) -> str:
    try:
        return _bot_mod().get_user_lang(user_id) or "uz"
    except Exception:
        return "uz"


# ------------------------------------------------------------ vaqt va havola

_YT_URL_RE = re.compile(
    r"https?://(?:www\.|m\.|music\.)?(?:youtube\.com|youtu\.be)/\S+", re.IGNORECASE
)
_ANY_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
# 34:15 yoki 1:02:30 — oldida/orqasida raqam yoki ":" bo'lmasligi shart.
_TIME_RE = re.compile(r"(?<![\d:])(\d{1,2}(?::\d{1,2}){1,2})(?![\d:])")
# Faqat soniyalar: "90-150"
_BARE_RANGE_RE = re.compile(r"^\s*(\d{1,5})\s*[-–—]\s*(\d{1,5})\s*(?:s|sek|sec)?\s*$", re.IGNORECASE)


def _find_time_tokens(text: str) -> list:
    return _TIME_RE.findall(_ANY_URL_RE.sub(" ", text or ""))


def _tok_to_seconds(tok: str) -> Optional[int]:
    parts = [int(p) for p in tok.split(":")]
    if len(parts) == 2:
        h, (m, s) = 0, parts
    else:
        h, m, s = parts
        if m >= 60:
            return None
    if s >= 60:
        return None
    return h * 3600 + m * 60 + s


def parse_time_range(text: str) -> Optional[Tuple[int, int]]:
    """Matndan (boshlanish, tugash) soniyalarini ajratadi. Tushunib bo'lmasa
    yoki tugash <= boshlanish bo'lsa — None."""
    cleaned = _ANY_URL_RE.sub(" ", text or "")
    toks = _TIME_RE.findall(cleaned)
    if len(toks) >= 2:
        start, end = _tok_to_seconds(toks[0]), _tok_to_seconds(toks[1])
        if start is None or end is None:
            return None
    else:
        m = _BARE_RANGE_RE.match(cleaned)
        if not m:
            return None
        start, end = int(m.group(1)), int(m.group(2))
    if end <= start:
        return None
    return start, end


def fmt_time(sec: int) -> str:
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# ------------------------------------------------------ yuklab olish va qirqish

def _build_ydl_opts(tmp_dir: str, ranges_fn=None, fallback: bool = False, source_height: Optional[int] = None) -> dict:
    """bot.py'dagi YouTube sozlamalari (cookies, proxy, JS runtime, PO token)
    bilan BIR XIL, faqat butun video o'rniga `download_ranges` bilan."""
    B = _bot_mod()
    opts = {
        "outtmpl": os.path.join(tmp_dir, "clip.%(ext)s"),
        # Hajm filtri ATAYLAB yo'q: butun video hajmi 50 MB'dan katta bo'ladi,
        # lekin biz faqat kichik bo'lakni olamiz. Hajm qirqilgandan KEYIN
        # tekshiriladi (kerak bo'lsa moslashtiriladi).
        "format": (
            "bestvideo[vcodec^=avc1]+bestaudio[acodec^=mp4a]/"
            "bestvideo[vcodec^=avc1]+bestaudio/"
            "bestvideo+bestaudio/best"
        ),
        "format_sort": [f"res:{source_height or CLIP_MAX_HEIGHT}", "fps", "vcodec:h264"],
        "merge_output_format": "mp4",
        "download_ranges": ranges_fn,
        # Aniq qirqish (keyframe chegarasiga yopishib qolmasin) — qayta kodlaydi,
        # lekin faqat kichik bo'lakni, shuning uchun tez.
        "force_keyframes_at_cuts": True,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        "postprocessor_args": {"merger": ["-movflags", "+faststart"]},
    }
    if getattr(B, "YT_COOKIES_FILE", None):
        opts["cookiefile"] = B.YT_COOKIES_FILE
    yt_mode = os.environ.get("YT_PROXY_MODE", "proxy").strip().lower()
    if getattr(B, "PROXY_URL", "") and yt_mode != "direct":
        opts["proxy"] = B.PROXY_URL
    for name in ("deno", "node", "bun"):
        p = shutil.which(name)
        if p:
            opts["js_runtimes"] = {name: {"path": p}}
            opts["remote_components"] = ["ejs:github"]
            break
    if getattr(B, "POT_PROVIDER_URL", ""):
        opts["extractor_args"] = {"youtubepot-bgutilhttp": {"base_url": [B.POT_PROVIDER_URL]}}
    if fallback:
        # Zaxira usul: ffmpeg-bo'lak yo'q, past sifatli BUTUN video odatiy
        # yuklagich bilan (asosiy bot bilan bir xil yo'l) yuklanadi.
        opts.pop("download_ranges", None)
        opts.pop("force_keyframes_at_cuts", None)
        opts["outtmpl"] = os.path.join(tmp_dir, "full.%(ext)s")
        fb_h = min(source_height, 720) if source_height else CLIP_FALLBACK_HEIGHT
        opts["format_sort"] = [f"res:{fb_h}", "fps", "vcodec:h264"]
        opts["max_filesize"] = CLIP_FALLBACK_MAX_MB * 1024 * 1024
    return opts


def _clip_download_sync(url: str, start: int, end: int, tmp_dir: str, fallback: bool = False,
                        source_height: Optional[int] = None) -> dict:
    """Bloklaydigan: avval video ma'lumotini oladi (uzunlik, live emasligini
    tekshiradi), keyin FAQAT [start, end] oralig'ini yuklab oladi."""
    holder = {"start": start, "end": end}

    def ranges_fn(info_dict, ydl):
        return [{"start_time": holder["start"], "end_time": holder["end"]}]

    with yt_dlp.YoutubeDL(_build_ydl_opts(tmp_dir, None if fallback else ranges_fn, fallback, source_height)) as ydl:
        info = ydl.extract_info(url, download=False)
        if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming"):
            raise ClipError("live")
        duration = info.get("duration")
        if duration:
            duration = int(duration)
            if holder["start"] >= duration:
                raise ClipError("out_of_range", dur=fmt_time(duration))
            if holder["end"] > duration:
                holder["end"] = duration  # oxiridan oshib ketsa — video oxirigacha
        if fallback and duration and duration > CLIP_FALLBACK_MAX_VIDEO_SECONDS:
            raise ClipError("fallback_too_long", max=fmt_time(CLIP_FALLBACK_MAX_VIDEO_SECONDS))
        try:
            ydl.process_ie_result(info, download=True)
        except yt_dlp.utils.DownloadError:
            if not fallback:
                _diagnose_section_failure(info, holder["start"], ydl)
            raise

    if fallback:
        return _cut_from_full_sync(tmp_dir, info, holder)

    candidates = [
        p for p in glob.glob(os.path.join(tmp_dir, "clip*"))
        if p.lower().endswith(".mp4") and os.path.getsize(p) > 0
    ]
    if not candidates:
        raise ClipError("download_failed")
    path = max(candidates, key=os.path.getsize)
    return {
        "path": path,
        "title": (info.get("title") or "").strip(),
        "start": holder["start"],
        "end": holder["end"],
    }


_last_diag = ""
_diag_last_sent = 0.0
DIAG_COOLDOWN_SECONDS = 1800


def _scrub(text: str) -> str:
    """Log/xabarlarda URL va proxy parollari ko'rinmasligi uchun."""
    return re.sub(r"https?://\S+", "<url>", text or "")


def _probe_http(url: str, headers: dict, proxy: Optional[str]) -> str:
    """Oddiy (ffmpeg'siz) kichik Range-so'rov — asosiy yuklagich kabi."""
    import urllib.error
    import urllib.request

    h = dict(headers or {})
    h["Range"] = "bytes=0-1023"
    handlers = [urllib.request.ProxyHandler({"http": proxy, "https": proxy})] if proxy else []
    opener = urllib.request.build_opener(*handlers)
    try:
        with opener.open(urllib.request.Request(url, headers=h), timeout=30) as resp:
            return f"HTTP {resp.status}, {len(resp.read(1024))} bayt olindi"
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code}"
    except Exception as e:
        return f"xato {type(e).__name__}: {_scrub(str(e))[:150]}"


def _probe_ydl(ydl, url: str, headers: dict, range_hdr: str, impersonate: bool) -> str:
    """yt-dlp'ning O'Z so'rov mexanizmi (asosiy yuklagich ishlatadigan) bilan
    kichik sinov so'rovi."""
    try:
        from yt_dlp.networking import Request

        ext = {}
        if impersonate:
            from yt_dlp.networking.impersonate import ImpersonateTarget

            ext["impersonate"] = ImpersonateTarget("chrome")
        h = dict(headers or {})
        h["Range"] = range_hdr
        resp = ydl.urlopen(Request(url, headers=h, extensions=ext))
        n = len(resp.read(1024))
        try:
            resp.close()
        except Exception:
            pass
        return f"HTTP {getattr(resp, 'status', '?')}, {n} bayt"
    except Exception as e:
        code = getattr(e, "status", None)
        return f"HTTP {code}" if code else f"xato {type(e).__name__}: {_scrub(str(e))[:120]}"


def _diagnose_section_failure(info: dict, start: int, ydl=None) -> None:
    """Tezkor usul (ffmpeg bilan bo'lak yuklash) yiqilganda sababini aniqlaydi:
    har bir format uchun ffmpeg'ni o'zimiz 1 soniyalik sinov bilan ishga tushirib
    HAQIQIY xato matnini olamiz, va xuddi shu havolaga oddiy so'rov yuboramiz.
    Natija logga yoziladi va bot egasiga yuboriladi (URL/parollar yashiriladi)."""
    global _last_diag
    lines = []
    try:
        B = _bot_mod()
        proxy = None
        yt_mode = os.environ.get("YT_PROXY_MODE", "proxy").strip().lower()
        if getattr(B, "PROXY_URL", "") and yt_mode != "direct":
            proxy = B.PROXY_URL
        ver = getattr(getattr(yt_dlp, "version", None), "__version__", "?")
        lines.append(
            f"yt-dlp={ver}, proxy={'ha' if proxy else 'yoq'}, "
            f"cookies={'bor' if getattr(B, 'YT_COOKIES_FILE', None) else 'YOQ'}, "
            f"pot={'ha' if getattr(B, 'POT_PROVIDER_URL', '') else 'YOQ'}"
        )
        for f in info.get("requested_formats") or [info]:
            tag = f"format={f.get('format_id')} proto={f.get('protocol')} vcodec={f.get('vcodec')} acodec={f.get('acodec')}"
            url = f.get("url") or ""
            if not url.startswith("http"):
                lines.append(f"{tag}: URL yo'q yoki http emas")
                continue
            headers = f.get("http_headers") or info.get("http_headers") or {}
            cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
            hdr = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
            if hdr:
                cmd += ["-headers", hdr]
            if proxy:
                cmd += ["-http_proxy", proxy]
            cmd += ["-ss", str(start), "-i", url, "-t", "1", "-f", "null", "-"]
            try:
                r = subprocess.run(cmd, capture_output=True, timeout=60)
                err = _scrub(r.stderr.decode(errors="ignore")).strip()[-350:]
                lines.append(f"{tag}\n  ffmpeg kod={r.returncode}: {err or '(bo`sh)'}")
            except Exception as e:
                lines.append(f"{tag}\n  ffmpeg sinovi xato berdi: {_scrub(repr(e))[:150]}")
            lines.append(f"  oddiy Range-so'rov: {_probe_http(url, headers, proxy)}")
            if ydl is not None:
                lines.append(f"  yt-dlp o'z so'rovi (1KB): {_probe_ydl(ydl, url, headers, 'bytes=0-1023', False)}")
                lines.append(f"  yt-dlp o'z so'rovi (10MB-Range): {_probe_ydl(ydl, url, headers, 'bytes=0-10485759', False)}")
                lines.append(f"  yt-dlp + chrome-impersonate (1KB): {_probe_ydl(ydl, url, headers, 'bytes=0-1023', True)}")
            lines.append(
                f"  impersonate-bayrog'i={f.get('impersonate')!r}, "
                f"downloader_options={f.get('downloader_options')!r}"
            )
    except Exception as e:
        lines.append(f"diagnostika o'zi xato berdi: {_scrub(repr(e))[:200]}")
    _last_diag = "\n".join(lines)
    logger.warning("/clip DIAG:\n" + _last_diag)


async def _notify_owner_diag(bot: Bot, text: str) -> None:
    """Diagnostikani bot egalariga (OWNER_CHAT_IDS) yuboradi — kamida
    DIAG_COOLDOWN_SECONDS oralig'ida bir marta."""
    global _diag_last_sent
    now = time.monotonic()
    if _diag_last_sent and now - _diag_last_sent < DIAG_COOLDOWN_SECONDS:
        return
    _diag_last_sent = now
    for owner_id in getattr(_bot_mod(), "OWNER_CHAT_IDS", []) or []:
        try:
            await bot.send_message(owner_id, "🔧 /clip diagnostika (tezkor usul yiqildi):\n\n" + text[:3500])
        except Exception as e:
            logger.warning(f"/clip diagnostikani egaga yuborib bo'lmadi: {e}")


_err_last_sent = 0.0
ERR_COOLDOWN_SECONDS = 300


async def _notify_owner_error(bot: Bot, where: str, err: Exception) -> None:
    """Oxirgi (foydalanuvchi ko'rgan) xatoning HAQIQIY matnini bot egasiga
    yuboradi — kamida ERR_COOLDOWN_SECONDS oralig'ida bir marta."""
    global _err_last_sent
    now = time.monotonic()
    if _err_last_sent and now - _err_last_sent < ERR_COOLDOWN_SECONDS:
        return
    _err_last_sent = now
    text = f"🛑 /clip XATO ({where}):\n\n{type(err).__name__}: {_scrub(str(err))[-1200:]}"
    for owner_id in getattr(_bot_mod(), "OWNER_CHAT_IDS", []) or []:
        try:
            await bot.send_message(owner_id, text[:3500])
        except Exception as e:
            logger.warning(f"/clip xatoni egaga yuborib bo'lmadi: {e}")


def _cut_local_sync(src: str, out: str, start: int, dur: int) -> None:
    """Lokal fayldan [start, start+dur] oralig'ini aniq qirqadi (qayta kodlab)."""
    cmd = [
        "ffmpeg", "-y", "-ss", str(start), "-i", src, "-t", str(dur),
        "-c:v", "libx264", "-preset", "fast", "-crf", "19", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", out,
    ]
    r = subprocess.run(cmd, capture_output=True, timeout=900)
    if r.returncode != 0 or not os.path.exists(out) or os.path.getsize(out) == 0:
        logger.error(f"/clip lokal qirqishda xato: {r.stderr[-400:]!r}")
        raise ClipError("download_failed")


def _cut_from_full_sync(tmp_dir: str, info: dict, holder: dict) -> dict:
    fulls = [
        p for p in glob.glob(os.path.join(tmp_dir, "full*"))
        if p.lower().endswith((".mp4", ".mkv", ".webm")) and os.path.getsize(p) > 0
    ]
    if not fulls:
        raise ClipError("download_failed")
    src = max(fulls, key=os.path.getsize)
    out = os.path.join(tmp_dir, "clip_fb.mp4")
    _cut_local_sync(src, out, holder["start"], holder["end"] - holder["start"])
    try:
        os.remove(src)  # katta fayl — joyni darhol bo'shatamiz
    except OSError:
        pass
    return {
        "path": out,
        "title": (info.get("title") or "").strip(),
        "start": holder["start"],
        "end": holder["end"],
    }


def _clean_dir(tmp_dir: str) -> None:
    for p in glob.glob(os.path.join(tmp_dir, "*")):
        try:
            if os.path.isfile(p):
                os.remove(p)
        except OSError:
            pass


async def _download_with_retry(url: str, start: int, end: int, tmp_dir: str, on_fallback=None, on_diag=None,
                                 source_height: Optional[int] = None) -> dict:
    """1) Tezkor usul: ffmpeg faqat kerakli bo'lakni yuklaydi.
    2) U ffmpeg/HTTP xatosi bilan yiqilsa — zaxira usul (past sifatli to'liq
       yuklash + lokal qirqish). Tezkor usul yiqilganini bir muddat eslab
       qolamiz va keyingi so'rovlarda darhol zaxiraga o'tamiz."""
    global _section_skip_until
    permanent_words = ("private", "unavailable", "removed", "not available", "sign in", "not a bot")

    if not CLIP_FALLBACK or time.monotonic() >= _section_skip_until:
        try:
            return await asyncio.to_thread(_clip_download_sync, url, start, end, tmp_dir, False, source_height)
        except ClipError:
            raise
        except yt_dlp.utils.DownloadError as e:
            err = str(e).lower()
            logger.warning(f"/clip tezkor usul xatosi: {e}")
            if on_diag is not None and _last_diag:
                await on_diag(_last_diag)
            if not CLIP_FALLBACK or any(p in err for p in permanent_words):
                raise
            if "ffmpeg" in err:
                _section_skip_until = time.monotonic() + CLIP_SECTION_RETRY_SECONDS
                logger.warning(
                    f"/clip: tezkor usul {CLIP_SECTION_RETRY_SECONDS}s davomida o'tkazib yuboriladi, zaxira usul ishlatiladi"
                )

    if on_fallback is not None:
        await on_fallback()
    await asyncio.to_thread(_clean_dir, tmp_dir)
    try:
        return await asyncio.to_thread(_clip_download_sync, url, start, end, tmp_dir, True, source_height)
    except yt_dlp.utils.DownloadError as e:
        logger.warning(f"/clip zaxira usul ham xato berdi: {e}")
        raise


def _tier_for_kbps(video_kbps: int) -> int:
    """Mavjud bitratega mos qisqa tomon (piksel). Bitrate kam bo'lsa — kichikroq
    o'lcham, aks holda blok-blok (past sifatli) rasm chiqadi."""
    if video_kbps < 700:
        return 360
    if video_kbps < 1400:
        return 480
    if video_kbps < 3000:
        return 720
    return 1080


def _shrink_to_fit_sync(path: str, duration_s: float, limit_mb: int) -> str:
    """Fayl limitdan katta bo'lsa — bitrate byudjeti ichida eng yaxshi sifatda
    qayta kodlaydi (CRF + maxrate, sekinroq preset). Sig'dira olmasa ASL faylni
    qaytaradi, chaqiruvchi "too_large" deb xabar beradi."""
    out = path.rsplit(".", 1)[0] + "_small.mp4"
    audio_kbps = 128 if duration_s <= 150 else 96
    total_kbit = limit_mb * 1024 * 8 * 0.92  # konteyner uchun ~8% zaxira
    video_kbps = int(total_kbit / max(duration_s, 1.0) - audio_kbps)
    if video_kbps < 150:
        return path
    short = _tier_for_kbps(video_kbps)
    preset = os.getenv("CLIP_ENC_PRESET", "").strip() or ("medium" if duration_s <= 150 else "fast")
    cmd = ["ffmpeg", "-y", "-i", path,
           "-vf", f"scale='if(gt(iw,ih),-2,min({short},iw))':'if(gt(iw,ih),min({short},ih),-2)':flags=lanczos"]
    cmd += [
        "-c:v", "libx264", "-preset", preset, "-profile:v", "high",
        "-crf", "19", "-maxrate", f"{video_kbps}k", "-bufsize", f"{video_kbps * 2}k",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", f"{audio_kbps}k",
        "-movflags", "+faststart", out,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=1500)
        if r.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 0:
            os.remove(path)
            return out
        logger.warning(f"/clip kichraytirishda xato: {r.stderr[-300:]!r}")
    except Exception as e:
        logger.warning(f"/clip kichraytirishda kutilmagan xato: {e}")
    if os.path.exists(out):
        try:
            os.remove(out)
        except OSError:
            pass
    return path


_VERT_WORD_RE = re.compile(r"(?<!\S)(?:vertikal|vertical|reels|9x16|9х16)(?!\S)", re.IGNORECASE)
_VERT_RATIO_RE = re.compile(r"(?<!\S)9\s?:\s?16(?!\S)")


def split_vertical_flag(text: str) -> Tuple[bool, str]:
    """Matndan "vertikal (9:16)" belgisini ajratadi. -> (vertikal_mi, tozalangan_matn).
    So'zlar: vertikal / vertical / reels / 9x16. "9:16" faqat alohida so'z bo'lib
    kelsa VA qolgan matnda kamida ikkita vaqt bo'lsa belgi hisoblanadi (aks holda
    "9:16-10:00" kabi haqiqiy vaqt bilan adashib ketmasligi uchun). URL'lar tegilmaydi."""
    text = text or ""
    urls = _ANY_URL_RE.findall(text)
    masked = _ANY_URL_RE.sub("\x00", text)
    vertical = False
    if _VERT_WORD_RE.search(masked):
        vertical = True
        masked = _VERT_WORD_RE.sub(" ", masked)
    m = _VERT_RATIO_RE.search(masked)
    if m:
        without = masked[:m.start()] + " " + masked[m.end():]
        if len(_TIME_RE.findall(without)) >= 2:
            vertical = True
            masked = without
    it = iter(urls)
    return vertical, re.sub("\x00", lambda _m: next(it), masked)


_HORIZ_WORD_RE = re.compile(r"(?<!\S)(?:gorizontal|horizontal|16x9|16х9)(?!\S)", re.IGNORECASE)


def split_format_flag(text: str) -> Tuple[Optional[str], str]:
    """Matndan aniq format belgisini ajratadi. -> ("v" | "h" | None, tozalangan_matn).
    "v": vertikal (9:16 / vertikal / vertical / reels / 9x16),
    "h": gorizontal (gorizontal / horizontal / 16x9).
    None — format aytilmagan, bot tugma bilan so'raydi."""
    vertical, cleaned = split_vertical_flag(text)
    if vertical:
        return "v", cleaned
    urls = _ANY_URL_RE.findall(cleaned)
    masked = _ANY_URL_RE.sub("\x00", cleaned)
    if _HORIZ_WORD_RE.search(masked):
        masked = _HORIZ_WORD_RE.sub(" ", masked)
        it = iter(urls)
        return "h", re.sub("\x00", lambda _m: next(it), masked)
    return None, cleaned


# --- Format so'rash (tugmalar) ---------------------------------------------
# Havola + vaqt oralig'i aniq bo'lgach, bot "16:9 yoki 9:16?" deb so'raydi. Telegram
# callback_data 64 baytdan oshmasligi kerak, shuning uchun havola/vaqt xotirada
# (qisqa token bilan) saqlanadi. Bot qayta ishga tushsa, eski tugmalar "eskirgan" deydi.
_pending: dict = {}
PENDING_TTL_SECONDS = 1800
PENDING_MAX = 500


def _pending_put(uid: int, url: str, start: int, end: int) -> str:
    now = time.monotonic()
    for k in [k for k, v in _pending.items() if now - v[4] > PENDING_TTL_SECONDS]:
        _pending.pop(k, None)
    while len(_pending) >= PENDING_MAX:
        _pending.pop(min(_pending, key=lambda k: _pending[k][4]), None)
    token = uuid.uuid4().hex[:10]
    _pending[token] = (uid, url, start, end, now)
    return token


async def _ask_format(message: Message, uid: int, url: str, start: int, end: int) -> None:
    lang = _lang(uid)
    if end - start > CLIP_MAX_SECONDS:
        await message.answer(ct("range_too_long", lang, max=fmt_time(CLIP_MAX_SECONDS)))
        return
    token = _pending_put(uid, url, start, end)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=ct("btn_h", lang), callback_data=f"clipfmt:{token}:h"),
        InlineKeyboardButton(text=ct("btn_v", lang), callback_data=f"clipfmt:{token}:v"),
    ]])
    await message.answer(ct("ask_format", lang, a=fmt_time(start), b=fmt_time(end)), reply_markup=kb)


async def _dispatch(message: Message, bot: Bot, uid: int, url: str, rng: Tuple[int, int], fmt: Optional[str]) -> None:
    """Format aniq aytilgan bo'lsa (v/h) — darhol qirqadi, aks holda tugma bilan so'raydi."""
    if fmt is None:
        await _ask_format(message, uid, url, rng[0], rng[1])
    else:
        await _run_clip(message, bot, uid, url, *rng, vertical=(fmt == "v"))


def _reframe_sync(path: str, src_w: int, src_h: int, duration: float) -> dict:
    """16:9 -> 9:16 (yuzni kuzatib). Muvaffaqiyatda eski faylni o'chiradi."""
    import vertical  # sekin import: cv2/numpy faqat vertikal rejimda yuklanadi

    out = path.rsplit(".", 1)[0] + "_9x16.mp4"
    # Chiqish hajmi ~46 MB dan oshmasligi uchun bitrate chegarasi (audio 128k).
    kbps = int(46 * 1024 * 8 * 0.92 / max(duration, 1.0) - 128)
    res = vertical.reframe_to_vertical(path, out, src_w, src_h, duration=duration, max_kbps=max(kbps, 400))
    if res.get("path"):
        try:
            os.remove(path)
        except OSError:
            pass
    return res


def _build_clip_caption(title: str, start: int, end: int, note: str = "") -> str:
    B = _bot_mod()
    footer = f"\n\n✂️ {B.BOT_USERNAME_TAG} orqali qirqib olindi"
    head = f"⏱ {fmt_time(start)} – {fmt_time(end)}"
    if note:
        head += f"\n{note}"
    title = (title or "").strip()
    room = 1024 - len(footer) - len(head) - 6
    if title and room > 10:
        if len(title) > room:
            title = title[:room].rstrip() + "..."
        head = f"{title}\n{head}"
    return (head + footer)[:1024]


# ------------------------------------------------------------- asosiy jarayon

async def _run_clip(message: Message, bot: Bot, user_id: int, url: str, start: int, end: int,
                    vertical: bool = False):
    B = _bot_mod()
    lang = _lang(user_id)
    status = await message.answer(ct("downloading", lang))
    max_len = fmt_time(CLIP_MAX_SECONDS)

    if end - start > CLIP_MAX_SECONDS:
        await B.safe_edit(status, ct("range_too_long", lang, max=max_len))
        return
    if vertical and end - start > CLIP_VERTICAL_MAX_SECONDS:
        await B.safe_edit(status, ct("vertical_too_long", lang, max=fmt_time(CLIP_VERTICAL_MAX_SECONDS)))
        return

    with tempfile.TemporaryDirectory() as tmp_dir:
        try:
            was_queued = clip_semaphore.locked()
            if was_queued:
                await B.safe_edit(status, ct("queued", lang))
            # Navbatda ham cheksiz kutmaymiz (boshqa ish osilib qolgan bo'lishi mumkin).
            try:
                await asyncio.wait_for(clip_semaphore.acquire(), CLIP_QUEUE_TIMEOUT)
            except asyncio.TimeoutError:
                logger.error("/clip: navbat vaqti tugadi — semafor band (osilib qolgan ish bo'lishi mumkin)")
                await _notify_owner_error(bot, "navbat", RuntimeError("clip semaforini olib bo'lmadi, band ishlar osilib qolgan bo'lishi mumkin"))
                raise ClipError("download_failed")
            try:
                if was_queued:
                    await B.safe_edit(status, ct("downloading", lang))
                async def _notice():
                    await B.safe_edit(status, ct("fallback_notice", lang))

                async def _diag(text):
                    await _notify_owner_diag(bot, text)

                try:
                    info = await asyncio.wait_for(
                        _download_with_retry(
                            url, start, end, tmp_dir, on_fallback=_notice, on_diag=_diag,
                            source_height=CLIP_VERTICAL_SOURCE_HEIGHT if vertical else None,
                        ),
                        CLIP_DOWNLOAD_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    logger.error(f"/clip: yuklash {CLIP_DOWNLOAD_TIMEOUT}s ichida tugamadi — bekor qilindi")
                    await _notify_owner_error(bot, "yuklash", RuntimeError(f"yuklash {CLIP_DOWNLOAD_TIMEOUT}s dan oshdi (osilib qoldi)"))
                    raise ClipError("download_failed")
            finally:
                clip_semaphore.release()

            path = info["path"]
            start, end = info["start"], info["end"]

            applied = False
            face_note = ""

            # --- Vertikal (9:16): yuzni kuzatib kesish (logo/outro'dan OLDIN) ---
            if vertical:
                await B.safe_edit(status, ct("reframing", lang))
                try:
                    vw0, vh0, _vd0 = await asyncio.to_thread(B._probe_video_meta_sync, path)
                    if not (vw0 and vh0):
                        raise RuntimeError("video o'lchamini aniqlab bo'lmadi")
                    rf = await asyncio.to_thread(_reframe_sync, path, vw0, vh0, float(end - start))
                    if rf.get("path"):
                        path = rf["path"]
                        applied = True  # allaqachon H.264 + faststart — qayta tekshirish shart emas
                        if rf.get("face_ratio", 1.0) < 0.2:
                            face_note = ct("no_face_note", lang)
                except Exception as e:
                    logger.error(f"/clip 9:16 kesishda xato (gorizontal yuboriladi): {e}")
                    await _notify_owner_error(bot, "9:16", e)
                    await B.safe_edit(status, ct("vertical_failed", lang))

            # --- Logo / Outro: bot.py'dagi doimiy sozlamalar bilan bir xil ---
            user_logo = B._find_user_logo(user_id) if B.get_user_logo_enabled(user_id) else None
            user_outro = B._find_user_outro(user_id) if B.get_user_outro_enabled(user_id) else None
            if user_logo:
                wm_out = path.rsplit(".", 1)[0] + "_wm.mp4"
                try:
                    await asyncio.to_thread(
                        B._add_watermark_sync, path, wm_out, user_logo,
                        B.get_user_logo_position(user_id), True,
                    )
                    os.remove(path)
                    path = wm_out
                    applied = True
                except Exception as e:
                    logger.warning(f"/clip: logo qo'yishda xato (logosiz yuboriladi): {e}")
            if user_outro:
                outro_out = path.rsplit(".", 1)[0] + "_outro.mp4"
                try:
                    await asyncio.to_thread(B._append_outro_sync, path, outro_out, user_outro)
                    os.remove(path)
                    path = outro_out
                    applied = True
                except Exception as e:
                    logger.warning(f"/clip: outro qo'shishda xato (outrosiz yuboriladi): {e}")
            if not applied:
                path = await asyncio.to_thread(B._ensure_telegram_compatible_sync, path)

            # --- Hajm: 50 MB'dan oshsa kichraytirish ---
            shrunk = False
            if os.path.getsize(path) > SAFE_LIMIT_MB * 1024 * 1024:
                vw, vh, vdur = await asyncio.to_thread(B._probe_video_meta_sync, path)
                path_before = path
                path = await asyncio.to_thread(
                    _shrink_to_fit_sync, path, float(vdur or (end - start)), SAFE_LIMIT_MB
                )
                shrunk = path != path_before
            size_mb = os.path.getsize(path) / (1024 * 1024)
            if size_mb > TELEGRAM_LIMIT_MB:
                await B.safe_edit(status, ct("too_large", lang, max=TELEGRAM_LIMIT_MB))
                return

            if not applied and not shrunk:
                path = await asyncio.to_thread(B._faststart_remux_sync, path)

            vw, vh, vdur = await asyncio.to_thread(B._probe_video_meta_sync, path)
            extra = {}
            if vw and vh:
                extra.update(width=vw, height=vh)
            if vdur:
                extra["duration"] = vdur
            thumb = await asyncio.to_thread(B._make_thumbnail_sync, path)
            if thumb:
                extra["thumbnail"] = FSInputFile(thumb)

            await B.safe_edit(status, ct("uploading", lang))
            await bot.send_video(
                chat_id=message.chat.id,
                video=FSInputFile(path),
                caption=_build_clip_caption(info.get("title", ""), start, end, face_note),
                supports_streaming=True,
                **extra,
            )
            await status.delete()

        except ClipError as ce:
            await B.safe_edit(status, ct(ce.key, lang, **ce.fmt))
        except yt_dlp.utils.DownloadError as e:
            logger.warning(f"/clip download xato: {e}")
            await _notify_owner_error(bot, "yuklash", e)
            err = str(e).lower()
            if any(p in err for p in ("sign in", "not a bot", "login", "cookies")):
                await B.safe_edit(status, ct("youtube_blocked", lang))
            else:
                await B.safe_edit(status, ct("download_failed", lang))
        except Exception as e:
            import traceback
            logger.error(f"/clip kutilmagan xato: {type(e).__name__}: {e!r}")
            logger.error(traceback.format_exc())
            await _notify_owner_error(bot, "kutilmagan", e)
            await B.safe_edit(status, ct("unexpected_error", lang))


async def _ensure_subscribed(bot: Bot, message: Message, user_id: int) -> bool:
    """Majburiy obuna — bot.py'dagi handle_link bilan bir xil qoida."""
    B = _bot_mod()
    lang = _lang(user_id)
    if await B.is_subscribed(bot, user_id):
        return True
    await message.answer(B.t("subscribe", lang), reply_markup=B.subscribe_keyboard(lang))
    return False


# ------------------------------------------------------------------ handlerlar

@clip_router.message(Command("clip"))
async def cmd_clip(message: Message, command: CommandObject, state: FSMContext, bot: Bot):
    """/clip [havola] [vaqt] — argumentlar bo'lsa, ularni ishlatadi."""
    uid = message.from_user.id
    await state.clear()
    _bot_mod().track_user(uid, message.from_user.username)
    if not await _ensure_subscribed(bot, message, uid):
        return
    lang = _lang(uid)
    args = command.args or ""
    fmt, args = split_format_flag(args)
    m = _YT_URL_RE.search(args)
    if m:
        url = m.group(0)
        if len(_find_time_tokens(args)) >= 2:
            rng = parse_time_range(args)
            if rng is None:
                await message.answer(ct("bad_range", lang))
                return
            await _dispatch(message, bot, uid, url, rng, fmt)
            return
        await state.update_data(url=url, fmt=fmt)
        await state.set_state(ClipStates.waiting_range)
        await message.answer(ct("ask_range", lang, max=fmt_time(CLIP_MAX_SECONDS)))
        return
    await state.set_state(ClipStates.waiting_link)
    await message.answer(ct("ask_link", lang))


@clip_router.callback_query(F.data == "mode_clip")
async def cb_clip(callback: CallbackQuery, state: FSMContext, bot: Bot):
    # MUHIM: foydalanuvchi — callback.from_user (callback.message — botning o'zi).
    await callback.answer()
    uid = callback.from_user.id
    await state.clear()
    if not await _ensure_subscribed(bot, callback.message, uid):
        return
    await state.set_state(ClipStates.waiting_link)
    await callback.message.answer(ct("ask_link", _lang(uid)))


@clip_router.message(
    F.text,
    lambda m: bool(m.text) and _YT_URL_RE.search(m.text) is not None and len(_find_time_tokens(m.text)) >= 2,
)
async def handle_link_with_range(message: Message, state: FSMContext, bot: Bot):
    """Bir xabarda havola + vaqt: 'https://youtu.be/XXXX 34:15-35:15'."""
    uid = message.from_user.id
    await state.clear()
    _bot_mod().track_user(uid, message.from_user.username)
    if not await _ensure_subscribed(bot, message, uid):
        return
    lang = _lang(uid)
    fmt, cleaned = split_format_flag(message.text)
    rng = parse_time_range(cleaned)
    if rng is None:
        await message.answer(ct("bad_range", lang))
        return
    url = _YT_URL_RE.search(message.text).group(0)
    await _dispatch(message, bot, uid, url, rng, fmt)


@clip_router.message(ClipStates.waiting_link, F.text, ~F.text.startswith("/"))
async def got_link(message: Message, state: FSMContext):
    uid = message.from_user.id
    lang = _lang(uid)
    m = _YT_URL_RE.search(message.text)
    if not m:
        await message.answer(ct("not_youtube", lang))
        return
    await state.update_data(url=m.group(0))
    await state.set_state(ClipStates.waiting_range)
    await message.answer(ct("ask_range", lang, max=fmt_time(CLIP_MAX_SECONDS)))


@clip_router.message(ClipStates.waiting_range, F.text, ~F.text.startswith("/"))
async def got_range(message: Message, state: FSMContext, bot: Bot):
    uid = message.from_user.id
    lang = _lang(uid)
    data = await state.get_data()
    url = data.get("url")
    if not url:
        await state.clear()
        await message.answer(ct("ask_link", lang))
        await state.set_state(ClipStates.waiting_link)
        return
    flag, cleaned = split_format_flag(message.text)
    fmt = flag or data.get("fmt")
    rng = parse_time_range(cleaned)
    if rng is None:
        await message.answer(ct("bad_range", lang))
        return
    if rng[1] - rng[0] > CLIP_MAX_SECONDS:
        await message.answer(ct("range_too_long", lang, max=fmt_time(CLIP_MAX_SECONDS)))
        return
    if fmt == "v" and rng[1] - rng[0] > CLIP_VERTICAL_MAX_SECONDS:
        await message.answer(ct("vertical_too_long", lang, max=fmt_time(CLIP_VERTICAL_MAX_SECONDS)))
        return
    await state.clear()
    await _dispatch(message, bot, uid, url, rng, fmt)


@clip_router.callback_query(F.data.startswith("clipfmt:"))
async def cb_clip_format(callback: CallbackQuery, bot: Bot):
    """«16:9» / «9:16» tugmasi bosilganda qirqishni boshlaydi."""
    uid = callback.from_user.id  # MUHIM: bosgan odam (callback.message — botning o'zi)
    lang = _lang(uid)
    try:
        _prefix, token, choice = callback.data.split(":", 2)
    except ValueError:
        await callback.answer()
        return
    item = _pending.get(token)
    if not item or item[0] != uid or choice not in ("h", "v"):
        await callback.answer(ct("format_expired", lang), show_alert=True)
        return
    _uid, url, start, end, _ts = item
    vertical = choice == "v"
    if vertical and end - start > CLIP_VERTICAL_MAX_SECONDS:
        # So'rov saqlanib qoladi — foydalanuvchi 16:9 ni bosishi mumkin.
        await callback.answer(
            ct("vertical_btn_too_long", lang, max=fmt_time(CLIP_VERTICAL_MAX_SECONDS)), show_alert=True
        )
        return
    _pending.pop(token, None)  # ikki marta bosilsa ikki marta ishlamasin
    await callback.answer()
    B = _bot_mod()
    await B.safe_edit(
        callback.message,
        ct("chosen_v" if vertical else "chosen_h", lang, a=fmt_time(start), b=fmt_time(end)),
    )
    await _run_clip(callback.message, bot, uid, url, start, end, vertical=vertical)
