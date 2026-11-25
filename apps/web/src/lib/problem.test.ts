import { describe, expect, it } from "vitest";
import { fieldErrors, isConflict, isStepUp, toProblem } from "./problem";

describe("problem+json handling", () => {
  it("normalises bodies", () => {
    expect(toProblem(409, { code: "conflict", detail: "Settings changed" })).toMatchObject({
      status: 409,
      code: "conflict",
      detail: "Settings changed",
    });
    expect(toProblem(503, "<html>")).toMatchObject({ status: 503, code: "unavailable" });
    expect(toProblem(403, null).detail).toMatch(/permission/);
  });

  it("maps FastAPI and settings 422 locations to field paths", () => {
    const errs = fieldErrors({
      errors: [
        { loc: ["body", "email"], msg: "value is not a valid email address" },
        { loc: ["calling_window", "start"], msg: "Value error, expected HH:MM" },
        { loc: ["body", "data", "max_daily_calls"], msg: "Input should be greater than 0" },
        { loc: ["body", "rows", 2, "email"], msg: "bad" },
        { loc: ["body"], msg: "Field required" },
        { loc: ["body", "email"], msg: "second message ignored" },
      ],
    });
    expect(errs).toEqual({
      email: "value is not a valid email address",
      "calling_window.start": "expected HH:MM",
      max_daily_calls: "Input should be greater than 0",
      "rows.2.email": "bad",
      body: "Field required",
    });
    expect(fieldErrors(undefined)).toEqual({});
  });

  it("recognises step-up and conflicts", () => {
    expect(isStepUp(toProblem(401, { code: "step_up_required" }))).toBe(true);
    expect(isStepUp(toProblem(401, { code: "session_expired" }))).toBe(false);
    expect(isConflict(toProblem(409, {}))).toBe(true);
  });
});
