"use client";
/** Fields of a prompt version (system prompt, greeting, disclosure, model settings, tools, knowledge, escalation). */
import { Plus, Trash2 } from "lucide-react";
import * as React from "react";
import { humanize } from "@/lib/format";
import { Button, Checkbox, Field, Input, Select, Textarea } from "@/components/ui";
import { ESCALATION_ACTIONS, TRIGGERS, VOICE_TOOL_IDS, type VersionForm } from "./lib/version-form";
import type { KnowledgeSource } from "./lib/types";

export function VersionFields({
  form,
  setForm,
  errors,
  kind,
  sources,
}: {
  form: VersionForm;
  setForm: React.Dispatch<React.SetStateAction<VersionForm>>;
  errors: Record<string, string | undefined>;
  kind: string;
  sources: KnowledgeSource[];
}) {
  const set = <K extends keyof VersionForm>(k: K, v: VersionForm[K]) => setForm((f) => ({ ...f, [k]: v }));
  const toggle = (k: "tool_ids" | "knowledge_source_ids", id: string, on: boolean) =>
    setForm((f) => ({ ...f, [k]: on ? [...new Set([...f[k], id])] : f[k].filter((x) => x !== id) }));
  return (
    <div className="flex flex-col gap-4">
      <Field id="v-system" label="System prompt" required hint="Instructions for the agent. Treated as trusted configuration; caller input never changes it." error={errors.system_prompt}>
        {(p) => <Textarea {...p} rows={10} className="font-mono text-xs" maxLength={20000} value={form.system_prompt} onChange={(e) => set("system_prompt", e.target.value)} />}
      </Field>
      <div className="grid gap-3 sm:grid-cols-2">
        <Field id="v-greeting" label="Greeting" error={errors.greeting}>
          {(p) => <Textarea {...p} rows={2} maxLength={1000} value={form.greeting} onChange={(e) => set("greeting", e.target.value)} />}
        </Field>
        <Field id="v-disclosure" label="AI disclosure" hint="Spoken at call start when the workspace requires disclosure." error={errors.disclosure_text}>
          {(p) => <Textarea {...p} rows={2} maxLength={1000} value={form.disclosure_text} onChange={(e) => set("disclosure_text", e.target.value)} />}
        </Field>
      </div>
      <div className="grid gap-3 sm:grid-cols-4">
        <Field id="v-model" label="Model" hint="Empty: workspace default." error={errors.model}>
          {(p) => <Input {...p} maxLength={80} value={form.model} onChange={(e) => set("model", e.target.value)} />}
        </Field>
        <Field id="v-temp" label="Temperature" hint="0–1, empty: default." error={errors.temperature}>
          {(p) => <Input {...p} inputMode="decimal" value={form.temperature} onChange={(e) => set("temperature", e.target.value)} />}
        </Field>
        {kind === "voice" ? (
          <>
            <Field id="v-voice" label="Voice ID" error={errors.voice_id}>
              {(p) => <Input {...p} maxLength={120} value={form.voice_id} onChange={(e) => set("voice_id", e.target.value)} />}
            </Field>
            <Field id="v-max" label="Max call (seconds)" error={errors.max_call_seconds}>
              {(p) => <Input {...p} inputMode="numeric" value={form.max_call_seconds} onChange={(e) => set("max_call_seconds", e.target.value)} />}
            </Field>
          </>
        ) : null}
      </div>
      {kind === "voice" ? (
        <fieldset>
          <legend className="mb-1 text-sm font-medium">Tools the agent may request</legend>
          <p className="mb-2 text-xs text-muted">The tool must also be granted on the Tool grants tab; the registry and the approval policy decide at call time.</p>
          <div className="grid gap-2 sm:grid-cols-2">
            {VOICE_TOOL_IDS.map((t) => (
              <Checkbox key={t} label={<code className="text-xs">{t}</code>} checked={form.tool_ids.includes(t)} onChange={(e) => toggle("tool_ids", t, e.target.checked)} />
            ))}
          </div>
          {errors.tool_ids ? <p className="mt-1 text-xs text-danger">{errors.tool_ids}</p> : null}
        </fieldset>
      ) : (
        <p className="text-xs text-muted">Email drafting agents never use tools.</p>
      )}
      <fieldset>
        <legend className="mb-1 text-sm font-medium">Knowledge sources</legend>
        {sources.length === 0 ? <p className="text-xs text-muted">No knowledge sources yet.</p> : (
          <div className="grid gap-2 sm:grid-cols-2">
            {sources.map((s) => (
              <Checkbox key={s.id} label={s.name} description={`${s.classification}${s.status !== "active" ? ` · ${s.status}` : ""}`} checked={form.knowledge_source_ids.includes(s.id)} onChange={(e) => toggle("knowledge_source_ids", s.id, e.target.checked)} />
            ))}
          </div>
        )}
        {errors.knowledge_source_ids ? <p className="mt-1 text-xs text-danger">{errors.knowledge_source_ids}</p> : null}
      </fieldset>
      {kind === "voice" ? (
        <fieldset className="flex flex-col gap-2">
          <legend className="mb-1 text-sm font-medium">Escalation rules</legend>
          {form.escalation_rules.map((r, i) => (
            <div key={i} className="grid gap-2 sm:grid-cols-[1fr_1fr_1fr_auto]">
              <label className="sr-only" htmlFor={`esc-t-${i}`}>Trigger</label>
              <Select id={`esc-t-${i}`} value={r.trigger} onChange={(e) => set("escalation_rules", form.escalation_rules.map((x, j) => (j === i ? { ...x, trigger: e.target.value } : x)))}>
                {TRIGGERS.map((t) => <option key={t} value={t}>{humanize(t)}</option>)}
              </Select>
              <label className="sr-only" htmlFor={`esc-a-${i}`}>Action</label>
              <Select id={`esc-a-${i}`} value={r.action} onChange={(e) => set("escalation_rules", form.escalation_rules.map((x, j) => (j === i ? { ...x, action: e.target.value } : x)))}>
                {ESCALATION_ACTIONS.map((a) => <option key={a} value={a}>{humanize(a)}</option>)}
              </Select>
              <label className="sr-only" htmlFor={`esc-k-${i}`}>Keyword</label>
              <Input id={`esc-k-${i}`} placeholder={r.trigger === "keyword" ? "Keyword" : "—"} disabled={r.trigger !== "keyword"} maxLength={60} value={r.keyword} aria-invalid={errors[`escalation_rules.${i}.keyword`] ? true : undefined} onChange={(e) => set("escalation_rules", form.escalation_rules.map((x, j) => (j === i ? { ...x, keyword: e.target.value } : x)))} />
              <Button type="button" size="sm" variant="ghost" aria-label="Remove rule" onClick={() => set("escalation_rules", form.escalation_rules.filter((_, j) => j !== i))}>
                <Trash2 className="h-4 w-4" aria-hidden />
              </Button>
              {errors[`escalation_rules.${i}.keyword`] ? <p className="text-xs text-danger sm:col-span-4">{errors[`escalation_rules.${i}.keyword`]}</p> : null}
            </div>
          ))}
          <div>
            <Button type="button" size="sm" variant="secondary" onClick={() => set("escalation_rules", [...form.escalation_rules, { trigger: "caller_request", action: "transfer_to_human", keyword: "" }])}>
              <Plus className="h-4 w-4" aria-hidden /> Rule
            </Button>
          </div>
        </fieldset>
      ) : null}
      <Field id="v-note" label="Change note" hint="Why this version exists. Shown in the history." error={errors.change_note}>
        {(p) => <Input {...p} maxLength={500} value={form.change_note} onChange={(e) => set("change_note", e.target.value)} />}
      </Field>
    </div>
  );
}
