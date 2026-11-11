# v1 plan: from pilot-ready to a paid pilot and a v1

Status on 2026-11-11: groups B (except B05 hosting, recipe only), D, E and F are built and merged
(405 engine commits, 8,361 tests). C01 (leaderboard) and B06/B07 (pilot page, one-page pdf)
are live on the site. Open: group A (owner decisions), C02-C06 (more model runs), and real
services (stripe live pull, slack, jira, linear) never exercised against the real apis. Each line is one planned commit
(key + ten words or fewer). Groups A and B need decisions from the owner first.

## A. owner decisions (no code until decided)

- A1 choose a licence (see LICENSE-PENDING.md); unblocks the pypi release
- A2 buy a domain; wire it to the site and a contact email
- A3 set pilot pricing in docs/pilot/README.md (options A and B are drafted)
- A4 approve or decline the narrowly scoped red-team search
- A5 pick the first 10 pilot targets from docs/pilot/OUTREACH.md

## B. pilot blockers (needed before the first paid pilot)

```
B01 [!] split evd-02 so honest citations stop failing the gate
B02 [+] add stripe test-mode pull: disputes by api key, paged
B03 [+] add one-command pilot run: ingest, backtest, shadow, report
B04 [+] add pilot readout generator filled from real cli outputs
B05 [+] add hosted demo instance config for one region
B06 [+] add contact form on the site posting to email
B07 [+] add a one-page pilot leave-behind as pdf
B08 [#] write pilot runbook: day one to readout, both sides
B09 [~] rebuild benchmark v1 references after the engine fixes
B10 [+] add per-partner seed salt and manifest handover script
```

## C. evaluation depth (results people can cite)

```
C01 [+] add leaderboard page on the site from eval json
C02 [+] run every claude model through the api runner path
C03 [+] grow real-model samples to 100 dispute seeds each
C04 [#] preregister e6: larger incentive test across providers
C05 [+] add cost and latency per model to the leaderboard
C06 [+] add real-situation suite results for every model
```

## D. product v1

```
D01 [+] add slack approvals with signed links
D02 [+] add jira and linear change tickets for gate events
D03 [+] add outbound webhooks for gate decisions
D04 [+] add policy files for per-type thresholds and profiles
D05 [+] add scruple init: guided setup for a new partner
D06 [~] console: pagination, search and saved filters
D07 [+] add saml sso alongside oidc
D08 [+] add usage metering per rollout and gate run
D09 [+] add billing hooks through stripe for subscriptions
D10 [#] write public docs site: quickstart, concepts, api
```

## E. more money-movement workflows

```
E01 [+] add aml alert triage env on synthetic transaction graphs
E02 [+] add typologies: structuring, mule rings, rapid movement
E03 [+] add sar decision verifier weighted against false negatives
E04 [+] add payables env: invoices, duplicates, vendor fraud
E05 [+] add bank detail change fraud scenario for payables
```

## F. trust and compliance

```
F01 [#] write soc 2 readiness policies: access, change, incident
F02 [+] add audit head anchoring to an external timestamp
F03 [#] write a vendor security questionnaire answer pack
F04 [+] add a pen test scope and a self-run zap scan in ci
```

Order of work once approved: B, then C01-C03, then D01-D05, then the rest.
