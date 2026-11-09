# Simulator fidelity against real CFPB complaint data (roadmap 040)

How far the synthetic dispute mix is from the few real, public marginals that can be
compared with it, and what calibrating the generator to them does. **The simulated side
is synthetic.** The real side is public complaint data; it is not card-network dispute
data, and most of what the simulator models cannot be checked against it at all.

## Data

| set | what | mirror (commit) | file sha256 | licence |
|---|---|---|---|---|
| 1 | CFPB Consumer Complaint Database, Kaggle snapshot (file dated 2016-04-26), complaints received 2012-01-01 to 2015-12-31; the payments subset (135,313 rows) of the mirror's `data/consumer_complaints.csv.zip` (sha256 `38b11cfd7ebd5ec9...`) | https://github.com/jiegzhan/multi-class-text-classification-cnn (`e75a4b1fafb011873055c2d39ce7a48f835f0c86`) | `0ae2fefa23550ef941f9fcbf198d2e1e82ef51dd7bedca4478d4d4c7f40c3393` | US federal government work, public domain (17 USC 105) |
| 2 | CFPB checking or savings account complaints received 2025-05-14 to 2026-04-24, a random sample of 8,000 | https://github.com/mhoehler22/cfpb-agglomerative-clustering (`5ddb49aeb18218b10d11df15cfe60a5dbe8ece09`), `data/outputs/sample_metadata.csv` | `483854821b404dc6ef9454c06b8aab1be464a1b0cdb0c941c2610f942a81c318` | underlying data public domain (CFPB) |

Only count tables are committed: `scruple/sim/data/cfpb_aggregates.json` (issue or
sub-issue by company response, with the provenance above). No rows or narratives.

Reproduce (the raw files are described in the data directory's DATA_CARD.md):

```bash
uv run scruple sim fidelity                              # committed counts, FULL_MIX
uv run scruple sim fidelity --profile calibrated         # the opt-in calibrated profile
uv run scruple sim fidelity --raw-dir <data-src/real> --calibrate   # rebuild counts, refit
```

## What is compared

| comparison | real side | simulated side (4,000 cases, seed 1) |
|---|---|---|
| `card_category` | set 1, product Credit card: issue "Identity theft / Fraud / Embezzlement" (5,899) vs "Billing disputes" (11,042) | dispute filed under a fraud reason code vs any other category |
| `debit_category` | set 2: sub-issue "Transaction was not authorized" (953) vs "Money was taken from your account on the wrong day or for the wrong amount" (170) | fraud vs processing-error disputes |
| `relief_fraud` | set 1 fraud complaints closed with monetary relief (1,011) vs explanation, non-monetary relief or closed (4,276) | fraud disputes where the cardholder ends with money back under the reference policy |
| `relief_billing` | the same for billing disputes (3,473 vs 6,551) | the same for every other category |

Reference merchant policy (`scruple.sim.fidelity.reference_outcome`): represent with
every document held whenever representing is allowed; never refund, accept or escalate
(a pre-arbitration lapses, which moves the same money as accepting). "Money back" is
`cardholder_net_minor > 0` at resolution (chargeback stands, or a racing refund settled).
Excluded: the pre-2013 labels "Closed with relief" / "Closed without relief" (they do
not say whether relief was monetary: 551 fraud and 951 billing complaints), "In
progress" and "Untimely response".

Metrics: total variation (TV) and Jensen-Shannon divergence (base 2) between the two
categorical distributions, with percentile bootstrap 95% intervals (2,000 resamples;
both sides resampled multinomially from their counts). "TV floor" is the 95th
percentile of TV between the real distribution and a same-size (4,000) resample of
itself: a TV at or below the floor is indistinguishable from sampling noise at this
simulation size.

## Results (pasted from `scruple sim fidelity --raw-dir ... --calibrate --save`)

```
profile FULL_MIX (default): 4,000 synthetic cases (seed 1), 2,000 bootstrap resamples; real data CFPB (public domain)
comparison                     real share                sim share              TV [95% CI]           JS bits [95% CI]  TV floor
card_category    fraud=0.348 non_fraud=0.652 fraud=0.500 non_fraud=0.500 0.152 [0.135, 0.168]   0.0170 [0.0135, 0.0209]   0.014
debit_category   fraud=0.849 processing_error=0.151 fraud=0.904 processing_error=0.096 0.055 [0.031, 0.078]   0.0051 [0.0016, 0.0099]   0.015
relief_fraud     relief=0.191 no_relief=0.809 relief=0.410 no_relief=0.590 0.219 [0.194, 0.242]   0.0418 [0.0332, 0.0507]   0.017
relief_billing   relief=0.346 no_relief=0.654 relief=0.399 no_relief=0.601 0.052 [0.027, 0.076]   0.0021 [0.0006, 0.0044]   0.021
profile calibrated: 4,000 synthetic cases (seed 1), 2,000 bootstrap resamples; real data CFPB (public domain)
comparison                     real share                sim share              TV [95% CI]           JS bits [95% CI]  TV floor
card_category    fraud=0.348 non_fraud=0.652 fraud=0.358 non_fraud=0.642 0.010 [0.001, 0.025]   0.0001 [0.0000, 0.0005]   0.015
debit_category   fraud=0.849 processing_error=0.151 fraud=0.838 processing_error=0.162 0.011 [0.001, 0.038]   0.0002 [0.0000, 0.0020]   0.017
relief_fraud     relief=0.191 no_relief=0.809 relief=0.182 no_relief=0.818 0.009 [0.000, 0.032]   0.0001 [0.0000, 0.0013]   0.020
relief_billing   relief=0.346 no_relief=0.654 relief=0.374 no_relief=0.626 0.027 [0.006, 0.048]   0.0006 [0.0000, 0.0018]   0.018
fitted: fraud share 0.3482, valid share within fraud 0.0000, within other families 0.3540
```

| comparison | TV, default `FULL_MIX` | TV, calibrated | note |
|---|---|---|---|
| card_category | 0.152 [0.135, 0.168] | 0.010 [0.001, 0.025] | fitted target |
| debit_category | 0.055 [0.031, 0.078] | 0.011 [0.001, 0.038] | **not** a fitting target (different dataset and product) |
| relief_fraud | 0.219 [0.194, 0.242] | 0.009 [0.000, 0.032] | fitted target, at a boundary (below) |
| relief_billing | 0.052 [0.027, 0.076] | 0.027 [0.006, 0.048] | fitted target |

The calibration was fitted on seed 11 with 1,500 cases per evaluation and the report
uses seed 1, so the after-numbers are out of sample in the random draws (not in the
marginals: three of the four comparisons were the fitting targets).

## Reading the numbers

- **Default mix.** `FULL_MIX` (an invented mix, A-22) files half its disputes as fraud;
  the real credit card complaints are 35% fraud. The largest gap is relief on fraud:
  under the reference policy 41% of simulated fraud disputes end with the cardholder
  paid, against 19% of real fraud complaints closed with monetary relief. Every default
  TV is several times the noise floor, so the gaps are real at this sample size.
- **Calibrated mix.** Fitting the fraud share and the valid-claim share within each
  group brings every TV to the noise floor or within about 1.5x of it
  (relief_billing 0.027 vs floor 0.018). The debit comparison, which was not fitted,
  also moved from 0.055 to 0.011, but that is one held-out marginal and a two-bin one.
- **The fit hit a boundary, and that matters.** Even with no true fraud at all (every
  fraud dispute friendly fraud), the simulated relief rate on fraud is 0.200 (seed 11,
  1,500 cases), above the real 0.191, so the fitted valid share within fraud is 0. The
  calibrated profile therefore contains **no true fraud**. That is not a finding about
  real fraud rates: it says the simulated issuer (A-21) already gives cardholders the
  money in about a fifth of friendly-fraud cases when the merchant represents, and that
  a complaint's monetary relief rate cannot identify how many claims were valid. Do
  not use the calibrated profile where true fraud matters (fraud-detection agents,
  CE 3.0 behaviour on true fraud).
- Relief at the other end: with every non-fraud claim valid the simulated relief rate
  is only 0.569, and 0.239 with none valid (seed 11, 1,500 cases), so a range of real
  relief rates is reachable there; the fitted 35.4% valid share is interior.

## What is and is not comparable

- **Population.** CFPB rows are complaints a consumer chose to escalate to a federal
  regulator, dominated by a few large issuers (DATA_CARD.md). The simulator models
  disputes a merchant receives through a card network. Selection into each is
  different, so equal marginals would not mean equal populations.
- **Outcome.** "Closed with monetary relief" is the company's self-reported response to
  the complaint, not a network ruling. The simulator's "money back" depends on our
  invented issuer odds and on a policy we chose. The relief comparisons test whether
  the synthetic world's money flows are of the same order, nothing more.
- **Category mapping.** "Billing disputes" covers consumer disputes, processing errors
  and some authorization problems, and probably some fraud a consumer described as a
  billing error; we map it to "every non-fraud category". Set 2 is debit (Regulation E),
  not credit card chargebacks, and the two sub-issues are a small slice (1,123 of 8,000).
- **Time.** Set 1 is 2012 to 2015; the network rules in the simulator are as reported
  in 2016 to 2026 sources.
- **Not comparable at all with this data:** amounts, reason codes, network, deadlines
  and their timing, fees, representment win rates, arbitration, CE 3.0, monitoring
  programmes, split shipments, FX. Nothing here validates them.

## Status

The default generator is unchanged (`FULL_MIX`, A-22). The calibrated profile is
opt-in: `scruple.sim.fidelity.calibrated_mix()`, stored with its fitted values in
`scruple/sim/data/calibrated_cfpb_2012_2015.json`.
