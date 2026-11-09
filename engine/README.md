# agent-gate

**Change control for agent learning.** AI agents in financial operations increasingly
learn from experience by writing their own rules and memory. agent-gate makes sure
nothing an agent learns goes live until it has been verified against history, staged
on a canary, and approved by a human, with a tamper-evident audit trail and an
evidence report a model-risk reviewer can read.

> Prototype on synthetic data. Not for production use. No real customer data.

## Quick start

```bash
make install
make demo        # the full story in a few seconds
make test lint typecheck
```

The demo shows:

1. A **good** learned rule is replayed on 3,000 held-out disputes, beats the current
   policy (with a confidence interval) with no regressions, passes a canary, and is
   activated by a model-risk approver.
2. A **subtly bad** rule, almost identical but with a priority override, improves the
   *average* yet breaks the late-evidence edge case and violates policy. It is
   blocked, and the report names the safeguard it overrode.
3. The good rule is **rolled back**, restoring the previous policy, and the live policy
   is re-checked.
4. A **red team** of 7 attacks tries to game the verifiers. Each one fools a naive
   verifier, and agent-gate stops all of them.
5. The audit log verifies, and a **tampered copy** of the database is detected.
6. The **evidence reports** are printed and written to `demo-output/reports/`.

## API

```bash
uv run agent-gate serve   # http://127.0.0.1:8000/docs
```

`POST /rules`, `POST /rules/{id}/versions/{v}/evaluate`, `.../approve`,
`POST /rules/{id}/rollback`, `GET /decisions/{id}/report`, `GET /audit/verify`.
Identity comes from the `X-Actor` header (an authentication stub).

## Docs

- [docs/DESIGN.md](docs/DESIGN.md): architecture, threat model, what is stubbed, what comes next
- [docs/VERIFIER_FINDINGS.md](docs/VERIFIER_FINDINGS.md): attacks, findings, honest weaknesses
- [docs/DECISIONS.md](docs/DECISIONS.md) and [docs/ASSUMPTIONS.md](docs/ASSUMPTIONS.md)
- [docs/MARKET.md](docs/MARKET.md): market sizing, competitors, pricing, discovery plan
- [CLAUDE.md](CLAUDE.md): working conventions and status
