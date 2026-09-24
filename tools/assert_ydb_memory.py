"""Reject disk-backed PDisks in the disposable local test server."""

import json
import sys

import yaml


def verify(config):
    disks = config.get("blob_storage_config", {}).get("service_set", {}).get("pdisks", [])
    if not disks or any(not str(disk.get("path", "")).startswith("SectorMap:") for disk in disks):
        raise ValueError("test YDB must use in-memory PDisks; check YDB_USE_IN_MEMORY_PDISKS=true")
    return {"in_memory": True, "pdisk_count": len(disks)}


if __name__ == "__main__":
    print(json.dumps(verify(yaml.safe_load(sys.stdin))))
