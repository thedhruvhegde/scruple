# Scruple roadmap: 200 commits to a paid pilot

From the current engine (4.2k lines, 79 tests, one dispute workflow, three verifiers, a gate,
an audit log, a CLI and a demo) to something a design partner pays to pilot.

Commit format follows the site repo: one key, then ten words or fewer, lowercase.
`[+]` add, `[!]` fix, `[~]` change, `[#]` words only.

Each phase ends in something you can show. Test counts are cumulative targets.

---

## Phase 0: foundation (001-016)
Goal: an engine that can run for a customer, not just on a laptop.
Demo: `docker compose up`, run the existing demo against Postgres.
Tests: ~150.

```
001 [~] rename package agent_gate to scruple, keep import shim
002 [+] add version command and build metadata
003 [~] split storage into one repository interface per aggregate
004 [+] add postgres storage backend behind the same interface
005 [+] add alembic migrations for the postgres schema
006 [+] add sqlite backend for local runs and tests
007 [~] make every id a typed ulid, not a string
008 [+] add structured json logging with request ids
009 [+] add settings loader with env and file sources
010 [!] reject unknown fields at every api boundary
011 [+] add github actions ci: ruff, mypy, pytest, coverage
012 [+] add pre-commit hooks for lint, types and secrets
013 [+] add dockerfile and compose for api plus postgres
014 [+] add make targets for demo, bench and release
015 [#] write architecture overview with one diagram per layer
016 [+] add hypothesis property test harness with fixed seeds
```

## Phase 1: disputes, in depth (017-040)
Goal: a dispute simulator a payments ops lead recognises as real.
Demo: one case walks inquiry to arbitration, fees and deadlines on the ledger.
Tests: ~450.

```
017 [+] add card network model: visa and mastercard rule sets
018 [+] add reason code catalogue with evidence requirements
019 [+] add lifecycle: inquiry, chargeback, representment, pre-arb, arbitration
020 [+] add stage deadlines with network time windows
021 [+] add fees per stage, posted to the ledger
022 [+] add merchant profiles: mcc, chargeback ratio, history
023 [+] add evidence documents: receipts, delivery proof, logs, refunds
024 [+] add forged and missing evidence as generator options
025 [+] add issuer behaviour model with seeded acceptance odds
026 [+] add friendly fraud and true fraud case families
027 [+] add refunds racing open disputes
028 [+] add partial disputes and split shipments
029 [+] add multi-currency amounts with fx at settlement
030 [+] add compelling evidence eligibility rules
031 [+] add merchant monitoring program thresholds and fines
032 [+] add difficulty knob for ambiguity and rarity
033 [+] add held-out seeds sealed from training splits
034 [+] add scenario dsl for hand-written edge cases
035 [+] add 40 hand-written scenarios from public network rules
036 [~] make the engine event-sourced so any step replays
037 [+] add rewind: restore a case to any event
038 [~] speed the generator to 10k cases per second
039 [#] document every rule with its public source
040 [+] add fidelity report against published dispute aggregates
```

## Phase 2: more money-movement workflows (041-060)
Goal: five environments on one shared ledger and clock.
Demo: one customer complaint becomes a dispute, a refund and a recon break.
Tests: ~800.

```
041 [+] add ach model: entries, returns, return codes, windows
042 [+] add ach unauthorized returns and reinitiation rules
043 [+] add wire model with recalls and beneficiary checks
044 [+] add instant payments with irreversibility
045 [+] add reconciliation env: bank statement against ledger
046 [+] add recon breaks: timing, duplicates, fx, fees, missing
047 [+] add recon verifier scored on breaks cleared correctly
048 [+] add aml alert triage env on synthetic transaction graphs
049 [+] add typologies: structuring, mule rings, rapid movement
050 [+] add sar decision verifier weighted against false negatives
051 [+] add refund and goodwill credit env with policy limits
052 [+] add payables env: invoices, duplicates, vendor fraud
053 [+] add bank detail change fraud scenario for payables
054 [+] add month-end close env: accruals and journal entries
055 [+] add close verifier: trial balance and tie-outs
056 [+] add cross-workflow cases spanning dispute, refund, recon
057 [~] share one ledger and clock across workflows
058 [+] add workflow registry so envs plug in by name
059 [+] add per-workflow cost model with stated assumptions
060 [#] write workflow catalogue: rules, costs, verifiers
```

## Phase 3: verifiers that resist gaming (061-082)
Goal: the moat. Every known exploit is caught, and there is a test proving it.
Demo: a reward-hacking agent scores 94% on naive metrics and fails on scruple.
Tests: ~1,200.

```
061 [~] split verifiers into weighted checks with evidence
062 [+] add ledger invariant checker after every action
063 [+] add counterfactual verifier: replay against baseline policy
064 [+] add process verifier: required steps in order
065 [+] add evidence verifier: cited documents exist and match
066 [+] add deadline verifier with partial credit by stage
067 [+] add consistency check across merchant segments
068 [+] add calibration score for agent confidence statements
069 [+] add self-report detector: agent claims against the ledger
070 [+] add scope detector: actions outside allowed tools
071 [+] add exploit library starting with 30 known patterns
072 [+] add exploit: closing cases without resolving them
073 [+] add exploit: refunding to inflate win rate
074 [+] add exploit: silently writing off small balances
075 [+] add exploit: editing evidence timestamps
076 [+] add exploit: stalling to game deadlines
077 [+] add search-based red team over policy space
078 [+] add llm red team agent that attacks verifiers
079 [+] add robustness score per environment
080 [+] add verifier versioning with rescoring of past runs
081 [+] add disagreement report between verifier versions
082 [#] publish verifier findings: each exploit and its fix
```

## Phase 4: environments for labs (083-100)
Goal: a post-training team can run rollouts in five minutes.
Demo: parallel rollouts of an open model with reward breakdowns streaming.
Tests: ~1,450.

```
083 [+] add env protocol: reset, step, observation, reward
084 [+] add gymnasium compatible wrapper
085 [+] add tool-calling observations as json schema
086 [+] add text observations for chat models
087 [+] add multi-turn episodes with tool calls as actions
088 [+] add dense and sparse reward modes
089 [+] add reward breakdown in every step
090 [+] add episode recorder with full replay files
091 [+] add jsonl task export for offline training
092 [+] add curriculum sampler by difficulty and case type
093 [+] add parallel env runner on a process pool
094 [+] add http env server for remote rollouts
095 [+] add rollout throughput benchmark
096 [+] add adapter for openenv style environment hubs
097 [+] add adapter for verifiers style rubric libraries
098 [+] add contamination guard: hashed held-out tasks, leak detection
099 [+] add environment cards: rules, rewards, known gaps
100 [#] write lab quickstart: first rollout in five minutes
```

## Phase 5: agents, sdks, plugins (101-124)
Goal: plug any agent in, from any framework, in an afternoon.
Demo: Claude Code plugin gates a prompt change from the terminal.
Tests: ~1,800.

```
101 [+] add agent interface: observe, act, explain
102 [+] add agent tool suite: ledger, cases, evidence
103 [+] add anthropic api agent runner
104 [+] add openai api agent runner
105 [+] add openai compatible runner for local models
106 [+] add claude agent sdk adapter
107 [+] add langgraph adapter
108 [+] add mcp server exposing simulator tools
109 [+] add mcp server exposing gate actions and reports
110 [+] add claude code plugin: run the gate from terminal
111 [+] add python sdk: scruple.evaluate(agent)
112 [+] add typescript sdk for node agents
113 [+] add trace import from opentelemetry genai spans
114 [+] add trace import from langsmith and langfuse exports
115 [+] add replay of production traces into the simulator
116 [+] add prompt and memory diffs between agent versions
117 [+] add model matrix: one env, many models
118 [+] add cost and latency per episode
119 [+] add token budget limits per run
120 [+] add provider rate limits and retries
121 [+] add response cache so reruns cost nothing
122 [+] add baseline agents: rules, random, oracle
123 [+] add leaderboard across baselines and models
124 [#] write an integration guide per adapter
```

## Phase 6: the gate in the release path (125-142)
Goal: no agent change ships without a scorecard.
Demo: a pull request gets blocked, a fix passes, the canary rolls back on breach.
Tests: ~2,100.

```
125 [+] add github action that gates a pull request
126 [+] add pr comment with scorecard and regressions
127 [+] add gitlab ci template
128 [+] add required status check on block
129 [+] add shadow mode: score live decisions, act on none
130 [+] add canary controller with traffic percentage
131 [+] add automatic rollback on canary breach
132 [+] add approval workflow with named approvers
133 [+] add slack approvals with signed links
134 [+] add separation of duties: authors cannot approve
135 [+] add change tickets linked to jira or linear
136 [+] add webhooks for gate events
137 [+] add gate thresholds as policy files
138 [+] add per-case-type thresholds with statistical tests
139 [+] add sequential testing so canaries stop early
140 [+] add evidence pack export as pdf and json
141 [+] add audit log anchoring to an external timestamp
142 [#] write model risk mapping: evidence per review question
```

## Phase 7: real data and security (143-160)
Goal: a pilot can safely touch a customer's history.
Demo: backtest a new rule on six months of a test-mode Stripe account.
Tests: ~2,500.

```
143 [+] add stripe disputes adapter, test mode first
144 [+] add stripe webhook ingest with signature checks
145 [+] add adyen dispute notification adapter
146 [+] add csv and parquet case import
147 [+] add ledger adapters: modern treasury, formance, generic
148 [+] add bank statement import: bai2, camt.053, csv
149 [+] add nacha file parser for ach returns
150 [+] add pii redaction before anything is stored
151 [+] add tokenised card numbers, never raw pans
152 [+] add field level encryption with key rotation
153 [+] add sim calibration from customer aggregates only
154 [+] add backtest: replay historical cases against a rule
155 [+] add retention policies per tenant
156 [+] add secrets from env or vault, never config
157 [+] add sso with oidc and saml
158 [+] add role based access control
159 [+] add tenant isolation tests
160 [#] write security overview and data flow diagram
```

## Phase 8: testing at scale, and results (161-180)
Goal: thousands of tests, plus pre-registered experiments with published results.
Demo: the results page, regenerated nightly from the frozen benchmark.
Tests: ~3,500 (most are parametrised across workflows, case types and seeds).

```
161 [+] add ledger properties: always balanced, never negative silently
162 [+] add properties for every workflow state machine
163 [+] add metamorphic tests: scaled amounts scale costs
164 [+] add golden episodes per workflow, byte for byte
165 [+] add mutation testing on verifiers
166 [+] add api fuzzing from the openapi schema
167 [+] add exploit regression suite, one test per exploit
168 [+] add cross-version determinism tests
169 [+] add load test: 1k rollouts per minute
170 [+] add chaos tests: database loss mid-promotion
171 [+] add audit tamper tests at every chain position
172 [+] add contract tests for each external adapter
173 [+] add end-to-end test: pr to canary to rollback
174 [+] add frozen benchmark of 10k held-out cases
175 [+] add experiment: verifiers catch planted regressions
176 [+] add experiment: reward-hacked policy against honest policy
177 [+] add experiment: frontier models on disputes, scored
178 [+] add nightly benchmark job with trend chart
179 [+] add results page generated from benchmark runs
180 [#] write test strategy: what numbers prove and do not
```

## Phase 9: product surface (181-192)
Goal: a buyer can log in and see their runs without a terminal.
Demo: console walkthrough: runs, a case timeline, a version diff, an approval.
Tests: ~3,800.

```
181 [+] add web console: runs, scores, cases
182 [+] add case viewer: timeline, ledger, agent reasoning
183 [+] add diff view between two agent versions
184 [+] add approvals inbox
185 [+] add orgs, projects and api keys
186 [+] add usage metering per rollout and gate run
187 [+] add billing through stripe
188 [+] add helm chart for customer vpc deploys
189 [+] add terraform module for hosted single tenant
190 [+] add health, status and metrics endpoints
191 [+] add backups and a restore runbook
192 [#] write docs site: quickstarts, concepts, api reference
```

## Phase 10: demo and pilot (193-200)
Goal: walk into a meeting, run the demo, leave with a pilot.

```
193 [+] add guided demo: agent claims 97%, scruple disproves it
194 [+] add demo dataset with three agents of different quality
195 [+] add live demo on the website through the api
196 [+] add pilot kit: scope, success criteria, data checklist
197 [+] add pilot report template filled from real runs
198 [+] add draft pilot terms and dpa for counsel review
199 [#] write security questionnaire answers from real controls
200 [~] tag v1.0.0 and freeze the benchmark
```

---

## Shortest path to a paid pilot (~90 commits)

If a design partner appears early, build these first and leave the rest:

- 001-014: foundation
- 017-040: disputes in depth
- 061-076: verifiers and the first exploits
- 101-111: agent runners, mcp server, claude code plugin, python sdk
- 125-132, 140: github action, shadow mode, canary, approvals, evidence pack
- 143-154: stripe, csv import, redaction, backtest
- 161-176: properties, golden episodes, exploits, frozen benchmark, two experiments
- 193-197: demo and pilot kit

This is a disputes-only pilot. Phases 2 and 4 (more workflows, lab environments) are the
expansion pitch, not the entry.

## What the tests will and will not prove

- Thousands of passing tests prove the engine does what it claims: the ledger balances,
  replays are deterministic, every listed exploit is caught. They do not prove a customer
  saves money.
- Simulation results are synthetic. Say "on the frozen synthetic benchmark", every time.
- Real proof comes from 154 (backtest on a pilot partner's own history) and the pilot
  itself. The pilot report (197) is the first claim that can go on the website.
- Experiments 175-177 are pre-registered: write the hypothesis and pass bar into the repo
  before running, and publish the result either way.

## Before building, check

- Card network rules: only the publicly available parts. Mark anything inferred.
- Stripe and Adyen: build against their test modes and current docs, not memory.
- OpenEnv, verifiers-style libraries, MCP and agent SDKs move fast. Pin versions and
  re-read their specs when you reach phases 4 and 5.
- Draft terms (198) are for a lawyer to review, not to send as-is.
