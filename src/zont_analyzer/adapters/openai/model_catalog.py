"""Small, evidence-preserving reader for public OpenAI model documentation.

The catalog deliberately does not use the Models API: public documentation says
what OpenAI publishes, whereas the API would only say what one account can see.
No account availability is inferred from this module.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

MODELS_URL = "https://developers.openai.com/api/docs/models.md"
DEPRECATIONS_URL = "https://developers.openai.com/api/docs/deprecations.md"


@dataclass(frozen=True)
class ModelFact:
    id: str
    input_price_per_mtok_usd: str | None = None
    output_price_per_mtok_usd: str | None = None
    context_window_tokens: int | None = None
    reasoning_efforts: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    responses_supported: bool | None = None
    structured_outputs_supported: bool | None = None
    deprecated: bool = False
    deprecation_date: str | None = None
    source_url: str = MODELS_URL

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CatalogSnapshot:
    fetched_at: datetime
    sources: tuple[dict[str, str], ...]
    models: tuple[ModelFact, ...]
    incomplete: bool = False
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "fetched_at": self.fetched_at.astimezone(UTC).isoformat(),
            "sources": list(self.sources),
            "models": [model.as_dict() for model in self.models],
            "incomplete": self.incomplete,
            "error": self.error,
        }


def _plain(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


def _number(text: str) -> int | None:
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)([KMB]?)", text.replace(",", "").strip(), re.I)
    if not match:
        return None
    multiplier = {"": 1, "K": 1_000, "M": 1_000_000, "B": 1_000_000_000}[match.group(2).upper()]
    return int(float(match.group(1)) * multiplier)


def parse_models_html(html: str, *, source_url: str = MODELS_URL) -> dict[str, ModelFact]:
    """Parse explicitly-labelled public model cards, leaving absent facts unknown."""
    text = _plain(html)
    # Each card has a literal Model ID label followed by its stable API identifier.
    starts = list(re.finditer(r"\bModel ID\s+(gpt-[a-z0-9.\-]+)", text, re.I))
    facts: dict[str, ModelFact] = {}
    for index, start in enumerate(starts):
        chunk = text[start.start() : starts[index + 1].start() if index + 1 < len(starts) else len(text)]
        model_id = start.group(1).lower()
        input_match = re.search(r"Input price\s+\$([0-9.]+)\s*/\s*Input MTok", chunk, re.I)
        output_match = re.search(r"Output price\s+\$([0-9.]+)\s*/\s*Output MTok", chunk, re.I)
        context_match = re.search(r"Context window\s+([0-9.,]+\s*[KMB]?)", chunk, re.I)
        reasoning_match = re.search(
            r"Reasoning\s+(.+?)(?:Input price|Output price|Max output|Context window)", chunk, re.I
        )
        tools_match = re.search(
            r"Tools\s+(.+?)(?:Model ID|Reasoning|Input price|Output price|Max output|Context window|$)", chunk, re.I
        )
        efforts: tuple[str, ...] = ()
        if reasoning_match:
            efforts = tuple(
                item
                for item in ("none", "low", "medium", "high", "xhigh", "max")
                if re.search(rf"\b{item}\b", reasoning_match.group(1))
            )
        facts[model_id] = ModelFact(
            id=model_id,
            input_price_per_mtok_usd=input_match.group(1) if input_match else None,
            output_price_per_mtok_usd=output_match.group(1) if output_match else None,
            context_window_tokens=_number(context_match.group(1)) if context_match else None,
            reasoning_efforts=efforts,
            tools=tuple(item.strip() for item in tools_match.group(1).split(",") if item.strip())
            if tools_match
            else (),
            source_url=source_url,
        )
    return facts


def parse_model_markdown(markdown: str, *, source_url: str) -> ModelFact | None:
    """Parse one official `/models/<family>.md` page, never filling absent facts."""
    id_match = re.search(r"^Model ID:\s+`([^`]+)`", markdown, re.M)
    if not id_match:
        return None
    model_id = id_match.group(1).lower()
    pricing = re.search(
        r"\| Input \| \$([0-9.]+) \| 1M tokens \|.*?\| Output \| \$([0-9.]+) \| 1M tokens \|", markdown, re.S
    )
    context = re.search(r"- ([0-9,]+) context window", markdown)
    efforts = re.search(r"reasoning\.effort\s+supports:?\s+([^\.]+)\.", markdown.replace("`", ""), re.I)
    endpoints = re.search(r"\| Responses \| `v1/responses` \| (Supported|Not supported) \|", markdown)
    features = re.search(r"## Supported features\s+(.+?)(?:\n## |\Z)", markdown, re.S)
    feature_text = features.group(1) if features else ""
    return ModelFact(
        id=model_id,
        input_price_per_mtok_usd=pricing.group(1) if pricing else None,
        output_price_per_mtok_usd=pricing.group(2) if pricing else None,
        context_window_tokens=_number(context.group(1)) if context else None,
        reasoning_efforts=tuple(re.findall(r"\b(?:none|minimal|low|medium|high|xhigh|max)\b", efforts.group(1)))
        if efforts else (),
        responses_supported=(endpoints.group(1) == "Supported") if endpoints else None,
        structured_outputs_supported=True if "- structured_outputs" in feature_text else None,
        source_url=source_url,
    )


def parse_deprecations_html(html: str, *, source_url: str = DEPRECATIONS_URL) -> dict[str, str | None]:
    """Return only explicit model/date relationships from the official page."""
    result: dict[str, str | None] = {}
    # Official Markdown tables put the shutdown date in column one and the
    # deprecated model(s) in column two; never scan the replacement column.
    for line in html.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 2 or not cells[0] or set(cells[0]) <= {"-", ":", " "}:
            continue
        if not re.search(r"\d", cells[0]) or "model" in cells[0].lower():
            continue
        for model_id in re.findall(r"`(gpt-[a-z0-9.\-]+)`", cells[1], re.I):
            result[model_id.lower()] = cells[0]
    return result


def discover_featured_model_ids(markdown: str, limit: int = 4) -> tuple[str, ...]:
    """Read only the current, small Featured models section from the catalog."""
    featured = re.search(r"## Featured models\s+(.+?)(?:\n## |\Z)", markdown, re.S)
    if featured is None:
        return ()
    return tuple(
        dict.fromkeys(re.findall(r"\(/api/docs/models/([a-z0-9.\-]+)\.md\)", featured.group(1), re.I))
    )[:limit]


class OpenAIModelCatalog:
    """Fetch and parse the two public OpenAI documentation sources synchronously."""

    def __init__(self, client: httpx.Client | None = None, timeout_seconds: float = 15.0):
        self._client = client or httpx.Client(timeout=timeout_seconds, follow_redirects=False)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def fetch(self, now: datetime | None = None, model_ids: tuple[str, ...] = ()) -> CatalogSnapshot:
        fetched_at = (now or datetime.now(UTC)).astimezone(UTC)
        try:
            models_response = self._client.get(MODELS_URL)
            models_response.raise_for_status()
            deprecations_response = self._client.get(DEPRECATIONS_URL)
            deprecations_response.raise_for_status()
        except httpx.HTTPError as exc:
            return CatalogSnapshot(fetched_at, (), (), incomplete=True, error=f"official catalog unavailable: {exc}")
        facts: dict[str, ModelFact] = {}
        # A review has at most two candidates per profile plus the two configured
        # models; bounded page reads keep it independent of catalog size.
        discovered = discover_featured_model_ids(models_response.text)
        requested = tuple(dict.fromkeys(model_ids + discovered))[:8]
        for model_id in requested:
            url = f"https://developers.openai.com/api/docs/models/{model_id}.md"
            try:
                response = self._client.get(url)
                response.raise_for_status()
            except httpx.HTTPError:
                continue
            fact = parse_model_markdown(response.text, source_url=url)
            if fact is not None and fact.id == model_id:
                facts[fact.id] = fact
        deprecations = parse_deprecations_html(deprecations_response.text)
        incomplete = (not discovered or len(facts) != len(requested)
                      or not re.search(r"\|\s*Shutdown date\s*\|", deprecations_response.text, re.I))
        merged = tuple(
            ModelFact(
                **{
                    **fact.as_dict(),
                    "deprecated": model_id in deprecations,
                    "deprecation_date": deprecations.get(model_id),
                    "source_url": source_url,
                }
            )
            for model_id, fact in sorted(facts.items())
            for source_url in (fact.source_url,)
        )
        return CatalogSnapshot(
            fetched_at,
            ({"url": MODELS_URL, "fetched_at": fetched_at.isoformat()},)
            + tuple({"url": fact.source_url, "fetched_at": fetched_at.isoformat()} for fact in merged)
            + ({"url": DEPRECATIONS_URL, "fetched_at": fetched_at.isoformat()},),
            merged,
            incomplete=incomplete,
            error="Не удалось проверить все страницы моделей или формат списка прекращения поддержки."
            if incomplete else None,
        )
