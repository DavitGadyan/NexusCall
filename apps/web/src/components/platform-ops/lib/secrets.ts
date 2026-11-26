/** Secrets health (integration_service `/api/v1/platform/secrets`): references and rotation only, never values. */

type Tone = "neutral" | "ok" | "warn" | "danger" | "brand";

const SECRET_HEALTH: Record<string, { label: string; tone: Tone }> = {
  healthy: { label: "Healthy", tone: "ok" },
  rotation_overdue: { label: "Rotation overdue", tone: "warn" },
  vault_error: { label: "Vault error", tone: "danger" },
  error: { label: "Error", tone: "danger" },
  disabled: { label: "Disabled", tone: "neutral" },
  revoked: { label: "Revoked", tone: "neutral" },
  revoked_delete_pending: { label: "Delete pending", tone: "warn" },
};

export function secretHealth(health: string): { label: string; tone: Tone } {
  return SECRET_HEALTH[health] ?? { label: health.replace(/_/g, " "), tone: "neutral" };
}

export function summarizeSecrets(items: { health: string }[]): { total: number; healthy: number; attention: number } {
  const healthy = items.filter((i) => i.health === "healthy").length;
  const attention = items.filter((i) => ["rotation_overdue", "vault_error", "error", "revoked_delete_pending"].includes(i.health)).length;
  return { total: items.length, healthy, attention };
}

/** A Vault path is metadata, but a long one is shortened for the table (full path in the title). */
export function shortRef(ref: string, max = 48): string {
  if (ref.length <= max) return ref;
  return `…${ref.slice(ref.length - max + 1)}`;
}
