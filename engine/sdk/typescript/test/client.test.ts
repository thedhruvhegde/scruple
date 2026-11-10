import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  ApiError,
  BadRequestError,
  Client,
  ConflictError,
  ForbiddenError,
  NO_RETRY,
  NotFoundError,
  RateLimitedError,
  ServerError,
  TransportError,
  UnauthorizedError,
  ValidationError,
  retryDelay,
  DEFAULT_RETRY,
  type DecisionOut,
  type RuleSpec,
} from "../src/index.js";

type Step = Response | Error;

interface Seen {
  method: string;
  url: URL;
  headers: Headers;
  body: string | undefined;
}

function scripted(steps: Step[]): { fetch: typeof fetch; seen: Seen[] } {
  const seen: Seen[] = [];
  const queue = [...steps];
  const fake = async (input: string | URL | Request, init?: RequestInit): Promise<Response> => {
    seen.push({
      method: init?.method ?? "GET",
      url: new URL(String(input)),
      headers: new Headers(init?.headers),
      body: typeof init?.body === "string" ? init.body : undefined,
    });
    const step = queue.shift();
    if (step === undefined) throw new Error("script exhausted");
    if (step instanceof Error) throw step;
    return step;
  };
  return { fetch: fake as typeof fetch, seen };
}

const json = (status: number, body: unknown, headers: Record<string, string> = {}): Response =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json", ...headers } });

function client(steps: Step[], opts: { attempts?: number; actor?: string; apiKey?: string } = {}) {
  const { fetch, seen } = scripted(steps);
  const slept: number[] = [];
  const c = new Client({
    baseUrl: "http://scruple.test/",
    fetch,
    sleep: async (ms) => {
      slept.push(ms);
    },
    retry: { attempts: opts.attempts ?? 3 },
    ...(opts.actor ? { actor: opts.actor } : {}),
    ...(opts.apiKey ? { apiKey: opts.apiKey } : {}),
  });
  return { c, seen, slept };
}

const DECISION: DecisionOut = {
  decision_id: "d1",
  stage: "activation",
  outcome: "activated",
  rule_id: "r",
  version: 1,
  summary: "s",
  reasons: [],
  report_url: "/decisions/d1/report",
};

describe("requests", () => {
  it("builds urls, query strings and headers", async () => {
    const { c, seen } = client([json(200, [])]);
    await c.decisions("rule-1", { limit: 5, offset: 10 });
    const s = seen[0]!;
    assert.equal(s.method, "GET");
    assert.equal(s.url.pathname, "/decisions");
    assert.deepEqual(Object.fromEntries(s.url.searchParams), { rule_id: "rule-1", limit: "5", offset: "10" });
    assert.equal(s.headers.get("accept"), "application/json");
  });

  it("omits undefined query values", async () => {
    const { c, seen } = client([json(200, { ok: true, chain: {}, problems: [] })]);
    await c.verifyIntegrity();
    assert.equal(seen[0]!.url.search, "");
  });

  it("sends the actor and json body on state changes", async () => {
    const { c, seen } = client([json(200, DECISION)], { actor: "risk@bank" });
    const d = await c.rollback("r", "drill");
    assert.equal(d.decision_id, "d1");
    const s = seen[0]!;
    assert.equal(s.method, "POST");
    assert.equal(s.url.pathname, "/rules/r/rollback");
    assert.equal(s.headers.get("x-actor"), "risk@bank");
    assert.equal(s.headers.get("content-type"), "application/json");
    assert.deepEqual(JSON.parse(s.body!), { reason: "drill" });
  });

  it("per-call actor overrides the default", async () => {
    const { c, seen } = client([json(200, DECISION)], { actor: "a" });
    await c.approve("r", 1, { actor: "b" });
    assert.equal(seen[0]!.headers.get("x-actor"), "b");
  });

  it("sends the api key on every request and no actor", async () => {
    const { c, seen } = client([json(200, []), json(200, DECISION), json(201, {})], { apiKey: "scr_k", actor: "x" });
    await c.rules();
    await c.approve("r", 1);
    const spec = { description: "x", conditions: [], action: "accept_liability" } as unknown as RuleSpec;
    await c.propose("rule-1", spec, { agent_id: "agent-7" });
    for (const s of seen) {
      assert.equal(s.headers.get("x-api-key"), "scr_k");
      assert.equal(s.headers.get("x-actor"), null);
    }
  });

  it("sends the default actor on reads too (development-only servers)", async () => {
    const { c, seen } = client([json(200, [])], { actor: "auditor" });
    await c.rules();
    assert.equal(seen[0]!.headers.get("x-actor"), "auditor");
  });

  it("maps 401 to UnauthorizedError", async () => {
    const { c } = client([json(401, { detail: "invalid API key" })], { attempts: 1, apiKey: "bad" });
    await assert.rejects(c.rules(), UnauthorizedError);
  });

  it("refuses state changes without an actor", async () => {
    const { c, seen } = client([]);
    assert.throws(() => c.approve("r", 1), /needs an actor/);
    assert.equal(seen.length, 0);
  });

  it("propose sends the provenance agent as actor and defaults the owner", async () => {
    const { c, seen } = client([json(201, {})]);
    const spec = { description: "x", conditions: [], action: "accept_liability" } as unknown as RuleSpec;
    await c.propose("rule-1", spec, { agent_id: "agent-7" });
    const s = seen[0]!;
    assert.equal(s.headers.get("x-actor"), "agent-7");
    assert.deepEqual(JSON.parse(s.body!).owner, "agent-7");
    assert.equal(JSON.parse(s.body!).rule_id, "rule-1");
  });

  it("percent-encodes and validates path segments", async () => {
    const { c, seen } = client([json(404, { detail: "unknown decision" })], { attempts: 1 });
    await assert.rejects(c.decision("a?b#c d"), NotFoundError);
    assert.equal(seen[0]!.url.pathname, "/decisions/a%3Fb%23c%20d");
    for (const bad of ["", "a/b", ".", ".."]) {
      assert.throws(() => c.ruleVersions(bad), /path segment/);
    }
    assert.throws(() => c.evaluate("r", 0, { actor: "a" }), /version/);
    assert.throws(() => c.evaluate("r", 1.5, { actor: "a" }), /version/);
  });

  it("reads the report as text", async () => {
    const { c } = client([new Response("# Evidence report\n", { status: 200 })]);
    assert.equal(await c.report("d1"), "# Evidence report\n");
  });
});

describe("errors", () => {
  const cases: [number, new (...a: never[]) => ApiError][] = [
    [400, BadRequestError],
    [403, ForbiddenError],
    [404, NotFoundError],
    [409, ConflictError],
    [422, ValidationError],
    [429, RateLimitedError],
    [500, ServerError],
    [503, ServerError],
    [418, ApiError],
  ];
  for (const [status, Cls] of cases) {
    it(`maps ${status} to ${Cls.name}`, async () => {
      const { c } = client([json(status, { detail: "nope" }, { "x-request-id": "req-1" })], { attempts: 1 });
      const err = await c.version().then(
        () => assert.fail("expected an error"),
        (e: unknown) => e,
      );
      assert.ok(err instanceof Cls);
      assert.equal(Object.getPrototypeOf(err).constructor, Cls);
      assert.ok(err instanceof ApiError);
      assert.equal(err.status, status);
      assert.equal(err.detail, "nope");
      assert.equal(err.requestId, "req-1");
      assert.equal(err.method, "GET");
      assert.equal(err.path, "/version");
    });
  }

  it("keeps a non-json body as text", async () => {
    const { c } = client([new Response("<html>boom</html>", { status: 500 })], { attempts: 1 });
    await assert.rejects(c.health(), (e: unknown) => e instanceof ServerError && e.detail === "<html>boom</html>");
  });

  it("keeps structured validation details", async () => {
    const detail = [{ loc: ["query", "rule"], msg: "unknown query parameter", type: "extra_forbidden" }];
    const { c } = client([json(422, { detail })], { attempts: 1 });
    await assert.rejects(c.decisions(), (e: unknown) => e instanceof ValidationError && Array.isArray(e.detail));
  });
});

describe("retries", () => {
  for (const status of [429, 502, 503, 504]) {
    it(`GET retries ${status} with exponential backoff`, async () => {
      const { c, seen, slept } = client([json(status, {}), json(status, {}), json(200, { status: "ok" })]);
      assert.equal(await c.health(), true);
      assert.equal(seen.length, 3);
      assert.deepEqual(slept, [250, 500]);
    });
  }

  it("GET gives up after the last attempt", async () => {
    const { c, seen } = client([json(503, {}), json(503, {}), json(503, {})]);
    await assert.rejects(c.health(), ServerError);
    assert.equal(seen.length, 3);
  });

  it("GET retries network errors, then throws TransportError", async () => {
    const ok = client([new TypeError("fetch failed"), json(200, { status: "ok" })]);
    assert.equal(await ok.c.health(), true);
    const bad = client([new TypeError("a"), new TypeError("b"), new TypeError("c")]);
    await assert.rejects(bad.c.health(), (e: unknown) => e instanceof TransportError && e.cause instanceof TypeError);
  });

  it("POST is never retried", async () => {
    const status = client([json(503, { detail: "busy" })], { actor: "a" });
    await assert.rejects(status.c.approve("r", 1), ServerError);
    assert.equal(status.seen.length, 1);
    const network = client([new TypeError("fetch failed")], { actor: "a" });
    await assert.rejects(network.c.approve("r", 1), TransportError);
    assert.equal(network.seen.length, 1);
  });

  it("honours and caps Retry-After", async () => {
    const { c, slept } = client([
      json(429, {}, { "retry-after": "2" }),
      json(429, {}, { "retry-after": "999" }),
      json(200, { status: "ok" }),
    ]);
    await c.health();
    assert.deepEqual(slept, [2000, 8000]);
  });

  it("falls back to the schedule on an http-date Retry-After", () => {
    assert.equal(retryDelay(DEFAULT_RETRY, 1, "Wed, 21 Oct 2015 07:28:00 GMT"), 250);
    assert.equal(retryDelay(DEFAULT_RETRY, 2, ""), 500);
    assert.equal(retryDelay(DEFAULT_RETRY, 10), 8000);
  });

  it("delays are monotone and capped", () => {
    for (let attempt = 1; attempt < 12; attempt++) {
      const d = retryDelay(DEFAULT_RETRY, attempt);
      assert.ok(d <= DEFAULT_RETRY.maxBackoffMs);
      assert.ok(d >= retryDelay(DEFAULT_RETRY, Math.max(1, attempt - 1)));
    }
  });

  it("NO_RETRY tries once", async () => {
    const { fetch, seen } = scripted([json(503, {})]);
    const c = new Client({ baseUrl: "http://t", fetch, retry: NO_RETRY });
    await assert.rejects(c.health(), ServerError);
    assert.equal(seen.length, 1);
  });

  it("rejects an invalid policy", () => {
    assert.throws(() => new Client({ retry: { attempts: 0 } }), RangeError);
  });
});

describe("paging", () => {
  it("follows X-Total-Count", async () => {
    const { c, seen } = client([
      json(200, [{ n: 1 }, { n: 2 }], { "x-total-count": "3" }),
      json(200, [{ n: 3 }], { "x-total-count": "3" }),
    ]);
    const got: unknown[] = [];
    for await (const r of c.iterRules(2)) got.push(r);
    assert.deepEqual(got, [{ n: 1 }, { n: 2 }, { n: 3 }]);
    assert.deepEqual(
      seen.map((s) => Object.fromEntries(s.url.searchParams)),
      [
        { limit: "2", offset: "0" },
        { limit: "2", offset: "2" },
      ],
    );
  });

  it("stops on a short page when the header is missing", async () => {
    const { c, seen } = client([json(200, [1, 2]), json(200, [3])]);
    const got: unknown[] = [];
    for await (const d of c.iterDecisions("r", 2)) got.push(d);
    assert.deepEqual(got, [1, 2, 3]);
    assert.equal(seen[1]!.url.searchParams.get("rule_id"), "r");
  });

  it("stops on an empty page", async () => {
    const { c, seen } = client([json(200, [], { "x-total-count": "5" })]);
    for await (const _ of c.iterRules(2)) assert.fail("no items expected");
    assert.equal(seen.length, 1);
  });

  it("validates the page size", async () => {
    const { c } = client([]);
    await assert.rejects(c.iterRules(0).next(), RangeError);
    await assert.rejects(c.iterRules(1001).next(), RangeError);
  });
});
