/** Mirrors nexus_core.authz.has_permission. UI-only gating; the backend is authoritative. */
export function hasPermission(granted: ReadonlySet<string> | readonly string[], required: string): boolean {
  const set = granted instanceof Set ? granted : new Set(granted);
  if (set.has(required)) return true;
  const parts = required.split(":");
  for (let i = parts.length; i > 0; i--) {
    if (set.has([...parts.slice(0, i - 1), "*"].join(":"))) return true;
  }
  return false;
}
