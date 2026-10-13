export type UsageLimit = { kind: string; percentUsed: number; resetsAt?: string }

export type UsageSnapshot = {
  /** The rate-limit windows last read, as the session reports them. */
  limits: UsageLimit[]
  /** The clock's time (ms since the epoch) when they were read; 0 before the first read. */
  nowMs: number
}

declare module 'claude-code' {
  interface PluginState {
    'toolbelt-usage': { snapshot: UsageSnapshot }
  }
}
