/**
 * Fetch-based client for the scruple HTTP API.
 *
 *   const c = new Client({ baseUrl: "http://127.0.0.1:8000", apiKey: process.env.SCRUPLE_API_KEY });
 *   console.log((await c.version()).version);
 *   for await (const rule of c.iterRules()) console.log(rule.rule_id, rule.status);
 *
 * Types come from the server's OpenAPI schema (src/schema.ts, generated).
 *
 * Retries: GET requests are retried on network errors and 429/502/503/504 with
 * exponential backoff (honouring Retry-After, capped). POST requests are never
 * retried after they may have reached the server, so an evaluation or approval
 * cannot run twice because of a retry. There is no jitter: behaviour is reproducible.
 */

import { ApiError, TransportError, errorFor } from "./errors.js";
import type {
  BatchResult,
  BuildInfo,
  Decision,
  DecisionOut,
  IntegrityReport,
  LiveDecision,
  Provenance,
  RegisterIn,
  RuleSpec,
  RuleVersion,
  ShadowSummary,
} from "./types.js";

export const TOTAL_COUNT = "x-total-count";
export const MAX_PAGE = 1000;
const IDEMPOTENT = new Set(["GET", "HEAD", "OPTIONS"]);

export interface RetryPolicy {
  /** Total tries, including the first. */
  attempts: number;
  /** Milliseconds before the first retry; doubles each time. */
  backoffMs: number;
  maxBackoffMs: number;
  statuses: ReadonlySet<number>;
}

export const DEFAULT_RETRY: RetryPolicy = Object.freeze({
  attempts: 3,
  backoffMs: 250,
  maxBackoffMs: 8000,
  statuses: new Set([429, 502, 503, 504]),
});

export const NO_RETRY: RetryPolicy = Object.freeze({ ...DEFAULT_RETRY, attempts: 1 });

/** Milliseconds to wait after failed try number `attempt` (1-based). */
export function retryDelay(policy: RetryPolicy, attempt: number, retryAfter?: string | null): number {
  if (retryAfter != null && retryAfter.trim() !== "") {
    const seconds = Number(retryAfter);
    if (Number.isFinite(seconds)) {
      return Math.min(Math.max(seconds * 1000, 0), policy.maxBackoffMs);
    }
  }
  return Math.min(policy.backoffMs * 2 ** (attempt - 1), policy.maxBackoffMs);
}

export interface ClientOptions {
  baseUrl?: string;
  /**
   * A scruple API key (`scruple keys create`), sent as X-API-Key on every request.
   * The server takes the caller's identity from it; no X-Actor is sent.
   */
  apiKey?: string;
  /**
   * For a server in the development-only mode that trusts X-Actor
   * (SCRUPLE_AUTH_DEV_ACTOR_HEADER=true): sent on every request. Ignored with apiKey.
   */
  actor?: string;
  retry?: Partial<RetryPolicy>;
  /** Per-try timeout in milliseconds. */
  timeoutMs?: number;
  fetch?: typeof fetch;
  sleep?: (ms: number) => Promise<void>;
  headers?: Record<string, string>;
}

export interface RequestOptions {
  json?: unknown;
  query?: Record<string, string | number | boolean | null | undefined>;
  headers?: Record<string, string>;
}

export interface Page {
  limit?: number;
  offset?: number;
}

export class Client {
  readonly baseUrl: string;
  readonly actor: string | undefined;
  readonly retry: RetryPolicy;
  readonly #apiKey: string | undefined;
  private readonly timeoutMs: number;
  private readonly fetchImpl: typeof fetch;
  private readonly sleep: (ms: number) => Promise<void>;
  private readonly extraHeaders: Record<string, string>;

  constructor(options: ClientOptions = {}) {
    this.baseUrl = (options.baseUrl ?? "http://127.0.0.1:8000").replace(/\/+$/, "");
    this.actor = options.actor;
    this.#apiKey = options.apiKey;
    this.retry = { ...DEFAULT_RETRY, ...options.retry };
    if (!Number.isInteger(this.retry.attempts) || this.retry.attempts < 1) {
      throw new RangeError("retry.attempts must be an integer of at least 1");
    }
    this.timeoutMs = options.timeoutMs ?? 30_000;
    this.fetchImpl = options.fetch ?? globalThis.fetch.bind(globalThis);
    this.sleep = options.sleep ?? ((ms) => new Promise((resolve) => setTimeout(resolve, ms)));
    this.extraHeaders = options.headers ?? {};
  }

  // --- transport ------------------------------------------------------------------

  /** Send one request with the retry policy; throw an ApiError on non-2xx. */
  async request(method: string, path: string, options: RequestOptions = {}): Promise<Response> {
    const verb = method.toUpperCase();
    const url = new URL(this.baseUrl + path);
    for (const [k, v] of Object.entries(options.query ?? {})) {
      if (v !== undefined && v !== null) url.searchParams.set(k, String(v));
    }
    const headers: Record<string, string> = { accept: "application/json", ...this.extraHeaders, ...options.headers };
    if (this.#apiKey !== undefined) {
      headers["x-api-key"] = this.#apiKey;
    } else if (this.actor !== undefined && headers["x-actor"] === undefined) {
      headers["x-actor"] = this.actor; // a development-only server authenticates by it
    }
    let body: string | undefined;
    if (options.json !== undefined) {
      headers["content-type"] = "application/json";
      body = JSON.stringify(options.json);
    }

    for (let attempt = 1; ; attempt++) {
      const last = attempt >= this.retry.attempts;
      let resp: Response;
      try {
        const init: RequestInit = { method: verb, headers, signal: AbortSignal.timeout(this.timeoutMs) };
        if (body !== undefined) init.body = body;
        resp = await this.fetchImpl(url, init);
      } catch (err) {
        // fetch does not say whether the request left the machine, so only
        // idempotent requests are retried on a network error.
        if (last || !IDEMPOTENT.has(verb)) {
          throw new TransportError(`${verb} ${path}: ${String(err)}`, err);
        }
        await this.sleep(retryDelay(this.retry, attempt));
        continue;
      }
      if (this.retry.statuses.has(resp.status) && IDEMPOTENT.has(verb) && !last) {
        await resp.body?.cancel();
        await this.sleep(retryDelay(this.retry, attempt, resp.headers.get("retry-after")));
        continue;
      }
      if (resp.ok) return resp;
      throw await toApiError(resp, verb, path);
    }
  }

  private async json<T>(method: string, path: string, options?: RequestOptions): Promise<T> {
    return (await (await this.request(method, path, options)).json()) as T;
  }

  private actorHeaders(actor: string | undefined): Record<string, string> {
    if (this.#apiKey !== undefined) return {}; // the key is the identity
    const who = actor ?? this.actor;
    if (!who) throw new TypeError("this call needs an actor: pass { actor } here or to new Client()");
    return { "x-actor": who };
  }

  private async *pages<T>(
    path: string,
    pageSize: number,
    query: Record<string, string | undefined> = {},
  ): AsyncGenerator<T, void, undefined> {
    if (!Number.isInteger(pageSize) || pageSize < 1 || pageSize > MAX_PAGE) {
      throw new RangeError(`pageSize must be an integer between 1 and ${MAX_PAGE}`);
    }
    let offset = 0;
    for (;;) {
      const resp = await this.request("GET", path, { query: { ...query, limit: pageSize, offset } });
      const page = (await resp.json()) as T[];
      yield* page;
      offset += page.length;
      const total = resp.headers.get(TOTAL_COUNT);
      const done = total !== null ? offset >= Number(total) : page.length < pageSize;
      if (done || page.length === 0) return;
    }
  }

  // --- meta -----------------------------------------------------------------------

  async health(): Promise<boolean> {
    return (await this.json<{ status: string }>("GET", "/health")).status === "ok";
  }

  version(): Promise<BuildInfo> {
    return this.json("GET", "/version");
  }

  /** Who the server says the caller is: subject, tenant, method, permissions. */
  whoami(): Promise<Record<string, unknown>> {
    return this.json("GET", "/auth/whoami");
  }

  openapi(): Promise<Record<string, unknown>> {
    return this.json("GET", "/openapi.json");
  }

  // --- rules ----------------------------------------------------------------------

  /** Register a candidate rule version. The caller (the key's subject) must be `provenance.agent_id`. */
  propose(
    ruleId: string,
    spec: RuleSpec,
    provenance: Provenance,
    options: { owner?: string } = {},
  ): Promise<RuleVersion> {
    const body: RegisterIn = { rule_id: ruleId, spec, owner: options.owner ?? provenance.agent_id, provenance };
    const headers: Record<string, string> = this.#apiKey === undefined ? { "x-actor": provenance.agent_id } : {};
    return this.json("POST", "/rules", { json: body, headers });
  }

  rules(page: Page = {}): Promise<RuleVersion[]> {
    return this.json("GET", "/rules", { query: { limit: page.limit, offset: page.offset } });
  }

  iterRules(pageSize = 100): AsyncGenerator<RuleVersion, void, undefined> {
    return this.pages<RuleVersion>("/rules", pageSize);
  }

  ruleVersions(ruleId: string): Promise<RuleVersion[]> {
    return this.json("GET", `/rules/${seg(ruleId)}`);
  }

  evaluate(ruleId: string, version: number, options: { actor?: string } = {}): Promise<DecisionOut[]> {
    return this.json("POST", `/rules/${seg(ruleId)}/versions/${int(version)}/evaluate`, {
      headers: this.actorHeaders(options.actor),
    });
  }

  approve(ruleId: string, version: number, options: { actor?: string } = {}): Promise<DecisionOut> {
    return this.json("POST", `/rules/${seg(ruleId)}/versions/${int(version)}/approve`, {
      headers: this.actorHeaders(options.actor),
    });
  }

  rollback(ruleId: string, reason: string, options: { actor?: string } = {}): Promise<DecisionOut> {
    return this.json("POST", `/rules/${seg(ruleId)}/rollback`, {
      json: { reason },
      headers: this.actorHeaders(options.actor),
    });
  }

  // --- decisions ------------------------------------------------------------------

  decisions(ruleId?: string, page: Page = {}): Promise<DecisionOut[]> {
    return this.json("GET", "/decisions", { query: { rule_id: ruleId, limit: page.limit, offset: page.offset } });
  }

  iterDecisions(ruleId?: string, pageSize = 100): AsyncGenerator<DecisionOut, void, undefined> {
    return this.pages<DecisionOut>("/decisions", pageSize, { rule_id: ruleId });
  }

  decision(decisionId: string): Promise<Decision> {
    return this.json("GET", `/decisions/${seg(decisionId)}`);
  }

  /** The evidence report for a decision, as Markdown. */
  async report(decisionId: string): Promise<string> {
    return (await this.request("GET", `/decisions/${seg(decisionId)}/report`)).text();
  }

  // --- shadow mode ----------------------------------------------------------------

  shadowRecord(
    ruleId: string,
    version: number,
    decisions: LiveDecision[],
    options: { actor?: string } = {},
  ): Promise<BatchResult> {
    return this.json("POST", `/shadow/rules/${seg(ruleId)}/versions/${int(version)}/decisions`, {
      json: { decisions },
      headers: this.actorHeaders(options.actor),
    });
  }

  shadowSummary(ruleId: string, version: number): Promise<ShadowSummary> {
    return this.json("GET", `/shadow/rules/${seg(ruleId)}/versions/${int(version)}`);
  }

  // --- audit ----------------------------------------------------------------------

  verifyIntegrity(expectedHead?: string): Promise<IntegrityReport> {
    return this.json("GET", "/audit/verify", { query: { expected_head: expectedHead } });
  }
}

/** One URL path segment; refuses values that would change the path. */
export function seg(value: string): string {
  if (!value || value.includes("/") || value === "." || value === "..") {
    throw new TypeError(`not a valid path segment: ${JSON.stringify(value)}`);
  }
  return encodeURIComponent(value);
}

function int(n: number): number {
  if (!Number.isSafeInteger(n) || n < 1) throw new RangeError(`not a valid version: ${n}`);
  return n;
}

async function toApiError(resp: Response, method: string, path: string): Promise<ApiError> {
  const text = await resp.text();
  let detail: unknown = text;
  try {
    const body: unknown = JSON.parse(text);
    detail = body !== null && typeof body === "object" && "detail" in body ? (body as { detail: unknown }).detail : body;
  } catch {
    // not JSON: keep the text
  }
  const Cls = errorFor(resp.status);
  return new Cls(resp.status, detail, method, path, resp.headers.get("x-request-id"));
}
