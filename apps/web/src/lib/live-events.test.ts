import { describe, expect, it } from "vitest";
import { matchesEvent, parseLiveEvent, reconnectDelay } from "./live-events";

describe("live events", () => {
  it("parses notification and ui events", () => {
    expect(parseLiveEvent('{"type":"notification","id":"n1"}')).toEqual({ type: "notification", id: "n1" });
    expect(parseLiveEvent('{"type":"ui","event":"call.updated","data":{"call_id":"c1","n":5}}')).toEqual({
      type: "ui",
      event: "call.updated",
      data: { call_id: "c1" },
    });
  });

  it("drops anything else", () => {
    for (const raw of ["", "nope", "null", "[]", '{"type":"ui","event":"<script>"}', '{"type":"other"}', '{"type":"notification"}']) {
      expect(parseLiveEvent(raw)).toBeNull();
    }
  });

  it("filters by event name with wildcards", () => {
    const evt = { type: "ui", event: "call.updated", data: {} } as const;
    expect(matchesEvent(evt, undefined)).toBe(true);
    expect(matchesEvent(evt, ["call.updated"])).toBe(true);
    expect(matchesEvent(evt, ["call.*"])).toBe(true);
    expect(matchesEvent(evt, ["email.*"])).toBe(false);
    expect(matchesEvent({ type: "notification", id: "x" }, ["notification"])).toBe(true);
  });

  it("backs off exponentially with a cap", () => {
    expect(reconnectDelay(0)).toBe(1000);
    expect(reconnectDelay(3)).toBe(8000);
    expect(reconnectDelay(30)).toBe(60_000);
  });
});
