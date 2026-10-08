"""To'plam (emoji, GIF, stiker) fayllarini yig'ish (GitHub Actions, `packs.yml`).

ALOHIDA workflow: kodlash (`tool/encode/run.py`, `encode.yml`) ga TEGILMAYDI.
Yangi akkauntdagi repoda (`GH_REPO`) ishlaydi; worker uni faqat admin
tasdiqlagan rasm bo'lganda ishga tushiradi (`worker/src/packs.rs` -> `kick`).

Navbat worker'da (`worker/src/packs.rs`). Bu modul:

  1. `claim` — tasdiqlangan (admin ruxsat bergan) elementi bor ENG ESKI
     to'plamni oladi (bir vaqtda faqat bittasi, ijara bilan);
  2. to'plamning joriy faylini kanaldan yuklaydi va ochadi;
  3. har qo'shiladigan elementni (kanaldagi vaqtinchalik `pki_...` fayl)
     yuklab, kaliti bilan ochib, YENGIL WebP ga aylantiradi (`arunorm`);
  4. olib tashlanadigan elementlarni chiqaradi;
  5. yangi faylni YANGI kalit bilan shifrlab kanalga yuklaydi
     (`pk_<to'plam>_<versiya>.arp`);
  6. `finish` — natija (har amal uchun: bo'ldi yoki sababi bilan rad).

Telegram sessiyasi kodlash bilan BIR XIL (`session.enc`). Bitta sessiya ikki
joyda bir vaqtda ishlatilsa Telegram uni o'chirib yuboradi — shu sabab
`packs.yml` kodlash bilan BIR `concurrency` guruhida: ikkisi hech qachon
bir vaqtda ishlamaydi (kodlash hozir ishlayotgan bo'lsa to'plamlar kutadi).

XAVFSIZLIK:
  * bitta element xatosi (buzuq rasm, katta fayl) butun to'plamni to'xtatmaydi
    — o'sha element sababi bilan rad etiladi, qolganlari qo'shiladi;
  * to'plam 1 GB dan oshsa qolgan elementlar "to'plam to'ldi" bilan rad etiladi;
  * ish boshqa run'ga o'tgan bo'lsa (409) — darhol to'xtaydi, hech narsa yozmaydi.
"""

import asyncio
import json
import os
import re
import shutil
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import arunorm
import arupack

API = os.environ.get("API_BASE", "").rstrip("/")
TOKEN = os.environ.get("ENCODE_TOKEN", "")
RUNNER = f"{os.environ.get('GITHUB_RUN_ID', 'local')}-{os.environ.get('GITHUB_RUN_ATTEMPT', '1')}"
WORK = Path(os.environ.get("RUNNER_TEMP", "/tmp")) / "arugram_packs"


class JobLost(Exception):
    """Ish boshqa run'ga o'tdi."""


def api(path: str, body: dict) -> dict:
    data = json.dumps(body).encode()
    err = None
    for attempt in range(5):
        req = urllib.request.Request(
            f"{API}/api/packs/job/{path}", data=data, method="POST",
            headers={"X-Encode-Token": TOKEN, "Content-Type": "application/json",
                     "User-Agent": "arugram-encoder"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            if e.code == 409:
                raise JobLost()
            if e.code in (400, 401, 403, 404):
                raise RuntimeError(f"packs/{path}: HTTP {e.code} {e.read()[:200]!r}")
            err = e
        except Exception as e:  # tarmoq
            err = e
        time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"packs/{path}: {err}")


def apply_ops(pack: arupack.Pack, ops: list, fetch) -> list:
    """Amallarni to'plamga qo'llaydi. `fetch(op)` — element baytlari (yoki
    `arunorm.Rejected` / `LookupError`). Qaytadi: `finish` uchun natijalar."""
    results = []
    for op in ops:
        oid = op["id"]
        if op["op"] == "remove":
            pack.remove(int(op.get("item_id") or 0))
            # Yo'q element ham "bo'ldi": maqsad — u to'plamda bo'lmasligi.
            results.append({"id": oid, "ok": True, "item": int(op.get("item_id") or 0)})
            continue
        try:
            raw = fetch(op)
            res = arunorm.normalize(raw, pack.kind, op.get("emoji", ""),
                                    trim_of(op.get("file", "")),
                                    mute=mute_of(op.get("file", "")))
            need = res_size(res) + 256
            if pack.data_size() + need + 256 * len(pack.items) > arupack.MAX_PACK:
                raise arunorm.Rejected("to'plam to'ldi (1 GB)")
            item_id = pack.add(res.data, res.thumb, 2 if res.video else res.animated, res.w, res.h,
                               (op.get("emoji") or "")[:16])
            results.append({"id": oid, "ok": True, "item": item_id})
        except arunorm.Rejected as e:
            results.append({"id": oid, "ok": False, "reason": str(e)[:200]})
        except arupack.PackError as e:
            results.append({"id": oid, "ok": False, "reason": str(e)[:200]})
        except LookupError as e:
            results.append({"id": oid, "ok": False, "reason": str(e)[:200] or "fayl topilmadi"})
    return results


def trim_of(name: str):
    """Video bo'lagi fayl nomidan: `pki_..._t<boshi_ms>-<oxiri_ms>.bin`."""
    m = re.search(r"_t(\d{1,9})-(\d{1,9})(?:_m)?\.bin$", name or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def mute_of(name: str) -> bool:
    """Ovozsiz qilish belgisi: `..._m.bin` (GIF to'plamida ovoz saqlanadi, shu
    belgi bo'lmasa)."""
    return bool(re.search(r"_m\.bin$", name or ""))


def res_size(res) -> int:
    return len(res.data) + len(res.thumb)


class Heartbeat:
    def __init__(self, ident):
        self.ident, self.lost = ident, False
        self.stop = threading.Event()
        threading.Thread(target=self.run, daemon=True).start()

    def run(self):
        while not self.stop.wait(240):
            try:
                api("heartbeat", self.ident)
            except JobLost:
                self.lost = True
                return
            except Exception:
                pass

    def check(self):
        if self.lost:
            raise JobLost()


async def _download(app, channel, msg_id, dst: Path) -> Path:
    m = await app.get_messages(channel, int(msg_id))
    if not m or m.empty or not (m.document or m.video or m.photo):
        raise LookupError("fayl kanalda topilmadi")
    got = await app.download_media(m, file_name=str(dst))
    if not got or Path(got).stat().st_size == 0:
        raise LookupError("fayl yuklab olinmadi")
    return Path(got)


async def process(app, channel: int, job: dict, log) -> None:
    p = job["pack"]
    pid = int(p["id"])
    ident = {"runner": RUNNER, "pack": pid}
    hb = Heartbeat(ident)
    work = WORK / str(pid)
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    try:
        log(f"To'plam #{pid} ({p['kind']}, «{p['title']}»): {len(job['ops'])} ta amal")
        if p.get("msg_id") and p.get("key"):
            log("  joriy fayl yuklab olinmoqda...")
            enc = await _download(app, channel, p["msg_id"], work / "old.enc")
            plain = work / "old.plain"
            await asyncio.to_thread(arupack.ctr_file, enc, plain, bytes.fromhex(p["key"]))
            enc.unlink()
            pack = arupack.read_plain(plain)
            if pack.id != pid or pack.kind != p["kind"]:
                raise RuntimeError("fayl boshqa to'plamniki")
        else:
            pack = arupack.Pack(pid, p["kind"], p["title"])
        pack.title = p["title"]
        hb.check()

        # Qo'shiladigan fayllar oldindan yuklab olinadi (asinxron), keyin
        # sinxron `apply_ops` ularni tekshiradi/aylantiradi.
        raws = {}
        for op in job["ops"]:
            if op["op"] != "add":
                continue
            try:
                f = await _download(app, channel, op["msg_id"], work / f"in_{op['id']}.enc")
                out = work / f"in_{op['id']}.bin"
                await asyncio.to_thread(arupack.ctr_file, f, out, bytes.fromhex(op["key"]))
                f.unlink()
                raws[op["id"]] = out.read_bytes()
                out.unlink()
            except LookupError as e:
                raws[op["id"]] = e
            hb.check()

        def fetch(op):
            v = raws.get(op["id"])
            if isinstance(v, Exception):
                raise v
            if v is None:
                raise LookupError("fayl topilmadi")
            return v

        results = await asyncio.to_thread(apply_ops, pack, job["ops"], fetch)
        changed = any(r["ok"] for r in results)
        body = {**ident, "results": results, "ok": True, "changed": changed}
        if changed:
            pack.version = int(p["version"]) + 1
            name = f"pk_{pid}_{pack.version}.arp"
            sealed, key, size = await asyncio.to_thread(arupack.seal, pack, work)
            log(f"  {len(pack.items)} ta element, {size / 1048576:.1f} MB — yuklanmoqda...")
            hb.check()
            sent = await app.send_document(
                channel, str(sealed), file_name=name, force_document=True,
                caption=name, disable_notification=True)
            body.update({"file": name, "key": key.hex(), "msg_id": sent.id,
                         "version": pack.version, "items": len(pack.items), "bytes": size})
        api("finish", body)
        bad = sum(1 for r in results if not r["ok"])
        log(f"  tayyor: {sum(1 for r in results if r['ok'])} ta bo'ldi, {bad} ta rad etildi")
    except JobLost:
        log("  to'plam ishi boshqa run'ga o'tdi — to'xtatildi")
    except Exception as ex:
        log("  XATO:", ex)
        try:
            api("finish", {**ident, "ok": False, "error": str(ex)[:300]})
        except Exception:
            pass
    finally:
        hb.stop.set()
        shutil.rmtree(work, ignore_errors=True)


async def drain(app, log, allowed=lambda: True) -> int:
    """Tayyor (admin tasdiqlagan) amallari bor hamma to'plamni ishlaydi."""
    if not API or not TOKEN:
        return 0
    done = 0
    while allowed() and done < 200:
        try:
            r = api("claim", {"runner": RUNNER})
        except Exception as e:
            log("to'plam navbatini olib bo'lmadi:", e)
            break
        job = r.get("job")
        if not job:
            break
        await process(app, int(r["channel"]), job, log)
        done += 1
    return done


# ── ISHGA TUSHIRISH ─────────────────────────────────────────────

T0 = time.time()
# Shu vaqtdan keyin yangi to'plam olinmaydi (Actions limiti 6 soat).
START_BUDGET = int(os.environ.get("START_BUDGET_MIN", "240")) * 60
SESSION = str(Path(__file__).with_name("pyro_session"))


def _log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def _session_api_id() -> int:
    """Sessiya faylidagi api_id (o'qib bo'lmasa 0)."""
    try:
        import sqlite3
        c = sqlite3.connect(f"file:{SESSION}.session?mode=ro", uri=True)
        return int(c.execute("SELECT api_id FROM sessions").fetchone()[0] or 0)
    except Exception:
        return 0


async def main():
    import pyrogram.utils
    from pyrogram import Client

    # Pyrogram 2.0.106: yangi kanallar uchun ma'lum xato (kodlashdagi yamoq).
    pyrogram.utils.MIN_CHANNEL_ID = -1009999999999
    api_id = _session_api_id() or int(os.environ["TG_API_ID"])
    app = Client(SESSION, api_id=api_id, api_hash=os.environ["TG_API_HASH"],
                 no_updates=True)
    async with app:
        # Kanal Pyrogram peer keshida bo'lsin.
        async for _ in app.get_dialogs():
            pass
        n = await drain(app, _log, lambda: time.time() - T0 <= START_BUDGET)
        _log(f"Tugadi: {n} ta to'plam ishlandi")


if __name__ == "__main__":
    asyncio.run(main())
