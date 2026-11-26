/**
 * Prompt version form (ai_agent_service `VersionIn`). Versions are immutable: "editing" always starts a new
 * version from a copy of an existing one, which takes effect only when activated.
 */
import { z } from "zod";
import type { AgentVersion, EscalationRule } from "./types";

export const TRIGGERS = ["caller_request", "sensitive_topic", "complaint", "legal", "repeated_failure", "keyword"] as const;
export const ESCALATION_ACTIONS = ["transfer_to_human", "end_call", "flag_for_review"] as const;
/** Tools a voice agent's configuration may reference (ai_agent_service domain.VOICE_TOOLS); email agents use none. */
export const VOICE_TOOL_IDS = ["knowledge.search", "booking.find_slots", "booking.create_tentative", "crm.get_contact", "crm.add_note", "call.transfer_to_human"] as const;

export type VersionForm = {
  system_prompt: string;
  greeting: string;
  disclosure_text: string;
  change_note: string;
  model: string;
  temperature: string;
  voice_id: string;
  max_call_seconds: string;
  tool_ids: string[];
  knowledge_source_ids: string[];
  escalation_rules: { trigger: string; action: string; keyword: string }[];
};

export function emptyVersionForm(): VersionForm {
  return {
    system_prompt: "",
    greeting: "",
    disclosure_text: "",
    change_note: "",
    model: "",
    temperature: "",
    voice_id: "",
    max_call_seconds: "600",
    tool_ids: [],
    knowledge_source_ids: [],
    escalation_rules: [],
  };
}

export function versionToForm(v: AgentVersion): VersionForm {
  return {
    system_prompt: v.system_prompt,
    greeting: v.greeting,
    disclosure_text: v.disclosure_text,
    change_note: "",
    model: v.model ?? "",
    temperature: v.temperature === null ? "" : String(v.temperature),
    voice_id: v.voice_id ?? "",
    max_call_seconds: String(v.max_call_seconds),
    tool_ids: [...v.tool_ids],
    knowledge_source_ids: [...v.knowledge_source_ids],
    escalation_rules: v.escalation_rules.map((r: EscalationRule) => ({ trigger: r.trigger, action: r.action, keyword: r.keyword ?? "" })),
  };
}

const schema = z
  .object({
    system_prompt: z.string().trim().min(20, "At least 20 characters").max(20000, "At most 20 000 characters"),
    greeting: z.string().max(1000, "At most 1000 characters"),
    disclosure_text: z.string().max(1000, "At most 1000 characters"),
    change_note: z.string().max(500, "At most 500 characters"),
    model: z
      .string()
      .trim()
      .refine((v) => v === "" || /^[a-z0-9][a-z0-9.-]{2,79}$/.test(v), "Lowercase model name, e.g. default-chat")
      .transform((v) => v || null),
    temperature: z
      .string()
      .trim()
      .refine((v) => v === "" || (/^\d*\.?\d+$/.test(v) && Number(v) >= 0 && Number(v) <= 1), "Between 0 and 1")
      .transform((v) => (v === "" ? null : Number(v))),
    voice_id: z
      .string()
      .trim()
      .refine((v) => v === "" || /^[A-Za-z0-9_.-]{1,120}$/.test(v), "Letters, digits, . _ - only")
      .transform((v) => v || null),
    max_call_seconds: z
      .string()
      .trim()
      .regex(/^\d+$/, "Whole seconds")
      .transform(Number)
      .refine((n) => n >= 30 && n <= 3600, "Between 30 and 3600 seconds"),
    tool_ids: z.array(z.string()),
    knowledge_source_ids: z.array(z.string().uuid()),
    escalation_rules: z.array(
      z.object({ trigger: z.enum(TRIGGERS), action: z.enum(ESCALATION_ACTIONS), keyword: z.string().trim().max(60, "At most 60 characters") }),
    ),
  })
  .superRefine((v, ctx) => {
    v.escalation_rules.forEach((r, i) => {
      if (r.trigger === "keyword" && !r.keyword) ctx.addIssue({ code: "custom", path: ["escalation_rules", i, "keyword"], message: "Enter the keyword" });
    });
  });

export type VersionBody = Omit<z.output<typeof schema>, "escalation_rules"> & { escalation_rules: EscalationRule[] };

export function buildVersion(form: VersionForm, kind: string): { body?: VersionBody; errors: Record<string, string> } {
  const r = schema.safeParse(form);
  const errors: Record<string, string> = {};
  if (!r.success) {
    for (const issue of r.error.issues) {
      const key = issue.path.join(".") || "_form";
      if (!errors[key]) errors[key] = issue.message;
    }
    return { errors };
  }
  const bad = r.data.tool_ids.filter((t) => kind !== "voice" || !(VOICE_TOOL_IDS as readonly string[]).includes(t));
  if (bad.length) return { errors: { tool_ids: `Not allowed for ${kind} agents: ${bad.join(", ")}` } };
  return {
    body: {
      ...r.data,
      tool_ids: [...new Set(r.data.tool_ids)].sort(),
      knowledge_source_ids: [...new Set(r.data.knowledge_source_ids)],
      escalation_rules: r.data.escalation_rules.map((e) => ({ trigger: e.trigger, action: e.action, keyword: e.trigger === "keyword" ? e.keyword : null })),
    },
    errors,
  };
}

/** Local errors win; API field errors are accepted with or without a body prefix (e.g. `config.`). */
export function mergeErrors(local: Record<string, string>, api: Record<string, string> | undefined, prefix = ""): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [k, v] of Object.entries(api ?? {})) out[prefix && k.startsWith(prefix) ? k.slice(prefix.length) : k] = v;
  return { ...out, ...local };
}
