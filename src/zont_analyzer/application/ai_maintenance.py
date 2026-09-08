"""Start bounded model maintenance independently of telemetry and report viewing."""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from zont_analyzer.adapters.openai.model_catalog import CatalogSnapshot, OpenAIModelCatalog
from zont_analyzer.application.ai_settings import AISettingsStore
from zont_analyzer.application.model_review import ModelReviewStore

if TYPE_CHECKING:
    from zont_analyzer.runtime import Runtime

logger = logging.getLogger(__name__)
_lock = threading.Lock()
_threads: dict[str, threading.Thread] = {}


def local_assessments(runtime: Runtime) -> dict[str, Any]:
    from zont_analyzer.adapters.openai.provider import PROMPT_VERSION, SCHEMA_VERSION
    from zont_analyzer.evaluation.dataset import build_dataset, dataset_sha
    from zont_analyzer.evaluation.runner import load_assessments

    configured = runtime.config.openai.evaluation_results_file
    if not configured:
        return {}
    path = Path(configured)
    if not path.is_absolute():
        path = runtime.loaded.data_dir / path
    cases = build_dataset()
    try:
        assessments = load_assessments(path, dataset_sha256=dataset_sha(cases),
                                       prompt_id=PROMPT_VERSION, schema_id=SCHEMA_VERSION)
    except (OSError, ValueError):
        logger.warning("Local model assessments unavailable; candidates still require evaluation")
        return {}
    required = {case["id"] for case in cases}
    return {model: {**item, "source_file": str(path)} for model, item in assessments.items()
            if isinstance(item.get("completed_case_ids"), list) and set(item["completed_case_ids"]) == required}


class _ReadOnlyCatalog:
    def fetch(self, now: datetime | None = None, model_ids: tuple[str, ...] = ()) -> CatalogSnapshot:
        raise RuntimeError("Report/settings reads must never fetch the model catalog")


def review_state(runtime: Runtime) -> dict[str, Any]:
    result = ModelReviewStore(runtime.db, _ReadOnlyCatalog()).state(
        AISettingsStore(runtime.db, runtime.config).snapshot(),
    )
    with _lock:
        thread = _threads.get(str(runtime.db.path))
        result["running"] = result["running"] or bool(thread and thread.is_alive())
    return result


def start_review(runtime: Runtime, *, manual: bool = False) -> bool:
    key = str(runtime.db.path)
    settings = AISettingsStore(runtime.db, runtime.config).snapshot()
    catalog = OpenAIModelCatalog()
    store = ModelReviewStore(runtime.db, catalog, assessments=local_assessments(runtime))
    if not manual and not store.due(settings):
        catalog.close()
        return False

    def run() -> None:
        try:
            store.run_if_due(settings, trigger="manual" if manual else "scheduled")
        except Exception:
            logger.exception("Model maintenance failed; telemetry worker continues")
        finally:
            catalog.close()

    with _lock:
        previous = _threads.get(key)
        if previous and previous.is_alive():
            catalog.close()
            return False
        thread = threading.Thread(target=run, name="ai-model-review", daemon=True)
        _threads[key] = thread
        thread.start()
    return True
