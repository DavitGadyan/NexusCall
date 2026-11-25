"""TwiML responses. Every dynamic value is XML-escaped; caller-supplied text never reaches TwiML."""

from __future__ import annotations

from xml.sax.saxutils import escape

from fastapi.responses import Response

REJECT_UNKNOWN = "Sorry, this number is not in service. Goodbye."
REJECT_UNAVAILABLE = "Sorry, we are unable to take your call right now. Please try again later. Goodbye."
REJECT_AFTER_HOURS = "Thank you for calling. We are currently closed. Please call again during business hours. Goodbye."


def _attr(value: str) -> str:
    return '"' + escape(value, {'"': "&quot;", "'": "&apos;"}) + '"'


def _response(body: str) -> Response:
    xml = f'<?xml version="1.0" encoding="UTF-8"?><Response>{body}</Response>'
    return Response(xml, media_type="application/xml")


def connect_stream(stream_url: str, token: str) -> Response:
    """`<Connect><Stream>` to voice-service. The token travels as a stream parameter, never in the URL."""
    return _response(
        f"<Connect><Stream url={_attr(stream_url)}>"
        f'<Parameter name="token" value={_attr(token)}/>'
        "</Stream></Connect>"
    )


def say_and_hangup(message: str) -> Response:
    return _response(f"<Say>{escape(message)}</Say><Hangup/>")


def hangup() -> Response:
    return _response("<Hangup/>")


def conference(name: str, announcement: str | None = None) -> str:
    """TwiML (string) that moves a live call into a human-handoff conference."""
    say = f"<Say>{escape(announcement)}</Say>" if announcement else ""
    return (
        f'<?xml version="1.0" encoding="UTF-8"?><Response>{say}<Dial><Conference startConferenceOnEnter="false" '
        f'endConferenceOnExit="true">{escape(name)}</Conference></Dial></Response>'
    )
