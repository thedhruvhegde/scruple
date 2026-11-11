# Vendor security questionnaire: answer pack

Common questions from SIG Lite and CAIQ style questionnaires, grouped by domain and
answered from controls that exist today. **Yes** means the control exists in code or
in a written procedure, with the evidence named. **Partial** says which part. **Not
yet** means it does not exist; it is never answered as yes. Questions are paraphrased;
map them to the buyer's numbering when filling their form.

As of 2026-11-11. Re-check each answer against the code before sending. Deeper pilot
detail: docs/pilot/SECURITY.md. Policies: the other files in docs/trust/. Not legal
advice.

## Company context

scruple is pre-revenue, one founder, no employees. The product is a Python engine
(image and package) that partners run in their own environment by default. There is
no hosted production service; a read-only demo on synthetic data has a deployment
recipe (deploy/demo/) and is not deployed.

## A. Governance and compliance

| question | answer |
|---|---|
| Do you hold SOC 2, ISO 27001, PCI DSS or similar? | **Not yet.** No audit has been done or scheduled. A readiness policy set exists (docs/trust/). |
| Do you have written security policies? | **Yes**, written 2026-11-11: access control, change management, incident response, vendor management, data retention, business continuity (docs/trust/). None has been independently reviewed. |
| Who is responsible for security? | The founder. There is no separate security function. |
| Do you perform risk assessments? | **Partial.** Threat models exist for the audit log (docs/DESIGN.md "Audit threat model") and the verifiers (docs/VERIFIER_FINDINGS.md, tests/test_known_gaps.py). No company-wide risk register. |
| Are you subject to PCI DSS? | The engine is built not to store card numbers: a named column is tokenised and the case store refuses clear-text card numbers (tests/test_ingest_pipeline.py). Whether a deployment is in PCI scope is the partner's assessment with their QSA. |
| Cyber insurance? | **Not yet.** |

## B. Human resources

| question | answer |
|---|---|
| Background checks, security training, NDAs for staff? | **N/A today** (no staff). The policies apply to the first hire or contractor; not yet exercised. |
| Acceptable use and confidentiality agreements? | Mutual NDA and pilot terms before any data (docs/pilot/TERMS_DRAFT.md, draft for counsel). |

## C. Asset and data management

| question | answer |
|---|---|
| Data inventory and classification? | **Yes**, for what the engine holds (docs/trust/DATA_RETENTION.md): case store (partner data, redacted and tokenised), gate database (derived records), reports (aggregates, small cells suppressed). |
| What partner data do you need? | Dispute records: ids, amounts, currency, status, reason codes, dates, which evidence fields exist, balance movements (docs/pilot/DATA_CHECKLIST.md). No free text, no card numbers. |
| Is data used to train models? | **No.** scruple trains no models on partner data. |
| Synthetic and real data separated? | **Yes.** A case store holds one or the other; every result says which (docs/INTEGRATIONS.md). |
| Data deletion at contract end? | **Yes**, procedure with written confirmation (docs/pilot/RUNBOOK.md "Deletion"). Audited per-tenant retention purges (`scruple retention`). Backups expire on their own schedule. |

## D. Access control

| question | answer |
|---|---|
| Is every API request authenticated? | **Yes.** API keys (salted scrypt, shown once) or OIDC bearer tokens; anonymous only `/health`, `/ready`, `/version` (`scruple/auth/`, tests/test_auth_rbac.py). |
| Role-based access? | **Yes.** Five roles; every route declares a permission; undeclared routes refused (`scruple/auth/rbac.py`). |
| Separation of duties? | **Yes** in the product: proposer cannot approve; owner and agent cannot approve; N of M approvals (`scruple/gate/approval.py`). **Not yet** inside scruple the company (one person). |
| SSO / SAML / SCIM? | **Partial.** OIDC bearer tokens are verified (`scruple/auth/oidc.py`). SAML and SCIM: not yet. |
| MFA? | For the engine: delegated to the partner's OIDC issuer. For company accounts: process, not verified here. |
| Credential expiry and revocation? | **Yes.** `--expires-days`; `scruple keys revoke` effective on the next request; both audited. |
| Multi-tenant isolation? | **Yes.** Tenant from the credential; every row keyed and filtered by tenant; separate audit chains; cross-tenant property tests (tests/test_tenancy_api.py). |
| Privileged access reviews? | **Not yet** done; procedure in docs/trust/ACCESS_CONTROL.md. |

## E. Application security and development

| question | answer |
|---|---|
| Secure development lifecycle? | **Partial.** Every commit: ruff, mypy strict, pytest (about 7,400 tests), in CI on push (`.github/workflows/ci.yml`). Strict input models (`extra="forbid"`). No second reviewer (one person). |
| Static analysis? | **Yes**: ruff with the bandit-derived `S` rules, mypy strict. |
| Secrets scanning? | **Yes**: gitleaks and detect-private-key in pre-commit (`.pre-commit-config.yaml`). Not enforced in CI. |
| Dependency management? | **Yes**: locked (`uv.lock`), installed `--locked`. **Not yet**: automated dependency vulnerability scanning. |
| Container image scanning, SBOM, signing? | **Yes, unexercised**: Trivy, SBOM, provenance and cosign keyless in the release workflow; it has never run on GitHub (docs/DEPLOY.md). |
| DAST? | **Partial**: a ZAP baseline (passive) workflow (deploy/github-workflows/zap.yml); never run on GitHub; does not reach the JSON API (docs/trust/PENTEST_SCOPE.md). |
| Penetration test? | **Not yet.** Scope written: docs/trust/PENTEST_SCOPE.md. |
| Input validation? | **Yes**: Pydantic strict models, byte and depth limits, defusedxml, fuzz tests (docs/pilot/SECURITY.md section 3). |
| Web console protections? | **Yes**: CSRF tokens, strict CSP without inline script, `X-Frame-Options: DENY`, no-store caching (`scruple/console/security.py`). The JSON API sets no such headers (known, PENTEST_SCOPE.md). |

## F. Cryptography

| question | answer |
|---|---|
| Encryption in transit? | **Not terminated by scruple**: run behind the partner's TLS proxy; the demo recipe uses Caddy with Let's Encrypt. |
| Encryption at rest? | **Partial**: AES-256-GCM field encryption for chosen fields, context-bound (`scruple/security/crypto.py`); full-disk or database encryption is the platform's (RDS encryption in the Terraform skeleton). |
| Key management? | **Partial**: keys from the environment with ids and rotation; crypto-shredding by removing a key. **Not yet**: KMS or HSM, per-tenant keys, encryption counters. |
| Card data tokenisation? | **Yes**: keyed HMAC-SHA256 tokens (`scruple/security/pan.py`). |
| Password/credential hashing? | **Yes**: salted scrypt for API keys. |

## G. Logging, monitoring and integrity

| question | answer |
|---|---|
| Audit trail of changes? | **Yes**: hash-chained, append-only audit log per tenant, enforced by database triggers (`scruple/audit/log.py`, `scruple/storage/`). |
| Tamper evidence? | **Yes**: `scruple audit verify` recomputes the chain and checks rules and decisions against it; `/ready` reports a broken chain. |
| Anchoring outside your control? | **Yes, on demand**: `scruple audit anchor` gets an RFC 3161 timestamp or commits the head to a git repository the partner controls; `verify-anchor` checks it offline (`scruple/audit/anchor.py`). Not scheduled; no revocation checks. |
| Application logs? | **Yes**: one JSON object per line with request ids; card numbers scrubbed by a filter where installed. **Not yet**: central log store and retention. |
| Monitoring and alerting? | **Partial**: Prometheus metrics, optional tracing; alert rules proposed in docs/SLOs.md; nobody is paged. |

## H. Infrastructure

| question | answer |
|---|---|
| Hosting? | Partner environment by default. Recipes: compose, Helm chart (never installed on a cluster), AWS Terraform skeleton (never applied), single-VM demo (not deployed). |
| Container hardening? | **Yes**: non-root uid 10001, read-only root filesystem, no capabilities in compose, tini, digest-pinned base images (`Dockerfile`, tests/test_deploy.py). |
| Network segmentation? | **Partial**: compose binds to 127.0.0.1; Helm ships a NetworkPolicy option; the ALB never serves `/metrics`. |
| Physical security? | **N/A**: no offices or servers; inherited from the partner's or the cloud provider's facilities. |

## I. Incident response and continuity

| question | answer |
|---|---|
| Incident response plan? | **Yes**, written (docs/trust/INCIDENT_RESPONSE.md, docs/DEPLOY.md "Incident response"). Never exercised. |
| Breach notification? | Same day to the affected partner's owner for anything touching their data or evidence; contractual timelines in the pilot terms, for counsel. |
| Backups and restore tested? | **Partial**: verified backup and restore scripts, tested against a local Postgres (tests/test_deploy_backup.py). No drill against a real backup yet. |
| BCP / DR plan, RPO / RTO? | **Partial**: plan with proposed targets (docs/trust/BCP.md); not measured. Key-person risk: one founder. |

## J. Third parties

| question | answer |
|---|---|
| Sub-processors of partner data? | **None.** No vendor processes partner data on scruple's behalf (docs/trust/VENDOR_MANAGEMENT.md). |
| Vendor risk reviews? | **Not yet** performed; inventory and rule written. |

## K. AI-specific

| question | answer |
|---|---|
| Does the product call an LLM with our data? | **No** in the gate and the pilot path. Model API runners exist for research evaluations on simulated episodes (`scruple/agents/runners/`). |
| How are AI-made changes controlled? | That is the product: a learned rule goes live only after shadow replay with verifiers, canary and a named human approval, with an audit trail and an evidence pack (docs/ARCHITECTURE.md). |
| Are results from simulation labelled? | **Yes**: synthetic results say so; real-data results name the dataset (docs/data/CFPB.md). |
