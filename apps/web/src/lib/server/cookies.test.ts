import { describe, expect, it } from "vitest";
import { authTxCookieOptions, cookieNames, csrfCookieOptions, sessionCookieOptions } from "./cookies";

describe("cookie flags per environment", () => {
  it.each(["production", "staging"] as const)("%s uses __Host- cookies with Secure", (env) => {
    expect(cookieNames(env)).toEqual({ session: "__Host-nx_sid", csrf: "__Host-nx_csrf", authTx: "__Host-nx_auth" });
    const s = sessionCookieOptions(env, 3600);
    expect(s).toEqual({ httpOnly: true, secure: true, sameSite: "lax", path: "/", maxAge: 3600 });
    expect(csrfCookieOptions(env, 3600).secure).toBe(true);
    expect(authTxCookieOptions(env).secure).toBe(true);
  });

  it.each(["development", "test"] as const)("%s uses nx_sid without Secure", (env) => {
    expect(cookieNames(env).session).toBe("nx_sid");
    const s = sessionCookieOptions(env, 60);
    expect(s.secure).toBe(false);
    expect(s.httpOnly).toBe(true);
    expect(s.sameSite).toBe("lax");
  });

  it("the session cookie is HttpOnly and the CSRF cookie is readable", () => {
    expect(sessionCookieOptions("production", 1).httpOnly).toBe(true);
    expect(csrfCookieOptions("production", 1).httpOnly).toBe(false);
    expect(csrfCookieOptions("production", 1).sameSite).toBe("strict");
  });

  it("the login transaction cookie is short-lived and HttpOnly", () => {
    const tx = authTxCookieOptions("production");
    expect(tx.httpOnly).toBe(true);
    expect(tx.maxAge).toBeLessThanOrEqual(600);
    expect(tx.sameSite).toBe("lax");
  });

  it("never sets a Domain attribute (host-only cookies)", () => {
    expect(Object.keys(sessionCookieOptions("production", 1))).not.toContain("domain");
  });
});
