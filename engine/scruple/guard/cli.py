"""`scruple guard ...`: Scruple Guard from the terminal.

    scruple guard sign    --mandate m.json --key-env SCRUPLE_GUARD_KEY   > signed.json
    scruple guard check   --mandate signed.json --request r.json --registry reg.json \
                          [--state s.json] [--key-env VAR] [--chain chain.json] [--audit-db DB]
    scruple guard verify  --chain chain.json [--head HASH]
    scruple guard demo    [--seed N]
    scruple guard redteam [--seeds N] [--json]   (commerce exploits XC1-XC9)

`check` prints the decision (allow / step_up / deny, with every rule's result) and, with
`--chain`, appends the evidence record to that JSON file (created if missing); with
`--audit-db`, it also appends the record to that database's tenant audit trail
(event `guard.decision`). Exit code: 0 allow, 3 step_up, 4 deny. The key comes from
an environment variable (never a flag, so it stays out of shell history); `demo` uses
a synthetic commerce case and its synthetic key.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from scruple.guard.evidence import EvidenceChain, GuardEvidence, record_to_audit, verify_chain
from scruple.guard.mandate import Mandate, MerchantRecord, PaymentRequest, sign_mandate
from scruple.guard.rules import GuardState, authorize

EXIT = {"allow": 0, "step_up": 3, "deny": 4}


def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=2, sort_keys=True, default=str))


def _key(var: str) -> str:
    key = os.environ.get(var)
    if not key:
        raise ValueError(f"set the signing key in ${var}")
    return key


def _load(path: str) -> Any:
    return json.loads(Path(path).read_text())


def _registry(raw: Any) -> dict[str, MerchantRecord]:
    items = raw.values() if isinstance(raw, dict) else raw
    recs = [MerchantRecord.model_validate(x) for x in items]
    return {r.merchant_id: r for r in recs}


def cmd_sign(args: argparse.Namespace) -> int:
    m = Mandate.model_validate(_load(args.mandate))
    _print(sign_mandate(m, _key(args.key_env)).model_dump(mode="json"))
    return 0


def _chain(path: str | None) -> EvidenceChain:
    if path is None or not Path(path).exists():
        return EvidenceChain()
    return EvidenceChain([GuardEvidence.model_validate(x) for x in _load(path)])


def cmd_check(args: argparse.Namespace) -> int:
    m = Mandate.model_validate(_load(args.mandate))
    req = PaymentRequest.model_validate(_load(args.request))
    reg = _registry(_load(args.registry))
    st = (
        GuardState.model_validate(_load(args.state))
        if args.state
        else GuardState(mandate_id=m.mandate_id)
    )
    d = authorize(m, req, st, reg, key=_key(args.key_env))
    out: dict[str, Any] = {
        "verdict": d.verdict,
        "reasons": [r.model_dump() for r in d.reasons],
        "rules": [r.model_dump() for r in d.rules],
        "request_digest": d.request_digest,
        "mandate_digest": d.mandate_digest,
    }
    if args.chain:
        chain = _chain(args.chain)
        rec = chain.append(d, d.verdict)
        Path(args.chain).write_text(
            json.dumps([r.model_dump(mode="json") for r in chain.records], indent=1)
        )
        out["evidence"] = {"seq": rec.seq, "hash": rec.hash, "prev_hash": rec.prev_hash}
        if args.audit_db:
            from scruple.audit.log import AuditLog
            from scruple.clock import SystemClock
            from scruple.storage import open_storage

            entry = record_to_audit(
                AuditLog(open_storage(args.audit_db), SystemClock()), rec, req.agent_id
            )
            out["audit"] = {"seq": entry.seq, "entry_hash": entry.entry_hash}
    _print(out)
    return EXIT[d.verdict]


def cmd_verify(args: argparse.Namespace) -> int:
    recs = [GuardEvidence.model_validate(x) for x in _load(args.chain)]
    bad = verify_chain(recs, args.head)
    _print(
        {
            "ok": bad is None,
            "records": len(recs),
            "first_bad": bad,
            "head": recs[-1].hash if recs else None,
        }
    )
    return 0 if bad is None else 1


def cmd_demo(args: argparse.Namespace) -> int:
    """Run the careful and the obedient agent on one synthetic commerce case and print
    each Guard decision."""
    from scruple.agents.commerce_baselines import careful_policy, obedient_policy, run_commerce

    rows: dict[str, Any] = {}
    for name, pol in (("careful", careful_policy), ("obedient", obedient_policy)):
        ep = run_commerce(pol, args.seed, args.case_type)
        rec = ep.record()
        rows[name] = {
            "decisions": [
                {
                    "request": x.decision.request_id,
                    "final": x.final,
                    "reasons": [f"{r.rule}: {r.detail}" for r in x.decision.reasons],
                    "hash": x.hash[:16],
                }
                for x in rec.evidence
            ],
            "scorecard": None
            if ep.score is None or ep.score.scorecard is None
            else ep.score.scorecard.summary(),
            "chain_ok": verify_chain(rec.evidence) is None,
        }
    case = ep.case
    _print(
        {
            "synthetic": True,
            "seed": args.seed,
            "task": case.task.model_dump(mode="json"),
            "scenarios": [s.value for s in case.truth.scenarios],
            "agents": rows,
        }
    )
    return 0


def cmd_redteam(args: argparse.Namespace) -> int:
    from scruple.verifiers.commerce_exploits import commerce_coverage, render_commerce

    seeds = range(args.start, args.start + args.seeds)
    rows = commerce_coverage(seeds)
    if args.json:
        _print({"seeds": [seeds.start, seeds.stop - 1], "rows": rows, "synthetic": True})
    else:
        print(render_commerce(rows, seeds))
    return 0


def add_guard_parser(sub: Any) -> None:
    p = sub.add_parser("guard", help="Scruple Guard: pre-flight checks of agent payments")
    gs = p.add_subparsers(dest="guard_cmd", required=True)
    s = gs.add_parser("sign", help="sign a mandate (key from an environment variable)")
    s.add_argument("--mandate", required=True)
    s.add_argument("--key-env", default="SCRUPLE_GUARD_KEY")
    s.set_defaults(fn=cmd_sign)
    c = gs.add_parser("check", help="check one payment request against a signed mandate")
    c.add_argument("--mandate", required=True)
    c.add_argument("--request", required=True)
    c.add_argument("--registry", required=True, help="JSON list of merchant records")
    c.add_argument("--state", default=None, help="GuardState JSON (spend so far)")
    c.add_argument("--key-env", default="SCRUPLE_GUARD_KEY")
    c.add_argument("--chain", default=None, help="evidence chain JSON file to append to")
    c.add_argument("--audit-db", default=None, help="also append to this audit trail")
    c.set_defaults(fn=cmd_check)
    v = gs.add_parser("verify", help="verify an evidence chain file")
    v.add_argument("--chain", required=True)
    v.add_argument("--head", default=None)
    v.set_defaults(fn=cmd_verify)
    d = gs.add_parser("demo", help="careful vs obedient agent on a synthetic commerce case")
    d.add_argument("--seed", type=int, default=7)
    d.add_argument("--case-type", default="injection+beneficiary_change+duplicate_charge")
    d.set_defaults(fn=cmd_demo)
    r = gs.add_parser("redteam", help="run the commerce exploits against the scorecard")
    r.add_argument("--seeds", type=int, default=100)
    r.add_argument("--start", type=int, default=0)
    r.add_argument("--json", action="store_true")
    r.set_defaults(fn=cmd_redteam)


__all__ = ["add_guard_parser"]
