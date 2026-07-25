"""Research provider: structured evidence gathering.

The research agent's job is to extract *what fact decides this market* and find dated
evidence about it - not to opine on a probability. Its output is validated against a
Pydantic schema before it is allowed anywhere near the ensemble, and it enters the
ensemble as one weak member whose weight is bounded by the quality and novelty of the
evidence it actually produced.

Fluency is not evidence. A response with no sources, or only sources that predate the
last market move, earns near-zero weight regardless of how confident its prose is.

The default is :class:`NullResearchProvider`, which returns nothing. That is the
correct behaviour with no API key configured: contributing no evidence is honest,
whereas contributing invented evidence is not.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol

from pydantic import BaseModel, Field, ValidationError, field_validator

from pmvl_shared.config import get_settings
from pmvl_shared.enums import EvidenceStance
from pmvl_shared.logging_setup import get_logger
from pmvl_shared.money import D, ZERO, clamp_prob
from pmvl_shared.schemas import EvidenceRecord, NormalizedMarket
from pmvl_shared.timeutil import parse_ts, utcnow

log = get_logger(__name__)


# --------------------------------------------------------------------- schema
class ResearchSource(BaseModel):
    """One cited source. Unparseable entries are dropped, not repaired."""

    name: str = Field(min_length=1, max_length=200)
    url: str = ""
    title: str = ""
    summary: str = ""
    stance: EvidenceStance = EvidenceStance.NEUTRAL
    published_at: datetime | None = None
    event_time: datetime | None = None
    #: Whether this reports something new, or repeats an existing story.
    is_novel: bool = True
    #: 0-1 assessment of source reliability.
    source_quality: Decimal = Decimal("0.5")

    @field_validator("published_at", "event_time", mode="before")
    @classmethod
    def _parse(cls, v: Any) -> datetime | None:
        return parse_ts(v)

    @field_validator("source_quality", mode="before")
    @classmethod
    def _quality(cls, v: Any) -> Decimal:
        try:
            return max(ZERO, min(D(1), D(v)))
        except Exception:  # noqa: BLE001
            return Decimal("0.5")


class ResearchResult(BaseModel):
    """The structured contract every research provider must satisfy."""

    #: The specific factual question that decides settlement.
    decisive_question: str = ""
    sources: list[ResearchSource] = Field(default_factory=list)
    supports_yes: list[str] = Field(default_factory=list)
    supports_no: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    #: The model's probability, which is treated as *one weak opinion*, never as the
    #: answer. May be None when the evidence does not support a numeric view.
    probability: Decimal | None = None
    #: The model's own confidence. Capped hard downstream; see ResearchModel.
    self_reported_confidence: Decimal = Decimal("0")
    reasoning: str = ""
    provider: str = ""

    @field_validator("probability", mode="before")
    @classmethod
    def _prob(cls, v: Any) -> Decimal | None:
        if v is None or v == "":
            return None
        try:
            return clamp_prob(D(v))
        except Exception:  # noqa: BLE001
            return None

    @field_validator("self_reported_confidence", mode="before")
    @classmethod
    def _conf(cls, v: Any) -> Decimal:
        try:
            return max(ZERO, min(D(1), D(v)))
        except Exception:  # noqa: BLE001
            return ZERO

    def to_evidence(self) -> list[EvidenceRecord]:
        return [
            EvidenceRecord(
                source_name=s.name,
                source_url=s.url,
                title=s.title,
                summary=s.summary,
                stance=s.stance,
                published_at=s.published_at,
                event_time=s.event_time,
                is_novel=s.is_novel,
                source_quality=s.source_quality,
                conflicts_with=self.conflicts,
                provider=self.provider,
            )
            for s in self.sources
        ]

    def evidence_quality(self) -> Decimal:
        """Aggregate quality in [0,1], driven by novelty and source reliability.

        Repeated coverage of the same story is deliberately discounted: five outlets
        rewriting one wire report is one piece of evidence, not five.
        """
        if not self.sources:
            return ZERO
        novel = [s for s in self.sources if s.is_novel]
        if not novel:
            return min(D("0.25"), max((s.source_quality for s in self.sources), default=ZERO))
        best = max(s.source_quality for s in novel)
        breadth = min(D(len(novel)) / D(3), D(1))
        return min(D(1), best * (D("0.7") + D("0.3") * breadth))


class ResearchProvider(Protocol):
    """Swappable research backend."""

    name: str

    async def research(self, market: NormalizedMarket) -> ResearchResult | None: ...

    async def aclose(self) -> None: ...


class BaseResearchProvider(ABC):
    name = "base"

    @abstractmethod
    async def research(self, market: NormalizedMarket) -> ResearchResult | None: ...

    async def aclose(self) -> None:
        return None


class NullResearchProvider(BaseResearchProvider):
    """Returns nothing. The default when no research backend is configured."""

    name = "null"

    async def research(self, market: NormalizedMarket) -> ResearchResult | None:
        return None


class AnthropicResearchProvider(BaseResearchProvider):
    """Claude-backed research, gated behind an explicit opt-in.

    Requires ``ANTHROPIC_API_KEY`` **and** ``RESEARCH_ENABLED=true``. The response is
    parsed strictly: any output that fails schema validation is discarded rather than
    coerced, because a half-parsed research result is worse than none.
    """

    name = "anthropic"

    def __init__(self) -> None:
        settings = get_settings()
        self._api_key = settings.anthropic_api_key
        self._model = settings.research_model
        self._client: Any = None

    @property
    def available(self) -> bool:
        settings = get_settings()
        return bool(self._api_key) and settings.research_enabled

    async def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from anthropic import AsyncAnthropic  # type: ignore[import-not-found]
        except ImportError:
            log.warning(
                "research enabled but the `anthropic` package is not installed; "
                "falling back to no research"
            )
            return None
        self._client = AsyncAnthropic(api_key=self._api_key)
        return self._client

    def _prompt(self, market: NormalizedMarket) -> str:
        return (
            "You are a research assistant for a prediction-market analysis system.\n"
            "Extract evidence. Do NOT speculate, and do NOT invent sources or dates.\n\n"
            f"MARKET: {market.title}\n"
            f"SUBTITLE: {market.subtitle}\n"
            f"SETTLEMENT RULES: {market.settlement_rules_raw[:2000]}\n"
            f"SETTLEMENT SOURCE: {market.settlement_source}\n"
            f"EXPECTED RESOLUTION (UTC): {market.expected_resolution_time}\n\n"
            "Return ONLY a JSON object with these keys:\n"
            '  decisive_question (string), sources (array of {name, url, title, '
            'summary, stance in [supports_yes|supports_no|neutral|conflicting], '
            'published_at ISO8601, event_time ISO8601, is_novel bool, source_quality '
            '0-1}), supports_yes (array of strings), supports_no (array of strings), '
            'conflicts (array of strings), probability (0-1 or null), '
            'self_reported_confidence (0-1), reasoning (string).\n\n'
            "Rules you must follow:\n"
            "- If you have no dated, verifiable evidence, return an empty sources "
            "array, probability null and self_reported_confidence 0.\n"
            "- published_at is when the SOURCE published; event_time is when the "
            "underlying event occurred. A re-report of an old event is not novel.\n"
            "- Never cite a URL you are not certain exists.\n"
        )

    async def research(self, market: NormalizedMarket) -> ResearchResult | None:
        if not self.available:
            return None
        client = await self._ensure_client()
        if client is None:
            return None

        try:
            response = await client.messages.create(
                model=self._model,
                max_tokens=2000,
                messages=[{"role": "user", "content": self._prompt(market)}],
            )
            text = "".join(
                block.text for block in response.content if getattr(block, "type", "") == "text"
            )
        except Exception as exc:  # noqa: BLE001 - research must never break a scan
            log.warning("research call failed for %s: %s", market.platform_market_id, exc)
            return None

        return parse_research_response(text, provider=self.name)


def parse_research_response(text: str, *, provider: str = "") -> ResearchResult | None:
    """Strictly parse a research response into :class:`ResearchResult`.

    Returns ``None`` on any validation failure. Nothing is repaired or defaulted:
    an unparseable response means the provider produced no usable evidence.
    """
    import json
    import re

    if not text or not text.strip():
        return None

    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?\s*|\s*```$", "", candidate, flags=re.MULTILINE)
    match = re.search(r"\{.*\}", candidate, re.DOTALL)
    if not match:
        return None

    try:
        payload = json.loads(match.group(0))
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None

    payload["provider"] = provider
    try:
        return ResearchResult.model_validate(payload)
    except ValidationError as exc:
        log.debug("research response failed schema validation: %s", exc)
        return None


def get_research_provider() -> BaseResearchProvider:
    """Select the configured backend, defaulting to the null provider."""
    settings = get_settings()
    if settings.research_enabled and settings.anthropic_api_key:
        return AnthropicResearchProvider()
    return NullResearchProvider()
