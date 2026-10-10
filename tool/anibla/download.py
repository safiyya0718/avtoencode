#!/usr/bin/env python3
"""anibla.uz videolarini yuklab olish — kodlash botining "Anibla yuklash" bo'limi
(`worker/src/anibla.rs`).

ALOHIDA workflow (`avtoencode` repoda `.github/workflows/anibla.yml`, shablon —
`tool/anibla/anibla.workflow.yml`). Sifat tugmasi bosilganda video bazadagi
navbatga (`anibla_jobs`) tushadi va worker shu workflow'ni ishga tushiradi.
Run navbat bo'shaguncha videolarni KETMA-KET oladi (`/api/anibla/claim`).

Bitta video:
  1. ffmpeg HLS bo'laklarini qayta kodlamasdan (`-c copy`) bitta mp4 ga yig'adi;
  2. Telegram sessiyasi bilan yopiq kanalga video bo'lib yuklanadi;
  3. `/api/anibla/done` — kodlash boti videoni BOT CHATIGA ko'chiradi va
     kanal postini o'chiradi, navbat yozuvi o'chadi.
Jarayon botdagi holat xabarida jonli ko'rinadi (`/api/anibla/progress`).
"""

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORK = Path(os.environ.get("RUNNER_TEMP", "/tmp")) / "anibla"
SESSION = str(HERE / "pyro_session")
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
# Telegram oddiy akkaunt uchun fayl chegarasi — 2 GB.
MAX_BYTES = 2000 * 1048576
RUNNER = f"{os.environ.get('GITHUB_RUN_ID', 'local')}-{os.environ.get('GITHUB_RUN_ATTEMPT', '1')}"
# Shu vaqtdan keyin yangi video olinmaydi (Actions limiti 6 soat).
START_BUDGET = int(os.environ.get("START_BUDGET_MIN", "280")) * 60
T0 = time.time()
CURRENT = None
# Botdagi holat xabari shuncha soniyada bir tahrirlanadi (foydalanuvchi talabi:
# "har 5 soniyada"). Telegram cheklasa — worker aytgan vaqtcha kutiladi.
LIVE_SEC = float(os.environ.get("LIVE_SEC", "5"))
# Holat xabarida ko'rinadigan oxirgi log qatorlari.
LOG_LINES = int(os.environ.get("LOG_LINES", "4"))
LIVE = None
# Saytdan bir vaqtda yuklanadigan HLS bo'laklari soni (foydalanuvchi: sayt
# 10+ MB/s bera oladi, bitta oqim ~2 MB/s).
PARALLEL = int(os.environ.get("PARALLEL", "12"))
# `external-hls` (alohida o'zbekcha ovozli qismlar) bo'laklari sayt orqali
# boshqa serverdan olinadi: har biri ~2.3 s kutadi, tezlik oqimlar soniga
# to'g'ri proporsional. Sinov: 12 oqim — 2.5 MB/s, 32 — 6.5, 48 — 8.1, 64 — 8.0.
PARALLEL_EXT = int(os.environ.get("PARALLEL_EXT", "48"))


class Lost(Exception):
    """Video navbatdan olib tashlandi yoki boshqa run'ga o'tdi."""


class Fatal(Exception):
    """Qayta urinishdan foyda yo'q."""


class Job:
    def __init__(self, r):
        j = r["job"]
        self.id = int(j["id"])
        self.url = str(j.get("url") or "").strip()
        self.caption = str(j.get("caption") or "").strip()
        name = re.sub(r'[\\/:*?"<>|]', "", str(j.get("file_name") or "")).strip()[:90]
        self.file_name = name or f"video_{self.id}"
        self.status = int(j.get("status_msg") or 0)
        self.chat = int(j.get("chat") or 0)
        self.attempt = int(j.get("attempt") or 1)
        self.channel = int(r.get("channel") or 0)

    def ident(self):
        return {"runner": RUNNER, "id": self.id, "status_msg": self.status, "chat": self.chat}


def log(*a):
    line = " ".join(str(x) for x in a)
    print(time.strftime("%H:%M:%S"), line, flush=True)
    if LIVE:
        LIVE.add_log(line)


def api(path, body):
    base = os.environ["API_BASE"].rstrip("/")
    data = json.dumps(body).encode()
    err = None
    for attempt in range(4):
        req = urllib.request.Request(
            f"{base}/api/anibla/{path}", data=data, method="POST",
            headers={"X-Encode-Token": os.environ["ENCODE_TOKEN"],
                     "Content-Type": "application/json", "User-Agent": "arugram-anibla"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            if e.code == 409:
                raise Lost()
            if e.code in (400, 401, 403, 404):
                raise RuntimeError(f"anibla/{path}: HTTP {e.code}")
            err = e
        except Exception as e:  # tarmoq
            err = e
        time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"anibla/{path}: {err}")


def hms(sec):
    sec = max(0, int(sec))
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    return f"{h}:{m:02}:{s:02}" if h else f"{m:02}:{s:02}"


def bar(pct, width=12):
    full = int(round(max(0.0, min(100.0, pct)) / 100 * width))
    return "▓" * full + "░" * (width - full)


class Live:
    """Botdagi holat xabari: bosqich, foiz, tezlik, hajm, qolgan vaqt va
    oxirgi log qatorlari. Alohida oqim har `LIVE_SEC` soniyada yuboradi —
    yuklash va Telegram'ga yuklash to'xtab qolmaydi. Xatosi ishga tegmaydi."""

    def __init__(self, job):
        global LIVE
        self.job = job
        self.head2 = f"(urinish {job.attempt})" if job.attempt > 1 else ""
        self.steps = []
        self.logs = deque(maxlen=LOG_LINES)
        self.line = "\u23F3 boshlanmoqda..."
        self.t0 = time.time()
        self.sent = ""
        self.pause_until = 0.0
        self.lock = threading.Lock()
        self.stop = threading.Event()
        LIVE = self
        if job.status:
            threading.Thread(target=self._loop, daemon=True).start()

    def text(self, line=None):
        with self.lock:
            # Sarlavhani ("⬇️ Yuklash #4: Nomi") worker qo'yadi — "Post kodlash"
            # holati bilan bir xil ko'rinish.
            parts = ([self.head2] if self.head2 else []) + self.steps + [self.line if line is None else line]
            if self.logs:
                parts += ["", "\U0001F4DC Log:"] + list(self.logs)
        parts += ["", f"\u23F1 jami: {hms(time.time() - self.t0)}"]
        return "\n".join(parts)[-3500:]

    def add_log(self, line):
        line = line.strip()
        if line:
            with self.lock:
                self.logs.append(f"{time.strftime('%H:%M:%S')} {line[:160]}")

    def done(self, line):
        with self.lock:
            self.steps.append("\u2705 " + line)

    def send(self, line, force=False):
        with self.lock:
            self.line = line
        if force:
            self._push()

    def _push(self):
        if not self.job.status or time.time() < self.pause_until:
            return
        text = self.text()
        if text == self.sent:
            return
        try:
            r = api("progress", {**self.job.ident(), "text": text})
            wait = int(r.get("retry_after") or 0)
            if wait:
                self.pause_until = time.time() + wait
            else:
                self.sent = text
        except Exception as e:  # noqa: BLE001
            print("progress:", e, flush=True)

    def _loop(self):
        while not self.stop.wait(LIVE_SEC):
            self._push()

    def close(self):
        global LIVE
        self.stop.set()
        if LIVE is self:
            LIVE = None

    def transfer(self, kind):
        t0 = time.time()
        state = {"log": 0.0}

        def cb(cur, total):
            now = time.time()
            pct = cur * 100 / total if total else 0
            sp = cur / max(now - t0, 0.1) / 1048576
            eta = (total - cur) / 1048576 / sp if sp > 0 and total else 0
            self.send(f"{kind}\n{bar(pct)} {pct:.1f}%\n"
                      f"tezlik {sp:.2f} MB/s\n"
                      f"hajm {cur / 1048576:.1f} / {total / 1048576:.1f} MB\n"
                      f"o'tdi {hms(now - t0)} \u00B7 qoldi ~{hms(eta)}")
            if now - state["log"] >= 10 or cur >= total:
                state["log"] = now
                log(f"Telegram'ga {pct:.1f}% \u00B7 {cur / 1048576:.1f}/{total / 1048576:.1f} MB \u00B7 {sp:.2f} MB/s")
        return cb


def playlist_duration(url):
    """Variant playlist'idagi bo'laklar uzunligi (soniya)."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=60) as r:
            text = r.read().decode(errors="replace")
        return sum(float(m) for m in re.findall(r"#EXTINF:([\d.]+)", text))
    except Exception as e:  # noqa: BLE001
        log("playlist:", e)
        return 0.0


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


def fetch(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def segments(url):
    """Variant playlist'idagi bo'laklar: [(to'liq manzil, uzunlik)].
    Shifrlangan (EXT-X-KEY), fMP4 (EXT-X-MAP) yoki bayt oralig'i bo'lsa — None
    (unda ffmpeg o'zi yuklaydi)."""
    text = fetch(url).decode(errors="replace")
    if any(t in text for t in ("#EXT-X-KEY", "#EXT-X-MAP", "#EXT-X-BYTERANGE", "#EXT-X-STREAM-INF")):
        return None
    segs, dur = [], 0.0
    for ln in text.splitlines():
        ln = ln.strip()
        if ln.startswith("#EXTINF:"):
            try:
                dur = float(ln[8:].split(",")[0])
            except ValueError:
                dur = 0.0
        elif ln and not ln.startswith("#"):
            segs.append((urllib.parse.urljoin(url, ln), dur))
            dur = 0.0
    return segs or None


def download(url, out, live):
    """HLS -> mp4. Bo'laklar `PARALLEL` tadan bir vaqtda yuklanadi, ketma-ket
    bitta .ts ga qo'shiladi va ffmpeg qayta kodlamasdan mp4 ga o'tkazadi.

    `url` — `video` yoki `video\naudio`: ba'zi qismlarda o'zbekcha ovoz ALOHIDA
    playlist (worker `#EXT-X-MEDIA` dan tanlaydi), video bo'laklari ichidagi
    ovoz esa boshqa til (ruscha). Shunda video faqat videosi bilan, ovoz esa
    alohida playlistdan olinadi.
    Playlist g'ayrioddiy bo'lsa — eski usul (ffmpeg o'zi, bitta oqim)."""
    vurl, _, aurl = url.partition("\n")
    vurl, aurl = vurl.strip(), aurl.strip()
    try:
        vsegs = segments(vurl)
        asegs = segments(aurl) if aurl else []
    except Exception as e:  # noqa: BLE001
        log("playlist:", e)
        vsegs = asegs = None
    if not vsegs or asegs is None:
        log("parallel yuklab bo'lmaydi — ffmpeg o'zi yuklaydi")
        return download_ffmpeg(vurl, out, live, aurl)
    if aurl:
        log(f"  alohida ovoz yo'li: {len(asegs)} bo'lak (video ichidagi ovoz tashlanadi)")
    par = PARALLEL_EXT if "external-hls" in vurl else PARALLEL
    jobs = [("v", k, u, d) for k, (u, d) in enumerate(vsegs)] + [("a", k, u, d) for k, (u, d) in enumerate(asegs)]
    total = sum(d for _, _, _, d in jobs)
    n = len(jobs)
    log(f"  yuklanmoqda: {n} bo'lak, {hms(sum(d for _, d in vsegs))}, {par} tadan parallel")
    parts = WORK / "parts"
    parts.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    lock = threading.Lock()
    st = {"bytes": 0, "done": 0, "dur": 0.0, "log": 0.0}

    def one(job):
        kind, idx, seg_url, d = job
        dest = parts / f"{kind}{idx:06d}.ts"
        err = None
        for attempt in range(6):
            try:
                data = fetch(seg_url, timeout=90)
                if not data:
                    raise RuntimeError("bo'sh javob")
                dest.write_bytes(data)
                with lock:
                    st["bytes"] += len(data)
                    st["done"] += 1
                    st["dur"] += d
                return
            except Exception as e:  # noqa: BLE001
                err = e
                time.sleep(min(2 * (attempt + 1), 10))
        raise RuntimeError(f"{'ovoz' if kind == 'a' else 'video'} {idx + 1}-bo'lagi yuklanmadi: {err}")

    with ThreadPoolExecutor(max_workers=par) as ex:
        pending = {ex.submit(one, jb) for jb in jobs}
        while pending:
            done_now = {f for f in pending if f.done()}
            for f in done_now:
                f.result()  # xato bo'lsa — shu yerda chiqadi
            pending -= done_now
            with lock:
                b, c, dsec = st["bytes"], st["done"], st["dur"]
            el = time.time() - t0
            pct = dsec * 100 / total if total else c * 100 / n
            sp = b / max(el, 0.1) / 1048576
            eta = el * (100 - pct) / pct if pct > 0.5 else 0
            est = f" (~{b / 1048576 * 100 / pct:.0f} MB bo'ladi)" if pct > 3 else ""
            live.send(f"\u2B07\uFE0F Saytdan yuklanmoqda ({par} oqim)\n{bar(pct)} {pct:.1f}%\n"
                      f"tezlik {sp:.2f} MB/s \u00B7 bo'lak {c}/{n}\n"
                      f"hajm {b / 1048576:.1f} MB{est}\n"
                      f"o'tdi {hms(el)} \u00B7 qoldi ~{hms(eta)}")
            if time.time() - st["log"] >= 10:
                st["log"] = time.time()
                log(f"saytdan {pct:.1f}% \u00B7 {b / 1048576:.1f} MB \u00B7 {sp:.2f} MB/s \u00B7 bo'lak {c}/{n}")
            if pending:
                time.sleep(0.5)
    el = time.time() - t0
    log(f"  bo'laklar tayyor: {st['bytes'] / 1048576:.1f} MB, {hms(el)}, "
        f"o'rtacha {st['bytes'] / max(el, 0.1) / 1048576:.2f} MB/s")

    # Bo'laklar ketma-ket bitta .ts ga (MPEG-TS bo'laklarini shunday qo'shish to'g'ri).
    def join(kind, count):
        dst = WORK / f"{kind}.ts"
        with open(dst, "wb") as w:
            for k in range(count):
                f = parts / f"{kind}{k:06d}.ts"
                with open(f, "rb") as r:
                    shutil.copyfileobj(r, w, 4 * 1048576)
                f.unlink()
        return dst
    vts = join("v", len(vsegs))
    ats = join("a", len(asegs)) if asegs else None
    live.send("\U0001F9E9 Bo'laklar mp4 ga yig'ilmoqda...")
    log("  mp4 ga yig'ilmoqda (qayta kodlamasdan)" + (", o'zbekcha ovoz bilan" if ats else "") + "...")
    cmd = ["ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "error", "-i", str(vts)]
    if ats:
        cmd += ["-i", str(ats), "-map", "0:v:0", "-map", "1:a:0"]
    else:
        cmd += ["-map", "0:v:0?", "-map", "0:a?"]
    cmd += ["-c", "copy", "-bsf:a", "aac_adtstoasc", "-movflags", "+faststart", str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    vts.unlink(missing_ok=True)
    if ats:
        ats.unlink(missing_ok=True)
    if r.returncode != 0 or not out.exists() or out.stat().st_size == 0:
        raise RuntimeError("mp4 ga yig'ib bo'lmadi: " + ((r.stderr or "").strip().splitlines() or ["?"])[-1][:200])
    return time.time() - t0


def download_ffmpeg(url, out, live, audio=""):
    """HLS -> mp4 (qayta kodlamasdan). ffmpeg `-progress` dan foiz.
    `audio` — alohida ovoz playlisti (bo'lsa, videoning ichki ovozi o'rniga)."""
    total = playlist_duration(url)
    log(f"  yuklanmoqda: {url} ({hms(total)})")
    cmd = ["ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "warning", "-progress", "pipe:1",
           "-user_agent", UA, "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "10",
           "-i", url] + (["-user_agent", UA, "-i", audio, "-map", "0:v:0", "-map", "1:a:0"] if audio
                         else ["-map", "0:v:0?", "-map", "0:a?"]) + ["-c", "copy",
           "-bsf:a", "aac_adtstoasc", "-movflags", "+faststart", str(out)]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
    errs = deque(maxlen=20)

    def read_err():
        for ln in p.stderr:
            ln = ln.strip()
            if ln:
                errs.append(ln)
                log("ffmpeg:", ln)
    reader = threading.Thread(target=read_err, daemon=True)
    reader.start()
    t0 = time.time()
    last_log = 0.0
    cur_t = size = 0
    for line in p.stdout:
        k, _, v = line.strip().partition("=")
        if k == "out_time_us" and v.isdigit():
            cur_t = int(v) / 1e6
        elif k == "total_size" and v.isdigit():
            size = int(v)
        elif k == "progress":
            el = time.time() - t0
            pct = cur_t * 100 / total if total else 0
            eta = el * (100 - pct) / pct if pct > 0.5 else 0
            sp = size / max(el, 0.1) / 1048576
            est = f" (~{size / 1048576 * 100 / pct:.0f} MB bo'ladi)" if pct > 3 else ""
            live.send(f"⬇️ Saytdan yuklab olinmoqda\n{bar(pct)} {pct:.1f}%\n"
                      f"{size / 1048576:.1f} MB{est} · {sp:.2f} MB/s\n"
                      f"o'tdi {hms(el)} · qoldi ~{hms(eta)}")
            if time.time() - last_log >= 10:
                last_log = time.time()
                log(f"saytdan {pct:.1f}% · {size / 1048576:.1f} MB · {sp:.2f} MB/s · {hms(cur_t)}/{hms(total)}")
    code = p.wait()
    reader.join(timeout=5)
    if code != 0 or not out.exists() or out.stat().st_size == 0:
        raise RuntimeError("ffmpeg xatosi: " + (list(errs) or ["?"])[-1][:200])
    return time.time() - t0


# TELEGRAM'GA TEZ YUKLASH (foydalanuvchi talabi). Pyrogram katta faylni BITTA
# ulanishda (4 ta so'rov navbatda) yuboradi. Bu yerda bo'laklar (512 KB) `UP_CONN`
# ta alohida ulanish x `UP_WORKERS` ta so'rov bilan parallel yuboriladi. Har
# bo'lak xato/FloodWait bo'lsa qayta yuboriladi (Pyrogram'niki xatoni yutib
# yuborardi). Biror sabab bilan ishlamasa — Pyrogram'ning o'z usuli.
UP_CONN = int(os.environ.get("UP_CONN", "4"))
UP_WORKERS = int(os.environ.get("UP_WORKERS", "4"))
PART = 512 * 1024


async def fast_save_file(app, orig, path, progress=None, progress_args=(), file_id=None, file_part=0):
    import math
    from pyrogram import raw
    from pyrogram.errors import FloodWait
    from pyrogram.session import Session
    size = os.path.getsize(path) if isinstance(path, (str, Path)) else 0
    # Kichik fayl (rasm), qayta yuborish (`file_id`) yoki fayl yo'li emas — asl usul.
    if file_id is not None or size <= 10 * 1048576 or UP_CONN <= 1:
        return await orig(path, progress=progress, progress_args=progress_args, file_id=file_id, file_part=file_part)
    total = int(math.ceil(size / PART))
    fid = app.rnd_id()
    dc, key, test = await app.storage.dc_id(), await app.storage.auth_key(), await app.storage.test_mode()
    sessions = [Session(app, dc, key, test, is_media=True) for _ in range(UP_CONN)]
    queue = asyncio.Queue()
    for i in range(total):
        queue.put_nowait(i)
    sent = [0]

    async def worker(sess):
        with open(path, "rb") as f:
            while True:
                try:
                    i = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                f.seek(i * PART)
                chunk = f.read(PART)
                for attempt in range(8):
                    try:
                        await sess.invoke(raw.functions.upload.SaveBigFilePart(
                            file_id=fid, file_part=i, file_total_parts=total, bytes=chunk))
                        break
                    except FloodWait as e:
                        log(f"Telegram FloodWait {e.value} s")
                        await asyncio.sleep(int(e.value) + 1)
                    except Exception as e:  # noqa: BLE001
                        if attempt == 7:
                            raise RuntimeError(f"{i}-bo'lak Telegram'ga yuborilmadi: {e}")
                        await asyncio.sleep(2 * (attempt + 1))
                sent[0] += len(chunk)
                if progress:
                    progress(min(sent[0], size), size, *progress_args)

    started = []
    try:
        for sess in sessions:
            await sess.start()
            started.append(sess)
        log(f"Telegram'ga {UP_CONN} ulanish x {UP_WORKERS} so'rov bilan yuklanmoqda ({total} bo'lak)")
        await asyncio.gather(*[worker(sess) for sess in started for _ in range(UP_WORKERS)])
    finally:
        for sess in started:
            try:
                await sess.stop()
            except Exception:  # noqa: BLE001
                pass
    return raw.types.InputFileBig(id=fid, parts=total, name=os.path.basename(str(path)))


async def upload(app, job, out, thumb, live):
    extra = {}
    for k, v in (("duration", ffprobe(out, "duration")),
                 ("width", ffprobe(out, "width", "v:0")),
                 ("height", ffprobe(out, "height", "v:0"))):
        if v:
            extra[k] = v
    if thumb.exists() and thumb.stat().st_size > 0:
        extra["thumb"] = str(thumb)
    orig = app.save_file

    async def patched(path, progress=None, progress_args=(), file_id=None, file_part=0):
        return await fast_save_file(app, orig, path, progress, progress_args, file_id, file_part)

    def send():
        return app.send_video(
            job.channel, str(out), caption=job.caption[:1024], file_name=f"{job.file_name}.mp4",
            supports_streaming=True, disable_notification=True,
            progress=live.transfer("⬆️ Telegram'ga yuklanmoqda"), **extra)

    app.save_file = patched
    try:
        m = await send()
    except Exception as e:  # noqa: BLE001
        log(f"tez yuklash ishlamadi ({e}) — oddiy usulda qayta urinilmoqda")
        app.save_file = orig
        m = await send()
    finally:
        app.save_file = orig
    if not m:
        raise RuntimeError("Telegram'ga yuklanmadi")
    return m.id


def session_api_id():
    """Sessiya faylidagi api_id (sessiya qaysi ilova bilan yaratilgan bo'lsa)."""
    try:
        import sqlite3
        c = sqlite3.connect(f"file:{SESSION}.session?mode=ro", uri=True)
        return int(c.execute("SELECT api_id FROM sessions").fetchone()[0] or 0)
    except Exception:
        return 0


def claim():
    """Navbatdagi video (yo'q bo'lsa — None)."""
    try:
        r = api("claim", {"runner": RUNNER})
    except Exception as e:  # noqa: BLE001
        log("claim:", e)
        return None
    return Job(r) if r.get("job") else None


async def process(app, job):
    live = Live(job)
    shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True)
    out = WORK / "video.mp4"
    thumb = WORK / "thumb.jpg"
    log(f"#{job.id}: {job.caption.replace(chr(10), ' | ')} (urinish {job.attempt}, run {RUNNER})")
    try:
        if not job.channel:
            raise Fatal("yopiq kanal (TG_CHANNEL_ID) worker'da sozlanmagan")
        if not job.url:
            raise Fatal("video manzili yo'q")
        live.send("⏳ boshlanmoqda...", force=True)
        el = await asyncio.to_thread(download, job.url, out, live)
        size = out.stat().st_size
        w, h, dur = ffprobe(out, "width", "v:0"), ffprobe(out, "height", "v:0"), ffprobe(out, "duration")
        live.done(f"Yuklab olindi: {size / 1048576:.1f} MB · {w or '?'}x{h or '?'} · {hms(dur or 0)} · {hms(el)} da")
        log(f"  yuklab olindi: {size / 1048576:.1f} MB")
        if size > MAX_BYTES:
            raise Fatal(f"fayl {size / 1048576:.0f} MB — Telegram chegarasi 2 GB. Pastroq sifatni tanlang.")
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", "10", "-i", str(out), "-frames:v", "1",
                        "-vf", "scale='min(320,iw)':-2", "-q:v", "5", str(thumb)])
        log("Telegram'ga yuklanmoqda (yopiq kanal orqali)...")
        live.send("⬆️ Telegram'ga yuklanmoqda...", force=True)
        t_up = time.time()
        msg = await upload(app, job, out, thumb, live)
        live.done(f"Telegram'ga yuklandi · {hms(time.time() - t_up)}")
        log(f"  kanalga yuklandi: xabar {msg}")
        live.close()
        try:
            api("done", {**job.ident(), "ok": True, "channel_msg": msg,
                         "size": size, "height": int(h or 0),
                         "text": live.text(f"✅ Tayyor ({size / 1048576:.1f} MB)")})
        except Lost:
            # Shu orada navbatdan olib tashlangan — kanaldagi nusxa ham kerak emas.
            await app.delete_messages(job.channel, msg)
            raise
        log(f"  ✅ #{job.id} yuborildi")
    except Lost:
        log(f"  #{job.id} navbatdan olib tashlandi yoki boshqa run'ga o'tdi")
    except Exception as ex:  # noqa: BLE001
        log(f"  #{job.id} XATO: {ex}")
        live.close()
        try:
            api("done", {**job.ident(), "ok": False, "fatal": isinstance(ex, Fatal),
                         "error": str(ex)[:300], "text": live.text("")})
        except Exception:
            pass
    finally:
        live.close()
        shutil.rmtree(WORK, ignore_errors=True)


def on_cancel(signum, frame):
    """Run bekor qilindi — yuklanayotgan video navbatga qaytadi."""
    if CURRENT:
        try:
            api("done", {**CURRENT.ident(), "ok": False, "cancelled": True})
        except Exception:
            pass
        print("Run bekor qilindi — video navbatga qaytarildi", flush=True)
    os._exit(0)


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
        # Kanal Pyrogram peer keshida bo'lsin.
        async for _ in app.get_dialogs():
            pass
        while time.time() - T0 < START_BUDGET:
            job = claim()
            if not job:
                break
            CURRENT = job
            try:
                await process(app, job)
                done += 1
            finally:
                CURRENT = None
    log(f"Run tugadi: {done} ta video.")


if __name__ == "__main__":
    import signal
    signal.signal(signal.SIGINT, on_cancel)
    signal.signal(signal.SIGTERM, on_cancel)
    asyncio.run(main())
    sys.exit(0)
