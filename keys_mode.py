# -*- coding: utf-8 -*-
"""
🔑 Kalit (key) tarqatish bo'limi.

Foydalanuvchi:
    botga  key  deb yozsa (katta-kichik harf farqi yo'q, /key ham bo'ladi) —
    admin kiritgan kalitlardan NAVBATDAGISI yuboriladi. Kalitlar aylanma
    tartibda beriladi: 1, 2, 3, ..., oxirgisidan keyin yana 1-dan.
    Majburiy obunaga a'zo bo'lmagan foydalanuvchiga kalit YUBORILMAYDI —
    o'rniga obuna bo'lish tugmalari chiqadi.

Admin (OWNER_CHAT_IDS) buyruqlari:
    /addkey KALIT          — bitta kalit qo'shish
    /addkey                — QABUL REJIMI: shundan keyin yuborilgan har bir
                             xabar kalit bo'lib qo'shiladi (bir xabarda bir
                             nechta qator bo'lsa — har qatori alohida kalit);
                             /done bilan tugaydi (yoki 10 daqiqa jimlikdan keyin)
    /addkey KALIT1\nKALIT2  — bir xabarda bir nechtasini darhol qo'shish
    /keys                  — barcha kalitlar ro'yxati (raqami va necha marta berilgani)
    /delkey 3              — 3-raqamli kalitni o'chirish
    /clearkeys             — hammasini o'chirish

Kalitlar va navbat bazada (DB_PATH) saqlanadi — bot qayta ishga tushsa ham
yo'qolmaydi. Botning boshqa funksiyalariga ta'sir qilmaydi: router faqat
"key" so'zi va yuqoridagi admin buyruqlarini ushlaydi.
"""

import asyncio
import html
import logging
import sqlite3
import time

from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery

from bot_ref import bot_mod

logger = logging.getLogger(__name__)

keys_router = Router(name="keys_mode")

# Foydalanuvchi shu so'zlardan birini yozsa kalit beriladi (kichik harfda).
TRIGGER_WORDS = {"key", "/key"}

_lock = asyncio.Lock()

# Admin "kalit qabul qilish" rejimi: /addkey (argumentsiz) -> keyingi har bir
# xabar kalit sifatida qo'shiladi; /done bilan yoki ADD_MODE_TIMEOUT soniya
# jimlikdan keyin avtomatik tugaydi. {admin_id: {"last": vaqt, "count": n}}
ADD_MODE_TIMEOUT = 10 * 60
_add_mode: dict = {}

KEY_TEXTS = {
    "uz": {
        "your_key": "🔑 Sizning kalitingiz:\n\n<code>{key}</code>\n\nNusxa olish uchun kalit ustiga bosing.",
        "no_keys": "😕 Hozircha bo'sh kalit yo'q. Birozdan keyin qayta urinib ko'ring.",
        "paused": "🔄 KEYlar yangilanmoqda. Birozdan keyin qayta urinib ko'ring.",
    },
    "ru": {
        "your_key": "🔑 Ваш ключ:\n\n<code>{key}</code>\n\nНажмите на ключ, чтобы скопировать.",
        "no_keys": "😕 Сейчас свободных ключей нет. Попробуйте чуть позже.",
        "paused": "🔄 KEY-и обновляются. Попробуйте чуть позже.",
    },
    "en": {
        "your_key": "🔑 Your key:\n\n<code>{key}</code>\n\nTap the key to copy it.",
        "no_keys": "😕 No keys available right now. Please try again a bit later.",
        "paused": "🔄 KEYs are being updated. Please try again a bit later.",
    },
}


def kt(name: str, lang: str, **kw) -> str:
    # MUHIM: parametr nomi "key" EMAS — chunki matnga kalit qiymati ham
    # key=... bilan beriladi (aks holda "got multiple values for argument").
    d = KEY_TEXTS.get(lang) or KEY_TEXTS["ru"]
    text = d.get(name) or KEY_TEXTS["uz"][name]
    return text.format(**kw) if kw else text


# ------------------------------------------------------------------ baza

def _conn():
    conn = sqlite3.connect(bot_mod().DB_PATH)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS keys_pool ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " value TEXT NOT NULL,"
        " given INTEGER NOT NULL DEFAULT 0,"
        " created TEXT DEFAULT (datetime('now')))"
    )
    conn.execute("CREATE TABLE IF NOT EXISTS keys_state (k TEXT PRIMARY KEY, v TEXT)")
    return conn


def _list_keys() -> list:
    conn = _conn()
    rows = conn.execute("SELECT id, value, given FROM keys_pool ORDER BY id").fetchall()
    conn.close()
    return rows


def _add_keys(values: list) -> int:
    conn = _conn()
    conn.executemany("INSERT INTO keys_pool (value) VALUES (?)", [(v,) for v in values])
    conn.commit()
    conn.close()
    return len(values)


def _delete_by_position(pos: int):
    rows = _list_keys()
    if not 1 <= pos <= len(rows):
        return None
    key_id, value, _ = rows[pos - 1]
    conn = _conn()
    conn.execute("DELETE FROM keys_pool WHERE id = ?", (key_id,))
    conn.commit()
    conn.close()
    return value


def _clear_keys() -> int:
    conn = _conn()
    n = conn.execute("SELECT COUNT(*) FROM keys_pool").fetchone()[0]
    conn.execute("DELETE FROM keys_pool")
    conn.execute("DELETE FROM keys_state WHERE k = 'last_id'")
    conn.commit()
    conn.close()
    return n


def _is_paused() -> bool:
    conn = _conn()
    row = conn.execute("SELECT v FROM keys_state WHERE k = 'paused'").fetchone()
    conn.close()
    return bool(row and row[0] == "1")


def _set_paused(paused: bool) -> None:
    conn = _conn()
    conn.execute(
        "INSERT INTO keys_state (k, v) VALUES ('paused', ?) "
        "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
        ("1" if paused else "0",),
    )
    conn.commit()
    conn.close()


def _next_key():
    """Navbatdagi kalitni qaytaradi (aylanma tartib) va hisobini oshiradi.
    Oxirgi berilgan kalitning id'si saqlanadi — kalit o'chirilsa yoki
    yangisi qo'shilsa ham navbat to'g'ri davom etadi."""
    conn = _conn()
    try:
        row = conn.execute("SELECT v FROM keys_state WHERE k = 'last_id'").fetchone()
        last_id = int(row[0]) if row else 0
        nxt = conn.execute(
            "SELECT id, value FROM keys_pool WHERE id > ? ORDER BY id LIMIT 1", (last_id,)
        ).fetchone()
        if nxt is None:  # ro'yxat oxiriga yetdik — boshidan
            nxt = conn.execute("SELECT id, value FROM keys_pool ORDER BY id LIMIT 1").fetchone()
        if nxt is None:
            return None
        key_id, value = nxt
        conn.execute("UPDATE keys_pool SET given = given + 1 WHERE id = ?", (key_id,))
        conn.execute(
            "INSERT INTO keys_state (k, v) VALUES ('last_id', ?) "
            "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
            (str(key_id),),
        )
        conn.commit()
        return value
    finally:
        conn.close()


# --------------------------------------------------------------- handlerlar

def _is_owner(user_id: int) -> bool:
    try:
        return user_id in bot_mod().OWNER_CHAT_IDS
    except Exception:
        return False


def _is_trigger(m: Message) -> bool:
    return (m.text or "").strip().lower() in TRIGGER_WORDS


def _in_add_mode(m: Message) -> bool:
    if not m.from_user or m.from_user.id not in _add_mode:
        return False
    if time.monotonic() - _add_mode[m.from_user.id]["last"] > ADD_MODE_TIMEOUT:
        _add_mode.pop(m.from_user.id, None)  # muddati o'tdi
        return False
    return not (m.text or "").startswith("/")


@keys_router.message(F.chat.type == "private", F.text, _in_add_mode)
async def handle_key_input(message: Message):
    """Qabul rejimidagi admin yuborgan xabar — kalit(lar) sifatida qo'shiladi."""
    values = [v.strip() for v in (message.text or "").splitlines() if v.strip()]
    if not values:
        return
    n = await asyncio.to_thread(_add_keys, values)
    st = _add_mode.get(message.from_user.id) or {"count": 0}
    st["count"] = st.get("count", 0) + n
    st["last"] = time.monotonic()
    _add_mode[message.from_user.id] = st
    total = len(await asyncio.to_thread(_list_keys))
    await message.answer(f"✅ +{n} (bu safar: {st['count']}, jami: {total}). Davom eting yoki /done")


@keys_router.message(F.text.in_({"/done", "/stop", "/done@AllSaveUz_Bot", "/stop@AllSaveUz_Bot"}),
                     lambda m: bool(m.from_user) and m.from_user.id in _add_mode)
async def cmd_done(message: Message):
    st = _add_mode.pop(message.from_user.id, None) or {"count": 0}
    total = len(await asyncio.to_thread(_list_keys))
    await message.answer(f"✅ Tugatildi. Bu safar {st.get('count', 0)} ta kalit qo'shildi. Jami: {total} ta.\nRo'yxat: /keys")


async def _give_key(bot: Bot, chat_message: Message, user) -> None:
    """Majburiy obunani tekshirib, navbatdagi kalitni yuboradi. "key" matni
    handleri shu funksiyani chaqiradi."""
    B = bot_mod()
    B.track_user(user.id, user.username)
    lang = B.get_user_lang(user.id) or "ru"

    # Majburiy obuna — a'zo bo'lmasa kalit berilmaydi.
    if not await B.is_subscribed(bot, user.id):
        await chat_message.answer(B.t("subscribe", lang), reply_markup=B.subscribe_keyboard(lang))
        return

    # Admin /pause qilgan bo'lsa — kalit berilmaydi, "yangilanmoqda" deyiladi.
    if await asyncio.to_thread(_is_paused):
        await chat_message.answer(kt("paused", lang))
        return

    async with _lock:  # bir vaqtda kelgan so'rovlar bir xil kalitni olmasligi uchun
        value = await asyncio.to_thread(_next_key)
    if value is None:
        await chat_message.answer(kt("no_keys", lang))
        return
    logger.info(f"KEY: {user.id} ga kalit berildi")
    await chat_message.answer(kt("your_key", lang, key=html.escape(value)), parse_mode="HTML")


@keys_router.message(F.chat.type == "private", F.text, _is_trigger)
async def handle_key_request(message: Message, bot: Bot):
    await _give_key(bot, message, message.from_user)


def _cmd_args(message: Message) -> str:
    text = message.text or ""
    parts = text.split(maxsplit=1)
    return parts[1] if len(parts) > 1 else ""


@keys_router.message(F.text.startswith("/addkey"))
async def cmd_addkey(message: Message):
    if not _is_owner(message.from_user.id):
        return
    values = [v.strip() for v in _cmd_args(message).splitlines() if v.strip()]
    if not values:
        _add_mode[message.from_user.id] = {"last": time.monotonic(), "count": 0}
        await message.answer(
            "🔑 Kalit qabul qilish rejimi YOQILDI.\n\n"
            "Endi kalitlarni yuboravering — har bir xabar kalit bo'lib qo'shiladi "
            "(bir xabarda bir nechta qator bo'lsa, har qatori alohida kalit).\n\n"
            "Tugatish: /done"
        )
        return
    n = await asyncio.to_thread(_add_keys, values)
    total = len(await asyncio.to_thread(_list_keys))
    await message.answer(f"✅ {n} ta kalit qo'shildi. Jami: {total} ta.")


@keys_router.message(F.text.in_({"/keys", "/keys@AllSaveUz_Bot"}))
async def cmd_keys(message: Message):
    if not _is_owner(message.from_user.id):
        return
    rows = await asyncio.to_thread(_list_keys)
    if not rows:
        await message.answer("Kalitlar yo'q. Qo'shish: /addkey KALIT")
        return
    holat = "⏸ TO'XTATILGAN (/resume bilan yoqing)" if await asyncio.to_thread(_is_paused) else "▶️ ishlayapti"
    lines = [f"🔑 Kalitlar ({len(rows)} ta) — foydalanuvchilarga navbat bilan beriladi.\nHolat: {holat}\n"]
    for i, (_id, value, given) in enumerate(rows, 1):
        lines.append(f"{i}. <code>{html.escape(value)}</code> — {given} marta berilgan")
    lines.append("\nO'chirish: /delkey RAQAM   •   Hammasini: /clearkeys")
    text = "\n".join(lines)
    # Telegram xabari 4096 belgidan oshmasligi uchun bo'lib yuboramiz.
    while text:
        chunk = text[:4000]
        if len(text) > 4000:
            cut = chunk.rfind("\n")
            chunk = text[:cut if cut > 0 else 4000]
        await message.answer(chunk, parse_mode="HTML")
        text = text[len(chunk):].lstrip("\n")


@keys_router.message(F.text.startswith("/delkey"))
async def cmd_delkey(message: Message):
    if not _is_owner(message.from_user.id):
        return
    arg = _cmd_args(message).strip()
    if not arg.isdigit():
        await message.answer("Foydalanish: /delkey RAQAM  (raqamni /keys ro'yxatidan oling)")
        return
    value = await asyncio.to_thread(_delete_by_position, int(arg))
    if value is None:
        await message.answer("Bunday raqamli kalit yo'q. Ro'yxat: /keys")
        return
    await message.answer(f"🗑 O'chirildi: <code>{html.escape(value)}</code>", parse_mode="HTML")


@keys_router.message(F.text.in_({"/pause", "/pause@AllSaveUz_Bot"}))
async def cmd_pause(message: Message):
    if not _is_owner(message.from_user.id):
        return
    await asyncio.to_thread(_set_paused, True)
    await message.answer(
        "⏸ Kalit berish TO'XTATILDI.\n"
        "Endi \"key\" yozganlarga \"🔄 KEYlar yangilanmoqda\" degan xabar boradi.\n\n"
        "Kalitlarni yangilang (/clearkeys, /addkey ...), keyin qayta yoqing: /resume"
    )


@keys_router.message(F.text.in_({"/resume", "/resume@AllSaveUz_Bot"}))
async def cmd_resume(message: Message):
    if not _is_owner(message.from_user.id):
        return
    await asyncio.to_thread(_set_paused, False)
    total = len(await asyncio.to_thread(_list_keys))
    await message.answer(f"▶️ Kalit berish QAYTA YOQILDI. Jami kalitlar: {total} ta.")


@keys_router.message(F.text.in_({"/clearkeys", "/clearkeys@AllSaveUz_Bot"}))
async def cmd_clearkeys(message: Message):
    if not _is_owner(message.from_user.id):
        return
    n = await asyncio.to_thread(_clear_keys)
    await message.answer(f"🗑 {n} ta kalit o'chirildi.")
