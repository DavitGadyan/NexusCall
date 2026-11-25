"""Registry of typed payload models per (event_type, version).

Producers validate payloads on creation; consumers validate on receipt. Unknown
event types are permitted on the wire (forward compatibility) but consumers only
subscribe to types they know.
"""

from __future__ import annotations

from pydantic import BaseModel

from nexus_events import payloads as p

EVENT_REGISTRY: dict[tuple[str, int], type[BaseModel]] = {
    ("audit.event", 1): p.AuditEventPayload,
    ("tenant.created", 1): p.TenantRef,
    ("tenant.status_changed", 1): p.TenantStatusChanged,
    ("membership.changed", 1): p.MembershipChanged,
    ("invitation.created", 1): p.InvitationRef,
    ("invitation.accepted", 1): p.InvitationRef,
    ("invitation.revoked", 1): p.InvitationRef,
    ("settings.changed", 1): p.SettingsChanged,
    ("lead.created", 1): p.ContactRef,
    ("contact.updated", 1): p.ContactRef,
    ("suppression.added", 1): p.SuppressionAdded,
    ("import.completed", 1): p.ImportCompleted,
    ("campaign.launched", 1): p.CampaignRef,
    ("campaign.paused", 1): p.CampaignRef,
    ("campaign.resumed", 1): p.CampaignRef,
    ("campaign.canceled", 1): p.CampaignRef,
    ("campaign.kill_switch", 1): p.KillSwitch,
    ("call.state_changed", 1): p.CallStateChanged,
    ("call.completed", 1): p.CallCompleted,
    ("call.opted_out", 1): p.CallOptedOut,
    ("voice.session.ended", 1): p.VoiceSessionEnded,
    ("voice.handoff.requested", 1): p.CallRef,
    ("call.outcome.extracted", 1): p.CallOutcomeExtracted,
    ("booking.created", 1): p.BookingRef,
    ("booking.confirmed", 1): p.BookingRef,
    ("booking.canceled", 1): p.BookingRef,
    ("email.message.state_changed", 1): p.EmailMessageStateChanged,
    ("email.unsubscribed", 1): p.EmailAddressEvent,
    ("email.complaint", 1): p.EmailAddressEvent,
    ("email.inbound.received", 1): p.InboundMessageRef,
    ("email.reply_draft.requested", 1): p.InboundMessageRef,
    ("email.reply_draft.generated", 1): p.DraftRef,
    ("email.reply_draft.approved", 1): p.DraftRef,
    ("email.reply_draft.rejected", 1): p.DraftRef,
    ("email.reply.sent", 1): p.DraftRef,
    ("integration.connection.changed", 1): p.ConnectionChanged,
    ("webhook.delivery.failed", 1): p.WebhookDeliveryFailed,
    ("ai.request.completed", 1): p.AiRequestCompleted,
    ("ai.security_event", 1): p.AiSecurityEvent,
    ("ai.budget.exceeded", 1): p.AiBudgetExceeded,
    ("notification.requested", 1): p.NotificationRequested,
}


def payload_model_for(event_type: str, version: int) -> type[BaseModel] | None:
    return EVENT_REGISTRY.get((event_type, version))
