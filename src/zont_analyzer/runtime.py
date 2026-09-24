from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from zont_analyzer.adapters.openai import OpenAIAnalyst
from zont_analyzer.adapters.ydb.application import Database
from zont_analyzer.adapters.ydb.database import YdbConfig
from zont_analyzer.adapters.zont_readonly import ZontReadOnlyClient
from zont_analyzer.application.analysis import AnalysisService
from zont_analyzer.application.ingestion import IngestionService
from zont_analyzer.config import AppConfig, LoadedConfig, load_config

logger = logging.getLogger(__name__)


@dataclass
class Runtime:
    loaded: LoadedConfig
    db: Database

    @property
    def config(self) -> AppConfig:
        return self.loaded.config

    def zont_client(self) -> ZontReadOnlyClient:
        token = self.loaded.secrets.zont_token
        email = self.loaded.config.zont.client_email
        if token is None or not email:
            raise RuntimeError("ZONT_TOKEN and ZONT_CLIENT_EMAIL (or token file with email) are required")
        return ZontReadOnlyClient(
            token=token.get_secret_value(),
            client_email=email,
            base_url=self.config.zont.base_url,
            timeout=self.config.zont.request_timeout_seconds,
        )

    def ingestion(self, client: ZontReadOnlyClient) -> IngestionService:
        return IngestionService(self.db, client, self.config)

    def analysis(
        self, *, no_ai: bool = False, job_fence: tuple[str, str, int] | None = None,
    ) -> AnalysisService:
        from zont_analyzer.application.ai_settings import AISettingsStore

        config = AISettingsStore(self.db, self.config).effective_config()
        analyst = None
        api_key = self.loaded.secrets.openai_api_key
        if config.openai.enabled and not no_ai and api_key:
            analyst = OpenAIAnalyst(api_key=api_key.get_secret_value(), config=config, db=self.db)
        return AnalysisService(self.db, config, analyst, job_fence=job_fence)

    def maintain_recommendation_lifecycle(self, *, now: datetime | None = None) -> dict[str, Any]:
        """Expire unanswered recommendations and retain an auditable pre-change count."""
        reference = now or datetime.now(UTC)
        eligible = self.db.stale_recommendation_count(now=reference)
        if eligible:
            logger.info(
                "recommendation maintenance: %d new recommendation(s) older than 48 hours will be ignored",
                eligible,
            )
        result = self.db.expire_stale_recommendations(now=reference)
        if eligible:
            logger.info(
                "recommendation maintenance complete: ignored=%d statuses=%s",
                result["ignored"],
                result["status_counts"],
            )
        return result


def build_runtime(config_path: Path | None, data_dir: Path | None) -> Runtime:
    loaded = load_config(config_path, data_dir)
    storage = YdbConfig.from_environment(namespace=loaded.config.storage.namespace)
    db = Database(storage)
    try:
        db.initialize()
        runtime = Runtime(loaded=loaded, db=db)
        from zont_analyzer.application.timezone import apply_device_timezone
        apply_device_timezone(db, loaded.config)
        runtime.maintain_recommendation_lifecycle()
        return runtime
    except BaseException:
        db.close()
        raise
