import { describe, expect, it, vi } from "vitest";
import { parseSessionKey } from "./crypto";
import { ensureFreshSession, RefreshRejected, type TokenSet } from "./refresh";
import { isValidSessionId, type NewSession, SessionManager } from "./session";
import { MemorySessionStore } from "./session-store";

const KEY = parseSessionKey("ab".repeat(32));
const MIN = 60_000;

function setup(start = 1_700_000_000_000) {
  let t = start;
  const clock = { now: () => t, advance: (ms: number) => (t += ms) };
  const store = new MemorySessionStore(clock.now);
  const manager = new SessionManager(store, KEY, { idleMs: 30 * MIN, absoluteMs: 10 * 60 * MIN }, clock.now);
  const input: NewSession = {
    sub: "00000000-0000-4000-8000-000000000101",
    email: "owner@acme.test",
    name: "Ada",
    accessToken: "access-1",
    accessExpiresAt: t + 5 * MIN,
    refreshToken: "refresh-1",
    refreshExpiresAt: t + 30 * MIN,
    idToken: "id-1",
    acr: "aal1",
    authTime: Math.floor(t / 1000),
    tenantId: null,
  };
  return { clock, store, manager, input };
}

describe("SessionManager", () => {
  it("issues an opaque 256-bit id and stores only ciphertext under a hashed key", async () => {
    const { manager, store, input } = setup();
    const { sid, session } = await manager.create(input);
    expect(isValidSessionId(sid)).toBe(true);
    expect(session.csrf).toMatch(/^[A-Za-z0-9_-]{43}$/);
    const keys = store.keys();
    expect(keys).toHaveLength(1);
    expect(keys[0]).not.toContain(sid);
    const raw = store.raw(keys[0] ?? "") ?? "";
    expect(raw).not.toContain("refresh-1");
    expect(raw).not.toContain("access-1");
    expect((await manager.load(sid))?.accessToken).toBe("access-1");
  });

  it("rejects malformed and unknown ids", async () => {
    const { manager } = setup();
    expect(await manager.load("nope")).toBeNull();
    expect(await manager.load("a".repeat(43))).toBeNull();
  });

  it("enforces the idle timeout", async () => {
    const { manager, input, clock } = setup();
    const { sid } = await manager.create(input);
    clock.advance(29 * MIN);
    expect(await manager.load(sid)).not.toBeNull(); // touch extends idle deadline
    clock.advance(29 * MIN);
    expect(await manager.load(sid)).not.toBeNull();
    clock.advance(31 * MIN);
    expect(await manager.load(sid)).toBeNull();
  });

  it("enforces the absolute timeout even when active", async () => {
    const { manager, input, clock } = setup();
    const { sid } = await manager.create(input);
    for (let i = 0; i < 24; i++) {
      clock.advance(25 * MIN);
      if (i < 23) expect(await manager.load(sid)).not.toBeNull();
    }
    expect(await manager.load(sid)).toBeNull();
  });

  it("destroy removes the record", async () => {
    const { manager, input } = setup();
    const { sid } = await manager.create(input);
    await manager.destroy(sid);
    expect(await manager.load(sid)).toBeNull();
  });
});

describe("refresh single-flight", () => {
  const tokens = (n: number): TokenSet => ({ accessToken: `access-${n}`, expiresIn: 300, refreshToken: `refresh-${n}` });

  it("does nothing while the access token is fresh", async () => {
    const { manager, input } = setup();
    const { sid, session } = await manager.create(input);
    const refresh = vi.fn();
    expect(await ensureFreshSession(manager, sid, session, refresh, { now: () => input.accessExpiresAt - 5 * MIN })).toBe(session);
    expect(refresh).not.toHaveBeenCalled();
  });

  it("calls the token endpoint once for concurrent requests and rotates the refresh token", async () => {
    const { manager, input, clock } = setup();
    const { sid } = await manager.create(input);
    clock.advance(4 * MIN + 45_000);
    const session = (await manager.load(sid))!;
    let n = 1;
    const refresh = vi.fn(async (rt: string) => {
      expect(rt).toBe("refresh-1");
      await new Promise((r) => setTimeout(r, 5));
      return tokens(++n);
    });
    const results = await Promise.all(
      Array.from({ length: 5 }, () => ensureFreshSession(manager, sid, session, refresh, { now: clock.now })),
    );
    expect(refresh).toHaveBeenCalledTimes(1);
    expect(results.every((r) => r?.accessToken === "access-2")).toBe(true);
    expect((await manager.load(sid))?.refreshToken).toBe("refresh-2");
  });

  it("waits for another replica holding the lock instead of refreshing again", async () => {
    const { manager, input, clock, store } = setup();
    const { sid } = await manager.create(input);
    clock.advance(5 * MIN);
    const stale = (await manager.peek(sid))!;
    const otherReplica = await store.acquireLock(manager.lockKey(sid), 15_000);
    expect(otherReplica).not.toBeNull();
    const refresh = vi.fn(async () => tokens(9));
    let polls = 0;
    const sleep = async () => {
      polls++;
      if (polls === 2) await manager.save(sid, { ...stale, accessToken: "from-other", accessExpiresAt: clock.now() + 300_000 });
    };
    const result = await ensureFreshSession(manager, sid, stale, refresh, { now: clock.now, sleep });
    expect(refresh).not.toHaveBeenCalled();
    expect(result?.accessToken).toBe("from-other");
  });

  it("destroys the session when the IdP rejects the refresh token", async () => {
    const { manager, input, clock } = setup();
    const { sid, session } = await manager.create(input);
    clock.advance(5 * MIN);
    const result = await ensureFreshSession(manager, sid, session, async () => {
      throw new RefreshRejected();
    }, { now: clock.now });
    expect(result).toBeNull();
    expect(await manager.peek(sid)).toBeNull();
  });

  it("releases the lock after a failure", async () => {
    const { manager, input, clock, store } = setup();
    const { sid, session } = await manager.create(input);
    clock.advance(6 * MIN);
    await expect(
      ensureFreshSession(manager, sid, session, async () => {
        throw new Error("network");
      }, { now: clock.now }),
    ).rejects.toThrow("network");
    expect(await store.acquireLock(manager.lockKey(sid), 1000)).not.toBeNull();
  });
});
