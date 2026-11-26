"""CI helper: build platform-data chart values from generated/topology.json (mirrors addons/data.tf).

Usage: python ci_values.py infra/terraform/generated/topology.json > values.yaml
"""

import json
import sys

import yaml  # type: ignore[import-untyped]

with open(sys.argv[1]) as fh:
    topo = json.load(fh)
vals = {
    "postgres": {
        "databases": topo["databases"],
        "backup": {"enabled": True, "destinationPath": "s3://nexuscall-pg-backups/ci"},
    },
    "rabbitmq": {
        "exchanges": [topo["event_exchange"], topo["task_exchange"], topo["dead_letter_exchange"]],
        "deadLetterExchange": topo["dead_letter_exchange"]["name"],
        "deadLetterQueues": topo["dead_letter_queues"],
        "dlqPolicy": {k: topo["dlq_policy"][k] for k in ("name", "pattern", "definition")},
    },
}
print(yaml.safe_dump(vals))
