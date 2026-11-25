# ADR-0001: Record architecture decisions

- Status: Accepted
- Date: 2026-11-25

## Context
build.md §8 of the agent instructions requires us to record material decisions, and to document the options and a recommendation whenever a choice is ambiguous.

## Decision
We use lightweight ADRs (Nygard format) in `docs/adr/NNNN-title.md`. Each ADR has Status, Date, Context, Decision, Consequences, and Alternatives where relevant. A superseded ADR stays in the repo, marked `Superseded by ADR-XXXX`.

## Consequences
- Decisions can be reviewed in PRs.
- Assumptions marked as reversible defaults are revisited at phase boundaries.
