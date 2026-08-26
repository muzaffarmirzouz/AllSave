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
import tempfile
import uuid

from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import Message, FSInputFile
from aiogram.client.default import DefaultBotProperties
import yt_dlp

BOT_TOKEN = os.environ["BOT_TOKEN"]

# Ixtiyoriy: agar bo'sh qoldirsangiz, bot HAMMAGA ochiq bo'ladi.
# Faqat ma'lum odamlarga cheklamoqchi bo'lsangiz, vergul bilan ID kiriting:
# masalan OWNER_CHAT_IDS=987654321,111222333
_owner_ids_raw = os.environ.get("OWNER_CHAT_IDS", "").strip()
OWNER_CHAT_IDS = [int(x.strip()) for x in _owner_ids_raw.split(",") if x.strip()]

MAX_TELEGRAM_MB = 50

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("video-bot")

router = Router()


def is_allowed(user_id: int) -> bool:
    if not OWNER_CHAT_IDS:
        return True  # cheklov qo'yilmagan, hammaga ochiq
    return user_id in OWNER_CHAT_IDS


@router.message(F.text == "/start")
async def cmd_start(message: Message):
    await message.answer(
        "Salom! Instagram Reels, TikTok yoki YouTube Shorts havolasini "
        "yuboring — videoni yuklab, sizga jo'nataman.\n\n"
        f"Eslatma: Telegram cheklovi tufayli faqat {MAX_TELEGRAM_MB} MB'gacha "
        "bo'lgan videolarni yubora olaman."
    )


@router.message(F.text.startswith("http"))
async def handle_link(message: Message, bot: Bot):
    if not is_allowed(message.from_user.id):
        await message.answer("Kechirasiz, bu bot sizga ochiq emas.")
        return

    url = message.text.strip()
    status = await message.answer("⏳ Video yuklab olinmoqda...")

    tmp_dir = tempfile.mkdtemp()
    out_template = os.path.join(tmp_dir, f"{uuid.uuid4().hex}.%(ext)s")

    ydl_opts = {
        "outtmpl": out_template,
        "format": "mp4/best[ext=mp4]/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "max_filesize": MAX_TELEGRAM_MB * 1024 * 1024,
    }

    downloaded_path = None
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            downloaded_path = ydl.prepare_filename(info)

        if not downloaded_path or not os.path.exists(downloaded_path):
            await status.edit_text(
                "❌ Video topilmadi yoki yuklab bo'lmadi. "
                "Havola to'g'riligini tekshiring."
            )
            return

        size_mb = os.path.getsize(downloaded_path) / (1024 * 1024)
        if size_mb > MAX_TELEGRAM_MB:
            await status.edit_text(
                f"❌ Video {size_mb:.1f} MB — bu {MAX_TELEGRAM_MB} MB "
                "Telegram chegarasidan katta, yubora olmayman."
            )
            return

        await status.edit_text("✅ Yuklandi, yuborilmoqda...")
        await bot.send_video(
            chat_id=message.chat.id,
            video=FSInputFile(downloaded_path),
            caption="✅ Tayyor!",
        )
        await status.delete()

    except yt_dlp.utils.DownloadError as e:
        log.warning(f"Download xato: {e}")
        await status.edit_text(
            "❌ Videoni yuklab bo'lmadi. Havola noto'g'ri, video "
            "o'chirilgan yoki maxfiy bo'lishi mumkin."
        )
    except Exception as e:
        log.error(f"Kutilmagan xato: {e}")
        await status.edit_text("❌ Xatolik yuz berdi, birozdan keyin qayta urinib ko'ring.")
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
    await dp.start_polling(bot, allowed_updates=["message"])


if __name__ == "__main__":
    asyncio.run(main())
