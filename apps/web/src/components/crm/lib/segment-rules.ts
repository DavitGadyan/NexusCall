/**
 * Segment rule builder model. Mirrors the allowlists of crm_service `segment_dsl.py`:
 *   {"op": "and"|"or", "rules": [ {"field", "operator", "value"} | <nested group> ]}
 * The service validates again; this keeps the builder from offering combinations it would reject.
 */
import type { FieldDef } from "./types";
import { CONTACT_STATUSES, LEAD_STATUSES } from "./types";

export const MAX_DEPTH = 4;
export const MAX_RULES = 50;
export const MAX_LIST = 100;
export const MAX_STRING = 200;

export type Operator =
  | "eq" | "neq" | "in" | "not_in" | "contains" | "starts_with" | "is_set" | "is_not_set"
  | "gt" | "gte" | "lt" | "lte" | "has" | "has_any" | "has_none";

export type ValueKind = "string" | "uuid" | "datetime" | "date" | "number" | "boolean" | "enum" | "tag";

export type FieldSpec = { field: string; label: string; kind: ValueKind; operators: Operator[]; options?: readonly string[] };

const STRING_OPS: Operator[] = ["eq", "neq", "in", "not_in", "contains", "starts_with", "is_set", "is_not_set"];
const UUID_OPS: Operator[] = ["eq", "neq", "in", "not_in", "is_set", "is_not_set"];
const ORDER_OPS: Operator[] = ["eq", "neq", "gt", "gte", "lt", "lte", "is_set", "is_not_set"];
const ENUM_OPS: Operator[] = ["eq", "neq", "in", "not_in"];

export const STANDARD_FIELDS: FieldSpec[] = [
  { field: "first_name", label: "First name", kind: "string", operators: STRING_OPS },
  { field: "last_name", label: "Last name", kind: "string", operators: STRING_OPS },
  { field: "company", label: "Company", kind: "string", operators: STRING_OPS },
  { field: "email", label: "Email", kind: "string", operators: STRING_OPS },
  { field: "phone_e164", label: "Phone (E.164)", kind: "string", operators: STRING_OPS },
  { field: "timezone", label: "Timezone", kind: "string", operators: STRING_OPS },
  { field: "country", label: "Country", kind: "string", operators: STRING_OPS },
  { field: "locale", label: "Locale", kind: "string", operators: STRING_OPS },
  { field: "source", label: "Source", kind: "string", operators: STRING_OPS },
  { field: "status", label: "Contact status", kind: "enum", operators: ENUM_OPS, options: CONTACT_STATUSES },
  { field: "owner_id", label: "Owner ID", kind: "uuid", operators: UUID_OPS },
  { field: "created_at", label: "Created", kind: "datetime", operators: ["gt", "gte", "lt", "lte"] },
  { field: "updated_at", label: "Updated", kind: "datetime", operators: ["gt", "gte", "lt", "lte"] },
  { field: "tag", label: "Tag", kind: "tag", operators: ["has", "has_any", "has_none"] },
  { field: "lead_status", label: "Lead status", kind: "enum", operators: ["eq", "in", "not_in"], options: LEAD_STATUSES },
];

const CUSTOM_OPS: Record<FieldDef["type"], Operator[]> = {
  text: STRING_OPS,
  number: ORDER_OPS,
  boolean: ["eq", "is_set", "is_not_set"],
  date: ORDER_OPS,
  select: ["eq", "neq", "in", "not_in", "is_set", "is_not_set"],
};
const CUSTOM_KIND: Record<FieldDef["type"], ValueKind> = { text: "string", number: "number", boolean: "boolean", date: "date", select: "enum" };

export const OPERATOR_LABEL: Record<Operator, string> = {
  eq: "is", neq: "is not", in: "is any of", not_in: "is none of", contains: "contains", starts_with: "starts with",
  is_set: "is set", is_not_set: "is not set", gt: "after / greater than", gte: "on or after / at least",
  lt: "before / less than", lte: "on or before / at most", has: "has", has_any: "has any of", has_none: "has none of",
};

export const NO_VALUE: ReadonlySet<Operator> = new Set(["is_set", "is_not_set"]);
export const LIST_OPS: ReadonlySet<Operator> = new Set(["in", "not_in", "has_any", "has_none"]);

export function fieldCatalogue(customFields: readonly FieldDef[]): FieldSpec[] {
  return [
    ...STANDARD_FIELDS,
    ...customFields.map((d) => ({
      field: `custom.${d.key}`,
      label: `${d.label} (custom)`,
      kind: CUSTOM_KIND[d.type],
      operators: CUSTOM_OPS[d.type],
      options: d.type === "select" ? d.options : undefined,
    })),
  ];
}

// ---------------------------------------------------------------------------------------- editor model
/** Editor state keeps values as text (lists comma-separated) and converts on output. */
export type RuleNode = { kind: "rule"; id: string; field: string; operator: Operator; value: string };
export type GroupNode = { kind: "group"; id: string; op: "and" | "or"; children: Node[] };
export type Node = RuleNode | GroupNode;

let seq = 0;
export function nodeId(): string {
  seq += 1;
  return `n${seq}`;
}

export function emptyGroup(op: "and" | "or" = "and"): GroupNode {
  return { kind: "group", id: nodeId(), op, children: [] };
}

export function newRule(spec: FieldSpec = STANDARD_FIELDS[0]!): RuleNode {
  return { kind: "rule", id: nodeId(), field: spec.field, operator: spec.operators[0]!, value: "" };
}

export type Action =
  | { type: "add_rule"; groupId: string; spec?: FieldSpec }
  | { type: "add_group"; groupId: string }
  | { type: "remove"; id: string }
  | { type: "set_op"; groupId: string; op: "and" | "or" }
  | { type: "set_field"; id: string; spec: FieldSpec }
  | { type: "set_operator"; id: string; operator: Operator }
  | { type: "set_value"; id: string; value: string }
  | { type: "replace"; root: GroupNode };

function mapTree(node: Node, fn: (n: Node) => Node): Node {
  const next = fn(node);
  if (next.kind === "group") return { ...next, children: next.children.map((c) => mapTree(c, fn)) };
  return next;
}

export function depthOf(root: GroupNode, id: string, depth = 1): number | null {
  if (root.id === id) return depth;
  for (const c of root.children) {
    if (c.kind === "group") {
      const d = depthOf(c, id, depth + 1);
      if (d !== null) return d;
    }
  }
  return null;
}

export function countRules(node: Node): number {
  return node.kind === "rule" ? 1 : node.children.reduce((n, c) => n + countRules(c), 0);
}

export function reducer(root: GroupNode, action: Action): GroupNode {
  switch (action.type) {
    case "replace":
      return action.root;
    case "add_rule":
      if (countRules(root) >= MAX_RULES) return root;
      return mapTree(root, (n) => (n.kind === "group" && n.id === action.groupId ? { ...n, children: [...n.children, newRule(action.spec)] } : n)) as GroupNode;
    case "add_group": {
      const depth = depthOf(root, action.groupId);
      if (depth === null || depth >= MAX_DEPTH) return root;
      return mapTree(root, (n) =>
        n.kind === "group" && n.id === action.groupId ? { ...n, children: [...n.children, { ...emptyGroup(n.op === "and" ? "or" : "and"), children: [newRule()] }] } : n,
      ) as GroupNode;
    }
    case "remove": {
      if (action.id === root.id) return root;
      const strip = (g: GroupNode): GroupNode => ({
        ...g,
        children: g.children.filter((c) => c.id !== action.id).map((c) => (c.kind === "group" ? strip(c) : c)),
      });
      return strip(root);
    }
    case "set_op":
      return mapTree(root, (n) => (n.kind === "group" && n.id === action.groupId ? { ...n, op: action.op } : n)) as GroupNode;
    case "set_field":
      return mapTree(root, (n) =>
        n.kind === "rule" && n.id === action.id
          ? { ...n, field: action.spec.field, operator: action.spec.operators.includes(n.operator) ? n.operator : action.spec.operators[0]!, value: "" }
          : n,
      ) as GroupNode;
    case "set_operator":
      return mapTree(root, (n) => (n.kind === "rule" && n.id === action.id ? { ...n, operator: action.operator, value: NO_VALUE.has(action.operator) ? "" : n.value } : n)) as GroupNode;
    case "set_value":
      return mapTree(root, (n) => (n.kind === "rule" && n.id === action.id ? { ...n, value: action.value } : n)) as GroupNode;
    default:
      return root;
  }
}

// ---------------------------------------------------------------------------------------- DSL conversion
export type DslRule = { field: string; operator: Operator; value?: unknown };
export type DslGroup = { op: "and" | "or"; rules: (DslRule | DslGroup)[] };

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export function splitList(text: string): string[] {
  return text.split(",").map((s) => s.trim()).filter(Boolean);
}

/** Converts one editor value into the DSL value; returns an error message instead when invalid. */
export function convertValue(spec: FieldSpec, op: Operator, raw: string): { value?: unknown; error?: string } {
  if (NO_VALUE.has(op)) return {};
  const one = (s: string): { value?: unknown; error?: string } => {
    const v = s.trim();
    if (!v) return { error: "Enter a value" };
    if (v.length > MAX_STRING) return { error: `At most ${MAX_STRING} characters` };
    switch (spec.kind) {
      case "number": {
        const n = Number(v);
        return Number.isFinite(n) ? { value: n } : { error: "Enter a number" };
      }
      case "boolean":
        if (v === "true" || v === "false") return { value: v === "true" };
        return { error: "Choose yes or no" };
      case "uuid":
        return UUID.test(v) ? { value: v.toLowerCase() } : { error: "Enter a valid ID" };
      case "date":
        return /^\d{4}-\d{2}-\d{2}$/.test(v) && !Number.isNaN(Date.parse(v)) ? { value: v } : { error: "Use YYYY-MM-DD" };
      case "datetime": {
        const t = Date.parse(v);
        return Number.isNaN(t) ? { error: "Enter a date and time" } : { value: new Date(t).toISOString() };
      }
      case "enum":
        return spec.options && !spec.options.includes(v) ? { error: "Choose one of the listed values" } : { value: v };
      default:
        return { value: v };
    }
  };
  if (LIST_OPS.has(op)) {
    const parts = splitList(raw);
    if (parts.length === 0) return { error: "Enter at least one value" };
    if (parts.length > MAX_LIST) return { error: `At most ${MAX_LIST} values` };
    const out: unknown[] = [];
    for (const p of parts) {
      const r = one(p);
      if (r.error) return r;
      out.push(r.value);
    }
    return { value: out };
  }
  return one(raw);
}

export type Compiled = { dsl: DslGroup | null; errors: Record<string, string> };

export function toDsl(root: GroupNode, catalogue: readonly FieldSpec[]): Compiled {
  const errors: Record<string, string> = {};
  const byField = new Map(catalogue.map((s) => [s.field, s]));
  const walk = (g: GroupNode): DslGroup => ({
    op: g.op,
    rules: g.children.map((c) => {
      if (c.kind === "group") return walk(c);
      const spec = byField.get(c.field);
      if (!spec) {
        errors[c.id] = "This field is no longer available";
        return { field: c.field, operator: c.operator };
      }
      if (!spec.operators.includes(c.operator)) errors[c.id] = "This operator is not allowed for the field";
      const r = convertValue(spec, c.operator, c.value);
      if (r.error) errors[c.id] = r.error;
      return NO_VALUE.has(c.operator) ? { field: c.field, operator: c.operator } : { field: c.field, operator: c.operator, value: r.value };
    }),
  });
  const dsl = walk(root);
  if (countRules(root) > MAX_RULES) errors[root.id] = `At most ${MAX_RULES} rules`;
  return { dsl: Object.keys(errors).length ? null : dsl, errors };
}

function valueToText(value: unknown): string {
  if (Array.isArray(value)) return value.map((v) => String(v)).join(", ");
  if (value === null || value === undefined) return "";
  return String(value);
}

/** Loads stored rules into the editor. Unknown shapes become an empty AND group. */
export function fromDsl(rules: unknown, depth = 1): GroupNode {
  if (!rules || typeof rules !== "object" || depth > MAX_DEPTH) return emptyGroup();
  const g = rules as { op?: unknown; rules?: unknown };
  const op = g.op === "or" ? "or" : "and";
  const children: Node[] = [];
  for (const c of Array.isArray(g.rules) ? g.rules : []) {
    if (!c || typeof c !== "object") continue;
    if ("op" in c) children.push(fromDsl(c, depth + 1));
    else {
      const r = c as { field?: unknown; operator?: unknown; value?: unknown };
      if (typeof r.field !== "string" || typeof r.operator !== "string") continue;
      children.push({ kind: "rule", id: nodeId(), field: r.field, operator: r.operator as Operator, value: valueToText(r.value) });
    }
  }
  return { kind: "group", id: nodeId(), op, children };
}

/** Human-readable one-liner for lists ("Country is US and (Tag has vip or …)"). */
export function describe(rules: unknown, catalogue: readonly FieldSpec[], depth = 0): string {
  if (!rules || typeof rules !== "object") return "All contacts";
  const g = rules as DslGroup;
  if (!Array.isArray(g.rules) || g.rules.length === 0) return depth === 0 ? "All contacts" : "(empty)";
  const label = (f: string) => catalogue.find((s) => s.field === f)?.label ?? f;
  const parts = g.rules.map((r) =>
    "op" in r ? `(${describe(r, catalogue, depth + 1)})` : `${label(r.field)} ${OPERATOR_LABEL[r.operator] ?? r.operator}${NO_VALUE.has(r.operator) ? "" : ` ${valueToText(r.value)}`}`,
  );
  return parts.join(g.op === "or" ? " or " : " and ");
}
