import { describe, expect, it } from "vitest";
import { openAuthTx, sealAuthTx, type AuthTransaction } from "./auth-tx";
import { parseSessionKey, seal } from "./crypto";

const KEY = parseSessionKey("cd".repeat(32));
const NOW = 1_700_000_000_000;
const tx: AuthTransaction = { state: "state-abc", nonce: "n", verifier: "v", returnTo: "/app", stepUp: false, createdAt: NOW };

describe("login transaction", () => {
  it("opens with the matching state", () => {
    expect(openAuthTx(KEY, sealAuthTx(KEY, tx), "state-abc", NOW + 1000)).toEqual(tx);
  });

  it("rejects a different state (CSRF on the callback)", () => {
    expect(openAuthTx(KEY, sealAuthTx(KEY, tx), "state-abd", NOW)).toBeNull();
    expect(openAuthTx(KEY, sealAuthTx(KEY, tx), null, NOW)).toBeNull();
  });

  it("rejects a missing cookie, an expired transaction and foreign ciphertext", () => {
    expect(openAuthTx(KEY, undefined, "state-abc", NOW)).toBeNull();
    expect(openAuthTx(KEY, sealAuthTx(KEY, tx), "state-abc", NOW + 11 * 60_000)).toBeNull();
    expect(openAuthTx(KEY, seal(KEY, JSON.stringify(tx), "nx:sess:other"), "state-abc", NOW)).toBeNull();
  });
});
