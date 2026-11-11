# Vendor management policy

Owner: the founder. Review: every six months and before adding a vendor that touches
partner data. Not legal advice; see README.md for the status words.

## Rule

A vendor that would store or process partner data needs, before it does: a written
reason, its security documentation read (SOC 2 report or equivalent), a data
processing agreement where the law or the partner requires one, and the partner's
agreement in the pilot terms. **Today no vendor processes partner data on scruple's
behalf**: the pilot runs in the partner's environment by default, and the engine
makes no outbound calls to run the gate (docs/pilot/SECURITY.md section 1).

## Inventory

| vendor | used for | partner data? | how it is limited | status |
|---|---|---|---|---|
| GitHub (repository, Actions, Container Registry, Pages) | source, CI, release images, the marketing site | no | workflows default to `contents: read`; actions in the ZAP workflow pinned to commit SHAs (the release workflow still uses tags: docs/DEPLOY.md "Known gaps") | in use |
| Python packages from PyPI | the engine's dependencies | runs with it | versions locked in `uv.lock`, installed `--locked`; image scanned by Trivy at release | in use |
| Docker Hub and GHCR base images (python, postgres, uv, caddy) | the image and compose stacks | runs with it | pinned by digest in the Dockerfile; caddy in deploy/demo is pinned by tag only (to fix before use) | in use |
| Stripe, Adyen | the partner's processors, read by ingest | the partner's own data, read in their account | read-only client (`GET` only), test mode unless `allow_live` (`scruple/ingest/stripe/`) | the partner's vendors, not scruple's |
| Anthropic, OpenAI-compatible and Gemini model APIs | model evaluations on simulated episodes (`scruple/agents/runners/`, `providers/`) | no: the runners take simulator episodes | keys from the environment; tests use fake transports | in use for research only; not part of a pilot |
| RFC 3161 time-stamping authority (chosen per use) | audit head anchors (`scruple audit anchor --tsa-url`) | no: it receives a SHA-256 digest of a four-line statement | the token is verified against the TSA certificate the operator pins | optional |
| Let's Encrypt (through Caddy) | TLS for the hosted demo | no | | demo recipe only, not deployed |
| AWS | the Terraform skeleton (deploy/terraform/aws) | would host a single-tenant deployment | | written, never applied |
| domain registrar, email | the site and contact | no | | owner decision pending (docs/V1_PLAN.md A2) |

## Ongoing

| activity | status |
|---|---|
| re-read each in-use vendor's security page or SOC 2 report yearly | process, not yet done |
| dependency updates: refresh base image digests monthly or when a scan flags a fixed CVE (docs/DEPLOY.md) | process |
| pin every third-party action to a commit SHA | done for zap.yml; not yet for release.yml and ci.yml |
| a vendor breach that could affect a deployment is an incident (INCIDENT_RESPONSE.md) | process |
| offboarding a vendor: revoke its tokens, remove its access, confirm deletion in writing | process |
