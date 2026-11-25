"""Rate limits, concurrency slots, periodic-job locking (real Redis) and object storage (real S3 API)."""

import asyncio
import time
import uuid
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from redis.asyncio import Redis

from nexus_core.circuit import CircuitBreaker
from nexus_core.ratelimit import acquire_slot, allow_fixed_window, release_slot, slots_in_use, take_token
from nexus_core.scheduling import PeriodicJob, run_once_locked
from nexus_core.storage import ObjectStore, tenant_key

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]


@pytest.fixture(scope="module")
def redis_container():
    tc = pytest.importorskip("testcontainers.redis")
    try:
        container = tc.RedisContainer("redis:7.4.1-alpine")
        container.start()
    except Exception as exc:  # noqa: BLE001 - skip when Docker is unavailable
        pytest.skip(f"Docker unavailable: {exc}")
    yield container
    container.stop()


@pytest_asyncio.fixture(loop_scope="module")
async def redis(redis_container):
    client = Redis(host=redis_container.get_container_host_ip(), port=int(redis_container.get_exposed_port(6379)))
    yield client
    await client.aclose()


async def test_fixed_window(redis):
    key = f"t:{uuid.uuid4()}"
    assert [await allow_fixed_window(redis, key, 3, 60) for _ in range(5)] == [True, True, True, False, False]


async def test_token_bucket_refills(redis):
    key = f"t:{uuid.uuid4()}"
    assert await take_token(redis, key, rate_per_second=10, burst=2) == 0.0
    assert await take_token(redis, key, rate_per_second=10, burst=2) == 0.0
    wait = await take_token(redis, key, rate_per_second=10, burst=2)
    assert 0 < wait <= 0.11
    await asyncio.sleep(wait + 0.02)
    assert await take_token(redis, key, rate_per_second=10, burst=2) == 0.0


async def test_concurrency_slots_are_bounded_idempotent_and_expire(redis):
    key = f"t:{uuid.uuid4()}"
    assert await acquire_slot(redis, key, "call-1", max_slots=2, ttl_seconds=60)
    assert await acquire_slot(redis, key, "call-1", max_slots=2, ttl_seconds=60)  # same holder: no double count
    assert await acquire_slot(redis, key, "call-2", max_slots=2, ttl_seconds=60)
    assert not await acquire_slot(redis, key, "call-3", max_slots=2, ttl_seconds=60)
    assert await slots_in_use(redis, key) == 2
    await release_slot(redis, key, "call-1")
    assert await acquire_slot(redis, key, "call-3", max_slots=2, ttl_seconds=60)
    # A crashed holder's lease expires.
    key2 = f"t:{uuid.uuid4()}"
    assert await acquire_slot(redis, key2, "dead", max_slots=1, ttl_seconds=1)
    await asyncio.sleep(1.1)
    assert await acquire_slot(redis, key2, "alive", max_slots=1, ttl_seconds=60)


async def test_concurrent_acquire_never_exceeds_limit(redis):
    key = f"t:{uuid.uuid4()}"
    results = await asyncio.gather(*(acquire_slot(redis, key, f"m{i}", max_slots=5, ttl_seconds=60) for i in range(40)))
    assert sum(results) == 5


async def test_circuit_breaker_shared_state(redis):
    name = f"prov-{uuid.uuid4()}"
    a, b = (CircuitBreaker(redis, name, failure_threshold=3, open_seconds=30) for _ in range(2))
    for _ in range(3):
        await a.record_failure()
    assert await b.state() == "open"  # another replica sees it
    await b.record_success()
    assert await a.state() == "closed"


async def test_periodic_job_runs_once_across_replicas(redis):
    ran: list[float] = []

    async def job(_app):
        ran.append(time.time())
        await asyncio.sleep(0.2)

    app = SimpleNamespace(
        state=SimpleNamespace(redis=redis, settings=SimpleNamespace(service_name=f"s-{uuid.uuid4()}"))
    )
    spec = PeriodicJob("tick", interval_seconds=30, func=job)
    results = await asyncio.gather(*(run_once_locked(app, spec) for _ in range(5)))
    assert sum(results) == 1 and len(ran) == 1
    assert await run_once_locked(app, spec) is True  # lock released after the run


# ------------------------------------------------------------------------------------ object storage
@pytest.fixture(scope="module")
def s3():
    from testcontainers.core.container import DockerContainer
    from testcontainers.core.waiting_utils import wait_for_logs

    try:
        container = (
            DockerContainer("chrislusf/seaweedfs:3.80")
            .with_command("server -dir=/data -s3 -s3.port=8333 -master.volumeSizeLimitMB=64 -ip.bind=0.0.0.0")
            .with_exposed_ports(8333)
        )
        container.start()
        wait_for_logs(container, "Start Seaweed S3 API Server", timeout=90)
    except Exception as exc:  # noqa: BLE001 - skip when Docker is unavailable
        pytest.skip(f"Object store container unavailable: {exc}")
    endpoint = f"http://{container.get_container_host_ip()}:{container.get_exposed_port(8333)}"
    yield endpoint
    container.stop()


async def test_object_store_put_get_presign_delete(s3):
    store = ObjectStore(endpoint_url=s3, access_key="any", secret_key="any")
    from aiobotocore.session import get_session

    for _ in range(30):  # volume servers register a moment after the S3 gateway starts
        try:
            async with get_session().create_client(
                "s3", endpoint_url=s3, region_name="us-east-1", aws_access_key_id="any", aws_secret_access_key="any"
            ) as c:
                await c.create_bucket(Bucket="attachments")
            break
        except Exception:  # noqa: BLE001 - retry until the store is ready
            await asyncio.sleep(1)
    tenant = uuid.uuid4()
    key = tenant_key(tenant, "inbox", "report.pdf")
    await store.put("attachments", key, b"%PDF-demo", content_type="application/pdf")
    assert await store.exists("attachments", key)
    assert await store.get("attachments", key) == b"%PDF-demo"
    url = await store.presign_get("attachments", key, expires_seconds=60, download_filename="report (final).pdf")
    assert "X-Amz-Signature" in url and "X-Amz-Expires=60" in url
    async with httpx.AsyncClient() as client:
        resp = await client.get(url)
    assert resp.status_code == 200 and resp.content == b"%PDF-demo"
    await store.delete("attachments", key)
    assert not await store.exists("attachments", key)
