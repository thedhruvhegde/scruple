# scruple pilot: card disputes, six weeks

Status: draft offer, November 2026. Nothing here has been run with a partner yet.
Every number in the engine today comes from the synthetic simulator or from public
CFPB complaints (docs/data/CFPB.md). A pilot is how scruple gets its first number
from a real dispute operation.

## Who it is for

A team that already decides card disputes with software and wants to change that
software safely:

- a fintech or marketplace that runs its own dispute rules or an agent on Stripe or Adyen;
- a processor or acquirer whose operations team maintains representment rules;
- a vertical-AI company that sells a dispute or chargeback agent and has to show its
  customers that each release is controlled.

You need: at least a few thousand resolved disputes in your history (the planner below
says how many), an export path (Stripe API, Adyen notifications, or CSV), and one
change you want to evaluate (a new rule, a prompt edit, a model swap).

Not a fit yet: AML, ACH returns, reconciliation or payables as the main workflow. The
engine has parsers for some of those files, but no verifiers or gate for them.

## What you get

1. **A backtest of one candidate change on your own history.** Which decisions change,
   in which reason codes, and which hard policy constraints the change would break
   (contesting after the evidence deadline, auto-deciding a high-value case, accepting a
   duplicate). Reported per reason code, never only as an average.
2. **A shadow run on live traffic.** Your production system keeps deciding; scruple scores
   what the candidate would have done and acts on nothing.
3. **A gate in your CI.** A GitHub Action (or GitLab template) that blocks a pull request
   when the candidate regresses, posts a scorecard comment and a `scruple/gate` status.
   It replays candidates on synthetic held-out cases and, with `--history`, on your own
   history, and shows the two results separately (SCOPE.md week 5). The history side
   judges the candidate rule alone, not your full current rule set.
4. **A self-report check.** What your agent or dashboard says it wins, against what the
   balance transactions say happened.
5. **An evidence pack per decision** (JSON plus PDF, audit chain proof included) that a
   risk or compliance reviewer can read without running anything.
6. **A written readout** (READOUT_TEMPLATE.md) with results against criteria you agreed
   to before seeing any data. We report the result whether it passes or fails.

What exists today and what we build during the pilot is listed in SCOPE.md. In short:
the gate, CI integration, evidence packs, audit chain, Stripe/Adyen/CSV ingest
(`scruple ingest`, with redaction, card tokenisation and field encryption applied before
storage), the backtest on imported cases, shadow mode on imported cases and the CI gate
on your history exist and are tested, on Stripe's published fixtures and on synthetic
histories; none has yet run on a partner's data. Still to build or agree in the pilot:
your own policy constraints as checks, your `ImportConfig` if you export CSV, and
anything SCOPE.md lists as not built.

## What you give

- A named owner on your side (about 3 to 5 hours a week, see SCOPE.md) and an
  engineer for the export and CI steps (about 4 to 8 hours in weeks 1 and 5).
- Read-only export of resolved disputes and their balance movements for the window
  the planner asks for (DATA_CHECKLIST.md).
- One candidate change, and the rule or agent version it would replace.
- A reviewer from risk, compliance or the dispute team for the readout.
- Agreement on pre-registered success criteria before data arrives (SUCCESS_CRITERIA.md).

## Timeline (six weeks)

| week | what happens | output |
|---|---|---|
| 1 | kickoff, success criteria signed, data export started | signed criteria, export plan |
| 2 | data export validated; ingest and redaction run in your environment | ingest report: rows read, rows rejected and why |
| 3 | backtest of the candidate on your history | backtest report per reason code |
| 4 | shadow mode on live traffic | shadow summary |
| 5 | gate installed in your CI on one repository | scorecards on real pull requests |
| 6 | readout with your reviewer | readout, evidence packs, deletion certificate |

Detail, roles and exit criteria: SCOPE.md.

## Success criteria

Agreed in week 1, before any of your data is loaded, using SUCCESS_CRITERIA.md. The
template has four kinds of hypothesis: the gate catches regressions planted in your own
history; the candidate adds no hard policy violations; the self-report gap is measured
from the ledger; the scorecard is reproducible byte for byte in your CI. A cost
improvement claim is optional and sized with `scripts/pilot_plan.py`, which often shows
that six weeks of traffic is not enough to detect a small improvement. We will say so
up front rather than after.

## Price

**Pricing is to be set by the founder.** The options below are proposals for
discussion, not quotes. They are derived from the hypotheses in docs/MARKET.md section 5
and docs/PIVOT.md; none has been tested with a buyer.

| option | structure | rationale |
|---|---|---|
| A. fixed-fee pilot, credited | one fixed fee for the six weeks: `[FEE A, to be set]`; credited in full against a first-year subscription signed within `[N]` days of the readout | MARKET.md: paid pilots filter out tourists; its hypothesis is $50k to $100k for a 90-day, one-workflow pilot, so a six-week pilot would sit at or below that range. Crediting the fee removes the "pay twice" objection. |
| B. smaller fee, pre-agreed conversion | a smaller fixed fee `[FEE B]` plus a pre-agreed subscription price `[SUBSCRIPTION]` that applies only if the pre-registered criteria pass | Fits vertical-AI vendors, which PIVOT.md calls the possibly fastest-closing segment and which buy from an engineering budget. The conversion price is anchored on MARKET.md's $150k to $300k per year platform hypothesis. |
| C. design-partner rate | reduced fee `[FEE C]` in exchange for permission to publish the readout (anonymised, partner approves the final text) | ROADMAP.md: the pilot report is the first claim that can go on the website. PIVOT.md asks for fidelity metrics calibrated against a partner's history. This trades price for evidence. |

Proposals common to all three:

- No per-seat and no per-evaluation pricing (MARKET.md: procurement dislikes unpredictable
  bills). Evaluation volume during the pilot is unlimited.
- Any LLM inference needed to replay your agent runs on your account at your cost
  (MARKET.md, gross margin note); scruple's own simulator and gate are cheap to run.
- The fee does not depend on the result. Results are reported either way.

## Documents

| file | what it is |
|---|---|
| SCOPE.md | week-by-week plan, roles, hours, exit criteria, what is out of scope |
| SUCCESS_CRITERIA.md | pre-registration template with a worked sample-size example |
| DATA_CHECKLIST.md | exact fields to export, volume, PII handling, deletion |
| SECURITY.md | security questionnaire answers from controls that exist in the code |
| DEMO_SCRIPT.md | 20-minute live demo with commands that run today |
| READOUT_TEMPLATE.md | the report we hand over in week 6 |
| TERMS_DRAFT.md | draft pilot terms and DPA outline, for counsel review |
| OUTREACH.md | who to approach first, email, leave-behind, discovery, objections |
