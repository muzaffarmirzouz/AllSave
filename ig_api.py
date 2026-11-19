# -*- coding: utf-8 -*-
"""
Instagram'ni COOKIE'SIZ yuklash — tashqi API orqali (Apify: snapinsta/instagram-downloader-api).

Nega kerak: Instagram bulut serverlari (Railway) IP'larini va bot uchun
ishlatilgan akkauntlarni tez-tez bloklaydi ("HTTP Error 400", akkaunt
"suspended"). Bu API sessiya/proxy masalasini o'zi hal qiladi.

Ishlash tartibi (bot.py handle_link):
    1) APIFY_TOKEN bo'lsa — AVVAL shu API;
    2) u xato bersa — eskicha yt-dlp (cookie) -> gallery-dl zaxiralari.
Shuning uchun API ishlamay qolsa ham bot to'xtamaydi.

Railway Variables:
    APIFY_TOKEN   — Apify shaxsiy API tokeni (apify.com -> Settings ->
                    API & Integrations -> Personal API token). Bo'lmasa bu
                    modul o'chiq turadi.
Narxi (2026-11): muvaffaqiyatli so'rov uchun ~$0.0004 (1000 tasi ~$0.40).
Xizmat tomonidagi xatolar (timeout, rate-limit) uchun pul olinmaydi.
"""

import logging
import os
from typing import List, Optional

import aiohttp

log = logging.getLogger(__name__)

APIFY_TOKEN = (os.environ.get("APIFY_TOKEN") or "").strip()
APIFY_ENDPOINT = "https://snapinsta--instagram-downloader-api.apify.actor/api/download"

API_TIMEOUT = aiohttp.ClientTimeout(total=90)
DL_TIMEOUT = aiohttp.ClientTimeout(total=180, sock_read=60)

_EXT_BY_MIME = {
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/webm": ".webm",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/heic": ".heic",
}


class IgApiError(Exception):
    """API natija bermadi — chaqiruvchi zaxira usulga (yt-dlp) o'tadi."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code


def enabled() -> bool:
    return bool(APIFY_TOKEN)


def _guess_ext(file_type: Optional[str], url: str, content_type: str = "") -> str:
    for mime in (file_type or "", content_type or ""):
        mime = mime.split(";")[0].strip().lower()
        if mime in _EXT_BY_MIME:
            return _EXT_BY_MIME[mime]
        if mime.startswith("video/"):
            return ".mp4"
        if mime.startswith("image/"):
            return ".jpg"
    low = url.lower().split("?")[0]
    for ext in (".mp4", ".mov", ".webm", ".jpg", ".jpeg", ".png", ".webp", ".heic"):
        if low.endswith(ext):
            return ext
    return ".mp4"


async def _fetch_media_list(session: aiohttp.ClientSession, url: str) -> list:
    headers = {"Authorization": f"Bearer {APIFY_TOKEN}"}
    async with session.get(APIFY_ENDPOINT, params={"url": url}, headers=headers, timeout=API_TIMEOUT) as resp:
        try:
            data = await resp.json(content_type=None)
        except Exception:
            text = (await resp.text())[:200]
            raise IgApiError(f"HTTP_{resp.status}", f"JSON emas: {text!r}")
    if not isinstance(data, dict) or not data.get("status"):
        err = (data or {}).get("error") or {} if isinstance(data, dict) else {}
        raise IgApiError(str(err.get("code") or f"HTTP_{resp.status}"), str(err.get("message") or "")[:200])
    media = data.get("media") or []
    if not media:
        raise IgApiError("EMPTY_MEDIA")
    return media


async def _download_one(session: aiohttp.ClientSession, item: dict, out_dir: str, idx: int,
                        max_bytes: int) -> Optional[str]:
    media_url = item.get("url") or ""
    if not media_url.startswith("http"):
        return None
    async with session.get(media_url, timeout=DL_TIMEOUT) as resp:
        if resp.status != 200:
            raise IgApiError(f"MEDIA_HTTP_{resp.status}")
        length = resp.content_length or 0
        if length and length > max_bytes:
            raise IgApiError("TOO_LARGE", f"{length / 1048576:.1f} MB")
        ext = _guess_ext(item.get("fileType"), media_url, resp.headers.get("Content-Type", ""))
        path = os.path.join(out_dir, f"igapi_{idx:02d}{ext}")
        size = 0
        with open(path, "wb") as f:
            async for chunk in resp.content.iter_chunked(256 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    raise IgApiError("TOO_LARGE", f">{max_bytes / 1048576:.0f} MB")
                f.write(chunk)
    if size == 0:
        os.remove(path)
        return None
    return path


async def download_instagram(url: str, out_dir: str, max_mb: int = 50, max_items: int = 30) -> List[str]:
    """Instagram havolasidagi barcha media fayllarni out_dir'ga yuklaydi va
    ularning yo'llarini (tartib bilan) qaytaradi. Xato bo'lsa IgApiError."""
    if not enabled():
        raise IgApiError("DISABLED")
    max_bytes = max_mb * 1024 * 1024
    async with aiohttp.ClientSession() as session:
        media = await _fetch_media_list(session, url)
        paths = []
        for i, item in enumerate(media[:max_items]):
            p = await _download_one(session, item, out_dir, i, max_bytes)
            if p:
                paths.append(p)
    if not paths:
        raise IgApiError("NO_FILES")
    log.info(f"IG API: {len(paths)} ta fayl yuklandi ({url})")
    return paths


# ======================================================================
# Apify kredit monitoringi — adminlarga ogohlantirish
# ======================================================================
#
#  1) Har MONITOR_INTERVAL soniyada Apify hisobidagi oylik sarf tekshiriladi
#     (GET /v2/users/me/limits). Sarf 80% ga, so'ng 100% ga yetganda —
#     adminlarga BIR MARTA (har oylik davr uchun) xabar yuboriladi. Holat
#     faylda saqlanadi, shuning uchun bot qayta ishga tushganda takrorlanmaydi.
#  2) API "pul/limit tugadi" yoki "token noto'g'ri" xatosini qaytarsa —
#     darhol xabar (ALERT_COOLDOWN oralig'ida bir martadan ko'p emas).

import asyncio
import json
import time

LIMITS_URL = "https://api.apify.com/v2/users/me/limits"
MONITOR_INTERVAL = int(os.environ.get("APIFY_CHECK_INTERVAL", str(3 * 3600)))
WARN_PERCENTS = (80, 100)
ALERT_COOLDOWN = 6 * 3600

# Pul/limit tugaganini yoki token muammosini bildiradigan xato kodlari.
BILLING_ERROR_CODES = ("PAYMENT_LIMIT_REACHED", "BILLING_UNAVAILABLE", "HTTP_402", "HTTP_401", "HTTP_403")

_alert_cb = None          # async fn(text) — bot.py beradi
_state_file = None
_last_error_alert = 0.0


async def get_usage() -> dict:
    """{"used": $, "limit": $, "start": iso, "end": iso} — Apify hisobidan."""
    headers = {"Authorization": f"Bearer {APIFY_TOKEN}"}
    async with aiohttp.ClientSession() as session:
        async with session.get(LIMITS_URL, headers=headers, timeout=API_TIMEOUT) as resp:
            data = await resp.json(content_type=None)
            if resp.status != 200:
                raise IgApiError(f"HTTP_{resp.status}", str(data)[:200])
    d = (data or {}).get("data") or {}
    cycle = d.get("monthlyUsageCycle") or {}
    return {
        "used": float((d.get("current") or {}).get("monthlyUsageUsd") or 0),
        "limit": float((d.get("limits") or {}).get("maxMonthlyUsageUsd") or 0),
        "start": cycle.get("startAt") or "",
        "end": cycle.get("endAt") or "",
    }


def format_usage(u: dict) -> str:
    used, limit = u["used"], u["limit"]
    pct = (used / limit * 100) if limit else 0
    left = max(limit - used, 0)
    approx = int(left / 0.0004) if left else 0
    end = (u.get("end") or "")[:10]
    return (
        f"💳 Apify (Instagram API) hisobi:\n\n"
        f"Ishlatilgan: ${used:.2f} / ${limit:.2f} ({pct:.0f}%)\n"
        f"Qolgan: ${left:.2f} (~{approx:,} ta yuklash)\n"
        f"Oylik davr tugaydi: {end or '?'}"
    ).replace(",", " ")


def _load_state() -> dict:
    try:
        with open(_state_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    try:
        with open(_state_file, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except Exception as e:
        log.warning(f"Apify monitor holatini saqlab bo'lmadi: {e}")


async def _alert(text: str) -> None:
    if _alert_cb is None:
        return
    try:
        await _alert_cb(text)
    except Exception as e:
        log.warning(f"Apify ogohlantirishini yuborib bo'lmadi: {e}")


async def check_once() -> None:
    """Sarfni tekshirib, kerak bo'lsa 80% / 100% ogohlantirishini yuboradi."""
    u = await get_usage()
    if not u["limit"]:
        return
    pct = u["used"] / u["limit"] * 100
    state = _load_state()
    if state.get("cycle") != u["start"]:
        state = {"cycle": u["start"], "sent": []}  # yangi oy — hisob noldan
    sent = set(state.get("sent") or [])
    for level in WARN_PERCENTS:
        if pct >= level and level not in sent:
            head = (
                "🛑 Apify krediti TUGADI — Instagram API to'xtadi!\n"
                "Bot hozir eskicha (cookie) usulda ishlayapti — ko'p Instagram postlari yuklanmasligi mumkin.\n"
                "Yechim: apify.com → Billing → tarifni oshiring yoki limitni ko'taring."
                if level >= 100 else
                f"⚠️ Apify kreditining {level}% i ishlatildi — tez orada tugashi mumkin."
            )
            await _alert(f"{head}\n\n{format_usage(u)}")
            sent.add(level)
    state["sent"] = sorted(sent)
    _save_state(state)


async def report_api_error(err: Exception) -> None:
    """download_instagram() xatosida chaqiriladi — pul/token muammosi bo'lsa
    adminlarga darhol xabar beradi (cooldown bilan)."""
    global _last_error_alert
    code = getattr(err, "code", "") or ""
    if code not in BILLING_ERROR_CODES:
        return
    now = time.monotonic()
    if _last_error_alert and now - _last_error_alert < ALERT_COOLDOWN:
        return
    _last_error_alert = now
    if code in ("HTTP_401", "HTTP_403"):
        text = (
            "🛑 Apify TOKENI ishlamayapti (" + code + ") — Instagram API to'xtadi.\n"
            "Railway → Variables → APIFY_TOKEN qiymatini tekshiring (apify.com → Settings → API & Integrations)."
        )
    else:
        text = (
            "🛑 Apify krediti/limiti tugadi (" + code + ") — Instagram API to'xtadi!\n"
            "Bot hozir eskicha (cookie) usulda ishlayapti.\n"
            "Yechim: apify.com → Billing → tarifni oshiring yoki limitni ko'taring."
        )
    try:
        text += "\n\n" + format_usage(await get_usage())
    except Exception:
        pass
    await _alert(text)


async def _monitor_loop() -> None:
    await asyncio.sleep(60)  # bot to'liq ishga tushib olsin
    while True:
        try:
            await check_once()
        except Exception as e:
            log.warning(f"Apify sarfini tekshirib bo'lmadi: {e}")
        await asyncio.sleep(MONITOR_INTERVAL)


def start_monitor(alert_cb, state_dir: str):
    """bot.py main() dan chaqiriladi. alert_cb(text) — adminlarga yuboradi."""
    global _alert_cb, _state_file
    if not enabled():
        return None
    _alert_cb = alert_cb
    _state_file = os.path.join(state_dir or ".", "apify_alerts.json")
    log.info(f"Apify monitoringi yoqildi (har {MONITOR_INTERVAL // 60} daqiqada)")
    return asyncio.create_task(_monitor_loop())


# ======================================================================
# Post izohini (caption) olish — Instagram'ning ochiq "embed" sahifasidan
# ======================================================================
#
# Apify API faqat media fayllarni beradi, izohni bermaydi. Izohni
# instagram.com/p/<kod>/embed/captioned/ sahifasidan olamiz — bu sahifa
# boshqa saytlarga post joylash uchun mo'ljallangan va login talab qilmaydi.
# Video yuklanishi bilan PARALLEL ishlaydi; topilmasa — izohsiz davom etiladi.

import html as _html
import re as _re

_SHORTCODE_RE = _re.compile(r"instagram\.com/(?:[A-Za-z0-9_.]+/)?(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)")
_EMBED_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
CAPTION_TIMEOUT = aiohttp.ClientTimeout(total=15)


def _caption_from_json(page: str) -> str:
    # Sahifa ichidagi JSON: "edge_media_to_caption":{"edges":[{"node":{"text":"..."}}]}
    m = _re.search(r'"edge_media_to_caption"\s*:\s*\{\s*"edges"\s*:\s*\[\s*\{\s*"node"\s*:\s*\{\s*"text"\s*:\s*("(?:[^"\\]|\\.)*")', page)
    if not m:
        m = _re.search(r'\\"edge_media_to_caption\\"\s*:\s*\{\s*\\"edges\\"\s*:\s*\[\s*\{\s*\\"node\\"\s*:\s*\{\s*\\"text\\"\s*:\s*(\\"(?:[^\\]|\\\\.|\\[^"])*?\\")', page)
        if not m:
            return ""
        try:
            return json.loads(json.loads('"' + m.group(1) + '"'))
        except Exception:
            return ""
    try:
        return json.loads(m.group(1))
    except Exception:
        return ""


def _caption_from_html(page: str) -> str:
    # <div class="Caption"><a class="CaptionUsername" ...>user</a><br/><br/>IZOH<div class="CaptionComments">...
    m = _re.search(r'<div class="Caption">(.*?)(?:<div class="CaptionComments"|</div>)', page, _re.S)
    if not m:
        return ""
    block = m.group(1)
    block = _re.sub(r'<a class="CaptionUsername"[^>]*>.*?</a>', "", block, count=1, flags=_re.S)
    block = _re.sub(r"<br\s*/?>", "\n", block)
    block = _re.sub(r"<[^>]+>", "", block)
    return _html.unescape(block).strip()


async def fetch_caption(url: str, proxy: Optional[str] = None) -> str:
    """Post izohini qaytaradi; topilmasa yoki xato bo'lsa bo'sh satr."""
    m = _SHORTCODE_RE.search(url)
    if not m:
        return ""  # story/highlight — izohi yo'q
    embed = f"https://www.instagram.com/p/{m.group(1)}/embed/captioned/"
    # aiohttp faqat http(s) proxy'ni qo'llaydi (socks5 bo'lsa proxy'siz).
    px = proxy if proxy and proxy.lower().startswith("http") else None
    try:
        async with aiohttp.ClientSession(headers=_EMBED_HEADERS) as session:
            async with session.get(embed, proxy=px, timeout=CAPTION_TIMEOUT) as resp:
                if resp.status != 200:
                    log.info(f"IG izoh: embed sahifa HTTP {resp.status}")
                    return ""
                page = await resp.text()
    except Exception as e:
        log.info(f"IG izoh: embed sahifani olib bo'lmadi: {type(e).__name__}: {e}")
        return ""
    cap = (_caption_from_json(page) or _caption_from_html(page) or "").strip()
    log.info(f"IG izoh: {'topildi (' + str(len(cap)) + ' belgi)' if cap else 'topilmadi'}")
    return cap
