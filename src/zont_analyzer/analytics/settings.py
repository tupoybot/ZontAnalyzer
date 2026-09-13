"""Observed ZONT controls decoded using the vendor's web client contract."""
from __future__ import annotations

import math
from typing import Any, TypeGuard

# Protocol evidence and scope: docs/zont-control-settings.md.
_CONTRACT = "zont-controls-v1"
_FIELDS = {
    "pza": "Ссылка на кривую ПЗА",
    "pid_prop_koef": "Пропорциональный коэффициент PID",
    "pid_integral_koef": "Интегральный коэффициент PID",
    "winter_summer_switch": "Автоматическое переключение зима/лето",
    "summer_threshold": "Порог наружной температуры режима лето",
    "hysteresis": "Гистерезис контура (не подтверждён как гистерезис зима/лето)",
    "off_to_start_delay": "Задержка включения",
    "start_to_off_delay": "Задержка выключения",
    "turn_off_delay": "Задержка отключения",
    "water_min_temperature": "Нижняя граница температуры теплоносителя",
    "water_max_temperature": "Верхняя граница температуры теплоносителя",
}


def _integer(value: Any) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def regulation_settings(circuit: dict[str, Any], circuit_id: str) -> dict[str, Any]:
    """Decode configured regulation; configuration never proves runtime activity."""
    register, kind, pza = circuit.get("setting_register"), circuit.get("type"), circuit.get("pza")
    has_pza = pza not in {0, 255} if _integer(pza) else False if pza is None else None
    result: dict[str, Any] = {
        "id": f"setting:{circuit_id}:regulation", "status": "unknown", "mode": None,
        "label": "Способ регулирования не определён", "enabled": None,
        "pza_configured": has_pza, "pza_role": "unknown", "pza_role_label": "Роль ПЗА не определена",
        "source": f"zont:z3k_config.heating_circuits[id={circuit_id}].setting_register",
        "contract": _CONTRACT, "applicability": "configuration_snapshot; not runtime activity or period history",
    }
    if _integer(kind) and kind != 3:
        result.update(status="not_applicable", label="Контур не является отопительным")
        return result
    if kind != 3 or not _integer(kind) or not _integer(register):
        return result
    mode = {0: ("air", "По воздуху"), 1: ("air_pid", "По воздуху с ПИД"),
            2: ("water", "По теплоносителю")}.get(register & 3)
    result.update(setting_register_raw=register, enabled=not bool(register & 64))
    if mode is None:
        return result
    result.update(status="decoded", mode=mode[0], label=mode[1])
    if has_pza is False:
        result.update(pza_role="off", pza_role_label="ПЗА не настроена")
    elif has_pza:
        role = ("upper_limit", "Ограничение расчётной температуры ПИД") if mode[0] == "air_pid" else (
            ("heat_request_only", "Только для запроса тепла") if mode[0] == "water" and register & 128
            else ("target", "Расчёт температуры теплоносителя по погоде")
        )
        result.update(pza_role=role[0], pza_role_label=role[1])
    return result


def pza_points_c(x: Any, y: Any) -> list[dict[str, float]] | None:
    """Decode vendor decikelvin coordinates; reject invalid/sentinel curves."""
    if not (isinstance(x, list) and isinstance(y, list) and 1 < len(x) == len(y) <= 32
            and all(_integer(v) for v in x + y)):
        return None
    # Reject implausible temperatures, raw Celsius and protocol sentinels.
    if not (all(2130 <= v <= 3330 for v in x) and all(2730 <= v <= 4230 for v in y)):
        return None
    if not (all(a < b for a, b in zip(x, x[1:], strict=False))
            or all(a > b for a, b in zip(x, x[1:], strict=False))):
        return None
    return [{"outdoor_c": (a - 2730) / 10, "flow_c": (b - 2730) / 10} for a, b in zip(x, y, strict=True)]


def control_settings(device: dict[str, Any], circuit_id: str, *, captured_at: str | None = None) -> dict[str, Any]:
    config = device.get("z3k_config")
    config = config if isinstance(config, dict) else {}
    circuits = config.get("heating_circuits", [])
    matches = [item for item in circuits if isinstance(item, dict) and str(item.get("id")) == circuit_id
               ] if isinstance(circuits, list) else []
    result: dict[str, Any] = {
        "id": f"settings:{circuit_id}", "source": "zont:devices.z3k_config",
        "captured_at": captured_at, "historical_applicability": "snapshot_only; not historical telemetry",
        "status": "unknown", "parameters": [], "pza_curve": None, "contract": _CONTRACT,
        "unknowns": [],
        "experiment_policy": (
            "Confirm meaning, current value, active control algorithm and user accessibility before a change; "
            "raw fields do not authorize service adjustments. No ZONT write API."
        ),
    }
    if len(matches) != 1:
        result["unknowns"] = ["heating_circuit_unresolved"]
        return result
    circuit = matches[0]
    result["status"] = "observed"
    regulation = regulation_settings(circuit, circuit_id)
    result["regulation"] = regulation
    prefix = f"z3k_config.heating_circuits[id={circuit_id}]"
    for key, label in _FIELDS.items():
        value = circuit.get(key)
        if (value is None or not isinstance(value, (int, float)) or not math.isfinite(value)
                or isinstance(value, bool) and key != "winter_summer_switch"):
            result["unknowns"].append(f"{key}:unavailable")
            continue
        result["parameters"].append({
            "id": f"setting:{circuit_id}:{key}", "field": key, "label": label, "value": value,
            "source": f"zont:{prefix}.{key}", "epistemic_level": "observed",
            "unit": ("celsius" if key in {"summer_threshold", "hysteresis", "water_min_temperature",
                                          "water_max_temperature"} else
                     "zont_coefficient" if key.startswith("pid_") else "raw"),
            "user_access": "needs_confirmation", "active": "runtime_not_observed",
        })
    curves = config.get("pzas", [])
    selected = [item for item in curves if isinstance(item, dict) and _integer(item.get("id"))
                and item.get("id") == circuit.get("pza") and regulation["pza_configured"] is True
                ] if isinstance(curves, list) else []
    if len(selected) == 1:
        curve = selected[0]
        x, y = curve.get("x"), curve.get("y")
        if (isinstance(x, list) and isinstance(y, list) and 1 < len(x) == len(y) <= 32
                and all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in x + y)):
            points = pza_points_c(x, y)
            result["pza_curve"] = {
                "id": f"setting:{circuit_id}:pza_curve", "source": f"zont:z3k_config.pzas[id={curve['id']}]",
                "x_raw": x, "y_raw": y, "epistemic_level": "observed",
                "points_c": points,
                "encoding": "zont_decikelvin_offset_2730" if points else "unverified_or_invalid_coordinates",
                "active": "configured_reference; runtime_activity_not_observed",
            }
            if points is None:
                result["unknowns"].append("pza_curve_coordinates_invalid")
    if result["pza_curve"] is None and regulation["pza_configured"] is not False:
        result["unknowns"].append("pza_curve_unavailable_or_ambiguous")
    if regulation["status"] == "unknown":
        result["unknowns"].append("configured_regulation_not_decoded")
    result["unknowns"].extend([
        "configuration_snapshot_does_not_prove_runtime_or_period_history",
        "summer_switch_hysteresis_and_delay_semantics_not_verified",
    ])
    return result
