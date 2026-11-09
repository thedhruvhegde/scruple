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
