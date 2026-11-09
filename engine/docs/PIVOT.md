# Pivot memo: from "governance tool" to "verified simulation for money-movement agents"

Status: recommendation, November 2026. Numbers are **sourced** (links at the bottom)
or marked **ASSUMPTION**. Read this alongside docs/MARKET.md, which sized the
original governance product.

## 1. Why the first sizing came out small

The original SOM (~$2–6M ARR by year 3) was not a ceiling on the idea. It is what
you get when you sell a **compliance/governance tool** into a **risk budget**:

- Small contracts (~$150–300k), because governance software is priced against
  headcount in model risk, not against the value of the system it governs.
- Slow cycles (12–18 months in banks) and a soft regulatory pull (SR 26-2 scopes
  agentic AI out).
- The buyer (model risk) is a cost center whose job is to say no.

## 2. How Applied Intuition gets so much ARR

Reported estimates (not company filings): ~$415M ARR in 2024, ~$800M+ in 2025;
$15B valuation in June 2025, with reported offers at $30B+ in October 2026; 18 of
the top 20 automakers as customers. The mechanics:

1. **It sells into the R&D budget, not the compliance budget.** Automakers spend
   billions developing autonomy, and validation is a large line item inside that.
2. **Simulation replaces something expensive.** Simulated miles are cheaper than
   test fleets and safety drivers. The ROI is hard and obvious.
3. **It sits in the release path.** Safety-critical software cannot ship without
   validation, so the product is mandatory every release, not optional per audit.
4. **Few customers, huge contracts.** ~$415M across fewer than ~30 large OEMs works
   out to **eight-figure average contracts** (inference from the reported figures).
5. **The same core asset gets sold several times.** Simulation → validation →
   data tooling → vehicle OS → defense.

**The lesson for us:** the asset we already built (a deterministic simulator of a
financial workflow plus verifiers that are hard to game) is the same *kind* of asset.
We were selling it to the wrong budget.

## 3. The pivot (small in code, large in business)

> **agent-gate becomes simulation and verification infrastructure for agents that
> move money.** It covers payments operations, disputes, reconciliation, AML alert
> triage and the accounting close. The same environments and verifiers are sold
> three ways:

| Buyer | What they buy | Budget | Why now |
|---|---|---|---|
| **Frontier labs and neolabs** (post-training / RL teams) | Procedurally generated RL environments for money-movement workflows, with **hack-resistant reward functions** and a red-team report per environment | Model R&D / data spend. One lab reportedly discussed spending **>$1B on RL environments in a year**; contracts are often **six to seven figures per quarter** | Labs are moving into agentic finance. In Epoch's interviews, robustness to reward hacking was the quality criterion buyers cited most consistently |
| **Vertical AI companies building finance-ops agents** | Training and regression environments, plus a pre-release gate (the current product) for every model, prompt or memory change | Their engineering budget. Proving reliability is how they win bank deals | Every release of their agent is a change that has to be verified |
| **Banks, processors, fintechs building agents in-house** | The enterprise gate: shadow replay, canary, audit, evidence reports (what we built) | Ops transformation and AI budgets, with model risk as the approver | Same as the original thesis; becomes the expansion motion, not the entry |

**Why this is only a slight pivot:** the verifier stays the product. A verifier
that cannot be gamed is simultaneously a **reward function** (training), an
**eval** (model selection), and a **release gate** (deployment). We already have the
simulator, three outcome-anchored verifiers and an adversarial suite that attacks
them. What changes is the interface and the first customer.

**Why this niche is defensible, not general:**

- "Money-movement ops" has **ground truth that is hard to fake**: ledger state,
  network dispute outcomes, reconciliation breaks. Most finance environment vendors
  work on *knowledge* tasks (Excel models, decks, 10-K Q&A), where grading is fuzzy.
- Faithful simulators of card networks, ledgers, ACH/wire flows and dispute
  lifecycles are tedious to build and require domain expertise. That is the moat,
  as test tracks and sensor models were for Applied Intuition.
- **Procedural generators, not static task sets.** Labs pay roughly $200–$2,000 per
  hand-made task. A seeded generator with verifiable rewards produces unlimited fresh,
  held-out tasks with difficulty control. Static task sets get consumed; generators
  are sold as subscriptions.

## 4. Competitive landscape for the pivoted product

| Player | Focus | Overlap |
|---|---|---|
| Halluminate | Computer-use RL gyms for investment banking/PE (Excel, PowerPoint); says it works with frontier labs | Finance, but **knowledge work**, not ledgers or money movement |
| Snorkel (FinQA) | Open RL environment: Q&A over 10-K filings | Reasoning over documents; static |
| Centific RL-ADA | Banking customer-support dialogue environment | Closest on disputes, but conversational, not ledger-verified |
| NPCI + NVIDIA | Open synthetic banking-agent environment (India) | Open and regional; validates demand; possible partner |
| EdotEnv (YC S26) | RL environments from market data, for quant research | Trading, not operations |
| Vals AI ($40M Series A, a16z) | Independent benchmarks incl. Finance Agent v2 | Evals for model selection; not training environments or release gates |
| Mercor, Surge, Scale, Turing | Human data at very large scale (Mercor and Surge each reported at ~$2B *gross* run rate, with 60–70% paid out to contractors) | They could add finance environments; their model is labor, not simulators |
| Mechanize, Fleet, HUD, Plato, Bespoke, Deeptune | General environment builders | Horizontal; could enter any niche, but none is ledger-native today |
| Labs in-house | Build the environments they care about most | The biggest risk: we must be cheaper and better than building |

**The open lane:** stateful, ledger-verified, procedurally generated environments
for operational money movement, with published red-team evidence that the reward
cannot be gamed. I found no vendor positioned there (absence of evidence, not proof).

## 5. Sizing the pivoted business

All ASSUMPTIONS unless sourced; ranges are wide on purpose.

**TAM (~2028): about $1.5B–$3B per year**

| Pool | Logic | Range |
|---|---|---|
| Lab RL environment and eval spend on finance/money ops | Total lab environment+eval spend of $3–6B/yr across ~5 frontier labs and ~15 neolabs (anchored on one lab discussing >$1B/yr), × 5–10% share for finance operations | $150M–$600M |
| Agent-builder testing and validation spend in financial services | Global bank AI spending ~$53B in 2026 (Statista, unverified), × 1–3% on testing/simulation of agents (software QA typically takes a larger share of development spend) | $0.5B–$1.5B |
| Enterprise release gate (original product) | From docs/MARKET.md | ~$0.5B |

Expansion beyond that: insurance claims ops, treasury, tax and accounting close,
and any regulated workflow with a verifiable ledger.

**SAM: about $300M–$600M.** Labs plus the top ~50 vertical finance-ops agent
companies plus the top ~100 institutions building in-house, limited to payments,
disputes, reconciliation and AML triage.

**SOM (year 3): about $10M–$40M ARR, base case ~$15M**

- Labs: 3–5 labs × $2–5M/yr = $6–25M (lumpy, concentrated).
- Vendors and enterprises: 15–30 × $300k–$1M = $5–20M.

That is 3–7x the governance-only plan. More importantly, the first dollars arrive
in months (lab pilots), not after 12–18-month bank cycles.

**Honest caveats:** lab revenue is concentrated, renegotiated often, and at risk of
in-housing. The largest data companies' numbers are *gross* marketplace revenue
with low margins; a simulator business should have software-like margins, but only
if environments are sold as generators plus subscriptions, not one-off task batches.

## 6. Risks and how to de-risk them fast

| Risk | Test in the next 30–60 days |
|---|---|
| Labs don't want money-movement environments | Five conversations with post-training / data leads. Signal: a paid pilot of $100k or more, or a request for an exclusive environment |
| Labs build it themselves | Sell what is expensive for them to build: domain-faithful simulators, procedural difficulty and red-team evidence |
| Lab revenue is lumpy and concentrated | Land two or three labs plus vertical vendors in parallel; keep the enterprise gate as the durable layer |
| Becoming a services company | Ship generators and verifiers as versioned software; charge for subscription and refresh, not per task |
| Simulator fidelity gets questioned | Calibrate against a design partner's historical disputes; publish fidelity metrics |

**Kill or adjust:** if no lab or vertical vendor commits to a paid pilot within 60
days, fall back to the vendor-first enterprise gate (sell to companies that sell
agents into banks).

## 7. What changes in the code (deliberately small)

1. **Episodic, tool-use interface.** The agent acts step by step (fetch case, request
   evidence, post a ledger entry, submit representment, escalate) instead of
   emitting a rule. Reward = composite of the existing verifiers. Export to standard
   harness formats (Gym-style `reset`/`step`; check which formats labs actually use).
2. **Reward-hacking red team per environment.** Turn `adversarial.py` and
   `redteam.py` into a report that ships with every environment version.
3. **Difficulty curriculum and held-out splits.** Edge-case rates and drift profiles
   already exist in `Profile`; expose them as difficulty knobs.
4. **A second workflow:** reconciliation (bank statement vs. ledger breaks). It reuses
   the ledger and has crisp ground truth.
5. **Generalize the gate** to any agent release (model, prompt, tool or memory
   change), not only learned rules.

**Not now:** a UI, multi-tenancy, a Postgres migration, more than two workflows.

## Sources

- Applied Intuition: [Forbes profile](https://www.forbes.com/companies/applied-intuition/), [revenue and ARR analysis (secondary)](https://blog.pebblous.ai/project/BizReport/applied-intuition-analysis-01/en/), [$30B offers report](https://cryptobriefing.com/applied-intuition-30-billion-valuation-offers/), [GuruFocus](https://www.gurufocus.com/news/9121063/applied-intuition-eyes-30-billion-valuation-amid-new-funding-round)
- RL environments: [TechCrunch: Silicon Valley bets big on environments](https://techcrunch.com/2025/09/21/silicon-valley-bets-big-on-environments-to-train-ai-agents/), [Epoch AI: FAQ on RL environments](https://epoch.ai/gradient-updates/state-of-rl-envs), [SemiAnalysis: RL environments](https://newsletter.semianalysis.com/p/rl-environments-and-rl-for-science), [RL environment vendor directory](https://www.rl-list.com/), [Wing VC: who will win the training and verification layer](https://www.wing.vc/content/rl-environments-for-agentic-ai-who-will-win-the-training-verification-layer-by-2030) (not read; host blocked in this environment)
- Finance environments: [Halluminate](https://standout.work/companies/halluminate), [Snorkel FinQA](https://snorkel.ai/blog/building-finqa-an-open-rl-environment-for-financial-reasoning-agents/), [NPCI + NVIDIA](https://www.businesstoday.in/latest/corporate/story/npci-nvidia-unveil-open-ai-training-environment-for-banking-agents-using-synthetic-data-554347-2026-09-09), [Centific RL-ADA](https://www.centific.com/rl-environments/banking-adversarial-dialogue-agent-(rl-ada)), [EdotEnv](https://runtimewire.com/article/edotenv-market-derived-rl-environments-financial-agents)
- Evals: [Vals AI Series A](https://www.kucoin.com/blog/vals-ai-40m-series-a-real-world-benchmarks), [Sacra on Vals AI](https://sacra.com/c/vals-ai)
- Data companies: [Mercor $2B gross run rate](https://dealroom.co/news/137121-mercor-doubles-to-2b-gross-revenue-run-rate-as-ai-labs-buy-expert-data/), [contractor payout share](https://runtimewire.com/article/as-amazon-lets-mechanical-turk-fade-mercor-hits-a-2-billion-gross-run-rate), [Surge AI](https://enterprisedna.co/resources/ai-map/company/surge-ai)
- Bank AI spend and validation capacity: [Statista: banking AI spending forecast](https://www.statista.com/statistics/1557311/global-banking-sector-ai-spending-forecast), [RMA model risk survey](https://www.rmahq.org/journal-articles/2024/october-november-2024/in-rma-s-model-risk-management-survey-a-picture-of-banks-diligence-and-frustrations/?gmssopc=1)
