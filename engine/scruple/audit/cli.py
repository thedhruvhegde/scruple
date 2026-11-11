"""`scruple audit verify | anchor | verify-anchor`.

    scruple audit anchor --tsa-url https://tsa.example/tsr --tsa-cert tsa.pem --out anchor.json
    scruple audit anchor --git-repo ../audit-anchors          # commits, never pushes
    scruple audit verify-anchor anchor.json --tsa-cert tsa.pem
    scruple audit verify-anchor --git-repo ../audit-anchors --commit <sha>

Anchoring and verifying are read-only on the database (no baseline import).
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from scruple.audit import anchor as an
from scruple.audit.models import AuditEntry
from scruple.audit.timestamp import TimestampError

if TYPE_CHECKING:
    from scruple.service import AgentGate

GateFactory = Callable[..., "AgentGate"]


def add_audit_args(au: argparse.ArgumentParser) -> None:
    au.add_argument("action", choices=["verify", "anchor", "verify-anchor"])
    au.add_argument(
        "anchor_file", nargs="?", default=None, help="verify-anchor: the anchor JSON file"
    )
    au.add_argument("--expected-head", default=None, help="verify: externally anchored head")
    au.add_argument("--tsa-url", default=None, help="anchor: RFC 3161 TSA endpoint")
    au.add_argument(
        "--tsa-cert",
        action="append",
        default=[],
        type=Path,
        help="trusted TSA certificate or its issuer (PEM or DER); repeatable",
    )
    au.add_argument("--hash", choices=["sha256", "sha384", "sha512"], default="sha256")
    au.add_argument("--no-nonce", action="store_true", help="anchor: send no nonce")
    au.add_argument("--out", type=Path, default=None, help="anchor: write the anchor JSON here")
    au.add_argument("--git-repo", type=Path, default=None, help="git repository for the anchor")
    au.add_argument("--git-path", default=None, help="file in the repo (audit-anchors/<tenant>)")
    au.add_argument("--commit", default=None, help="verify-anchor: git commit holding the anchor")


def dispatch(args: argparse.Namespace, gate_factory: GateFactory) -> int:
    if args.action == "anchor":
        return cmd_anchor(args, gate_factory)
    if args.action == "verify-anchor":
        return cmd_verify_anchor(args, gate_factory)
    raise AssertionError(args.action)  # pragma: no cover


def _chain(args: argparse.Namespace, gate_factory: GateFactory) -> tuple[list[AuditEntry], str]:
    audit = gate_factory(args, bootstrap=False).audit
    return audit.entries(), audit.tenant


def cmd_anchor(args: argparse.Namespace, gate_factory: GateFactory) -> int:
    if not args.tsa_url and not args.git_repo:
        raise ValueError("anchor needs --tsa-url or --git-repo (or both)")
    entries, tenant = _chain(args, gate_factory)
    st = an.statement_for(entries, tenant)
    print(f"statement: tenant {st.tenant}, {st.entries} entries, head {st.head}")
    if args.tsa_url:
        trusted = an.load_certs(args.tsa_cert) if args.tsa_cert else None
        a = an.anchor_rfc3161(
            st, args.tsa_url, trusted, hash_name=args.hash, use_nonce=not args.no_nonce
        )
        out = args.out or Path(f"audit-anchor-{tenant}-{st.entries}.json")
        out.write_text(json.dumps(a.model_dump(mode="json"), indent=2, sort_keys=True) + "\n")
        print(f"rfc3161: genTime {a.gen_time.isoformat()} serial {a.serial} by {a.signer_subject}")
        if trusted is None:
            print("  WARNING: no --tsa-cert given; signature checked, TSA trust NOT checked")
        print(f"  wrote {out}")
    if args.git_repo:
        g = an.anchor_git(st, args.git_repo, args.git_path)
        print(f"git: committed {g.path} as {g.commit[:12]} ({g.committed_at})")
        print("  not pushed; the anchor is external only once pushed to a protected remote:")
        print(f"  git -C {g.repo} push")
    return 0


def cmd_verify_anchor(args: argparse.Namespace, gate_factory: GateFactory) -> int:
    entries, tenant = _chain(args, gate_factory)
    try:
        st, what = _load_anchor(args, tenant)
    except TimestampError as exc:
        print(f"anchor: {exc}")
        print("ANCHOR FAILED")
        return 1
    check = an.check_statement(entries, st, tenant)
    print(what)
    print(
        f"chain: anchored {check.anchored_entries} entries, now {check.current_entries}, "
        f"head {st.head[:16]}"
    )
    if not check.ok:
        print(f"  PROBLEM: {check.problem}")
        print("ANCHOR FAILED")
        return 1
    print("ANCHOR OK")
    return 0


def _load_anchor(args: argparse.Namespace, tenant: str) -> tuple[an.AnchorStatement, str]:
    data: dict[str, object] | None = None
    if args.anchor_file:
        data = json.loads(Path(args.anchor_file).read_text())
    if data is not None and data.get("kind") == "rfc3161":
        a = an.TimestampAnchor.model_validate(data)
        if not args.tsa_cert:
            raise ValueError("verify-anchor of an rfc3161 anchor needs --tsa-cert")
        info = an.verify_rfc3161(a, an.load_certs(args.tsa_cert))
        return a.statement, (
            f"rfc3161: token OK, genTime {info.gen_time.isoformat()}, serial {info.serial}, "
            f"TSA {info.signer_subject}"
        )
    repo, commit, path = args.git_repo, args.commit, args.git_path
    if data is not None:
        ga = an.GitAnchor.model_validate(data)
        repo, commit, path = repo or Path(ga.repo), commit or ga.commit, path or ga.path
    if repo is None or commit is None:
        raise ValueError("verify-anchor needs an anchor file, or --git-repo and --commit")
    g = an.read_git_anchor(repo, commit, path or an.default_git_path(tenant))
    return g.statement, f"git: {g.path} at {g.commit[:12]} (committed {g.committed_at})"
