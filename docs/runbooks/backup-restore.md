# Runbook: backup and restore drill (PostgreSQL, Vault, object store)

**Cadence:** a full drill every quarter, plus a PostgreSQL PITR restore before every major upgrade.
**Who:** two people from the platform team (operator + witness). **Where:** a scratch namespace or a scratch cluster. Never restore over production as part of a drill.
**Evidence to keep:** the drill log (timestamps, commands, row counts and checksums), measured RPO/RTO, and any deviations. File it with the change record.

Targets (assumptions, to be agreed with the business):

| Asset | RPO | RTO |
|---|---|---|
| PostgreSQL | ≤ 5 min (continuous WAL archiving) | ≤ 2 h |
| Vault | ≤ 1 h (hourly Raft snapshots) | ≤ 1 h |
| Object store | ≤ 24 h (replication / versioning) | ≤ 4 h |

## 1. PostgreSQL (CloudNativePG + Barman Cloud plugin)
Backups: a daily `ScheduledBackup` `nexus-pg-daily` plus continuous WAL to the `ObjectStore` `nexus-pg-backups` (30-day retention). The bucket lives outside the cluster.

**Check the backups are healthy (weekly, automated alert):**
```bash
kubectl -n nexus-data get backups.postgresql.cnpg.io --sort-by=.metadata.creationTimestamp | tail -3
kubectl -n nexus-data get cluster nexus-pg -o jsonpath='{.status.conditions[?(@.type=="ContinuousArchiving")].status}'   # True
```

**Drill: restore to a point in time into a new cluster.**
1. Choose a target time T, for example 15 minutes ago, and record a known row count at T. Platform → Security → RLS scan stores a timestamped scan, which makes a good marker.
2. Apply a recovery cluster in a scratch namespace (`nexus-restore`) that has copies of the backup credentials Secret and the ObjectStore:
   ```yaml
   apiVersion: postgresql.cnpg.io/v1
   kind: Cluster
   metadata: { name: nexus-pg-restore, namespace: nexus-restore }
   spec:
     instances: 1
     imageName: <same image as production>
     storage: { size: 100Gi }
     bootstrap:
       recovery:
         source: origin
         recoveryTarget: { targetTime: "2026-11-25 10:15:00+00" }
     externalClusters:
       - name: origin
         plugin:
           name: barman-cloud.cloudnative-pg.io
           parameters: { barmanObjectName: nexus-pg-backups, serverName: nexus-pg }
   ```
3. Wait for `Cluster in healthy state`. Then verify:
   - every service database exists
   - `SELECT count(*)` on a few tables matches the marker
   - RLS is still forced (`SELECT relname FROM pg_class WHERE relrowsecurity AND NOT relforcerowsecurity` returns nothing)
   - `<db>_app` roles are `NOSUPERUSER NOBYPASSRLS`
4. Record the elapsed time (RTO) and the gap between T and the last restored transaction (RPO).
5. Delete the scratch cluster and its PVCs. The restored data is production data, so handle it under the same controls and delete it the same day.

**Real recovery (production incident):**
1. Declare the incident.
2. Engage the platform kill switches if writes must stop ([kill switches](kill-switches.md)).
3. Restore to a **new** cluster as above, at the agreed T.
4. Switch the app over: set `global.database.host` to the new `-rw` Service, update the Vault database connection (`connection_url`), run `vault write -f database/rotate-root/nexus-pg`, then `helm upgrade`.
5. Replay is not needed for events. Outbox rows after T are lost along with the data. Run reconciliation: the call-service reconcile job checks provider call state, and email provider events are re-fetched by webhook redelivery.

## 2. Vault (integrated Raft)
**Snapshots:** an hourly CronJob, or an external scheduler, runs the command below with a token limited to `sys/storage/raft/snapshot` read. It copies the file to the backup bucket (encrypted, 30-day retention).
```bash
vault operator raft snapshot save vault-$(date -u +%Y%m%dT%H%MZ).snap
```

**Drill (scratch cluster only; a restore replaces all data):**
1. Install a throwaway single-node Vault with the same chart and version. Initialise it, and keep its temporary unseal key.
2. `vault operator raft snapshot restore -force <snapshot>`. The cluster now needs the **production** unseal or recovery keys. Two custodians bring their key shares, which also tests the key ceremony.
3. Verify:
   - `vault kv metadata get secret/production/platform/tenant-service/keycloak-client` returns metadata (never print values)
   - policies are listed
   - the Kubernetes auth role `crm-service` exists
4. Destroy the scratch Vault and its PVCs. Record the RTO and whether all key custodians were reachable within it.

**Loss of quorum (production):** restore the latest snapshot onto a fresh Raft cluster (same procedure), then roll the application pods so agents re-authenticate. Unseal keys and recovery keys are stored offline under split custody. Losing them is unrecoverable by design.

## 3. Object store
- **SeaweedFS (in-cluster):** volume replication across nodes (`replication` setting), plus a scheduled `weed filer.backup` or `aws s3 sync` of each bucket to the off-cluster backup bucket.
- **External S3:** bucket versioning plus cross-region or second-site replication. Use lifecycle rules for the retention windows. Object Lock (compliance mode) for audit archives is a decision for counsel.
- **Drill:**
  1. Pick 20 random object keys from the `recordings`, `attachments` and `exports` buckets, recording the key prefix (tenant id) and checksum.
  2. Delete them in a scratch copy, or confirm a versioned delete in a test bucket.
  3. Restore them from the backup or the previous version and compare checksums.
  4. Confirm that the restored objects are still private, and that downloads still go only through presigned URLs with a 5-minute expiry.

## 4. After every drill
- Update this runbook with anything that was wrong or slow.
- Raise tickets for RPO/RTO misses.
- Platform → Audit: confirm the drill's privileged actions are in the audit log (snapshot, restore, rotate-root).
