---
description: Submit a candidate rule to the scruple gate, evaluate it and report the verdict
argument-hint: <path to rule json>
---

Gate a candidate dispute rule with the `scruple-gate` MCP server. All data is synthetic.

The rule file is `$ARGUMENTS`. It holds `rule_id`, `spec` (a RuleSpec: `description`,
`conditions` as `[{field, op, value}]`, `action` one of accept, contest, escalate, and
`priority`), `owner` and `provenance.agent_id`.

1. Read the rule file. If it is missing or malformed, say what is wrong and stop.
2. Call `submit_rule` with the rule's fields. Note the version it returns.
3. Call `evaluate_rule` with the rule id and version.
4. Call `get_evaluation_status`, then `get_report` for each decision id.
5. Report: the outcome of each stage (shadow, canary), the reasons, any failed checks
   and whether the rule now waits for a human approver.

Do not try to approve or activate the rule. Approval needs a named human who is not
the rule's owner or agent, and is done with `scruple approve` outside this session.
Do not invent numbers; quote only what the reports say.
