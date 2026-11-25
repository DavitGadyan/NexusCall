/** Request-time wiring of config, session store and token refresh (server only). */
import { cookies } from "next/headers";
import { cookieNames } from "./cookies";
import { config } from "./env";
import { refresher } from "./oidc";
import { ensureFreshSession } from "./refresh";
import { type SessionData, SessionManager } from "./session";
import { redisStore } from "./session-store";

let manager: SessionManager | null = null;

export function sessions(): SessionManager {
  if (!manager) {
    const cfg = config();
    manager = new SessionManager(redisStore(cfg.redisUrl), cfg.sessionKey, {
      idleMs: cfg.idleTimeoutMs,
      absoluteMs: cfg.absoluteTimeoutMs,
    });
  }
  return manager;
}

export async function readSessionId(): Promise<string | null> {
  const jar = await cookies();
  return jar.get(cookieNames(config().env).session)?.value ?? null;
}

export async function readCsrfCookie(): Promise<string | null> {
  const jar = await cookies();
  return jar.get(cookieNames(config().env).csrf)?.value ?? null;
}

export type ActiveSession = { sid: string; session: SessionData };

/** Loads the session (timeouts enforced) and makes sure its access token is usable. */
export async function activeSession(): Promise<ActiveSession | null> {
  const sid = await readSessionId();
  if (!sid) return null;
  const mgr = sessions();
  const loaded = await mgr.load(sid);
  if (!loaded) return null;
  const fresh = await ensureFreshSession(mgr, sid, loaded, refresher(config(), loaded.sub));
  return fresh ? { sid, session: fresh } : null;
}
