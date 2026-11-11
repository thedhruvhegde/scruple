# Incident response policy

Owner: the founder (incident lead by default). Review: every six months and after
every incident. Not legal advice: notification duties under law or contract are set by
the pilot terms and counsel, not by this file. See README.md for the status words.

## What counts as an incident

- Integrity: the audit chain does not verify, an anchor does not match, a decision or
  rule status differs from what the log says.
- Confidentiality: partner data, a key or a secret is somewhere it should not be (a
  log, a ticket, a laptop, a public place), or someone used access they should not have.
- Availability: a deployment scruple operates breaks its service levels (docs/SLOs.md).
- A vulnerability reported to us or found in a dependency that affects a deployment.

## Detection

| signal | source | status |
|---|---|---|
| `/ready` reports `audit_chain` broken with the first bad position | `scruple/api/health.py` | code |
| `scruple audit verify --expected-head H` or `scruple audit verify-anchor` fails | `scruple/audit/` | code; run on demand |
| `ingest verify` finds raw card numbers in a store (exit 1) | `scruple/ingest/verify.py` | code |
| error rate, latency, readiness alerts | Prometheus expressions in docs/SLOs.md | proposal; nobody is paged yet |
| image vulnerabilities | Trivy in the release workflow, ZAP baseline weekly | code; never run on GitHub |
| reports from partners or researchers | email to the founder | process; no security.txt or public address yet |

## Steps

1. **Declare.** Anyone can. Write down the time, the symptom and who is handling it.
   Start a timeline in a private note; every action goes in it with a time.
2. **Contain.** Integrity first (docs/DEPLOY.md "Incident response"): stop writers
   (scale to zero, revoke credentials with `scruple keys revoke`), snapshot the
   database before anything else, do not restart to make it go away. For a leaked
   key: revoke it, issue a new one, check `last_used_at` and the audit log for its
   subject. For leaked data: remove it at the source, list every copy.
3. **Assess.** Which tenants, decisions, records and time window. For integrity,
   compare against the last anchored head and the last backup's `.head` file.
4. **Tell.** The affected partner's named owner, the same day for anything that
   touches their data or evidence, with what is known and what is not. Contractual
   and legal timelines are in the pilot terms (docs/pilot/TERMS_DRAFT.md) and are
   counsel's to confirm.
5. **Fix.** Roll back first, debug second (docs/DEPLOY.md). Restore from backup with
   `deploy/scripts/restore.sh`, which refuses a dump whose chain or head does not
   check out. Never edit history in place; append-only is the evidence.
6. **Write it down** within five working days: timeline, impact, cause, what changes,
   and a test that would have caught it where one can be written. Weaknesses that stay
   go to tests/test_known_gaps.py and docs/VERIFIER_FINDINGS.md.

## Roles

One person holds every role today: lead, communications, fixer. When a second person
joins, the lead and the person communicating with partners should differ.

## Exercises

A tabletop exercise each half year (proposal): walk a broken-chain scenario and a
leaked-key scenario through the steps above with the commands. **Not yet done.** The
quarterly restore drill (docs/SLOs.md SLI 7) doubles as practice for step 5; also not
yet done against a real backup.
