export type SecurityLogEvent = {
  route: "/api/auth/exchange" | "/api/edition";
  status: number;
  reasonCode: string;
  latencyMs: number;
  requestId: string;
  selectorVersion?: number;
  environment?: "Production" | "Sandbox";
  rateLimitBucket?: "ip_exchange" | "subject_exchange" | "token_edition";
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
