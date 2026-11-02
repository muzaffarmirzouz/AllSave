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
from typing import Optional, Tuple

import yt_dlp
from aiogram import Router, F, Bot
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, FSInputFile

logger = logging.getLogger(__name__)

clip_router = Router(name="clip_mode")

# ---------------------------------------------------------------- sozlamalar

CLIP_MAX_SECONDS = int(os.getenv("CLIP_MAX_SECONDS", "300"))
CLIP_MAX_HEIGHT = (os.getenv("CLIP_MAX_HEIGHT", "720").strip() or "720")
CLIP_MAX_CONCURRENT = int(os.getenv("CLIP_MAX_CONCURRENT", "2"))
clip_semaphore = asyncio.Semaphore(CLIP_MAX_CONCURRENT)

TELEGRAM_LIMIT_MB = 50
# Telegram chegarasidan biroz pastroq — konteyner qo'shimcha hajmi uchun zaxira.
SAFE_LIMIT_MB = 48


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
            "Bitta qism eng ko'pi bilan {max} bo'lishi mumkin.\n(Bekor qilish: /cancel)"
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
    },
    "ru": {
        "ask_link": (
            "✂️ Вырежу нужный фрагмент из видео YouTube.\n\n"
            "Сначала пришлите ссылку на видео YouTube.\n(Отмена: /cancel)"
        ),
        "ask_range": (
            "⏱ Теперь напишите время начала и конца нужного фрагмента, например:\n\n"
            "34:15-35:15\n1:02:30-1:04:00\n\n"
            "Один фрагмент — не длиннее {max}.\n(Отмена: /cancel)"
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
    },
    "en": {
        "ask_link": (
            "✂️ I'll cut the part you need out of a YouTube video.\n\n"
            "First, send me the YouTube video link.\n(Cancel: /cancel)"
        ),
        "ask_range": (
            "⏱ Now write the start and end time of the part you want, e.g.:\n\n"
            "34:15-35:15\n1:02:30-1:04:00\n\n"
            "A single clip can be at most {max} long.\n(Cancel: /cancel)"
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

def _build_ydl_opts(tmp_dir: str, ranges_fn) -> dict:
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
        "format_sort": [f"res:{CLIP_MAX_HEIGHT}", "fps", "vcodec:h264"],
        "merge_output_format": "mp4",
        "download_ranges": ranges_fn,
        # Aniq qirqish (keyframe chegarasiga yopishib qolmasin) — qayta kodlaydi,
        # lekin faqat kichik bo'lakni, shuning uchun tez.
        "force_keyframes_at_cuts": True,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
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
    return opts


def _clip_download_sync(url: str, start: int, end: int, tmp_dir: str) -> dict:
    """Bloklaydigan: avval video ma'lumotini oladi (uzunlik, live emasligini
    tekshiradi), keyin FAQAT [start, end] oralig'ini yuklab oladi."""
    holder = {"start": start, "end": end}

    def ranges_fn(info_dict, ydl):
        return [{"start_time": holder["start"], "end_time": holder["end"]}]

    with yt_dlp.YoutubeDL(_build_ydl_opts(tmp_dir, ranges_fn)) as ydl:
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
        ydl.process_ie_result(info, download=True)

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


async def _download_with_retry(url: str, start: int, end: int, tmp_dir: str, attempts: int = 2) -> dict:
    last = None
    for attempt in range(1, attempts + 1):
        try:
            return await asyncio.to_thread(_clip_download_sync, url, start, end, tmp_dir)
        except ClipError:
            raise
        except yt_dlp.utils.DownloadError as e:
            last = e
            err = str(e).lower()
            permanent = any(p in err for p in ("private", "unavailable", "removed", "not available", "sign in", "not a bot"))
            if attempt < attempts and not permanent:
                logger.warning(f"/clip yuklashda xato (urinish {attempt}/{attempts}), qayta urinilmoqda: {e}")
                await asyncio.sleep(4 * attempt)
                continue
            raise
    raise last


def _shrink_to_fit_sync(path: str, duration_s: float, limit_mb: int) -> str:
    """Fayl limitdan katta bo'lsa — maqsadli bitrate bilan qayta kodlab
    kichraytiradi. Sig'dira olmasa (juda uzun/bitrate juda past) ASL faylni
    qaytaradi, chaqiruvchi "too_large" deb xabar beradi."""
    out = path.rsplit(".", 1)[0] + "_small.mp4"
    audio_kbps = 96
    total_kbit = limit_mb * 1024 * 8 * 0.92  # konteyner uchun ~8% zaxira
    video_kbps = int(total_kbit / max(duration_s, 1.0) - audio_kbps)
    if video_kbps < 150:
        return path
    cmd = ["ffmpeg", "-y", "-i", path]
    if video_kbps < 700:
        cmd += ["-vf", "scale=-2:'min(480,ih)'"]
    cmd += [
        "-c:v", "libx264", "-preset", "veryfast",
        "-b:v", f"{video_kbps}k", "-maxrate", f"{int(video_kbps * 1.3)}k",
        "-bufsize", f"{video_kbps * 2}k", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", f"{audio_kbps}k",
        "-movflags", "+faststart", out,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=900)
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


def _build_clip_caption(title: str, start: int, end: int) -> str:
    B = _bot_mod()
    footer = f"\n\n✂️ {B.BOT_USERNAME_TAG} orqali qirqib olindi"
    head = f"⏱ {fmt_time(start)} – {fmt_time(end)}"
    title = (title or "").strip()
    room = 1024 - len(footer) - len(head) - 6
    if title and room > 10:
        if len(title) > room:
            title = title[:room].rstrip() + "..."
        head = f"{title}\n{head}"
    return (head + footer)[:1024]


# ------------------------------------------------------------- asosiy jarayon

async def _run_clip(message: Message, bot: Bot, user_id: int, url: str, start: int, end: int):
    B = _bot_mod()
    lang = _lang(user_id)
    status = await message.answer(ct("downloading", lang))
    max_len = fmt_time(CLIP_MAX_SECONDS)

    if end - start > CLIP_MAX_SECONDS:
        await B.safe_edit(status, ct("range_too_long", lang, max=max_len))
        return

    with tempfile.TemporaryDirectory() as tmp_dir:
        try:
            was_queued = clip_semaphore.locked()
            if was_queued:
                await B.safe_edit(status, ct("queued", lang))
            async with clip_semaphore:
                if was_queued:
                    await B.safe_edit(status, ct("downloading", lang))
                info = await _download_with_retry(url, start, end, tmp_dir)

            path = info["path"]
            start, end = info["start"], info["end"]

            # --- Logo / Outro: bot.py'dagi doimiy sozlamalar bilan bir xil ---
            applied = False
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
                caption=_build_clip_caption(info.get("title", ""), start, end),
                supports_streaming=True,
                **extra,
            )
            await status.delete()

        except ClipError as ce:
            await B.safe_edit(status, ct(ce.key, lang, **ce.fmt))
        except yt_dlp.utils.DownloadError as e:
            logger.warning(f"/clip download xato: {e}")
            err = str(e).lower()
            if any(p in err for p in ("sign in", "not a bot", "login", "cookies")):
                await B.safe_edit(status, ct("youtube_blocked", lang))
            else:
                await B.safe_edit(status, ct("download_failed", lang))
        except Exception as e:
            import traceback
            logger.error(f"/clip kutilmagan xato: {type(e).__name__}: {e!r}")
            logger.error(traceback.format_exc())
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
    m = _YT_URL_RE.search(args)
    if m:
        url = m.group(0)
        if len(_find_time_tokens(args)) >= 2:
            rng = parse_time_range(args)
            if rng is None:
                await message.answer(ct("bad_range", lang))
                return
            await _run_clip(message, bot, uid, url, *rng)
            return
        await state.update_data(url=url)
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
    rng = parse_time_range(message.text)
    if rng is None:
        await message.answer(ct("bad_range", lang))
        return
    url = _YT_URL_RE.search(message.text).group(0)
    await _run_clip(message, bot, uid, url, *rng)


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
    rng = parse_time_range(message.text)
    if rng is None:
        await message.answer(ct("bad_range", lang))
        return
    if rng[1] - rng[0] > CLIP_MAX_SECONDS:
        await message.answer(ct("range_too_long", lang, max=fmt_time(CLIP_MAX_SECONDS)))
        return
    await state.clear()
    await _run_clip(message, bot, uid, url, *rng)
