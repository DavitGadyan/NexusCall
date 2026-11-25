#!/usr/bin/env python3
"""Generate docker-compose.yml (DEVELOPMENT stack) from the service/worker tables below.

Profiles (ADR / plan): infra always starts; app services join named profiles so a
10 GB laptop can run subsets: `make up PROFILE="core crm"`; `make up-all` runs everything.
"""

from __future__ import annotations

from pathlib import Path

import yaml  # type: ignore[import-untyped]

ROOT = Path(__file__).resolve().parents[1]

# (compose name, python package, database, profiles, extra env)
SERVICES: list[tuple[str, str, str, list[str], dict[str, str]]] = [
    ("tenant-service", "tenant_service", "tenant", ["core"], {}),
    ("audit-service", "audit_service", "audit", ["core"], {}),
    ("notification-service", "notification_service", "notify", ["core"],
     {"MAILPIT_SMTP_HOST": "mailpit:1025", "NOTIFICATION_FROM_ADDRESS": "no-reply@nexus.local"}),
    ("crm-service", "crm_service", "crm", ["crm"], {}),
    ("campaign-service", "campaign_service", "campaign", ["crm"], {}),
    ("ai-agent-service", "ai_agent_service", "agent", ["crm", "calls", "email"], {}),
    ("ai-gateway", "ai_gateway", "aigw", ["crm", "calls", "email"], {}),
    ("mcp-registry", "mcp_registry", "mcpreg", ["calls", "email"], {"MCP_ALLOW_HTTP_PRIVATE": "true"}),
    ("tool-execution", "tool_execution", "toolexec", ["calls", "email"],
     {"MCP_ALLOW_HTTP_PRIVATE": "true", "APPROVAL_TTL_SECONDS": "1800"}),
    ("call-service", "call_service", "call", ["calls"], {}),
    ("voice-service", "voice_service", "voice", ["calls"], {}),
    ("booking-service", "booking_service", "booking", ["calls"], {}),
    ("email-service", "email_service", "email", ["email"], {}),
    ("email-inbox-service", "email_inbox_service", "inbox", ["email"], {}),
    ("integration-service", "integration_service", "integration", ["platform"], {}),
    ("analytics-service", "analytics_service", "analytics", ["platform"], {}),
]

# Workers reuse their owning service image (same code, different command). Added per phase.
# (name, owning compose service, command, profiles)
WORKERS: list[tuple[str, str, list[str], list[str]]] = [
    ("worker-imports", "crm-service",
     ["celery", "-A", "crm_service.worker", "worker", "-Q", "imports.process", "--concurrency", "2",
      "--without-heartbeat", "--without-gossip", "--without-mingle"], ["crm"]),
    ("worker-campaign", "campaign-service",
     ["celery", "-A", "campaign_service.worker", "worker", "-Q", "calls.schedule", "--concurrency", "2",
      "--without-heartbeat", "--without-gossip", "--without-mingle"], ["crm"]),
    ("worker-ai-agent", "ai-agent-service",
     ["celery", "-A", "ai_agent_service.worker", "worker", "-Q", "ai.knowledge_ingest", "--concurrency", "2",
      "--without-heartbeat", "--without-gossip", "--without-mingle"], ["crm", "calls", "email"]),
    ("worker-analytics", "analytics-service",
     ["celery", "-A", "analytics_service.worker:celery_app", "worker", "-Q", "analytics.aggregate", "--concurrency",
      "2", "--without-heartbeat", "--without-gossip", "--without-mingle"], ["platform"]),
]

# Outbox relays and event consumers run inside each service process (lifespan tasks) to keep the
# laptop footprint small; FOR UPDATE SKIP LOCKED makes multiple replicas safe.

ALL_PROFILES = ["core", "crm", "calls", "email", "platform", "obs", "fakes"]

PY_COMMON = {
    "restart": "unless-stopped",
    "read_only": True,
    "cap_drop": ["ALL"],
    "security_opt": ["no-new-privileges:true"],
    "tmpfs": ["/tmp:size=64m"],
    "networks": ["internal"],
    "mem_limit": "256m",
    "env_file": [".env"],
}


def py_env(name: str, db: str) -> dict[str, str]:
    return {
        "SERVICE_NAME": name,
        "ENV": "${ENV:-development}",
        "PROVIDER_MODE": "${PROVIDER_MODE:-fake}",
        "DATABASE_URL": f"postgresql+asyncpg://{db}_app:${{NEXUS_DB_DEV_PASSWORD}}@postgres:5432/{db}",
        "INSPECTOR_DATABASE_URL": f"postgresql+asyncpg://{db}_inspector:${{NEXUS_DB_DEV_PASSWORD}}@postgres:5432/{db}",
        "PLATFORM_DATABASE_URL": f"postgresql+asyncpg://{db}_platform:${{NEXUS_DB_DEV_PASSWORD}}@postgres:5432/{db}",
        "REDIS_URL": "redis://:${NEXUS_REDIS_DEV_PASSWORD}@redis:6379/0",
        "AMQP_URL": f"amqp://{name}:${{NEXUS_BROKER_DEV_PASSWORD}}@rabbitmq:5672/nexus",
        "OIDC_ISSUER": "${NEXUS_OIDC_PUBLIC_ISSUER}",
        "OIDC_JWKS_URL": "http://keycloak:8080/realms/nexus/protocol/openid-connect/certs",
        "OIDC_TOKEN_URL": "http://keycloak:8080/realms/nexus/protocol/openid-connect/token",
        "SERVICE_CLIENT_ID": f"svc-{name}",
        "SERVICE_CLIENT_SECRET": "${KC_DEV_SERVICE_CLIENT_SECRET}",
        "VAULT_ADDR": "http://vault:8200",
        "VAULT_ROLE_ID_FILE": f"/approle/{name}/role_id",
        "VAULT_SECRET_ID_FILE": f"/approle/{name}/secret_id",
        "FAKE_PROVIDERS_URL": "http://fake-providers:8000",
        "FAKE_PROVIDER_SECRET": "${FAKE_PROVIDER_SECRET}",
        "OTEL_EXPORTER_OTLP_ENDPOINT": "${OTEL_EXPORTER_OTLP_ENDPOINT:-}",
        "S3_ENDPOINT_URL": "http://objectstore:9000",
        "S3_PUBLIC_ENDPOINT_URL": "http://localhost:${NEXUS_PORT_S3:-47900}",
        "S3_ACCESS_KEY_ID": "${S3_DEV_ACCESS_KEY}",
        "S3_SECRET_ACCESS_KEY": "${S3_DEV_SECRET_KEY}",
        "PUBLIC_BASE_URL": "${NEXUS_PUBLIC_BASE_URL}",
    }


DEPENDS_APP = {
    "migrate": {"condition": "service_completed_successfully"},
    "rabbitmq-bootstrap": {"condition": "service_completed_successfully"},
    "vault-bootstrap": {"condition": "service_completed_successfully"},
    "redis": {"condition": "service_healthy"},
    "keycloak": {"condition": "service_healthy"},
}


def service_block(name: str, pkg: str, db: str, profiles: list[str], extra: dict[str, str]) -> dict:
    return {
        **PY_COMMON,
        "image": f"nexuscall/{name}:dev",
        "build": {"context": ".", "dockerfile": f"services/{pkg}/Dockerfile"},
        "profiles": profiles,
        "environment": {**py_env(name, db), **extra},
        "volumes": ["approle:/approle:ro"],
        "depends_on": DEPENDS_APP,
    }


def compose() -> dict:
    infra: dict[str, dict] = {
        "postgres": {
            "image": "pgvector/pgvector:0.8.0-pg16",
            "restart": "unless-stopped",
            "environment": {
                "POSTGRES_USER": "nexus_bootstrap",
                "POSTGRES_PASSWORD": "${POSTGRES_BOOTSTRAP_PASSWORD}",
                "POSTGRES_DB": "postgres",
                "NEXUS_DB_DEV_PASSWORD": "${NEXUS_DB_DEV_PASSWORD}",
            },
            "command": [
                "postgres",
                "-c",
                "max_connections=400",
                "-c",
                "shared_buffers=256MB",
                "-c",
                "log_min_duration_statement=500",
            ],
            "volumes": ["pgdata:/var/lib/postgresql/data", "./infra/postgres/init:/docker-entrypoint-initdb.d:ro"],
            "ports": ["127.0.0.1:${NEXUS_PORT_POSTGRES:-47432}:5432"],
            "networks": ["internal"],
            "mem_limit": "1g",
            "healthcheck": {
                "test": ["CMD-SHELL", "pg_isready -h 127.0.0.1 -U nexus_bootstrap -d audit"],
                "interval": "5s",
                "timeout": "3s",
                "retries": 30,
            },
        },
        "redis": {
            "image": "redis:7.4.1-alpine",
            "restart": "unless-stopped",
            "command": [
                "redis-server",
                "--requirepass",
                "${NEXUS_REDIS_DEV_PASSWORD}",
                "--maxmemory",
                "128mb",
                "--maxmemory-policy",
                "volatile-lru",
                "--appendonly",
                "no",
            ],
            "networks": ["internal"],
            "mem_limit": "192m",
            "healthcheck": {
                "test": ["CMD-SHELL", 'redis-cli -a "$$NEXUS_REDIS_DEV_PASSWORD" --no-auth-warning ping'],
                "interval": "5s",
                "retries": 20,
            },
            "environment": {"NEXUS_REDIS_DEV_PASSWORD": "${NEXUS_REDIS_DEV_PASSWORD}"},
        },
        "rabbitmq": {
            "image": "rabbitmq:4.0.5-management-alpine",
            "restart": "unless-stopped",
            "environment": {
                "RABBITMQ_DEFAULT_USER": "${RABBITMQ_DEFAULT_USER}",
                "RABBITMQ_DEFAULT_PASS": "${RABBITMQ_DEFAULT_PASS}",
            },
            "volumes": [
                "./infra/rabbitmq/rabbitmq.conf:/etc/rabbitmq/rabbitmq.conf:ro",
                "./infra/rabbitmq/definitions.json:/etc/rabbitmq/definitions.json:ro",
                "rabbitdata:/var/lib/rabbitmq",
            ],
            "ports": ["127.0.0.1:${NEXUS_PORT_RABBITMQ_UI:-47672}:15672"],
            "networks": ["internal"],
            "mem_limit": "512m",
            "healthcheck": {
                "test": ["CMD", "rabbitmq-diagnostics", "-q", "check_port_connectivity"],
                "interval": "10s",
                "timeout": "10s",
                "retries": 30,
            },
        },
        "rabbitmq-bootstrap": {
            "image": "nexuscall/tools:dev",
            "build": {"context": ".", "dockerfile": "infra/docker/tools.Dockerfile"},
            "command": ["scripts/bootstrap_rabbitmq.py"],
            "env_file": [".env"],
            "environment": {"RABBITMQ_API": "http://rabbitmq:15672/api"},
            "networks": ["internal"],
            "depends_on": {"rabbitmq": {"condition": "service_healthy"}},
            "restart": "no",
        },
        "vault": {
            "image": "hashicorp/vault:1.18.3",
            "restart": "unless-stopped",
            "cap_add": ["IPC_LOCK"],
            "command": ["server", "-dev", "-dev-listen-address=0.0.0.0:8200"],
            "environment": {"VAULT_DEV_ROOT_TOKEN_ID": "${VAULT_DEV_ROOT_TOKEN}", "SKIP_SETCAP": "true"},
            "ports": ["127.0.0.1:${NEXUS_PORT_VAULT:-47200}:8200"],
            "networks": ["internal"],
            "mem_limit": "256m",
            "healthcheck": {
                "test": ["CMD", "vault", "status", "-address=http://127.0.0.1:8200"],
                "interval": "5s",
                "retries": 20,
            },
        },
        "vault-bootstrap": {
            "image": "hashicorp/vault:1.18.3",
            "entrypoint": ["/bin/sh", "/bootstrap.sh"],
            "environment": {
                "VAULT_ADDR": "http://vault:8200",
                "VAULT_TOKEN": "${VAULT_DEV_ROOT_TOKEN}",
                "NEXUS_BROKER_OPS_PASSWORD": "${NEXUS_BROKER_OPS_PASSWORD}",
                "ENV": "${ENV:-development}",
            },
            "volumes": ["./infra/vault/bootstrap.sh:/bootstrap.sh:ro", "approle:/approle"],
            "user": "0:0",
            "networks": ["internal"],
            "depends_on": {"vault": {"condition": "service_healthy"}},
            "restart": "no",
        },
        "keycloak": {
            "image": "quay.io/keycloak/keycloak:26.0.7",
            "restart": "unless-stopped",
            "command": ["start-dev", "--import-realm"],
            "environment": {
                "KC_DB": "postgres",
                "KC_DB_URL": "jdbc:postgresql://postgres:5432/keycloak",
                "KC_DB_USERNAME": "keycloak",
                "KC_DB_PASSWORD": "${NEXUS_DB_DEV_PASSWORD}",
                "KC_HOSTNAME": "${NEXUS_OIDC_PUBLIC_BASE}",
                "KC_HOSTNAME_BACKCHANNEL_DYNAMIC": "true",
                "KC_HTTP_ENABLED": "true",
                "KC_HEALTH_ENABLED": "true",
                "KC_BOOTSTRAP_ADMIN_USERNAME": "${KC_BOOTSTRAP_ADMIN_USERNAME}",
                "KC_BOOTSTRAP_ADMIN_PASSWORD": "${KC_BOOTSTRAP_ADMIN_PASSWORD}",
                "KC_DEV_USER_PASSWORD": "${KC_DEV_USER_PASSWORD}",
                "KC_DEV_SERVICE_CLIENT_SECRET": "${KC_DEV_SERVICE_CLIENT_SECRET}",
                "KC_WEB_BFF_CLIENT_SECRET": "${KC_WEB_BFF_CLIENT_SECRET}",
                "NEXUS_WEB_ORIGIN": "${NEXUS_PUBLIC_BASE_URL}",
                "JAVA_OPTS_KC_HEAP": "-XX:MaxRAMPercentage=65",
            },
            "volumes": ["./infra/keycloak/realm-nexus.json:/opt/keycloak/data/import/realm-nexus.json:ro"],
            "ports": ["127.0.0.1:${NEXUS_PORT_KEYCLOAK:-47080}:8080"],
            "networks": ["internal"],
            "mem_limit": "900m",
            "depends_on": {"postgres": {"condition": "service_healthy"}},
            "healthcheck": {
                "test": [
                    "CMD-SHELL",
                    "exec 3<>/dev/tcp/127.0.0.1/9000 && printf 'GET /health/ready HTTP/1.1\\r\\n"
                    "Host: localhost\\r\\nConnection: close\\r\\n\\r\\n' >&3 && grep -q UP <&3",
                ],
                "interval": "10s",
                "timeout": "5s",
                "retries": 40,
                "start_period": "30s",
            },
        },
        "objectstore": {
            # SeaweedFS (Apache-2.0) S3 gateway. DEV ONLY; production uses any S3-compatible store (ADR-0013).
            "image": "chrislusf/seaweedfs:3.80",
            "restart": "unless-stopped",
            "entrypoint": [
                "/bin/sh",
                "-c",
                'printf \'{"identities":[{"name":"nexus","credentials":[{"accessKey":"%s",\''
                '\'"secretKey":"%s"}],"actions":["Admin","Read","Write","List","Tagging"]}]}\' '
                '"$$S3_DEV_ACCESS_KEY" "$$S3_DEV_SECRET_KEY" > /tmp/s3.json && '
                "exec weed server -dir=/data -s3 -s3.port=9000 -s3.config=/tmp/s3.json -master.volumeSizeLimitMB=256 "
                "-volume.max=8 -ip.bind=0.0.0.0",
            ],
            "environment": {"S3_DEV_ACCESS_KEY": "${S3_DEV_ACCESS_KEY}", "S3_DEV_SECRET_KEY": "${S3_DEV_SECRET_KEY}"},
            "volumes": ["objectdata:/data"],
            # Published on loopback only so browsers can fetch short-lived presigned URLs in development.
            "ports": ["127.0.0.1:${NEXUS_PORT_S3:-47900}:9000"],
            "networks": ["internal"],
            "mem_limit": "384m",
            "healthcheck": {
                "test": ["CMD-SHELL", "wget -q -O /dev/null http://127.0.0.1:9000/healthz || exit 1"],
                "interval": "5s",
                "retries": 30,
            },
        },
        "objectstore-init": {
            "image": "amazon/aws-cli:2.22.20",
            "entrypoint": [
                "/bin/sh",
                "-c",
                "for b in recordings transcripts attachments raw-mail imports exports knowledge; do "
                "aws --endpoint-url http://objectstore:9000 s3api head-bucket --bucket $$b 2>/dev/null || "
                "aws --endpoint-url http://objectstore:9000 s3api create-bucket --bucket $$b; done",
            ],
            "environment": {
                "AWS_ACCESS_KEY_ID": "${S3_DEV_ACCESS_KEY}",
                "AWS_SECRET_ACCESS_KEY": "${S3_DEV_SECRET_KEY}",
                "AWS_DEFAULT_REGION": "us-east-1",
            },
            "networks": ["internal"],
            "depends_on": {"objectstore": {"condition": "service_healthy"}},
            "restart": "no",
        },
        "migrate": {
            "image": "nexuscall/tools:dev",
            "build": {"context": ".", "dockerfile": "infra/docker/tools.Dockerfile"},
            "command": ["scripts/migrate_all.py"],
            "environment": {
                "MIGRATION_DB_HOST": "postgres",
                "MIGRATION_DB_PASSWORD": "${NEXUS_DB_DEV_PASSWORD}",
                "ENV": "${ENV:-development}",
            },
            "networks": ["internal"],
            "depends_on": {"postgres": {"condition": "service_healthy"}},
            "restart": "no",
        },
        "traefik": {
            "image": "traefik:v3.2.3",
            "restart": "unless-stopped",
            "command": ["--configFile=/etc/traefik/traefik.yml"],
            "volumes": [
                "./infra/traefik/traefik.yml:/etc/traefik/traefik.yml:ro",
                "./infra/traefik/dynamic.yml:/etc/traefik/dynamic.yml:ro",
            ],
            "ports": ["127.0.0.1:${NEXUS_PORT_WEB:-47000}:80"],
            "networks": ["internal", "edge"],
            "mem_limit": "128m",
            "healthcheck": {"test": ["CMD", "traefik", "healthcheck", "--ping"], "interval": "10s", "retries": 10},
        },
        "web": {
            "image": "nexuscall/web:dev",
            "build": {"context": ".", "dockerfile": "apps/web/Dockerfile"},
            "restart": "unless-stopped",
            "read_only": True,
            "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges:true"],
            "tmpfs": ["/tmp:size=64m", "/srv/apps/web/.next/cache:size=64m,uid=10001"],
            "profiles": ["core"],
            "env_file": [".env"],
            "environment": {
                "NEXUS_OIDC_INTERNAL_BASE": "http://keycloak:8080/realms/nexus",
                # The BFF proxies /api/v1/* through the gateway on the internal network.
                "NEXUS_API_INTERNAL_URL": "http://traefik:8081",
                "REDIS_URL": "redis://:${NEXUS_REDIS_DEV_PASSWORD}@redis:6379/1",
                **{f"SVC_{n.replace('-', '_').upper()}_URL": f"http://{n}:8000" for n, *_ in SERVICES},
            },
            "networks": ["internal"],
            "mem_limit": "384m",
            "depends_on": {"keycloak": {"condition": "service_healthy"}, "redis": {"condition": "service_healthy"}},
        },
        "fake-providers": {
            **PY_COMMON,
            "image": "nexuscall/fake-providers:dev",
            "build": {"context": ".", "dockerfile": "services/fake_providers/Dockerfile"},
            "profiles": ["fakes", "calls", "email", "crm"],
            "environment": {
                "ENV": "development",
                "SERVICE_NAME": "fake-providers",
                "FAKE_PROVIDER_SECRET": "${FAKE_PROVIDER_SECRET}",
                "NEXUS_PUBLIC_BASE_URL": "${NEXUS_PUBLIC_BASE_URL}",
            },
            "mem_limit": "192m",
        },
        "mailpit": {
            "image": "axllent/mailpit:v1.21.8",
            "profiles": ["email", "core"],
            "ports": ["127.0.0.1:${NEXUS_PORT_MAILPIT:-47025}:8025"],
            "networks": ["internal"],
            "mem_limit": "96m",
        },
        "clamav": {
            "image": "clamav/clamav:1.4.1",
            "profiles": ["email"],
            "networks": ["internal"],
            "mem_limit": "1536m",
            "healthcheck": {"test": ["CMD", "clamdcheck.sh"], "interval": "30s", "retries": 20, "start_period": "120s"},
        },
        "otel-collector": {
            "image": "otel/opentelemetry-collector-contrib:0.116.1",
            "profiles": ["obs"],
            "command": ["--config=/etc/otel/collector.yml"],
            "volumes": ["./infra/otel/collector.yml:/etc/otel/collector.yml:ro"],
            "networks": ["internal"],
            "mem_limit": "192m",
        },
        "tempo": {
            "image": "grafana/tempo:2.6.1",
            "profiles": ["obs"],
            "command": ["-config.file=/etc/tempo.yml"],
            "volumes": ["./infra/otel/tempo.yml:/etc/tempo.yml:ro", "tempodata:/var/tempo"],
            "networks": ["internal"],
            "mem_limit": "256m",
        },
        "loki": {
            "image": "grafana/loki:3.3.2",
            "profiles": ["obs"],
            "networks": ["internal"],
            "mem_limit": "256m",
        },
        "prometheus": {
            "image": "prom/prometheus:v3.0.1",
            "profiles": ["obs"],
            "volumes": ["./infra/prometheus/prometheus.yml:/etc/prometheus/prometheus.yml:ro"],
            "networks": ["internal"],
            "mem_limit": "256m",
        },
        "grafana": {
            "image": "grafana/grafana:11.4.0",
            "profiles": ["obs"],
            "environment": {
                "GF_SECURITY_ADMIN_USER": "${GRAFANA_ADMIN_USER}",
                "GF_SECURITY_ADMIN_PASSWORD": "${GRAFANA_ADMIN_PASSWORD}",
                "GF_USERS_ALLOW_SIGN_UP": "false",
                "GF_ANALYTICS_REPORTING_ENABLED": "false",
            },
            "volumes": [
                "./infra/grafana/provisioning:/etc/grafana/provisioning:ro",
                "./infra/grafana/dashboards:/var/lib/grafana/dashboards:ro",
            ],
            "ports": ["127.0.0.1:${NEXUS_PORT_GRAFANA:-47001}:3000"],
            "networks": ["internal"],
            "mem_limit": "192m",
        },
    }
    services = dict(infra)
    for name, pkg, db, profiles, extra in SERVICES:
        services[name] = service_block(name, pkg, db, profiles, extra)
    by_name = {s[0]: s for s in SERVICES}
    for wname, owner, command, profiles in WORKERS:
        _, pkg, db, _, extra = by_name[owner]
        services[wname] = {
            **PY_COMMON,
            "mem_limit": "256m",
            "image": f"nexuscall/{owner}:dev",
            "profiles": profiles,
            "command": command,
            "environment": {**py_env(owner, db), **extra},
            "volumes": ["approle:/approle:ro"],
            "depends_on": {**DEPENDS_APP, owner: {"condition": "service_started"}},
            "healthcheck": {"disable": True},
        }
    return {
        "name": "nexuscall",
        "services": services,
        "networks": {"internal": {"driver": "bridge"}, "edge": {"driver": "bridge"}},
        "volumes": {v: {} for v in ("pgdata", "rabbitdata", "objectdata", "tempodata", "approle")},
    }


HEADER = """# GENERATED by scripts/gen_compose.py — DEVELOPMENT ONLY. Do not edit by hand.
# All host ports bind to 127.0.0.1. Production uses Helm (infra/helm) with Vault Agent,
# NetworkPolicies and managed secrets; nothing here is production configuration.
"""


def main() -> None:
    text = yaml.safe_dump(compose(), sort_keys=False, width=140)
    (ROOT / "docker-compose.yml").write_text(HEADER + text)
    print("wrote docker-compose.yml")


if __name__ == "__main__":
    main()
