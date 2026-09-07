"""Semantic chart tokens shared by every static report period."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ChartRoleStyle:
    color: str
    dasharray: str | None = None
    marker: str | None = None


ROLE_STYLES = {
    "control_temperature": ChartRoleStyle("#1d6fa5"),
    "target_temperature": ChartRoleStyle("#7d55c7", "7 5"),
    "outdoor_temperature": ChartRoleStyle("#2f8a67"),
    "flow_temperature": ChartRoleStyle("#cf5d32"),
    "target_flow_temperature": ChartRoleStyle("#b58217", "7 5"),
    "return_temperature": ChartRoleStyle("#3f78a8"),
    "dhw_temperature": ChartRoleStyle("#b13e7b"),
    "burner_ch": ChartRoleStyle("#b85c00", marker="CH"),
    "burner_dhw": ChartRoleStyle("#0066b3", marker="ГВС"),
    "heating_request": ChartRoleStyle("#4c6f92", marker="CH"),
    "unknown": ChartRoleStyle("#697586", "3 3", marker="?"),
    "missing": ChartRoleStyle("#9aa5b1", "2 3", marker="—"),
}

STATE_COLORS = {
    "ch": "#b85c00", "dhw": "#0066b3", "burner": "#e07a35",
    "ch_flame": "#b85c00", "dhw_flame": "#0066b3", "concurrent_unknown": "#697586",
    "unknown": "#697586", "missing": "#9aa5b1",
}


def style_for(role: str) -> ChartRoleStyle:
    return ROLE_STYLES.get(role, ROLE_STYLES["unknown"])


def state_color(state: str) -> str:
    return STATE_COLORS.get(state.casefold(), STATE_COLORS["unknown"])
