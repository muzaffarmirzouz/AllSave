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
