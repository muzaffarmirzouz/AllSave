# -*- coding: utf-8 -*-
"""
🎵 Musiqa bo'limi — AllSave botga qo'shimcha modul.

Nima qiladi:
  1) QIDIRUV: foydalanuvchi oddiy matn yozsa (qo'shiq yoki ijrochi nomi,
     masalan "eminem"), bot YouTube'dan 10 ta natija topib, raqamli
     tugmalar (1..10) bilan ro'yxat ko'rsatadi. Raqam bosilsa — o'sha
     qo'shiq MP3 qilib yuboriladi.
  2) ANIQLASH (Shazam): foydalanuvchi video, audio, ovozli xabar yoki
     dumaloq video yuborsa — undagi musiqa aniqlanadi (nomi + ijrochisi),
     so'ng darhol shu qo'shiq bo'yicha qidiruv natijalari chiqariladi.
  3) Havola orqali yuklab olingan har bir video ostida
     "🎵 Musiqasini topish" tugmasi bo'ladi — bosilsa o'sha videodagi
     musiqa aniqlanadi.

Bir marta yuklangan qo'shiqning Telegram file_id'si bazada saqlanadi —
keyingi safar xuddi shu qo'shiq so'ralsa, qayta yuklamasdan DARHOL
yuboriladi.

Botga ulash (bot.py main()):
  dp.include_router(music_router) — `router`dan KEYIN (eng oxirida), shunda
  u faqat boshqa hech bir bo'lim ushlamagan oddiy matn/video/audio'ni oladi.

Sozlamalar (Railway Variables, hammasi ixtiyoriy):
  MUSIC_MAX_SECONDS     — eng uzun qo'shiq, soniya (standart 900 = 15 daqiqa)
  MUSIC_MAX_CONCURRENT  — bir vaqtda nechta MP3 yuklash (standart 3)
  MUSIC_MP3_KBPS        — MP3 sifati (standart 128)
YouTube cookies/proxy/PO-token sozlamalari bot.py'dagi bilan BIR XIL.

Musiqa aniqlash uchun `shazamio` kutubxonasi kerak (requirements.txt'da).
U o'rnatilmagan/ishlamay qolsa — qidiruv baribir ishlayveradi, faqat
aniqlash "hozircha ishlamayapti" deb javob beradi.
"""

import asyncio
import glob
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
import uuid
from collections import OrderedDict
from typing import Optional

import yt_dlp
from aiogram import Router, F, Bot
from aiogram.enums import ChatAction
from aiogram.filters import Command
from aiogram.types import (
    Message, CallbackQuery, FSInputFile, InlineKeyboardMarkup, InlineKeyboardButton,
)

from bot_ref import bot_mod

logger = logging.getLogger(__name__)

music_router = Router(name="music_mode")

# ---------------------------------------------------------------- sozlamalar

MUSIC_RESULTS = 10
MUSIC_MAX_SECONDS = int(os.getenv("MUSIC_MAX_SECONDS", "900"))
MUSIC_MAX_CONCURRENT = int(os.getenv("MUSIC_MAX_CONCURRENT", "3"))
MUSIC_MP3_KBPS = (os.getenv("MUSIC_MP3_KBPS", "128").strip() or "128")
music_semaphore = asyncio.Semaphore(MUSIC_MAX_CONCURRENT)
# MP3'ni Telegram'ga yuborish uchun vaqt chegarasi (soniya). aiogram'ning
# standarti 60 soniya — Telegram audio faylni qabul qilib, ichidagi
# ma'lumotni qayta ishlashi ba'zan undan uzoq davom etadi va so'rov
# "Request timeout error" bilan uzilib, qo'shiq yetib bormay qolardi.
MUSIC_UPLOAD_TIMEOUT = int(os.getenv("MUSIC_UPLOAD_TIMEOUT", "300"))

# Musiqa aniqlash
REC_MAX_MB = 20                 # Telegram botga 20 MB dan kattasini bermaydi
REC_SEGMENT_SECONDS = 15        # Shazam'ga yuboriladigan bo'lak uzunligi
REC_MAX_CONCURRENT = int(os.getenv("MUSIC_REC_MAX_CONCURRENT", "3"))
rec_semaphore = asyncio.Semaphore(REC_MAX_CONCURRENT)

SEND_LIMIT_MB = 50
QUERY_MIN_LEN = 2
QUERY_MAX_LEN = 100

# Qidiruv natijalari xotirada saqlanadi (tugma bosilganda kerak). Eng ko'pi
# _SEARCH_CACHE_MAX ta, har biri _SEARCH_TTL soniya amal qiladi.
_SEARCH_CACHE_MAX = 3000
_SEARCH_TTL = 6 * 3600
_searches: "OrderedDict[str, dict]" = OrderedDict()

_shazam = None


# --------------------------------------------------------------- ko'p tillilik

MUSIC_TEXTS = {
    "uz": {
        "help": (
            "🎵 <b>Musiqa qidirish</b>\n\n"
            "• Qo'shiq yoki ijrochi nomini yozing (masalan: <i>eminem</i>) — "
            "10 ta natija chiqaraman, raqamini bossangiz MP3 qilib yuboraman.\n\n"
            "• Video, audio, ovozli xabar yoki dumaloq video yuboring — "
            "undagi musiqani aniqlab beraman.\n\n"
            "• Havola orqali yuklangan video ostidagi «🎵 Musiqasini topish» "
            "tugmasini bossangiz ham — o'sha videodagi qo'shiqni topaman."
        ),
        "searching": "🔎 Qidirilmoqda...",
        "results": "🔎 «{q}» bo'yicha natijalar:",
        "nothing": "😕 «{q}» bo'yicha hech narsa topilmadi. Boshqacha yozib ko'ring.",
        "search_error": "❌ Qidiruvda xatolik yuz berdi, birozdan keyin qayta urinib ko'ring.",
        "expired": "Natijalar eskirgan — qayta qidiring.",
        "downloading": "⏳ «{title}» yuklanmoqda...",
        "queued": "⏳ Navbatdasiz, biroz kuting...",
        "too_long": "❌ Bu juda uzun ({dur}). {max} gacha bo'lgan qo'shiqlarni yuklayman.",
        "dl_error": "❌ Qo'shiqni yuklab bo'lmadi. Boshqa natijani tanlab ko'ring.",
        "too_big": "❌ Fayl Telegram chegarasidan ({max} MB) katta chiqdi.",
        "audio_caption": "🎵 {bot} orqali yuklab olindi",
        "recognizing": "🎧 Musiqa aniqlanmoqda...",
        "rec_found": "🎧 Topildi: {artist} — {title}",
        "rec_not_found": "😕 Bu videoda/audioda musiqa aniqlanmadi. Qo'shiq aniq eshitiladigan qismini yuboring.",
        "rec_unavailable": "❌ Musiqa aniqlash hozircha ishlamayapti. Qo'shiq nomini yozib qidirib ko'ring.",
        "rec_too_big": "❌ Fayl juda katta. 20 MB dan kichik video/audio yuboring.",
        "rec_error": "❌ Faylni o'qib bo'lmadi, boshqasini yuborib ko'ring.",
        "btn_find_music": "🎵 Musiqasini topish",
        "btn_close": "❌",
    },
    "ru": {
        "help": (
            "🎵 <b>Поиск музыки</b>\n\n"
            "• Напишите название песни или исполнителя (например: <i>eminem</i>) — "
            "покажу 10 результатов, нажмите номер — пришлю MP3.\n\n"
            "• Отправьте видео, аудио, голосовое или кружок — "
            "определю, какая музыка в нём играет.\n\n"
            "• Под видео, скачанным по ссылке, есть кнопка «🎵 Найти музыку» — "
            "она найдёт песню из этого видео."
        ),
        "searching": "🔎 Ищу...",
        "results": "🔎 Результаты по запросу «{q}»:",
        "nothing": "😕 По запросу «{q}» ничего не найдено. Попробуйте написать иначе.",
        "search_error": "❌ Ошибка при поиске, попробуйте чуть позже.",
        "expired": "Результаты устарели — повторите поиск.",
        "downloading": "⏳ Загружаю «{title}»...",
        "queued": "⏳ Вы в очереди, подождите немного...",
        "too_long": "❌ Слишком длинный трек ({dur}). Загружаю треки до {max}.",
        "dl_error": "❌ Не удалось скачать трек. Попробуйте другой результат.",
        "too_big": "❌ Файл больше лимита Telegram ({max} МБ).",
        "audio_caption": "🎵 Скачано через {bot}",
        "recognizing": "🎧 Распознаю музыку...",
        "rec_found": "🎧 Найдено: {artist} — {title}",
        "rec_not_found": "😕 Музыку распознать не удалось. Отправьте фрагмент, где песня хорошо слышна.",
        "rec_unavailable": "❌ Распознавание музыки сейчас не работает. Попробуйте найти песню по названию.",
        "rec_too_big": "❌ Файл слишком большой. Отправьте видео/аудио меньше 20 МБ.",
        "rec_error": "❌ Не удалось прочитать файл, попробуйте другой.",
        "btn_find_music": "🎵 Найти музыку",
        "btn_close": "❌",
    },
    "en": {
        "help": (
            "🎵 <b>Music search</b>\n\n"
            "• Type a song or artist name (e.g. <i>eminem</i>) — I'll show 10 "
            "results; tap a number and I'll send it as MP3.\n\n"
            "• Send a video, audio, voice message or video note — "
            "I'll recognize the music playing in it.\n\n"
            "• Videos downloaded from a link have a «🎵 Find the music» button — "
            "it finds the song from that video."
        ),
        "searching": "🔎 Searching...",
        "results": "🔎 Results for «{q}»:",
        "nothing": "😕 Nothing found for «{q}». Try different words.",
        "search_error": "❌ Search failed, please try again in a moment.",
        "expired": "These results expired — please search again.",
        "downloading": "⏳ Downloading «{title}»...",
        "queued": "⏳ You're in the queue, please wait...",
        "too_long": "❌ That's too long ({dur}). I download tracks up to {max}.",
        "dl_error": "❌ Couldn't download that track. Try another result.",
        "too_big": "❌ The file is larger than Telegram's {max} MB limit.",
        "audio_caption": "🎵 Downloaded via {bot}",
        "recognizing": "🎧 Recognizing the music...",
        "rec_found": "🎧 Found: {artist} — {title}",
        "rec_not_found": "😕 Couldn't recognize any music. Send a part where the song is clearly audible.",
        "rec_unavailable": "❌ Music recognition isn't working right now. Try searching by song name.",
        "rec_too_big": "❌ The file is too large. Send a video/audio under 20 MB.",
        "rec_error": "❌ Couldn't read the file, try another one.",
        "btn_find_music": "🎵 Find the music",
        "btn_close": "❌",
    },
}


def mt(key: str, lang: str, **kwargs) -> str:
    lang = lang if lang in MUSIC_TEXTS else "uz"
    text = MUSIC_TEXTS[lang].get(key) or MUSIC_TEXTS["uz"].get(key, "")
    return text.format(**kwargs) if kwargs else text


def _lang(user_id: int) -> str:
    try:
        return bot_mod().get_user_lang(user_id) or "uz"
    except Exception:
        return "uz"


def find_music_keyboard(lang: str) -> InlineKeyboardMarkup:
    """Havola orqali yuklangan video ostiga qo'yiladigan tugma (bot.py
    handle_link'dan chaqiriladi)."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=mt("btn_find_music", lang), callback_data="mrec"),
    ]])


def fmt_dur(sec) -> str:
    try:
        sec = int(sec or 0)
    except (TypeError, ValueError):
        return "?"
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


async def _edit(msg: Message, text: str, **kwargs) -> None:
    try:
        await msg.edit_text(text, **kwargs)
    except Exception as e:
        if "message is not modified" not in str(e):
            logger.warning(f"music: xabarni yangilab bo'lmadi: {e}")


async def _ensure_subscribed(bot: Bot, message: Message, user) -> bool:
    """Majburiy obuna + foydalanuvchini ro'yxatga olish (bot.py bilan bir xil)."""
    B = bot_mod()
    try:
        B.track_user(user.id, user.username)
        if await B.is_subscribed(bot, user.id):
            return True
        lang = _lang(user.id)
        await message.answer(B.t("subscribe", lang), reply_markup=B.subscribe_keyboard(lang))
        return False
    except Exception as e:
        logger.warning(f"music: obuna tekshiruvida xato (o'tkazib yuborildi): {e}")
        return True


# ------------------------------------------------------- MP3 file_id keshi

def _cache_conn():
    conn = sqlite3.connect(bot_mod().DB_PATH)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS music_cache ("
        " video_id TEXT PRIMARY KEY, file_id TEXT NOT NULL, created TEXT)"
    )
    return conn


def _cache_get(video_id: str) -> Optional[str]:
    try:
        conn = _cache_conn()
        row = conn.execute("SELECT file_id FROM music_cache WHERE video_id = ?", (video_id,)).fetchone()
        conn.close()
        return row[0] if row else None
    except Exception as e:
        logger.warning(f"music_cache o'qishda xato: {e}")
        return None


def _cache_put(video_id: str, file_id: str) -> None:
    try:
        conn = _cache_conn()
        conn.execute(
            "INSERT OR REPLACE INTO music_cache (video_id, file_id, created) VALUES (?, ?, datetime('now'))",
            (video_id, file_id),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f"music_cache yozishda xato: {e}")


def _cache_del(video_id: str) -> None:
    try:
        conn = _cache_conn()
        conn.execute("DELETE FROM music_cache WHERE video_id = ?", (video_id,))
        conn.commit()
        conn.close()
    except Exception:
        pass


# --------------------------------------------------------------- YouTube

def _yt_base_opts() -> dict:
    """bot.py'dagi YouTube sozlamalari (cookies, proxy, JS runtime, PO token)."""
    B = bot_mod()
    opts = {"quiet": True, "no_warnings": True, "noplaylist": True}
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
    return opts


def _search_sync(query: str, limit: int = MUSIC_RESULTS) -> list:
    """YouTube'dan qidiradi (tez — videolarni ochmasdan, "flat" ro'yxat).
    Jonli efirlar, kanallar/pleylistlar va juda uzun videolar tashlab
    yuboriladi."""
    opts = _yt_base_opts()
    opts.update({"extract_flat": "in_playlist", "skip_download": True})
    # js_runtimes/remote_components qidiruv uchun shart emas — keraksiz
    # tarmoq so'rovini oldini olamiz.
    opts.pop("remote_components", None)
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch{limit + 6}:{query}", download=False)
    items = []
    for e in (info or {}).get("entries") or []:
        vid = e.get("id") or ""
        if len(vid) != 11:  # kanal/pleylist natijalari
            continue
        if e.get("live_status") in ("is_live", "is_upcoming"):
            continue
        dur = e.get("duration")
        if dur and dur > MUSIC_MAX_SECONDS:
            continue
        items.append({
            "id": vid,
            "title": (e.get("title") or "").strip() or vid,
            "duration": int(dur) if dur else None,
            "uploader": (e.get("channel") or e.get("uploader") or "").strip(),
        })
        if len(items) >= limit:
            break
    return items


# Sarlavhadagi "(Official Music Video)", "[HD]", "| Lyrics" kabi qo'shimchalar.
_NOISE_RE = re.compile(
    r"\s*[\(\[\{][^\)\]\}]*(official|video|audio|lyric|lyrics|clip|klip|hd|hq|4k|mv|visualizer|music)[^\)\]\}]*[\)\]\}]",
    re.IGNORECASE,
)


def _split_artist_title(raw_title: str, uploader: str) -> tuple:
    """'Eminem - Lose Yourself (Official Video)' -> ('Eminem', 'Lose Yourself')."""
    title = _NOISE_RE.sub("", raw_title or "").strip()
    title = re.sub(r"\s*\|.*$", "", title).strip() or (raw_title or "").strip()
    for sep in (" - ", " – ", " — "):
        if sep in title:
            artist, song = title.split(sep, 1)
            if artist.strip() and song.strip():
                return artist.strip()[:64], song.strip()[:64]
    artist = re.sub(r"\s*(- Topic|VEVO|Official)$", "", uploader or "", flags=re.IGNORECASE).strip()
    return (artist[:64] or None), title[:64]


def _make_audio_thumb_sync(tmp_dir: str) -> Optional[str]:
    """yt-dlp yozgan muqovani Telegram talabiga (JPEG, <=320px, <200KB) moslaydi."""
    srcs = [p for p in glob.glob(os.path.join(tmp_dir, "*"))
            if p.lower().endswith((".webp", ".jpg", ".jpeg", ".png"))]
    if not srcs:
        return None
    out = os.path.join(tmp_dir, "cover_tg.jpg")
    for q in ("4", "8", "14"):
        r = subprocess.run(
            ["ffmpeg", "-y", "-i", srcs[0], "-frames:v", "1",
             "-vf", "scale=320:320:force_original_aspect_ratio=decrease", "-q:v", q, out],
            capture_output=True, timeout=30,
        )
        if r.returncode == 0 and os.path.exists(out) and os.path.getsize(out) < 190_000:
            return out
    return None


def _download_mp3_sync(video_id: str, tmp_dir: str) -> dict:
    opts = _yt_base_opts()
    opts.update({
        "outtmpl": os.path.join(tmp_dir, "%(id)s.%(ext)s"),
        "format": "bestaudio[ext=m4a]/bestaudio/best",
        "writethumbnail": True,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": MUSIC_MP3_KBPS,
        }],
        "match_filter": yt_dlp.utils.match_filter_func(f"duration <=? {MUSIC_MAX_SECONDS} & !is_live"),
    })
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=True)
    mp3s = glob.glob(os.path.join(tmp_dir, "*.mp3"))
    if not mp3s:
        raise RuntimeError("mp3 fayl hosil bo'lmadi (video juda uzun yoki jonli efir bo'lishi mumkin)")
    artist, title = _split_artist_title(info.get("title") or "", info.get("channel") or info.get("uploader") or "")
    return {
        "path": mp3s[0],
        "title": title,
        "artist": artist,
        "duration": int(info.get("duration") or 0) or None,
        "thumb": _make_audio_thumb_sync(tmp_dir),
    }


# ----------------------------------------------------------- qidiruv natijasi

def _store_search(query: str, items: list) -> str:
    now = time.time()
    # Eskirganlarini va ortiqchasini tozalaymiz.
    while _searches:
        k, v = next(iter(_searches.items()))
        if now - v["ts"] > _SEARCH_TTL or len(_searches) >= _SEARCH_CACHE_MAX:
            _searches.pop(k, None)
        else:
            break
    sid = uuid.uuid4().hex[:10]
    _searches[sid] = {"q": query, "items": items, "ts": now}
    return sid


def _results_text(header: str, items: list) -> str:
    lines = [header, ""]
    for i, it in enumerate(items, 1):
        dur = f" ({fmt_dur(it['duration'])})" if it.get("duration") else ""
        lines.append(f"{i}. {it['title']}{dur}")
    return "\n".join(lines)[:4000]


def _results_keyboard(sid: str, n: int, lang: str) -> InlineKeyboardMarkup:
    rows, row = [], []
    for i in range(n):
        row.append(InlineKeyboardButton(text=str(i + 1), callback_data=f"msel:{sid}:{i}"))
        if len(row) == 5:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text=mt("btn_close", lang), callback_data="mclose")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _search_and_show(message: Message, query: str, lang: str, header: Optional[str] = None,
                           status: Optional[Message] = None) -> None:
    """Qidiradi va natijalarni (raqamli tugmalar bilan) ko'rsatadi. status
    berilsa — o'sha xabar yangilanadi, aks holda yangisi yuboriladi."""
    if status is None:
        status = await message.answer(mt("searching", lang))
    try:
        items = await asyncio.to_thread(_search_sync, query)
    except Exception as e:
        logger.warning(f"music: qidiruvda xato ({query!r}): {e}")
        await _edit(status, mt("search_error", lang))
        return
    if not items:
        prefix = f"{header}\n\n" if header else ""
        await _edit(status, prefix + mt("nothing", lang, q=query))
        return
    sid = _store_search(query, items)
    head = mt("results", lang, q=query)
    if header:
        head = f"{header}\n\n{head}"
    await _edit(status, _results_text(head, items), reply_markup=_results_keyboard(sid, len(items), lang))


# ------------------------------------------------------------- Shazam aniqlash

def _get_shazam():
    global _shazam
    if _shazam is None:
        from shazamio import Shazam  # lazy — o'rnatilmagan bo'lsa ImportError
        _shazam = Shazam()
    return _shazam


def _probe_duration_sync(path: str) -> float:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=20,
        )
        return float(r.stdout.strip())
    except Exception:
        return 0.0


def _extract_segment_sync(src: str, out: str, start: float, length: float) -> bool:
    r = subprocess.run(
        ["ffmpeg", "-y", "-ss", f"{start:.2f}", "-i", src, "-t", f"{length:.2f}",
         "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", out],
        capture_output=True, timeout=60,
    )
    return r.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 4000


def _segment_starts(duration: float) -> list:
    """Qaysi joylardan bo'lak olib Shazam'ga yuborish: avval o'rtaroqdan
    (intro/gaplar ko'pincha boshida bo'ladi), topilmasa boshidan va oxiriroqdan."""
    L = REC_SEGMENT_SECONDS
    if duration <= L + 2:
        return [0.0]
    starts = [max(0.0, min(duration * 0.3, duration - L)), 0.0]
    if duration > 3 * L:
        starts.append(max(0.0, duration * 0.65))
    out = []
    for s in starts:
        if all(abs(s - o) > L / 2 for o in out):
            out.append(s)
    return out


async def _recognize_file(src_path: str, tmp_dir: str) -> Optional[dict]:
    """Fayldagi musiqani aniqlaydi. Topilsa {"title","artist"}, aks holda None.
    shazamio ishlamasa RuntimeError("unavailable")."""
    try:
        shazam = _get_shazam()
    except Exception as e:
        logger.error(f"music: shazamio yuklanmadi: {e}")
        raise RuntimeError("unavailable")

    duration = await asyncio.to_thread(_probe_duration_sync, src_path)
    for i, start in enumerate(_segment_starts(duration or 0.0)):
        seg = os.path.join(tmp_dir, f"seg{i}.wav")
        ok = await asyncio.to_thread(_extract_segment_sync, src_path, seg, start, REC_SEGMENT_SECONDS)
        if not ok:
            if i == 0:
                raise ValueError("audio ajratib bo'lmadi")
            continue
        try:
            fn = getattr(shazam, "recognize", None) or getattr(shazam, "recognize_song")
            out = await asyncio.wait_for(fn(seg), timeout=40)
        except asyncio.TimeoutError:
            logger.warning("music: Shazam javobi kechikdi")
            continue
        except Exception as e:
            logger.warning(f"music: Shazam xatosi: {e}")
            continue
        track = (out or {}).get("track") or {}
        title = (track.get("title") or "").strip()
        if title:
            return {"title": title, "artist": (track.get("subtitle") or "").strip()}
    return None


async def _recognize_and_reply(message: Message, bot: Bot, file_id: str, file_size: Optional[int],
                               lang: str) -> None:
    if file_size and file_size > REC_MAX_MB * 1024 * 1024:
        await message.answer(mt("rec_too_big", lang))
        return
    status = await message.answer(mt("recognizing", lang))
    tmp_dir = tempfile.mkdtemp(prefix="mrec_")
    try:
        async with rec_semaphore:
            src = os.path.join(tmp_dir, "input")
            tg_file = await bot.get_file(file_id)
            await bot.download_file(tg_file.file_path, destination=src)
            try:
                found = await _recognize_file(src, tmp_dir)
            except RuntimeError:
                await _edit(status, mt("rec_unavailable", lang))
                return
            except ValueError:
                await _edit(status, mt("rec_error", lang))
                return
        if not found:
            await _edit(status, mt("rec_not_found", lang))
            return
        artist, title = found["artist"], found["title"]
        header = mt("rec_found", lang, artist=artist or "?", title=title)
        query = f"{artist} {title}".strip()
        await _search_and_show(message, query, lang, header=header, status=status)
    except Exception as e:
        logger.exception(f"music: aniqlashda kutilmagan xato: {e}")
        msg = mt("rec_too_big", lang) if "too big" in str(e).lower() else mt("rec_error", lang)
        await _edit(status, msg)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ------------------------------------------------------------------ handlerlar

@music_router.message(Command("music"))
async def cmd_music(message: Message, bot: Bot):
    if not await _ensure_subscribed(bot, message, message.from_user):
        return
    await message.answer(mt("help", _lang(message.from_user.id)), parse_mode="HTML")


@music_router.callback_query(F.data == "mode_music")
async def cb_mode_music(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    if not await _ensure_subscribed(bot, callback.message, callback.from_user):
        return
    await callback.message.answer(mt("help", _lang(callback.from_user.id)), parse_mode="HTML")


_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)


def _is_search_query(m: Message) -> bool:
    text = (m.text or "").strip()
    return (
        QUERY_MIN_LEN <= len(text) <= QUERY_MAX_LEN
        and not text.startswith("/")
        and _URL_RE.search(text) is None
    )


@music_router.message(F.chat.type == "private", F.text, _is_search_query)
async def handle_search_text(message: Message, bot: Bot):
    """Oddiy matn (qo'shiq/ijrochi nomi) — boshqa hech bir bo'lim
    ushlamagan matnlar shu yerga keladi."""
    if not await _ensure_subscribed(bot, message, message.from_user):
        return
    lang = _lang(message.from_user.id)
    query = " ".join(message.text.split())
    await _search_and_show(message, query, lang)


@music_router.callback_query(F.data == "mclose")
async def cb_close(callback: CallbackQuery):
    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        pass


@music_router.callback_query(F.data.startswith("msel:"))
async def cb_select(callback: CallbackQuery, bot: Bot):
    user = callback.from_user
    lang = _lang(user.id)
    try:
        _, sid, idx = callback.data.split(":", 2)
        item = _searches[sid]["items"][int(idx)]
    except (ValueError, KeyError, IndexError):
        await callback.answer(mt("expired", lang), show_alert=True)
        return
    await callback.answer()
    if not await _ensure_subscribed(bot, callback.message, user):
        return

    chat_id = callback.message.chat.id
    B = bot_mod()
    bot_tag = getattr(B, "BOT_USERNAME_TAG", "@AllSaveUz_Bot")
    caption = mt("audio_caption", lang, bot=bot_tag)

    if item.get("duration") and item["duration"] > MUSIC_MAX_SECONDS:
        await callback.message.answer(
            mt("too_long", lang, dur=fmt_dur(item["duration"]), max=fmt_dur(MUSIC_MAX_SECONDS))
        )
        return

    # 1) Avval yuklangan bo'lsa — keshdan darhol.
    cached = await asyncio.to_thread(_cache_get, item["id"])
    if cached:
        try:
            await bot.send_audio(chat_id, audio=cached, caption=caption, request_timeout=60)
            return
        except Exception as e:
            logger.info(f"music: keshdagi file_id ishlamadi, qayta yuklanadi: {e}")
            await asyncio.to_thread(_cache_del, item["id"])

    # 2) YouTube'dan MP3 qilib yuklaymiz.
    status = await callback.message.answer(mt("downloading", lang, title=item["title"][:80]))
    tmp_dir = tempfile.mkdtemp(prefix="music_")
    try:
        if music_semaphore.locked():
            await _edit(status, mt("queued", lang))
        async with music_semaphore:
            await _edit(status, mt("downloading", lang, title=item["title"][:80]))
            try:
                await bot.send_chat_action(chat_id, ChatAction.UPLOAD_VOICE)
            except Exception:
                pass
            t0 = time.monotonic()
            res = await asyncio.to_thread(_download_mp3_sync, item["id"], tmp_dir)
            t_dl = time.monotonic() - t0

        size_mb = os.path.getsize(res["path"]) / (1024 * 1024)
        if size_mb > SEND_LIMIT_MB:
            await _edit(status, mt("too_big", lang, max=SEND_LIMIT_MB))
            return

        kwargs = {"title": res["title"], "caption": caption}
        if res.get("artist"):
            kwargs["performer"] = res["artist"]
        if res.get("duration"):
            kwargs["duration"] = res["duration"]
        if res.get("thumb"):
            kwargs["thumbnail"] = FSInputFile(res["thumb"])
        fname = re.sub(r'[\\/:*?"<>|]+', "", f"{res.get('artist') or ''} - {res['title']}".strip(" -"))[:60] or "audio"
        try:
            await bot.send_chat_action(chat_id, ChatAction.UPLOAD_VOICE)
        except Exception:
            pass
        t1 = time.monotonic()
        sent = await bot.send_audio(
            chat_id, audio=FSInputFile(res["path"], filename=f"{fname}.mp3"),
            request_timeout=MUSIC_UPLOAD_TIMEOUT, **kwargs,
        )
        logger.info(
            f"music: {item['id']} yuborildi — {size_mb:.1f} MB, "
            f"yuklash {t_dl:.1f}s, Telegram'ga yuborish {time.monotonic() - t1:.1f}s"
        )
        if sent and sent.audio:
            await asyncio.to_thread(_cache_put, item["id"], sent.audio.file_id)
        try:
            await status.delete()
        except Exception:
            pass
    except Exception as e:
        logger.warning(f"music: yuklash/yuborishda xato ({item['id']}): {type(e).__name__}: {e}")
        await _edit(status, mt("dl_error", lang))
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@music_router.callback_query(F.data == "mrec")
async def cb_recognize_from_video(callback: CallbackQuery, bot: Bot):
    """Yuklab olingan video ostidagi '🎵 Musiqasini topish' tugmasi."""
    await callback.answer()
    lang = _lang(callback.from_user.id)
    msg = callback.message
    media = (msg.video or msg.audio or msg.document) if msg else None
    if not media:
        await msg.answer(mt("rec_error", lang))
        return
    await _recognize_and_reply(msg, bot, media.file_id, getattr(media, "file_size", None), lang)


@music_router.message(F.chat.type == "private", F.audio | F.voice | F.video_note | F.video)
async def handle_media_recognize(message: Message, bot: Bot):
    """Video/audio/ovozli/dumaloq video yuborilsa — undagi musiqani aniqlaydi.
    ESLATMA: bu router ENG OXIRIDA ulangan — Logo/Outro/Titr yoqilgan
    bo'lsa, videoni avval o'sha bo'limlar oladi (bu yerga kelmaydi)."""
    if not await _ensure_subscribed(bot, message, message.from_user):
        return
    lang = _lang(message.from_user.id)
    media = message.audio or message.voice or message.video_note or message.video
    await _recognize_and_reply(message, bot, media.file_id, media.file_size, lang)
