/** RFC 7807 problem details as returned by every service (nexus_core.errors) and by the BFF. */
export type Problem = {
  type?: string;
  title?: string;
  status: number;
  code: string;
  detail: string;
  request_id?: string | null;
  errors?: { loc?: (string | number)[]; msg?: string; type?: string }[];
  step_up?: { login_url: string };
  [k: string]: unknown;
};

export function toProblem(status: number, body: unknown): Problem {
  const b = body && typeof body === "object" ? (body as Record<string, unknown>) : {};
  return {
    ...b,
    status: typeof b.status === "number" ? b.status : status,
    code: typeof b.code === "string" ? b.code : status >= 500 ? "unavailable" : "error",
    detail: typeof b.detail === "string" && b.detail ? b.detail : defaultDetail(status),
  } as Problem;
}

export function defaultDetail(status: number): string {
  if (status === 401) return "Your session has ended. Sign in again.";
  if (status === 403) return "You do not have permission to do this.";
  if (status === 404) return "Not found.";
  if (status === 409) return "This was changed by someone else. Reload and try again.";
  if (status === 422) return "Some fields need attention.";
  if (status === 429) return "Too many attempts. Wait a moment and try again.";
  if (status >= 500) return "The service is unavailable. Try again shortly.";
  return "The request failed.";
}

const WRAPPERS = new Set(["body", "data", "query", "path"]);

/**
 * Maps 422 `errors[].loc` to dotted field paths. FastAPI locations start with `body` (and, for settings
 * sections, `data`); the remaining parts name the field, e.g. ["body","data","calling_window","start"].
 */
export function fieldErrors(problem: Pick<Problem, "errors"> | null | undefined): Record<string, string> {
  const out: Record<string, string> = {};
  for (const e of problem?.errors ?? []) {
    const loc = [...(e.loc ?? [])];
    while (loc.length > 1 && typeof loc[0] === "string" && WRAPPERS.has(loc[0])) loc.shift();
    const key = loc.filter((p) => typeof p === "string" || typeof p === "number").join(".") || "_form";
    const msg = (e.msg ?? "Invalid value").replace(/^Value error, /, "");
    if (!out[key]) out[key] = msg;
  }
  return out;
}

export function isStepUp(problem: Problem | null | undefined): boolean {
  return problem?.status === 401 && problem.code === "step_up_required";
}

export function isConflict(problem: Problem | null | undefined): boolean {
  return problem?.status === 409;
}
