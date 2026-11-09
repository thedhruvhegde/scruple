# Agents: episodes, tools, runners, MCP, SDK

How an external agent plays the dispute simulator and gets scored. Everything here
runs on synthetic data; every number an episode produces comes from `scruple.sim`.

## Episode

One episode is one simulated dispute (`scruple.agents.Episode(seed, case_type)`).

1. **Observe.** The agent gets an `Observation`: the case's whitelisted features
   (`scruple.domain.FIELD_TYPES`, the same whitelist learned rules use) and the tool
   names. No ground truth, no case type, no case, transaction or merchant ids.
2. **Act.** Read tools as often as it likes, then exactly one action. The action
   moves money on the simulator's ledger immediately.
3. **Explain.** `submit_decision` (or returning a `Decision` in-process) closes the
   episode and returns an `EpisodeScore`.

Same seed and same calls give the same score. Episode ids are opaque hashes of
(seed, case type, nonce). Every call is logged; `Episode.replay` rebuilds an episode
from its log, which is how the CLI persists episodes.

## Tools (`scruple/agents/tools.py`)

| tool | scope | notes |
|---|---|---|
| `get_case` | read | features and status |
| `list_evidence`, `get_evidence` | read | documents built from whitelisted features only |
| `get_ledger_view` | read | fresh, anonymised copy of the journal for this transaction |
| `request_info` | info | only before acting; the current sim has nothing more to say |
| `accept_liability`, `refund`, `escalate` | action | once per episode |
| `represent` | action | needs 1-10 known, unique evidence ids |
| `submit_decision` | submit | closes the episode |

Schemas are generated from the pydantic argument models (`$ref`s inlined, no extra
properties) and exported as `anthropic_tools()` and `openai_tools()`. Bad arguments,
unknown tools and refused calls raise `EpisodeError`, are counted as tool errors and
are shown to the model. After submit every call is rejected, reads included.

## Scoring (`scruple/agents/scoring.py`)

Computed from the journal with the existing verifiers, never from the agent's claims:

- `ledger_correctness`: double-entry invariants, over-refunds, result/journal mismatch
- `policy_constraints`: high value needs a human, no contest with late evidence, no
  accepting a duplicate
- `dispute_outcome`: cost against the best policy-compliant action in hindsight
  (`compliant_regret_usd_minor`); unconstrained `regret_usd_minor` is also reported

Flags: `claimed_cost_mismatch`, `claimed_action_mismatch`, `unknown_evidence_probe`,
`lapsed` (with the reason). Claims raise flags; they never change cost or verdicts.

## Ways in

| | where | verified |
|---|---|---|
| in-process callable `agent(obs, toolbox) -> Decision` | `interface.py` | tests |
| Anthropic Messages API runner (httpx) | `runners/anthropic.py` | fake transport only |
| OpenAI Chat Completions runner | `runners/openai.py` | fake transport only |
| local OpenAI-compatible runner (`base_url`) | `runners/local.py` | fake transport only |
| Claude Agent SDK, in-process MCP server | `adapters/claude_sdk.py` | server via in-memory MCP client; live `query()` not run |
| LangGraph graph and LangChain tools | `adapters/langgraph.py` | fake model |
| MCP server, simulator (`scruple mcp sim`) | `scruple/mcp/sim_server.py` | in-memory client and a real stdio subprocess |
| MCP server, gate (`scruple mcp gate --db`) | `scruple/mcp/gate_server.py` | in-memory client and a real stdio subprocess |
| Claude Code plugin | `plugins/claude-code/` | manifests validated; not installed into Claude Code |
| terminal (`scruple episode ...`) | `agents/cli.py`, `agents/store.py` | tests |
| `scruple.evaluate(agent, n, seed)` | `agents/sdk.py` | tests |

Runner limits: `max_turns`, `token_budget` (input plus output as reported by the API),
`max_tokens` per response, HTTP `timeout_s`, retries with backoff on 408/429/5xx/529,
one nudge if the model stops without a tool call. Anything that stops a run early
lapses the episode: the response deadline passes, so liability is accepted.

Default Anthropic model: `claude-sonnet-5-5` (`claude-haiku-5-5` as the fast option),
overridable with `SCRUPLE_ANTHROPIC_MODEL`. The OpenAI runners have no default model.

The gate MCP server does not expose approve or rollback: activation needs a named
human approver who is neither the owner nor the agent.

## Terminal

```bash
uv run scruple episode start --seed 7 --case-type fraud      # prints episode_id + observation
uv run scruple episode tools                                  # tool schemas
uv run scruple episode call <id> list_evidence
uv run scruple episode call <id> represent '{"evidence_ids": ["ev-01"]}'
uv run scruple episode submit <id> '{"explanation": "3DS authenticated"}'
```

State lives in `.scruple/episodes/` (or `--store`, or `$SCRUPLE_EPISODE_STORE`).

## Baselines (`scruple/agents/baselines.py`)

`rule_based` (the synthetic baseline policy), `always_accept` (the floor) and `oracle`
(cheapest compliant action using ground truth; only with `ground_truth=True`, and the
scorecard says so). `examples/rule_based_agent.py` prints all of them next to a
hand-written agent.

## Plugging in a richer simulator

`scruple/agents/env.py` is the only module that touches `scruple.sim`. A new
simulator provides a `CaseEnvironment` whose cases offer `observable`, `evidence`,
`ledger_view`, `request_info`, `resolve`, `run`, `cases`, `target_id` and
`ground_truth`. Places marked `ADAPTER:` are where the current sim is thinner than the
tool suite.

## Dispute episodes (the rich engine)

`scruple.agents.DisputeEpisode(seed, family)` (kind `dispute`) plays one case of
`scruple.sim.disputes.DisputeEngine` (FULL_MIX: reason codes, evidence documents,
refund races, split shipments, FX, CE 3.0) across its lifecycle. The old
single-action `Episode` (kind `simple`) is unchanged.

| tool | scope | notes |
|---|---|---|
| `get_case` | read | whitelisted case fields (`env.DISPUTE_CASE_FIELDS` plus the reason code's required and supporting evidence kinds, CE 3.0 eligibility, merchant risk tier), stage, who is due, deadline, allowed actions, refunded so far |
| `list_evidence`, `get_evidence` | read | the case's sealed documents (content hash included) |
| `get_ledger_view` | read | anonymised copy of this transaction's journal |
| `accept`, `represent`, `escalate`, `refund(amount_minor)`, `request_info` | action | engine actions, legal per stage; refused actions change nothing |
| `upload_evidence` | write | merchant-authored document; the environment records the ingestion day |
| `wait(days)` | clock | the issuer replies and networks rule as days pass |
| `submit_decision` | submit | `SelfReport`: explanation, optional stated win probability, structured claims; time then runs until the case resolves with the agent idle |

What changed against the caveats of the simple kind: `refund` is its own engine path
(refunding an inquiry resolves it without a chargeback; at chargeback it posts money
and the case stays open; a refund that would refund twice is refused).
`request_info` returns the cardholder's statement and the ids of evidence that
appeared since the agent last looked (for example a refund record the engine filed).
Cited evidence changes the outcome: the `SeededIssuer` scores the share of the reason
code's required evidence kinds that were submitted and not caught as flawed.

Every call (refused ones and tools that do not exist included) and every state
change (transitions, postings, evidence, incidents, clock) is appended to a
hash-chained trace (`scruple.verifiers.trace`). At close the episode builds an
`EpisodeRecord` (`scruple.verifiers.record`), the input of the dispute verifiers.
`ground_truth_for_reference()` is recorded in the trace as a protected read.

Terminal: `scruple episode start --kind dispute --seed 3 [--case-type true_fraud]`,
then `scruple episode call <id> wait '{"days": 5}'` and so on; `scruple episode tools
--kind dispute` prints the schemas. MCP: `start_dispute_episode`, `list_case_families`,
`accept`, `wait`, `upload_evidence`, `submit_dispute`, and `refund(amount_minor=...)`;
the read tools are shared. Reference policies: `scruple/agents/dispute_baselines.py`.

## Assumptions and gaps

- ASSUMPTION: `refund` maps to accepting liability; the current sim has no separate
  refund path.
- ASSUMPTION: an episode that lapses accepts liability (deadline passes).
- `request_info` always returns no new information in the current sim.
- Evidence choice does not change outcomes in the current sim; only timing does.
- The Responses API is not implemented; Chat Completions only.
- The plugin's `.mcp.json` points at `${CLAUDE_PLUGIN_ROOT}/../..`, which is this
  repository only when the plugin is used from a checkout.
- The Claude Agent SDK validates arguments against the schema itself, so those
  rejections do not reach the episode's tool error count.
- Python agents run in-process and could reach into private state; the in-process
  path is for development and baselines. For untrusted agents use MCP or the CLI.
