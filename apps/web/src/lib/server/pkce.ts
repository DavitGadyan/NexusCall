/** OAuth 2.0 PKCE (RFC 7636, S256 only) plus state / nonce generation. */
import { createHash, randomBytes } from "node:crypto";

export function createCodeVerifier(): string {
  // 32 random bytes -> 43 base64url characters (RFC 7636 allows 43..128 of [A-Za-z0-9-._~]).
  return randomBytes(32).toString("base64url");
}

export function codeChallengeS256(verifier: string): string {
  if (!/^[A-Za-z0-9\-._~]{43,128}$/.test(verifier)) throw new Error("Invalid PKCE code verifier");
  return createHash("sha256").update(verifier, "ascii").digest("base64url");
}

export function createState(): string {
  return randomBytes(32).toString("base64url");
}

export function createNonce(): string {
  return randomBytes(32).toString("base64url");
}
