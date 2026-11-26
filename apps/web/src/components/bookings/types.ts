/** Response shapes of booking-service (contracts/openapi/booking_service.json). */
export type BookingStatus = "tentative" | "confirmed" | "canceled" | "completed" | "no_show";

export type Booking = {
  id: string;
  contact_id: string;
  resource_id: string;
  start: string;
  end: string;
  status: BookingStatus | string;
  source_type: string;
  cancel_reason: string | null;
  hold_expires_at: string | null;
  calendar_sync_state: string | null;
  external_event_id: string | null;
  version: number;
  created_at: string;
  updated_at: string;
};

export type Resource = {
  id: string;
  name: string;
  kind: "calendar" | "staff" | string;
  status: "active" | "inactive" | string;
  timezone: string;
  calendar_provider: string | null;
  calendar_id: string | null;
  calendar_connection_id: string | null;
  version: number;
  created_at: string;
  updated_at: string;
};

export type Rule = {
  id?: string;
  weekday: number;
  start_time: string;
  end_time: string;
  slot_minutes: number;
  buffer_before_minutes: number;
  buffer_after_minutes: number;
  min_notice_minutes: number;
};

export type RulesOut = { resource_id: string; resource_version: number; rules: Rule[] };
export type Slot = { start: string; end: string; resource_id: string };
export type Availability = { slots: Slot[]; calendar_unavailable: string[] };

export const CANCEL_REASONS = ["requested", "rescheduled_elsewhere", "no_longer_needed", "duplicate", "staff_unavailable", "other"] as const;
export const BOOKING_STATUSES: BookingStatus[] = ["tentative", "confirmed", "completed", "no_show", "canceled"];
