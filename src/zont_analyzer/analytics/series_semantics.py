"""Source contracts distinguish commanded temperatures from sensor readings."""
from __future__ import annotations


def is_setpoint_series(source_type: str, metric_key: str) -> bool:
    # Heating/DHW targets may be returned only at changes and request boundaries.
    # OpenTherm cs is a commanded flow temperature (possibly calculated by PZA),
    # not a measured temperature; its changes also must not be sensor faults.
    return (source_type == "z3k_heating_circuit" and metric_key in {"target_temp", "setpoint_temp"}
            or source_type == "z3k_boiler_adapter" and metric_key == "cs")
