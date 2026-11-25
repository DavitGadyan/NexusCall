import { describe, expect, it } from "vitest";
import { parseSessionKey, randomId, safeEqual, seal, SealError, sha256Hex, unseal } from "./crypto";

const KEY = parseSessionKey("00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff");
const OTHER = parseSessionKey("ffeeddccbbaa99887766554433221100ffeeddccbbaa99887766554433221100");

describe("session crypto (AES-256-GCM)", () => {
  it("round-trips", () => {
    const sealed = seal(KEY, JSON.stringify({ sub: "u1", refresh: "r" }), "session:abc");
    expect(JSON.parse(unseal(KEY, sealed, "session:abc"))).toEqual({ sub: "u1", refresh: "r" });
  });

  it("never contains the plaintext and uses a fresh IV each time", () => {
    const a = seal(KEY, "refresh-token-value", "aad");
    const b = seal(KEY, "refresh-token-value", "aad");
    expect(a).not.toBe(b);
    expect(Buffer.from(a, "base64url").toString("utf8")).not.toContain("refresh-token-value");
  });

  it("rejects a wrong key", () => {
    const sealed = seal(KEY, "x", "aad");
    expect(() => unseal(OTHER, sealed, "aad")).toThrow(SealError);
  });

  it("binds ciphertext to its associated data (no swapping between keys or purposes)", () => {
    const sealed = seal(KEY, "x", "session:one");
    expect(() => unseal(KEY, sealed, "session:two")).toThrow(SealError);
    expect(() => unseal(KEY, sealed, "auth-tx")).toThrow(SealError);
  });

  it("detects tampering with any byte", () => {
    const sealed = seal(KEY, "payload", "aad");
    const raw = Buffer.from(sealed, "base64url");
    for (const index of [0, 5, 20, raw.length - 1]) {
      const copy = Buffer.from(raw);
      copy[index] = (copy[index] ?? 0) ^ 0x01;
      expect(() => unseal(KEY, copy.toString("base64url"), "aad")).toThrow(SealError);
    }
    expect(() => unseal(KEY, "", "aad")).toThrow(SealError);
    expect(() => unseal(KEY, "not-base64!!", "aad")).toThrow(SealError);
  });

  it("requires a 64-hex-character key", () => {
    expect(() => parseSessionKey(undefined)).toThrow();
    expect(() => parseSessionKey("abcd")).toThrow();
    expect(() => parseSessionKey("z".repeat(64))).toThrow();
    expect(parseSessionKey("A".repeat(64))).toHaveLength(32);
  });
});

describe("helpers", () => {
  it("randomId has 256 bits by default", () => {
    expect(randomId()).toMatch(/^[A-Za-z0-9_-]{43}$/);
  });
  it("sha256Hex", () => {
    expect(sha256Hex("abc")).toBe("ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
  });
  it("safeEqual", () => {
    expect(safeEqual("abc", "abc")).toBe(true);
    expect(safeEqual("abc", "abd")).toBe(false);
    expect(safeEqual("abc", "abcd")).toBe(false);
    expect(safeEqual("", "")).toBe(false);
    expect(safeEqual(null, "a")).toBe(false);
  });
});
