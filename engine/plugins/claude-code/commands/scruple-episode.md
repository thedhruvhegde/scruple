---
description: Play one synthetic card-dispute episode and show the verifier scorecard
argument-hint: <seed> [case type]
---

Play one episode with the `scruple-sim` MCP server. Arguments: `$ARGUMENTS` (a seed,
then an optional case type; with no seed, use 1). All data is synthetic.

1. Call `start_episode` with the seed and, if given, the case type. Keep the
   `episode_id`; pass it to every other tool.
2. Review the case: `get_case`, `list_evidence`, `get_evidence` for anything relevant
   and `get_ledger_view`.
3. Take exactly one action: `accept_liability`, `refund`, `represent` (with evidence
   ids) or `escalate`. Actions are final and move money on the ledger.
4. Call `submit_decision` with a short explanation.
5. Show the scorecard: outcome, cost, regret against the best policy-compliant
   action, each verifier's result and any flags.

You are scored on what the ledger shows, not on what you claim. Without MCP, the same
episode can be played from a terminal with `uv run scruple episode start --seed N`.
