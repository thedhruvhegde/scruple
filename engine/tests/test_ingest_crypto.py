"""Field-level encryption: round trips, rotation, crypto-shredding, tamper detection
(every byte, context swaps, key-id relabelling), and fuzzing of the envelope parser."""

from __future__ import annotations

import base64
import contextlib
import itertools

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from hypothesis import given, settings
from hypothesis import strategies as st

from scruple.ingest.errors import IngestError, InputTooLargeError
from scruple.security.crypto import (
    MAX_PLAINTEXT,
    CryptoError,
    FieldCipher,
    MalformedEnvelopeError,
    TamperedError,
    UnknownKeyError,
    is_envelope,
)

K_OLD = bytes(range(32))
K_NEW = bytes(range(32, 64))
CTX = "case:stripe:dp_1:note"


def ring(active: str = "new", **extra: bytes) -> FieldCipher:
    return FieldCipher({"old": K_OLD, "new": K_NEW, **extra}, active=active)


def counter_nonces() -> FieldCipher:
    c = itertools.count()
    return FieldCipher({"k": K_OLD}, "k", nonce_source=lambda n: next(c).to_bytes(n, "big"))


def test_round_trip_and_shape() -> None:
    f = ring()
    env = f.encrypt("refund note", CTX)
    assert is_envelope(env)
    assert env.startswith("enc:v1:new:")
    assert "refund" not in env
    assert f.decrypt_str(env, CTX) == "refund note"
    assert f.decrypt(f.encrypt(b"\x00\xff", CTX), CTX) == b"\x00\xff"


def test_fixed_vector_matches_independent_aesgcm() -> None:
    env = counter_nonces().encrypt("hello", "ctx")
    _, _, kid, n64, c64 = env.split(":")
    nonce = base64.urlsafe_b64decode(n64 + "=" * (-len(n64) % 4))
    ct = base64.urlsafe_b64decode(c64 + "=" * (-len(c64) % 4))
    assert kid == "k"
    assert nonce == bytes(12)
    assert AESGCM(K_OLD).decrypt(nonce, ct, b"scruple:v1:k:ctx") == b"hello"


def test_fresh_nonce_every_time() -> None:
    f = ring()
    envs = {f.encrypt("same", CTX) for _ in range(500)}
    assert len(envs) == 500


def test_rotation() -> None:
    old_writer = ring(active="old")
    env = old_writer.encrypt("secret", CTX)
    f = ring(active="new")
    assert f.key_id_of(env) == "old"
    assert f.decrypt_str(env, CTX) == "secret"
    rotated = f.rotate(env, CTX)
    assert f.key_id_of(rotated) == "new"
    assert f.decrypt_str(rotated, CTX) == "secret"
    shredded = FieldCipher({"new": K_NEW}, "new")
    assert shredded.decrypt_str(rotated, CTX) == "secret"
    with pytest.raises(UnknownKeyError):
        shredded.decrypt(env, CTX)


def test_record_helpers() -> None:
    old = ring(active="old")

    def ctx(field: str) -> str:
        return f"case:42:{field}"

    rec = {"id": "42", "note": "call back", "email": None, "amount": 10}
    enc = old.encrypt_fields(rec, ["note", "email"], ctx)
    assert enc["email"] is None
    assert enc["id"] == "42"
    assert is_envelope(enc["note"])
    assert old.encrypt_fields(enc, ["note"], ctx) == enc  # already encrypted: unchanged
    new = ring(active="new")
    rotated, n = new.rotate_record(enc, ["note", "email", "id"], ctx)
    assert n == 1
    assert new.key_id_of(rotated["note"]) == "new"
    assert new.rotate_record(rotated, ["note"], ctx)[1] == 0
    assert new.decrypt_str(rotated["note"], ctx("note")) == "call back"
    with pytest.raises(CryptoError):
        old.encrypt_fields({"amount": 10}, ["amount"], ctx)


# --- tamper detection -------------------------------------------------------------


def test_every_ciphertext_byte_flip_is_detected() -> None:
    f = ring()
    env = f.encrypt("evidence: tracking 1Z999", CTX)
    head, c64 = env.rsplit(":", 1)
    ct = bytearray(base64.urlsafe_b64decode(c64 + "=" * (-len(c64) % 4)))
    for i in range(len(ct)):
        bad = bytearray(ct)
        bad[i] ^= 0x01
        forged = head + ":" + base64.urlsafe_b64encode(bytes(bad)).decode().rstrip("=")
        with pytest.raises(TamperedError):
            f.decrypt(forged, CTX)


def test_nonce_flip_detected() -> None:
    f = ring()
    p, v, kid, n64, c64 = f.encrypt("x", CTX).split(":")
    nonce = bytearray(base64.urlsafe_b64decode(n64 + "=="))
    nonce[0] ^= 1
    forged = ":".join([p, v, kid, base64.urlsafe_b64encode(bytes(nonce)).decode().rstrip("="), c64])
    with pytest.raises(TamperedError):
        f.decrypt(forged, CTX)


def test_moving_ciphertext_to_another_field_fails() -> None:
    f = ring()
    env = f.encrypt("4242", "case:1:last4")
    with pytest.raises(TamperedError):
        f.decrypt(env, "case:2:last4")
    with pytest.raises(TamperedError):
        f.decrypt(env, "case:1:note")


def test_relabelling_key_id_fails() -> None:
    f = ring(same=K_NEW)  # even with an identical key under another id
    env = f.encrypt("x", CTX)
    relabelled = env.replace(":new:", ":same:", 1)
    with pytest.raises(TamperedError):
        f.decrypt(relabelled, CTX)


def test_truncation_and_extension_fail() -> None:
    f = ring()
    env = f.encrypt("hello world", CTX)
    with pytest.raises((TamperedError, MalformedEnvelopeError)):
        f.decrypt(env[:-2], CTX)
    with pytest.raises((TamperedError, MalformedEnvelopeError)):
        f.decrypt(env + "AA", CTX)


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "plain text",
        "enc:v2:new:AAAA:AAAA",
        "enc:v1:new:AAAA",
        "enc:v1:new:AAAA:AAAA:AAAA",
        "enc:v1:bad id:AAAAAAAAAAAAAAAA:" + "A" * 24,
        "enc:v1:new:!!!!:AAAA",
        "enc:v1:new:AAAAAAAAAAAAAAAA:AAAA",  # ciphertext shorter than a tag
        "enc:v1:new:AAAA:" + "A" * 24,  # nonce too short
    ],
)
def test_malformed_envelopes(bad: str) -> None:
    with pytest.raises(MalformedEnvelopeError):
        ring().decrypt(bad, CTX)


def test_unknown_key_and_config() -> None:
    env = FieldCipher({"gone": K_OLD}, "gone").encrypt("x", CTX)
    with pytest.raises(UnknownKeyError):
        ring().decrypt(env, CTX)
    with pytest.raises(ValueError, match="32 bytes"):
        FieldCipher({"a": b"short"}, "a")
    with pytest.raises(ValueError, match="active"):
        FieldCipher({"a": K_OLD}, "b")
    with pytest.raises(ValueError, match="key ids"):
        FieldCipher({"a:b": K_OLD}, "a:b")
    with pytest.raises(CryptoError):
        FieldCipher({"a": K_OLD}, "a", nonce_source=lambda n: b"x").encrypt("x", CTX)
    with pytest.raises(InputTooLargeError):
        ring().encrypt(b"x" * (MAX_PLAINTEXT + 1), CTX)
    assert K_OLD.hex() not in repr(ring())
    f = ring()
    binary = f.encrypt(b"\xff", CTX)
    with pytest.raises(CryptoError):
        f.decrypt_str(binary, CTX)


# --- properties -------------------------------------------------------------------


@settings(max_examples=150, deadline=None)
@given(st.text(max_size=300), st.text(max_size=40), st.sampled_from(["old", "new"]))
def test_round_trip_property(text: str, context: str, writer: str) -> None:
    env = ring(active=writer).encrypt(text, context)
    f = ring()
    assert f.decrypt_str(env, context) == text
    assert f.decrypt_str(f.rotate(env, context), context) == text


@settings(max_examples=300, deadline=None)
@given(st.text(max_size=200))
def test_fuzz_decrypt_only_typed_errors(s: str) -> None:
    with contextlib.suppress(IngestError):
        ring().decrypt("enc:v1:" + s, CTX)
    with contextlib.suppress(IngestError):
        ring().decrypt(s, CTX)
