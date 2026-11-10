/**
 * Drive one synthetic dispute episode end to end over MCP (stdio), with a plain
 * evidence policy: represent when every required document is on file, else accept.
 * Then let simulated days pass until the issuer resolves the case, and submit.
 *
 *   npm run example:mcp                       # runs `uv run scruple mcp sim` from the repo
 *   SCRUPLE_MCP="scruple mcp sim" SEED=11 node dist/examples/dispute-episode.js
 *
 * Everything here is synthetic and deterministic given the seed.
 */

import { fileURLToPath } from "node:url";

import { McpStdioClient } from "../src/mcp.js";

interface Status {
  stage: string;
  resolved: boolean;
  allowed_actions: string[];
  deadline: { due: string; days_left: number } | null;
}

interface Observation {
  episode_id: string;
  case: { network: string; reason_code: string; reason_title: string; amount_minor: number; currency: string; required_evidence: string[]; supporting_evidence: string[] };
  status: Status;
}

interface Evidence {
  evidence_id: string;
  kind: string;
}

export async function runEpisode(sim: McpStdioClient, seed: number, log: (line: string) => void = console.log) {
  const { episode_id: id, observation } = await sim.callTool<{ episode_id: string; observation: Observation }>(
    "start_dispute_episode",
    { seed },
  );
  const c = observation.case;
  log(`episode ${id}: ${c.network} ${c.reason_code} (${c.reason_title}), ${c.amount_minor} ${c.currency} minor units`);

  const { evidence } = await sim.callTool<{ evidence: Evidence[] }>("list_evidence", { episode_id: id });
  const onFile = new Set(evidence.map((e) => e.kind));
  const missing = c.required_evidence.filter((k) => !onFile.has(k));
  const useful = new Set([...c.required_evidence, ...c.supporting_evidence]);
  const chosen = evidence.filter((e) => useful.has(e.kind)).map((e) => e.evidence_id);

  let action: string;
  if (missing.length === 0 && observation.status.allowed_actions.includes("represent")) {
    await sim.callTool("represent", {
      episode_id: id,
      evidence_ids: chosen,
      narrative: `Submitting ${chosen.length} documents covering ${c.required_evidence.join(", ")}.`,
    });
    action = `represented with ${chosen.length} documents`;
  } else {
    await sim.callTool("accept", { episode_id: id, reason: `missing required evidence: ${missing.join(", ") || "none"}` });
    action = "accepted";
  }
  log(`action: ${action}`);

  // Let the issuer reply. Each wait returns the new status.
  for (let i = 0; i < 12; i++) {
    const { status } = await sim.callTool<{ status: Status }>("get_case", { episode_id: id });
    if (status.resolved) break;
    if (status.allowed_actions.includes("represent") && status.stage !== "chargeback") {
      // A later stage asks for a response again: stop here rather than guess.
      break;
    }
    await sim.callTool("wait", { episode_id: id, days: 30 });
  }

  const result = await sim.callTool("submit_dispute", {
    episode_id: id,
    explanation: `Evidence policy: ${action}.`,
  });
  log(JSON.stringify(result, null, 2));
  return result;
}

async function main(): Promise<void> {
  const seed = Number(process.env.SEED ?? 7);
  const cmd = (process.env.SCRUPLE_MCP ?? "").trim().split(/\s+/).filter(Boolean);
  const repo = fileURLToPath(new URL("../../../../", import.meta.url));
  const [command, ...args] = cmd.length ? cmd : ["uv", "run", "--project", repo, "scruple", "mcp", "sim"];
  const sim = await McpStdioClient.spawn(command!, args);
  try {
    await runEpisode(sim, seed);
  } finally {
    await sim.close();
  }
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  await main();
}
