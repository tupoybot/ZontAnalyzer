"""Bounded, explicitly requested external API checks; never generates reports."""
from __future__ import annotations

import json
import os
import re
from typing import Any

from .egress import PolicyClient


def check(payload: dict[str, Any]) -> dict[str, Any]:
    if set(payload) != {"service"} or payload["service"] not in ("zont", "openai", "openai_tls"):
        raise ValueError("select exactly one supported service")
    client = PolicyClient()
    if payload["service"] == "openai_tls":
        client.verify_openai_tls()
        return {"service": "openai_tls", "route": "xray", "verified_tls": True, "api_requests": 0}
    if payload["service"] == "zont":
        data = json.loads(client.request(
            "https://my.zont.online/api/devices", method="POST", body=b'{"load_io":false}',
            headers={"X-ZONT-Client": os.environ["ZONT_CLIENT_EMAIL"],
                     "X-ZONT-Token": os.environ["ZONT_TOKEN"], "Content-Type": "application/json"},
        ))
        if data.get("ok") is not True or not isinstance(data.get("devices"), list):
            raise RuntimeError("ZONT response rejected")
        return {"service": "zont", "http_status": 200, "route": "direct", "read_only": True}
    if os.environ.get("CLOUD_OPENAI_ACCESS_CONFIRMED") != "true":
        raise PermissionError("OpenAI access conditions must be confirmed")
    model = os.environ["CLOUD_OPENAI_MODEL"]
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", model):
        raise ValueError("invalid metadata model")
    data = json.loads(client.request(
        f"https://api.openai.com/v1/models/{model}",
        headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"},
    ))
    if data.get("object") != "model" or data.get("id") != model:
        raise RuntimeError("OpenAI metadata rejected")
    return {"service": "openai", "http_status": 200, "route": "xray", "generation": False}
