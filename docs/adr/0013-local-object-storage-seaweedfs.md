# ADR-0013: SeaweedFS for local S3-compatible object storage

- Status: Accepted
- Date: 2026-11-25
- Supersedes: the "MinIO locally" note in build.md §2 / ARCHITECTURE.md

## Context
MinIO no longer publishes its container images on public registries, so pinned `minio/minio` tags fail to pull. We still need S3-compatible storage locally for recordings, transcripts, attachments, imports, exports and knowledge files.

## Decision
- Local development uses **SeaweedFS** (Apache-2.0, `chrislusf/seaweedfs:3.80`) with its S3 gateway on `objectstore:9000`.
- Buckets are created by an `aws-cli` one-off container.
- Services talk to storage only through the S3 API (`S3_ENDPOINT_URL`), with private buckets and short-lived presigned URLs.
- Production uses any S3-compatible store (Ceph RGW, SeaweedFS, MinIO under a commercial licence, or cloud S3), selected by configuration only.

## Consequences
- No code depends on MinIO-specific APIs.
- Dev credentials come from `.env` (generated). Production credentials come from Vault.
