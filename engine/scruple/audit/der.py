"""A small, strict DER reader and writer: just enough ASN.1 for RFC 3161 timestamps.

Only the subset the timestamp code needs: single-byte tags (tag numbers 0..30),
definite minimal lengths, INTEGER, BOOLEAN, NULL, OCTET STRING, OBJECT IDENTIFIER,
GeneralizedTime, SEQUENCE, SET and context tags. Anything outside DER (indefinite
lengths, long-form lengths that could be short, high tag numbers, trailing bytes)
is refused with `DERError`, so a malformed token never parses into something else.
Certificates and keys are left to `cryptography`; this module never interprets them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

INTEGER = 0x02
BOOLEAN = 0x01
BIT_STRING = 0x03
OCTET_STRING = 0x04
NULL = 0x05
OID = 0x06
UTF8_STRING = 0x0C
GENERALIZED_TIME = 0x18
SEQUENCE = 0x30
SET = 0x31


class DERError(ValueError):
    """The bytes are not the DER this module expects."""


@dataclass(frozen=True)
class TLV:
    """One decoded element. `raw` is its full encoding (tag, length and value)."""

    tag: int
    value: bytes
    raw: bytes

    @property
    def constructed(self) -> bool:
        return bool(self.tag & 0x20)

    def is_context(self, number: int) -> bool:
        return (self.tag & 0xC0) == 0x80 and (self.tag & 0x1F) == number

    def children(self) -> list[TLV]:
        if not self.constructed:
            raise DERError(f"tag 0x{self.tag:02x} is primitive, it has no children")
        return read_all(self.value)


def _read(data: bytes, pos: int) -> tuple[TLV, int]:
    start = pos
    if pos + 2 > len(data):
        raise DERError("truncated element")
    tag = data[pos]
    if tag & 0x1F == 0x1F:
        raise DERError("high tag numbers are not supported")
    first = data[pos + 1]
    pos += 2
    if first < 0x80:
        length = first
    elif first == 0x80:
        raise DERError("indefinite length is not DER")
    else:
        n = first & 0x7F
        if n > 4 or pos + n > len(data):
            raise DERError("bad length")
        length = int.from_bytes(data[pos : pos + n], "big")
        if length < 0x80 or data[pos] == 0:
            raise DERError("length is not minimally encoded")
        pos += n
    end = pos + length
    if end > len(data):
        raise DERError("element runs past the end of the input")
    return TLV(tag=tag, value=data[pos:end], raw=data[start:end]), end


def read_all(data: bytes) -> list[TLV]:
    """Every element in `data`, back to back."""
    out: list[TLV] = []
    pos = 0
    while pos < len(data):
        tlv, pos = _read(data, pos)
        out.append(tlv)
    return out


def parse(data: bytes) -> TLV:
    """Exactly one element; trailing bytes are an error."""
    tlv, end = _read(data, 0)
    if end != len(data):
        raise DERError("trailing bytes after the element")
    return tlv


def expect(tlv: TLV, tag: int, what: str) -> TLV:
    if tlv.tag != tag:
        raise DERError(f"{what}: expected tag 0x{tag:02x}, found 0x{tlv.tag:02x}")
    return tlv


# -- decoding primitives -----------------------------------------------------------


def dec_int(tlv: TLV) -> int:
    expect(tlv, INTEGER, "integer")
    v = tlv.value
    if not v:
        raise DERError("empty integer")
    if len(v) > 1 and ((v[0] == 0 and v[1] < 0x80) or (v[0] == 0xFF and v[1] >= 0x80)):
        raise DERError("integer is not minimally encoded")
    return int.from_bytes(v, "big", signed=True)


def dec_bool(tlv: TLV) -> bool:
    expect(tlv, BOOLEAN, "boolean")
    if tlv.value not in (b"\x00", b"\xff"):
        raise DERError("boolean is not DER (must be 0x00 or 0xff)")
    return tlv.value == b"\xff"


def dec_oid(tlv: TLV) -> str:
    expect(tlv, OID, "object identifier")
    v = tlv.value
    if not v or v[-1] & 0x80:
        raise DERError("bad object identifier")
    arcs: list[int] = []
    n = 0
    fresh = True
    for b in v:
        if fresh and b == 0x80:
            raise DERError("object identifier arc is not minimally encoded")
        n = (n << 7) | (b & 0x7F)
        fresh = not b & 0x80
        if fresh:
            arcs.append(n)
            n = 0
    first = arcs[0]
    head = [0, first] if first < 40 else [1, first - 40] if first < 80 else [2, first - 80]
    return ".".join(str(a) for a in head + arcs[1:])


def dec_gentime(tlv: TLV) -> datetime:
    """GeneralizedTime as RFC 3161 requires it: UTC (`Z`), optional fraction."""
    expect(tlv, GENERALIZED_TIME, "generalized time")
    s = tlv.value.decode("ascii")
    if not s.endswith("Z") or len(s) < 15:
        raise DERError(f"generalized time must be UTC ending in Z: {s!r}")
    body = s[:-1]
    main, _, frac = body.partition(".")
    if len(main) != 14 or not main.isdigit() or (frac and (not frac.isdigit() or frac[-1] == "0")):
        raise DERError(f"bad generalized time {s!r}")
    t = datetime.strptime(main, "%Y%m%d%H%M%S").replace(tzinfo=UTC)
    if frac:
        t = t.replace(microsecond=int((frac + "000000")[:6]))
    return t


# -- encoding ----------------------------------------------------------------------


def enc(tag: int, value: bytes) -> bytes:
    n = len(value)
    if n < 0x80:
        length = bytes([n])
    else:
        body = n.to_bytes((n.bit_length() + 7) // 8, "big")
        length = bytes([0x80 | len(body)]) + body
    return bytes([tag]) + length + value


def seq(*items: bytes) -> bytes:
    return enc(SEQUENCE, b"".join(items))


def set_of(*items: bytes) -> bytes:
    """SET OF: DER orders the members by their encodings."""
    return enc(SET, b"".join(sorted(items)))


def integer(n: int) -> bytes:
    length = max(1, (n + (n < 0)).bit_length() // 8 + 1)
    return enc(INTEGER, n.to_bytes(length, "big", signed=True))


def boolean(v: bool) -> bytes:
    return enc(BOOLEAN, b"\xff" if v else b"\x00")


def null() -> bytes:
    return enc(NULL, b"")


def octets(b: bytes) -> bytes:
    return enc(OCTET_STRING, b)


def utf8(s: str) -> bytes:
    return enc(UTF8_STRING, s.encode("utf-8"))


def oid(dotted: str) -> bytes:
    arcs = [int(a) for a in dotted.split(".")]
    if len(arcs) < 2 or arcs[0] > 2 or (arcs[0] < 2 and arcs[1] >= 40):
        raise DERError(f"bad object identifier {dotted!r}")
    out = bytearray()
    for a in [40 * arcs[0] + arcs[1], *arcs[2:]]:
        chunk = [a & 0x7F]
        a >>= 7
        while a:
            chunk.append(0x80 | (a & 0x7F))
            a >>= 7
        out.extend(reversed(chunk))
    return enc(OID, bytes(out))


def gentime(t: datetime) -> bytes:
    t = t.astimezone(UTC)
    s = t.strftime("%Y%m%d%H%M%S")
    if t.microsecond:
        s += "." + f"{t.microsecond:06d}".rstrip("0")
    return enc(GENERALIZED_TIME, (s + "Z").encode("ascii"))


def explicit(number: int, inner: bytes) -> bytes:
    return enc(0xA0 | number, inner)


def implicit(number: int, encoded: bytes) -> bytes:
    """Re-tag an encoded element as [number] IMPLICIT, keeping its constructed bit."""
    return bytes([0x80 | (encoded[0] & 0x20) | number]) + encoded[1:]
