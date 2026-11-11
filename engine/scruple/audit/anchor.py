"""Anchor the audit head outside the database: an RFC 3161 timestamp or a git commit.

The hash chain (`scruple.audit.log`) catches edits, deletions and reordering, but not
a truncated tail or a whole chain rewritten consistently by whoever controls the
database. An anchor is a record, held by someone else, that says "tenant T's chain had
N entries ending in head H at time X". Later, `check_statement` shows the current chain
still contains that exact head at position N (the chain may have grown since).

The anchored bytes are a short text statement (`AnchorStatement.text()`), so a third
party can rebuild and hash it by hand:

    scruple-audit-anchor/1
    tenant default
    entries 42
    head 3f1c...

Two ways to put it outside the operator's control:

- `rfc3161`: a TSA signs the statement's SHA-256 (`scruple.audit.timestamp`). The proof
  is the signed token plus the TSA's certificate.
- `git`: the statement is committed to a git repository. A commit date is whatever the
  committer's clock says, so a git anchor is only external once the commit is pushed
  somewhere the operator cannot rewrite (a remote with protected history, or a public
  one). Scruple never pushes; it prints the command for the operator to run.
"""

from __future__ import annotations

import base64
import re
import subprocess
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Literal

import httpx
from cryptography import x509
from pydantic import BaseModel, ConfigDict, field_validator

from scruple.audit import timestamp as ts
from scruple.audit.log import verify_entries
from scruple.audit.models import AuditEntry, genesis_hash

FORMAT = "scruple-audit-anchor/1"
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class AnchorStatement(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant: str
    entries: int
    head: str

    @field_validator("tenant")
    @classmethod
    def _tenant(cls, v: str) -> str:
        if not v or any(c.isspace() for c in v):
            raise ValueError("tenant must be non-empty without whitespace")
        return v

    @field_validator("entries")
    @classmethod
    def _entries(cls, v: int) -> int:
        if v < 0:
            raise ValueError("entries must be >= 0")
        return v

    @field_validator("head")
    @classmethod
    def _head(cls, v: str) -> str:
        if not _HEX64.match(v):
            raise ValueError("head must be 64 lowercase hex characters")
        return v

    def text(self) -> str:
        return f"{FORMAT}\ntenant {self.tenant}\nentries {self.entries}\nhead {self.head}\n"

    def digest(self, hash_name: ts.HashName = "sha256") -> bytes:
        return ts.digest_of(self.text().encode("ascii"), hash_name)

    @classmethod
    def from_text(cls, text: str) -> AnchorStatement:
        lines = text.splitlines()
        if len(lines) != 4 or lines[0] != FORMAT:
            raise ValueError(f"not a {FORMAT} statement")
        fields: dict[str, str] = {}
        for line in lines[1:]:
            key, _, value = line.partition(" ")
            fields[key] = value
        if set(fields) != {"tenant", "entries", "head"} or not fields["entries"].isdigit():
            raise ValueError(f"not a {FORMAT} statement")
        st = cls(tenant=fields["tenant"], entries=int(fields["entries"]), head=fields["head"])
        if st.text() != text:
            raise ValueError("statement is not in canonical form")
        return st


def statement_for(entries: Sequence[AuditEntry], tenant: str) -> AnchorStatement:
    """The statement for the chain as it stands. Refuses a broken chain: anchoring a
    chain that does not verify would certify the tampering."""
    v = verify_entries(entries, tenant=tenant)
    if not v.ok:
        raise ValueError(f"audit chain does not verify, not anchoring it: {v.problem}")
    return AnchorStatement(tenant=tenant, entries=v.entries, head=v.head_hash)


class ChainCheck(BaseModel):
    model_config = ConfigDict(frozen=True)

    ok: bool
    anchored_entries: int
    current_entries: int
    problem: str | None = None


def check_statement(
    entries: Sequence[AuditEntry], statement: AnchorStatement, tenant: str
) -> ChainCheck:
    """Does the current chain still contain the anchored head at the anchored position?"""

    def result(problem: str | None) -> ChainCheck:
        return ChainCheck(
            ok=problem is None,
            anchored_entries=statement.entries,
            current_entries=len(entries),
            problem=problem,
        )

    if statement.tenant != tenant:
        return result(f"anchor is for tenant {statement.tenant!r}, not {tenant!r}")
    v = verify_entries(entries, tenant=tenant)
    if not v.ok:
        return result(f"audit chain does not verify: {v.problem} (seq {v.first_bad_seq})")
    if len(entries) < statement.entries:
        return result(
            f"chain has {len(entries)} entries, the anchor saw {statement.entries} (truncated)"
        )
    n = statement.entries
    head = genesis_hash(tenant) if n == 0 else entries[n - 1].entry_hash
    if head != statement.head:
        return result(f"entry {statement.entries} differs from the anchored head (rewritten)")
    return result(None)


# -- RFC 3161 ---------------------------------------------------------------------


class TimestampAnchor(BaseModel):
    """What `scruple audit anchor --tsa-url` writes. `token` is the base64 DER
    TimeStampToken; the rest is a readable copy of what the token proves."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["rfc3161"] = "rfc3161"
    statement: AnchorStatement
    tsa_url: str
    hash_algorithm: ts.HashName = "sha256"
    gen_time: datetime
    serial: str
    policy: str
    nonce: str | None
    signer_subject: str
    signer_fingerprint_sha256: str
    trusted_at_anchor: bool
    token: str

    def token_der(self) -> bytes:
        return base64.b64decode(self.token, validate=True)


def anchor_rfc3161(
    statement: AnchorStatement,
    tsa_url: str,
    trusted: list[x509.Certificate] | None,
    *,
    hash_name: ts.HashName = "sha256",
    use_nonce: bool = True,
    transport: httpx.BaseTransport | None = None,
) -> TimestampAnchor:
    req = ts.build_request(statement.digest(hash_name), hash_name, use_nonce=use_nonce)
    resp = ts.request_timestamp(tsa_url, req, transport=transport)
    info = ts.verify_response(resp, req, trusted)
    return TimestampAnchor(
        statement=statement,
        tsa_url=tsa_url,
        hash_algorithm=hash_name,
        gen_time=info.gen_time,
        serial=str(info.serial),
        policy=info.policy,
        nonce=None if info.nonce is None else str(info.nonce),
        signer_subject=info.signer_subject,
        signer_fingerprint_sha256=info.signer_fingerprint_sha256,
        trusted_at_anchor=trusted is not None,
        token=base64.b64encode(info.token).decode("ascii"),
    )


def verify_rfc3161(anchor: TimestampAnchor, trusted: list[x509.Certificate]) -> ts.TokenInfo:
    """Re-verify the stored token from scratch against `trusted` TSA certificates; the
    readable fields in the file are ignored except where they must agree."""
    info = ts.verify_token(
        anchor.token_der(),
        anchor.statement.digest(anchor.hash_algorithm),
        trusted,
        hash_oid=ts.HASH_OIDS[anchor.hash_algorithm],
    )
    if info.gen_time != anchor.gen_time or str(info.serial) != anchor.serial:
        raise ts.TimestampError("anchor file fields disagree with its token (edited)")
    return info


def load_certs(paths: Sequence[Path]) -> list[x509.Certificate]:
    out: list[x509.Certificate] = []
    for p in paths:
        data = p.read_bytes()
        if b"-----BEGIN" in data:
            out.extend(x509.load_pem_x509_certificates(data))
        else:
            out.append(x509.load_der_x509_certificate(data))
    return out


# -- git --------------------------------------------------------------------------


class GitAnchor(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["git"] = "git"
    statement: AnchorStatement
    repo: str
    path: str
    commit: str
    committed_at: str


def default_git_path(tenant: str) -> str:
    return f"audit-anchors/{tenant}.txt"


def _git(repo: Path, *args: str) -> str:
    try:
        done = subprocess.run(  # noqa: S603 (fixed argv, no shell)
            ["git", "-C", str(repo), *args],  # noqa: S607
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise ValueError("git is not installed") from exc
    except subprocess.CalledProcessError as exc:
        raise ValueError(f"git {args[0]} failed: {exc.stderr.strip()}") from exc
    return done.stdout


def _safe_path(path: str) -> str:
    p = Path(path)
    if p.is_absolute() or ".." in p.parts or not path:
        raise ValueError("git anchor path must be relative and inside the repository")
    return p.as_posix()


def anchor_git(statement: AnchorStatement, repo: Path, path: str | None = None) -> GitAnchor:
    """Write the statement to `path` in `repo` and commit only that file. Uses the
    repository's own git identity; never pushes."""
    rel = _safe_path(path or default_git_path(statement.tenant))
    _git(repo, "rev-parse", "--git-dir")
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(statement.text(), encoding="ascii")
    _git(repo, "add", "--", rel)
    msg = f"audit anchor: tenant {statement.tenant}, {statement.entries} entries"
    _git(repo, "commit", "--quiet", "--allow-empty", "--only", "-m", msg, "--", rel)
    commit = _git(repo, "rev-parse", "HEAD").strip()
    return GitAnchor(
        statement=statement,
        repo=str(repo),
        path=rel,
        commit=commit,
        committed_at=_git(repo, "show", "-s", "--format=%cI", commit).strip(),
    )


def read_git_anchor(repo: Path, commit: str, path: str) -> GitAnchor:
    rel = _safe_path(path)
    if commit.startswith("-"):
        raise ValueError("bad commit name")
    full = _git(repo, "rev-parse", "--verify", f"{commit}^{{commit}}").strip()
    text = _git(repo, "show", f"{full}:{rel}")
    return GitAnchor(
        statement=AnchorStatement.from_text(text),
        repo=str(repo),
        path=rel,
        commit=full,
        committed_at=_git(repo, "show", "-s", "--format=%cI", full).strip(),
    )
