"""Small, conservative cleanup of technical names in owner-facing prose.

The canonical AI result may contain internal field names and evidence IDs.  This
module is used only on the display copy of narrative fields, so those values
remain unchanged in storage and in the debug JSON.
"""

import re

from .timezone_labels import timezone_label

_PROSE_REPLACEMENTS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Keep the Russian inflection of the surrounding sentence readable.
    (re.compile(r"\bПаттерн подтверждён\b", re.I), "Закономерность подтверждена"),
    (re.compile(r"\bОтмечен повторяющийся паттерн (?P<subject>[^.;]+?), но его связь\b", re.I),
     r"Отмечена повторяющаяся закономерность \g<subject>, но её связь"),
    (re.compile(r"\bОценка газа имеет статус estimated и низкий reliability_index_pct\s*(\d+(?:[.,]\d+)?)%", re.I),
     r"Расход газа рассчитан по модели; индекс надёжности низкий — \1%"),
    (re.compile(r"\bпокрытии\s+temporal_evidence\b(?!:)", re.I), "полноте временных данных"),
    (re.compile(r"\bограничено\s+stuck_pct\s*[:=]?\s*(?P<value>\d+(?:[.,]\d+)?)\s*%", re.I),
     r"ограничено: доля неизменных показаний \g<value>%"),
    (re.compile(r"(?<![\w:])derived[- ]метриками\b", re.I), "расчётными показателями"),
    (re.compile(r"(?<![\w:])derived[- ]метрика\b", re.I), "расчётный показатель"),
    (re.compile(r"(?<![\w:])derived[- ]кандидат\b", re.I), "косвенный признак"),
    (re.compile(r"(?<![\w:])derived[- ]кандидата\b", re.I), "косвенного признака"),
    (re.compile(r"(?<![\w:])derived[- ]кандидатом\b", re.I), "косвенным признаком"),
    (re.compile(r"(?<![\w:])temporal_evidence\b(?!:)", re.I), "временные данные"),
    (re.compile(r"(?<![\w:])(?:period|период)_comparisons\b(?!:)", re.I), "сравнения с другими периодами"),
    (re.compile(r"(?<![\w:])reliability_index_pct\s*[:=]?\s*(?P<value>\d+(?:[.,]\d+)?)\s*%(?!\w)", re.I),
     r"индекс надёжности \g<value>%"),
    (re.compile(r"(?<![\w:])stuck_pct\s*[:=]?\s*(?P<value>\d+(?:[.,]\d+)?)\s*%(?!\w)", re.I),
     r"доля неизменных показаний \g<value>%"),
    (re.compile(r"(?<![\w:])reliability_index_pct\b(?!:)", re.I), "индекс надёжности"),
    (re.compile(r"(?<![\w:])stuck_pct\b(?!:)", re.I), "доля неизменных показаний"),
    (re.compile(r"(?<![\w:])estimated\b(?!:)", re.I), "«расчёт по модели»"),
    (re.compile(r"(?<![\w:])score(?=\s+\d)", re.I), "индексом качества данных"),
)


def normalize_user_text(value: str) -> str:
    """Translate a short list of internal names without touching evidence IDs."""
    for pattern, replacement in _PROSE_REPLACEMENTS:
        value = pattern.sub(replacement, value)
    value = re.sub(
        r"(?<![\w:])Etc/GMT(?:[+-]\d+)?\b(?!:)",
        lambda match: timezone_label(match.group()).split(" — ")[0], value,
    )
    return value
