# -*- coding: utf-8 -*-
"""
"/caption" bo'limi — AllSave botga qo'shimcha modul.

Nima qiladi:
  1) /caption bosilsa rejim yoqiladi
  2) Foydalanuvchi video fayl YOKI istalgan qo'llab-quvvatlanadigan platforma
     (Instagram, TikTok va h.k.) havolasini yuboradi
  3) Havola bo'lsa — yt-dlp orqali yuklab olinadi. Instagram uchun bot.py'da
     allaqachon sozlangan IG_COOKIES_FILE va impersonatsiya (Chrome) qayta
     ishlatiladi — shuning uchun bu bot.py'dagi asosiy yuklab olish
     funksiyasi kabi ishonchli ishlaydi.
  4) ffmpeg bilan audio ajratiladi
  5) Maxsus o'zbek tiliga moslashtirilgan ASR modeli (OvozifyLabs/whisper-
     small-uz-v1, transformers kutubxonasi orqali) bilan transkript
     qilinadi — umumiy Whisper modellari o'zbek tilida juda kam ma'lumot
     bilan o'qitilgani uchun ishlatilmaydi
  6) So'z darajasidagi vaqt belgilaridan SRT yasaladi (har qator ~4-5
     so'zdan oshmaydi)
  7) ffmpeg (libass) bilan subtitr videoga "kuydiriladi" (hardsub)
  8) Tayyor video foydalanuvchiga qaytariladi

Botga ulash (bot.py):
  1) Bu faylni loyihaga "caption_mode.py" nomi bilan qo'shing (bot.py bilan
     bir papkada)
  2) bot.py boshiga (boshqa importlardan keyin) qo'shing:
         from caption_mode import caption_router
  3) main() funksiyasida, dp.include_router(router)dan OLDIN:
         dp.include_router(caption_router)
  4) set_my_commands ro'yxatiga qo'shing:
         BotCommand(command="caption", description="Videoga o'zbekcha titr qo'shish"),
  5) requirements.txt'ga qo'shing:
         transformers
         torch
     (yt-dlp, ffmpeg — allaqachon bor, chunki asosiy bot ham ulardan
     foydalanadi)
     ESLATMA: torch o'rnatilishi build vaqtini va konteyner hajmini
     sezilarli oshiradi (yuzlab MB).
"""

import asyncio
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Optional

from aiogram import Router, F, Bot
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, FSInputFile

logger = logging.getLogger(__name__)

caption_router = Router(name="caption_mode")

# ---------------------------------------------------------------- sozlamalar

MAX_VIDEO_SECONDS = int(os.getenv("CAPTION_MAX_SECONDS", "180"))  # 3 daqiqa
MAX_FILE_MB = 200

_asr_pipeline = None
_asr_lock = threading.Lock()

# Umumiy (generic) Whisper modellari o'zbek tilida juda kam ma'lumot bilan
# o'qitilgan — shuning uchun maxsus o'zbek tiliga moslashtirilgan model
# ishlatiladi (haqiqiy Telegram ovozli xabarlari ustida o'qitilgan).
ASR_MODEL_NAME = os.getenv("CAPTION_ASR_MODEL", "OvozifyLabs/whisper-small-uz-v1")


def get_asr_pipeline():
    """Maxsus o'zbek tiliga moslashtirilgan ASR pipeline'ni bitta marta
    yuklaydi (lazy singleton)."""
    global _asr_pipeline
    if _asr_pipeline is None:
        with _asr_lock:
            if _asr_pipeline is None:
                from transformers import pipeline as hf_pipeline

                logger.info(f"O'zbekcha ASR modeli ({ASR_MODEL_NAME}) yuklanmoqda...")
                _asr_pipeline = hf_pipeline(
                    "automatic-speech-recognition",
                    model=ASR_MODEL_NAME,
                    chunk_length_s=30,
                    stride_length_s=5,
                    device=-1,  # CPU
                )
                logger.info("O'zbekcha ASR modeli tayyor.")
    return _asr_pipeline


class CaptionStates(StatesGroup):
    waiting_input = State()


# ------------------------------------------------------------- /caption kirish

@caption_router.message(Command("caption"))
async def cmd_caption(message: Message, state: FSMContext):
    await state.set_state(CaptionStates.waiting_input)
    await message.answer(
        "🎬 <b>Titr qo'shish rejimi yoqildi.</b>\n\n"
        "Menga video fayl yuboring YOKI video havolasini (Instagram, TikTok va h.k.) tashlang.\n"
        "Agar video o'zbek tilida bo'lsa — unga avtomatik titr yozib qaytaraman.\n\n"
        "Bekor qilish uchun /cancel bosing.",
        parse_mode="HTML",
    )


@caption_router.callback_query(F.data == "mode_caption")
async def cb_caption_mode(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await cmd_caption(callback.message, state)


@caption_router.message(Command("cancel"), CaptionStates.waiting_input)
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Bekor qilindi.")


# ---------------------------------------------------------------- video kelsa

@caption_router.message(CaptionStates.waiting_input, F.video | F.document)
async def handle_video_input(message: Message, bot: Bot, state: FSMContext):
    media = message.video or message.document
    if media.file_size and media.file_size > MAX_FILE_MB * 1024 * 1024:
        await message.answer(f"Video juda katta ({MAX_FILE_MB}MB dan oshmasin).")
        return

    status = await message.answer("⏳ Video yuklab olinmoqda...")

    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        src_path = tmp / "input.mp4"
        tg_file = await bot.get_file(media.file_id)
        await bot.download_file(tg_file.file_path, destination=src_path)
        await process_and_reply(message, status, src_path, tmp)

    await state.clear()


# ---------------------------------------------------------------- link kelsa

@caption_router.message(CaptionStates.waiting_input, F.text.startswith("http"))
async def handle_link_input(message: Message, state: FSMContext):
    url = message.text.strip()
    status = await message.answer("⏳ Video yuklab olinmoqda...")

    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        src_path = tmp / "input.mp4"
        ok = await download_video(url, str(src_path))
        if not ok:
            await status.edit_text(
                "❌ Videoni yuklab bo'lmadi. Havola to'g'riligini va postning "
                "ochiq (public) ekanligini tekshiring."
            )
            await state.clear()
            return
        await process_and_reply(message, status, src_path, tmp)

    await state.clear()


@caption_router.message(CaptionStates.waiting_input, F.text, ~F.text.startswith("/"))
async def handle_unrecognized_input(message: Message):
    await message.answer(
        "Menga video fayl yoki video havolasini yuboring, yoki bekor qilish "
        "uchun /cancel bosing."
    )


# ----------------------------------------------------------------- yordamchi

async def download_video(url: str, output_path: str) -> bool:
    """yt-dlp orqali videoni yuklab oladi. bot.py'da asosiy funksiya uchun
    sozlangan Instagram cookies va impersonatsiyani QAYTA ISHLATADI —
    shunda bu ham xuddi asosiy bot kabi ishonchli ishlaydi. Import
    funksiya ICHIDA (module darajasida emas), chunki bot.py caption_router'ni
    import qilganda hali to'liq yuklanib ulgurmagan bo'ladi (circular
    import) — bu chaqiruv esa faqat foydalanuvchi haqiqatan havola
    yuborganda amalga oshadi, ya'ni bot allaqachon to'liq ishga tushgan."""
    import yt_dlp

    ydl_opts = {
        "outtmpl": output_path,
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "overwrites": True,
    }

    try:
        from bot import IG_COOKIES_FILE, YT_COOKIES_FILE
        if IG_COOKIES_FILE and "instagram.com" in url:
            ydl_opts["cookiefile"] = IG_COOKIES_FILE
        if YT_COOKIES_FILE and ("youtube.com" in url or "youtu.be" in url):
            ydl_opts["cookiefile"] = YT_COOKIES_FILE
    except ImportError:
        pass  # bot.py hali yuklanmagan bo'lsa (masalan test muhitida) — davom etamiz

    if "instagram.com" in url:
        try:
            from yt_dlp.networking.impersonate import ImpersonateTarget
            ydl_opts["impersonate"] = ImpersonateTarget("chrome")
        except Exception as e:
            logger.warning(f"ImpersonateTarget sozlashda xato (o'tkazib yuborildi): {e}")

    def _sync_download():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

    try:
        await asyncio.to_thread(_sync_download)
    except Exception as e:
        logger.error(f"yt-dlp xatosi: {e}")
        return False

    return os.path.exists(output_path)


async def process_and_reply(message: Message, status: Message, src_path: Path, tmp: Path):
    """Audio ajratish -> transkript -> SRT -> kuydirish -> yuborish."""

    duration = await get_duration(src_path)
    if duration and duration > MAX_VIDEO_SECONDS:
        await status.edit_text(
            f"❌ Video juda uzun ({int(duration)}s). {MAX_VIDEO_SECONDS}s dan qisqa video yuboring."
        )
        return

    audio_path = tmp / "audio.wav"
    try:
        await run_ffmpeg([
            "ffmpeg", "-y", "-i", str(src_path),
            "-ac", "1", "-ar", "16000", str(audio_path),
        ])
    except RuntimeError as e:
        logger.error(f"ffmpeg audio ajratish xatosi: {e}")
        await status.edit_text("❌ Videoni o'qib bo'lmadi. Fayl formatini tekshiring.")
        return

    await status.edit_text("🧠 Nutq tanilmoqda...")
    pipe = get_asr_pipeline()
    result = await asyncio.to_thread(
        pipe,
        str(audio_path),
        return_timestamps="word",
        generate_kwargs={"language": "uzbek", "task": "transcribe"},
    )
    words = result.get("chunks") or []

    if not words:
        await status.edit_text("❌ Videoda nutq topilmadi.")
        return

    srt_path = tmp / "subs.ass"
    write_ass(words, srt_path)

    await status.edit_text("🎞 Titr videoga yozilmoqda...")
    out_path = tmp / "output.mp4"
    try:
        await burn_subtitles(src_path, srt_path, out_path)
    except RuntimeError as e:
        logger.error(f"ffmpeg subtitr kuydirish xatosi: {e}")
        await status.edit_text("❌ Titr yozishda xatolik yuz berdi.")
        return

    await status.edit_text("📤 Yuborilmoqda...")
    await message.answer_video(FSInputFile(out_path), caption="✅ Tayyor!")
    await status.delete()


async def get_duration(path: Path) -> Optional[float]:
    cmd = [
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, _ = await proc.communicate()
    try:
        return float(out.decode().strip())
    except ValueError:
        return None


async def run_ffmpeg(cmd: list):
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(stderr.decode(errors="ignore"))


def group_words_into_lines(words, max_words: int = 5, max_chars: int = 42, max_duration: float = 4.0):
    """transformers ASR pipeline'ning so'z darajasidagi natijasidan
    qisqa subtitr qatorlarini yasaydi."""
    lines = []
    current_words = []
    current_start = None
    last_end = 0.0

    for word in words:
        text = word.get("text", "")
        start, end = word.get("timestamp", (None, None))
        if start is None:
            start = last_end
        if end is None:
            end = start
        last_end = end

        if current_start is None:
            current_start = start
        current_words.append(text)

        text_so_far = "".join(current_words).strip()
        duration = end - current_start

        if (
            len(current_words) >= max_words
            or len(text_so_far) >= max_chars
            or duration >= max_duration
        ):
            lines.append((current_start, end, text_so_far))
            current_words = []
            current_start = None

    if current_words:
        text_so_far = "".join(current_words).strip()
        lines.append((current_start, last_end, text_so_far))

    return lines


def format_ass_timestamp(seconds: float) -> str:
    cs_total = int(round(seconds * 100))
    h, cs_total = divmod(cs_total, 360000)
    m, cs_total = divmod(cs_total, 6000)
    s, cs = divmod(cs_total, 100)
    return f"{h:01}:{m:02}:{s:02}.{cs:02}"


def write_ass(
    words,
    path: Path,
    font_name: str = "DejaVu Sans",
    font_size: int = 20,
    fade_in_ms: int = 250,
    fade_out_ms: int = 150,
):
    """SRT o'rniga to'liq ASS fayl yasaydi — bu orqa fonsiz (faqat nozik
    qora outline bilan) va har qator sekin paydo bo'ladigan (fade-in)
    subtitrlarga imkon beradi (bular SRT formatida ishlamaydi).
    ESLATMA: font_name konteynerda o'rnatilgan bo'lishi kerak — "DejaVu
    Sans" ko'pchilik Linux tizimlarida standart o'rnatilgan bo'ladi.
    Boshqa shrift kerak bo'lsa, uni ffmpeg image'ga apt orqali o'rnatib
    (masalan "fonts-dejavu", "fonts-noto"), shu yerdagi font_name'ni
    o'sha shrift nomiga almashtiring."""
    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        "PlayResX: 384\n"
        "PlayResY: 288\n"
        "ScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        # BorderStyle=1 -> faqat outline+shadow, orqa fon (quti) YO'Q.
        # Outline=2 -> matn har xil fonda ham o'qilishi uchun yetarli qalin
        # qora chegara. Shadow=0 -> qo'shimcha soya yo'q (toza ko'rinish).
        f"Style: Default,{font_name},{font_size},&H00FFFFFF,&H000000FF,"
        "&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,0,2,10,10,30,1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )

    entries = group_words_into_lines(words)
    lines = [header]
    for start, end, text in entries:
        if not text:
            continue
        start_ts = format_ass_timestamp(start)
        end_ts = format_ass_timestamp(end)
        # {\fad(in_ms,out_ms)} -> qator sekin paydo bo'lib, sekin yo'qoladi.
        lines.append(
            f"Dialogue: 0,{start_ts},{end_ts},Default,,0,0,0,,"
            f"{{\\fad({fade_in_ms},{fade_out_ms})}}{text}\n"
        )
    path.write_text("".join(lines), encoding="utf-8")


async def burn_subtitles(src: Path, ass: Path, out: Path):
    """libass 'ass' filtri bilan ASS subtitr faylini videoga hardsub qiladi
    (stil, fade-in/out va shrift ASS faylning o'zida belgilangan)."""
    ass_escaped = str(ass).replace("\\", "/").replace(":", "\\:")
    vf = f"ass='{ass_escaped}'"
    cmd = ["ffmpeg", "-y", "-i", str(src), "-vf", vf, "-c:a", "copy", str(out)]
    await run_ffmpeg(cmd)
