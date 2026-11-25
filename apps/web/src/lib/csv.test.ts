import { describe, expect, it } from "vitest";
import { csvCell, parseCsv, parseInviteCsv, toCsv } from "./csv";

describe("parseCsv", () => {
  it("handles quotes, escaped quotes, CRLF and a BOM", () => {
    expect(parseCsv('﻿a,"b,c","d ""e"""\r\n1,2,3\n\n')).toEqual([
      ["a", "b,c", 'd "e"'],
      ["1", "2", "3"],
    ]);
  });
});

describe("parseInviteCsv", () => {
  const opts = {
    knownRoles: new Set(["analyst", "tenant_admin", "tenant_owner"]),
    grantable: new Set(["analyst", "tenant_admin"]),
    teams: new Set(["Sales"]),
  };

  it("validates every row and keeps line numbers", () => {
    const { rows, fileErrors } = parseInviteCsv(
      "email,roles,team\nA@Example.test,analyst,Sales\nbad,analyst,\nb@example.test,tenant_owner;nope,\na@example.test,analyst,Ops\nc@example.test,,\n",
      opts,
    );
    expect(fileErrors).toEqual([]);
    expect(rows.map((r) => [r.line, r.email, r.errors])).toEqual([
      [2, "a@example.test", []],
      [3, "bad", ["invalid email"]],
      [4, "b@example.test", ['you cannot grant "tenant_owner"', 'unknown role "nope"']],
      [5, "a@example.test", ["duplicate email in file", 'unknown team "Ops"']],
      [6, "c@example.test", ["no role"]],
    ]);
    expect(rows[0]?.roleKeys).toEqual(["analyst"]);
    expect(rows[0]?.team).toBe("Sales");
  });

  it("works without a header and accepts | as role separator", () => {
    const { rows } = parseInviteCsv("x@example.test,analyst|tenant_admin\n", opts);
    expect(rows[0]).toMatchObject({ line: 1, roleKeys: ["analyst", "tenant_admin"], errors: [] });
  });

  it("limits the upload size", () => {
    const text = Array.from({ length: 201 }, (_, i) => `u${i}@example.test,analyst`).join("\n");
    const { rows, fileErrors } = parseInviteCsv(text, opts);
    expect(rows).toHaveLength(200);
    expect(fileErrors[0]).toMatch(/At most 200/);
    expect(parseInviteCsv("", opts).fileErrors).toEqual(["The file has no rows."]);
  });
});

describe("report export", () => {
  it("neutralises formulas and quotes fields", () => {
    expect(csvCell("=HYPERLINK(\"x\")")).toBe("\"'=HYPERLINK(\"\"x\"\")\"");
    expect(csvCell("+1")).toBe("'+1");
    expect(csvCell("@cmd")).toBe("'@cmd");
    expect(csvCell("a,b")).toBe('"a,b"');
    expect(csvCell(null)).toBe("");
    expect(toCsv([["row", "email"], [1, "a@example.test"]])).toBe("row,email\r\n1,a@example.test\r\n");
  });
});
