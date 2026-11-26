import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "./api";

type Captured = { url: string; init: RequestInit };

function stubFetch(status = 200, body = "{}") {
  const calls: Captured[] = [];
  vi.stubGlobal("fetch", async (url: string, init: RequestInit) => {
    calls.push({ url, init });
    return new Response(body, { status, headers: { "content-type": "application/json" } });
  });
  return calls;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("api()", () => {
  it("sends JSON bodies with a JSON content type", async () => {
    const calls = stubFetch();
    await api("/api/v1/leads", { body: { name: "x" } });
    const call = calls[0]!;
    const headers = call.init.headers as Record<string, string>;
    expect(headers["Content-Type"]).toBe("application/json");
    expect(call.init.body).toBe('{"name":"x"}');
  });

  it("sends FormData as multipart: no JSON content type, body untouched, CSRF and Idempotency-Key kept", async () => {
    vi.stubGlobal("document", { cookie: "nx_csrf=tok123" });
    const calls = stubFetch();
    const form = new FormData();
    form.append("file", new Blob(["a,b\n1,2\n"], { type: "text/csv" }), "leads.csv");
    await api("/api/v1/imports", { method: "POST", body: form, idempotencyKey: "ik-1" });
    const call = calls[0]!;
    const headers = call.init.headers as Record<string, string>;
    expect(call.url).toBe("/api/bff/api/v1/imports");
    expect(headers["Content-Type"]).toBeUndefined();
    expect(headers["X-CSRF-Token"]).toBe("tok123");
    expect(headers["Idempotency-Key"]).toBe("ik-1");
    expect(call.init.body).toBe(form);
  });

  it("refuses paths outside /api/v1/", async () => {
    stubFetch();
    await expect(api("/internal/v1/x")).rejects.toThrow(/must start with \/api\/v1\//);
  });
});
