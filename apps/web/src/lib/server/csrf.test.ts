import { describe, expect, it } from "vitest";
import { checkMutatingRequest, isMutating, isSameOriginRequest, verifyCsrfTokens } from "./csrf";

const ORIGIN = "https://app.example";
const T = "tok_0123456789abcdefghijklmnopqrstuvwxyz";

describe("CSRF double-submit", () => {
  it("accepts when header, cookie and session agree", () => {
    expect(verifyCsrfTokens({ headerToken: T, cookieToken: T, sessionToken: T })).toBe(true);
  });

  it.each([
    [{ headerToken: null, cookieToken: T, sessionToken: T }],
    [{ headerToken: T, cookieToken: null, sessionToken: T }],
    [{ headerToken: T, cookieToken: T, sessionToken: null }],
    [{ headerToken: "", cookieToken: "", sessionToken: "" }],
    [{ headerToken: T + "x", cookieToken: T + "x", sessionToken: T }],
    [{ headerToken: T, cookieToken: T + "x", sessionToken: T }],
  ])("rejects %j", (input) => {
    expect(verifyCsrfTokens(input)).toBe(false);
  });

  it("binds the token to the session: an attacker's own valid pair is useless", () => {
    const attacker = "attacker_token_value_000000000000000000";
    expect(verifyCsrfTokens({ headerToken: attacker, cookieToken: attacker, sessionToken: T })).toBe(false);
  });
});

describe("origin checks", () => {
  const h = (init: Record<string, string>) => new Headers(init);

  it("accepts same-origin and rejects other origins", () => {
    expect(isSameOriginRequest(h({ origin: ORIGIN }), ORIGIN)).toBe(true);
    expect(isSameOriginRequest(h({ origin: "https://evil.example" }), ORIGIN)).toBe(false);
    expect(isSameOriginRequest(h({ origin: "null" }), ORIGIN)).toBe(false);
  });

  it("uses fetch metadata when Origin is absent", () => {
    expect(isSameOriginRequest(h({ "sec-fetch-site": "same-origin" }), ORIGIN)).toBe(true);
    expect(isSameOriginRequest(h({ "sec-fetch-site": "cross-site" }), ORIGIN)).toBe(false);
    expect(isSameOriginRequest(h({ "sec-fetch-site": "same-site" }), ORIGIN)).toBe(false);
  });

  it("combines both checks", () => {
    const tokens = { headerToken: T, cookieToken: T, sessionToken: T };
    expect(checkMutatingRequest(h({ origin: ORIGIN }), ORIGIN, tokens)).toBeNull();
    expect(checkMutatingRequest(h({ origin: "https://evil.example" }), ORIGIN, tokens)).toBe("cross_origin");
    expect(checkMutatingRequest(h({ origin: ORIGIN }), ORIGIN, { ...tokens, headerToken: null })).toBe("csrf_invalid");
  });

  it("classifies methods", () => {
    expect(["GET", "HEAD", "OPTIONS"].map(isMutating)).toEqual([false, false, false]);
    expect(["POST", "PUT", "PATCH", "DELETE", "post"].map(isMutating)).toEqual([true, true, true, true, true]);
  });
});
