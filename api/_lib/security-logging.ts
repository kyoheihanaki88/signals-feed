/**
 * Fixed allowlist for runtime-init failure classification. The value is chosen by
 * `instanceof` checks in `vercel-runtime.ts` — never derived from error text.
 */
export type RuntimeInitFailureClassification =
  | "runtime_config"
  | "vercel_runtime"
  | "runtime_composition"
  | "apple_root_certificate"
  | "unknown";

export type SecurityLogEvent = {
  route: "/api/auth/exchange" | "/api/edition";
  status: number;
  reasonCode: string;
  latencyMs: number;
  requestId: string;
  selectorVersion?: number;
  environment?: "Production" | "Sandbox";
  rateLimitBucket?: "ip_exchange" | "subject_exchange" | "token_edition";
  /**
   * Present ONLY on `reasonCode: "runtime_init_failed"` events. `codes` carries the
   * mechanical issue/reason codes of the KNOWN error classes, whose constructors
   * guarantee they name a variable and a constraint — never a value, a key or an
   * identifier. Unknown errors log the classification alone.
   */
  initFailure?: {
    classification: RuntimeInitFailureClassification;
    codes?: readonly string[];
  };
};

export interface SecurityLogger {
  log(event: SecurityLogEvent): void;
}

export class JsonSecurityLogger implements SecurityLogger {
  constructor(
    private readonly write: (line: string) => void = (line) =>
      console.info(line),
  ) {}

  log(event: SecurityLogEvent): void {
    this.write(JSON.stringify(event));
  }
}

export class MemorySecurityLogger implements SecurityLogger {
  readonly events: SecurityLogEvent[] = [];

  log(event: SecurityLogEvent): void {
    this.events.push(structuredClone(event));
  }
}
