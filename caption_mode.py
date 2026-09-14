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


# --------------------------------------------------------------- ko'p tillilik

CAPTION_TEXTS = {
    "uz": {
        "mode_on": (
            "🎬 <b>Titr qo'shish rejimi yoqildi.</b>\n\n"
            "Menga video fayl yoki video havolasini (Instagram, TikTok va h.k.) yuboring — "
            "o'zbek tilida bo'lsa, unga avtomatik titr yozib qaytaraman.\n\n"
            "Istagancha video/link yuborishingiz mumkin — har birida titr qo'shib "
            "boraman. Oddiy rejimga qaytish uchun /start, faqat shu rejimdan chiqish "
            "uchun /cancel bosing."
        ),
        "cancelled": "Bekor qilindi.",
        "too_large": "Video juda katta ({max}MB dan oshmasin).",
        "downloading": "⏳ Video yuklab olinmoqda...",
        "download_failed": (
            "❌ Videoni yuklab bo'lmadi. Havola to'g'riligini va postning "
            "ochiq (public) ekanligini tekshiring."
        ),
        "unrecognized": "Menga video fayl yoki video havolasini yuboring, yoki bekor qilish uchun /cancel bosing.",
        "too_long": "❌ Video juda uzun ({sec}s). {max}s dan qisqa video yuboring.",
        "cant_read": "❌ Videoni o'qib bo'lmadi. Fayl formatini tekshiring.",
        "transcribing": "🧠 Nutq tanilmoqda...",
        "no_speech": "❌ Videoda nutq topilmadi.",
        "burning": "🎞 Titr videoga yozilmoqda...",
        "burn_failed": "❌ Titr yozishda xatolik yuz berdi.",
        "uploading": "📤 Yuborilmoqda...",
        "done_caption": "✅ Tayyor! Yana video/link yuborishingiz mumkin, yoki /start bilan oddiy rejimga qayting.",
    },
    "ru": {
        "mode_on": (
            "🎬 <b>Режим добавления субтитров включён.</b>\n\n"
            "Отправьте мне видеофайл или ссылку на видео (Instagram, TikTok и т.д.) — "
            "если видео на узбекском языке, автоматически добавлю субтитры.\n\n"
            "Можете отправлять сколько угодно видео/ссылок — буду добавлять субтитры "
            "к каждому. Чтобы вернуться в обычный режим — /start, чтобы выйти только "
            "из этого режима — /cancel."
        ),
        "cancelled": "Отменено.",
        "too_large": "Видео слишком большое (не более {max}МБ).",
        "downloading": "⏳ Скачиваю видео...",
        "download_failed": (
            "❌ Не удалось скачать видео. Проверьте ссылку и убедитесь, что пост "
            "открытый (public)."
        ),
        "unrecognized": "Отправьте мне видеофайл или ссылку на видео, либо нажмите /cancel для отмены.",
        "too_long": "❌ Видео слишком длинное ({sec}с). Отправьте видео короче {max}с.",
        "cant_read": "❌ Не удалось прочитать видео. Проверьте формат файла.",
        "transcribing": "🧠 Распознаю речь...",
        "no_speech": "❌ Речь в видео не найдена.",
        "burning": "🎞 Добавляю субтитры на видео...",
        "burn_failed": "❌ Произошла ошибка при добавлении субтитров.",
        "uploading": "📤 Отправляю...",
        "done_caption": "✅ Готово! Можете отправить ещё видео/ссылку, или вернуться в обычный режим через /start.",
    },
    "en": {
        "mode_on": (
            "🎬 <b>Subtitle mode enabled.</b>\n\n"
            "Send me a video file or a video link (Instagram, TikTok, etc.) — "
            "if the video is in Uzbek, I'll add subtitles automatically.\n\n"
            "You can send as many videos/links as you like — I'll add subtitles to "
            "each one. To return to normal mode, use /start; to exit just this mode, "
            "use /cancel."
        ),
        "cancelled": "Cancelled.",
        "too_large": "The video is too large (must be under {max}MB).",
        "downloading": "⏳ Downloading video...",
        "download_failed": "❌ Couldn't download the video. Check the link and make sure the post is public.",
        "unrecognized": "Send me a video file or a video link, or press /cancel to cancel.",
        "too_long": "❌ The video is too long ({sec}s). Please send a video under {max}s.",
        "cant_read": "❌ Couldn't read the video. Check the file format.",
        "transcribing": "🧠 Transcribing speech...",
        "no_speech": "❌ No speech found in the video.",
        "burning": "🎞 Adding subtitles to the video...",
        "burn_failed": "❌ An error occurred while adding subtitles.",
        "uploading": "📤 Uploading...",
        "done_caption": "✅ Done! You can send another video/link, or return to normal mode with /start.",
    },
}


def get_lang(user_id: int) -> str:
    """bot.py'da saqlangan foydalanuvchi tilini o'qiydi (uz/ru/en).
    Import funksiya ICHIDA — circular import bo'lmasligi uchun."""
    try:
        from bot import get_user_lang
        return get_user_lang(user_id) or "uz"
    except ImportError:
        return "uz"


def ct(key: str, lang: str, **kwargs) -> str:
    lang = lang if lang in CAPTION_TEXTS else "uz"
    text = CAPTION_TEXTS[lang].get(key) or CAPTION_TEXTS["uz"].get(key, "")
    return text.format(**kwargs) if kwargs else text


# ------------------------------------------------------------- /caption kirish

@caption_router.message(Command("caption"))
async def cmd_caption(message: Message, state: FSMContext):
    await state.set_state(CaptionStates.waiting_input)
    lang = get_lang(message.from_user.id)
    await message.answer(ct("mode_on", lang), parse_mode="HTML")


@caption_router.callback_query(F.data == "mode_caption")
async def cb_caption_mode(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await cmd_caption(callback.message, state)


@caption_router.message(Command("cancel"), CaptionStates.waiting_input)
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    lang = get_lang(message.from_user.id)
    await message.answer(ct("cancelled", lang))


# ---------------------------------------------------------------- video kelsa

@caption_router.message(CaptionStates.waiting_input, F.video | F.document)
async def handle_video_input(message: Message, bot: Bot, state: FSMContext):
    lang = get_lang(message.from_user.id)
    media = message.video or message.document
    if media.file_size and media.file_size > MAX_FILE_MB * 1024 * 1024:
        await message.answer(ct("too_large", lang, max=MAX_FILE_MB))
        return

    status = await message.answer(ct("downloading", lang))

    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        src_path = tmp / "input.mp4"
        tg_file = await bot.get_file(media.file_id)
        await bot.download_file(tg_file.file_path, destination=src_path)
        await process_and_reply(message, status, src_path, tmp, lang)

    # ESLATMA: bu yerda ATAYLAB state.clear() chaqirilmaydi — rejim
    # davom etadi, shunda foydalanuvchi ketma-ket bir nechta video
    # yuborsa ham har birida titr qo'shiladi. Rejimdan chiqish uchun
    # /start (oddiy rejimga qaytaradi) yoki /cancel kerak.


# ---------------------------------------------------------------- link kelsa

@caption_router.message(CaptionStates.waiting_input, F.text.startswith("http"))
async def handle_link_input(message: Message, state: FSMContext):
    lang = get_lang(message.from_user.id)
    url = message.text.strip()
    status = await message.answer(ct("downloading", lang))

    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        src_path = tmp / "input.mp4"
        ok = await download_video(url, str(src_path))
        if not ok:
            await status.edit_text(ct("download_failed", lang))
            return
        await process_and_reply(message, status, src_path, tmp, lang)

    # ESLATMA: state.clear() ATAYLAB chaqirilmaydi — rejim davom etadi
    # (muvaffaqiyatli bo'lsa ham, xato bo'lsa ham), shunda foydalanuvchi
    # qayta urinib ko'rishi yoki boshqa video/link yuborishi mumkin.
    # Rejimdan chiqish uchun /start yoki /cancel kerak.


@caption_router.message(CaptionStates.waiting_input, F.text, ~F.text.startswith("/"))
async def handle_unrecognized_input(message: Message):
    lang = get_lang(message.from_user.id)
    await message.answer(ct("unrecognized", lang))


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


async def process_and_reply(message: Message, status: Message, src_path: Path, tmp: Path, lang: str = "uz"):
    """Audio ajratish -> transkript -> ASS -> kuydirish -> yuborish."""

    duration = await get_duration(src_path)
    if duration and duration > MAX_VIDEO_SECONDS:
        await status.edit_text(ct("too_long", lang, sec=int(duration), max=MAX_VIDEO_SECONDS))
        return

    audio_path = tmp / "audio.wav"
    try:
        await run_ffmpeg([
            "ffmpeg", "-y", "-i", str(src_path),
            "-ac", "1", "-ar", "16000", str(audio_path),
        ])
    except RuntimeError as e:
        logger.error(f"ffmpeg audio ajratish xatosi: {e}")
        await status.edit_text(ct("cant_read", lang))
        return

    await status.edit_text(ct("transcribing", lang))
    pipe = get_asr_pipeline()
    result = await asyncio.to_thread(
        pipe,
        str(audio_path),
        return_timestamps="word",
        generate_kwargs={"language": "uzbek", "task": "transcribe"},
    )
    words = result.get("chunks") or []

    if not words:
        await status.edit_text(ct("no_speech", lang))
        return

    srt_path = tmp / "subs.ass"
    video_width, video_height = await get_video_dimensions(src_path)
    write_ass(words, srt_path, video_width, video_height)

    await status.edit_text(ct("burning", lang))
    out_path = tmp / "output.mp4"
    try:
        await burn_subtitles(src_path, srt_path, out_path)
    except RuntimeError as e:
        logger.error(f"ffmpeg subtitr kuydirish xatosi: {e}")
        await status.edit_text(ct("burn_failed", lang))
        return

    await status.edit_text(ct("uploading", lang))
    await message.answer_video(FSInputFile(out_path), caption=ct("done_caption", lang))
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


async def get_video_dimensions(path: Path) -> tuple:
    """Videoning kenglik/balandligini (piksellarda) aniqlaydi. Bu subtitr
    o'lchamini videoning HAQIQIY o'lchamiga moslashtirish uchun kerak —
    aks holda ASS faylidagi etalon o'lcham (masalan 384x288) bilan haqiqiy
    video o'lchami (masalan 1080x1920) mos kelmay, matn nisbatan juda katta
    yoki kichik bo'lib chiqadi."""
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "csv=s=x:p=0", str(path),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, _ = await proc.communicate()
    try:
        w_str, h_str = out.decode().strip().split("x")
        return int(w_str), int(h_str)
    except (ValueError, AttributeError):
        return 1080, 1920  # aniqlab bo'lmasa, vertikal video uchun oqilona standart


async def run_ffmpeg(cmd: list):
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(stderr.decode(errors="ignore"))


def group_words_into_lines(words, max_words: int = 4, max_chars: int = 28, max_duration: float = 3.0):
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
    video_width: int,
    video_height: int,
    font_name: str = "Noto Sans",
    fade_in_ms: int = 250,
    fade_out_ms: int = 150,
):
    """SRT o'rniga to'liq ASS fayl yasaydi — bu orqa fonsiz (faqat nozik
    qora outline bilan) va har qator sekin paydo bo'ladigan (fade-in)
    subtitrlarga imkon beradi (bular SRT formatida ishlamaydi).

    video_width/video_height — ASS faylining "etalon o'lchami"
    (PlayResX/PlayResY) videoning HAQIQIY o'lchamiga tenglashtiriladi,
    aks holda libass matnni noto'g'ri nisbatda (juda katta/kichik)
    chizib yuboradi. Shrift o'lchami videoning balandligiga nisbatan
    (~4.2%) hisoblanadi, shunda istalgan o'lchamdagi videoda mutanosib
    chiqadi.

    ESLATMA: font_name konteynerda o'rnatilgan bo'lishi kerak. "Noto Sans"
    uchun Railway'da RAILPACK_DEPLOY_APT_PACKAGES ga "fonts-noto" ni
    qo'shing (ffmpeg bilan bir qatorda, vergul bilan ajratib)."""
    font_size = max(16, int(video_height * 0.032))
    margin_v = int(video_height * 0.34)

    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {video_width}\n"
        f"PlayResY: {video_height}\n"
        "ScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        # Bold=-1 -> qalin (TikTok-uslubidagi) matn. BorderStyle=1 -> orqa
        # fon (quti) YO'Q, faqat outline+shadow. Outline qalinligi ham
        # video o'lchamiga nisbatan hisoblanadi (juda ingichka/qalin
        # bo'lib qolmasligi uchun).
        f"Style: Default,{font_name},{font_size},&H00FFFFFF,&H000000FF,"
        f"&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,{max(2, font_size // 12)},0,"
        f"2,20,20,{margin_v},1\n\n"
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
