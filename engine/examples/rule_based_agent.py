"""A hand-written rule-based dispute agent, scored with `scruple.evaluate`.

Run: uv run python examples/rule_based_agent.py [n] [seed]

The agent sees only whitelisted case features (`obs.fields`) and acts through tools.
Its scorecard is printed next to the three built-in reference agents. All numbers
come from the synthetic simulator.
"""

from __future__ import annotations

import sys

import scruple
from scruple.agents import Decision, Observation, Toolbox
from scruple.agents.baselines import always_accept, oracle, rule_based


def my_agent(obs: Observation, tools: Toolbox) -> Decision:
    f = obs.fields
    if int(f["amount_usd_minor"]) > 1_000_000:
        tools.call("escalate", reason="over $10,000: needs a human")
        return Decision(explanation="High value, escalated.")
    if not f["evidence_on_time"]:
        tools.call("accept_liability", reason="evidence cannot arrive in time")
        return Decision(explanation="Evidence would be late, so contesting cannot win.")
    evidence = tools.call("list_evidence")["evidence"]
    kinds = {e["kind"] for e in evidence}
    strong = {"delivery", "prior_dispute", "refund_record", "fx_record"} & kinds
    if strong or (f["reason_code"] == "fraud" and f["three_ds"]):
        ids = [e["evidence_id"] for e in evidence]
        tools.call("represent", evidence_ids=ids, narrative="see attached evidence")
        return Decision(explanation=f"Contested with {', '.join(sorted(kinds))}.")
    tools.call("accept_liability", reason="no compelling evidence")
    return Decision(explanation="No compelling evidence, accepted.")


def main(n: int = 40, seed: int = 0) -> list[scruple.Scorecard]:
    cards = [
        scruple.evaluate(my_agent, n=n, seed=seed),
        scruple.evaluate(rule_based, n=n, seed=seed),
        scruple.evaluate(always_accept, n=n, seed=seed),
        scruple.evaluate(oracle, n=n, seed=seed, ground_truth=True),
    ]
    for card in cards:
        print(card.summary(), end="\n\n")
    return cards


if __name__ == "__main__":
    args = [int(a) for a in sys.argv[1:3]]
    main(*args)
