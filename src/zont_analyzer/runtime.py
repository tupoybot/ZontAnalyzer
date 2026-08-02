from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from zont_analyzer.adapters.openai import OpenAIAnalyst
from zont_analyzer.adapters.sqlite import Database
from zont_analyzer.adapters.sqlite.database import MigrationResult
from zont_analyzer.adapters.zont_readonly import ZontReadOnlyClient
from zont_analyzer.application.analysis import AnalysisService
from zont_analyzer.application.ingestion import IngestionService
from zont_analyzer.config import AppConfig, LoadedConfig, load_config


@dataclass
class Runtime:
    loaded: LoadedConfig
    db: Database
    migration: MigrationResult

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

    def analysis(self, *, no_ai: bool = False) -> AnalysisService:
        analyst = None
        api_key = self.loaded.secrets.openai_api_key
        if self.config.openai.enabled and not no_ai and api_key:
            analyst = OpenAIAnalyst(api_key=api_key.get_secret_value(), config=self.config, db=self.db)
        return AnalysisService(self.db, self.config, analyst)


def build_runtime(config_path: Path | None, data_dir: Path | None) -> Runtime:
    loaded = load_config(config_path, data_dir)
    db_path = Path(loaded.config.storage.path)
    if not db_path.is_absolute():
        db_path = loaded.data_dir / db_path
    db = Database(db_path)
    backup_dir = Path(loaded.config.storage.backup_dir)
    if not backup_dir.is_absolute():
        backup_dir = loaded.data_dir / backup_dir
    migration = db.initialize(backup_dir)
    return Runtime(loaded=loaded, db=db, migration=migration)
