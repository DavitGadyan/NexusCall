/** Session record storage: Redis in every running environment; in-memory only for unit tests. */
import Redis from "ioredis";

export interface SessionStore {
  get(key: string): Promise<string | null>;
  set(key: string, value: string, ttlMs: number): Promise<void>;
  del(key: string): Promise<void>;
  /** Returns a lock token when acquired, `null` when someone else holds the lock. */
  acquireLock(key: string, ttlMs: number): Promise<string | null>;
  releaseLock(key: string, token: string): Promise<void>;
}

const RELEASE_LUA = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end";

export class RedisSessionStore implements SessionStore {
  constructor(private readonly redis: Redis) {}

  async get(key: string) {
    return this.redis.get(key);
  }

  async set(key: string, value: string, ttlMs: number) {
    await this.redis.set(key, value, "PX", Math.max(1000, Math.floor(ttlMs)));
  }

  async del(key: string) {
    await this.redis.del(key);
  }

  async acquireLock(key: string, ttlMs: number) {
    const token = crypto.randomUUID();
    const ok = await this.redis.set(key, token, "PX", ttlMs, "NX");
    return ok === "OK" ? token : null;
  }

  async releaseLock(key: string, token: string) {
    await this.redis.eval(RELEASE_LUA, 1, key, token);
  }
}

export class MemorySessionStore implements SessionStore {
  private data = new Map<string, { value: string; expiresAt: number }>();

  constructor(private readonly now: () => number = () => Date.now()) {}

  private live(key: string) {
    const entry = this.data.get(key);
    if (!entry) return null;
    if (entry.expiresAt <= this.now()) {
      this.data.delete(key);
      return null;
    }
    return entry;
  }

  async get(key: string) {
    return this.live(key)?.value ?? null;
  }

  async set(key: string, value: string, ttlMs: number) {
    this.data.set(key, { value, expiresAt: this.now() + ttlMs });
  }

  async del(key: string) {
    this.data.delete(key);
  }

  async acquireLock(key: string, ttlMs: number) {
    if (this.live(key)) return null;
    const token = crypto.randomUUID();
    this.data.set(key, { value: token, expiresAt: this.now() + ttlMs });
    return token;
  }

  async releaseLock(key: string, token: string) {
    if (this.live(key)?.value === token) this.data.delete(key);
  }

  keys(): string[] {
    return [...this.data.keys()];
  }

  raw(key: string): string | undefined {
    return this.data.get(key)?.value;
  }
}

let shared: RedisSessionStore | null = null;

export function redisStore(url: string): SessionStore {
  if (!shared) {
    const client = new Redis(url, {
      lazyConnect: false,
      maxRetriesPerRequest: 2,
      enableOfflineQueue: true,
      connectTimeout: 3000,
    });
    client.on("error", () => {
      // Connection errors surface on the failing command; never log URLs (they contain the password).
    });
    shared = new RedisSessionStore(client);
  }
  return shared;
}
