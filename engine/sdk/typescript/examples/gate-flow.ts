/**
 * The change-control flow over HTTP: propose a learned rule, let the gate evaluate
 * it (shadow then canary, synthetic cases), approve as a human reviewer, read the
 * evidence report, then verify the audit chain.
 *
 *   uv run scruple serve &              # http://127.0.0.1:8000
 *   npm run example:http
 *   SCRUPLE_URL=http://host:8000 node dist/examples/gate-flow.js
 */

import { fileURLToPath } from "node:url";

import { Client, ConflictError, type RuleSpec } from "../src/index.js";

// The demo approver is the default GateConfig.approvers entry.
// The library's known-good rule (scruple.sim.library.GOOD_RULE). Typed by the schema.
const SPEC: RuleSpec = {
  description: "Contest 'item not received' disputes when the carrier confirmed delivery.",
  conditions: [
    { field: "reason_code", op: "eq", value: "not_received" },
    { field: "delivery_confirmed", op: "eq", value: true },
  ],
  action: "contest",
  priority: 50,
};

export async function runFlow(c: Client, log: (line: string) => void = console.log): Promise<void> {
  const ruleId = "ts-example-friendly-fraud";
  let version = 1;
  try {
    const rv = await c.propose(ruleId, SPEC, { agent_id: "ts-example-agent" }, { owner: "payments-ops" });
    version = rv.version;
    log(`proposed ${rv.rule_id} v${rv.version} (${rv.status})`);
  } catch (err) {
    if (!(err instanceof ConflictError)) throw err;
    log("already proposed; reusing it");
  }
  for (const d of await c.evaluate(ruleId, version, { actor: "ts-example-agent" })) {
    log(`${d.stage}: ${d.outcome}: ${d.summary}`);
  }
  const approver = process.env.SCRUPLE_APPROVER ?? "model-risk@demo.bank";
  const approved = await c.approve(ruleId, version, { actor: approver });
  log(`approval: ${approved.outcome}`);
  const report = await c.report(approved.decision_id);
  log(report.split("\n")[0] ?? "");
  log(`audit chain ok: ${(await c.verifyIntegrity()).ok}`);
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  await runFlow(new Client({ baseUrl: process.env.SCRUPLE_URL ?? "http://127.0.0.1:8000" }));
}
