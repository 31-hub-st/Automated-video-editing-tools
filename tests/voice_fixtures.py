from __future__ import annotations

from unittest import TestCase
from unittest.mock import patch

from storyforge.providers.tts import (
    available_female_voice_candidates,
    female_voice_candidates,
)


_KOKORO_PROVIDER_ALIASES = frozenset(
    {
        "kokoro",
        "local",
        "local_kokoro",
        "kokoro_local",
        "kokoro_http",
        "kokoro_cli",
    }
)


def verified_voice_candidates(
    provider: object,
    language: object = "en",
    *,
    endpoint: object = "",
    command: object = "",
):
    """Simulate a workstation whose official Kokoro voices were verified.

    The production implementation must inspect the installed bundle and fail
    closed. Tests which replace synthesis with a fake provider instead use
    this fixture so their voice identities do not depend on untracked model
    files being present in the checkout.
    """

    normalized_provider = str(provider or "").strip().casefold().replace("-", "_")
    if (
        normalized_provider in _KOKORO_PROVIDER_ALIASES
        and not str(endpoint or "").strip()
        and not str(command or "").strip()
    ):
        return female_voice_candidates(provider, language)
    return available_female_voice_candidates(
        provider,
        language,
        endpoint=endpoint,
        command=command,
    )


def install_verified_kokoro_voice_catalog(
    test_case: TestCase,
    *targets: str,
) -> None:
    """Patch module-local availability imports for one unittest test case."""

    if not targets:
        raise ValueError("at least one voice catalog patch target is required")
    for target in targets:
        patcher = patch(target, side_effect=verified_voice_candidates)
        patcher.start()
        test_case.addCleanup(patcher.stop)
