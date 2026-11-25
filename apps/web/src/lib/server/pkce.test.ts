import { describe, expect, it } from "vitest";
import { codeChallengeS256, createCodeVerifier, createNonce, createState } from "./pkce";
import { buildAuthorizeUrl } from "./oidc";

describe("PKCE", () => {
  it("matches the RFC 7636 appendix B test vector", () => {
    expect(codeChallengeS256("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk")).toBe(
      "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
    );
  });

  it("creates high-entropy verifiers in the allowed alphabet", () => {
    const v = createCodeVerifier();
    expect(v).toMatch(/^[A-Za-z0-9_-]{43}$/);
    expect(createCodeVerifier()).not.toBe(v);
  });

  it("rejects malformed verifiers", () => {
    expect(() => codeChallengeS256("short")).toThrow();
    expect(() => codeChallengeS256("a".repeat(42) + "+")).toThrow();
  });

  it("state and nonce are random and distinct", () => {
    const values = new Set([createState(), createState(), createNonce(), createNonce()]);
    expect(values.size).toBe(4);
  });
});

describe("authorize URL", () => {
  const base = {
    publicIssuer: "https://id.example/realms/nexus",
    clientId: "web-bff",
    redirectUri: "https://app.example/api/auth/callback",
    state: "s",
    nonce: "n",
    codeChallenge: "c",
  };

  it("uses the code flow with S256 PKCE, state and nonce", () => {
    const url = new URL(buildAuthorizeUrl({ ...base, stepUp: false }));
    expect(url.origin + url.pathname).toBe("https://id.example/realms/nexus/protocol/openid-connect/auth");
    expect(url.searchParams.get("response_type")).toBe("code");
    expect(url.searchParams.get("code_challenge_method")).toBe("S256");
    expect(url.searchParams.get("code_challenge")).toBe("c");
    expect(url.searchParams.get("state")).toBe("s");
    expect(url.searchParams.get("nonce")).toBe("n");
    expect(url.searchParams.get("scope")).toBe("openid profile email");
    expect(url.searchParams.has("acr_values")).toBe(false);
    expect(url.searchParams.has("max_age")).toBe(false);
  });

  it("step-up requests aal2 with a fresh authentication", () => {
    const url = new URL(buildAuthorizeUrl({ ...base, stepUp: true }));
    expect(url.searchParams.get("acr_values")).toBe("aal2");
    expect(url.searchParams.get("max_age")).toBe("0");
  });
});
