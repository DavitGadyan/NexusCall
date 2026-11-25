"""Every worker queue must be provisioned on the real broker, not only in mocked tests.

Unit tests mock RabbitMQ, so a queue that a worker consumes but the broker bootstrap does not cover
(no dead-letter queue, no delivery limit, or no permission for the owning service user) fails only
when the stack starts. This test makes that a CI failure.
"""

from __future__ import annotations

import importlib
import re
import sys
from pathlib import Path

import pytest

from nexus_core.celery_app import LATENCY_SENSITIVE, QUEUES

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))


@pytest.fixture(scope="module")
def topology(monkeypatch_module):
    for var in ("RABBITMQ_DEFAULT_USER", "RABBITMQ_DEFAULT_PASS", "NEXUS_BROKER_DEV_PASSWORD"):
        monkeypatch_module.setenv(var, "unused-in-this-test")
    bootstrap = importlib.import_module("bootstrap_rabbitmq")
    compose = importlib.import_module("gen_compose")
    return bootstrap, compose


@pytest.fixture(scope="module")
def monkeypatch_module():
    with pytest.MonkeyPatch.context() as mp:
        yield mp


def _worker_queues(compose) -> list[tuple[str, str, str]]:
    out = []
    for name, service, cmd, _profiles in compose.WORKERS:
        if "-Q" in cmd:
            for queue in cmd[cmd.index("-Q") + 1].split(","):
                out.append((name, service, queue))
    return out


def test_every_worker_queue_is_registered_and_dead_lettered(topology):
    bootstrap, compose = topology
    policy = re.compile(bootstrap.TASK_DLQ_POLICY_PATTERN)
    pairs = _worker_queues(compose)
    assert pairs, "no workers found in gen_compose.WORKERS"
    for worker, _service, queue in pairs:
        assert queue in QUEUES, f"{worker} consumes {queue}, which is not in nexus_core.celery_app.QUEUES (no DLQ)"
        assert policy.match(queue), f"{queue} is not covered by the dead-letter / delivery-limit policy"


def test_owning_service_may_use_its_worker_queues(topology):
    bootstrap, compose = topology
    for worker, service, queue in _worker_queues(compose):
        prefix = bootstrap.TASK_QUEUES.get(service)
        assert prefix and queue.startswith(
            f"{prefix}."
        ), f"broker user {service} has no permission on {queue} (consumed by {worker})"


def test_every_registered_queue_is_dead_lettered(topology):
    bootstrap, _compose = topology
    policy = re.compile(bootstrap.TASK_DLQ_POLICY_PATTERN)
    for queue in QUEUES:
        assert policy.match(queue), f"{queue} has no dead-letter policy"
    assert set(LATENCY_SENSITIVE) <= set(QUEUES)
