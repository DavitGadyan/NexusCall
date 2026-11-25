#!/usr/bin/env python3
"""DEVELOPMENT broker bootstrap: one user per service with least-privilege permissions.

Topic permissions restrict which routing keys (event types) each producer may
publish on `nexus.events`, so a compromised service cannot forge another
context's events (THREAT_MODEL B6). Consumers still re-validate resources.
"""

from __future__ import annotations

import os
import sys
import time

import httpx

from nexus_core.celery_app import QUEUES

API = os.environ.get("RABBITMQ_API", "http://rabbitmq:15672/api")
ADMIN = (os.environ["RABBITMQ_DEFAULT_USER"], os.environ["RABBITMQ_DEFAULT_PASS"])
SVC_PASSWORD = os.environ["NEXUS_BROKER_DEV_PASSWORD"]
VHOST = "nexus"

# service -> event-type prefixes it may publish (plus audit.event, which everyone emits)
PUBLISH: dict[str, list[str]] = {
    "tenant-service": ["tenant", "membership", "invitation", "settings", "notification"],
    "crm-service": ["lead", "contact", "suppression", "import"],
    "campaign-service": ["campaign"],
    "call-service": ["call"],
    "voice-service": ["voice"],
    "ai-agent-service": ["call\\.outcome", "email\\.reply_draft\\.generated"],
    "ai-gateway": ["ai"],
    "mcp-registry": ["ai\\.policy"],
    "tool-execution": ["ai\\.tool", "ai\\.security_event"],
    "booking-service": ["booking"],
    "email-service": ["email\\.message", "email\\.unsubscribed", "email\\.complaint", "notification"],
    "email-inbox-service": ["email\\.inbound", "email\\.reply_draft", "email\\.reply", "notification"],
    "integration-service": ["integration", "webhook"],
    "notification-service": [],
    "analytics-service": [],
    "audit-service": [],
}
TASK_QUEUES: dict[str, str] = {
    "campaign-service": "calls",
    "call-service": "calls",
    "voice-service": "voice",
    "email-service": "emails",
    "email-inbox-service": "emails",
    "crm-service": "imports",
    "integration-service": "integrations",
    "analytics-service": "analytics",
    "notification-service": "notifications",
    "tenant-service": "notifications",
}


def main() -> int:
    with httpx.Client(auth=ADMIN, timeout=5.0) as c:
        for _ in range(60):
            try:
                if c.get(f"{API}/overview").status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(2)
        else:
            print("rabbitmq not reachable", file=sys.stderr)
            return 1
        c.put(
            f"{API}/vhosts/{VHOST}", json={"description": "NexusCall", "default_queue_type": "quorum"}
        ).raise_for_status()
        c.put(
            f"{API}/permissions/{VHOST}/{ADMIN[0]}", json={"configure": ".*", "write": ".*", "read": ".*"}
        ).raise_for_status()
        for name, etype in (("nexus.events", "topic"), ("nexus.tasks", "topic"), ("nexus.dlx", "topic")):
            existing = c.get(f"{API}/exchanges/{VHOST}/{name}")
            if existing.status_code == 200 and existing.json().get("type") != etype:
                c.delete(f"{API}/exchanges/{VHOST}/{name}").raise_for_status()
            c.put(f"{API}/exchanges/{VHOST}/{name}", json={"type": etype, "durable": True}).raise_for_status()
        # Dead-letter queues for every task queue: <queue>.dlq (quorum), bound to nexus.dlx.
        for queue in QUEUES:
            dlq = f"{queue}.dlq"
            c.put(
                f"{API}/queues/{VHOST}/{dlq}", json={"durable": True, "arguments": {"x-queue-type": "quorum"}}
            ).raise_for_status()
            c.post(f"{API}/bindings/{VHOST}/e/nexus.dlx/q/{dlq}", json={"routing_key": dlq}).raise_for_status()
        c.put(
            f"{API}/policies/{VHOST}/task-dlq",
            json={
                "pattern": r"^(calls|voice|emails|integrations|imports|analytics|notifications)\.[a-z_]+$",
                "apply-to": "queues",
                "priority": 0,
                "definition": {"dead-letter-exchange": "nexus.dlx", "delivery-limit": 6},
            },
        ).raise_for_status()
        for svc, prefixes in PUBLISH.items():
            user = svc
            c.put(f"{API}/users/{user}", json={"password": SVC_PASSWORD, "tags": ""}).raise_for_status()
            q = TASK_QUEUES.get(svc)
            own = f"events\\.{svc.replace('-', '_')}.*"
            task_q = f"|{q}\\..*|celery.*|celeryev.*|.*\\.pidbox" if q else ""
            c.put(
                f"{API}/permissions/{VHOST}/{user}",
                json={
                    "configure": f"^({own}{task_q}|nexus\\.(events|tasks|dlx))$",
                    "write": f"^({own}{task_q}|nexus\\.(events|tasks|dlx)|amq\\.default)$",
                    "read": f"^({own}{task_q}|nexus\\.(events|tasks|dlx))$",
                },
            ).raise_for_status()
            write_keys = "|".join(["audit\\.event", *[f"{p}(\\..*)?" for p in prefixes]])
            c.put(
                f"{API}/topic-permissions/{VHOST}/{user}",
                json={
                    "exchange": "nexus.events",
                    "write": f"^({write_keys})$",
                    "read": ".*",
                },
            ).raise_for_status()
            print(f"broker user ready: {user}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
