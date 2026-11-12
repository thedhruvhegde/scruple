# Outreach for the first pilot

Who to contact, what to send, what to ask, and how to answer the usual objections
using only what exists. No named prospects here: profiles only. Never imply that any
company is a customer, partner or user (docs/MARKET.md section 6).

## Who to approach first

In this order, because of how fast each can say yes and how directly each feels the
problem (reasoning from docs/MARKET.md sections 1 and 6 and docs/PIVOT.md section 3):

| order | company type | role to contact | why first |
|---|---|---|---|
| 1 | vertical-AI companies that sell a dispute, chargeback or payments-ops agent | CTO, head of product, head of ML | Every release is a change they must prove safe to their own customers; buys from an engineering budget; MARKET.md calls this possibly the fastest-closing segment |
| 2 | fintechs, marketplaces and platforms that run their own dispute rules or agent on Stripe or Adyen | head of payments operations, dispute operations lead, risk engineering lead | They own the rules and the data, and the export paths match the parsers scruple already has |
| 3 | processors and acquirers with in-house representment rules | dispute operations leader, head of operations engineering | Larger volumes make the sample-size math work; longer cycles |
| later | banks building agents in-house | model risk / validation lead, with the operations leader as champion | 12 to 18 month cycles (PIVOT.md); the enterprise gate is the expansion, not the entry |

Signals that a team is a fit: they change dispute logic at least monthly; they can
name their monthly dispute volume and reason-code mix; someone has asked them "how do
you know the new version is better?" and the answer was a dashboard.

Signals to stop: no software in the dispute decision at all; fewer than a few hundred
resolved disputes a month (run `scripts/pilot_plan.py` with their numbers first); no
way to export history.

## Cold email (about 120 words)

Subject: a release check for your dispute rules

> Hi `[first name]`,
>
> When your team changes a dispute rule or an agent prompt, you learn weeks later, as
> chargebacks resolve, whether it cost money. Scruple replays each change on held-out
> disputes, scores it from ledger postings instead of the agent's own report, and
> blocks the pull request when any reason code gets worse.
>
> In our synthetic demo, a rule that looked $10.94 per case better on average was
> blocked: it contested 128 disputes after the evidence deadline.
>
> We are running a six-week paid pilot with one dispute team: a backtest on your
> history, a shadow run, a check in your CI, and success criteria you sign before we
> see any data.
>
> Worth 20 minutes to see it run?
>
> `[name]`

Rules for every variant: say "synthetic" next to any demo number; no customer names;
no percentages that were not measured; no claims about what regulators or networks
require.

## One-page leave-behind (outline)

1. **Headline:** verifiers and a release gate for software that decides card disputes.
2. **The problem in one paragraph:** dispute logic changes often; outcomes arrive weeks
   later; averages hide the reason code that got worse; agents report their own wins.
3. **What scruple does (exists today):** replays a change on held-out cases, scores it
   from ledger postings and policy constraints, checks every reason code separately,
   blocks the pull request, stages a canary with one-step rollback, needs a named
   approver who is not the owner, and writes a hash-chained audit trail and an evidence
   pack.
4. **One picture:** the scorecard table from `scruple ci render` for the blocked demo
   rule (synthetic), with the late-evidence row highlighted.
5. **Real-data note:** on 100 held-out CFPB complaints, Claude Sonnet named the right
   regulation 93% of the time but predicted relief with AUROC 0.61, below a TF-IDF
   classifier's 0.70 (docs/results/REAL_MODELS.md; small n, wide intervals).
6. **The pilot:** six weeks, the timeline table from README.md, what they give, what they
   get, criteria signed before data.
7. **Security in three lines:** runs in their environment; never reads card numbers or
   stores free text; no SOC 2 yet, and SECURITY.md says what is not built.
8. **Contact.**

## Discovery questions

From docs/MARKET.md section 6, reworded for a disputes pilot, then the questions the
planner and the data checklist need.

1. Which dispute decisions are made by software today: rules, a model, an LLM agent?
   Which are pilots?
2. Can any of them change their own behaviour (memory, rules, prompts) without a human
   release?
3. Who approves a change to dispute logic today, and how long does it take?
4. What evidence does risk, compliance or audit ask for before a change? Can you show a
   redacted example?
5. What ground truth exists (won, lost, fees, reversals), and how long after opening
   does a dispute reach a final outcome?
6. Has a change to dispute logic ever cost money before anyone noticed? How was it
   found?
7. Where would this run (your cloud account, on-premises), and what data could a pilot
   use?
8. Who owns the budget: operations, risk, or the platform or ML team?
9. What would make you stop a pilot?
10. What would a paid pilot need to prove?

For sizing and data:

11. How many disputes a month, and what share per reason code?
12. What is the largest amount one decision can swing (dispute amount plus fees)?
13. Stripe, Adyen, another processor, or your own case system? Can you export resolved
    disputes with their balance movements?
14. Which of these do you record per dispute: 3-D Secure, delivery confirmation,
    evidence submitted on time, prior refund, prior dispute on the same payment?
15. Do you publish an internal win rate? How is it computed?

## Objection handling

Each answer uses only what is in the repository. If a question needs something that
does not exist, say so and say whether it is planned.

| objection | answer |
|---|---|
| "We already use an eval tool." | Eval tools score outputs, often with an LLM judge. scruple scores what moved on the ledger and which policy constraints broke, per reason code, and ships with attacks against its own verifiers: in the demo, 7 of 7 attacks that fool a naive verifier are stopped (`scruple redteam`). It also sits in the release path: required status check, canary, rollback, approvals. |
| "Is any of this real?" | The simulator and the demo are synthetic, and every output says so. The only real data so far is public CFPB complaints (docs/data/CFPB.md). The pilot would be the first result on a real dispute operation, which is why the criteria are signed before data and the result is published to you either way. |
| "Our data cannot leave our environment." | It does not have to. scruple runs as a Docker image or Python package in your environment. No adapter reads card numbers; Stripe evidence is recorded by field name only (DATA_CHECKLIST.md). |
| "Our agent is an LLM, not rules." | Today the gate replays structured rules. LLM agents can play the simulator through the episode tools, the MCP server or the Anthropic and OpenAI runners and are scored by the same verifiers; an agent callable as a Python `decide(view)` function, an HTTP endpoint you host, or a model with your prompt can be backtested on your history (one request per case, replies cached; tested against fakes only so far). |
| "Six weeks is not enough to see outcomes." | Often true, and the planner shows it before we start (SUCCESS_CRITERIA.md). Four of the five template criteria (planted regressions caught, no new hard violations, self-report gap, reproducibility) do not need new outcomes. |
| "Send your SOC 2." | There is none; SOC 2 has not been started and there has been no penetration test. SECURITY.md lists the controls that exist in code with their tests, and what is not built, including authentication. The pilot design avoids needing them: it runs in your environment, read-only. |
| "Why should we pay for a pilot?" | A fee keeps both sides committed to a fixed scope and criteria (MARKET.md). The proposals credit it against a subscription or tie it to criteria; the founder sets the price. |
| "We could build this ourselves." | You could. What takes time is verifiers that hold up against attacks, deterministic replay that gives identical bytes on rerun, an append-only audit chain with tamper tests, and per-reason-code statistics that stay valid under continuous monitoring (docs/math). The pilot costs less than finding out. |
| "Do you know the network rules?" | scruple models only publicly available parts, cites sources and marks anything inferred as an assumption (docs/SOURCES.md, docs/ASSUMPTIONS.md). It gives no network or legal advice, and network reason codes are kept verbatim, never remapped. |
| "Who approves changes?" | A named approver who is not the rule's owner or the agent; N of M approvers with a required role if you want (`scruple/gate/approval.py`). The MCP server cannot approve. |
