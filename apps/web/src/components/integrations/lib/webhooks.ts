/**
 * Outbound webhook form logic. The signing secret is supplied by the tenant, sent once, and never returned
 * by the API; the UI keeps it only in form state. Event types mirror SUBSCRIBABLE in
 * services/integration_service/integration_service/webhooks.py (the server validates again).
 */
import type { Endpoint } from "./types";

type Tone = "neutral" | "ok" | "warn" | "danger" | "brand";

export const EVENT_TYPES: { value: string; label: string }[] = [
  { value: "lead.created", label: "Lead created" },
  { value: "call.completed", label: "Call completed" },
  { value: "booking.created", label: "Booking created" },
  { value: "booking.confirmed", label: "Booking confirmed" },
  { value: "booking.canceled", label: "Booking canceled" },
  { value: "email.reply.sent", label: "Email reply sent" },
];

export const SECRET_MIN = 32;
export const SECRET_MAX = 256;

export type EndpointForm = { url: string; description: string; event_types: string[]; secret: string };

export function validateUrl(url: string): string | null {
  const v = url.trim();
  if (v.length < 10) return "Enter the full https:// URL of your receiver.";
  if (v.length > 2048) return "The URL is too long.";
  let u: URL;
  try {
    u = new URL(v);
  } catch {
    return "Enter a valid URL.";
  }
  if (u.protocol !== "https:") return "Webhooks are delivered over HTTPS only.";
  if (u.username || u.password) return "Do not put credentials in the URL; deliveries are signed instead.";
  return null;
}

export function validateSecret(secret: string): string | null {
  if (secret.length < SECRET_MIN) return `At least ${SECRET_MIN} characters.`;
  if (secret.length > SECRET_MAX) return `At most ${SECRET_MAX} characters.`;
  return null;
}

export function validateEndpointForm(form: EndpointForm, opts: { requireSecret: boolean }): Record<string, string> {
  const errors: Record<string, string> = {};
  const url = validateUrl(form.url);
  if (url) errors.url = url;
  if (form.event_types.length === 0) errors.event_types = "Choose at least one event.";
  if (form.description.length > 200) errors.description = "At most 200 characters.";
  if (opts.requireSecret) {
    const s = validateSecret(form.secret);
    if (s) errors.secret = s;
  }
  return errors;
}

/** A random 48-character signing secret (288 bits) the tenant copies into its receiver. */
export function generateSecret(random: (buf: Uint8Array) => Uint8Array = (b) => crypto.getRandomValues(b)): string {
  const bytes = random(new Uint8Array(36));
  let bin = "";
  for (const b of bytes) bin += String.fromCharCode(b);
  return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export type EndpointPatch = {
  expected_version: number;
  url?: string;
  description?: string | null;
  event_types?: string[];
  status?: "active" | "disabled";
};

/** Only changed fields; `null` when nothing changed. */
export function endpointPatch(original: Endpoint, form: Omit<EndpointForm, "secret">): EndpointPatch | null {
  const patch: EndpointPatch = { expected_version: original.version };
  const url = form.url.trim();
  if (url !== original.url) patch.url = url;
  const desc = form.description.trim();
  if (desc !== (original.description ?? "")) patch.description = desc || null;
  const a = [...form.event_types].sort();
  const b = [...original.event_types].sort();
  if (a.join(",") !== b.join(",")) patch.event_types = a;
  return Object.keys(patch).length > 1 ? patch : null;
}

const DELIVERY: Record<string, Tone> = {
  queued: "neutral",
  delivering: "brand",
  succeeded: "ok",
  failed: "danger",
  canceled: "neutral",
};

export function deliveryTone(status: string): Tone {
  return DELIVERY[status] ?? "neutral";
}

export function canRedeliver(status: string): boolean {
  return status === "failed" || status === "canceled";
}

export function attemptTone(outcome: string): Tone {
  if (outcome === "succeeded") return "ok";
  if (outcome === "blocked") return "warn";
  return "danger";
}

export function eventLabel(value: string): string {
  return EVENT_TYPES.find((e) => e.value === value)?.label ?? value;
}
