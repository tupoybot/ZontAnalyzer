from __future__ import annotations

import math
import time
from collections.abc import Iterable, Iterator, Sequence
from datetime import UTC, datetime
from typing import Any, Literal

import httpx

from zont_analyzer.domain import TelemetryPoint

ALLOWED_METHODS = frozenset({"devices", "load_data"})
_SENSITIVE_KEY_PARTS = frozenset(
    {
        "token",
        "password",
        "passwd",
        "secret",
        "phone",
        "email",
        "username",
        "login",
        "ssid",
        "netname",
        "serial",
        "imei",
        "iccid",
        "msisdn",
        "location",
        "mac",
        "sim_id",
        "wifi",
        "mask",
        "apn",
        "usbpassword",
    }
)


def _is_sensitive_key(key: str) -> bool:
    normalized = key.casefold().replace("-", "_")
    if normalized in {"pass", "ip", "loc", "gw"}:
        return True
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


class ZontApiError(RuntimeError):
    pass


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): "***" if _is_sensitive_key(str(key)) else redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def decode_delta_time_array(rows: Sequence[Sequence[Any]]) -> list[tuple[datetime, Any]]:
    """Decode ZONT Delta-time Array.

    The first positive value is an absolute Unix timestamp. Negative values mean
    an offset forward from the previous timestamp (e.g. -60 means +60 seconds).
    A later positive value resets the absolute timestamp.
    """

    decoded: list[tuple[datetime, Any]] = []
    timestamp: int | None = None
    for index, row in enumerate(rows):
        if not isinstance(row, Sequence) or isinstance(row, (str, bytes)) or len(row) < 2:
            raise ValueError(f"Invalid DTA row at index {index}")
        marker = row[0]
        if not isinstance(marker, (int, float)) or isinstance(marker, bool):
            raise ValueError(f"Invalid DTA timestamp at index {index}")
        marker_int = int(marker)
        if marker_int >= 0:
            timestamp = marker_int
        elif timestamp is None:
            raise ValueError("Delta-time Array starts with a relative timestamp")
        else:
            timestamp -= marker_int
        decoded.append((datetime.fromtimestamp(timestamp, UTC), row[1]))
    return decoded


def _looks_like_dta(value: Any) -> bool:
    if not isinstance(value, list) or not value:
        return False
    first = value[0]
    return (
        isinstance(first, list)
        and len(first) >= 2
        and isinstance(first[0], (int, float))
        and not isinstance(first[0], bool)
    )


def _walk_dta(value: Any, path: tuple[str, ...] = ()) -> Iterator[tuple[tuple[str, ...], list[Any]]]:
    if _looks_like_dta(value):
        yield path, value
        return
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _walk_dta(child, (*path, str(key)))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk_dta(child, (*path, str(index)))


def infer_unit(source_type: str, path: Iterable[str]) -> str | None:
    text = ".".join((source_type, *path)).casefold()
    if source_type == "z3k_boiler_adapter":
        metric = tuple(path)[-1] if tuple(path) else ""
        if metric in {"cs", "cs2", "bt", "rwt", "dt", "ot", "rt", "rors", "ds"}:
            return "°C"
        if metric in {"rml", "mrml", "rp"}:
            return "%"
        if metric in {"wp", "rbp"}:
            return "bar"
        if metric == "fr":
            return "l/min"
    if "temp" in text:
        return "°C"
    if any(term in text for term in ("work_time", "runtime", "duration")):
        return "s"
    if any(term in text for term in ("modulation", "percent", "duty")):
        return "%"
    if any(term in text for term in ("state", "flame", "burner", "mode")):
        return "state"
    return None


def infer_role(source_type: str, entity_id: str, metric_key: str, display_name: str = "") -> tuple[str, float]:
    text = " ".join((source_type, entity_id, metric_key, display_name)).casefold()
    if source_type == "z3k_boiler_adapter":
        boiler_roles = {
            "ot": "outdoor_temperature",
            "bt": "flow_temperature",
            "rwt": "return_temperature",
            "dt": "dhw_temperature",
            "cs": "target_flow_temperature",
            "rml": "burner_activity",
        }
        if metric_key in boiler_roles:
            return boiler_roles[metric_key], 0.95
    if source_type == "z3k_heating_circuit":
        if metric_key == "target_temp" and any(term in text for term in ("отоп", "room", "комнат")):
            return "target_temperature", 0.9
        if metric_key == "worktime" and any(term in text for term in ("отоп", "котел", "boiler")):
            return "heating_activity", 0.8
    if any(term in text for term in ("улиц", "наруж", "outdoor", "outside")):
        return "outdoor_temperature", 0.9
    if any(term in text for term in ("комнат", "room", "indoor", "воздух")) and "temp" in text:
        return "indoor_temperature", 0.85
    if any(term in text for term in ("подач", "flow_temp", "supply_temp")):
        return "flow_temperature", 0.85
    if any(term in text for term in ("обрат", "return_temp")):
        return "return_temperature", 0.85
    if any(term in text for term in ("burner", "горел", "flame", "boiler_work_time")):
        return "burner_activity", 0.9
    if "temp" in text:
        return "temperature", 0.55
    if "mode" in text:
        return "operating_mode", 0.75
    return "unknown", 0.3


class ZontReadOnlyClient:
    """Narrow adapter that can call only the audited ZONT read endpoints."""

    def __init__(
        self,
        *,
        token: str,
        client_email: str,
        base_url: str = "https://my.zont.online/api",
        timeout: float = 30,
        transport: httpx.BaseTransport | None = None,
        history_request_interval_seconds: float = 1.1,
    ):
        if not token:
            raise ValueError("ZONT token is required")
        if not client_email:
            raise ValueError("X-ZONT-Client contact email is required")
        self.__token = token
        self.__base_url = base_url.rstrip("/")
        self.__history_request_interval_seconds = max(0.0, history_request_interval_seconds)
        self.__last_history_request_at: float | None = None
        self.__client = httpx.Client(
            timeout=timeout,
            transport=transport,
            headers={
                "X-ZONT-Client": client_email,
                "X-ZONT-Token": token,
                "Content-Type": "application/json",
                "User-Agent": "ZontAnalyzer/0.1 read-only",
            },
        )

    def close(self) -> None:
        self.__client.close()

    def __enter__(self) -> ZontReadOnlyClient:
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

    def _post_allowed(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        if method not in ALLOWED_METHODS:
            raise PermissionError(f"ZONT method is not read-only allowlisted: {method}")
        for attempt in range(3):
            if method == "load_data":
                now = time.monotonic()
                if self.__last_history_request_at is not None:
                    delay = self.__history_request_interval_seconds - (now - self.__last_history_request_at)
                    if delay > 0:
                        time.sleep(delay)
                self.__last_history_request_at = time.monotonic()
            response = self.__client.post(f"{self.__base_url}/{method}", json=payload)
            try:
                data = response.json()
            except ValueError:
                data = None
            if response.status_code == 429 and attempt < 2:
                retry_after = response.headers.get("Retry-After")
                try:
                    delay = float(retry_after) if retry_after is not None else 60.0
                except ValueError:
                    delay = 60.0
                time.sleep(max(1.0, min(delay, 60.0)))
                continue
            if response.is_error:
                reason = None
                if isinstance(data, dict):
                    reason = data.get("error_ui") or data.get("error")
                raise ZontApiError(f"ZONT HTTP {response.status_code}: {reason or 'request rejected'}")
            break
        else:  # pragma: no cover - loop always breaks or raises
            raise ZontApiError("ZONT request retry loop exhausted")
        if not isinstance(data, dict):
            raise ZontApiError("ZONT returned a non-object response")
        if data.get("ok") is False:
            raise ZontApiError(str(data.get("error") or data.get("error_ui") or "ZONT request failed"))
        return data

    def healthcheck(self) -> bool:
        return bool(self._post_allowed("devices", {"load_io": False}).get("ok", True))

    def discover_devices(self) -> list[dict[str, Any]]:
        payload = self._post_allowed("devices", {"load_io": True})
        devices = payload.get("devices", [])
        if not isinstance(devices, list):
            raise ZontApiError("ZONT devices response does not contain a list")
        return [redact(item) for item in devices if isinstance(item, dict)]

    def load_config_snapshot(self) -> list[dict[str, Any]]:
        return self.discover_devices()

    def load_history(
        self,
        *,
        device_ids: Sequence[str],
        start: datetime,
        end: datetime,
        data_types: Sequence[str],
    ) -> list[dict[str, Any]]:
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError("History boundaries must be timezone-aware")
        requests = [
            {
                "device_id": int(device_id) if device_id.isdigit() else device_id,
                "data_types": list(data_types),
                "mintime": int(start.timestamp()),
                "maxtime": int(end.timestamp()),
            }
            for device_id in device_ids
        ]
        payload = self._post_allowed("load_data", {"requests": requests})
        responses = payload.get("responses", [])
        if not isinstance(responses, list):
            raise ZontApiError("ZONT load_data response does not contain responses")
        return [item for item in responses if isinstance(item, dict)]

    @staticmethod
    def normalize_history(response: dict[str, Any]) -> tuple[list[TelemetryPoint], dict[str, dict[str, Any]]]:
        device_id = str(response.get("device_id", "unknown"))
        points: list[TelemetryPoint] = []
        entities: dict[str, dict[str, Any]] = {}
        ignored = {"ok", "device_id", "error", "error_ui"}
        for source_type, payload in response.items():
            if source_type in ignored:
                continue
            for path, encoded in _walk_dta(payload):
                if not path:
                    entity_external = source_type
                    metric_key = source_type
                elif len(path) == 1:
                    entity_external = path[0]
                    metric_key = source_type if path[0].isdigit() else path[0]
                else:
                    entity_external = ".".join(path[:-1])
                    metric_key = path[-1]
                display_name = entity_external
                if isinstance(payload, dict):
                    candidate = payload.get(path[0]) if path else None
                    if isinstance(candidate, dict) and candidate.get("name"):
                        display_name = str(candidate["name"])
                stable_entity_id = f"zont:{device_id}:{source_type}:{entity_external}"
                unit = infer_unit(source_type, path)
                role, confidence = infer_role(source_type, entity_external, metric_key, display_name)
                entities[stable_entity_id] = {
                    "device_id": device_id,
                    "source_type": source_type,
                    "external_id": entity_external,
                    "display_name": display_name,
                    "role": role,
                    "confidence": confidence,
                    "unit": unit,
                }
                try:
                    decoded = decode_delta_time_array(encoded)
                except (ValueError, OverflowError, OSError):
                    continue
                for timestamp, value in decoded:
                    numeric: float | None = None
                    text: str | None = None
                    quality: Literal["valid", "invalid"] = "valid"
                    if isinstance(value, bool):
                        numeric = float(value)
                    elif isinstance(value, (int, float)):
                        numeric = float(value)
                        if not math.isfinite(numeric):
                            quality = "invalid"
                    elif value is not None:
                        text = str(value)
                    else:
                        quality = "invalid"
                    points.append(
                        TelemetryPoint(
                            device_id=device_id,
                            source_type=source_type,
                            entity_id=stable_entity_id,
                            metric_key=metric_key,
                            timestamp_utc=timestamp,
                            value_num=numeric,
                            value_text=text,
                            unit=unit,
                            quality=quality,
                        )
                    )
                    if source_type == "z3k_boiler_adapter" and metric_key == "s" and isinstance(value, list):
                        points.append(
                            TelemetryPoint(
                                device_id=device_id,
                                source_type=source_type,
                                entity_id=stable_entity_id,
                                metric_key="flame",
                                timestamp_utc=timestamp,
                                value_num=float("fl" in value),
                                unit="state",
                                quality="valid",
                            )
                        )
        return points, entities
