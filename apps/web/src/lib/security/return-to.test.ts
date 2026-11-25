import { describe, expect, it } from "vitest";
import { safeReturnTo } from "./return-to";

describe("safeReturnTo", () => {
  it.each([
    ["/app", "/app"],
    ["/admin/team?tab=roles", "/admin/team?tab=roles"],
    ["/invite/accept?token=abc_DEF-123", "/invite/accept?token=abc_DEF-123"],
    ["/platform/tenants#new", "/platform/tenants#new"],
    ["/a/../platform", "/platform"],
  ])("accepts same-origin path %s", (input, expected) => {
    expect(safeReturnTo(input)).toBe(expected);
  });

  it.each([
    "https://evil.example/app",
    "http:/evil.example",
    "//evil.example/app",
    "///evil.example",
    "/\\evil.example",
    "\\\\evil.example",
    "/app\\..\\..\\evil",
    "javascript:alert(1)",
    "data:text/html,hi",
    "app",
    "",
    "/\t/evil.example",
    "/\n/evil.example",
    "/ /evil",
    "/api/auth/login",
    "/api/auth/logout?x=1",
    "/api/bff/api/v1/me",
    "/api/session/tenant",
    "/" + "a".repeat(3000),
  ])("rejects %j", (input) => {
    expect(safeReturnTo(input)).toBe("/app");
  });

  it("rejects non-strings and uses the given fallback", () => {
    expect(safeReturnTo(undefined, "/")).toBe("/");
    expect(safeReturnTo(["/app"], "/")).toBe("/");
    expect(safeReturnTo(null)).toBe("/app");
  });

  it("keeps encoded slashes inside the path (no origin change)", () => {
    expect(safeReturnTo("/%2F%2Fevil.example")).toBe("/%2F%2Fevil.example");
  });
});
