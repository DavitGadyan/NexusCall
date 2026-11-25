/** The short-lived login transaction (state, nonce, PKCE verifier), sealed into an HttpOnly cookie. */
import { safeEqual, seal, unseal } from "./crypto";

export type AuthTransaction = {
  state: string;
  nonce: string;
  verifier: string;
  returnTo: string;
  stepUp: boolean;
  createdAt: number;
};

const AAD = "nexus:auth-tx:v1";
const MAX_AGE_MS = 10 * 60_000;

export function sealAuthTx(key: Buffer, tx: AuthTransaction): string {
  return seal(key, JSON.stringify(tx), AAD);
}

/** Returns the transaction only if it authenticates, is fresh and matches the returned `state`. */
export function openAuthTx(key: Buffer, sealed: string | undefined, state: string | null, now = Date.now()): AuthTransaction | null {
  if (!sealed || !state) return null;
  let tx: AuthTransaction;
  try {
    tx = JSON.parse(unseal(key, sealed, AAD)) as AuthTransaction;
  } catch {
    return null;
  }
  if (typeof tx.createdAt !== "number" || now - tx.createdAt > MAX_AGE_MS || now < tx.createdAt - 60_000) return null;
  if (!safeEqual(tx.state, state)) return null;
  return tx;
}
