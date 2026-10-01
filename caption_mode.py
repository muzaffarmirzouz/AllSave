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
import re
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

MAX_VIDEO_SECONDS = int(os.getenv("CAPTION_MAX_SECONDS", "120"))  # 2 daqiqa
MAX_FILE_MB = 200

_asr_pipeline = None
_asr_lock = threading.Lock()

# Bir vaqtning o'zida nechta /caption jobi (ASR + ffmpeg kuydirish) parallel
# ishlashi mumkinligini cheklaydi. Har bir job og'ir transformers ASR
# pipeline'ni CPU'da ishlatadi va ffmpeg bilan qayta kodlaydi — cheklovsiz
# bir nechtasi bir vaqtda ishga tushsa, xotira (RAM) tugab, konteyner
# "Out of memory" bilan qulashi mumkin (asosiy bot.py'dagi
# download_semaphore'ga o'xshash himoya, lekin bu yerda alohida — chunki
# caption jobi ancha ko'proq RAM yeydi).
CAPTION_MAX_CONCURRENT = int(os.getenv("CAPTION_MAX_CONCURRENT", "1"))
caption_semaphore = asyncio.Semaphore(CAPTION_MAX_CONCURRENT)
_active_captions = 0
_active_captions_lock = threading.Lock()

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


# ESLATMA: avval Titr (caption) rejimi FSM holatiga (CaptionStates,
# quyida saqlab qolingan — hozircha hech qayerda ishlatilmaydi) bog'liq
# edi: /caption bosilganda vaqtinchalik "kutish" holati yoqilardi, lekin
# bu holat /start bosilganda (yoki Railway konteyner qayta ishga
# tushganda, chunki FSM xotirada saqlanadi) DARHOL o'chib qolardi — shu
# sabab foydalanuvchi har safar yangi video uchun qaytadan /caption
# bosishga majbur bo'lardi. Endi bu Logo/Outro kabi BAZADA saqlanadigan,
# MUSTAQIL doimiy sozlama (caption_enabled) — bir marta yoqilsa, keyingi
# HAR BIR video/link avtomatik titr bilan qaytariladi, /start bosilsa
# ham o'chmaydi, faqat /captionoff (yoki mos tugma) bilan o'chadi.
class CaptionStates(StatesGroup):
    waiting_input = State()


def _caption_enabled(message: Message) -> bool:
    """Foydalanuvchida Titr rejimi (bazada saqlangan, doimiy) yoqilgan-
    yoqilmaganini tekshiradi. Import funksiya ICHIDA — circular import
    bo'lmasligi uchun (bot.py caption_router'ni import qilganda, bu modul
    hali to'liq yuklanib ulgurmagan bo'ladi)."""
    try:
        from bot import get_user_caption_enabled
        return get_user_caption_enabled(message.from_user.id)
    except ImportError:
        return False


def _set_caption_enabled(user_id: int, enabled: bool) -> None:
    try:
        from bot import set_user_caption_enabled
        set_user_caption_enabled(user_id, enabled)
    except ImportError:
        pass


# --------------------------------------------------------------- ko'p tillilik

CAPTION_TEXTS = {
    "uz": {
        "mode_on": (
            "🎬 <b>Titr qo'shish YOQILDI.</b>\n\n"
            "Endi menga yuboradigan HAR BIR video fayl yoki video havola "
            "(Instagram, TikTok va h.k.) uchun — o'zbek tilida bo'lsa — "
            "avtomatik titr yozib qaytaraman. Qayta yoqishning hojati yo'q, "
            "bu doimiy sozlama.\n\n"
            "O'chirish uchun: /captionoff"
        ),
        "mode_off": "🚫 Titr qo'shish o'chirildi. Endi videolar titrsiz (oddiy) yuboriladi.",
        "already_off": "Titr qo'shish allaqachon o'chirilgan edi.",
        "cancelled": "Bekor qilindi.",
        "too_large": "Video juda katta ({max}MB dan oshmasin).",
        "downloading": "⏳ Video yuklab olinmoqda...",
        "queued": "⏳ Hozir boshqa video qayta ishlanmoqda, navbatingiz keldi — biroz kuting...",
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
        "done_caption": "✅ Tayyor! Bu videoga {bot} orqali titr yozib berildi.\n\nTitr qo'shish hali ham YOQILGAN — keyingi videolarga ham avtomatik qo'shiladi. O'chirish uchun /captionoff.",
    },
    "ru": {
        "mode_on": (
            "🎬 <b>Добавление субтитров ВКЛЮЧЕНО.</b>\n\n"
            "Теперь к КАЖДОМУ видеофайлу или ссылке на видео (Instagram, "
            "TikTok и т.д.), которые вы мне отправите — если видео на "
            "узбекском — автоматически добавлю субтитры. Включать заново "
            "не нужно, это постоянная настройка.\n\n"
            "Чтобы выключить: /captionoff"
        ),
        "mode_off": "🚫 Добавление субтитров выключено. Теперь видео отправляются без субтитров.",
        "already_off": "Добавление субтитров уже было выключено.",
        "cancelled": "Отменено.",
        "too_large": "Видео слишком большое (не более {max}МБ).",
        "downloading": "⏳ Скачиваю видео...",
        "queued": "⏳ Сейчас обрабатывается другое видео, ваша очередь подошла — подождите немного...",
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
        "done_caption": "✅ Готово! Субтитры на это видео добавлены через {bot}.\n\nДобавление субтитров всё ещё ВКЛЮЧЕНО — к следующим видео тоже добавится автоматически. Чтобы выключить — /captionoff.",
    },
    "en": {
        "mode_on": (
            "🎬 <b>Subtitles ENABLED.</b>\n\n"
            "Now EVERY video file or video link (Instagram, TikTok, etc.) you "
            "send me — if it's in Uzbek — will automatically get subtitles "
            "added. No need to turn it on again, this is a persistent setting.\n\n"
            "To turn off: /captionoff"
        ),
        "mode_off": "🚫 Subtitles disabled. Videos will now be sent without subtitles.",
        "already_off": "Subtitles were already disabled.",
        "cancelled": "Cancelled.",
        "too_large": "The video is too large (must be under {max}MB).",
        "downloading": "⏳ Downloading video...",
        "queued": "⏳ Another video is being processed right now — your turn is next, please wait a moment...",
        "download_failed": "❌ Couldn't download the video. Check the link and make sure the post is public.",
        "unrecognized": "Send me a video file or a video link, or press /cancel to cancel.",
        "too_long": "❌ The video is too long ({sec}s). Please send a video under {max}s.",
        "cant_read": "❌ Couldn't read the video. Check the file format.",
        "transcribing": "🧠 Transcribing speech...",
        "no_speech": "❌ No speech found in the video.",
        "burning": "🎞 Adding subtitles to the video...",
        "burn_failed": "❌ An error occurred while adding subtitles.",
        "uploading": "📤 Uploading...",
        "done_caption": "✅ Done! Subtitles were added to this video via {bot}.\n\nSubtitles are still ENABLED — they'll be added to your next videos automatically too. To turn off, use /captionoff.",
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


def get_bot_tag() -> str:
    """bot.py'dagi BOT_USERNAME_TAG'ni qaytaradi (masalan '@AllSaveUz_Bot')."""
    try:
        from bot import BOT_USERNAME_TAG
        return BOT_USERNAME_TAG
    except ImportError:
        return "@AllSaveUz_Bot"


def ct(key: str, lang: str, **kwargs) -> str:
    lang = lang if lang in CAPTION_TEXTS else "uz"
    text = CAPTION_TEXTS[lang].get(key) or CAPTION_TEXTS["uz"].get(key, "")
    return text.format(**kwargs) if kwargs else text


# ------------------------------------------------------------- /caption kirish

def _enable_caption_for(user_id: int) -> None:
    """Titr qo'shishni YOQADI — bu endi doimiy (bazada saqlanadigan)
    sozlama, FSM "rejim" EMAS. Bir marta yoqilsa, keyingi HAR BIR
    video/link'ga avtomatik titr qo'shiladi, /start bosilganda ham
    o'chmaydi. O'chirish uchun /captionoff kerak."""
    _set_caption_enabled(user_id, True)


@caption_router.message(Command("caption"))
async def cmd_caption(message: Message, state: FSMContext):
    _enable_caption_for(message.from_user.id)
    lang = get_lang(message.from_user.id)
    await message.answer(ct("mode_on", lang), parse_mode="HTML")


@caption_router.callback_query(F.data == "mode_caption")
async def cb_caption_mode(callback: CallbackQuery, state: FSMContext):
    """MUHIM TUZATISH: avval bu yerda `cmd_caption(callback.message, ...)`
    chaqirilardi — lekin `callback.message` BOTNING o'zi yuborgan xabar
    (tugma bosilgan xabar), shuning uchun `callback.message.from_user.id`
    aslida BOTNING o'z ID'sini qaytaradi, tugmani bosgan FOYDALANUVCHINING
    EMAS! Shu sabab "📝 Videoga Text qo'yish" tugmasi orqali yoqilganda,
    sozlama botning (hech qachon video yubormaydigan) "foydalanuvchisi"ga
    yozilib, HAQIQIY foydalanuvchiga HECH QACHON yoqilmay qolardi — shuning
    uchun video yuborganda ham oddiy (titrsiz) qaytardi. To'g'ri ID —
    `callback.from_user.id` (tugmani bosgan HAQIQIY odam)."""
    await callback.answer()
    _enable_caption_for(callback.from_user.id)
    lang = get_lang(callback.from_user.id)
    await callback.message.answer(ct("mode_on", lang), parse_mode="HTML")


@caption_router.message(Command("captionoff"))
async def cmd_captionoff(message: Message, state: FSMContext):
    """Titr qo'shishni o'chiradi (doimiy sozlamani False qiladi)."""
    lang = get_lang(message.from_user.id)
    was_on = _caption_enabled(message)
    _set_caption_enabled(message.from_user.id, False)
    await state.clear()
    await message.answer(ct("mode_off" if was_on else "already_off", lang))


@caption_router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    """/cancel — asosan eski /setlogo va h.k. kabi vaqtinchalik FSM
    jarayonlarini bekor qilish uchun qoldirilgan. Titr endi FSM rejimi
    emasligi sababli, buni o'chirish uchun ATAYLAB o'zgartirmaydi —
    buning uchun /captionoff kerak."""
    await state.clear()
    lang = get_lang(message.from_user.id)
    await message.answer(ct("cancelled", lang))


# ---------------------------------------------------------------- video kelsa

def _is_video_document(message: Message) -> bool:
    """Hujjat (document) sifatida yuborilgan faylning aynan VIDEO
    ekanligini (mime_type orqali) tekshiradi. MUHIM: bu tekshiruv
    cookies.txt (matn fayli) va logo GIF/WEBM fayllarini Titr rejimi
    tomonidan tasodifan "video" sifatida ushlab qolinishining oldini
    oladi — chunki Titr endi DOIMIY yoqilgan bo'lishi mumkin (FSM
    "rejim" emas), shuning uchun bu handler har doim faol bo'ladi."""
    doc = message.document
    return bool(doc and (doc.mime_type or "").startswith("video/"))


@caption_router.message(lambda m: _caption_enabled(m) and bool(m.video or _is_video_document(m)))
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

    # Titr rejimi DOIMIY sozlama — hech narsa o'chirilmaydi, keyingi
    # video/link ham avtomatik titr bilan qaytadi. O'chirish uchun
    # /captionoff kerak.


# ---------------------------------------------------------------- link kelsa

# ESLATMA: avval bu yerda F.text.startswith("http") ishlatilgan edi — bu
# katta-kichik harfga sezgir va havola matn boshida (pozitsiya 0) bo'lishini
# talab qilardi (masalan "mana: https://..." kabi xabarlarni tutmay
# qolardi). bot.py'dagi asosiy havola aniqlash bilan bir xil, matn ICHIDA
# istalgan joyda bo'lgan havolani ham (katta-kichik harfdan qat'iy nazar)
# topadigan regex'ga o'tkazildi.
CAPTION_URL_IN_TEXT_RE = re.compile(r"https?://\S+", re.IGNORECASE)


@caption_router.message(F.text.regexp(r"(?i)https?://\S+"), lambda m: _caption_enabled(m))
async def handle_link_input(message: Message, state: FSMContext):
    lang = get_lang(message.from_user.id)
    match = CAPTION_URL_IN_TEXT_RE.search(message.text)
    url = match.group(0) if match else message.text.strip()
    status = await message.answer(ct("downloading", lang))

    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        src_path = tmp / "input.mp4"
        ok = await download_video(url, str(src_path))
        if not ok:
            await status.edit_text(ct("download_failed", lang))
            return
        await process_and_reply(message, status, src_path, tmp, lang)

    # Titr rejimi DOIMIY sozlama — muvaffaqiyatli bo'lsa ham, xato bo'lsa
    # ham hech narsa o'chirilmaydi, foydalanuvchi qayta urinib ko'rishi
    # yoki boshqa video/link yuborishi mumkin. O'chirish uchun
    # /captionoff kerak.

# ESLATMA: avval bu yerda, Titr FSM "rejimida" bo'lganda, video/link
# bo'lmagan har qanday matnni "tushunarsiz" deb javob qaytaradigan
# handler bor edi. Titr endi doimiy (orqa fonda) sozlama bo'lgani uchun
# (FSM rejimi emas), bunday handler endi noto'g'ri bo'lardi — aks holda
# foydalanuvchi Titr yoqilgan paytda yuborgan HAR QANDAY boshqa matn
# xabari (masalan admin buyrug'i yoki shunchaki yozishma) ushlab qolinib,
# botning boshqa funksiyalariga yetib bormay qolardi. Shuning uchun olib
# tashlandi — Titr endi FAQAT video va havolalarga ishlov beradi, boshqa
# matnlar odatdagidek bot.py'ning qolgan handlerlariga o'tadi.


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

    # PROXY_URL Railway environment variable orqali sozlansa — barcha
    # so'rovlar shu proxy orqali o'tadi (masalan Instagram cookie sessiyasi
    # tezroq eskirib qolmasligi uchun statik rezidensial proxy).
    # Format: http://user:pass@host:port yoki socks5://user:pass@host:port
    proxy_url = os.getenv("PROXY_URL")
    if proxy_url:
        ydl_opts["proxy"] = proxy_url

    if "instagram.com" in url:
        try:
            from yt_dlp.networking.impersonate import ImpersonateTarget
            ydl_opts["impersonate"] = ImpersonateTarget("chrome")
        except Exception as e:
            logger.warning(f"ImpersonateTarget sozlashda xato (o'tkazib yuborildi): {e}")

    def _sync_download(opts):
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])

    def _is_valid_download(path: str) -> bool:
        """Fayl mavjud bo'lishi YETARLI EMAS — yt-dlp ba'zan xato
        o'rtasida chala/bo'sh (0 bayt yoki juda kichik) fayl qoldirib
        ketadi, bu esa keyinchalik ffmpeg'da "moov atom not found" kabi
        xatoga olib keladi. Shuning uchun fayl hajmini ham tekshiramiz."""
        try:
            return os.path.exists(path) and os.path.getsize(path) > 10_000
        except OSError:
            return False

    # Instagram sessiyasini (bot.py bilan BIR XIL cookie/akkaunt) haddan
    # tashqari tez-tez so'rovlardan asrash uchun — bot.py'dagi umumiy
    # cheklovchidan (throttle) foydalanamiz, shunda /caption va oddiy
    # yuklab olish bitta hisoblagichni baham ko'radi.
    if "instagram.com" in url:
        try:
            from bot import _throttle_instagram
            await _throttle_instagram()
        except ImportError:
            pass

    json_error_patterns = ("failed to parse json", "jsondecodeerror", "expecting value")
    last_error = None

    # Vaqtinchalik xatolarda (Instagram ba'zan bir zumga bo'sh/noto'g'ri
    # javob qaytaradi) 3 martagacha qayta urinamiz — asosiy bot (bot.py)
    # dagi _download_with_retry bilan bir xil mantiq.
    attempts = 3
    for attempt in range(1, attempts + 1):
        try:
            if os.path.exists(output_path):
                os.remove(output_path)
            await asyncio.to_thread(_sync_download, ydl_opts)
            if _is_valid_download(output_path):
                return True
            last_error = RuntimeError("yuklangan fayl bo'sh yoki buzuq (0 bayt/juda kichik)")
        except Exception as e:
            last_error = e

        err_text = str(last_error).lower()
        if attempt < attempts:
            logger.warning(f"/caption yuklashda xato (urinish {attempt}/{attempts}), qayta urinilmoqda: {last_error}")
            await asyncio.sleep(4 * attempt)
            continue

        # Oxirgi urinish ham muvaffaqiyatsiz: Instagram uchun, cookie
        # (login) sessiyasi bilan "JSON parse" xatosi yoki bo'sh fayl
        # chiqsa — bu ko'pincha o'sha sessiyaning cheklanganini anglatadi.
        # Oxirgi chora sifatida anonim (cookie'siz) urinib ko'ramiz — ochiq
        # postlar ko'pincha mehmon sifatida muammosiz yuklanadi.
        if (
            "instagram.com" in url
            and ydl_opts.get("cookiefile")
            and (any(p in err_text for p in json_error_patterns) or "bo'sh yoki buzuq" in err_text)
        ):
            logger.warning("Cookie sessiyasi bilan hammasi muvaffaqiyatsiz — anonim (cookie'siz) urinib ko'ramiz...")
            anon_opts = dict(ydl_opts)
            anon_opts.pop("cookiefile", None)
            try:
                if os.path.exists(output_path):
                    os.remove(output_path)
                await asyncio.to_thread(_sync_download, anon_opts)
                if _is_valid_download(output_path):
                    logger.info("Anonim (cookie'siz) urinish MUVAFFAQIYATLI bo'ldi.")
                    return True
                logger.error("Anonim urinish ham bo'sh/buzuq fayl berdi.")
            except Exception as e2:
                logger.error(f"Anonim urinish ham muvaffaqiyatsiz: {e2}")
            return False
        else:
            logger.error(f"yt-dlp xatosi: {last_error}")
            return False

    return False


async def process_and_reply(message: Message, status: Message, src_path: Path, tmp: Path, lang: str = "uz"):
    """Audio ajratish -> transkript -> ASS -> kuydirish -> yuborish."""

    duration = await get_duration(src_path)
    if duration and duration > MAX_VIDEO_SECONDS:
        await status.edit_text(ct("too_long", lang, sec=int(duration), max=MAX_VIDEO_SECONDS))
        return

    # --- Bu yerdan boshlab og'ir qism (ASR + ffmpeg kuydirish + yuborish) ---
    # Bir vaqtda faqat CAPTION_MAX_CONCURRENT ta job ishlashi uchun semafor
    # ichiga olinadi (OOM'ning oldini olish uchun). Agar semafor band bo'lsa,
    # foydalanuvchiga "navbatda" xabari ko'rsatiladi.
    global _active_captions
    with _active_captions_lock:
        will_wait = _active_captions >= CAPTION_MAX_CONCURRENT
        _active_captions += 1
    try:
        if will_wait:
            await status.edit_text(ct("queued", lang))

        async with caption_semaphore:
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
                batch_size=8,
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

            # Original faylning taxminiy bitrate'ini hisoblaymiz, shunda chiqish
            # video hajmi asl faylga yaqin bo'ladi (standart CRF rejimi ba'zan
            # original'dan sezilarli kattaroq fayl berib yuborar edi). ~15%
            # audio uchun ajratib qo'yamiz (audio -c:a copy bilan o'zgarishsiz
            # qoladi, shuning uchun umumiy bitrate'dan uning ulushini olib
            # tashlaymiz).
            target_kbps = None
            try:
                src_size_bytes = src_path.stat().st_size
                if duration and duration > 0:
                    total_kbps = (src_size_bytes * 8 / 1000) / duration
                    target_kbps = max(500, int(total_kbps * 0.85))
            except OSError:
                pass

            try:
                await burn_subtitles(src_path, srt_path, out_path, target_kbps)
            except RuntimeError as e:
                logger.error(f"ffmpeg subtitr kuydirish xatosi: {e}")
                await status.edit_text(ct("burn_failed", lang))
                return

            await status.edit_text(ct("uploading", lang))
            bot_tag = get_bot_tag()
            await message.answer_video(FSInputFile(out_path), caption=ct("done_caption", lang, bot=bot_tag))
            await status.delete()
    finally:
        with _active_captions_lock:
            _active_captions -= 1


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
    font_name: str = "Montserrat",
    fade_in_ms: int = 250,
    fade_out_ms: int = 150,
):
    """SRT o'rniga to'liq ASS fayl yasaydi — bu zamonaviy (Montserrat)
    shrift, har qator ortida BILINAR-BILINMAS (yarim shaffof, juda nozik)
    qora fon va sekin paydo bo'ladigan (fade-in) subtitrlarga imkon
    beradi (bular SRT formatida ishlamaydi).

    video_width/video_height — ASS faylining "etalon o'lchami"
    (PlayResX/PlayResY) videoning HAQIQIY o'lchamiga tenglashtiriladi,
    aks holda libass matnni noto'g'ri nisbatda (juda katta/kichik)
    chizib yuboradi. Shrift o'lchami videoning balandligiga nisbatan
    (~4.2%) hisoblanadi, shunda istalgan o'lchamdagi videoda mutanosib
    chiqadi.

    ESLATMA: font_name konteynerda o'rnatilgan bo'lishi kerak. "Montserrat"
    uchun Railway'da RAILPACK_DEPLOY_APT_PACKAGES ga "fonts-montserrat" ni
    qo'shing (ffmpeg bilan bir qatorda, vergul bilan ajratib)."""
    # ESLATMA: gorizontal (16:9 kabi, kenglik balandlikdan katta) videolar
    # ekranda ko'rsatilganda balandligi kichikroq bo'lib chiqadi (masalan
    # Telegram uni kenglik bo'yicha moslashtiradi), shuning uchun bir xil
    # nisbat bilan hisoblangan shrift vertikal (9:16) videoga nisbatan
    # KICHIKROQ ko'rinadi. Shuni qoplash uchun gorizontal formatda kattaroq
    # nisbat ishlatiladi — vertikal formatga tegilmaydi.
    is_landscape = video_width > video_height
    font_ratio = 0.062 if is_landscape else 0.032
    font_size = max(16, int(video_height * font_ratio))
    margin_v = int(video_height * 0.34)
    # Fon (box) matn atrofidagi "yostiqcha" (padding) — BorderStyle=3'da
    # "Outline" maydoni shuni anglatadi (oddiy chiziq qalinligi emas).
    box_padding = max(6, int(font_size * 0.22))

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
        # Bold=-1 -> qalin (TikTok-uslubidagi) matn. BorderStyle=3 -> matn
        # ortida YAXLIT FON (quti) chiziladi — BackColour'ning birinchi ikki
        # heksa-raqami (&HCC......) ALFA (shaffoflik): 00=butunlay tiniq
        # (ko'rinadi), FF=butunlay shaffof (ko'rinmaydi). &HCC (~80%
        # shaffof, ~20% xiralik) — fon "bilinar-bilinmas", juda nozik
        # bo'lishi uchun tanlangan. "Outline" maydoni endi chiziq
        # qalinligi emas, fonning matn atrofidagi yostiqcha (padding)
        # kengligini bildiradi.
        f"Style: Default,{font_name},{font_size},&H00FFFFFF,&H000000FF,"
        f"&H00000000,&HCC000000,-1,0,0,0,100,100,0,0,3,{box_padding},0,"
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


async def burn_subtitles(src: Path, ass: Path, out: Path, target_kbps: Optional[int] = None):
    """libass 'ass' filtri bilan ASS subtitr faylini videoga hardsub qiladi
    (stil, fade-in/out va shrift ASS faylning o'zida belgilangan).

    target_kbps berilsa — video shu bitrate'ga yaqin kodlanadi (odatda
    original faylning taxminiy bitrate'i), shunda chiqish fayli asl fayl
    bilan taxminan bir xil hajmda bo'ladi (sifat-asosli CRF rejimi ba'zan
    original'dan sezilarli kattaroq fayl berib yuborishi mumkin edi).
    Berilmasa, standart CRF rejimiga tushadi."""
    ass_escaped = str(ass).replace("\\", "/").replace(":", "\\:")
    vf = f"ass='{ass_escaped}'"
    cmd = ["ffmpeg", "-y", "-i", str(src), "-vf", vf, "-c:v", "libx264", "-preset", "veryfast"]

    if target_kbps and target_kbps > 0:
        cmd += [
            "-b:v", f"{target_kbps}k",
            "-maxrate", f"{int(target_kbps * 1.4)}k",
            "-bufsize", f"{int(target_kbps * 2)}k",
        ]
    else:
        cmd += ["-crf", "23"]

    cmd += ["-c:a", "copy", str(out)]
    await run_ffmpeg(cmd)
