/**
 * End to end against a real server and the real MCP simulator. Opt in, since it
 * needs the Python project (uv) and takes tens of seconds:
 *
 *   SCRUPLE_SDK_INTEGRATION=1 npm test
 */

import assert from "node:assert/strict";
import { spawn, type ChildProcess } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, before, describe, it } from "node:test";
import { fileURLToPath } from "node:url";

import { runEpisode } from "../examples/dispute-episode.js";
import { runFlow } from "../examples/gate-flow.js";
import { Client, NotFoundError } from "../src/index.js";
import { McpStdioClient, McpToolError } from "../src/mcp.js";

const enabled = process.env.SCRUPLE_SDK_INTEGRATION === "1";
const REPO = fileURLToPath(new URL("../../../../", import.meta.url));
const UV = ["uv", "run", "--project", REPO, "scruple"];

function freePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const srv = createServer();
    srv.listen(0, "127.0.0.1", () => {
      const addr = srv.address();
      srv.close(() => (typeof addr === "object" && addr ? resolve(addr.port) : reject(new Error("no port"))));
    });
  });
}

describe("http, against `scruple serve`", { skip: !enabled }, () => {
  let server: ChildProcess;
  let dir: string;
  let c: Client;

  before(async () => {
    dir = mkdtempSync(join(tmpdir(), "scruple-sdk-"));
    const port = await freePort();
    server = spawn(UV[0]!, [...UV.slice(1), "serve", "--db", join(dir, "t.db"), "--port", String(port)], {
      stdio: "ignore",
    });
    c = new Client({ baseUrl: `http://127.0.0.1:${port}`, retry: { attempts: 1 } });
    for (let i = 0; i < 300; i++) {
      try {
        if (await c.health()) return;
      } catch {
        await new Promise((r) => setTimeout(r, 200));
      }
    }
    throw new Error("server did not start");
  });

  after(() => {
    server.kill("SIGTERM");
    rmSync(dir, { recursive: true, force: true });
  });

  it("serves the version the types were generated for", async () => {
    const v = await c.version();
    assert.equal(v.name, "scruple");
    const schema = await c.openapi();
    assert.ok((schema.paths as Record<string, unknown>)["/rules"]);
  });

  it("runs the gate flow example", async () => {
    const lines: string[] = [];
    await runFlow(c, (l) => lines.push(l));
    assert.ok(lines.some((l) => l.includes("approval: activated")), lines.join("\n"));
    assert.ok(lines.includes("audit chain ok: true"));
  });

  it("pages through rules and decisions", async () => {
    const all = await c.rules();
    const paged = [];
    for await (const r of c.iterRules(1)) paged.push(r);
    assert.deepEqual(paged, all);
    const decisions = [];
    for await (const d of c.iterDecisions(undefined, 2)) decisions.push(d);
    assert.deepEqual(decisions, await c.decisions());
  });

  it("maps a real 404", async () => {
    await assert.rejects(c.decision("does-not-exist"), NotFoundError);
  });
});

describe("mcp, against `scruple mcp sim`", { skip: !enabled }, () => {
  let sim: McpStdioClient;
  before(async () => {
    sim = await McpStdioClient.spawn(UV[0]!, [...UV.slice(1), "mcp", "sim"]);
  });
  after(async () => {
    await sim.close();
  });

  it("lists the dispute tools", async () => {
    const names = (await sim.listTools()).map((t) => t.name);
    for (const n of ["start_dispute_episode", "list_evidence", "represent", "accept", "wait", "submit_dispute"]) {
      assert.ok(names.includes(n), n);
    }
  });

  it("drives a dispute episode to a scorecard, deterministically", async () => {
    const a = await runEpisode(sim, 7, () => {});
    const b = await runEpisode(sim, 7, () => {});
    assert.ok(a && typeof a === "object");
    const strip = (x: unknown) => JSON.stringify(x).replace(/ep-[0-9a-f]+/g, "ep-");
    assert.equal(strip(a), strip(b));
  });

  it("surfaces tool errors", async () => {
    await assert.rejects(sim.callTool("wait", { episode_id: "nope", days: 1 }), McpToolError);
  });
});
