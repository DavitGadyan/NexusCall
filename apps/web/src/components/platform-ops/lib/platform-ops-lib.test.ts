import { describe, expect, it } from "vitest";
import {
  type AiModel,
  killPhrase,
  microsToUsdInput,
  modelPatch,
  moveItem,
  type Proposal,
  policyDiff,
  proposalActions,
  toPolicy,
  usdToMicros,
  validateKillSwitch,
  validatePolicy,
  validateRoute,
} from "./ai";
import { groupQueues, queueTone, sortServices, validateDlqAction } from "./infra";
import { enableBlocker, type McpServer, serverPatch, serverState, serverToForm, validatePin } from "./mcp";
import { secretHealth, shortRef, summarizeSecrets } from "./secrets";

describe("prices", () => {
  it("converts USD text to micros exactly", () => {
    expect(usdToMicros("3")).toBe(3_000_000);
    expect(usdToMicros("0.25")).toBe(250_000);
    expect(usdToMicros("15.000001")).toBe(15_000_001);
    expect(usdToMicros("-1")).toBeNull();
    expect(usdToMicros("1e3")).toBeNull();
    expect(usdToMicros("0.1234567")).toBeNull();
    expect(microsToUsdInput(250_000)).toBe("0.25");
    expect(microsToUsdInput(3_000_000)).toBe("3");
  });

  const model: AiModel = {
    model_id: "claude-haiku", provider: "anthropic", display_name: "Haiku", enabled: true, input_price_micros_per_mtok: 1_000_000,
    output_price_micros_per_mtok: 5_000_000, max_output_tokens: 4096, allowed_classifications: ["public", "internal"], purposes: [], updated_at: "", updated_by: null,
  };
  it("patches only changed model fields", () => {
    expect(modelPatch(model, { enabled: true, input: "1", output: "5", classifications: ["internal", "public"] }).patch).toBeNull();
    expect(modelPatch(model, { enabled: false, input: "1.5", output: "5", classifications: ["public"] }).patch).toEqual({
      enabled: false, input_price_micros_per_mtok: 1_500_000, allowed_classifications: ["public"],
    });
    expect(Object.keys(modelPatch(model, { enabled: true, input: "x", output: "5", classifications: [] }).errors).sort()).toEqual([
      "allowed_classifications", "input_price_micros_per_mtok",
    ]);
  });

  it("validates routes and reorders", () => {
    const reg = new Set(["a", "b"]);
    expect(validateRoute(["a"], reg)).toBeNull();
    expect(validateRoute([], reg)).not.toBeNull();
    expect(validateRoute(["a", "a"], reg)).toMatch(/once/);
    expect(validateRoute(["c"], reg)).toMatch(/registered/);
    expect(moveItem(["a", "b", "c"], 1, -1)).toEqual(["b", "a", "c"]);
    expect(moveItem(["a", "b"], 1, 1)).toEqual(["a", "b"]);
  });
});

describe("kill switches", () => {
  it("mirrors the gateway's target rules", () => {
    expect(validateKillSwitch({ scope: "platform", target: "*", reason: "Incident 42" })).toEqual({});
    expect(validateKillSwitch({ scope: "platform", target: "anthropic", reason: "Incident 42" }).target).toBeDefined();
    expect(validateKillSwitch({ scope: "provider", target: "*", reason: "Incident 42" }).target).toBeDefined();
    expect(validateKillSwitch({ scope: "tenant", target: "acme", reason: "Incident 42" }).target).toMatch(/UUID/);
    expect(validateKillSwitch({ scope: "model", target: "claude-haiku-5", reason: "no" }).reason).toBeDefined();
    expect(killPhrase("model", " claude-x ", true)).toBe("engage model:claude-x");
  });
});

describe("tool policy", () => {
  it("defaults like the registry and validates like it", () => {
    const p = toPolicy({});
    expect(p).toMatchObject({ max_agent_risk_class: 3, approval_required_classes: [3], agent_calls_per_minute: 60, mcp_tools_enabled: true });
    expect(validatePolicy(p)).toEqual({});
    expect(validatePolicy({ ...p, approval_required_classes: [2] }).approval_required_classes).toMatch(/Class 3/);
    expect(validatePolicy({ ...p, max_agent_risk_class: 4 }).max_agent_risk_class).toBeDefined();
    expect(validatePolicy({ ...p, disabled_tools: ["Bad Tool"] }).disabled_tools).toMatch(/Bad Tool/);
    expect(validatePolicy({ ...p, agent_calls_per_minute: 0 }).agent_calls_per_minute).toBeDefined();
  });

  it("diffs the active and proposed policy", () => {
    expect(policyDiff({ a: 1, b: [3] }, { a: 1, b: [2, 3] })).toEqual([{ key: "b", before: "3", after: "2, 3" }]);
  });

  const base: Proposal = {
    id: "p", base_version: 4, policy: {}, reason: "tighten", status: "pending", proposed_by: "alice", approved_by: null, rejected_by: null,
    deployed_by: null, decision_note: null, decided_at: null, deployed_at: null, deployed_version: null,
    expires_at: "2026-01-02T00:00:00Z", created_at: "2026-01-01T00:00:00Z",
  };
  const now = Date.parse("2026-01-01T12:00:00Z");

  it("never lets the proposer approve their own change", () => {
    const own = proposalActions(base, "alice", 4, now);
    expect(own.approve).toBe(false);
    expect(own.approveNote).toMatch(/different administrator/);
    expect(proposalActions(base, "bob", 4, now).approve).toBe(true);
  });

  it("blocks expired proposals and stale deploys", () => {
    expect(proposalActions(base, "bob", 4, Date.parse("2026-01-03T00:00:00Z"))).toMatchObject({ approve: false, reject: false });
    const approved = { ...base, status: "approved" };
    expect(proposalActions(approved, "alice", 4, now).deploy).toBe(true);
    const stale = proposalActions(approved, "alice", 5, now);
    expect(stale.deploy).toBe(false);
    expect(stale.deployNote).toMatch(/v5/);
  });
});

const server: McpServer = {
  id: "s", key: "crm-x", display_name: "CRM X", description: "", purpose: "CRM lookups", owner: "Ops team", environment: "dev",
  transport: "streamable_http", endpoint: "https://mcp.example.com/mcp", allowed_hosts: ["mcp.example.com"], vault_ref: null,
  service_identity: "mcp-crm-x", trust_level: "verified", enabled: true, revoked: false, revoked_reason: null, disabled_reason: null,
  health: "healthy", pinned_server_name: "crm-x", pinned_version: "1.0.0", pinned_sha256: "a".repeat(64), reviewed_by: "u",
  reviewed_at: "", last_verified_at: null, max_concurrency: 4, max_request_bytes: 16384, max_response_bytes: 65536,
  rate_per_minute: 60, timeout_ms: 5000, row_version: 3, created_at: "", updated_at: "",
};

describe("MCP servers", () => {
  it("flags identity changes, which clear the pin", () => {
    const form = serverToForm(server);
    expect(serverPatch(server, form).patch).toBeNull();
    const renamed = serverPatch(server, { ...form, display_name: "CRM Ex" });
    expect(renamed).toMatchObject({ identityChanged: false, patch: { expected_version: 3, display_name: "CRM Ex" } });
    const moved = serverPatch(server, { ...form, endpoint: "https://other.example.com/mcp" });
    expect(moved.identityChanged).toBe(true);
    expect(serverPatch(server, { ...form, endpoint: "https://x.example.com/mcp?a=1" }).errors.endpoint).toBeDefined();
    expect(serverPatch(server, { ...form, timeout_ms: "50" }).errors.timeout_ms).toBeDefined();
  });

  it("validates a pin", () => {
    expect(validatePin({ server_name: "crm-x", version: "1.2.3", sha256: "b".repeat(64) })).toEqual({});
    expect(Object.keys(validatePin({ server_name: "", version: "1 2", sha256: "B".repeat(64) })).sort()).toEqual(["server_name", "sha256", "version"]);
  });

  it("explains why a server cannot be enabled", () => {
    expect(enableBlocker(server)).toBeNull();
    expect(enableBlocker({ ...server, pinned_sha256: null })).toMatch(/Pin/);
    expect(enableBlocker({ ...server, health: "identity_mismatch" })).toMatch(/identity/);
    expect(serverState({ ...server, pinned_version: null }).label).toBe("Needs review");
    expect(serverState({ ...server, revoked: true }).tone).toBe("danger");
  });
});

describe("infrastructure", () => {
  it("puts unhealthy services first", () => {
    const out = sortServices([
      { service: "b", status: "ready" },
      { service: "a", status: "unreachable" },
      { service: "c", status: "not_ready" },
    ]);
    expect(out.map((s) => s.service)).toEqual(["a", "c", "b"]);
  });

  it("groups queues and flags stuck ones", () => {
    const g = groupQueues([
      { name: "x.dlq", kind: "dead_letter", messages: 2 },
      { name: "t1", kind: "task", messages: 5, consumers: 0 },
      { name: "t2", kind: "task", messages: 10, consumers: 2 },
    ]);
    expect(g.task.map((q) => q.name)).toEqual(["t2", "t1"]);
    expect(queueTone(g.task[1]!)).toBe("danger");
    expect(queueTone(g.dead_letter[0]!)).toBe("danger");
    expect(queueTone({ name: "e", kind: "event", messages: 0 })).toBe("ok");
  });

  it("validates replay and discard requests", () => {
    expect(validateDlqAction({ queue: "calls.tasks.dlq", count: "10", reason: "fixed bug 123" })).toEqual({});
    expect(Object.keys(validateDlqAction({ queue: "calls.tasks", count: "51", reason: "x" })).sort()).toEqual(["count", "queue", "reason"]);
  });
});

describe("secrets", () => {
  it("summarizes health without values", () => {
    expect(summarizeSecrets([{ health: "healthy" }, { health: "vault_error" }, { health: "disabled" }])).toEqual({ total: 3, healthy: 1, attention: 1 });
    expect(secretHealth("rotation_overdue").tone).toBe("warn");
    expect(shortRef("a".repeat(60), 10)).toBe(`…${"a".repeat(9)}`);
  });
});
