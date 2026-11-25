"""The finance-operations environment suite: reconciliation, AML triage, ACH returns
and payables as licensable, verifiable RL tasks.

Modules (import them directly; this package imports nothing so that the episode
modules can depend on `scruple.finops.knobs` without a cycle):

- `knobs`: difficulty knobs carried in the case type
- `artefacts`: seeded adversarial artefacts (inbox messages, case notes, memos)
- `envs`: the environment registry (episode class, tools, verifiers, policies)
- `policies`: honest, floor, oracle, credulous and reward-hacking policies
- `verifiers`: versioned scorecards (v1 is the kind's own, v2 hardened)
- `exploits`: exploit modules and the cross-environment coverage table
- `difficulty`: nominal and measured difficulty, curricula
- `tasks`: portable task export, replay grading, manifests
- `server`: a minimal HTTP grader and remote environment (FastAPI)
- `parallel`: multiprocessing bulk generation, rollouts and grading; throughput
- `cli`: `scruple tasks ...` and `scruple finops ...`

Everything is synthetic.
"""
