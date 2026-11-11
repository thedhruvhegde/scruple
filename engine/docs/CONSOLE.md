# Web console (roadmap 181-184)

A server-rendered console over the gate and the verifiers: FastAPI router, Jinja2
templates, one small script, two self-hosted fonts. No frontend framework and nothing
loaded from a CDN. All data it shows is synthetic unless a page names a real dataset.

```bash
scruple console --db agent-gate.db --port 8090            # its own app, / -> /console/
scruple console --db agent-gate.db --reports out/ --episodes .scruple/episodes
scruple serve                                              # the API also mounts it at /console
```

Options: `--seeds N` (seeds per reference agent version, default 24), `--episodes DIR`
(an `EpisodeStore`, shown as one more agent version), `--reports DIR` (backtest
`report.json` files, one folder level), `--replay-seed` / `--replay-cases` (the public
replay set for rule diffs, default 4242 / 1000).

## Pages

| Path | Roadmap | What it shows |
|---|---|---|
| `/console/` | 181 | rule versions with status and gate verdict; agent versions with gate pass rate, correct rate, mean reward, mean ledger cost and hard failures; the latest decisions |
| `/console/runs` | | every gate decision of the tenant (shadow, canary, activation, rollback, assurance), newest first, searchable and paged |
| `/console/rules/{id}/versions/{v}` | 181 | the rule, every decision with its checks; shadow statistics (interval, arms, per case-type slices); e-values per case type (regression, running max, improvement, confidence sequence bounds, e-threshold, alpha share, required n, the plan's total); canary metrics and the sequential canary (harm and benefit e-values, confidence sequences); the ramp; every audit event for the version; evidence pack downloads |
| `/console/agents/{name}` | 181 | every episode of one agent version: case, hidden case type, action, resolution, cost, correct, gate, reward, failed checks |
| `/console/agents/{name}/episodes/{ep}` | 182 | the case viewer, rendered from the `EpisodeRecord`: case fields and the hidden truth; the timeline (stage transitions, state changes with response deadlines, clock moves, incidents); ledger postings per journal entry with a balance check; evidence documents (kind, dates, source, field names, content hash, changed, forged); the agent's tool calls, explanation and claims; every verifier check with score, weight, explanation and evidence links to the trace event, journal entry or document; the full hash-chained trace with a chain check |
| `/console/diff/agents?a=&b=&case=` | 183 | two agent versions on the same seeds: per case-type cost deltas, correct and changed counts, the cases that changed outcome, and side-by-side traces of one case |
| `/console/diff/rules?a=rule@vN&b=rule@vM&case=` | 183 | the live policy replayed with A and with B on a public synthetic set: per case-type deltas, changed cases, and side by side for one case: every rule in priority order with which matched and which decided, the outcome, the journal entries, cost and regret |
| `/console/approvals` | 184 | versions that passed canary; the policy in force; request, approve and reject forms; the approval history |
| `/console/backtests`, `/console/backtests/{name}` | | backtest reports: verdict, observed and estimated counts, statistics, per-reason e-values and intervals, planning, assumptions |

Downloads: `.../versions/{v}/evidence.json` and `.pdf` (the CI evidence pack),
`/console/decisions/{id}/report.md`, `/console/backtests/{name}/report.json`.

The rule diff uses its own replay seed, not the gate's held-out seed, so the cases a
reviewer browses are never the cases the gate decides on. Pages print seed
commitments, never the held-out or canary seeds.

## Search, filters and pages

Rule versions (`/console/`), gate runs (`/console/runs`), cases (`/console/agents/{name}`)
and the approvals inbox take the same query parameters (`scruple/console/listing.py`):
`q` (up to 100 characters; every word must appear, case-insensitive), one value per
facet (`status`, `verdict`; `stage`, `outcome`; `family`, `action`, `gate`, `correct`;
`event`), `page` and `per` (10, 25, 50 or 100). Filtering runs on the server over the
caller's tenant only; facet values must match `^[A-Za-z0-9_.@:-]{0,64}$`, anything
else is a 422. On the approvals inbox, `q` also narrows the waiting versions; the
event facet and the pages apply to the history.

Saved filters are a name for a filter's canonical query string (`q` and facets only),
stored per tenant in the append-only `console_filter_events` table (alembic 0010):
saving appends, saving a name again replaces it (latest wins), deleting appends a
tombstone. They are shared by everyone in the tenant, so saving and deleting need the
console `admin` permission (engine `admin`, the `risk_admin` role); only whoever saved
a filter may delete it. At most 50 per list. A stored query string is parsed and
re-canonicalised before it is linked. Search terms and filter names are printed
escaped; tests send `<script>`, `<img onerror>`, quote-breaking and `</textarea>`
terms to every list and check none reaches a page unescaped.

## Approvals

The console calls the service and nothing else; refusals come back as the inbox with
the service's message (403), and a refused attempt writes nothing.

- With an approval policy in force: `request` (`AgentGate.request_approval`), then
  `approve` (`grant_approval`) until the quorum and the required role are met, which
  activates the version in the same transaction. Named approvers only; the proposer,
  the rule's owner and the learning agent can never approve.
- Without a policy: `approve` (`AgentGate.approve`), one approver from
  `GateConfig.approvers`; the owner and the agent cannot approve.
- `reject` (`AgentGate.reject_approval`, added for the console): an approver (named,
  under a policy) records a BLOCK decision at the activation stage, an
  `approval.rejected` audit event with the reason, and the canary -> rejected
  transition, all in one transaction. Rejection is final.
- Every approve and reject carries a reason (3 to 500 characters). Approval reasons go
  into the `approval.granted` payload or the activation decision's reasons.

## Permissions (the engine's RBAC)

Every route declares what it needs with one dependency from
`scruple/console/permissions.py`:

```python
principal: Annotated[Principal, requires("read")]  # "read" | "approve" | "export" | "admin"
```

GET pages need `read`, downloads `export`, approval POSTs `approve`, saved-filter
POSTs `admin`. Static files are
`public()`. `requires` wraps `scruple.auth.require`, so the console uses the same
authentication as the JSON API (API key, OIDC bearer token, or the development-only
`X-Actor` trust) and the same fail-closed route guard; no credentials answers 401, a
missing permission 403. The console permissions map onto the engine's:

| console | engine | |
|---|---|---|
| `read` | `read` | every page |
| `approve` | `approve` | approve, reject, request approval |
| `export` | `read` | evidence packs and report downloads: the same data the read-only JSON API returns, so an auditor can take evidence away |
| `admin` | `admin` | save and delete the tenant's shared saved filters |

Who acts is always the authenticated subject: the forms have no name field, and a
posted `actor` field is refused (422). Each caller sees only their own tenant's gate
(mounted under the API) or, run on its own (`scruple console`), the one tenant of the
database it opened; another tenant's caller gets 404. The service still decides
whether that subject may approve (named approvers, separation of duties). Tests check
that every route declares exactly one permission and that the API-wide route
enumeration covers the console.

## Security

- CSRF: an HttpOnly, SameSite=Strict cookie holds a random browser id; every form
  carries `HMAC-SHA256(secret, id)`. A POST needs both, matching, and, when the browser
  sends `Origin`, the same origin. The secret is `SCRUPLE_CONSOLE_SECRET` (environment
  only, at least 32 characters) or random per process. Set it when running more than
  one worker.
- CSP: `default-src 'none'; script-src 'self'; style-src 'self'; font-src 'self';
  img-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'`. No
  inline script, style or event handler anywhere (tests parse every page for them).
  Also `nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: same-origin`,
  `Cache-Control: no-store` on pages.
- Escaping: Jinja autoescape on every template; tests plant markup in a rule
  description, an agent's explanation, a tool name, tool arguments, an approval reason
  and a backtest dataset name and check none reaches a page unescaped.
- Forms: urlencoded only, 8 KiB cap, one value per field, Pydantic models with
  `extra="forbid"`. Unknown query parameters are refused (422), as in the API.
- No secrets in pages: no seeds, no config, no CSRF secret (tested).

## Design

The marketing site's rules: Martian Mono for display and every screen character, IBM
Plex Sans Condensed for body text (both OFL, self-hosted in `static/fonts` with their
licences); bands in pine, bone and sand; traces on dark screens in both themes;
hovers are instant inversions; tags are bracketed text. No gradients, shadows,
rounded corners, icons, emoji or motion. Light and dark follow the system and a
LIGHT / DARK toggle (saved in localStorage). On narrow screens wide text tables become
stacked records and numeric tables scroll inside their panel; the page itself never
scrolls sideways.

## Not built

Pagination of diffs (a diff shows every changed case; fine for hundreds), live
updates, and per-person (private) saved filters.
