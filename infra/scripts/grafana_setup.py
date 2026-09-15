#!/usr/bin/env python3
"""Provision the owned M1 Grafana folder, dashboard and paused alert rule."""

from __future__ import annotations

import argparse
import http.client
import json
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
TIMEOUT_SECONDS = 15
FOLDER_UID = "zont-m1"
FOLDER_TITLE = "ZontAnalyzer M1"
DASHBOARD_UID = "zont-m1"
RULE_UID = "zont-m1-dev-health"


class SetupError(ValueError):
    """A safe, user-facing setup error (without remote response bodies)."""


def validate_stack_url(value: str) -> tuple[str, int]:
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise SetupError("stack_url must be an HTTPS grafana.net URL")
    host = parsed.hostname.lower().rstrip(".")
    if not host.endswith(".grafana.net") or host == "grafana.net":
        raise SetupError("stack_url must use a *.grafana.net host")
    if parsed.query or parsed.fragment or parsed.path not in ("", "/"):
        raise SetupError("stack_url cannot contain a path, query, or fragment")
    try:
        port = parsed.port
    except ValueError as exc:
        raise SetupError("stack_url port must be 443") from exc
    if port not in (None, 443):
        raise SetupError("stack_url port must be 443")
    return host, 443


class GrafanaAPI:
    def __init__(self, stack_url: str, token: str, connection_factory: Any = None) -> None:
        self.host, self.port = validate_stack_url(stack_url)
        if not token or "\n" in token or "\r" in token:
            raise SetupError("service_account_token is required")
        self.token = token
        self.connection_factory = connection_factory or http.client.HTTPSConnection

    def request(self, method: str, path: str, payload: Any = None) -> tuple[int, Any]:
        if not path.startswith("/") or "?" in path or "#" in path:
            raise SetupError("invalid Grafana API path")
        body = None if payload is None else json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        conn = self.connection_factory(self.host, self.port, timeout=TIMEOUT_SECONDS)
        try:
            headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
            if body is not None:
                headers["Content-Type"] = "application/json"
            conn.request(method, path, body=body, headers=headers)
            response = conn.getresponse()
            if 300 <= response.status < 400:
                raise SetupError("Grafana API redirect refused")
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise SetupError("Grafana API response is too large")
            if not raw:
                return response.status, None
            try:
                return response.status, json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise SetupError("Grafana API returned invalid JSON") from exc
        except (OSError, TimeoutError) as exc:
            raise SetupError("Grafana API request failed") from exc
        finally:
            conn.close()


def _load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SetupError(f"cannot read template: {path.name}") from exc
    if not isinstance(data, dict):
        raise SetupError(f"template is not an object: {path.name}")
    return data


def discover_datasource(api: GrafanaAPI, configured_uid: str | None) -> str:
    status, data = api.request("GET", "/api/datasources")
    if status != 200 or not isinstance(data, list):
        raise SetupError("cannot discover Grafana datasources")
    prometheus = [d for d in data if isinstance(d, dict) and d.get("type") == "prometheus"]
    if configured_uid:
        matches = [d for d in prometheus if d.get("uid") == configured_uid]
        if len(matches) != 1:
            raise SetupError("configured Prometheus datasource was not found")
        return configured_uid
    defaults = [d for d in prometheus if d.get("isDefault") is True]
    if len(defaults) != 1 or not defaults[0].get("uid"):
        raise SetupError("exactly one default Prometheus datasource is required")
    return str(defaults[0]["uid"])


def ensure_folder(api: GrafanaAPI) -> None:
    status, folder = api.request("GET", f"/api/folders/{FOLDER_UID}")
    if status == 404:
        status, _ = api.request("POST", "/api/folders", {"uid": FOLDER_UID, "title": FOLDER_TITLE})
        if status not in (200, 201):
            raise SetupError("cannot create owned Grafana folder")
    elif status == 200:
        if not isinstance(folder, dict) or folder.get("uid") != FOLDER_UID or folder.get("title") != FOLDER_TITLE:
            raise SetupError("Grafana folder ownership conflict")
    else:
        raise SetupError("cannot inspect owned Grafana folder")


def ensure_dashboard(api: GrafanaAPI, template: dict[str, Any], datasource_uid: str) -> None:
    status, existing = api.request("GET", f"/api/dashboards/uid/{DASHBOARD_UID}")
    if status not in (200, 404):
        raise SetupError("cannot inspect owned Grafana dashboard")
    dashboard = dict(template)
    dashboard["uid"] = DASHBOARD_UID
    dashboard["title"] = template.get("title", "ZontAnalyzer — M1")
    dashboard["tags"] = list(dict.fromkeys([*template.get("tags", []), FOLDER_UID]))
    dashboard["folderUid"] = FOLDER_UID
    dashboard["id"] = None
    if status == 200:
        if not isinstance(existing, dict) or existing.get("dashboard") is None:
            raise SetupError("Grafana dashboard response is invalid")
        current = existing["dashboard"]
        if (current.get("uid") != DASHBOARD_UID or current.get("title") != dashboard["title"]
                or existing.get("meta", {}).get("folderUid") != FOLDER_UID
                or FOLDER_UID not in current.get("tags", [])):
            raise SetupError("Grafana dashboard ownership conflict")
        dashboard["version"] = current.get("version", 0)
    templating = dashboard.get("templating", {}).get("list", [])
    for variable in templating:
        if variable.get("type") == "datasource" and variable.get("name") == "datasource":
            variable["current"] = {"text": datasource_uid, "value": datasource_uid}
    status, _ = api.request(
        "POST", "/api/dashboards/db",
        {"dashboard": dashboard, "folderUid": FOLDER_UID, "overwrite": False},
    )
    if status not in (200, 201):
        raise SetupError("cannot save owned Grafana dashboard")


def _replace_datasource(value: Any, uid: str) -> Any:
    if isinstance(value, dict):
        return {
            k: uid if k in ("uid", "datasourceUid") and v in ("${datasource}", "${datasource_uid}")
            else _replace_datasource(v, uid)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_replace_datasource(v, uid) for v in value]
    return value


def ensure_rule(api: GrafanaAPI, template: dict[str, Any], datasource_uid: str, contact_point: str | None) -> None:
    status, existing = api.request("GET", f"/api/v1/provisioning/alert-rules/{RULE_UID}")
    if status not in (200, 404):
        raise SetupError("cannot inspect owned Grafana alert rule")
    rule = json.loads(json.dumps(template))
    rule["uid"] = RULE_UID
    rule["folderUID"] = FOLDER_UID
    rule["isPaused"] = True
    rule["data"] = _replace_datasource(rule.get("data", []), datasource_uid)
    if contact_point:
        rule["notification_settings"] = {"receiver": contact_point}
    elif status == 200 and isinstance(existing, dict) and existing.get("notification_settings"):
        rule["notification_settings"] = existing["notification_settings"]
    if status == 200:
        if (
            not isinstance(existing, dict)
            or existing.get("uid") != RULE_UID
            or existing.get("title") != rule.get("title")
        ):
            raise SetupError("Grafana alert rule ownership conflict")
        labels = existing.get("labels", {})
        if (
            existing.get("folderUID") != FOLDER_UID
            or labels.get("project") != FOLDER_UID
            or labels.get("environment") != "dev"
        ):
            raise SetupError("Grafana alert rule ownership conflict")
        status, _ = api.request("PUT", f"/api/v1/provisioning/alert-rules/{RULE_UID}", rule)
    else:
        status, _ = api.request("POST", "/api/v1/provisioning/alert-rules", rule)
    if status not in (200, 201):
        raise SetupError("cannot save owned Grafana alert rule")


def run(config_path: Path, template_dir: Path | None = None) -> None:
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SetupError("cannot read private Grafana config") from exc
    if not isinstance(config, dict):
        raise SetupError("private Grafana config must be an object")
    root = template_dir or Path(__file__).resolve().parents[1] / "grafana"
    stack_url = config.get("stack_url")
    token = config.get("service_account_token")
    if not isinstance(stack_url, str) or not isinstance(token, str):
        raise SetupError("private Grafana config requires stack_url and service_account_token")
    api = GrafanaAPI(stack_url, token)
    datasource_uid = discover_datasource(api, config.get("datasource_uid"))
    ensure_folder(api)
    ensure_dashboard(api, _load_json(root / "dashboard.json"), datasource_uid)
    ensure_rule(api, _load_json(root / "alert-rule.json"), datasource_uid, config.get("contact_point"))
    print("Grafana M1 setup complete (dashboard saved; alert rule paused)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    parser.add_argument("--template-dir", type=Path)
    args = parser.parse_args(argv)
    try:
        run(args.config, args.template_dir)
    except SetupError as exc:
        print(f"Grafana M1 setup failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
