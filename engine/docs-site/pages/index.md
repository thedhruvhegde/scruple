# scruple

Simulation and hack-resistant verifiers for agents that move money. Card disputes
first.

- The same verifier is three things: a reward for training, an eval for choosing a
  model, and a gate for releasing a change.
- Scores come from the ledger and a hash-chained trace, never from what the agent says
  it did.
- Nothing an agent learns goes live until it has been verified (shadow replay plus
  verifiers), staged (canary) and approved by a named person, with a tamper-evident
  audit trail and an evidence report for every decision.

!!! note "What the numbers are"
    Data in a fresh install is synthetic. No partner data has run through scruple yet.
    Results from simulation are labelled synthetic; results from public data name the
    dataset.

## Start here

- [Quickstart](quickstart.md): install, run the demo, set up a tenant with
  `scruple init`, serve the API and the console.
- [Concepts](concepts.md): rules, verifiers, the gate stages, the audit chain,
  tenants and roles, usage.
- [Install](install.md): pip, Docker, Compose, from source.
- [Pilot](pilot/README.md): six weeks on a partner's own dispute history.
- [HTTP API](api.md): every route, the permission it needs, its inputs and outputs,
  generated from the server's OpenAPI schema.
