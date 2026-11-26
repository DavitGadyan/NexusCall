/** Inbound number form checks that mirror the call-service schema (the server validates again). */
export const E164 = /^\+[1-9]\d{6,14}$/;
export const NUMBER_SID = /^PN[0-9a-fA-F]{32}$/;

export type NumberForm = {
  e164: string;
  label: string;
  agent_id: string;
  business_hours_only: boolean;
  handoff_e164: string;
  provider_number_sid: string;
  use_for_outbound: boolean;
};

export function validateNumberForm(f: NumberForm, creating: boolean): Record<string, string> {
  const errors: Record<string, string> = {};
  if (creating && !E164.test(f.e164.trim())) errors.e164 = "Use international format, e.g. +14155550123.";
  if (!f.agent_id) errors.agent_id = "Choose the agent that answers this number.";
  if (f.handoff_e164.trim() && !E164.test(f.handoff_e164.trim())) errors.handoff_e164 = "Use international format, e.g. +14155550199.";
  if (creating && f.provider_number_sid.trim() && !NUMBER_SID.test(f.provider_number_sid.trim())) errors.provider_number_sid = "A Twilio number SID starts with PN followed by 32 hex characters.";
  if (f.label.length > 120) errors.label = "At most 120 characters.";
  return errors;
}
