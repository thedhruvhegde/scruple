# agent-gate — Market, Positioning and Scope Notes

Status: pre-customer working notes, November 2026. Every number below is either
**sourced** (link at the bottom) or explicitly marked **ASSUMPTION**. Market-research
firm figures disagree with each other by 10x; treat all of them as directional. The
point of this document is to decide what to validate in the first 20 customer
conversations, not to put a number in a pitch deck.

---

## 1. Sharpening the idea

**One line:** agent-gate is change control for agent behavior. Nothing an agent learns
(or is changed to do) reaches production until it has been replayed against history,
scored by verifiers that are hard to game, and approved, with the evidence written up
for a model-risk reviewer.

**The wedge:** learned memory/rules in **dispute and chargeback operations**. Why this
workflow first:

- Ground truth exists and arrives within weeks: a dispute is won or lost. The ledger
  balances or it does not. Few agent workflows have outcomes this verifiable, and
  verifier quality is the whole product.
- The money is visible. Mastercard/Datos put global chargebacks at roughly 261M
  disputes and about $34B in 2025, growing to 359M by 2029 (sourced). The cost of a
  wrong decision can be counted in dollars per case.
- Edge cases are well known and rare: partial refunds, duplicates, late evidence and
  FX mismatches. That makes it the right place to show why averages lie.

**The broadening (do not build yet, but design so it is possible):** the same gate
applies to *any* change in agent behavior, such as a prompt edit, a model version bump,
a new tool, or a policy-document update. This matters because of risk #1 below. If
self-learning agents are slow to arrive in banks, "change control for agent
behavior" is still a product. "Change control for agent learning" might not be.

**What we are not:** an agent framework, a memory store, a runtime guardrail or
firewall, an observability dashboard, or a model-inventory/MRM workflow tool. We
integrate with all of those.

### Biggest risks to the thesis (validate first)

1. **Timing.** Are regulated institutions letting agents write their own memory in
   production *today*? Our working belief (ASSUMPTION) is that most bank agent
   deployments are still static: fixed prompts and fixed tools. Learning happens in
   pilots, at fintechs, and at vendors (case-management and chargeback-automation
   companies). If that is true, early buyers are more likely to be **vendors who sell
   learning agents into banks** and need to prove control to their customers, not the
   banks themselves.
2. **"Feature, not product."** Agent platforms, memory providers and eval vendors can
   each add "review before memory write". Our defense is domain verifiers, adversarial
   verifier testing, and evidence that a model-risk reviewer accepts. A generic eval
   vendor will not invest in ledger simulators for disputes.
3. **Regulatory pull is soft.** SR 26-2 / OCC 2026-13 (April 2026) replaced SR 11-7.
   Commentators report that it explicitly scopes generative and agentic AI *out*. The
   agencies say they plan an RFI on AI in model risk. So there is no rule that says
   "you must buy this". The buyer is motivated by internal risk appetite, audit
   findings and wanting to say yes to ops. That is a real but slower motion than
   compliance-driven buying. (We make no claim about what the RFI will contain.)
   In the EU, high-risk Annex III obligations (including credit scoring) were deferred
   to 2 Dec 2027 by the Digital Omnibus. Dispute handling is not obviously in Annex
   III scope (ASSUMPTION; needs legal review).

---

## 2. Market sizing

### Top-down context (sourced, directional)

| Figure | Value | Source |
|---|---|---|
| AI governance software spend, 2030 | ~$15.8B, ~30% CAGR 2024–2030 | Forrester |
| Other "AI governance market" estimates, 2030 | ~$1.4B–$5.5B, 7–45% CAGR | various research firms (definitions differ widely) |
| "AI model risk management" market | ~$5.7B (2024) to ~$10.5B (2029), ~13% CAGR | Bizwit; MarketsandMarkets |
| Global chargebacks | 261M (2025) to 359M (2029) | Mastercard / Datos Insights |
| Chargeback value | ~$33.8B (2025); $36.9B (2026) to $46.1B (2029) | Mastercard / Datos Insights |
| Merchant cost per chargeback | ~$128 (internal $82 + third-party $46) | Mastercard citing Javelin |
| FDIC-insured institutions (Q2 2026) | 4,238 | FDIC QBP via ABA Banking Journal |

### Bottom-up (the number we actually believe in, all ASSUMPTIONS)

Target accounts are institutions with enough operations volume to run agents in
disputes, payments exceptions, reconciliation or claims:

| Segment | Accounts (ASSUMPTION) |
|---|---|
| US banks large enough for in-house ops agents (roughly the largest ~150; verify with FDIC asset tiers) | 150 |
| Non-US banks of similar scale | 600 |
| Issuer processors, acquirers, PSPs | 300 |
| Scaled fintechs / neobanks / BNPL | 400 |
| Insurers (claims ops is the analog workflow) | 500 |
| **Total** | **~2,000** |

ACV assumption: $100k–$500k per year for a platform plus one or two workflow packs,
midpoint **$250k**. Comparable vendors (ValidMind, ModelOp, Credo AI, Holistic AI) do
not publish prices. All are sales-led with custom quotes, so this needs pricing
discovery.

- **TAM (governing ops agents in regulated financial services): ~2,000 × $250k ≈ $500M/yr.**
  Cross-check: if financial services is 20–25% of Forrester's $15.8B (ASSUMPTION), and
  "change control and verification" is 10–20% of governance spend (ASSUMPTION), that
  gives $300M–$800M. This is the same order of magnitude, which is the most we can
  say.
- **Expansion TAM (all agent behavior changes, several workflows per account):**
  ~$1B–$2B. Speculative.
- **SAM (US + UK/EU, disputes and payments-ops wedge, agents in production by
  ~2028):** ~500 accounts, of which perhaps 40% actually run ops agents (ASSUMPTION).
  A secondary report says 51% of banks are piloting agents, but that is unverified.
  **~200 × $200k ≈ $40M**, rising toward $100M if adoption broadens.
- **SOM (36 months):** 10–25 paying customers, so **~$2M–$6M ARR**. That is a credible
  seed-to-Series-A arc. It is not a "$1B in year 3" story, and we should not pitch it
  as one.

**Growth:** the category tailwind is roughly 30% CAGR (Forrester, AI governance
software). The workflow tailwind is chargeback volume up about 37% from 2025 to 2029
(Mastercard/Datos). The real driver is how quickly agents get permission to *learn*
in production. Nobody measures that yet, so we should ask every prospect.

---

## 3. Competitive landscape

| Category | Examples | What they do well | Why they don't solve this (today) | Threat |
|---|---|---|---|---|
| MRM / AI governance platforms | ValidMind (financial-services focused), ModelOp, Credo AI, Holistic AI, Lumenova, IBM watsonx.governance, SAS | Inventory, documentation, validation workflows, policy mapping; already sold to model-risk buyers | Govern *models* at a validation cadence of months, not a rule an agent wrote this morning; no outcome-based replay of each memory change | **High as partner / acquirer**, medium as competitor |
| LLM eval & observability | Braintrust, Arize, Galileo, LangSmith, Promptfoo, Patronus, W&B Weave | Eval harnesses, tracing, LLM-as-judge, CI for prompts | Generic scorers; no ledger or dispute ground truth; not written for a compliance reader; usually no staged promotion or rollback of memory | **High**: closest in mechanics |
| Agent "improvement layer" | Judgment Labs (reported $32M raise) | Learning loops for agents | Optimizes the agent; we gate it. Could add gating | **High**: watch closely |
| Agent security / runtime governance | Guild.ai, Geordie, JetStream, Zenity and many others (lots of 2026 funding) | Access control, runtime monitoring, policy enforcement | Control what an agent may *do* right now, not whether a learned change is *better and safe* | Medium |
| Memory providers | Letta, Mem0, Zep | The memory store itself | Could add "pending review" states; unlikely to build domain verifiers | Medium; **best integration partners** |
| Agent platforms / hyperscalers / labs | Bedrock, Azure, Vertex, frontier-lab agent SDKs | Distribution; native memory and evals | Horizontal; banks want an independent control, not the vendor grading itself | Long-term high |
| Vertical dispute automation | Chargeflow, Justt (and incumbents in chargeback management) | Own the workflow and data | Their own agents are what needs governing; they are **potential customers/channel** | Low as competitor |
| In-house build at large banks | Internal ML platform teams | Context, data access | Expensive to build, and independence matters to auditors | Medium (top-10 banks) |

**Positioning sentence:** *"Eval tools tell engineers whether a prompt got better.
agent-gate tells a model-risk officer whether a learned change is safe to let into a
regulated workflow, and proves it with replay against ground truth that the agent
cannot game."*

---

## 4. Differentiation (what to double down on)

1. **Outcome-anchored, domain verifiers.** Ledger invariants, dispute ground truth and
   policy constraints, not LLM-as-judge. This is the slowest part for a horizontal
   vendor to copy.
2. **Adversarial verifier testing as a shipped feature.** Every verifier comes with an
   attack suite, and every evidence report says which attacks it was tested against
   and where it is still weak. No competitor we found markets "we red-team our own
   scoring".
3. **Evidence a non-engineer reviewer can sign.** The report is the product for the
   buyer. Optimize it with real model-risk reviewers in design-partner sessions.
4. **Deterministic, reproducible replay.** An auditor can re-run a decision from its
   seed and get the same bytes. Hash-chained audit trail.
5. **Independence.** Vendor-neutral, deployable in the customer's VPC, sitting in the
   memory-write path of any agent framework.
6. **Segregation of duties built in.** The agent's owner cannot approve its own
   promotion. Small, but it reads well to auditors.

---

## 5. Pricing and margins (hypotheses to test)

- **Paid design-partner pilot:** $50k–$100k for 90 days, one workflow, with success
  criteria agreed up front. Paid pilots filter out tourists. Credit the fee toward the
  first-year contract.
- **Platform subscription:** $150k–$300k/yr including one workflow pack (for example,
  disputes), the audit trail and evidence reports.
- **Expansion:** +$50k–$100k/yr per additional workflow or verifier pack
  (reconciliation, payments exceptions, claims).
- **Avoid early:** per-seat pricing (few seats) and per-evaluation usage pricing.
  Bank procurement dislikes unpredictable bills. Offer generous evaluation volume
  bands instead.
- **Gross margin (ASSUMPTION):** mature software at 75–85%. Expect 50–65% in years
  1–2 because of forward-deployed integration work. The hidden cost is replay that
  requires calling the customer's LLM agent on thousands of historical cases. Run it
  in the customer's cloud on their inference budget, or pass it through at cost.
  Keep our own simulators deterministic and cheap.

---

## 6. Customers and need

**Who has the pain (profiles, not named prospects):**

- **Head of model risk / MRM validation lead** at a large bank. Needs a way to say yes
  to learning agents with documented controls, now that SR 26-2 is silent on agentic
  AI and examiners will still ask how it is governed (ASSUMPTION about examiner
  behavior).
- **Payments / dispute operations leader** (bank, issuer processor or acquirer). Wants
  agents to improve from experience; blocked by risk review. Likely our champion.
- **CTO / head of product at a vertical-AI vendor** selling dispute, reconciliation or
  claims agents into banks. Needs to prove control to win bank procurement. This is
  possibly the fastest-closing segment, and a channel.
- **Chief AI / data officer.** Owns the agent platform strategy and wants one control
  plane.

**Public signals of agent deployment** (from secondary reporting; verify before
quoting): JPMorgan has publicly discussed longer-running autonomous agents and
back-office automation, and BNY is reported to be using agents in payment-instruction
validation. These show the deployment trend. **They are not prospects or customers,
and we must never imply they are.**

---

## 7. How not to over-engineer (the next 90 days)

Build only what a design partner needs to see on screen in a one-hour meeting:

- **One domain (disputes), one storage engine (SQLite), one process.** No Kafka, no
  microservices, no Kubernetes, no multi-tenancy, no SSO, no UI beyond markdown reports
  and a JSON API.
- **Structured rules before free-text memory.** Executable predicates make replay
  deterministic. Supporting natural-language memory (wrapping a real agent's memory
  writes) is the *second* integration, driven by a design partner's actual stack.
- **Synthetic data only** until a pilot contract with a data agreement exists.
- **Simple statistics** (paired bootstrap, per-slice checks). No causal-inference
  framework.
- **No policy DSL, plugin system or workflow engine.** Hardcode the stage machine.
- **Do build properly from day one:** determinism, hash-chained audit, the adversarial
  test suite and the evidence report. These are the product and are expensive to
  retrofit.
- **Kill or pivot criteria:** if 15 discovery calls produce fewer than 3 teams that
  have (or plan within 12 months) agents writing their own memory in production,
  re-position to "change control for any agent behavior change" before writing more
  code.

### Discovery questions for the first 20 calls

1. Which agent workflows are in production? Which are pilots?
2. Can any of them change their own behavior (memory, rules, prompts) without a human
   release?
3. Who approves an agent behavior change today, and how long does it take?
4. What evidence does model risk or internal audit ask for? Can you show a redacted
   example?
5. What ground truth exists for this workflow, and how quickly does it arrive?
6. Has a change to agent behavior ever caused an incident? How was it found?
7. Where would this run (VPC, on-prem), and what data could a pilot use?
8. Who owns the budget: ops, model risk, or the AI platform team?
9. What would make you kill a pilot?
10. What would a paid 90-day pilot need to prove?

---

## Sources

- SR 26-2 / OCC 2026-13 commentary: [Management Solutions summary (PDF)](https://www.managementsolutions.com/sites/default/files/publicaciones/eng/SR26-2-revised-guide-on-mrm.pdf), [CRA Insights (PDF)](https://media.crai.com/wp-content/uploads/2026/07/09165122/FE-Insights-MRM-Guidance-SR-26-2-In-the-Era-of-AI-July2026.pdf), [American Banker opinion](https://www.americanbanker.com/opinion/regulators-guidance-on-model-risk-leaves-many-questions-unanswered), [Lumenova](https://www.lumenova.ai/blog/sr-26-2-control-framework/). Read the primary Federal Reserve / OCC texts before quoting.
- AI governance market: [Forrester blog](https://www.forrester.com/blogs/ai-governance-software-spend-will-see-30-cagr-from-2024-to-2030), [MarketsandMarkets](https://www.marketsandmarkets.com/Market-Reports/ai-governance-market-15848598.html), [Virtue Market Research](https://virtuemarketresearch.com/report/enterprise-ai-governance-risk-and-compliance-software-market)
- AI model risk management market: [Bizwit via MarketResearch.com](https://www.marketresearch.com/Bizwit-Research-Consulting-v4223/Global-AI-Model-Risk-Management-41387339), [MarketsandMarkets](https://www.marketsandmarkets.com/PressReleases/ai-model-risk-management.asp)
- Chargebacks: [Mastercard: true cost of a chargeback](https://b2b.mastercard.com/news-and-insights/blog/what-s-the-true-cost-of-a-chargeback-in-2025), [2025 State of Chargebacks report (PDF)](https://tearsheet.co/wp-content/uploads/2025/03/2025-state-of-chargebacks-report-1.pdf)
- Bank counts: [ABA Banking Journal, Q2 2026 QBP](https://bankingjournal.aba.com/2026/08/quarterly-banking-profile-banking-net-income-90-1b-in-q2-2026/), [FDIC Q1 2026 release](https://www.fdic.gov/news/press-releases/2026/fdic-insured-institutions-reported-return-assets-126-percent-and-net)
- EU AI Act deferral: [Gibson Dunn](https://www.gibsondunn.com/eu-ai-act-omnibus-agreement-postponed-high-risk-deadlines-and-other-key-changes/), [Holistic AI](https://www.holisticai.com/blog/eu-ai-acts-deadline-moved-2027)
- Competitor pricing posture: [ToolRadar (ValidMind)](https://toolradar.com/tools/validmind/pricing), [AI governance buyer's guide](https://www.aimagicx.com/blog/ai-governance-platforms-enterprise-buyers-guide-2026)
- Funding signals: [Forkast: enterprise agent funding](https://forkast.news/enterprise-ai-agent-funding-surges-to-435m-in-five-months-security-and-governance-lead/), [Judgment Labs](https://www.pulse.bot/venture-capital/news/judgment-labs-raises-32m-to-build-the-improvement-layer-for-ai-agents-56ba7625-8f17-441d-b34f-d1fbaf260e19/), [Letta seed](https://prnewswire.com/news-releases/berkeley-ai-research-lab-spinout-letta-raises-10m-seed-financing-led-by-felicis-to-build-ai-with-memory-302257004.html), [Mem0](https://www.pulse.bot/ai/deals/mem0-raises-24m-across-seed-and-series-a-to-build-ai-memory-infrastructure-9c616cce-0f11-407f-a690-249a27f7611a/), [Chargeflow](https://thepaypers.com/fraud-and-fincrime/news/chargeflow-raises-usd-35-million-in-series-a-funding), [Justt](https://prnewswire.com/news-releases/justt-secures-30m-series-c-funding-to-accelerate-global-expansion-advance-ai-technology-and-achieve-profitability-302336294.html)
- Bank agent deployment (secondary, verify): [JPMorgan agents](https://www.roic.ai/news/jpmorgan-to-deploy-more-powerful-ai-agents-this-year-06-09-2026), [banks piloting agents](https://ai2.work/blog/51-of-banks-are-piloting-ai-agents-wall-street-s-agentic-rollout)
