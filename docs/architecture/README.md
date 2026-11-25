# Architecture data (machine-readable)

`architecture.graph.json` is the single machine-readable description of NexusCall. It is meant to feed an interactive mind-map or architecture-explorer UI, such as the portfolio app.

| Key | Use it for |
|---|---|
| `layers` | Swim-lanes and colour groups (edge → identity → core → comms → AI → platform → workers → data → security → observability → external) |
| `nodes` | Every actor, service, worker, datastore and provider, with `summary`, `why`, `tech`, `owns`, `security`, `scaling`, `adr` and `phase` for detail panels |
| `edges` | The static dependency graph. `kind` is one of `sync`, `async`, `webhook`, `stream` or `data`, so you can style arrows differently |
| `flows` | Ordered, animatable user journeys. Each `step` is `from → to` plus an action, and can carry `detail` and `security` callouts. Highlight the active edge step by step |
| `mindmap` | A tree for the mind-map view. Leaves link back to graph nodes (`nodeRefs`) or flows (`flowRefs`) |

Rules:
- Every `from`, `to`, `layer`, `nodeRefs` and `flowRefs` value must resolve. The Phase 1 CI checks this against `architecture.schema.json` and the reference checks.
- The file is updated at the end of each phase. As services gain real endpoints, `phase` and `status` track progress.

Flows included: login, invite_user, outbound_call, inbound_call, email_campaign, ai_email_reply, provider_connection, tool_call_authz, break_glass.
