#!/usr/bin/env python3
"""Provision only owned M6 resources; preserve activation and delivery on reruns."""
from __future__ import annotations

import argparse
import copy
import re
import sys
from pathlib import Path
from typing import Any

from grafana_setup import GrafanaAPI, SetupError, _load_json, _replace_datasource, discover_datasource

FOLDER_UID = "zont-m6"
FOLDER_TITLE = "ZontAnalyzer M6"
DASHBOARD_UID = "zont-m6-overview"


def render(value: Any, environment: str) -> Any:
    if isinstance(value, dict):
        return {key: render(item, environment) for key, item in value.items()}
    if isinstance(value, list):
        return [render(item, environment) for item in value]
    if isinstance(value, str):
        return value.replace("${environment}", environment)
    return value


def prepare(api: GrafanaAPI, root: Path, datasource: str, environment: str,
            contact: str | None, thresholds: dict[str, float] | None = None) -> list[tuple[str, str, Any]]:
    """Read and validate all ownership before the first write."""
    writes: list[tuple[str, str, Any]] = []
    status, folder = api.request("GET", f"/api/folders/{FOLDER_UID}")
    if status == 404:
        writes.append(("POST", "/api/folders", {"uid": FOLDER_UID, "title": FOLDER_TITLE}))
    elif status != 200 or not isinstance(folder, dict) or (
        folder.get("uid"), folder.get("title")
    ) != (FOLDER_UID, FOLDER_TITLE):
        raise SetupError("cannot inspect owned M6 folder or ownership conflict")

    dashboard = copy.deepcopy(_load_json(root / "m6-dashboard.json"))
    status, current = api.request("GET", f"/api/dashboards/uid/{DASHBOARD_UID}")
    if status == 200:
        if (not isinstance(current, dict) or not isinstance(current.get("dashboard"), dict)
                or current.get("meta", {}).get("folderUid") != FOLDER_UID
                or current["dashboard"].get("uid") != DASHBOARD_UID
                or FOLDER_UID not in current["dashboard"].get("tags", [])
                or current["dashboard"].get("title") != dashboard["title"]):
            raise SetupError("M6 dashboard ownership conflict")
        dashboard["version"] = current["dashboard"].get("version", 0)
    elif status != 404:
        raise SetupError("cannot inspect M6 dashboard")
    dashboard["id"] = None
    for variable in dashboard["templating"]["list"]:
        if variable["name"] == "datasource":
            variable["current"] = {"text": datasource, "value": datasource}
        if variable["name"] == "environment":
            variable["current"] = {"text": environment, "value": environment}
    writes.append(("POST", "/api/dashboards/db", {
        "dashboard": dashboard, "folderUid": FOLDER_UID, "overwrite": False,
    }))

    templates = _load_json(root / "m6-alert-rules.json")["rules"]
    for template in templates:
        rule = _replace_datasource(render(template, environment), datasource)
        uid = rule["uid"]
        suffix = uid.removeprefix(f"zont-m6-{environment}-")
        if thresholds and suffix in thresholds:
            rule["data"][1]["model"]["conditions"][0]["evaluator"]["params"] = [thresholds[suffix]]
        if not uid.startswith(f"zont-m6-{environment}-") or rule["folderUID"] != FOLDER_UID:
            raise SetupError("invalid M6 rule template ownership")
        path = f"/api/v1/provisioning/alert-rules/{uid}"
        status, existing = api.request("GET", path)
        if status == 200:
            if (not isinstance(existing, dict) or existing.get("uid") != uid
                    or existing.get("folderUID") != FOLDER_UID
                    or existing.get("title") != rule["title"]
                    or existing.get("labels", {}).get("project") != FOLDER_UID
                    or existing.get("labels", {}).get("environment") != environment
                    or not isinstance(existing.get("isPaused"), bool)):
                raise SetupError("M6 alert rule ownership or activation conflict")
            rule["isPaused"] = existing["isPaused"]
            if existing.get("notification_settings"):
                rule["notification_settings"] = copy.deepcopy(existing["notification_settings"])
            elif contact:
                rule["notification_settings"] = {"receiver": contact}
            elif not rule["isPaused"]:
                raise SetupError("active M6 rule requires an existing private contact point")
            writes.append(("PUT", path, rule))
        elif status == 404:
            rule["isPaused"] = True
            if contact:
                rule["notification_settings"] = {"receiver": contact}
            writes.append(("POST", "/api/v1/provisioning/alert-rules", rule))
        else:
            raise SetupError("cannot inspect M6 alert rule")
    return writes


def activation_writes(api: GrafanaAPI, root: Path, environment: str,
                      selected: list[str]) -> list[tuple[str, str, Any]]:
    """Activate explicit owned rules only; preflight every selection before writing."""
    templates = render(_load_json(root / "m6-alert-rules.json")["rules"], environment)
    allowed = {rule["uid"].removeprefix(f"zont-m6-{environment}-"): rule for rule in templates}
    if not selected or len(set(selected)) != len(selected) or any(name not in allowed for name in selected):
        raise SetupError("activation requires unique known M6 rule suffixes")
    writes = []
    for name in selected:
        template = allowed[name]
        path = f'/api/v1/provisioning/alert-rules/{template["uid"]}'
        status, rule = api.request("GET", path)
        if (status != 200 or not isinstance(rule, dict)
                or rule.get("uid") != template["uid"] or rule.get("title") != template["title"]
                or rule.get("folderUID") != FOLDER_UID
                or rule.get("labels", {}).get("project") != FOLDER_UID
                or rule.get("labels", {}).get("environment") != environment
                or not isinstance(rule.get("isPaused"), bool)
                or not rule.get("notification_settings", {}).get("receiver")):
            raise SetupError("activation requires an owned rule with private notification routing")
        rule = copy.deepcopy(rule)
        rule["isPaused"] = False
        writes.append(("PUT", path, rule))
    return writes


def run(config_path: Path, template_dir: Path | None = None, activate: list[str] | None = None) -> None:
    config = _load_json(config_path)
    environment = config.get("environment")
    if not isinstance(environment, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,15}", environment):
        raise SetupError("environment must be a short lowercase identifier")
    contact = config.get("contact_point")
    thresholds = config.get("thresholds", {})
    if (not isinstance(thresholds, dict) or any(
        name not in {"stale", "loops", "tokens", "budget"}
        or isinstance(value, bool) or not isinstance(value, (int, float))
        or not 0 < value < 1e15 for name, value in thresholds.items()
    )):
        raise SetupError("thresholds must contain positive finite stale/loops/tokens/budget values")
    if contact is not None and (not isinstance(contact, str) or not contact.strip()):
        raise SetupError("contact_point must name an existing private contact point")
    if not isinstance(config.get("stack_url"), str) or not isinstance(config.get("service_account_token"), str):
        raise SetupError("private config requires stack_url and service_account_token")
    api = GrafanaAPI(config["stack_url"], config["service_account_token"])
    root = template_dir or Path(__file__).resolve().parents[1] / "grafana"
    if activate is not None:
        writes = activation_writes(api, root, environment, activate)
    else:
        datasource = discover_datasource(api, config.get("datasource_uid"))
        writes = prepare(api, root, datasource, environment, contact, thresholds)
    for method, path, payload in writes:
        status, _ = api.request(method, path, payload)
        if status not in (200, 201, 202):
            raise SetupError("cannot save owned M6 resource; rerun after resolving API failure")
    print("Grafana M6 selected rules activated" if activate is not None else
          "Grafana M6 setup complete; new rules paused, existing activation preserved")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    parser.add_argument("--template-dir", type=Path)
    parser.add_argument("--activate", nargs="+", metavar="RULE", help="activate explicit owned rule suffixes")
    args = parser.parse_args(argv)
    try:
        run(args.config, args.template_dir, args.activate)
    except SetupError as exc:
        print(f"Grafana M6 setup failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
