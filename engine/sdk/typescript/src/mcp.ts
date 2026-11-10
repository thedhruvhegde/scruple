/**
 * Minimal MCP client over stdio, enough to drive the scruple dispute simulator
 * (`scruple mcp sim`) from TypeScript without an MCP SDK dependency.
 *
 *   const sim = await McpStdioClient.spawn("scruple", ["mcp", "sim"]);
 *   const { episode_id } = await sim.callTool("start_dispute_episode", { seed: 7 });
 *   ...
 *   await sim.close();
 *
 * Messages are newline-delimited JSON-RPC 2.0, as the MCP stdio transport defines.
 * Requests are answered in any order; each is matched to its response by id.
 */

import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { createInterface } from "node:readline";

import { ScrupleError } from "./errors.js";

export const PROTOCOL_VERSION = "2025-06-18";

/** A tool call the server answered with `isError: true`, or a JSON-RPC error. */
export class McpToolError extends ScrupleError {
  override name = "McpToolError";
  constructor(
    readonly tool: string,
    message: string,
    readonly code?: number,
  ) {
    super(`${tool}: ${message}`);
  }
}

interface Pending {
  method: string;
  resolve: (value: unknown) => void;
  reject: (err: Error) => void;
}

interface RpcMessage {
  jsonrpc: "2.0";
  id?: number | string | null;
  method?: string;
  result?: unknown;
  error?: { code: number; message: string; data?: unknown };
}

export interface ToolInfo {
  name: string;
  description?: string;
  inputSchema: Record<string, unknown>;
}

interface CallToolResult {
  content?: { type: string; text?: string }[];
  structuredContent?: Record<string, unknown>;
  isError?: boolean;
}

export interface ServerInfo {
  protocolVersion: string;
  serverInfo: { name: string; version: string };
  instructions?: string;
}

export class McpStdioClient {
  private nextId = 1;
  private readonly pending = new Map<number, Pending>();
  private closed = false;
  private stderr = "";
  server: ServerInfo | undefined;

  private constructor(private readonly child: ChildProcessWithoutNullStreams) {
    createInterface({ input: child.stdout }).on("line", (line) => this.onLine(line));
    child.stderr.on("data", (chunk: Buffer) => {
      this.stderr = (this.stderr + chunk.toString()).slice(-4000);
    });
    child.on("exit", (code, signal) => {
      this.closed = true;
      const why = new ScrupleError(
        `MCP server exited (code ${code}, signal ${signal}); stderr tail:\n${this.stderr}`,
      );
      for (const p of this.pending.values()) p.reject(why);
      this.pending.clear();
    });
  }

  /** Start `command args...` and complete the MCP initialize handshake. */
  static async spawn(
    command: string,
    args: string[] = [],
    options: { env?: NodeJS.ProcessEnv; cwd?: string } = {},
  ): Promise<McpStdioClient> {
    const child = spawn(command, args, {
      stdio: ["pipe", "pipe", "pipe"],
      env: options.env ?? process.env,
      ...(options.cwd ? { cwd: options.cwd } : {}),
    });
    const client = new McpStdioClient(child);
    await new Promise<void>((resolve, reject) => {
      child.once("spawn", () => resolve());
      child.once("error", reject);
    });
    client.server = (await client.rpc("initialize", {
      protocolVersion: PROTOCOL_VERSION,
      capabilities: {},
      clientInfo: { name: "scruple-typescript", version: "0" },
    })) as ServerInfo;
    client.notify("notifications/initialized");
    return client;
  }

  private onLine(line: string): void {
    if (!line.trim()) return;
    let msg: RpcMessage;
    try {
      msg = JSON.parse(line) as RpcMessage;
    } catch {
      return; // not protocol output
    }
    if (typeof msg.id !== "number" || msg.method !== undefined) return; // server requests/notifications
    const p = this.pending.get(msg.id);
    if (!p) return;
    this.pending.delete(msg.id);
    if (msg.error) p.reject(new McpToolError(p.method, msg.error.message, msg.error.code));
    else p.resolve(msg.result);
  }

  private send(msg: RpcMessage & { params?: unknown }): void {
    if (this.closed) throw new ScrupleError("MCP client is closed");
    this.child.stdin.write(JSON.stringify(msg) + "\n");
  }

  notify(method: string, params?: unknown): void {
    this.send({ jsonrpc: "2.0", method, ...(params === undefined ? {} : { params }) });
  }

  rpc(method: string, params?: unknown): Promise<unknown> {
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { method, resolve, reject });
      try {
        this.send({ jsonrpc: "2.0", id, method, ...(params === undefined ? {} : { params }) });
      } catch (err) {
        this.pending.delete(id);
        reject(err as Error);
      }
    });
  }

  async listTools(): Promise<ToolInfo[]> {
    const res = (await this.rpc("tools/list", {})) as { tools: ToolInfo[] };
    return res.tools;
  }

  /** Call a tool; returns its structured result, or throws McpToolError. */
  async callTool<T = Record<string, unknown>>(name: string, args: Record<string, unknown> = {}): Promise<T> {
    const res = (await this.rpc("tools/call", { name, arguments: args })) as CallToolResult;
    const text = (res.content ?? []).map((c) => c.text ?? "").join("\n");
    if (res.isError) throw new McpToolError(name, text || "tool failed");
    if (res.structuredContent !== undefined) {
      // Tools returning a non-object are wrapped as { result: ... } by the server.
      return res.structuredContent as T;
    }
    return JSON.parse(text) as T;
  }

  async close(): Promise<void> {
    if (this.closed) return;
    const exited = new Promise<void>((resolve) => this.child.once("exit", () => resolve()));
    this.child.stdin.end();
    const timer = setTimeout(() => this.child.kill("SIGTERM"), 5000);
    await exited;
    clearTimeout(timer);
  }
}
