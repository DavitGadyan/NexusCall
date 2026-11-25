/**
 * Refresh-token rotation behind a single-flight lock.
 *
 * Keycloak rotates refresh tokens with reuse detection, so two concurrent refreshes with the same token
 * would revoke the whole session. One request per session refreshes (in-process promise + Redis lock
 * across replicas); the others wait and re-read the updated record.
 */
import type { SessionData, SessionManager } from "./session";

export type TokenSet = {
  accessToken: string;
  expiresIn: number;
  refreshToken?: string | null;
  refreshExpiresIn?: number | null;
  idToken?: string | null;
  acr?: string | null;
  authTime?: number | null;
};

export class RefreshRejected extends Error {
  constructor() {
    super("The identity provider rejected the refresh token");
  }
}

export type RefreshFn = (refreshToken: string) => Promise<TokenSet>;

export type RefreshOptions = {
  /** refresh when the access token expires within this window */
  skewMs?: number;
  lockTtlMs?: number;
  waitTimeoutMs?: number;
  pollMs?: number;
  now?: () => number;
  sleep?: (ms: number) => Promise<void>;
};

const inflight = new Map<string, Promise<SessionData | null>>();

export function needsRefresh(session: SessionData, now: number, skewMs = 30_000): boolean {
  return session.accessExpiresAt - now <= skewMs;
}

export function applyTokenSet(session: SessionData, tokens: TokenSet, now: number): SessionData {
  return {
    ...session,
    accessToken: tokens.accessToken,
    accessExpiresAt: now + tokens.expiresIn * 1000,
    refreshToken: tokens.refreshToken ?? session.refreshToken,
    refreshExpiresAt: tokens.refreshExpiresIn ? now + tokens.refreshExpiresIn * 1000 : session.refreshExpiresAt,
    idToken: tokens.idToken ?? session.idToken,
    acr: tokens.acr ?? session.acr,
    authTime: tokens.authTime ?? session.authTime,
  };
}

/**
 * Returns a session whose access token is valid for at least `skewMs`, or `null` when the session can
 * no longer be refreshed (it is then destroyed).
 */
export async function ensureFreshSession(
  manager: SessionManager,
  sid: string,
  session: SessionData,
  refresh: RefreshFn,
  opts: RefreshOptions = {},
): Promise<SessionData | null> {
  const now = opts.now ?? (() => Date.now());
  const skew = opts.skewMs ?? 30_000;
  if (!needsRefresh(session, now(), skew)) return session;
  const running = inflight.get(sid);
  if (running) return running;
  const task = refreshUnderLock(manager, sid, refresh, { ...opts, now, skewMs: skew }).finally(() => inflight.delete(sid));
  inflight.set(sid, task);
  return task;
}

async function refreshUnderLock(
  manager: SessionManager,
  sid: string,
  refresh: RefreshFn,
  opts: RefreshOptions & { now: () => number; skewMs: number },
): Promise<SessionData | null> {
  const store = manager.lockStore;
  const lockKey = manager.lockKey(sid);
  const sleep = opts.sleep ?? ((ms: number) => new Promise<void>((r) => setTimeout(r, ms)));
  const token = await store.acquireLock(lockKey, opts.lockTtlMs ?? 15_000);
  if (token === null) {
    // Another replica is refreshing: wait for it to publish the new tokens.
    const deadline = opts.now() + (opts.waitTimeoutMs ?? 5_000);
    while (opts.now() < deadline) {
      await sleep(opts.pollMs ?? 150);
      const latest = await manager.peek(sid);
      if (!latest) return null;
      if (!needsRefresh(latest, opts.now(), opts.skewMs)) return latest;
    }
    const latest = await manager.peek(sid);
    return latest && latest.accessExpiresAt > opts.now() ? latest : null;
  }
  try {
    const current = await manager.peek(sid);
    if (!current) return null;
    if (!needsRefresh(current, opts.now(), opts.skewMs)) return current;
    if (!current.refreshToken || (current.refreshExpiresAt !== null && current.refreshExpiresAt <= opts.now())) {
      await manager.destroy(sid);
      return null;
    }
    let tokens: TokenSet;
    try {
      tokens = await refresh(current.refreshToken);
    } catch (err) {
      if (err instanceof RefreshRejected) {
        await manager.destroy(sid);
        return null;
      }
      // IdP unreachable: keep going with the current token while it is still valid.
      if (current.accessExpiresAt > opts.now()) return current;
      throw err;
    }
    const updated = applyTokenSet(current, tokens, opts.now());
    await manager.save(sid, updated);
    return updated;
  } finally {
    await store.releaseLock(lockKey, token);
  }
}
