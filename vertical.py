# -*- coding: utf-8 -*-
"""
vertical.py — gorizontal (16:9) videoni vertikal (9:16) formatga AQLLI kesish.

Qanday ishlaydi:
  1) Video 4 kadr/soniya tezlikda kichraytirilgan kul-rang kadrlarga ajratiladi.
  2) Har bir kadrda odam yuzi (OpenCV Haar kaskadlari) qidiriladi. Bir nechta
     yuz bo'lsa, oldingi kadrdagi odamga eng yaqini tanlanadi (tez-tez
     sakrab ketmasligi uchun), lekin ancha kichik yuzlar hisobga olinmaydi.
  3) Topilgan joylar vaqt bo'yicha tekislanadi (bo'sh joylar interpolyatsiya,
     "o'lik zona" — odam biroz qimirlasa kadr qimirlamaydi, keyin silliq
     harakat), shunda kamera "operator qo'li" kabi silliq ergashadi.
  4) ffmpeg `crop` filtri vaqt bo'yicha o'zgaruvchi x bilan (sendcmd orqali)
     9:16 oynani kesadi va 720x1280 ga keltiradi. Audio saqlanadi.
Yuz topilmasa — kadr markazidan kesiladi (bu haqda `face_ratio` qaytariladi).

Cheklov: bu YUZ aniqlash, har qanday obyekt emas. Odam yuzi ko'rinmaydigan
sahnalarda (masalan sport yoki manzara) kadr markazda qoladi.
"""

import logging
import os
import subprocess
from typing import Callable, Iterator, List, Optional, Tuple

logger = logging.getLogger(__name__)

SAMPLE_FPS = float(os.getenv("VERT_SAMPLE_FPS", "4"))
DETECT_WIDTH = int(os.getenv("VERT_DETECT_WIDTH", "640"))
OUT_HEIGHT = int(os.getenv("VERT_OUT_HEIGHT", "1280"))  # chiqish 720x1280
SMOOTH_SECONDS = float(os.getenv("VERT_SMOOTH_SECONDS", "0.8"))
DEADZONE = float(os.getenv("VERT_DEADZONE", "0.16"))  # kesim kengligining ulushi: odam shu oraliqda qimirlasa kamera turadi
OUTLIER = float(os.getenv("VERT_OUTLIER", "0.15"))  # qo'shni kadrlardagi o'rtachadan shuncha (kadr kengligi ulushi) chetga chiqqan "yuz" — xato deb tashlanadi
SETTLE_SECONDS = float(os.getenv("VERT_SETTLE", "0.5"))  # kamera maqsadga yetib borish vaqti (kattaroq = silliqroq)
MAX_SPEED = float(os.getenv("VERT_MAX_SPEED", "0.5"))  # kamera tezligi chegarasi: kesim kengligining ulushi / soniya
VERT_PRESET = os.getenv("VERT_PRESET", "medium").strip() or "medium"
VERT_CRF = os.getenv("VERT_CRF", "18").strip() or "18"
CMD_FPS = 12  # kamera holati soniyasiga necha marta yangilanadi

Detector = Callable[["object"], List[Tuple[float, float, float, float]]]


# ------------------------------------------------------------------ aniqlash

def _find_cascade_dir() -> Optional[str]:
    import cv2

    cands = []
    data = getattr(cv2, "data", None)
    if data is not None and getattr(data, "haarcascades", None):
        cands.append(data.haarcascades)
    cands.append(os.path.join(os.path.dirname(cv2.__file__), "data"))
    cands += ["/usr/share/opencv4/haarcascades", "/usr/share/opencv/haarcascades"]
    for d in cands:
        if d and os.path.exists(os.path.join(d, "haarcascade_frontalface_alt2.xml")):
            return d
    return None


def make_haar_detector() -> Optional[Detector]:
    """OpenCV Haar yuz aniqlagichi. cv2 yo'q yoki kaskad topilmasa — None."""
    try:
        import cv2
    except Exception as e:
        logger.warning(f"vertical: cv2 import bo'lmadi ({e}) — markazdan kesiladi")
        return None
    d = _find_cascade_dir()
    if not d:
        logger.warning("vertical: Haar kaskad fayllari topilmadi — markazdan kesiladi")
        return None
    frontal = cv2.CascadeClassifier(os.path.join(d, "haarcascade_frontalface_alt2.xml"))
    profile_path = os.path.join(d, "haarcascade_profileface.xml")
    profile = cv2.CascadeClassifier(profile_path) if os.path.exists(profile_path) else None
    if frontal.empty():
        logger.warning("vertical: frontal kaskad yuklanmadi — markazdan kesiladi")
        return None

    def detect(gray):
        h, w = gray.shape[:2]
        g = cv2.equalizeHist(gray)
        min_side = max(20, w // 32)
        faces = frontal.detectMultiScale(g, scaleFactor=1.1, minNeighbors=4, minSize=(min_side, min_side))
        if len(faces) == 0 and profile is not None and not profile.empty():
            faces = profile.detectMultiScale(g, scaleFactor=1.1, minNeighbors=4, minSize=(min_side, min_side))
        return [tuple(map(float, f)) for f in faces]

    return detect


def _pick_center(faces, prev_cx: Optional[float]) -> Optional[float]:
    """Yuzlardan birini tanlaydi: kattasiga yaqin o'lchamdagilar orasida, oldingi
    kadrdagi odamga eng yaqini (sakrashning oldini oladi). Yuz markazi x (piksel)."""
    if not faces:
        return None
    amax = max(w * h for (_x, _y, w, h) in faces)
    cands = [f for f in faces if f[2] * f[3] >= 0.5 * amax]
    if prev_cx is None:
        f = max(cands, key=lambda f: f[2] * f[3])
    else:
        f = min(cands, key=lambda f: abs((f[0] + f[2] / 2.0) - prev_cx))
    return f[0] + f[2] / 2.0


def _iter_gray_frames(src: str, w: int, h: int, fps: float) -> Iterator["object"]:
    import numpy as np

    cmd = [
        "ffmpeg", "-v", "error", "-i", src,
        "-vf", f"fps={fps},scale={w}:{h},format=gray",
        "-f", "rawvideo", "-",
    ]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    size = w * h
    try:
        while True:
            buf = p.stdout.read(size)
            if len(buf) < size:
                break
            yield np.frombuffer(buf, dtype=np.uint8).reshape(h, w)
    finally:
        try:
            p.stdout.close()
        except Exception:
            pass
        p.kill()
        p.wait()


def detect_track(src: str, src_w: int, src_h: int, detector: Optional[Detector]) -> List[Optional[float]]:
    """Har bir namuna kadr uchun odam markazining x ulushi (0..1) yoki None."""
    dw = DETECT_WIDTH
    dh = max(2, int(round(dw * src_h / src_w / 2.0)) * 2)
    out: List[Optional[float]] = []
    prev = None
    for gray in _iter_gray_frames(src, dw, dh, SAMPLE_FPS):
        cx = None
        if detector is not None:
            try:
                cx = _pick_center(detector(gray), prev)
            except Exception as e:
                logger.warning(f"vertical: aniqlashda xato (kadr o'tkazildi): {e}")
        if cx is not None:
            prev = cx
            out.append(cx / dw)
        else:
            out.append(None)
    return out


# ----------------------------------------------------------- kamera harakati

def clean_samples(samples: List[Optional[float]]) -> List[Optional[float]]:
    """Noto'g'ri aniqlashlarni (qo'shni kadrlardagi median joydan keskin chetga
    chiqqanlarini) tashlaydi, qolganini 3 ta median bilan tekislaydi."""
    import numpy as np

    n = len(samples)
    if n == 0:
        return []
    arr = np.array([np.nan if v is None else float(v) for v in samples])
    out = arr.copy()
    half = max(1, int(round(1.0 * SAMPLE_FPS)))  # ±1 soniya
    for i in range(n):
        if np.isnan(arr[i]):
            continue
        win = arr[max(0, i - half): i + half + 1]
        win = win[~np.isnan(win)]
        if len(win) >= 3 and abs(arr[i] - float(np.median(win))) > OUTLIER:
            out[i] = np.nan
    res = out.copy()
    for i in range(n):
        if np.isnan(out[i]):
            continue
        win = out[max(0, i - 1): i + 2]
        win = win[~np.isnan(win)]
        res[i] = float(np.median(win))
    return [None if np.isnan(v) else float(v) for v in res]


def compute_crop_x(samples: List[Optional[float]], duration: float, src_w: int, crop_w: int):
    """-> (vaqtlar, x_pozitsiyalar). Yuz joylaridan silliq kamera yo'li:
    xato aniqlashlarni tozalash -> Gauss silliqlash -> "o'lik zona" ->
    tezlik chegarali yumshoq ergashish."""
    import numpy as np

    samples = clean_samples(samples)
    dur = max(float(duration or 0.0), len(samples) / SAMPLE_FPS, 0.5)
    dt = 1.0 / CMD_FPS
    t_cmd = np.arange(0.0, dur + dt, dt)

    valid = [(i / SAMPLE_FPS, c) for i, c in enumerate(samples) if c is not None]
    if valid:
        ts, cs = zip(*valid)
        subject = np.interp(t_cmd, ts, cs) * src_w
    else:
        subject = np.full_like(t_cmd, src_w / 2.0)

    # 1) Gauss silliqlash (kechikishsiz, markazlashgan).
    sigma = max(SMOOTH_SECONDS * CMD_FPS, 0.5)
    rad = int(3 * sigma) + 1
    kern = np.exp(-0.5 * (np.arange(-rad, rad + 1) / sigma) ** 2)
    kern /= kern.sum()
    padded = np.pad(subject, rad, mode="edge")
    subject = np.convolve(padded, kern, mode="valid")

    # 2) "O'lik zona": odam kesim markazidan ±dz dan oshmaguncha maqsad joyida turadi.
    dz = DEADZONE * crop_w
    target = np.empty_like(subject)
    c = subject[0]
    for i, v in enumerate(subject):
        d = v - c
        if abs(d) > dz:
            c += d - np.sign(d) * dz
        target[i] = c

    # 3) Maqsadga yumshoq (eksponensial) va tezligi cheklangan ergashish.
    alpha = 1.0 - float(np.exp(-dt / max(SETTLE_SECONDS, 0.05)))
    vmax = MAX_SPEED * crop_w * dt
    cam = np.empty_like(target)
    p = target[0]
    for i, tg in enumerate(target):
        step = (tg - p) * alpha
        p += max(-vmax, min(vmax, step))
        cam[i] = p

    x = np.clip(cam - crop_w / 2.0, 0, max(src_w - crop_w, 0))
    return t_cmd, x


def _write_sendcmd(path: str, times, xs) -> None:
    with open(path, "w") as f:
        for t, x in zip(times, xs):
            f.write(f"{float(t):.3f} crop x {int(round(float(x)))};\n")


# ------------------------------------------------------------------ asosiy

def reframe_to_vertical(
    src: str,
    out: str,
    src_w: int,
    src_h: int,
    duration: Optional[float] = None,
    max_kbps: Optional[int] = None,
    detector="auto",
) -> dict:
    """src -> out (9:16). Qaytaradi: {"path", "face_ratio", "crop_w"} yoki
    {"skipped": "already_narrow"} (video allaqachon vertikal bo'lsa).
    Xatoda RuntimeError."""
    crop_w = int(src_h * 9 / 16) // 2 * 2
    if crop_w >= src_w - 2:
        return {"skipped": "already_narrow"}

    if detector == "auto":
        detector = make_haar_detector()

    samples = detect_track(src, src_w, src_h, detector)
    detected = [s for s in samples if s is not None]
    face_ratio = (len(detected) / len(samples)) if samples else 0.0

    times, xs = compute_crop_x(samples, duration or 0.0, src_w, crop_w)
    cmd_path = os.path.join(os.path.dirname(os.path.abspath(out)), "vert_cmds.txt")
    _write_sendcmd(cmd_path, times, xs)

    out_h = OUT_HEIGHT // 2 * 2
    out_w = int(round(out_h * 9 / 16)) // 2 * 2
    vf = (
        f"sendcmd=f={cmd_path},"
        f"crop=w={crop_w}:h={src_h}:x={int(round(float(xs[0])))}:y=0,"
        f"scale={out_w}:{out_h}:flags=lanczos,setsar=1"
    )
    cmd = ["ffmpeg", "-y", "-i", src, "-vf", vf,
           "-c:v", "libx264", "-preset", VERT_PRESET, "-profile:v", "high", "-crf", VERT_CRF,
           "-pix_fmt", "yuv420p"]
    if max_kbps and max_kbps > 0:
        cmd += ["-maxrate", f"{int(max_kbps)}k", "-bufsize", f"{int(max_kbps) * 2}k"]
    cmd += ["-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", out]

    r = subprocess.run(cmd, capture_output=True, timeout=1500)
    if r.returncode != 0 or not os.path.exists(out) or os.path.getsize(out) == 0:
        raise RuntimeError(f"ffmpeg 9:16 kesishda xato: {r.stderr[-400:]!r}")
    return {"path": out, "face_ratio": face_ratio, "crop_w": crop_w}
