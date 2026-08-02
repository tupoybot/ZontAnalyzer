from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from zont_analyzer.runtime import Runtime


def run_doctor(runtime: Runtime, *, live: bool = False) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})

    data_dir = runtime.loaded.data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    add("data_directory", data_dir.is_dir(), str(data_dir))
    usage = shutil.disk_usage(data_dir)
    add("free_disk", usage.free > 100 * 1024 * 1024, f"{usage.free} bytes free")
    integrity = runtime.db.integrity_check()
    add("sqlite_integrity", integrity == "ok", integrity)
    token = runtime.loaded.secrets.zont_token
    credentials_ok = bool(token and runtime.config.zont.client_email)
    add("zont_credentials", credentials_ok, "present" if credentials_ok else "missing token or client email")
    if runtime.config.openai.enabled:
        add("openai_key", bool(runtime.loaded.secrets.openai_api_key), "required because OpenAI is enabled")
    else:
        add("openai_key", True, "optional; AI is disabled")
    token_file = runtime.loaded.zont_token_path or Path(runtime.config.zont.token_file)
    if token_file.exists():
        mode = token_file.stat().st_mode & 0o777
        add("token_file_mode", mode & 0o077 == 0, oct(mode))
    if runtime.loaded.openai_key_path is not None:
        mode = runtime.loaded.openai_key_path.stat().st_mode & 0o777
        add("openai_key_file_mode", mode & 0o077 == 0, oct(mode))
    if live and token and runtime.config.zont.client_email:
        try:
            with runtime.zont_client() as client:
                add("zont_api", client.healthcheck(), "read-only devices endpoint")
        except Exception as exc:
            add("zont_api", False, f"{type(exc).__name__}: {exc}")
    return {"ok": all(item["ok"] for item in checks), "checks": checks}
