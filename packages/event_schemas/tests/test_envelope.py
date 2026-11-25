import uuid

import pytest
from pydantic import ValidationError

from nexus_events import ActorRef, EventEnvelope, make_event


def test_make_event_validates_payload():
    ev = make_event(
        "campaign.launched",
        producer="campaign-service",
        actor=ActorRef(type="user", id="u1"),
        correlation_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        payload={"campaign_id": str(uuid.uuid4())},
    )
    assert ev.event_type == "campaign.launched"
    assert ev.payload["channel"] == "call"


def test_unknown_payload_field_rejected():
    with pytest.raises(ValidationError):
        make_event(
            "campaign.launched",
            producer="x",
            actor=ActorRef(type="system", id="s"),
            correlation_id=uuid.uuid4(),
            tenant_id=None,
            payload={"campaign_id": str(uuid.uuid4()), "email_body": "secret"},
        )


def test_envelope_rejects_bad_event_type():
    with pytest.raises(ValidationError):
        EventEnvelope(
            event_type="Bad Type", correlation_id=uuid.uuid4(), producer="x", actor=ActorRef(type="system", id="s")
        )


def test_round_trip_json():
    ev = make_event(
        "call.opted_out",
        producer="call",
        actor=ActorRef(type="system", id="s"),
        correlation_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        payload={"call_id": str(uuid.uuid4()), "contact_id": None},
    )
    again = EventEnvelope.model_validate_json(ev.model_dump_json())
    assert again == ev
