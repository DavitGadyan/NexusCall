/**
 * AES-256-GCM sealing for BFF session records and the short-lived login transaction (ADR-0006).
 *
 * Format: base64url( version(1) | iv(12) | tag(16) | ciphertext ). The caller supplies associated
 * data (AAD) that binds a ciphertext to its purpose and storage key, so a record copied to another
 * key, or a login-transaction blob replayed as a session, fails authentication.
 */
import { createCipheriv, createDecipheriv, createHash, randomBytes, timingSafeEqual } from "node:crypto";

const VERSION = 1;
const IV_BYTES = 12;
const TAG_BYTES = 16;

export class SealError extends Error {
  constructor() {
    super("Sealed data could not be authenticated");
  }
}

export function parseSessionKey(hex: string | undefined): Buffer {
  if (!hex || !/^[0-9a-fA-F]{64}$/.test(hex)) {
    throw new Error("NEXUS_SESSION_KEY must be exactly 64 hex characters (32 bytes)");
  }
  return Buffer.from(hex, "hex");
}

export function seal(key: Buffer, plaintext: string, aad: string): string {
  if (key.length !== 32) throw new Error("AES-256-GCM requires a 32-byte key");
  const iv = randomBytes(IV_BYTES);
  const cipher = createCipheriv("aes-256-gcm", key, iv, { authTagLength: TAG_BYTES });
  cipher.setAAD(Buffer.from(aad, "utf8"));
  const ct = Buffer.concat([cipher.update(plaintext, "utf8"), cipher.final()]);
  return Buffer.concat([Buffer.from([VERSION]), iv, cipher.getAuthTag(), ct]).toString("base64url");
}

export function unseal(key: Buffer, sealed: string, aad: string): string {
  let raw: Buffer;
  try {
    raw = Buffer.from(sealed, "base64url");
  } catch {
    throw new SealError();
  }
  if (raw.length < 1 + IV_BYTES + TAG_BYTES || raw[0] !== VERSION) throw new SealError();
  const iv = raw.subarray(1, 1 + IV_BYTES);
  const tag = raw.subarray(1 + IV_BYTES, 1 + IV_BYTES + TAG_BYTES);
  const ct = raw.subarray(1 + IV_BYTES + TAG_BYTES);
  try {
    const decipher = createDecipheriv("aes-256-gcm", key, iv, { authTagLength: TAG_BYTES });
    decipher.setAAD(Buffer.from(aad, "utf8"));
    decipher.setAuthTag(tag);
    return Buffer.concat([decipher.update(ct), decipher.final()]).toString("utf8");
  } catch {
    throw new SealError();
  }
}

/** 256-bit random identifier, base64url (43 chars). */
export function randomId(bytes = 32): string {
  return randomBytes(bytes).toString("base64url");
}

export function sha256Hex(value: string): string {
  return createHash("sha256").update(value, "utf8").digest("hex");
}

/** Constant-time string comparison (length leak only). */
export function safeEqual(a: string | null | undefined, b: string | null | undefined): boolean {
  if (typeof a !== "string" || typeof b !== "string" || a.length === 0) return false;
  const ab = Buffer.from(a, "utf8");
  const bb = Buffer.from(b, "utf8");
  if (ab.length !== bb.length) return false;
  return timingSafeEqual(ab, bb);
}
