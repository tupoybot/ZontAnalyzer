"""Emergency suspension of derived gas work; original meter data remains available."""
import os


def gas_analysis_enabled() -> bool:
    """Disabled by default; opt-in exists for isolated regression tests and future acceptance."""
    return os.environ.get("ZONT_GAS_ANALYSIS_ENABLED") == "1"


GAS_DISABLED_NOTICE = (
    "Расчёт расхода и стоимости газа временно отключён. "
    "Показания счётчика сохраняются; ранее рассчитанные значения в архиве не обновляются."
)
