from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    """Provider-neutral configuration used by all adapters.

    ``options`` is deliberately provider-specific.  Keeping it separate prevents
    API-only details (for example a Cloudflare account id or a Kokoro CLI
    command) from leaking into the application's common settings model.
    """

    name: str
    model: str = ""
    endpoint: str = ""
    api_key: str = ""
    timeout_seconds: float = 90.0
    options: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("Provider name cannot be empty.")
        if self.timeout_seconds <= 0:
            raise ValueError("Provider timeout must be greater than zero.")


def coerce_provider_config(value: Any, *, kind: str) -> ProviderConfig:
    """Convert app settings, mappings, or an existing config into one config.

    ``kind`` must be ``"text"`` or ``"tts"``.  This accepts the existing
    :class:`storyforge.models.ProviderSettings` object without importing the
    application model here, which keeps the provider layer reusable and avoids
    a circular import.
    """

    if isinstance(value, ProviderConfig):
        return value
    if kind not in {"text", "tts"}:
        raise ValueError("Provider kind must be 'text' or 'tts'.")
    if value is None:
        return ProviderConfig(name="local" if kind == "text" else "local_kokoro")

    if isinstance(value, str):
        return ProviderConfig(name=value)

    source: Mapping[str, Any]
    if isinstance(value, Mapping):
        source = value
        getter: Callable[[str, Any], Any] = source.get
    else:
        getter = lambda key, default=None: getattr(value, key, default)
        source = {}

    prefix = f"{kind}_"
    name = getter(prefix + "provider", None) or getter("name", None) or getter(
        "provider", None
    )
    if not name:
        name = "local" if kind == "text" else "local_kokoro"
    model = getter(prefix + "model", None) or getter("model", "")
    endpoint = getter(prefix + "endpoint", None) or getter("endpoint", "")
    api_key = getter(prefix + "api_key", None) or getter("api_key", "")
    timeout = (
        getter(prefix + "timeout_seconds", None)
        or getter("timeout_seconds", None)
        or getter("timeout", 90.0)
    )
    options = getter(prefix + "options", None) or getter("options", None) or getter(
        "extra", {}
    )
    if not isinstance(options, Mapping):
        raise ValueError("Provider options must be a mapping.")

    if source:
        known = {
            "name",
            "provider",
            "model",
            "endpoint",
            "api_key",
            "timeout",
            "timeout_seconds",
            "options",
            "extra",
            prefix + "provider",
            prefix + "model",
            prefix + "endpoint",
            prefix + "api_key",
            prefix + "timeout_seconds",
            prefix + "options",
        }
        extras = {key: item for key, item in source.items() if key not in known}
        options = {**extras, **dict(options)}

    return ProviderConfig(
        name=str(name).strip(),
        model=str(model or "").strip(),
        endpoint=str(endpoint or "").strip(),
        api_key=str(api_key or "").strip(),
        timeout_seconds=float(timeout),
        options=dict(options),
    )


@dataclass(frozen=True, slots=True)
class HTTPResponse:
    status_code: int
    body: bytes = b""
    headers: Mapping[str, str] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("The provider response was not valid UTF-8 JSON.") from error


@runtime_checkable
class HTTPTransport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
        timeout: float = 90.0,
    ) -> HTTPResponse: ...


class TransportFailure(OSError):
    """Low-level connection failure raised before a provider can respond."""


class UrllibTransport:
    """Small standard-library HTTP transport.

    Tests inject a fake object implementing :class:`HTTPTransport`, so this
    class is never required to contact a real service during unit tests.
    """

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
        timeout: float = 90.0,
    ) -> HTTPResponse:
        request = Request(
            url,
            data=body,
            headers=dict(headers or {}),
            method=method.upper(),
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                return HTTPResponse(
                    status_code=int(response.status),
                    body=response.read(),
                    headers={key.lower(): value for key, value in response.headers.items()},
                )
        except HTTPError as error:
            # HTTP failures still carry useful provider status and response data.
            response_headers = error.headers or {}
            return HTTPResponse(
                status_code=int(error.code),
                body=error.read(),
                headers={key.lower(): value for key, value in response_headers.items()},
            )
        except (URLError, TimeoutError, OSError) as error:
            raise TransportFailure(str(error)) from error


class ProviderError(RuntimeError):
    """Base class for failures that are safe to show in a job error report."""

    def __init__(
        self,
        message: str,
        *,
        provider: str = "",
        status_code: int | None = None,
        retryable: bool = False,
        response_excerpt: str = "",
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.status_code = status_code
        self.retryable = retryable
        self.response_excerpt = response_excerpt


class ProviderConfigurationError(ProviderError):
    pass


class ProviderAuthenticationError(ProviderError):
    pass


class ProviderRateLimitError(ProviderError):
    pass


class ProviderRefusalError(ProviderError):
    pass


class ProviderRequestError(ProviderError):
    pass


class ProviderNetworkError(ProviderError):
    pass


class ProviderUnavailableError(ProviderError):
    pass


class ProviderResponseError(ProviderError):
    pass


def require_api_key(config: ProviderConfig) -> str:
    if not config.api_key:
        raise ProviderConfigurationError(
            f"{config.name} requires an API key.", provider=config.name
        )
    return config.api_key


def perform_request(
    transport: HTTPTransport,
    *,
    provider: str,
    method: str,
    url: str,
    headers: Mapping[str, str] | None = None,
    body: bytes | None = None,
    timeout: float = 90.0,
) -> HTTPResponse:
    """Call an injected transport and attach provider context to failures."""

    try:
        request_method = getattr(transport, "request", None)
        if callable(request_method):
            response = request_method(
                method, url, headers=headers, body=body, timeout=timeout
            )
        elif callable(transport):
            # A plain callable is convenient for a very small unit-test fake.
            response = transport(
                method, url, headers=headers, body=body, timeout=timeout
            )
        else:
            raise TypeError("HTTP transport must be callable or expose request().")
    except ProviderError:
        raise
    except (TransportFailure, TimeoutError, OSError) as error:
        raise ProviderNetworkError(
            f"Could not reach {provider}: {error}",
            provider=provider,
            retryable=True,
        ) from error
    if not isinstance(response, HTTPResponse):
        raise ProviderResponseError(
            f"The {provider} transport returned an invalid response object.",
            provider=provider,
        )
    return response


def ensure_http_success(provider: str, response: HTTPResponse) -> None:
    status = response.status_code
    if 200 <= status < 300:
        return
    excerpt = response.text.strip().replace("\r", " ").replace("\n", " ")[:500]
    lowered = excerpt.casefold()
    details = f" HTTP {status}." + (f" {excerpt}" if excerpt else "")
    common = {
        "provider": provider,
        "status_code": status,
        "response_excerpt": excerpt,
    }
    if status == 429:
        raise ProviderRateLimitError(
            f"{provider} rate limit or free quota was reached.{details}",
            retryable=True,
            **common,
        )
    refusal_markers = (
        "content policy",
        "content_policy",
        "moderation",
        "safety",
        "unsafe",
        "refused",
    )
    if status in {400, 403, 409, 422} and any(
        marker in lowered for marker in refusal_markers
    ):
        raise ProviderRefusalError(
            f"{provider} refused the content.{details}", **common
        )
    if status in {401, 403}:
        raise ProviderAuthenticationError(
            f"{provider} rejected the API credentials.{details}", **common
        )
    if status in {408, 425} or status >= 500:
        raise ProviderUnavailableError(
            f"{provider} is temporarily unavailable.{details}",
            retryable=True,
            **common,
        )
    raise ProviderRequestError(
        f"{provider} rejected the request.{details}", **common
    )


def json_request_body(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


# Concise aliases for callers that do not need the Provider prefix.
ConfigurationError = ProviderConfigurationError
AuthenticationError = ProviderAuthenticationError
RateLimitError = ProviderRateLimitError
RefusalError = ProviderRefusalError
NetworkError = ProviderNetworkError
ResponseError = ProviderResponseError
