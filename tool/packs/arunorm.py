"""Foydalanuvchi yuklagan rasmni to'plamga mos YENGIL WebP ga aylantiradi.

Maqsad — kuchsiz telefonlar ko'tara olsin:
  * o'lcham cheklanadi (stiker 512, emoji 128, GIF 480 px);
  * animatsiya sekundiga ko'pi bilan 20 kadr (ortiqcha kadrlar tashlanadi,
    vaqti oldingi kadrga qo'shiladi — tezlik o'zgarmaydi);
  * kadrlar soni va uzunlik CHEKLANMAYDI (faqat 5 MB); rasm ham,
    animatsiya ham qabul qilinadi;
  * har elementga kichik statik rasm (thumb) yasaladi: to'plam oynasida
    faqat shu ko'rinadi, animatsiya esa faqat kerak bo'lganda ochiladi.

Kiruvchi turlar: PNG, JPEG, GIF, WebP (animatsiyali ham) va VIDEO (MP4/MOV,
WebM). Fayl turi kengaytmadan emas, BAYTLARIDAN aniqlanadi. Video `ffmpeg`
bilan kadrlarga ajratiladi (foydalanuvchi ilovada tanlagan bo'lak — `trim`
— kesib olinadi, ovoz tashlanadi, emoji uchun markazdan kvadrat kesiladi),
so'ng rasm animatsiyasi bilan bir xil yo'l. Chiqish hajmi <= 5 MB
(`arupack.MAX_ITEM`).
"""

import io
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageSequence, UnidentifiedImageError

import arupack

# Ochiq holatda shuncha piksel/kadr o'qiladi — "dekompressiya bombasi"dan himoya.
Image.MAX_IMAGE_PIXELS = 40_000_000

LIMITS = {
    #           eng katta tomon, kadrlar, soniya, yumshoq hajm chegarasi
    # Kadrlar soni va uzunlik CHEKLANMAYDI (foydalanuvchi talabi); faqat 5 MB.
    # Emoji eng kichik, stiker o'rtacha, GIF eng katta (va xilma-xil nisbatda).
    "sticker": dict(side=384, soft=512 * 1024),
    "emoji":   dict(side=128, soft=256 * 1024),
    "gif":     dict(side=640, soft=3 * 1024 * 1024),
}
MAX_FPS = 20
# Kichik rasm (thumb) o'lchami: GIF devorida katta ko'rinadi, shu sabab kattaroq.
THUMB_SIDE = {"sticker": 192, "emoji": 72, "gif": 256}
QUALITIES = (80, 65, 50, 40, 30, 20)
SCALES = (1.0, 0.8, 0.65, 0.5, 0.35, 0.25)


class Rejected(Exception):
    """Element to'plamga mos emas — matn egasiga ko'rsatiladi."""


@dataclass
class Result:
    data: bytes
    thumb: bytes
    animated: bool
    w: int
    h: int
    # Ovozli MP4 (faqat GIF to'plami): data — mp4 baytlari.
    video: bool = False


def sniff(b: bytes) -> str:
    if b[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if b[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if b[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        return "webp"
    if b[4:8] == b"ftyp":
        return "mp4"
    if b[:4] == b"\x1a\x45\xdf\xa3":
        return "webm"
    return ""


def _fit(w: int, h: int, side: int):
    k = min(1.0, side / max(w, h))
    return max(1, round(w * k)), max(1, round(h * k))


def _frames(im: Image.Image, size, square=False):
    """(RGBA kadr, davomiyligi ms) ro'yxati. Xotira to'lmasligi uchun har kadr
    o'qilishi bilan `size` (w, h) gacha kichraytiriladi."""
    frames = []
    for fr in ImageSequence.Iterator(im):
        d = fr.info.get("duration", im.info.get("duration", 100))
        d = float(d) if d and d > 0 else 100.0
        # 10 ms dan qisqa kadrlar brauzerlarda 100 ms ga aylanadi — biz ham.
        if d < 20:
            d = 100.0
        rgba = fr.convert("RGBA")
        if square:
            m = min(rgba.size)
            l, t = (rgba.width - m) // 2, (rgba.height - m) // 2
            rgba = rgba.crop((l, t, l + m, t + m))
        if rgba.size != size:
            rgba = rgba.resize(size, Image.LANCZOS)
        frames.append((rgba, d))
    return frames


VIDEO_FPS = 15


def _run(cmd, timeout=180):
    return subprocess.run(cmd, capture_output=True, timeout=timeout, check=True)


def _video_frames(raw: bytes, kind: str, lim: dict, trim):
    """Video -> (RGBA kadr, ms) ro'yxati va manba o'lchami. `trim` — (boshi_ms,
    oxiri_ms) yoki None."""
    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / "in.bin"
        src.write_bytes(raw)
        try:
            out = _run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "stream=width,height:format=duration",
                        "-of", "csv=p=0", str(src)], 60).stdout.decode().split()
            wh, dur = out[0].split(","), None
            w0, h0 = int(wh[0]), int(wh[1])
            dur = float(out[1].split(",")[-1]) if len(out) > 1 else float(wh[2])
        except Exception:
            raise Rejected("video o'qilmadi")
        if w0 <= 0 or h0 <= 0 or dur <= 0:
            raise Rejected("video o'lchami yoki davomiyligi noto'g'ri")
        start, end = 0.0, dur
        if trim:
            start = max(0.0, min(dur, trim[0] / 1000.0))
            end = max(start, min(dur, trim[1] / 1000.0))
        length = end - start
        if length < 0.2:
            raise Rejected("tanlangan bo'lak juda qisqa")
        side = lim["side"]
        vf = []
        if kind == "emoji":
            vf.append("crop='min(iw,ih)':'min(iw,ih)'")
        vf.append(f"fps={VIDEO_FPS}")
        vf.append(f"scale='min({side},iw)':'min({side},ih)':force_original_aspect_ratio=decrease")
        try:
            _run(["ffmpeg", "-v", "error", "-y", "-ss", f"{start:.3f}", "-t", f"{length:.3f}",
                  "-i", str(src), "-an", "-vf", ",".join(vf),
                  str(Path(d) / "f%05d.png")], 600)
        except Exception:
            raise Rejected("videoni qayta ishlab bo'lmadi")
        files = sorted(Path(d).glob("f*.png"))
        if not files:
            raise Rejected("videodan kadr olinmadi")
        frames = []
        for f in files:
            with Image.open(f) as im:
                frames.append((im.convert("RGBA"), 1000.0 / VIDEO_FPS))
        w1, h1 = frames[0][0].size
        return frames, w1, h1


def _has_audio(src: Path) -> bool:
    try:
        out = _run(["ffprobe", "-v", "error", "-select_streams", "a:0",
                    "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(src)], 60)
        return b"audio" in out.stdout
    except Exception:
        return False


def _video_keep_audio(raw: bytes, lim: dict, trim):
    """GIF to'plami uchun: videoda OVOZ bo'lsa, u saqlanadi — H.265 (HEVC) + AAC MP4
    (<= 5 MB). Ovoz bo'lmasa `None` (oddiy yengil WebP yo'li ishlaydi)."""
    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / "in.bin"
        src.write_bytes(raw)
        if not _has_audio(src):
            return None
        try:
            out = _run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "stream=width,height:format=duration",
                        "-of", "csv=p=0", str(src)], 60).stdout.decode().split()
            wh = out[0].split(",")
            w0, h0 = int(wh[0]), int(wh[1])
            dur = float(out[1].split(",")[-1]) if len(out) > 1 else float(wh[2])
        except Exception:
            raise Rejected("video o'qilmadi")
        if w0 <= 0 or h0 <= 0 or dur <= 0:
            raise Rejected("video o'lchami yoki davomiyligi noto'g'ri")
        start, end = 0.0, dur
        if trim:
            start = max(0.0, min(dur, trim[0] / 1000.0))
            end = max(start, min(dur, trim[1] / 1000.0))
        length = end - start
        if length < 0.2:
            raise Rejected("tanlangan bo'lak juda qisqa")
        best = None
        for side, crf, ab in ((lim["side"], 28, "64k"), (lim["side"], 32, "48k"),
                              (int(lim["side"] * 0.75), 34, "48k"),
                              (int(lim["side"] * 0.5), 36, "32k"),
                              (int(lim["side"] * 0.35), 38, "32k")):
            dst = Path(d) / "out.mp4"
            vf = (f"fps=24,scale='min({side},iw)':'min({side},ih)':"
                  f"force_original_aspect_ratio=decrease:force_divisible_by=16")
            base = ["ffmpeg", "-v", "error", "-y", "-ss", f"{start:.3f}", "-t", f"{length:.3f}",
                    "-i", str(src), "-vf", vf]
            tail = ["-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", ab, "-ac", "1",
                    "-movflags", "+faststart", str(dst)]
            try:
                # H.265 (HEVC): xuddi shu sifatda H.264 dan ~40% kichik.
                _run(base + ["-c:v", "libx265", "-preset", "fast", "-crf", str(crf + 4),
                             "-tag:v", "hvc1", "-x265-params", "log-level=error"] + tail, 900)
            except Exception:
                try:  # x265 yo'q/yiqilsa — H.264
                    _run(base + ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf)] + tail, 600)
                except Exception:
                    raise Rejected("videoni qayta ishlab bo'lmadi")
            data = dst.read_bytes()
            best = data
            if len(data) <= arupack.MAX_ITEM:
                break
        if best is None or len(best) > arupack.MAX_ITEM:
            return None  # sig'madi — ovozsiz WebP yo'li o'zi moslashtiradi
        # kichik rasm (thumb) va o'lcham — birinchi kadrdan
        png = Path(d) / "t.png"
        try:
            _run(["ffmpeg", "-v", "error", "-y", "-i", str(dst), "-frames:v", "1", str(png)], 60)
            with Image.open(png) as im:
                w1, h1 = im.size
                tw, th = _fit(w1, h1, THUMB_SIDE["gif"])
                tb = io.BytesIO()
                im.convert("RGBA").resize((tw, th), Image.LANCZOS).save(
                    tb, "WEBP", quality=60, method=4)
        except Exception:
            raise Rejected("videodan kadr olinmadi")
        return Result(data=best, thumb=tb.getvalue(), animated=True, w=w1, h=h1, video=True)


def _drop_fast(frames):
    """Sekundiga MAX_FPS dan ortiq kadrlarni tashlaydi (vaqt saqlanadi)."""
    step = 1000.0 / MAX_FPS
    out = []
    for img, d in frames:
        if out and out[-1][1] < step:
            out[-1][1] += d
            continue
        out.append([img, d])
    return out


def _encode(frames, scale, quality, side_w, side_h):
    w, h = max(1, round(side_w * scale)), max(1, round(side_h * scale))
    imgs = [f[0].resize((w, h), Image.LANCZOS) if (w, h) != f[0].size else f[0]
            for f in frames]
    buf = io.BytesIO()
    if len(imgs) == 1:
        imgs[0].save(buf, "WEBP", quality=quality, method=4)
    else:
        imgs[0].save(buf, "WEBP", save_all=True, append_images=imgs[1:],
                     duration=[int(f[1]) for f in frames], loop=0,
                     quality=quality, method=4, minimize_size=False)
    return buf.getvalue(), w, h


def _probe_video(raw: bytes) -> bool:
    """Noma'lum formatdagi fayl ffmpeg o'qiy oladigan video-mi."""
    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / "in.bin"
        src.write_bytes(raw)
        try:
            out = _run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(src)], 60)
            return b"video" in out.stdout
        except Exception:
            return False


def normalize(raw: bytes, kind: str, emoji: str = "", trim=None, mute=False) -> Result:
    """Foydalanuvchi faylini to'plam elementiga aylantiradi (yoki `Rejected`).
    `trim` — video uchun (boshi_ms, oxiri_ms)."""
    if kind not in LIMITS:
        raise Rejected("noto'g'ri tur")
    lim = LIMITS[kind]
    fmt = sniff(raw)
    if not fmt:
        # Mos kelmagan format: rasm bo'lsa Pillow, video bo'lsa ffmpeg moslashtiradi.
        try:
            with Image.open(io.BytesIO(raw)) as probe:
                probe.verify()
            fmt = "image"
        except Exception:
            fmt = "mp4" if _probe_video(raw) else ""
    if not fmt:
        raise Rejected("fayl o'qilmadi: rasm yoki video emas")
    if len(raw) > arupack.MAX_ITEM:
        raise Rejected("fayl 5 MB dan katta")
    try:
        if fmt in ("mp4", "webm"):
            if kind == "gif" and not mute:
                keep = _video_keep_audio(raw, lim, trim)
                if keep is not None:
                    return keep
            frames, w0, h0 = _video_frames(raw, kind, lim, trim)
        else:
            im = Image.open(io.BytesIO(raw))
            w0, h0 = im.size
            if w0 <= 0 or h0 <= 0:
                raise Rejected("rasm o'lchami noto'g'ri")
            if kind == "emoji":
                w0 = h0 = min(w0, h0)
            frames = _frames(im, _fit(w0, h0, lim["side"]), square=(kind == "emoji"))
    except Rejected:
        raise
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, EOFError,
            Image.DecompressionBombError) as e:
        raise Rejected(f"fayl o'qilmadi ({type(e).__name__})")

    if not frames:
        raise Rejected("rasm bo'sh")
    animated = len(frames) > 1
    if animated:
        frames = _drop_fast(frames)
        animated = len(frames) > 1
    fw, fh = _fit(w0, h0, lim["side"])

    best = None
    # Sig'masa moslashtiriladi: avval sifat/o'lcham pasayadi, keyin kadrlar
    # siyraklashtiriladi (vaqt saqlanadi) — rad etilmaydi.
    for _thin in range(5):
        for scale in SCALES:
            for q in QUALITIES:
                data, w, h = _encode(frames, scale, q, fw, fh)
                if best is None or len(data) < len(best[0]):
                    best = (data, w, h)
                if len(data) <= lim["soft"]:
                    best = (data, w, h)
                    break
            else:
                continue
            break
        if len(best[0]) <= arupack.MAX_ITEM or len(frames) < 4 or _thin == 4:
            break
        frames = [[frames[i][0], frames[i][1] + (frames[i + 1][1] if i + 1 < len(frames) else 0)]
                  for i in range(0, len(frames), 2)]
        best = None
    data, w, h = best
    if len(data) > arupack.MAX_ITEM:
        raise Rejected("eng past sifatga tushirilgandan keyin ham 5 MB dan katta")

    tw, th = _fit(w0, h0, THUMB_SIDE[kind])
    tb = io.BytesIO()
    frames[0][0].resize((tw, th), Image.LANCZOS).save(tb, "WEBP", quality=60, method=4)
    return Result(data=data, thumb=tb.getvalue(), animated=animated, w=w, h=h)
