from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from ..services.text_processing import count_words

from .base import (
    HTTPResponse,
    HTTPTransport,
    ProviderConfig,
    ProviderConfigurationError,
    ProviderRefusalError,
    ProviderResponseError,
    UrllibTransport,
    coerce_provider_config,
    ensure_http_success,
    json_request_body,
    perform_request,
    require_api_key,
)


_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])(?:[\"'\N{RIGHT SINGLE QUOTATION MARK}]*)\s+")


@dataclass(frozen=True, slots=True)
class TextRequest:
    text: str
    title: str = ""
    platform: str = ""
    code: str = ""
    ending_template: str = ""
    adult_mode: str = "engaging"
    retention_min: float = 0.85
    retention_max: float = 0.90
    language: str = "English"
    enforce_retention: bool = True
    creative_line_index: int = 1
    creative_line_count: int = 1
    purpose: str = "narration"

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise ValueError("Story text cannot be empty.")
        if self.adult_mode not in {"direct", "engaging"}:
            raise ValueError("adult_mode must be 'direct' or 'engaging'.")
        if not 0 < self.retention_min <= self.retention_max <= 1:
            raise ValueError("Retention must be between zero and one.")
        if int(self.creative_line_index) < 1:
            raise ValueError("creative_line_index must be positive.")
        if int(self.creative_line_count) < 1:
            raise ValueError("creative_line_count must be positive.")
        if int(self.creative_line_index) > int(self.creative_line_count):
            raise ValueError("creative_line_index cannot exceed creative_line_count.")
        if self.purpose not in {"narration", "intro_card"}:
            raise ValueError("purpose must be 'narration' or 'intro_card'.")

    @property
    def source_text(self) -> str:
        return self.text


@dataclass(frozen=True, slots=True)
class TextResult:
    polished_text: str
    hook: str
    ending_cta: str
    mood: str
    provider: str
    model: str = ""
    retention_ratio: float = 1.0
    raw_response: Any = field(default=None, repr=False, compare=False)

    def to_dict(self) -> dict[str, str | float]:
        return {
            "polished_text": self.polished_text,
            "hook": self.hook,
            "ending_cta": self.ending_cta,
            "mood": self.mood,
            "provider": self.provider,
            "model": self.model,
            "retention_ratio": self.retention_ratio,
        }


PolishResult = TextResult


def _word_count(text: str) -> int:
    return count_words(text)


def _make_request(value: TextRequest | str, kwargs: dict[str, Any]) -> TextRequest:
    if isinstance(value, TextRequest):
        if kwargs:
            raise TypeError("Keyword request fields cannot accompany a TextRequest.")
        return value
    if not isinstance(value, str):
        raise TypeError("polish() expects TextRequest or story text.")
    return TextRequest(text=value, **kwargs)


def _prompt_messages(request: TextRequest) -> list[dict[str, str]]:
    direct_instruction = (
        "Keep direct adult expression faithful to the source; improve only grammar, "
        "naturalness, rhythm, and clarity."
        if request.adult_mode == "direct"
        else "Make the narration more compelling through conflict, emotion, pacing, and "
        "suspense without inventing plot events or merely making it more explicit."
    )
    ending = request.ending_template or (
        "Download {platform} and search code {code} to continue reading."
    )
    if request.purpose == "intro_card":
        system = (
            f"You write a compact {request.language} story-preview card for a vertical "
            "short-form video. Return exactly one JSON object and no markdown or commentary. "
            "The object must contain exactly these string fields: polished_text, hook, "
            "ending_cta, mood. Treat the supplied synopsis or excerpt as the complete factual "
            "boundary: do not add names, relationships, motives, actions, outcomes, numbers, "
            "or reveals that are not explicitly present. polished_text must be one or two "
            "natural sentences, normally 20-28 words and no more than 155 characters "
            "(or no more than 70 characters for languages without spaces). Make the existing "
            "conflict and curiosity clearer through word "
            "choice and sentence order only. Do not include the title, platform, search code, "
            "download instruction, chapter label, or a generic call to action in polished_text. "
            "hook must be a short factually grounded headline. ending_cta may be a short neutral "
            "continuation line. mood must be one short lowercase category such as suspense, "
            "romance, sad, or revenge."
        )
    else:
        system = (
            f"You edit {request.language} novel narration for a vertical short-form story video. "
            "Return exactly one JSON object and no markdown or commentary. The object must "
            "contain exactly these string fields: polished_text, hook, ending_cta, mood. "
            "Preserve every plot-critical fact, character, relationship, event, and reveal. "
            "Remove only repetition and translation-like phrasing. Never summarize or silently "
            "stop early. Do not include chapter headings in polished_text. The input may contain "
            "the exact token [[CHAPTER_BREAK]]; preserve every occurrence in the same narrative "
            "position because it becomes silence and is never spoken. Use natural spoken "
            f"{request.language}. Keep roughly {request.retention_min:.0%} to "
            f"{request.retention_max:.0%} of the source narration length. {direct_instruction} "
            "The hook must be brief and story-specific. When several creative lines are "
            "requested, make this hook meaningfully distinct from the other lines while keeping "
            "every statement faithful to the supplied story. The ending_cta must create curiosity, "
            "then accurately tell the listener where and how to search. mood must be one short "
            "lowercase category such as suspense, romance, sad, or revenge."
        )
    user_payload = {
        "title": request.title,
        "platform": request.platform,
        "search_code": request.code,
        "creative_line": {
            "index": int(request.creative_line_index),
            "total": int(request.creative_line_count),
            "instruction": (
                "Use a distinct, factually accurate hook angle for this creative line; "
                "do not invent plot events."
            ),
        },
        "purpose": request.purpose,
        "ending_cta_template": ending,
        "story_text": request.text,
    }
    return [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": json.dumps(user_payload, ensure_ascii=False),
        },
    ]


def _decode_json_response(response: HTTPResponse, provider: str) -> Any:
    try:
        return response.json()
    except ValueError as error:
        raise ProviderResponseError(
            f"{provider} returned a response that was not valid JSON.",
            provider=provider,
            status_code=response.status_code,
            response_excerpt=response.text[:500],
        ) from error


def _decode_result_object(content: Any, provider: str) -> dict[str, Any]:
    if isinstance(content, dict):
        value = content
    elif isinstance(content, str):
        if not content.strip():
            raise ProviderResponseError(
                f"{provider} returned empty generated content.", provider=provider
            )
        try:
            value = json.loads(content)
        except json.JSONDecodeError as error:
            raise ProviderResponseError(
                f"{provider} did not return the required single JSON object.",
                provider=provider,
                response_excerpt=content[:500],
            ) from error
    else:
        raise ProviderResponseError(
            f"{provider} returned generated content of an unexpected type.",
            provider=provider,
        )
    if not isinstance(value, dict):
        raise ProviderResponseError(
            f"{provider} generated JSON, but it was not an object.", provider=provider
        )
    return value


def _validated_result(
    value: dict[str, Any],
    request: TextRequest,
    config: ProviderConfig,
    *,
    raw_response: Any,
) -> TextResult:
    required = ("polished_text", "hook", "ending_cta", "mood")
    missing = [key for key in required if not isinstance(value.get(key), str) or not value[key].strip()]
    if missing:
        raise ProviderResponseError(
            f"{config.name} omitted required non-empty fields: {', '.join(missing)}.",
            provider=config.name,
        )
    polished = value["polished_text"].strip()
    hook = value["hook"].strip()
    ending = value["ending_cta"].strip()
    mood = re.sub(r"[^a-z0-9_-]+", "-", value["mood"].strip().casefold()).strip("-")
    if not mood:
        raise ProviderResponseError(
            f"{config.name} returned an invalid mood.", provider=config.name
        )
    expected_breaks = request.text.count("[[CHAPTER_BREAK]]")
    actual_breaks = polished.count("[[CHAPTER_BREAK]]")
    if actual_breaks != expected_breaks:
        raise ProviderResponseError(
            f"{config.name} changed the chapter boundary markers "
            f"({actual_breaks} returned, {expected_breaks} expected).",
            provider=config.name,
        )
    if request.code and request.code.casefold() not in ending.casefold():
        raise ProviderResponseError(
            f"{config.name} omitted search code {request.code!r} from ending_cta.",
            provider=config.name,
        )
    source_words = _word_count(request.text)
    result_words = _word_count(polished)
    ratio = result_words / source_words if source_words else 1.0
    # Short snippets legitimately fluctuate too much for a word-ratio guard. For
    # story-sized input, this detects model token truncation even if the service
    # incorrectly reports a normal finish reason.
    if request.enforce_retention and source_words >= 100 and ratio < request.retention_min:
        raise ProviderResponseError(
            f"{config.name} returned only {ratio:.1%} of the source words; expected at "
            f"least {request.retention_min:.1%}. The output may be truncated.",
            provider=config.name,
        )
    return TextResult(
        polished_text=polished,
        hook=hook,
        ending_cta=ending,
        mood=mood,
        provider=config.name,
        model=config.model,
        retention_ratio=ratio,
        raw_response=raw_response,
    )


class TextProvider(ABC):
    def __init__(
        self, config: ProviderConfig, transport: HTTPTransport | None = None
    ) -> None:
        self.config = config
        self.transport = transport or UrllibTransport()

    def polish(self, request: TextRequest | str, **kwargs: Any) -> TextResult:
        normalized = _make_request(request, kwargs)
        value, raw = self._generate(normalized)
        return _validated_result(value, normalized, self.config, raw_response=raw)

    @abstractmethod
    def _generate(self, request: TextRequest) -> tuple[dict[str, Any], Any]:
        raise NotImplementedError

    def _post_json(
        self, endpoint: str, payload: Any, headers: dict[str, str]
    ) -> tuple[HTTPResponse, Any]:
        response = perform_request(
            self.transport,
            provider=self.config.name,
            method="POST",
            url=endpoint,
            headers={"Content-Type": "application/json", **headers},
            body=json_request_body(payload),
            timeout=self.config.timeout_seconds,
        )
        ensure_http_success(self.config.name, response)
        return response, _decode_json_response(response, self.config.name)


class GroqTextProvider(TextProvider):
    DEFAULT_ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"
    DEFAULT_MODEL = "llama-3.3-70b-versatile"

    def __init__(
        self, config: ProviderConfig, transport: HTTPTransport | None = None
    ) -> None:
        if not config.model:
            config = ProviderConfig(
                name=config.name,
                model=self.DEFAULT_MODEL,
                endpoint=config.endpoint,
                api_key=config.api_key,
                timeout_seconds=config.timeout_seconds,
                options=config.options,
            )
        require_api_key(config)
        super().__init__(config, transport)

    def _generate(self, request: TextRequest) -> tuple[dict[str, Any], Any]:
        payload = {
            "model": self.config.model,
            "messages": _prompt_messages(request),
            "temperature": float(self.config.options.get("temperature", 0.45)),
            "response_format": {"type": "json_object"},
        }
        if self.config.options.get("max_completion_tokens"):
            payload["max_completion_tokens"] = int(
                self.config.options["max_completion_tokens"]
            )
        _, raw = self._post_json(
            self.config.endpoint or self.DEFAULT_ENDPOINT,
            payload,
            {"Authorization": f"Bearer {self.config.api_key}"},
        )
        try:
            choice = raw["choices"][0]
            message = choice["message"]
        except (KeyError, IndexError, TypeError) as error:
            raise ProviderResponseError(
                "Groq returned an unexpected chat-completions response.",
                provider=self.config.name,
            ) from error
        refusal = message.get("refusal") if isinstance(message, dict) else None
        if refusal:
            raise ProviderRefusalError(
                f"Groq refused the content: {str(refusal)[:300]}",
                provider=self.config.name,
            )
        finish_reason = str(choice.get("finish_reason") or "").casefold()
        if finish_reason in {"content_filter", "safety"}:
            raise ProviderRefusalError(
                f"Groq stopped because of {finish_reason}.", provider=self.config.name
            )
        if finish_reason and finish_reason not in {"stop"}:
            raise ProviderResponseError(
                f"Groq did not finish the response (finish_reason={finish_reason}).",
                provider=self.config.name,
            )
        return _decode_result_object(message.get("content"), self.config.name), raw


GroqProvider = GroqTextProvider


class CloudflareTextProvider(TextProvider):
    DEFAULT_MODEL = "@cf/meta/llama-3.3-70b-instruct-fp8-fast"

    def __init__(
        self, config: ProviderConfig, transport: HTTPTransport | None = None
    ) -> None:
        if not config.model:
            config = ProviderConfig(
                name=config.name,
                model=self.DEFAULT_MODEL,
                endpoint=config.endpoint,
                api_key=config.api_key,
                timeout_seconds=config.timeout_seconds,
                options=config.options,
            )
        require_api_key(config)
        super().__init__(config, transport)

    def _endpoint(self) -> str:
        if self.config.endpoint:
            endpoint = self.config.endpoint
            if "{model}" in endpoint:
                endpoint = endpoint.format(model=quote(self.config.model, safe="@/"))
            return endpoint
        account_id = str(self.config.options.get("account_id") or "").strip()
        if not account_id:
            raise ProviderConfigurationError(
                "Cloudflare requires either a full endpoint or options.account_id.",
                provider=self.config.name,
            )
        model_path = quote(self.config.model, safe="@/")
        return f"https://api.cloudflare.com/client/v4/accounts/{quote(account_id)}/ai/run/{model_path}"

    def _generate(self, request: TextRequest) -> tuple[dict[str, Any], Any]:
        payload = {
            "messages": _prompt_messages(request),
            "temperature": float(self.config.options.get("temperature", 0.45)),
            "response_format": {"type": "json_object"},
        }
        _, raw = self._post_json(
            self._endpoint(),
            payload,
            {"Authorization": f"Bearer {self.config.api_key}"},
        )
        if not isinstance(raw, dict):
            raise ProviderResponseError(
                "Cloudflare returned an unexpected Workers AI response.",
                provider=self.config.name,
            )
        if raw.get("success") is False:
            errors = json.dumps(raw.get("errors") or [], ensure_ascii=False)
            lowered = errors.casefold()
            if any(word in lowered for word in ("safety", "policy", "moderation", "refus")):
                raise ProviderRefusalError(
                    f"Cloudflare refused the content: {errors[:300]}",
                    provider=self.config.name,
                )
            raise ProviderResponseError(
                f"Cloudflare reported a Workers AI error: {errors[:300]}",
                provider=self.config.name,
            )
        result = raw.get("result")
        if isinstance(result, dict) and "response" in result:
            content = result["response"]
        elif isinstance(result, dict) and isinstance(result.get("choices"), list):
            try:
                content = result["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError) as error:
                raise ProviderResponseError(
                    "Cloudflare returned malformed generated content.",
                    provider=self.config.name,
                ) from error
        else:
            content = result
        return _decode_result_object(content, self.config.name), raw


CloudflareWorkersAIProvider = CloudflareTextProvider


class OllamaTextProvider(TextProvider):
    DEFAULT_ENDPOINT = "http://127.0.0.1:11434/api/chat"
    DEFAULT_MODEL = "llama3.1:8b"

    def __init__(
        self, config: ProviderConfig, transport: HTTPTransport | None = None
    ) -> None:
        if not config.model:
            config = ProviderConfig(
                name=config.name,
                model=self.DEFAULT_MODEL,
                endpoint=config.endpoint,
                api_key=config.api_key,
                timeout_seconds=config.timeout_seconds,
                options=config.options,
            )
        super().__init__(config, transport)

    def _generate(self, request: TextRequest) -> tuple[dict[str, Any], Any]:
        payload = {
            "model": self.config.model,
            "messages": _prompt_messages(request),
            "stream": False,
            "format": "json",
            "options": {
                "temperature": float(self.config.options.get("temperature", 0.35))
            },
        }
        headers = (
            {"Authorization": f"Bearer {self.config.api_key}"}
            if self.config.api_key
            else {}
        )
        _, raw = self._post_json(
            self.config.endpoint or self.DEFAULT_ENDPOINT, payload, headers
        )
        try:
            content = raw["message"]["content"]
        except (KeyError, TypeError) as error:
            raise ProviderResponseError(
                "Ollama returned an unexpected chat response.",
                provider=self.config.name,
            ) from error
        if raw.get("done") is False:
            raise ProviderResponseError(
                "Ollama returned an incomplete response.", provider=self.config.name
            )
        reason = str(raw.get("done_reason") or "stop").casefold()
        if reason not in {"stop"}:
            raise ProviderResponseError(
                f"Ollama did not finish the response (done_reason={reason}).",
                provider=self.config.name,
            )
        return _decode_result_object(content, self.config.name), raw


OllamaProvider = OllamaTextProvider


_MOJIBAKE_REPLACEMENTS = {
    "\u00e2\u20ac\u2122": "\N{RIGHT SINGLE QUOTATION MARK}",
    "\u00e2\u20ac\u0153": "\N{LEFT DOUBLE QUOTATION MARK}",
    "\u00e2\u20ac\u009d": "\N{RIGHT DOUBLE QUOTATION MARK}",
    "\u00e2\u20ac\u201c": "\N{EM DASH}",
    "\u00c2\u00a0": " ",
}


def _local_clean(text: str, *, remove_adjacent_duplicates: bool) -> str:
    for broken, replacement in _MOJIBAKE_REPLACEMENTS.items():
        text = text.replace(broken, replacement)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    paragraphs = [re.sub(r"[ \t]+", " ", item).strip() for item in text.split("\n")]
    paragraphs = [item for item in paragraphs if item]
    if not remove_adjacent_duplicates:
        return "\n\n".join(paragraphs)
    output: list[str] = []
    previous = ""
    for paragraph in paragraphs:
        marker = re.sub(r"\W+", " ", paragraph).strip().casefold()
        if marker and marker == previous:
            continue
        output.append(paragraph)
        previous = marker
    return "\n\n".join(output)


def _infer_mood(text: str) -> str:
    lowered = text.casefold()

    def count(pattern: str) -> int:
        return len(re.findall(pattern, lowered))

    # Relationship nouns are intentionally not scored by themselves.  A husband,
    # wife, or marriage is common in ordinary romance (and in every other genre);
    # it becomes a betrayal signal only when the story also contains concrete
    # evidence such as cheating, a mistress, or divorce.
    relationship_hits = count(
        r"\b(?:husband|wife|spouse|marriage|married|fianc(?:e|ee))s?\b"
    )
    betrayal_hits = count(
        r"\b(?:"
        r"cheat(?:s|ed|ing)?|"
        r"betray(?:s|ed|ing|al)?|"
        r"mistress(?:es)?|"
        r"adulter(?:y|ous)|"
        r"infidelit(?:y|ies)|unfaithful|"
        r"affairs?|"
        r"divorc(?:e|ed|es|ing)|"
        r"two[- ]tim(?:e|ed|es|ing)"
        r")\b"
    )
    explicit_revenge_hits = (
        count(
            r"\b(?:revenge|payback|retaliat(?:e|ed|es|ing|ion)|vengeance)\b"
        )
        + count(r"\b(?:make|made|making) (?:him|her|them) pay\b")
        + count(
            r"\b(?:decided|swore|vowed|promised) (?:that )?"
            r"(?:he|she|they) (?:would|will) pay\b"
        )
        + count(
            r"\b(?:expos(?:e|ed|es|ing)|destroy(?:ed|s|ing)?|"
            r"ruin(?:ed|s|ing)?|punish(?:ed|es|ing)?) "
            r"(?:him|her|them|his|her|their|the (?:cheater|mistress|affair))\b"
        )
        + count(r"\bteach(?:ing|es|t)? (?:him|her|them) a lesson\b")
        + count(r"\bt(?:ake|ook|aking) everything (?:away )?from (?:him|her|them)\b")
    )

    bereavement_hits = count(
        r"\b(?:died|death|dead|funeral|grief|grieving|mourn|mourned|mourning)\b"
    )
    emotional_pain_hits = count(
        r"\b(?:cry|cried|crying|tears?|devastated|heartbroken)\b"
    )

    scores = {
        "romance": 2
        * count(r"\b(?:love|loved|loves|loving|kiss|kissed|kisses|kissing)\b")
        + 3
        * count(
            r"\b(?:wedding|romance|romantic|soulmate|bride|groom|devotion|affection)\b"
        )
        + count(r"\bhearts?\b"),
        "sad": 6 * bereavement_hits + 2 * emotional_pain_hits,
        # Discovering an affair is not revenge.  This score stays at zero until
        # the narrator expresses an actual intent or action: payback, exposing
        # the offender, destroying them, making them pay, and so on.
        "revenge": (
            25 * explicit_revenge_hits + 6 * betrayal_hits
            if explicit_revenge_hits
            else 0
        ),
        "suspense": 3
        * count(
            r"\b(?:secret|murder|murdered|blood|bloody|disappear|disappeared|"
            r"missing|threat|threatened|blackmail|unknown caller|anonymous caller)\b"
        )
        + 2
        * count(
            r"\b(?:suddenly|truth|clue|evidence|proof|mystery|mysterious|"
            r"discover|discovered|discovers|reveal|revealed|reveals)\b"
        )
        + 5 * betrayal_hits,
    }

    if betrayal_hits:
        # Repeated betrayal evidence, or one such fact inside a marriage, marks
        # a reveal/conflict story.  It must outweigh sentimental flashbacks, but
        # it must not imply that the betrayed character has already retaliated.
        if betrayal_hits >= 2 or relationship_hits:
            scores["suspense"] += 6 + min(relationship_hits, 4)
        scores["romance"] = max(0, scores["romance"] - 3 * betrayal_hits)

    # In an exact tie, prefer the more plot-driving mood.  Dict insertion order
    # would otherwise let a few incidental romance words win over equal conflict.
    priority = {"romance": 0, "sad": 1, "suspense": 2, "revenge": 3}
    winner, score = max(
        scores.items(), key=lambda item: (item[1], priority[item[0]])
    )
    return winner if score else "suspense"


class LocalTextProvider(TextProvider):
    """Offline deterministic fallback.

    ``options.mode=passthrough`` performs only encoding/whitespace repair;
    ``options.mode=rules`` also removes immediately repeated paragraphs.  It
    intentionally makes no claim of AI-quality rewriting.
    """

    def _generate(self, request: TextRequest) -> tuple[dict[str, Any], Any]:
        mode = str(self.config.options.get("mode") or "rules").casefold()
        if mode not in {"passthrough", "rules", "rule"}:
            raise ProviderConfigurationError(
                "Local text mode must be 'passthrough' or 'rules'.",
                provider=self.config.name,
            )
        polished = _local_clean(
            request.text, remove_adjacent_duplicates=mode in {"rules", "rule"}
        )
        sentences = [item.strip() for item in _SENTENCE_SPLIT_RE.split(polished) if item.strip()]
        first = sentences[0] if sentences else polished
        hook = first if len(first) <= 180 else first[:177].rstrip() + "..."
        if request.ending_template:
            try:
                ending = request.ending_template.format(
                    platform=request.platform, code=request.code
                )
            except (KeyError, ValueError) as error:
                raise ProviderConfigurationError(
                    f"Invalid ending CTA template: {error}", provider=self.config.name
                ) from error
        elif request.platform and request.code:
            ending = (
                f"Download {request.platform} and search code {request.code} to "
                "continue reading."
            )
        elif request.code:
            ending = f"Search code {request.code} to continue reading."
        else:
            ending = "Continue reading to discover what happens next."
        value = {
            "polished_text": polished,
            "hook": hook,
            "ending_cta": ending,
            "mood": _infer_mood(polished),
        }
        return value, {"mode": mode, "offline": True}


LocalPassthroughProvider = LocalTextProvider
LocalRuleTextProvider = LocalTextProvider


def create_text_provider(
    config: ProviderConfig | Any = None,
    *,
    transport: HTTPTransport | None = None,
) -> TextProvider:
    normalized = coerce_provider_config(config, kind="text")
    name = normalized.name.casefold().replace("-", "_").strip()
    if name in {"groq", "groq_openai", "groq_openai_compatible"}:
        return GroqTextProvider(normalized, transport)
    if name in {"cloudflare", "cloudflare_workers_ai", "workers_ai"}:
        return CloudflareTextProvider(normalized, transport)
    if name in {"ollama", "ollama_local"}:
        return OllamaTextProvider(normalized, transport)
    if name in {"local", "local_rules", "local_rule", "local_passthrough", "passthrough"}:
        if name in {"local_passthrough", "passthrough"} and "mode" not in normalized.options:
            normalized = ProviderConfig(
                name=normalized.name,
                model=normalized.model,
                endpoint=normalized.endpoint,
                api_key=normalized.api_key,
                timeout_seconds=normalized.timeout_seconds,
                options={**normalized.options, "mode": "passthrough"},
            )
        return LocalTextProvider(normalized, transport)
    raise ProviderConfigurationError(
        f"Unsupported text provider {normalized.name!r}. Supported providers are "
        "Groq, Cloudflare Workers AI, Ollama, and local.",
        provider=normalized.name,
    )


__all__ = [
    "CloudflareTextProvider",
    "CloudflareWorkersAIProvider",
    "GroqProvider",
    "GroqTextProvider",
    "LocalPassthroughProvider",
    "LocalRuleTextProvider",
    "LocalTextProvider",
    "OllamaProvider",
    "OllamaTextProvider",
    "PolishResult",
    "TextProvider",
    "TextRequest",
    "TextResult",
    "create_text_provider",
]
