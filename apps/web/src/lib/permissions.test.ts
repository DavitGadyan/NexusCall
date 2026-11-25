import { describe, expect, it } from "vitest";
import { hasPermission } from "./permissions";

describe("hasPermission", () => {
  it.each([
    [["leads:read"], "leads:read", true],
    [["leads:read"], "leads:update", false],
    [["emails:*"], "emails:drafts:approve", true],
    [["emails:drafts:review"], "emails:drafts:approve", false],
    [["platform:*"], "platform:tenants:manage", true],
    [["*"], "audit:read", true],
  ])("%j needs %s -> %s", (granted, required, expected) => {
    expect(hasPermission(granted, required)).toBe(expected);
  });
});
