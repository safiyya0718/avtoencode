#!/usr/bin/env python3
"""Post kodlash — kodlash botining "Post kodlash" bo'limi (`worker/src/postbot.rs`).

ALOHIDA workflow (`avtoencode` repoda `.github/workflows/post.yml`, shablon —
`tool/post/post.workflow.yml`). Worker navbatda post bo'lsa uni o'zi ishga
tushiradi. Avto-kodlash (H.265, ilova qismlari) bilan bog'liq emas.

Run navbat bo'shaguncha postlarni KETMA-KET ishlaydi. Bitta post:
  1. rasm va video yopiq kanaldan yuklab olinadi (bot ko'chirgan postlar);
  2. `encode.sh` — `anime` repodagi "Encode (H265)" bilan bir xil (H.265,
     boshida 3 soniya rasm, burchakda logotip);
  3. tayyor video `<ID>_logo.png` fayl nomidagi ID'ga Telegram'da
     ochiladigan VIDEO bo'lib (fayl emas), tagida post nomi bilan yuboriladi;
  4. `/api/post/finish` — worker kanal postlarini va navbat yozuvini o'chiradi.
Jarayon botdagi bitta xabarda jonli ko'rinadi (`Live`, `/api/post/progress`).
"""

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORK = Path(os.environ.get("RUNNER_TEMP", "/tmp")) / "arugram_post"


class Lost(Exception):
    """Post o'chirildi yoki boshqa run'ga o'tdi."""


class Fatal(Exception):
    """Qayta urinishdan foyda yo'q."""


def api(path, body):
    base = os.environ["API_BASE"].rstrip("/")
    data = json.dumps(body).encode()
    err = None
    for attempt in range(5):
        req = urllib.request.Request(
            f"{base}/api/post/{path}", data=data, method="POST",
            headers={"X-Encode-Token": os.environ["ENCODE_TOKEN"],
                     "Content-Type": "application/json", "User-Agent": "arugram-encoder"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            if e.code == 409:
                raise Lost()
            if e.code in (400, 401, 403, 404):
                raise RuntimeError(f"post/{path}: HTTP {e.code}")
            err = e
        except Exception as e:  # tarmoq
            err = e
        time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"post/{path}: {err}")


def claim(runner):
    """Navbatdagi post (yo'q bo'lsa yoki worker eski bo'lsa — None)."""
    try:
        r = api("claim", {"runner": runner})
    except Exception as e:
        print("post/claim:", e, flush=True)
        return None
    if r.get("job") and r.get("channel"):
        return r
    return None


def target():
    """Video yuboriladigan ID — `<ID>_logo.png` fayl nomidan."""
    for p in sorted(HERE.glob("*_logo.png")):
        uid = p.name[: -len("_logo.png")]
        if re.fullmatch(r"-?\d+", uid):
            return int(uid), p
    raise Fatal("tool/post/<ID>_logo.png topilmadi")


def ffprobe(path, entry, stream=None):
    cmd = ["ffprobe", "-v", "error"]
    if stream:
        cmd += ["-select_streams", stream, "-show_entries", f"stream={entry}"]
    else:
        cmd += ["-show_entries", f"format={entry}"]
    cmd += ["-of", "csv=p=0", str(path)]
    try:
        v = subprocess.check_output(cmd, stderr=subprocess.DEVNULL).decode().split("\n")[0].strip()
        n = int(float(v))
        return n if n > 0 else None
    except Exception:
        return None


def hms(sec):
    sec = max(0, int(sec))
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    return f"{h}:{m:02}:{s:02}" if h else f"{m:02}:{s:02}"


def bar(pct, width=12):
    full = int(round(max(0.0, min(100.0, pct)) / 100 * width))
    return "\u2593" * full + "\u2591" * (width - full)


class Live:
    """Botdagi jonli holat xabari (`/api/post/progress`) — ilovadagi kodlash
    holati kabi: bosqich, foiz, tezlik, hajm, o'tgan/qolgan vaqt.
    Har 10 soniyada bittadan ko'p yuborilmaydi; xatosi kodlashga tegmaydi."""

    def __init__(self, r):
        job = r["job"]
        self.base = {"id": int(job["id"]), "name": str(job.get("name") or ""),
                     "attempt": int(job.get("attempt") or 1),
                     "status_msg": int(r.get("status_msg") or 0)}
        self.steps = []   # tugagan bosqichlar
        self.last = 0.0
        self.t0 = time.time()

    def done(self, line):
        self.steps.append("\u2705 " + line)

    def send(self, line, force=False):
        now = time.time()
        if not self.base["status_msg"] or (not force and now - self.last < 10):
            return
        self.last = now
        text = "\n".join(self.steps + [line, "", f"\u23F1 jami: {hms(now - self.t0)}"])
        try:
            api("progress", {**self.base, "text": text})
        except Exception as e:  # noqa: BLE001
            print("post/progress:", e, flush=True)

    def transfer(self, kind):
        t0 = time.time()

        def cb(cur, total):
            now = time.time()
            pct = cur * 100 / total if total else 0
            sp = cur / max(now - t0, 0.1) / 1048576
            eta = (total - cur) / 1048576 / sp if sp > 0 and total else 0
            self.send(f"{kind}\n{bar(pct)} {pct:.1f}%\n"
                      f"{cur / 1048576:.1f} / {total / 1048576:.1f} MB \u00B7 {sp:.2f} MB/s \u00B7 "
                      f"o'tdi {hms(now - t0)} \u00B7 qoldi ~{hms(eta)}", force=cur >= total)
        return cb


# encode.sh qatori: "🎬 [post_7] 37.5% | frm:900 | vaqt:00:00:37 | fps:81.0 | br:305.2kbps | 0.4MB | tezlik:3.37x"
ENC_RE = re.compile(r"\] ([\d.]+)% \| frm:(\d+) \| vaqt:\S+ \| fps:([\d.]+) \| br:([\d.]+)kbps \| ([\d.]+)MB \| tezlik:\s*([\d.]+)x")


def run_encode(args, log, live=None):
    """encode.sh — har qator Actions log'iga, har 30 soniyada bittasi kanal log'iga,
    jonli holat — botga."""
    p = subprocess.Popen(["bash", str(HERE / "encode.sh"), *map(str, args)],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    last = 0.0
    t0 = time.time()
    for line in p.stdout:
        line = line.rstrip()
        if not line:
            continue
        now = time.time()
        m = ENC_RE.search(line)
        if m and live:
            pct, fps, kbps, mb, speed = float(m[1]), m[3], m[4], float(m[5]), float(m[6])
            el = now - t0
            eta = el * (100 - pct) / pct if pct > 0.5 else 0
            est = f" (~{mb * 100 / pct:.0f} MB bo'ladi)" if pct > 3 else ""
            live.send(f"\u2699\uFE0F Kodlanmoqda (H.265)\n{bar(pct)} {pct:.1f}%\n"
                      f"tezlik {speed:.2f}x \u00B7 {fps} kadr/s \u00B7 bitreyt {kbps} kb/s\n"
                      f"hajm {mb:.1f} MB{est}\no'tdi {hms(el)} \u00B7 qoldi ~{hms(eta)}")
        if line.startswith("\U0001F3AC") and now - last < 30:
            print(line, flush=True)
            continue
        last = now
        log("  " + line)
    return p.wait()


async def process(app, r, runner, log):
    channel = int(r["channel"])
    job = r["job"]
    pid, name = int(job["id"]), str(job.get("name") or "").strip()
    live = Live(r)
    ident = {"runner": runner, "id": pid, "status_msg": live.base["status_msg"]}
    shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True)
    try:
        uid, logo = target()
        log(f"\U0001F3AC Post #{pid}: {name.splitlines()[0] if name else ''} "
            f"(urinish {job.get('attempt')}) -> {uid}")

        pm = await app.get_messages(channel, int(job["photo_msg"]))
        vm = await app.get_messages(channel, int(job["video_msg"]))
        if not pm or pm.empty or not (pm.photo or pm.document):
            raise Fatal("rasm kanalda topilmadi")
        if not vm or vm.empty or not (vm.video or vm.document):
            raise Fatal("video kanalda topilmadi")

        live.send("\u2B07\uFE0F Rasm va video yuklab olinmoqda...", force=True)
        raw = await app.download_media(pm, file_name=str(WORK / "cover.raw"))
        if not raw or Path(raw).stat().st_size == 0:
            raise RuntimeError("rasm yuklab olinmadi")
        log("  video yuklab olinmoqda...")
        src = await app.download_media(vm, file_name=str(WORK / "source.video"),
                                       progress=live.transfer("\u2B07\uFE0F Video yuklab olinmoqda"))
        if not src or Path(src).stat().st_size == 0:
            raise RuntimeError("video yuklab olinmadi")
        src_mb = Path(src).stat().st_size / 1048576
        sw, sh = ffprobe(src, "width", "v:0"), ffprobe(src, "height", "v:0")
        sd = ffprobe(src, "duration")
        live.done(f"Yuklab olindi: {src_mb:.1f} MB \u00B7 {sw or '?'}x{sh or '?'} \u00B7 {hms(sd or 0)}")

        # encode.sh rasmni PNG deb oladi; thumbnail — Telegram talabi (JPEG, 320px).
        cover = WORK / "cover.png"
        thumb = WORK / "thumb.jpg"
        r1 = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", raw, "-frames:v", "1", str(cover)])
        if r1.returncode != 0 or not cover.exists():
            raise Fatal("rasmni ochib bo'lmadi")
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(cover), "-vf",
                        "scale='min(320,iw)':-2", "-q:v", "5", str(thumb)])

        out = WORK / f"post_{pid}.mp4"
        log(f"  kodlanmoqda (H.265), manba {src_mb:.1f} MB...")
        t = time.time()
        live.send("\u2699\uFE0F Kodlash boshlanmoqda (H.265)...", force=True)
        code = await asyncio.to_thread(run_encode, [src, cover, logo, out], log, live)
        if code != 0 or not out.exists():
            raise RuntimeError("kodlashda xatolik (Actions log'iga qarang)")
        Path(src).unlink(missing_ok=True)
        size = out.stat().st_size
        log(f"  tayyor: {size / 1048576:.1f} MB, {int(time.time() - t)} s")
        live.done(f"Kodlandi: {src_mb:.1f} MB \u2192 {size / 1048576:.1f} MB \u00B7 {hms(time.time() - t)}")

        # Admin shu orada o'chirgan bo'lsa — yuborilmaydi.
        api("check", ident)
        extra = {}
        for k, v in (("duration", ffprobe(out, "duration")),
                     ("width", ffprobe(out, "width", "v:0")),
                     ("height", ffprobe(out, "height", "v:0"))):
            if v:
                extra[k] = v
        if thumb.exists() and thumb.stat().st_size > 0:
            extra["thumb"] = str(thumb)
        log(f"  {uid} ga yuborilmoqda...")
        first = (name.splitlines()[0] if name else f"post_{pid}")
        fname = re.sub(r'[\\/:*?"<>|]', "", first).strip()[:80] or f"post_{pid}"
        await app.send_video(
            uid, str(out), caption=name[:1024], file_name=f"{fname}.mp4",
            supports_streaming=True,
            progress=live.transfer(f"\u2B06\uFE0F {uid} ga yuborilmoqda"), **extra)
        api("finish", {**ident, "ok": True, "size": size, "to": str(uid)})
        log(f"  ✅ post #{pid} yuborildi")
    except Lost:
        log(f"  post #{pid} o'chirildi yoki boshqa run'ga o'tdi — yuborilmadi")
    except Fatal as ex:
        log(f"  post #{pid} XATO (qayta urinilmaydi): {ex}")
        try:
            api("finish", {**ident, "ok": False, "fatal": True, "error": str(ex)})
        except Exception:
            pass
    except Exception as ex:
        log(f"  post #{pid} XATO: {ex}")
        try:
            api("finish", {**ident, "ok": False, "error": str(ex)[:300]})
        except Exception:
            pass
    finally:
        shutil.rmtree(WORK, ignore_errors=True)


def cancel(runner, pid):
    """Run bekor qilindi — post navbatga qaytadi."""
    try:
        api("finish", {"runner": runner, "id": pid, "ok": False, "cancelled": True})
    except Exception:
        pass


SESSION = str(HERE / "pyro_session")
RUNNER = f"{os.environ.get('GITHUB_RUN_ID', 'local')}-{os.environ.get('GITHUB_RUN_ATTEMPT', '1')}"
# Shu vaqtdan keyin yangi post olinmaydi (Actions limiti 6 soat).
START_BUDGET = int(os.environ.get("START_BUDGET_MIN", "300")) * 60
T0 = time.time()
CURRENT = None


def session_api_id():
    """Sessiya faylidagi api_id (sessiya qaysi ilova bilan yaratilgan bo'lsa)."""
    try:
        import sqlite3
        c = sqlite3.connect(f"file:{SESSION}.session?mode=ro", uri=True)
        return int(c.execute("SELECT api_id FROM sessions").fetchone()[0] or 0)
    except Exception:
        return 0


def on_cancel(signum, frame):
    """Run bekor qilindi — ishlanayotgan post navbatga qaytadi."""
    if CURRENT:
        cancel(RUNNER, CURRENT)
        print("Run bekor qilindi — post navbatga qaytarildi", flush=True)
    os._exit(0)


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


async def main():
    global CURRENT
    import pyrogram.utils
    from pyrogram import Client
    # Pyrogram 2.0.106: yangi kanallar raqami eski chegaradan kichik ("Peer id invalid").
    pyrogram.utils.MIN_CHANNEL_ID = -1009999999999
    api_id = session_api_id() or int(os.environ["TG_API_ID"])
    app = Client(SESSION, api_id=api_id, api_hash=os.environ["TG_API_HASH"], no_updates=True)
    done = 0
    async with app:
        # Kanal va qabul qiluvchi Pyrogram peer keshida bo'lsin.
        async for _ in app.get_dialogs():
            pass
        while time.time() - T0 < START_BUDGET:
            r = claim(RUNNER)
            if not r:
                break
            CURRENT = int(r["job"]["id"])
            try:
                await process(app, r, RUNNER, log)
                done += 1
            finally:
                CURRENT = None
    log(f"Run tugadi: {done} ta post.")


if __name__ == "__main__":
    import signal
    signal.signal(signal.SIGINT, on_cancel)
    signal.signal(signal.SIGTERM, on_cancel)
    asyncio.run(main())
    sys.exit(0)
