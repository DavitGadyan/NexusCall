import { describe, expect, it } from "vitest";
import {
  convertValue,
  countRules,
  describe as describeRules,
  emptyGroup,
  fieldCatalogue,
  fromDsl,
  type GroupNode,
  MAX_DEPTH,
  MAX_RULES,
  newRule,
  reducer,
  STANDARD_FIELDS,
  toDsl,
} from "./segment-rules";
import type { FieldDef } from "./types";

const defs: FieldDef[] = [
  { id: "1", key: "plan", label: "Plan", type: "select", options: ["basic", "pro"], required: false, version: 1, created_at: "", updated_at: "" },
  { id: "2", key: "seats", label: "Seats", type: "number", options: [], required: false, version: 1, created_at: "", updated_at: "" },
  { id: "3", key: "vip", label: "VIP", type: "boolean", options: [], required: false, version: 1, created_at: "", updated_at: "" },
];
const catalogue = fieldCatalogue(defs);
const spec = (f: string) => catalogue.find((s) => s.field === f)!;

describe("field catalogue", () => {
  it("mirrors the service allowlist and adds custom fields with type-specific operators", () => {
    expect(STANDARD_FIELDS.map((f) => f.field)).toContain("lead_status");
    expect(spec("created_at").operators).toEqual(["gt", "gte", "lt", "lte"]);
    expect(spec("tag").operators).toEqual(["has", "has_any", "has_none"]);
    expect(spec("custom.vip").operators).toEqual(["eq", "is_set", "is_not_set"]);
    expect(spec("custom.plan").options).toEqual(["basic", "pro"]);
    expect(spec("custom.seats").operators).toContain("gte");
  });
});

describe("convertValue", () => {
  it("converts by kind and rejects bad values", () => {
    expect(convertValue(spec("custom.seats"), "gte", "10")).toEqual({ value: 10 });
    expect(convertValue(spec("custom.seats"), "gte", "ten").error).toBeTruthy();
    expect(convertValue(spec("custom.vip"), "eq", "true")).toEqual({ value: true });
    expect(convertValue(spec("owner_id"), "eq", "not-a-uuid").error).toBeTruthy();
    expect(convertValue(spec("status"), "eq", "deleted").error).toBeTruthy();
    expect(convertValue(spec("country"), "in", "US, CA ,")).toEqual({ value: ["US", "CA"] });
    expect(convertValue(spec("country"), "is_set", "ignored")).toEqual({});
    expect(convertValue(spec("country"), "eq", "x".repeat(201)).error).toMatch(/200/);
  });

  it("normalises datetimes to ISO", () => {
    const r = convertValue(spec("created_at"), "gte", "2026-01-02T03:04:00Z");
    expect(r.value).toBe("2026-01-02T03:04:00.000Z");
  });
});

describe("reducer", () => {
  it("adds, edits and removes rules and groups", () => {
    let root: GroupNode = emptyGroup();
    root = reducer(root, { type: "add_rule", groupId: root.id, spec: spec("country") });
    root = reducer(root, { type: "add_group", groupId: root.id });
    expect(root.children).toHaveLength(2);
    const group = root.children[1] as GroupNode;
    expect(group.kind).toBe("group");
    expect(group.op).toBe("or");
    const rule = root.children[0]!;
    root = reducer(root, { type: "set_value", id: rule.id, value: "US" });
    root = reducer(root, { type: "set_operator", id: rule.id, operator: "is_set" });
    expect(root.children[0]).toMatchObject({ operator: "is_set", value: "" });
    root = reducer(root, { type: "set_field", id: rule.id, spec: spec("created_at") });
    expect(root.children[0]).toMatchObject({ field: "created_at", operator: "gt" });
    root = reducer(root, { type: "remove", id: group.id });
    expect(root.children).toHaveLength(1);
    expect(reducer(root, { type: "remove", id: root.id })).toBe(root);
  });

  it("enforces the depth and rule limits", () => {
    let root: GroupNode = emptyGroup();
    let target = root.id;
    for (let d = 1; d < MAX_DEPTH + 2; d++) {
      root = reducer(root, { type: "add_group", groupId: target });
      let g: GroupNode = root;
      while (g.children.some((c) => c.kind === "group")) g = g.children.find((c) => c.kind === "group") as GroupNode;
      target = g.id;
    }
    let depth = 1;
    let g: GroupNode = root;
    while (g.children.some((c) => c.kind === "group")) {
      g = g.children.find((c) => c.kind === "group") as GroupNode;
      depth++;
    }
    expect(depth).toBe(MAX_DEPTH);
    let flat: GroupNode = emptyGroup();
    for (let i = 0; i < MAX_RULES + 5; i++) flat = reducer(flat, { type: "add_rule", groupId: flat.id });
    expect(countRules(flat)).toBe(MAX_RULES);
  });
});

describe("DSL round trip", () => {
  it("compiles the editor tree to the service DSL and back", () => {
    const dsl = {
      op: "and",
      rules: [
        { field: "country", operator: "in", value: ["US", "CA"] },
        { op: "or", rules: [{ field: "tag", operator: "has", value: "vip" }, { field: "custom.seats", operator: "gte", value: 5 }] },
        { field: "email", operator: "is_set" },
      ],
    };
    const tree = fromDsl(dsl);
    const out = toDsl(tree, catalogue);
    expect(out.errors).toEqual({});
    expect(out.dsl).toEqual(dsl);
  });

  it("reports per-rule errors and returns no DSL", () => {
    let root: GroupNode = { ...emptyGroup(), children: [newRule(spec("custom.seats"))] };
    root = reducer(root, { type: "set_operator", id: root.children[0]!.id, operator: "gt" });
    const out = toDsl(root, catalogue);
    expect(out.dsl).toBeNull();
    expect(Object.values(out.errors)[0]).toBe("Enter a value");
    const unknown = toDsl(fromDsl({ op: "and", rules: [{ field: "custom.gone", operator: "eq", value: "x" }] }), catalogue);
    expect(Object.values(unknown.errors)[0]).toMatch(/no longer available/);
  });

  it("ignores malformed stored rules", () => {
    expect(fromDsl("nope").children).toEqual([]);
    expect(fromDsl({ op: "xor", rules: [1, { field: 2 }] })).toMatchObject({ op: "and", children: [] });
  });

  it("describes rules in words", () => {
    expect(describeRules({ op: "and", rules: [] }, catalogue)).toBe("All contacts");
    expect(describeRules({ op: "or", rules: [{ field: "country", operator: "eq", value: "US" }, { field: "email", operator: "is_set" }] }, catalogue)).toBe("Country is US or Email is set");
  });
});
