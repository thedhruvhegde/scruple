# Access control policy

Owner: the founder. Review: every six months, and when a role, credential type or
system is added. Not legal advice; see README.md for what "code", "process" and
"not yet" mean.

## Scope

1. The scruple engine wherever it runs (partner environment, a scruple-run machine,
   the hosted demo): its API, console, command line and databases.
2. Company systems: the source repository and CI on GitHub, the container registry
   (GHCR), the domain and any cloud account used to run a service.

## Principles

- Every access is by a named identity. Shared credentials only for the public,
  read-only, synthetic hosted demo.
- Least privilege: grant the smallest role that does the job, with an expiry.
- Separation of duties: whoever proposes a rule change may not approve it.
- Revoke on the day a person or system no longer needs access.

## The engine

| control | how | status |
|---|---|---|
| every request authenticated before routing | `scruple/auth/middleware.py`; anonymous requests reach only `/health`, `/ready`, `/version`; tests/test_auth_rbac.py calls every route anonymously (401) | code |
| identity comes from the credential, never a header | `X-Actor` ignored unless the development-only flag is on, which logs a warning and flags every response (`scruple/settings.py`, tests/test_auth_keys.py) | code |
| API keys hashed, shown once | salted scrypt, lookup by public id, constant-time compare (`scruple/auth/keys.py`) | code |
| key expiry and revocation | `scruple keys create --expires-days N`, `scruple keys revoke ID`, effective on the next request; creation and revocation are audit events | code |
| OIDC for people | RS256/ES256 JWTs against the issuer's JWKS, `iss`/`aud`/`exp` required (`scruple/auth/oidc.py`, tests/test_auth_oidc.py) | code |
| roles | proposer, service, approver, risk_admin, auditor (`scruple/auth/rbac.py`); every route declares a permission; undeclared routes are refused | code |
| separation of duties | one credential may not hold propose and approve; owner, proposer and learning agent cannot approve; N of M approvals (`scruple/gate/approval.py`, tests/test_gate_approval.py) | code |
| tenant isolation | tenant from the credential; every row and query carries it; separate audit chains (tests/test_tenancy_api.py, tests/test_tenancy_storage.py) | code |
| tenant offboarding | `scruple tenant offboard T` is a dry run; with `--yes` it records the offboarding, revokes every key of T, purges its imported-shadow rows and case store, each step in T's audit chain; afterwards new keys for T and any token or session naming T are refused (`scruple/tenancy/`, tests/test_tenancy_admin.py) | code |
| tenant list and quota | `scruple tenant list`, `show`, `create`; `scruple tenant quota T --gate-runs-per-day N` caps shadow replays per UTC day (429 when used up); `GET /tenant` shows the caller's own tenant to `admin` | code |
| brute force | per-address limit on refused credentials, then 429 before hashing (`scruple/auth/ratelimit.py`) | code |
| database role cannot rewrite history | Postgres: service runs as a non-owner role with column grants (`scruple.storage.postgres.app_role_grants`); triggers refuse UPDATE/DELETE/TRUNCATE on history | code; granting the role is a deploy step (process) |
| metrics closed by default | `/metrics` 404 unless a token or explicit public flag (docs/DEPLOY.md) | code |
| keys per person, least role, expiry set | the operator chooses role and `--expires-days` | process |
| quarterly key review | `scruple tenant list`, then `scruple keys list --tenant T` per tenant; revoke unused (`last used never` or old) | process, not yet done |
| SAML, mTLS, SCIM provisioning | | not yet |

## Company systems

| control | how | status |
|---|---|---|
| repository and CI | GitHub account of the founder; workflows run with `permissions: contents: read` by default and only the release job may write packages (deploy/github-workflows/*.yml, tests/test_deploy_release.py, tests/test_deploy_zap.py) | code (workflow files); account settings process |
| two-factor authentication on GitHub, registrar, cloud accounts | | process; not verified in this document |
| secrets never in the repository | gitleaks and detect-private-key in pre-commit (`.pre-commit-config.yaml`); settings refuse secrets from config files (tests/test_settings.py) | code (checks run when pre-commit is installed) |
| secrets in a manager, not in shell history or chat | environment variables only; operators keep values in a secret manager | process |
| no partner data on laptops | pilot data stays in the partner's environment or the agreed machine (docs/pilot/SCOPE.md) | process |
| access removal when someone leaves | there is no one else yet; the policy applies to the first hire or contractor | not yet exercised |
