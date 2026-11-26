import { describe, expect, it } from "vitest";
import { buildCsp, generateNonce, isProtectedPath, mediaOrigin } from "./csp";

describe("buildCsp", () => {
  const prod = buildCsp("abc123==", { dev: false, https: true });

  it("allows scripts only by nonce (no inline, no eval) in production", () => {
    const script = prod.split("; ").find((d) => d.startsWith("script-src"));
    expect(script).toBe("script-src 'self' 'nonce-abc123==' 'strict-dynamic'");
    expect(prod).not.toContain("unsafe-eval");
    expect(script).not.toContain("unsafe-inline");
  });

  it("forbids framing and plugins", () => {
    expect(prod).toContain("frame-ancestors 'none'");
    expect(prod).toContain("object-src 'none'");
    expect(prod).toContain("base-uri 'self'");
    expect(prod).toContain("upgrade-insecure-requests");
  });

  it("only relaxes eval for next dev", () => {
    expect(buildCsp("n", { dev: true, https: false })).toContain("'unsafe-eval'");
    expect(buildCsp("n", { dev: true, https: false })).not.toContain("upgrade-insecure-requests");
  });
});

describe("generateNonce", () => {
  it("is random base64 of 16 bytes", () => {
    const a = generateNonce();
    expect(a).toMatch(/^[A-Za-z0-9+/]{22}==$/);
    expect(generateNonce()).not.toBe(a);
  });
});

describe("isProtectedPath", () => {
  it.each([
    ["/app", true],
    ["/app/calls", true],
    ["/admin/team", true],
    ["/platform", true],
    ["/platformer", false],
    ["/applications", false],
    ["/", false],
    ["/invite/accept", false],
    ["/api/health", false],
  ])("%s -> %s", (path, expected) => {
    expect(isProtectedPath(path)).toBe(expected);
  });
});

describe("media-src", () => {
  it("allows only same-origin media by default", () => {
    expect(buildCsp("n", { dev: false, https: true })).toContain("media-src 'self';");
  });
  it("adds the configured object-store origin, normalised to scheme://host[:port]", () => {
    const csp = buildCsp("n", { dev: false, https: true, mediaOrigin: mediaOrigin("https://s3.example.com/recordings?x=1") });
    expect(csp).toContain("media-src 'self' https://s3.example.com;");
  });
  it("ignores values that are not plain http(s) origins", () => {
    for (const bad of ["javascript:alert(1)", "*", "https://u:p@s3.example.com", "not a url", "data:,x", ""]) {
      expect(mediaOrigin(bad)).toBeNull();
    }
  });
});
