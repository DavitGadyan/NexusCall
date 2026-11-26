# ADR-0010: Traefik as the API gateway / ingress

- Status: Accepted
- Date: 2026-11-25

## Decision
Traefik v3 runs as the edge router in both Compose (file provider) and Kubernetes (IngressRoute CRDs). It handles:
- TLS
- path routing (`/api/v1/<context>/*` → service; `/webhooks/<provider>/*` → owning service; `/media/*` → voice-service, WebSocket)
- rate limiting (per IP, plus per-route budgets)
- request-ID headers
- CORS (web origin only)
- security headers

User-facing authorization is **not** delegated to Traefik. Every service validates the JWT and checks permissions itself (build.md §3.1B).

Request body limits are **not** enforced by Traefik. Its `buffering` middleware also buffers responses: a
response with an empty body (a 303 redirect, a 204) becomes a 500 (`vulcand/oxy/buffer: no data ready`),
and streaming responses are held back. Every service enforces its own limit (`BodySizeLimitMiddleware` in
`nexus_core`: 1 MiB by default, 11 MiB for upload services), and the web BFF enforces its own.

## Alternatives
- Kong or Envoy: more features, more operational weight. Not needed for the MVP.
- A custom FastAPI gateway: adds another hop without adding security.

## Consequences
- Traefik is configuration rather than application code. It still gets its own container, config and health check.
