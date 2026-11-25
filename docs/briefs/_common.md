# Common brief for every build agent

You are a senior engineer building one part of **NexusCall**, a secure multi-tenant AI call-center platform. The stack is FastAPI microservices (one PostgreSQL database each, with forced row-level security), RabbitMQ + Celery, Redis, HashiCorp Vault, Keycloak and Next.js.

Your working directory is your own git worktree, branched from `build/platform`. Other engineers are building other services in parallel on this same machine right now. Stay inside your assigned paths.

## Read first (binding, in this order)
1. `docs/BUILD_CONVENTIONS.md`: shared helpers, mandatory patterns, ownership rules and the exact cross-service contracts. Follow it. If your brief and that file disagree, follow the file and say so in your report.
2. `build.md`: the product requirements. Your brief names the sections for you.
3. `docs/ARCHITECTURE.md`, `docs/SERVICE_MAP.md`, `docs/THREAT_MODEL.md`, and the ADRs in `docs/adr/` that touch your area.
4. The reference implementation `services/tenant_service/` (`tables.py`, `migrations/versions/0002_tenancy_iam.py`, `api/`, `tests/conftest.py`, `tests/test_tenancy_security.py`), and the modules you will use in `packages/nexus_core/nexus_core/`.

## Environment
- Python 3.12 with uv. Run `uv sync --all-packages` once.
- Docker is available for testcontainers only. The images `pgvector/pgvector:0.8.0-pg16` and `redis:7.4.1-alpine` are already pulled.
- Do not run docker compose, build images, or touch containers you did not start.
- The machine is shared. Use one PostgreSQL test container per test session (the fixture pattern in tenant_service does this), and do not leave containers running.
- No real provider credentials exist. Write real adapters against the providers' documented HTTP APIs and test them with mocked HTTP. Fakes make everything runnable locally.
- Do the work yourself. Do not delegate to subagents.

## Definition of done
- Explicit migrations generated with `scripts/gen_migration.py`, with forced RLS on every tenant-owned table.
- The API, events, workers, adapters, demo seed and tests your brief describes.
- All of these pass:
  - `uv run pytest services/<each of your packages> -q`
  - `uv run pytest tests/security/test_migrations.py -k "<your packages>" -q`
  - `uv run ruff check <your paths>`
  - `uv run ruff format <your paths>`
- Commit on your branch. Stage only your own paths, and never stage `uv.lock`. End the commit message with exactly these two lines:

  ```
  Co-Authored-By: Claude <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01GULYrYAAeFsycpFhEurrDv
  ```

  Do not push.

## Report
Use the format in BUILD_CONVENTIONS §7, in at most 600 words.

Be factual. Say plainly what is not implemented, not tested, or uncertain. Never weaken a security rule to make a test pass. Never claim regulatory compliance.
