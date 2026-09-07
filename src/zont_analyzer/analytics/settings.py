"""Read-only controls from observed ZONT fields, without guessed encodings/access rights."""
from __future__ import annotations

import math
from typing import Any

# Field names observed in the pilot devices response. Presence does not establish
# controller-specific units, enablement, or permission to edit a service setting.
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
}


def control_settings(device: dict[str, Any], circuit_id: str, *, captured_at: str | None = None) -> dict[str, Any]:
    config = device.get("z3k_config")
    config = config if isinstance(config, dict) else {}
    circuits = config.get("heating_circuits", [])
    matches = [item for item in circuits if isinstance(item, dict) and str(item.get("id")) == circuit_id
               ] if isinstance(circuits, list) else []
    result: dict[str, Any] = {
        "id": f"settings:{circuit_id}", "source": "zont:devices.z3k_config",
        "captured_at": captured_at, "historical_applicability": "snapshot_only; not historical telemetry",
        "status": "unknown", "parameters": [], "pza_curve": None,
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
    prefix = f"z3k_config.heating_circuits[id={circuit_id}]"
    for key, label in _FIELDS.items():
        value = circuit.get(key)
        if value is None or not isinstance(value, (int, float, bool)) or not math.isfinite(value):
            result["unknowns"].append(f"{key}:unavailable")
            continue
        result["parameters"].append({
            "id": f"setting:{circuit_id}:{key}", "field": key, "label": label, "value": value,
            "source": f"zont:{prefix}.{key}", "epistemic_level": "observed",
            "unit": "celsius" if key in {"summer_threshold", "hysteresis"} else "raw",
            "user_access": "needs_confirmation", "active": "unknown",
        })
    curves = config.get("pzas", [])
    selected = [item for item in curves if isinstance(item, dict) and item.get("id") == circuit.get("pza")
                ] if isinstance(curves, list) else []
    if len(selected) == 1:
        curve = selected[0]
        x, y = curve.get("x"), curve.get("y")
        if (isinstance(x, list) and isinstance(y, list) and 1 < len(x) == len(y) <= 32
                and all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in x + y)):
            result["pza_curve"] = {
                "id": f"setting:{circuit_id}:pza_curve", "source": f"zont:z3k_config.pzas[id={curve['id']}]",
                "x_raw": x, "y_raw": y, "epistemic_level": "observed",
                "encoding": "unverified; do not convert to Celsius or infer a slope setting",
                "active": "unknown; configured reference alone does not establish active algorithm",
            }
    if result["pza_curve"] is None:
        result["unknowns"].append("pza_curve_unavailable_or_ambiguous")
    result["unknowns"].extend([
        "active_control_algorithm_not_verified", "pid_units_and_user_access_not_verified",
        "summer_switch_hysteresis_and_delay_semantics_not_verified",
    ])
    return result
