/**
 * BFF sessions (ADR-0006). The browser holds only an opaque 256-bit id. Tokens live in Redis, sealed
 * with AES-256-GCM, under the SHA-256 of the id (a Redis dump does not yield usable cookies).
 * Idle and absolute timeouts are enforced on every load.
 */
import { randomId, seal, SealError, sha256Hex, unseal } from "./crypto";
import type { SessionStore } from "./session-store";

export type SessionData = {
  v: 1;
  sub: string;
  email: string | null;
  name: string | null;
  accessToken: string;
  /** epoch ms */
  accessExpiresAt: number;
  refreshToken: string | null;
  /** epoch ms, null when the IdP did not say */
  refreshExpiresAt: number | null;
  idToken: string;
  acr: string | null;
  /** epoch seconds of the last interactive authentication */
  authTime: number | null;
  csrf: string;
  tenantId: string | null;
  /** epoch ms of the last tenant validation against /api/v1/me */
  tenantCheckedAt: number;
  createdAt: number;
  lastSeenAt: number;
};

export type NewSession = Omit<SessionData, "v" | "csrf" | "createdAt" | "lastSeenAt" | "tenantCheckedAt"> & {
  tenantCheckedAt?: number;
};

export type SessionTimeouts = { idleMs: number; absoluteMs: number };

const SID_PATTERN = /^[A-Za-z0-9_-]{43}$/;
const TOUCH_INTERVAL_MS = 60_000;

export function isValidSessionId(sid: unknown): sid is string {
  return typeof sid === "string" && SID_PATTERN.test(sid);
}

export class SessionManager {
  constructor(
    private readonly store: SessionStore,
    private readonly key: Buffer,
    private readonly timeouts: SessionTimeouts,
    private readonly now: () => number = () => Date.now(),
  ) {}

  storageKey(sid: string): string {
    return `nx:sess:${sha256Hex(sid)}`;
  }

  lockKey(sid: string): string {
    return `nx:lock:refresh:${sha256Hex(sid)}`;
  }

  get lockStore(): SessionStore {
    return this.store;
  }

  /** Remaining lifetime: the earlier of the idle deadline and the absolute deadline. */
  ttlMs(session: SessionData): number {
    const t = this.now();
    return Math.max(0, Math.min(session.lastSeenAt + this.timeouts.idleMs, session.createdAt + this.timeouts.absoluteMs) - t);
  }

  absoluteRemainingSeconds(session: SessionData): number {
    return Math.max(0, Math.floor((session.createdAt + this.timeouts.absoluteMs - this.now()) / 1000));
  }

  async create(input: NewSession): Promise<{ sid: string; session: SessionData }> {
    const t = this.now();
    const sid = randomId(32);
    const session: SessionData = {
      ...input,
      v: 1,
      csrf: randomId(32),
      createdAt: t,
      lastSeenAt: t,
      tenantCheckedAt: input.tenantCheckedAt ?? t,
    };
    await this.save(sid, session);
    return { sid, session };
  }

  async save(sid: string, session: SessionData): Promise<void> {
    const ttl = this.ttlMs(session);
    if (ttl <= 0) {
      await this.destroy(sid);
      return;
    }
    const key = this.storageKey(sid);
    await this.store.set(key, seal(this.key, JSON.stringify(session), key), ttl);
  }

  /** Reads without touching (used while waiting for another request's token refresh). */
  async peek(sid: string): Promise<SessionData | null> {
    if (!isValidSessionId(sid)) return null;
    const key = this.storageKey(sid);
    const sealed = await this.store.get(key);
    if (!sealed) return null;
    try {
      const data = JSON.parse(unseal(this.key, sealed, key)) as SessionData;
      return data.v === 1 ? data : null;
    } catch (err) {
      if (err instanceof SealError || err instanceof SyntaxError) {
        await this.store.del(key);
        return null;
      }
      throw err;
    }
  }

  async load(sid: string): Promise<SessionData | null> {
    const session = await this.peek(sid);
    if (!session) return null;
    const t = this.now();
    if (t - session.createdAt >= this.timeouts.absoluteMs || t - session.lastSeenAt >= this.timeouts.idleMs) {
      await this.destroy(sid);
      return null;
    }
    if (t - session.lastSeenAt >= TOUCH_INTERVAL_MS) {
      session.lastSeenAt = t;
      await this.save(sid, session);
    }
    return session;
  }

  async destroy(sid: string): Promise<void> {
    if (!isValidSessionId(sid)) return;
    await this.store.del(this.storageKey(sid));
  }
}
