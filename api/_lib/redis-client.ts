/**
 * Minimal Upstash Redis REST client. (Phase 3B-3)
 *
 * Deliberately dependency-free: Upstash's REST endpoint is plain HTTPS + bearer token,
 * so `fetch` is sufficient. That keeps the API surface auditable, avoids adding a
 * third-party package to a security path, and works unchanged on Vercel Node runtimes.
 *
 * Every operation FAILS CLOSED: any transport error, non-2xx status, or malformed body
 * raises `RedisUnavailableError`. Callers must treat that as "deny", never as "allow".
 */

export class RedisUnavailableError extends Error {
  constructor(reason: string) {
    // Reason is a short mechanical description — never a URL, token, key or value.
    super(`redis unavailable: ${reason}`);
    this.name = "RedisUnavailableError";
  }
}

export type RedisCommand = (string | number)[];

export interface RedisClient {
  /** Execute one Redis command, returning its raw result. */
  command<T = unknown>(command: RedisCommand): Promise<T>;
  /** Execute commands as one pipeline, returning results in order. */
  pipeline<T = unknown>(commands: RedisCommand[]): Promise<T[]>;
}

export type FetchLike = (
  input: string,
  init?: {
    method?: string;
    headers?: Record<string, string>;
    body?: string;
    signal?: AbortSignal;
  },
) => Promise<{ ok: boolean; status: number; text(): Promise<string> }>;

export type UpstashRestClientOptions = {
  restUrl: string;
  restToken: string;
  fetchImpl?: FetchLike;
  timeoutMs?: number;
};

type UpstashResult = { result?: unknown; error?: string };

export class UpstashRestClient implements RedisClient {
  private readonly restUrl: string;
  private readonly restToken: string;
  private readonly fetchImpl: FetchLike;
  private readonly timeoutMs: number;

  constructor(options: UpstashRestClientOptions) {
    this.restUrl = options.restUrl.replace(/\/+$/, "");
    this.restToken = options.restToken;
    this.fetchImpl = options.fetchImpl ?? (globalThis.fetch as unknown as FetchLike);
    this.timeoutMs = options.timeoutMs ?? 2_000;
    if (!this.restUrl || !this.restToken) {
      throw new RedisUnavailableError("missing endpoint configuration");
    }
  }

  async command<T = unknown>(command: RedisCommand): Promise<T> {
    const [parsed] = await this.post<T>(JSON.stringify(command), true);
    return parsed;
  }

  async pipeline<T = unknown>(commands: RedisCommand[]): Promise<T[]> {
    if (commands.length === 0) return [];
    return this.post<T>(JSON.stringify(commands), false, "/pipeline");
  }

  private async post<T>(
    body: string,
    single: boolean,
    path = "",
  ): Promise<T[]> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);
    let raw: string;
    try {
      const response = await this.fetchImpl(`${this.restUrl}${path}`, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${this.restToken}`,
          "Content-Type": "application/json",
        },
        body,
        signal: controller.signal,
      });
      if (!response.ok) {
        throw new RedisUnavailableError(`http ${response.status}`);
      }
      raw = await response.text();
    } catch (error) {
      if (error instanceof RedisUnavailableError) throw error;
      throw new RedisUnavailableError(
        error instanceof Error && error.name === "AbortError" ? "timeout" : "transport error",
      );
    } finally {
      clearTimeout(timer);
    }

    let parsed: unknown;
    try {
      parsed = JSON.parse(raw);
    } catch {
      throw new RedisUnavailableError("malformed response");
    }

    const entries: UpstashResult[] = single
      ? [parsed as UpstashResult]
      : Array.isArray(parsed)
        ? (parsed as UpstashResult[])
        : (() => {
            throw new RedisUnavailableError("malformed pipeline response");
          })();

    return entries.map((entry) => {
      if (entry && typeof entry === "object" && "error" in entry && entry.error) {
        throw new RedisUnavailableError("command error");
      }
      return (entry?.result ?? null) as T;
    });
  }
}

/**
 * Deterministic in-memory client for TESTS ONLY. Never selected by Production or
 * Sandbox configuration — those construct `UpstashRestClient` from the env contract.
 */
export class FakeRedisClient implements RedisClient {
  private readonly values = new Map<string, string>();
  private readonly expiries = new Map<string, number>();
  /** Set to make the next N operations fail, simulating an outage. */
  failNextOperations = 0;
  nowMs = 0;

  private ensureAvailable(): void {
    if (this.failNextOperations > 0) {
      this.failNextOperations -= 1;
      throw new RedisUnavailableError("simulated outage");
    }
  }

  private expireIfNeeded(key: string): void {
    const expiry = this.expiries.get(key);
    if (expiry !== undefined && expiry <= this.nowMs) {
      this.values.delete(key);
      this.expiries.delete(key);
    }
  }

  async command<T = unknown>(command: RedisCommand): Promise<T> {
    const [result] = await this.pipeline<T>([command]);
    return result;
  }

  async pipeline<T = unknown>(commands: RedisCommand[]): Promise<T[]> {
    this.ensureAvailable();
    return commands.map((command) => this.run(command) as T);
  }

  private run(command: RedisCommand): unknown {
    const [verb, ...args] = command.map(String);
    const key = args[0];
    if (key) this.expireIfNeeded(key);

    switch (verb.toUpperCase()) {
      case "INCR": {
        const next = Number.parseInt(this.values.get(key) ?? "0", 10) + 1;
        this.values.set(key, String(next));
        return next;
      }
      case "EXPIRE": {
        if (!this.values.has(key)) return 0;
        this.expiries.set(key, this.nowMs + Number.parseInt(args[1], 10) * 1_000);
        return 1;
      }
      case "PTTL": {
        const expiry = this.expiries.get(key);
        if (!this.values.has(key)) return -2;
        return expiry === undefined ? -1 : Math.max(0, expiry - this.nowMs);
      }
      case "GET":
        return this.values.get(key) ?? null;
      case "SET": {
        const nxIndex = args.findIndex((a) => a.toUpperCase() === "NX");
        if (nxIndex >= 0 && this.values.has(key)) return null;
        this.values.set(key, args[1]);
        const exIndex = args.findIndex((a) => a.toUpperCase() === "EX");
        if (exIndex >= 0) {
          this.expiries.set(key, this.nowMs + Number.parseInt(args[exIndex + 1], 10) * 1_000);
        }
        return "OK";
      }
      case "DEL": {
        const existed = this.values.delete(key);
        this.expiries.delete(key);
        return existed ? 1 : 0;
      }
      default:
        throw new RedisUnavailableError("unsupported command");
    }
  }

  /** Test helper: current stored value (undefined when absent/expired). */
  peek(key: string): string | undefined {
    this.expireIfNeeded(key);
    return this.values.get(key);
  }

  /** Test helper: every live key. */
  keys(): string[] {
    for (const key of [...this.values.keys()]) this.expireIfNeeded(key);
    return [...this.values.keys()];
  }
}
