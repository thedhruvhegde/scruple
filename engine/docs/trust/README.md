# Trust: policies, questionnaire, pen-test scope

Status, November 2026: **scruple has no SOC 2 report and no audit is scheduled.** These
documents are a readiness set: the policies a SOC 2 (Security, Availability,
Confidentiality) examination would ask for, written for a company of one founder and
no employees, with each statement marked by how it is met:

- **code**: enforced by the engine, with the file and the test that show it;
- **process**: a thing a person must do; nothing in the code makes it happen;
- **not yet**: true only once it is built or done. Not claimed anywhere else.

Not legal advice. Nothing here is a statement that scruple meets any regulation,
network rule or framework; a SOC 2 opinion only comes from an independent CPA firm.

| file | what |
|---|---|
| ACCESS_CONTROL.md | who and what can reach systems and data, keys, roles, tenancy |
| CHANGE_MANAGEMENT.md | how code, infrastructure and learned rules change |
| INCIDENT_RESPONSE.md | detect, contain, tell, fix, write it down |
| VENDOR_MANAGEMENT.md | third parties the engine and the company rely on |
| DATA_RETENTION.md | what is kept, for how long, how it is deleted |
| BCP.md | backups, restore, rollback, the single-founder risk |
| QUESTIONNAIRE.md | answers to common vendor security questions (SIG-lite / CAIQ style) |
| PENTEST_SCOPE.md | scope for an external test, and the self-run ZAP scan in CI |

Related: docs/pilot/SECURITY.md (pilot questionnaire, older and narrower),
docs/DEPLOY.md (operations), docs/SLOs.md (proposed service levels).

## Gaps that cut across every policy

| gap | effect |
|---|---|
| one person | no separation of duties between people: the founder writes, reviews, deploys and operates. The engine's own separation of duties applies to partners' rules, not to scruple's code. |
| no SOC 2, no external pen test, no audit of these policies | everything here is self-asserted until tested by someone else |
| hosted production does not exist | the policies for operating a service are written but have not been exercised; only the demo recipe (deploy/demo/) and partner-run deployments exist |
| no central log store, no paging | alerts in docs/SLOs.md are proposals; nobody is paged |
| audit anchors are on demand | `scruple audit anchor` exists (RFC 3161 or git); nothing schedules it. RFC 3161 verification does not check revocation (CRL/OCSP) and follows one issuer level only (`scruple/audit/timestamp.py`) |
| secrets from the environment only | no vault or KMS integration |

## Review

Each policy names a review interval. A review means: re-read against the code, fix
what drifted, note the date in the file's history (git log). None has been reviewed
since it was written on 2026-11-11.
