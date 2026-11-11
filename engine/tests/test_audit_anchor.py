"""Audit head anchoring: DER, RFC 3161 tokens (local TSA, no network), git anchors, CLI."""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives.serialization import Encoding
from hypothesis import given
from hypothesis import strategies as st

from scruple.audit import anchor as an
from scruple.audit import der
from scruple.audit import timestamp as ts
from scruple.audit.log import compute_hash
from scruple.audit.models import AuditEntry, genesis_hash
from scruple.cli.main import main
from scruple.service import AgentGate
from tests.tsa_support import NOW, LocalTSA, make_ca

# -- DER --------------------------------------------------------------------------


@given(st.integers(min_value=-(2**200), max_value=2**200))
def test_der_integer_round_trip(n: int) -> None:
    assert der.dec_int(der.parse(der.integer(n))) == n


@given(
    st.integers(0, 2),
    st.integers(0, 39),
    st.lists(st.integers(0, 2**64), max_size=8),
)
def test_der_oid_round_trip(a: int, b: int, rest: list[int]) -> None:
    dotted = ".".join(str(x) for x in [a, b, *rest])
    assert der.dec_oid(der.parse(der.oid(dotted))) == dotted


@given(st.datetimes(min_value=datetime(1970, 1, 1), max_value=datetime(9999, 1, 1)))
def test_der_gentime_round_trip(t: datetime) -> None:
    t = t.replace(tzinfo=UTC)
    assert der.dec_gentime(der.parse(der.gentime(t))) == t


@given(st.binary(max_size=300))
def test_der_length_round_trip(b: bytes) -> None:
    assert der.parse(der.octets(b)).value == b


@pytest.mark.parametrize(
    "data",
    [
        b"\x30\x80\x00\x00",  # indefinite length
        b"\x04\x81\x05abcde",  # long form for a short length
        b"\x04\x02ab\x00",  # trailing byte
        b"\x04\x05ab",  # runs past the end
        b"\x1f\x01\x00",  # high tag number
        b"\x02\x02\x00\x01",  # non-minimal integer
        b"\x01\x01\x01",  # boolean that is not 0x00/0xff
        b"\x06\x02\x80\x01",  # non-minimal oid arc
        b"\x04",  # truncated
    ],
)
def test_der_refuses_non_der(data: bytes) -> None:
    decoders = {0x02: der.dec_int, 0x01: der.dec_bool, 0x06: der.dec_oid}

    def decode() -> None:
        t = der.parse(data)
        if t.tag in decoders:
            decoders[t.tag](t)

    with pytest.raises(der.DERError):
        decode()


@pytest.mark.parametrize("s", [b"20261111120000", b"20261111120000+0100", b"20261111120000.50Z"])
def test_der_gentime_requires_utc_der_form(s: bytes) -> None:
    with pytest.raises(der.DERError):
        der.dec_gentime(der.parse(der.enc(der.GENERALIZED_TIME, s)))


def test_der_set_of_is_sorted() -> None:
    assert der.set_of(b"\x04\x01b", b"\x04\x01a") == der.set_of(b"\x04\x01a", b"\x04\x01b")


# -- requests ---------------------------------------------------------------------


@pytest.mark.parametrize("h", ["sha256", "sha384", "sha512"])
def test_request_round_trip(h: ts.HashName) -> None:
    d = ts.digest_of(b"x", h)
    req = ts.build_request(d, h, nonce=12345)
    back = ts.parse_request(req.der)
    assert (back.digest, back.hash_oid, back.nonce) == (d, ts.HASH_OIDS[h], 12345)


def test_request_random_nonce_and_none() -> None:
    d = ts.digest_of(b"x")
    a, b = ts.build_request(d), ts.build_request(d)
    assert a.nonce and b.nonce and a.nonce != b.nonce
    assert ts.build_request(d, use_nonce=False).nonce is None


def test_request_refuses_short_digest() -> None:
    with pytest.raises(ts.TimestampError):
        ts.build_request(b"short")


# -- tokens -----------------------------------------------------------------------


@pytest.fixture(scope="module")
def tsa() -> LocalTSA:
    return LocalTSA()


def _req() -> ts.TimestampRequest:
    return ts.build_request(ts.digest_of(b"statement"))


@pytest.mark.parametrize("key_type", ["rsa", "ec", "ed25519"])
@pytest.mark.parametrize("signing_cert", ["v1", "v2"])
def test_token_verifies(key_type: str, signing_cert: str) -> None:
    t = LocalTSA(key_type=key_type)  # type: ignore[arg-type]
    req = _req()
    info = ts.verify_response(t.respond(req.der, signing_cert=signing_cert), req, [t.ca_cert])  # type: ignore[arg-type]
    assert info.gen_time == NOW and info.serial == 4242 and info.nonce == req.nonce
    assert info.signer_subject == "CN=scruple test tsa"


def test_token_trusted_by_pinning_the_tsa_cert(tsa: LocalTSA) -> None:
    req = _req()
    ts.verify_response(tsa.respond(req.der), req, [tsa.cert])


def test_token_without_embedded_cert_uses_trusted_set(tsa: LocalTSA) -> None:
    req = _req()
    resp = tsa.respond(req.der, include_certs=False)
    ts.verify_response(resp, req, [tsa.cert])
    with pytest.raises(ts.TimestampError, match="not found"):
        ts.verify_response(resp, req, [tsa.ca_cert])


def test_token_trust_none_checks_everything_but_trust(tsa: LocalTSA) -> None:
    req = _req()
    ts.verify_response(tsa.respond(req.der), req, None)
    with pytest.raises(ts.TimestampError, match="signature"):
        ts.verify_response(tsa.respond(req.der, tamper_signature=True), req, None)


@pytest.mark.parametrize(
    ("kw", "match"),
    [
        ({"digest": b"\x00" * 32}, "imprint does not match"),
        ({"nonce": 7}, "nonce"),
        ({"nonce": None}, "nonce"),
        ({"tamper_tst": True}, "message digest"),
        ({"tamper_signature": True}, "signature"),
        ({"signing_cert": "none"}, "signing-certificate"),
        ({"signing_cert": "other"}, "different certificate"),
        ({"status": 2}, "rejection: policy says no"),
        ({"gen_time": NOW + timedelta(days=400)}, "not valid at"),
        ({"content_type_oid": "1.2.840.113549.1.7.1"}, "content type"),
    ],
)
def test_token_failures(tsa: LocalTSA, kw: dict[str, object], match: str) -> None:
    req = _req()
    with pytest.raises(ts.TimestampError, match=match):
        ts.verify_response(tsa.respond(req.der, **kw), req, [tsa.ca_cert])  # type: ignore[arg-type]


def test_token_from_another_ca_is_untrusted(tsa: LocalTSA) -> None:
    req = _req()
    other_ca, _ = make_ca("someone else")
    with pytest.raises(ts.TimestampError, match="not trusted"):
        ts.verify_response(tsa.respond(req.der), req, [other_ca])


def test_token_issuer_name_alone_is_not_trust(tsa: LocalTSA) -> None:
    """A different CA with the same name must not vouch for the TSA."""
    req = _req()
    impostor, _ = make_ca("scruple test root")
    with pytest.raises(ts.TimestampError, match="not trusted"):
        ts.verify_response(tsa.respond(req.der), req, [impostor])


@pytest.mark.parametrize(
    ("eku", "match"),
    [("none", "no extended"), ("noncritical", "critical"), ("wrong", "timeStamping")],
)
def test_tsa_cert_needs_critical_timestamping_eku(eku: str, match: str) -> None:
    t = LocalTSA(eku=eku)  # type: ignore[arg-type]
    req = _req()
    with pytest.raises(ts.TimestampError, match=match):
        ts.verify_response(t.respond(req.der), req, [t.ca_cert])


def test_hash_algorithm_must_match_request(tsa: LocalTSA) -> None:
    req = ts.build_request(ts.digest_of(b"s", "sha384"), "sha384")
    resp = tsa.respond(req.der)
    ts.verify_response(resp, req, [tsa.ca_cert])
    token = ts.token_from_response(resp)
    with pytest.raises(ts.TimestampError, match="hash algorithm"):
        ts.verify_token(token, req.digest, [tsa.ca_cert])


@given(st.binary(max_size=200))
def test_garbage_never_verifies(data: bytes) -> None:
    with pytest.raises(ts.TimestampError):
        ts.verify_token(data, b"\x00" * 32, None)


def test_flipped_bytes_never_verify(tsa: LocalTSA) -> None:
    req = _req()
    token = ts.token_from_response(tsa.respond(req.der))
    for i in range(0, len(token), 7):
        bad = token[:i] + bytes([token[i] ^ 0x01]) + token[i + 1 :]
        try:
            ts.verify_token(bad, req.digest, [tsa.ca_cert])
        except (ts.TimestampError, ValueError):
            continue
        # Only bytes the signature does not cover may change without failing: the
        # unsigned certificate copy and the outer lengths are all covered elsewhere.
        pytest.fail(f"flipping byte {i} still verified")


# -- statements and chain checks --------------------------------------------------


def _chain(n: int, tenant: str = "default") -> list[AuditEntry]:
    out: list[AuditEntry] = []
    prev = genesis_hash(tenant)
    for i in range(1, n + 1):
        draft = AuditEntry(
            seq=i,
            at=NOW,
            event_type="x",
            actor="a",
            payload={"i": i},
            prev_hash=prev,
            entry_hash="",
        )
        e = draft.model_copy(update={"entry_hash": compute_hash(draft)})
        out.append(e)
        prev = e.entry_hash
    return out


def test_statement_text_round_trip() -> None:
    s = an.statement_for(_chain(3), "default")
    assert s.entries == 3 and an.AnchorStatement.from_text(s.text()) == s
    assert s.text().startswith("scruple-audit-anchor/1\ntenant default\nentries 3\nhead ")


@pytest.mark.parametrize(
    "text",
    [
        "scruple-audit-anchor/2\ntenant d\nentries 1\nhead " + "a" * 64 + "\n",
        "scruple-audit-anchor/1\ntenant d\nentries 01\nhead " + "a" * 64 + "\n",
        "scruple-audit-anchor/1\ntenant d\nentries 1\nhead " + "A" * 64 + "\n",
        "scruple-audit-anchor/1\ntenant d\nentries 1\nhead " + "a" * 64,
        "scruple-audit-anchor/1\nentries 1\ntenant d\nhead " + "a" * 64 + "\n",
    ],
)
def test_statement_refuses_non_canonical(text: str) -> None:
    with pytest.raises(ValueError):
        an.AnchorStatement.from_text(text)


def test_statement_refuses_broken_chain() -> None:
    c = _chain(3)
    c[1] = c[1].model_copy(update={"actor": "mallory"})
    with pytest.raises(ValueError, match="does not verify"):
        an.statement_for(c, "default")


def test_check_statement_cases() -> None:
    c = _chain(5)
    s = an.statement_for(c[:3], "default")
    assert an.check_statement(c[:3], s, "default").ok
    assert an.check_statement(c, s, "default").ok  # grew since: fine
    assert "truncated" in str(an.check_statement(c[:2], s, "default").problem)
    rewritten = [*_chain(3)[:2], _chain(4)[3].model_copy(update={"seq": 3})]
    assert not an.check_statement(rewritten, s, "default").ok
    assert "tenant" in str(an.check_statement(c, s, "acme").problem)
    empty = an.statement_for([], "acme")
    assert (
        empty.head == genesis_hash("acme")
        and an.check_statement(_chain(2, "acme"), empty, "acme").ok
    )


def test_check_statement_detects_consistent_rewrite() -> None:
    c = _chain(3)
    s = an.statement_for(c, "default")
    forged: list[AuditEntry] = []
    prev = genesis_hash("default")
    for e in c:
        d = e.model_copy(update={"prev_hash": prev, "actor": "rewritten", "entry_hash": ""})
        d = d.model_copy(update={"entry_hash": compute_hash(d)})
        forged.append(d)
        prev = d.entry_hash
    check = an.check_statement(forged, s, "default")
    assert not check.ok and "rewritten" in str(check.problem)


# -- rfc3161 anchors --------------------------------------------------------------


def test_anchor_rfc3161_and_verify(tsa: LocalTSA) -> None:
    s = an.statement_for(_chain(4), "default")
    a = an.anchor_rfc3161(s, "https://tsa.invalid/", [tsa.ca_cert], transport=tsa.transport())
    assert a.trusted_at_anchor and a.gen_time == NOW
    back = an.TimestampAnchor.model_validate_json(a.model_dump_json())
    assert an.verify_rfc3161(back, [tsa.ca_cert]).serial == 4242
    edited = back.model_copy(update={"serial": "1"})
    with pytest.raises(ts.TimestampError, match="disagree"):
        an.verify_rfc3161(edited, [tsa.ca_cert])
    moved = back.model_copy(update={"statement": an.statement_for(_chain(5), "default")})
    with pytest.raises(ts.TimestampError, match="imprint"):
        an.verify_rfc3161(moved, [tsa.ca_cert])


def test_anchor_rfc3161_http_errors(tsa: LocalTSA) -> None:
    s = an.statement_for(_chain(1), "default")
    for resp in (
        httpx.Response(500),
        httpx.Response(200, content=b"x", headers={"content-type": "text/html"}),
    ):
        t = httpx.MockTransport(lambda _r, resp=resp: resp)
        with pytest.raises(ts.TimestampError):
            an.anchor_rfc3161(s, "https://tsa.invalid/", None, transport=t)


def test_load_certs_pem_and_der(tmp_path: Path, tsa: LocalTSA) -> None:
    (tmp_path / "a.pem").write_bytes(
        tsa.cert.public_bytes(Encoding.PEM) + tsa.ca_cert.public_bytes(Encoding.PEM)
    )
    (tmp_path / "b.der").write_bytes(tsa.cert.public_bytes(Encoding.DER))
    assert len(an.load_certs([tmp_path / "a.pem", tmp_path / "b.der"])) == 3


# -- git anchors ------------------------------------------------------------------


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "anchors"
    repo.mkdir()
    for args in (
        ["init", "-q"],
        ["config", "user.name", "anchor-test"],
        ["config", "user.email", "anchor-test@example.invalid"],
        ["config", "commit.gpgsign", "false"],
    ):
        subprocess.run(["git", "-C", str(repo), *args], check=True)
    return repo


needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


@needs_git
def test_git_anchor_round_trip(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    s = an.statement_for(_chain(2), "default")
    g = an.anchor_git(s, repo)
    assert g.path == "audit-anchors/default.txt"
    assert an.read_git_anchor(repo, g.commit, g.path).statement == s
    g2 = an.anchor_git(s, repo)  # same head again: a new commit, not an error
    assert g2.commit != g.commit
    s3 = an.statement_for(_chain(3), "default")
    g3 = an.anchor_git(s3, repo)
    assert an.read_git_anchor(repo, g.commit, g.path).statement == s  # history kept
    assert an.read_git_anchor(repo, g3.commit, g.path).statement == s3


@needs_git
@pytest.mark.parametrize("path", ["/etc/x", "../x", ""])
def test_git_anchor_refuses_paths_outside_repo(tmp_path: Path, path: str) -> None:
    s = an.statement_for(_chain(1), "default")
    with pytest.raises(ValueError, match="relative"):
        an.anchor_git(s, _repo(tmp_path), path or "..")


@needs_git
def test_git_anchor_commits_only_the_anchor_file(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "staged.txt").write_text("x")
    subprocess.run(["git", "-C", str(repo), "add", "staged.txt"], check=True)
    an.anchor_git(an.statement_for(_chain(1), "default"), repo)
    files = subprocess.run(
        ["git", "-C", str(repo), "show", "--name-only", "--format=", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    assert files == ["audit-anchors/default.txt"]


def test_git_anchor_needs_a_repo(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="git"):
        an.anchor_git(an.statement_for(_chain(1), "default"), tmp_path)


# -- CLI --------------------------------------------------------------------------


def _db(tmp_path: Path) -> str:
    db = str(tmp_path / "g.db")
    AgentGate.open(db).bootstrap_baseline()
    return db


@needs_git
def test_cli_git_anchor_and_verify(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db, repo = _db(tmp_path), _repo(tmp_path)
    assert main(["audit", "anchor", "--db", db, "--git-repo", str(repo)]) == 0
    out = capsys.readouterr().out
    assert "not pushed" in out
    commit = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    args = ["audit", "verify-anchor", "--db", db, "--git-repo", str(repo), "--commit", commit]
    assert main(args) == 0
    assert "ANCHOR OK" in capsys.readouterr().out
    g = AgentGate.open(db)
    g.audit.append("note", "ops", {"x": 1})  # the chain grows: still anchored
    assert main(args) == 0
    other = str(tmp_path / "other.db")
    AgentGate.open(other).audit.append("note", "ops", {"x": 2})
    assert main([*args[:3], other, *args[4:]]) == 1  # a different chain
    assert "ANCHOR FAILED" in capsys.readouterr().out


def test_cli_rfc3161_anchor_and_verify(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tsa: LocalTSA,
) -> None:
    real = ts.request_timestamp

    def fake(url: str, request: ts.TimestampRequest, **_: object) -> bytes:
        return real(url, request, transport=tsa.transport())

    monkeypatch.setattr(ts, "request_timestamp", fake)
    db = _db(tmp_path)
    ca = tmp_path / "ca.pem"
    ca.write_bytes(tsa.ca_cert.public_bytes(Encoding.PEM))
    out = tmp_path / "anchor.json"
    base = ["audit", "anchor", "--db", db, "--tsa-url", "https://tsa.invalid/", "--out", str(out)]
    assert main([*base, "--tsa-cert", str(ca)]) == 0
    assert "WARNING" not in capsys.readouterr().out
    assert main(["audit", "verify-anchor", str(out), "--db", db, "--tsa-cert", str(ca)]) == 0
    assert "ANCHOR OK" in capsys.readouterr().out
    # without a trusted cert at anchor time it warns, and verify always needs one
    assert main(base) == 0
    assert "TSA trust NOT checked" in capsys.readouterr().out
    assert main(["audit", "verify-anchor", str(out), "--db", db]) == 2
    other_ca, _ = make_ca("other")
    oc = tmp_path / "other.pem"
    oc.write_bytes(other_ca.public_bytes(Encoding.PEM))
    assert main(["audit", "verify-anchor", str(out), "--db", db, "--tsa-cert", str(oc)]) == 1
    data = json.loads(out.read_text())
    token = bytearray(base64.b64decode(data["token"]))
    token[-5] ^= 1
    data["token"] = base64.b64encode(bytes(token)).decode()
    out.write_text(json.dumps(data))
    assert main(["audit", "verify-anchor", str(out), "--db", db, "--tsa-cert", str(ca)]) == 1


def test_cli_anchor_needs_a_target(tmp_path: Path) -> None:
    assert main(["audit", "anchor", "--db", _db(tmp_path)]) == 2


def test_cli_anchor_is_read_only_on_the_database(tmp_path: Path) -> None:
    db = str(tmp_path / "empty.db")
    AgentGate.open(db)
    main(["audit", "anchor", "--db", db, "--git-repo", str(tmp_path / "nope")])
    assert AgentGate.open(db).audit.entries() == []


# -- interop with openssl ts ------------------------------------------------------

needs_openssl = pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl not installed")


def _openssl_tsa_files(tmp_path: Path, tsa: LocalTSA) -> tuple[Path, Path, Path, Path]:
    from cryptography.hazmat.primitives.serialization import NoEncryption, PrivateFormat

    cert, key, ca = tmp_path / "tsa.pem", tmp_path / "tsa.key", tmp_path / "ca.pem"
    cert.write_bytes(tsa.cert.public_bytes(Encoding.PEM))
    ca.write_bytes(tsa.ca_cert.public_bytes(Encoding.PEM))
    key.write_bytes(tsa.key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
    cfg = tmp_path / "tsa.cnf"
    cfg.write_text(
        "[ tsa ]\ndefault_tsa = tsa1\n[ tsa1 ]\n"
        f"serial = {tmp_path / 'serial'}\n"
        "default_policy = 1.3.6.1.4.1.99999.1\ndigests = sha256\nsigner_digest = sha256\n"
        "ess_cert_id_alg = sha256\naccuracy = secs:1\n"
    )
    (tmp_path / "serial").write_text("01\n")
    return cert, key, ca, cfg


@needs_openssl
def test_openssl_tsa_reply_verifies_here(tmp_path: Path) -> None:
    tsa = LocalTSA()
    cert, key, _ca, cfg = _openssl_tsa_files(tmp_path, tsa)
    req = ts.build_request(ts.digest_of(b"interop"))
    (tmp_path / "q.tsq").write_bytes(req.der)
    subprocess.run(
        [
            "openssl",
            "ts",
            "-reply",
            "-config",
            str(cfg),
            "-queryfile",
            str(tmp_path / "q.tsq"),
            "-signer",
            str(cert),
            "-inkey",
            str(key),
            "-out",
            str(tmp_path / "r.tsr"),
        ],
        check=True,
        capture_output=True,
    )
    info = ts.verify_response((tmp_path / "r.tsr").read_bytes(), req, [tsa.ca_cert])
    assert info.serial > 0 and info.policy == "1.3.6.1.4.1.99999.1"
    assert abs(info.gen_time - datetime.now(UTC)) < timedelta(minutes=5)


@needs_openssl
def test_local_tsa_reply_verifies_in_openssl(tmp_path: Path) -> None:
    """The test TSA is standard RFC 3161 (so the tests above test the real format)."""
    tsa = LocalTSA()
    _, _, ca, _ = _openssl_tsa_files(tmp_path, tsa)
    data = b"interop statement"
    req = ts.build_request(ts.digest_of(data))
    (tmp_path / "data").write_bytes(data)
    (tmp_path / "q.tsq").write_bytes(req.der)
    (tmp_path / "r.tsr").write_bytes(tsa.respond(req.der, gen_time=datetime.now(UTC)))
    done = subprocess.run(
        [
            "openssl",
            "ts",
            "-verify",
            "-queryfile",
            str(tmp_path / "q.tsq"),
            "-in",
            str(tmp_path / "r.tsr"),
            "-CAfile",
            str(ca),
        ],
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert "Verification: OK" in done.stdout
