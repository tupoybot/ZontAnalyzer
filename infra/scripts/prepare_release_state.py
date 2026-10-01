"""Prepare a reviewable v2 trigger adoption and retain deployed runtime limits."""
import json
import os
import sys
import urllib.request
from pathlib import Path


def resource(state, kind, name):
    rows = [item for item in state.get("resources", [])
            if item.get("mode") == "managed" and item.get("type") == kind and item.get("name") == name]
    instances = [instance["attributes"] for row in rows for instance in row.get("instances", [])]
    if len(instances) > 1:
        raise ValueError("ambiguous deployment resource")
    return instances[0] if instances else None


def prepare(inputs, state, read, *, preserve_runtime=False):
    folder = read("resource-manager.api.cloud.yandex.net", "/resource-manager/v1/folders/" + inputs["folder_id"])
    if folder.get("cloudId") != inputs["cloud_id"]:
        raise ValueError("deployed folder is outside authorized cloud")
    result = dict(inputs)
    # Import identity is derived only from the verified state and live trigger.
    result.pop("monitoring_trigger_import_id", None)
    old = resource(state, "yandex_function_trigger", "monitoring")
    current = resource(state, "yandex_serverless_triggers", "monitoring")
    if old and current and old["id"] != current["id"]:
        raise ValueError("monitoring migration has two different triggers")
    existing = current or old
    if existing:
        if not inputs.get("enable_monitoring_timer"):
            raise ValueError("cannot disable existing monitoring during compatibility migration")
        trigger = read("serverless-triggers.api.cloud.yandex.net", "/triggers/v2/triggers/" + existing["id"])
        application = resource(state, "yandex_serverless_container", "application")
        actions = trigger.get("action", [])
        expected = {"containerId": application["id"] if application else None,
                    "serviceAccountId": inputs["timer_service_account_id"], "path": "/internal/monitoring"}
        if (trigger.get("folderId") != inputs["folder_id"] or len(actions) != 1
                or actions[0].get("invokeContainer") != expected
                or set(actions[0]) != {"invokeContainer", "retryPolicy"}
                or actions[0]["retryPolicy"] != {"retryAttempts": "1", "interval": "10s"}
                or set(trigger.get("source", {})) != {"timer"}
                or set(trigger["source"]["timer"]) != {"cronExpression"}):
            raise ValueError("existing monitoring trigger has unexpected configuration")
        if old:
            result["monitoring_trigger_import_id"] = existing["id"]
        if old or preserve_runtime:
            result["monitoring_timer_schedule"] = trigger["source"]["timer"]["cronExpression"]
    if preserve_runtime:
        for name, variable in (("probe", "ydb_request_units_per_second"),
                               ("production", "production_ydb_request_units_per_second")):
            stored = resource(state, "yandex_ydb_database_serverless", name)
            if stored is None:
                continue
            database = read("ydb.api.cloud.yandex.net", "/ydb/v1/databases/" + stored["id"])
            if database.get("folderId") != inputs["folder_id"]:
                raise ValueError("deployed database is outside authorized folder")
            settings = database["serverlessDatabase"]
            if (not settings.get("enableThrottlingRcuLimit")
                    or int(settings.get("provisionedRcuLimit", "0")) != 0):
                raise ValueError("unexpected deployed capacity mode")
            result[variable] = int(settings["throttlingRcuLimit"])
    return result


def load_state(path):
    raw = path.read_text()
    # A successful `terraform state pull` writes no bytes before the first apply.
    # Whitespace or malformed nonempty output remains an error.
    return {"resources": []} if raw == "" else json.loads(raw)


def run(private):
    token = (private / "deploy-token").read_text().strip()

    def read(host, path):
        request = urllib.request.Request("https://" + host + path, headers={"Authorization": "Bearer " + token})
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.load(response)

    path = private / "cloud-work/inputs.tfvars.json"
    inputs = json.loads(path.read_text())
    state = load_state(private / "pre-deploy-state.json")
    result = prepare(inputs, state, read, preserve_runtime=os.environ.get("M1_PRESERVE_RUNTIME_SETTINGS") == "1")
    path.write_text(json.dumps(result, indent=2) + "\n")
    print("Existing monitoring identity and selected runtime limits verified; private plan inputs prepared.")


if __name__ == "__main__":
    run(Path(sys.argv[1]))
