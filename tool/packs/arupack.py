"""ARUGRAM to'plam fayli (`.arp`) — emoji, GIF va stikerlar to'plami.

BITTA fayl = BITTA to'plam. Fayl boshida to'liq ro'yxat (sarlavha) turadi —
xuddi MP4 dagi `moov` (faststart) kabi: ilova avval faqat sarlavhani
o'qiydi, keyin kerakli elementni o'z joyidan (offset) oladi.

    [16 bayt: "ARUP" | versiya u16 | bayroqlar u16 | sarlavha uzunligi u32 | 0 u32]
    [sarlavha: UTF-8 JSON]
    [hamma kichik rasmlar (thumb) ketma-ket][elementlarning o'zi ketma-ket]

Butun fayl AES-128-CTR (IV nol) bilan shifrlanadi — ilovadagi
`rust/src/telegram.rs` (`ctr_apply`) bilan BIR XIL, shu sabab ilova
fayldan istalgan bo'lakni butun faylni ochmasdan o'qiy oladi.

HAR YANGI VERSIYAGA YANGI TASODIFIY KALIT beriladi (`new_key`). Bir xil
kalit + nol IV bilan ikki xil matnni shifrlash CTR xavfsizligini buzadi
(ikki shifrmatnni XOR qilib ochiq matnni topish mumkin) — shu sabab fayl
o'zgarganda kalit HAR SAFAR almashadi.

Sarlavha (JSON):
    {"v":1, "id":<to'plam>, "kind":"sticker|emoji|gif", "title":"..",
     "ver":<versiya>, "next":<keyingi element raqami>,
     "items":[{"i":1, "o":<offset>, "l":<uzunlik>, "to":<thumb offset>,
               "tl":<thumb uzunligi>, "a":0|1, "w":<eni>, "h":<bo'yi>,
               "e":"😀"}]}
Offsetlar ma'lumot boshiga NISBATAN (`16 + sarlavha uzunligi`).
Element raqamlari (`i`) QAYTA ISHLATILMAYDI: `next` faqat oshadi.
"""

import io
import json
import secrets
import struct
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

MAGIC = b"ARUP"
VERSION = 1
PREAMBLE = 16
CHUNK = 4 * 1024 * 1024

KINDS = ("sticker", "emoji", "gif")

# Chegaralar (foydalanuvchi talabi): bitta element <= 5 MB, fayl <= 1 GB.
MAX_ITEM = 5 * 1024 * 1024
MAX_PACK = 1024 * 1024 * 1024
MAX_ITEMS = 1000


class PackError(Exception):
    """Fayl buzuq yoki qoidalarga to'g'ri kelmaydi."""


def new_key() -> bytes:
    return secrets.token_bytes(16)


def ctr_file(src: Path, dst: Path, key: bytes) -> None:
    """AES-128-CTR (IV nol, 128-bit big-endian hisoblagich). Shifrlash va
    ochish bir xil amal."""
    c = Cipher(algorithms.AES(key), modes.CTR(b"\0" * 16)).encryptor()
    with open(src, "rb") as fi, open(dst, "wb") as fo:
        while True:
            b = fi.read(CHUNK)
            if not b:
                break
            fo.write(c.update(b))
        fo.write(c.finalize())


def ctr_bytes(data: bytes, key: bytes, offset: int = 0) -> bytes:
    """Xotiradagi baytlar (fayl `offset` idan boshlanadi)."""
    c = Cipher(algorithms.AES(key), modes.CTR(b"\0" * 16)).encryptor()
    if offset:
        c.update(b"\0" * offset)
    return c.update(data)


# ── SARLAVHA ────────────────────────────────────────────────────

def preamble(header_len: int) -> bytes:
    return MAGIC + struct.pack(">HHII", VERSION, 0, header_len, 0)


def parse_preamble(b: bytes) -> int:
    """16 baytdan sarlavha uzunligini oladi."""
    if len(b) < PREAMBLE or b[:4] != MAGIC:
        raise PackError("fayl ARUP emas")
    ver, _flags, hlen, _res = struct.unpack(">HHII", b[4:16])
    if ver != VERSION:
        raise PackError(f"noma'lum versiya: {ver}")
    if hlen <= 0 or hlen > 16 * 1024 * 1024:
        raise PackError("sarlavha uzunligi noto'g'ri")
    return hlen


class Source:
    """Element baytlari: xotirada yoki boshqa fayldan bo'lak."""

    def __init__(self, data: bytes = None, path: Path = None, off: int = 0, length: int = 0):
        self.data, self.path, self.off = data, path, off
        self.length = len(data) if data is not None else length

    def copy_to(self, out) -> None:
        if self.data is not None:
            out.write(self.data)
            return
        left = self.length
        with open(self.path, "rb") as f:
            f.seek(self.off)
            while left > 0:
                b = f.read(min(CHUNK, left))
                if not b:
                    raise PackError("manba fayl qisqa")
                out.write(b)
                left -= len(b)


class Item:
    def __init__(self, item_id, data: Source, thumb: Source, animated, w, h, emoji):
        self.id, self.data, self.thumb = item_id, data, thumb
        # 0 — statik, 1 — animatsiyali WebP, 2 — ovozli MP4 (faqat GIF to'plami)
        self.animated, self.w, self.h, self.emoji = int(animated), w, h, emoji


class Pack:
    def __init__(self, pack_id, kind, title, version=0, next_id=1, items=None):
        if kind not in KINDS:
            raise PackError(f"noto'g'ri tur: {kind}")
        self.id, self.kind, self.title = pack_id, kind, title
        self.version, self.next_id = version, next_id
        self.items = list(items or [])

    def data_size(self) -> int:
        return sum(i.data.length + i.thumb.length for i in self.items)

    def remove(self, item_id) -> bool:
        n = len(self.items)
        self.items = [i for i in self.items if i.id != item_id]
        return len(self.items) != n

    def add(self, data: bytes, thumb: bytes, animated, w, h, emoji) -> int:
        if len(self.items) >= MAX_ITEMS:
            raise PackError(f"to'plamda {MAX_ITEMS} tadan ko'p element bo'lmaydi")
        item_id = self.next_id
        self.next_id += 1
        self.items.append(Item(item_id, Source(data=data), Source(data=thumb),
                               animated, w, h, emoji))
        return item_id


def header_json(pack: Pack) -> bytes:
    # Kichik rasmlar (thumb) BIRINCHI va ketma-ket: ilova to'plam oynasini
    # ochganda hammasini BITTA oraliq so'rovi bilan oladi. Undan keyin
    # elementlarning o'zi.
    thumb_total = sum(it.thumb.length for it in pack.items)
    toff, doff, items = 0, thumb_total, []
    for it in pack.items:
        items.append({
            "i": it.id, "o": doff, "l": it.data.length,
            "to": toff, "tl": it.thumb.length,
            "a": int(it.animated), "w": it.w, "h": it.h, "e": it.emoji,
        })
        toff += it.thumb.length
        doff += it.data.length
    return json.dumps({
        "v": VERSION, "id": pack.id, "kind": pack.kind, "title": pack.title,
        "ver": pack.version, "next": pack.next_id, "items": items,
    }, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def write_plain(pack: Pack, dst: Path) -> int:
    """Shifrlanmagan fayl. Qaytadi: fayl hajmi."""
    h = header_json(pack)
    with open(dst, "wb") as f:
        f.write(preamble(len(h)))
        f.write(h)
        for it in pack.items:
            it.thumb.copy_to(f)
        for it in pack.items:
            it.data.copy_to(f)
    return dst.stat().st_size


def read_plain(path: Path) -> Pack:
    """Shifrlanmagan fayldan to'plamni o'qiydi (elementlar faylga havola)."""
    with open(path, "rb") as f:
        hlen = parse_preamble(f.read(PREAMBLE))
        raw = f.read(hlen)
    if len(raw) != hlen:
        raise PackError("sarlavha to'liq emas")
    try:
        h = json.loads(raw.decode("utf-8"))
    except Exception as e:
        raise PackError(f"sarlavha o'qilmadi: {e}")
    base = PREAMBLE + hlen
    size = path.stat().st_size
    pack = Pack(h["id"], h["kind"], h.get("title", ""), h.get("ver", 0), h.get("next", 1))
    for it in h.get("items", []):
        o, l, to, tl = it["o"], it["l"], it["to"], it["tl"]
        if base + max(o + l, to + tl) > size:
            raise PackError("element fayldan tashqarida")
        pack.items.append(Item(
            it["i"], Source(path=path, off=base + o, length=l),
            Source(path=path, off=base + to, length=tl),
            it.get("a", 0), it.get("w", 0), it.get("h", 0), it.get("e", "")))
    return pack


def read_header_bytes(plain: bytes) -> dict:
    """Xotiradagi (ochilgan) baytlar boshidan sarlavha JSON'i (testlar uchun)."""
    hlen = parse_preamble(plain[:PREAMBLE])
    return json.loads(plain[PREAMBLE:PREAMBLE + hlen].decode("utf-8"))


def seal(pack: Pack, work: Path, key: bytes = None):
    """To'plamni yozadi va shifrlaydi. Qaytadi: (shifrlangan fayl, kalit, hajm)."""
    key = key or new_key()
    plain = work / f"pack_{pack.id}.plain"
    sealed = work / f"pack_{pack.id}.sealed"
    size = write_plain(pack, plain)
    if size > MAX_PACK:
        plain.unlink(missing_ok=True)
        raise PackError("to'plam 1 GB dan oshdi")
    ctr_file(plain, sealed, key)
    plain.unlink()
    return sealed, key, size
