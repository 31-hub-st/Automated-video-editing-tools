from __future__ import annotations

import base64
import builtins
import io
import json
import ntpath
import os
import subprocess
import sys
import tempfile
import unittest
import wave
import weakref
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from storyforge.providers.base import (
    HTTPResponse,
    ProviderConfig,
    ProviderConfigurationError,
    ProviderRateLimitError,
    ProviderRefusalError,
    ProviderResponseError,
    coerce_provider_config,
)
from storyforge.providers.text import TextRequest, create_text_provider
from storyforge.providers.tts import (
    EmbeddedKokoroProvider,
    IsolatedKokoroProvider,
    KOKORO_LANGUAGE_CODES,
    TTSRequest,
    _closed_http_client_error,
    _configure_kokoro_torch,
    _import_kokoro_runtime,
    _kokoro_device,
    _missing_kokoro_language_modules,
    _offline_kokoro_assets,
    _windows_espeak_cache_roots,
    create_tts_provider,
    female_voice_candidates,
    kokoro_language_code,
    release_embedded_kokoro_runtime,
)


def json_response(value: Any, status: int = 200) -> HTTPResponse:
    return HTTPResponse(
        status_code=status,
        body=json.dumps(value).encode("utf-8"),
        headers={"content-type": "application/json"},
    )


def wav_bytes(duration: float = 0.1, rate: int = 16_000) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(rate)
        stream.writeframes(b"\x00\x00" * int(rate * duration))
    return output.getvalue()


class QueueTransport:
    def __init__(self, *responses: HTTPResponse) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        timeout: float = 90,
    ) -> HTTPResponse:
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": headers or {},
                "body": body,
                "timeout": timeout,
            }
        )
        if not self.responses:
            raise AssertionError("Unexpected HTTP call")
        return self.responses.pop(0)


def generated_fields(code: str = "123456") -> dict[str, str]:
    return {
        "polished_text": "Last night, everything changed when the phone rang.",
        "hook": "One phone call destroyed her ordinary life.",
        "ending_cta": f"Download NovelApp and search code {code} to keep reading.",
        "mood": "Suspense",
    }


class ProviderConfigTests(unittest.TestCase):
    def test_app_settings_shape_is_coerced(self) -> None:
        class Settings:
            text_provider = "ollama"
            text_model = "local-model"
            text_endpoint = "http://localhost/api/chat"
            text_api_key = ""

        config = coerce_provider_config(Settings(), kind="text")
        self.assertEqual(config.name, "ollama")
        self.assertEqual(config.model, "local-model")
        self.assertEqual(config.endpoint, "http://localhost/api/chat")


class TextProviderTests(unittest.TestCase):
    def test_groq_uses_one_json_chat_request(self) -> None:
        response = {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"content": json.dumps(generated_fields())},
                }
            ]
        }
        transport = QueueTransport(json_response(response))
        provider = create_text_provider(
            ProviderConfig(name="groq", api_key="secret", model="test-model"),
            transport=transport,
        )
        result = provider.polish(
            TextRequest(
                text="Last night everything changed when the phone rang.",
                platform="NovelApp",
                code="123456",
                creative_line_index=2,
                creative_line_count=3,
            )
        )
        self.assertEqual(result.mood, "suspense")
        self.assertIn("123456", result.ending_cta)
        self.assertEqual(len(transport.calls), 1)
        call_body = json.loads(transport.calls[0]["body"])
        self.assertEqual(call_body["response_format"], {"type": "json_object"})
        self.assertEqual(call_body["messages"][0]["role"], "system")
        user_payload = json.loads(call_body["messages"][1]["content"])
        self.assertEqual(user_payload["creative_line"], {
            "index": 2,
            "total": 3,
            "instruction": (
                "Use a distinct, factually accurate hook angle for this creative line; "
                "do not invent plot events."
            ),
        })
        self.assertNotIn("secret", transport.calls[0]["body"].decode())

    def test_groq_detects_token_truncation(self) -> None:
        response = {
            "choices": [
                {
                    "finish_reason": "length",
                    "message": {"content": json.dumps(generated_fields())},
                }
            ]
        }
        provider = create_text_provider(
            ProviderConfig(name="groq", api_key="key"),
            transport=QueueTransport(json_response(response)),
        )
        with self.assertRaisesRegex(ProviderResponseError, "finish_reason=length"):
            provider.polish("A short source sentence.")

    def test_intro_card_request_uses_a_fact_bounded_summary_prompt(self) -> None:
        fields = generated_fields()
        fields["polished_text"] = (
            "A stranger knows her husband's secret, and one call threatens to expose it."
        )
        response = {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"content": json.dumps(fields)},
                }
            ]
        }
        transport = QueueTransport(json_response(response))
        provider = create_text_provider(
            ProviderConfig(name="groq", api_key="key"),
            transport=transport,
        )
        provider.polish(
            TextRequest(
                text="A stranger knows her husband's secret.",
                purpose="intro_card",
                enforce_retention=False,
            )
        )

        call_body = json.loads(transport.calls[0]["body"])
        system_prompt = call_body["messages"][0]["content"]
        user_payload = json.loads(call_body["messages"][1]["content"])
        self.assertIn("complete factual boundary", system_prompt)
        self.assertIn("one or two", system_prompt)
        self.assertIn("20-28 words", system_prompt)
        self.assertIn("155 characters", system_prompt)
        self.assertIn("70 characters", system_prompt)
        self.assertIn("do not add names", system_prompt)
        self.assertEqual(user_payload["purpose"], "intro_card")

    def test_text_request_rejects_unknown_purpose(self) -> None:
        with self.assertRaisesRegex(ValueError, "purpose"):
            TextRequest(text="Story text.", purpose="advertisement")

    def test_rate_limit_and_refusal_are_distinct(self) -> None:
        limited = create_text_provider(
            ProviderConfig(name="groq", api_key="key"),
            transport=QueueTransport(json_response({"error": "quota"}, status=429)),
        )
        with self.assertRaises(ProviderRateLimitError):
            limited.polish("A source sentence.")

        refused = create_text_provider(
            ProviderConfig(name="groq", api_key="key"),
            transport=QueueTransport(
                json_response({"error": "content policy refusal"}, status=400)
            ),
        )
        with self.assertRaises(ProviderRefusalError):
            refused.polish("A source sentence.")

    def test_retention_guard_rejects_silent_short_output(self) -> None:
        source = " ".join(f"word{index}" for index in range(120))
        fields = generated_fields()
        fields["polished_text"] = "Only a tiny fragment survived."
        response = {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"content": json.dumps(fields)},
                }
            ]
        }
        provider = create_text_provider(
            ProviderConfig(name="groq", api_key="key"),
            transport=QueueTransport(json_response(response)),
        )
        with self.assertRaisesRegex(ProviderResponseError, "may be truncated"):
            provider.polish(TextRequest(text=source, code="123456"))

    def test_cloudflare_and_ollama_response_shapes(self) -> None:
        cloudflare = create_text_provider(
            ProviderConfig(
                name="cloudflare",
                api_key="token",
                endpoint="https://example.test/accounts/a/ai/run/{model}",
                model="@cf/test/model",
            ),
            transport=QueueTransport(
                json_response(
                    {
                        "success": True,
                        "result": {"response": generated_fields("44")},
                    }
                )
            ),
        )
        self.assertEqual(
            cloudflare.polish(TextRequest(text="Story text.", code="44")).mood,
            "suspense",
        )

        ollama = create_text_provider(
            ProviderConfig(name="ollama", model="tiny"),
            transport=QueueTransport(
                json_response(
                    {
                        "done": True,
                        "done_reason": "stop",
                        "message": {"content": json.dumps(generated_fields("55"))},
                    }
                )
            ),
        )
        self.assertEqual(
            ollama.polish(TextRequest(text="Story text.", code="55")).mood,
            "suspense",
        )

    def test_local_rules_are_offline_and_deterministic(self) -> None:
        transport = QueueTransport()
        provider = create_text_provider(
            ProviderConfig(name="local", options={"mode": "rules"}),
            transport=transport,
        )
        result = provider.polish(
            TextRequest(
                text="A secret changed everything.\nA secret changed everything.\nShe cried.",
                platform="NovelApp",
                code="77",
            )
        )
        self.assertEqual(result.polished_text.count("A secret changed everything."), 1)
        self.assertIn("77", result.ending_cta)
        self.assertEqual(transport.calls, [])


class TTSProviderTests(unittest.TestCase):
    def test_kokoro_import_supports_windowed_frozen_process_without_console_streams(self) -> None:
        observed: dict[str, object] = {}
        fake_module = SimpleNamespace(KModel=object(), KPipeline=object())
        original_import = builtins.__import__

        def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "kokoro":
                observed["stdout"] = sys.stdout
                observed["stderr"] = sys.stderr
                return fake_module
            return original_import(name, globals, locals, fromlist, level)

        with (
            patch.object(sys, "stdout", None),
            patch.object(sys, "stderr", None),
            patch("builtins.__import__", side_effect=guarded_import),
        ):
            model, pipeline = _import_kokoro_runtime()
            self.assertIsNone(sys.stdout)
            self.assertIsNone(sys.stderr)

        self.assertIs(model, fake_module.KModel)
        self.assertIs(pipeline, fake_module.KPipeline)
        self.assertIsNotNone(observed["stdout"])
        self.assertIsNotNone(observed["stderr"])

    def test_embedded_kokoro_runtime_release_drops_models_and_cuda_caches(self) -> None:
        events: list[str] = []

        class FakeModel:
            pass

        class FakePipeline:
            pass

        model = FakeModel()
        pipeline = FakePipeline()
        pipeline.model = model
        model.pipeline = pipeline
        model_reference = weakref.ref(model)
        pipeline_reference = weakref.ref(pipeline)
        pipelines = {"a:cuda": pipeline}
        del model, pipeline

        fake_torch = SimpleNamespace(
            cuda=SimpleNamespace(
                is_available=lambda: True,
                empty_cache=lambda: events.append("empty_cache"),
                ipc_collect=lambda: events.append("ipc_collect"),
            )
        )
        with (
            patch.object(EmbeddedKokoroProvider, "_pipelines", pipelines),
            patch.dict(sys.modules, {"torch": fake_torch}),
        ):
            released = release_embedded_kokoro_runtime()

        self.assertEqual(released, 1)
        self.assertEqual(pipelines, {})
        self.assertIsNone(pipeline_reference())
        self.assertIsNone(model_reference())
        self.assertEqual(events, ["empty_cache", "ipc_collect"])

    def test_cpu_kokoro_runtime_release_does_not_touch_cuda(self) -> None:
        events: list[str] = []
        fake_torch = SimpleNamespace(
            cuda=SimpleNamespace(
                is_available=lambda: events.append("is_available") or True,
                empty_cache=lambda: events.append("empty_cache"),
                ipc_collect=lambda: events.append("ipc_collect"),
            )
        )
        with (
            patch.object(EmbeddedKokoroProvider, "_pipelines", {"a": object()}),
            patch.dict(sys.modules, {"torch": fake_torch}),
        ):
            self.assertEqual(release_embedded_kokoro_runtime(), 1)

        self.assertEqual(events, [])

    def test_embedded_kokoro_runtime_release_is_safe_without_torch_or_cuda(self) -> None:
        with (
            patch.object(EmbeddedKokoroProvider, "_pipelines", {"a": object()}),
            patch.dict(sys.modules, {"torch": None}),
        ):
            self.assertEqual(release_embedded_kokoro_runtime(), 1)

        with (
            patch.object(EmbeddedKokoroProvider, "_pipelines", {"a": object()}),
            patch.dict(sys.modules, {"torch": SimpleNamespace()}),
        ):
            self.assertEqual(release_embedded_kokoro_runtime(), 1)

    def test_empty_kokoro_cache_does_not_import_torch(self) -> None:
        original_import = builtins.__import__

        def reject_torch(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "torch":
                raise AssertionError("empty Kokoro cache must not import torch")
            return original_import(name, globals, locals, fromlist, level)

        with (
            patch.object(EmbeddedKokoroProvider, "_pipelines", {}),
            patch.dict(sys.modules, {"torch": None}),
            patch("builtins.__import__", side_effect=reject_torch),
        ):
            self.assertEqual(release_embedded_kokoro_runtime(), 0)

    def test_embedded_kokoro_defaults_to_cpu_and_safe_torch_threads(self) -> None:
        devices: list[str] = []
        intraop_calls: list[int] = []
        interop_calls: list[int] = []
        cuda_checks: list[bool] = []

        class FakeModel:
            def __init__(self, **_kwargs: Any) -> None:
                pass

            def to(self, device: str) -> FakeModel:
                devices.append(device)
                return self

            def eval(self) -> FakeModel:
                return self

        class FakePipeline:
            def __init__(self, **kwargs: Any) -> None:
                self.model = kwargs["model"]

        fake_torch = SimpleNamespace(
            cuda=SimpleNamespace(
                is_available=lambda: cuda_checks.append(True) or True,
            ),
            set_num_threads=intraop_calls.append,
            set_num_interop_threads=interop_calls.append,
        )
        with (
            patch.object(EmbeddedKokoroProvider, "_pipelines", {}),
            patch.dict(sys.modules, {"torch": fake_torch}),
            patch(
                "storyforge.providers.tts._ensure_kokoro_language_dependencies"
            ),
            patch(
                "storyforge.providers.tts._offline_kokoro_assets",
                return_value=Path("offline-kokoro"),
            ),
            patch("storyforge.providers.tts._prepare_windows_espeak_loader"),
            patch("storyforge.providers.tts._prepare_huggingface_cache"),
            patch(
                "storyforge.providers.tts._import_kokoro_runtime",
                return_value=(FakeModel, FakePipeline),
            ),
        ):
            pipeline = EmbeddedKokoroProvider(
                ProviderConfig(name="local_kokoro")
            )._pipeline("a")

        self.assertIsInstance(pipeline, FakePipeline)
        self.assertEqual(devices, ["cpu"])
        self.assertEqual(cuda_checks, [])
        self.assertEqual(intraop_calls, [2])
        self.assertEqual(interop_calls, [1])

    def test_embedded_kokoro_allows_cuda_and_auto_device_overrides(self) -> None:
        def load_device(requested: str, *, cuda_available: bool) -> str:
            devices: list[str] = []

            class FakeModel:
                def __init__(self, **_kwargs: Any) -> None:
                    pass

                def to(self, device: str) -> FakeModel:
                    devices.append(device)
                    return self

                def eval(self) -> FakeModel:
                    return self

            class FakePipeline:
                def __init__(self, **_kwargs: Any) -> None:
                    pass

            fake_torch = SimpleNamespace(
                cuda=SimpleNamespace(is_available=lambda: cuda_available),
                set_num_threads=lambda _value: None,
                set_num_interop_threads=lambda _value: None,
            )
            with (
                patch.object(EmbeddedKokoroProvider, "_pipelines", {}),
                patch.dict(sys.modules, {"torch": fake_torch}),
                patch(
                    "storyforge.providers.tts._ensure_kokoro_language_dependencies"
                ),
                patch(
                    "storyforge.providers.tts._offline_kokoro_assets",
                    return_value=Path("offline-kokoro"),
                ),
                patch("storyforge.providers.tts._prepare_windows_espeak_loader"),
                patch("storyforge.providers.tts._prepare_huggingface_cache"),
                patch(
                    "storyforge.providers.tts._import_kokoro_runtime",
                    return_value=(FakeModel, FakePipeline),
                ),
            ):
                EmbeddedKokoroProvider(
                    ProviderConfig(
                        name="local_kokoro",
                        options={"device": requested},
                    )
                )._pipeline("a")
            return devices[0]

        self.assertEqual(load_device("cuda", cuda_available=True), "cuda")
        self.assertEqual(load_device("auto", cuda_available=True), "cuda")
        self.assertEqual(load_device("auto", cuda_available=False), "cpu")

    def test_embedded_kokoro_thread_overrides_are_idempotent_and_tolerant(self) -> None:
        intraop_calls: list[int] = []
        interop_calls: list[int] = []

        class FakeModel:
            def __init__(self, **_kwargs: Any) -> None:
                pass

            def to(self, _device: str) -> FakeModel:
                return self

            def eval(self) -> FakeModel:
                return self

        class FakePipeline:
            def __init__(self, **_kwargs: Any) -> None:
                pass

        def reject_late_interop(value: int) -> None:
            interop_calls.append(value)
            raise RuntimeError("cannot set interop threads after work has started")

        fake_torch = SimpleNamespace(
            cuda=SimpleNamespace(is_available=lambda: True),
            set_num_threads=intraop_calls.append,
            set_num_interop_threads=reject_late_interop,
        )
        provider = EmbeddedKokoroProvider(
            ProviderConfig(
                name="local_kokoro",
                options={
                    "torch_num_threads": "6",
                    "torch_num_interop_threads": 3,
                },
            )
        )
        with (
            patch.object(EmbeddedKokoroProvider, "_pipelines", {}),
            patch.dict(sys.modules, {"torch": fake_torch}),
            patch(
                "storyforge.providers.tts._ensure_kokoro_language_dependencies"
            ),
            patch(
                "storyforge.providers.tts._offline_kokoro_assets",
                return_value=Path("offline-kokoro"),
            ),
            patch("storyforge.providers.tts._prepare_windows_espeak_loader"),
            patch("storyforge.providers.tts._prepare_huggingface_cache"),
            patch(
                "storyforge.providers.tts._import_kokoro_runtime",
                return_value=(FakeModel, FakePipeline),
            ),
        ):
            first = provider._pipeline("a")
            second = provider._pipeline("a")

        self.assertIs(first, second)
        self.assertEqual(intraop_calls, [6])
        self.assertEqual(interop_calls, [3])

    def test_kokoro_environment_override_is_available_to_host_source_runs(self) -> None:
        intraop_calls: list[int] = []
        interop_calls: list[int] = []
        fake_torch = SimpleNamespace(
            cuda=SimpleNamespace(is_available=lambda: True),
            set_num_threads=intraop_calls.append,
            set_num_interop_threads=interop_calls.append,
        )
        environment = {
            "STORYFORGE_KOKORO_DEVICE": "auto",
            "STORYFORGE_KOKORO_TORCH_THREADS": "5",
            "STORYFORGE_KOKORO_INTEROP_THREADS": "2",
        }
        with patch.dict(os.environ, environment, clear=False):
            self.assertEqual(_kokoro_device(fake_torch, {}, "local_kokoro"), "cuda")
            self.assertEqual(
                _configure_kokoro_torch(fake_torch, {}, "local_kokoro"),
                (5, 2),
            )
            # Per-provider options remain the highest-priority source override.
            self.assertEqual(
                _kokoro_device(
                    fake_torch,
                    {"device": "cpu"},
                    "local_kokoro",
                ),
                "cpu",
            )

        self.assertEqual(intraop_calls, [5])
        self.assertEqual(interop_calls, [2])

    def test_unicode_portable_cache_never_escapes_to_a_volume_root(self) -> None:
        unicode_cache = (
            "D:\\小说工具\\StoryForge\\StoryForgeData\\cache\\espeak"
        )
        with patch(
            "storyforge.providers.tts._windows_ascii_short_path",
            return_value=None,
        ):
            first = _windows_espeak_cache_roots(
                unicode_cache,
                "1.0.3",
                portable=True,
            )
            second = _windows_espeak_cache_roots(
                unicode_cache,
                "1.0.3",
                portable=True,
            )

        self.assertEqual(first, second)
        self.assertEqual(first, ())

    def test_portable_unicode_install_prefers_ascii_windows_short_path(self) -> None:
        unicode_cache = (
            "E:\\员工软件\\StoryForgeData\\cache\\espeak"
        )
        short_cache = Path(r"E:\\EMPLOY~1\\STORYF~1\\CACHE\\ESPEAK")
        with patch(
            "storyforge.providers.tts._windows_ascii_short_path",
            return_value=short_cache,
        ):
            roots = _windows_espeak_cache_roots(
                unicode_cache,
                "bundled",
                portable=False,
            )

        self.assertGreaterEqual(len(roots), 1)
        self.assertTrue(all(str(path).isascii() for path in roots))
        self.assertTrue(
            ntpath.normcase(str(roots[0])).startswith(
                ntpath.normcase(str(short_cache))
            )
        )
        self.assertTrue(
            all(ntpath.splitdrive(str(path))[0].casefold() == "e:" for path in roots)
        )

    def test_multilingual_female_voice_catalog_keeps_english_and_adds_free_languages(self) -> None:
        self.assertEqual(KOKORO_LANGUAGE_CODES["en"], "a")
        self.assertEqual(KOKORO_LANGUAGE_CODES["ja"], "j")
        self.assertEqual(KOKORO_LANGUAGE_CODES["zh"], "z")
        self.assertEqual(
            [item.voice_id for item in female_voice_candidates("local_kokoro", "en")[:4]],
            ["af_heart", "af_bella", "af_nicole", "af_sarah"],
        )
        for language in ("ja", "es", "fr", "hi", "it", "pt-BR", "zh-Hans"):
            with self.subTest(language=language):
                self.assertTrue(female_voice_candidates("local_kokoro", language))

    def test_deepgram_exposes_three_japanese_female_voices(self) -> None:
        self.assertEqual(
            [item.voice_id for item in female_voice_candidates("deepgram", "ja-JP")],
            [
                "aura-2-izanami-ja",
                "aura-2-uzume-ja",
                "aura-2-ama-ja",
            ],
        )

    def test_kokoro_language_is_inferred_from_voice_and_mismatch_is_actionable(self) -> None:
        self.assertEqual(kokoro_language_code("", "jf_alpha"), "j")
        self.assertEqual(kokoro_language_code("zh-Hans", "zf_xiaoxiao"), "z")
        configured = EmbeddedKokoroProvider(
            ProviderConfig(name="local_kokoro", options={"lang_code": "j"})
        )
        self.assertEqual(configured.language, "j")
        with self.assertRaisesRegex(ProviderConfigurationError, "与当前语种不匹配"):
            kokoro_language_code("ja", "af_heart")

    def test_japanese_dependency_probe_covers_verified_windows_runtime(self) -> None:
        required = {
            "pyopenjtalk",
            "fugashi",
            "jaconv",
            "mojimoji",
            "unidic_lite",
        }
        with patch(
            "storyforge.providers.tts.importlib.util.find_spec",
            side_effect=lambda module: None if module in required else object(),
        ):
            self.assertEqual(
                set(_missing_kokoro_language_modules("j")),
                required,
            )

    def test_missing_kokoro_language_extra_has_requirements_install_command(self) -> None:
        provider = EmbeddedKokoroProvider(
            ProviderConfig(name="local_kokoro", options={"lang_code": "j"})
        )
        with (
            patch(
                "storyforge.providers.tts._missing_kokoro_language_modules",
                return_value=(
                    "fugashi",
                    "jaconv",
                    "mojimoji",
                    "unidic_lite",
                ),
            ),
            self.assertRaisesRegex(
                ProviderConfigurationError,
                r"fugashi.*jaconv.*mojimoji.*unidic_lite.*"
                r"pip install -r requirements-ai\.txt",
            ),
        ):
            provider._pipeline("j")

    def test_missing_offline_voice_names_target_folder_and_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            pipeline = SimpleNamespace(
                _storyforge_asset_dir=folder,
                voices={},
            )
            with self.assertRaisesRegex(
                ProviderConfigurationError,
                r"jf_alpha\.pt.*放到.*voices.*联网环境",
            ):
                EmbeddedKokoroProvider._load_offline_voice(pipeline, "jf_alpha")

    def test_closed_http_client_error_recognizes_wrapped_failures(self) -> None:
        root = RuntimeError("download failed")
        root.__cause__ = RuntimeError("Cannot send a request, as the client has been closed")

        self.assertTrue(_closed_http_client_error(root))
        self.assertTrue(_closed_http_client_error(RuntimeError("HTTP client is closed")))
        self.assertFalse(_closed_http_client_error(RuntimeError("connection refused")))

    def test_offline_kokoro_assets_require_a_complete_nonempty_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            incomplete = root / "incomplete"
            complete = root / "complete"
            for asset_root in (incomplete, complete):
                (asset_root / "voices").mkdir(parents=True)
                (asset_root / "config.json").write_text("{}", encoding="utf-8")
                (asset_root / "kokoro-v1_0.pth").write_bytes(b"model")
            for voice in ("af_heart", "af_bella", "af_nicole", "af_sarah"):
                (complete / "voices" / f"{voice}.pt").write_bytes(b"voice")
            # A zero-byte required file must make an otherwise complete-looking
            # directory invalid.
            (incomplete / "voices" / "af_heart.pt").write_bytes(b"")

            with patch(
                "storyforge.providers.tts._kokoro_runtime_roots",
                return_value=(incomplete, complete),
            ):
                self.assertEqual(_offline_kokoro_assets(), complete)

            (complete / "voices" / "af_sarah.pt").unlink()
            with patch(
                "storyforge.providers.tts._kokoro_runtime_roots",
                return_value=(incomplete, complete),
            ):
                self.assertIsNone(_offline_kokoro_assets())

    def test_deepgram_generates_one_wav_per_sentence(self) -> None:
        audio = wav_bytes(0.125)
        transport = QueueTransport(
            HTTPResponse(200, audio, {"content-type": "audio/wav"}),
            HTTPResponse(200, audio, {"content-type": "audio/wav"}),
        )
        provider = create_tts_provider(
            ProviderConfig(
                name="deepgram",
                api_key="token",
                options={"cache_enabled": False},
            ),
            transport=transport,
        )
        with tempfile.TemporaryDirectory() as folder:
            result = provider.synthesize(
                "First sentence. Second sentence!", folder, voice="aura-2-luna-en"
            )
            self.assertEqual(len(result.segments), 2)
            self.assertAlmostEqual(result.duration_seconds, 0.25, places=2)
            self.assertTrue(all(Path(path).is_file() for path in result.paths))
        self.assertEqual(len(transport.calls), 2)
        self.assertIn("model=aura-2-luna-en", transport.calls[0]["url"])
        self.assertEqual(transport.calls[0]["headers"]["Authorization"], "Token token")

    def test_deepgram_extreme_wpm_caps_api_and_applies_residual_atempo(self) -> None:
        transport = QueueTransport(
            HTTPResponse(200, wav_bytes(0.125), {"content-type": "audio/wav"})
        )
        provider = create_tts_provider(
            ProviderConfig(
                name="deepgram",
                api_key="token",
                options={"cache_enabled": False},
            ),
            transport=transport,
        )
        commands: list[list[str]] = []

        def fake_ffmpeg(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
            commands.append(command)
            Path(command[-1]).write_bytes(wav_bytes(0.08))
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as folder, patch(
            "storyforge.providers.tts._edge_ffmpeg_executable",
            return_value=Path(folder) / "ffmpeg.exe",
        ), patch(
            "storyforge.providers.tts.run_cancellable_process",
            side_effect=fake_ffmpeg,
        ):
            result = provider.synthesize(
                TTSRequest(
                    ["Extreme pace."],
                    folder,
                    voice="aura-2-luna-en",
                    speed=280 / 155,
                )
            )

        self.assertIn("speed=1.5", transport.calls[0]["url"])
        self.assertEqual(len(result.segments), 1)
        self.assertTrue(commands)
        self.assertIn("atempo=1.20430108", commands[0])

    def test_kokoro_http_supports_base64_json_audio(self) -> None:
        audio = wav_bytes(0.05)
        response = json_response({"audio": base64.b64encode(audio).decode("ascii")})
        provider = create_tts_provider(
            ProviderConfig(
                name="local_kokoro",
                endpoint="http://127.0.0.1:8880",
                options={"cache_enabled": False},
            ),
            transport=QueueTransport(response),
        )
        with tempfile.TemporaryDirectory() as folder:
            result = provider.synthesize(
                TTSRequest(["Hello."], folder, voice="af_bella")
            )
            self.assertEqual(len(result.segments), 1)
            self.assertGreater(result.duration_seconds, 0)
            self.assertTrue(Path(result.path).is_file())

    def test_kokoro_without_engine_has_clear_error(self) -> None:
        provider = EmbeddedKokoroProvider(
            ProviderConfig(name="local_kokoro", options={"cache_enabled": False})
        )

        real_import = builtins.__import__

        def import_without_kokoro(name: str, *args: Any, **kwargs: Any) -> Any:
            if name == "kokoro" or name.startswith("kokoro."):
                raise ImportError("synthetic missing Kokoro package")
            return real_import(name, *args, **kwargs)

        with tempfile.TemporaryDirectory() as folder:
            with (
                patch("storyforge.providers.tts.EmbeddedKokoroProvider._pipelines", {}),
                patch("builtins.__import__", side_effect=import_without_kokoro),
                self.assertRaisesRegex(ProviderConfigurationError, "No Kokoro engine"),
            ):
                provider.synthesize("Hello.", folder)

    def test_embedded_kokoro_isolated_process_returns_complete_batch(self) -> None:
        provider = create_tts_provider(
            ProviderConfig(name="local_kokoro", options={"cache_enabled": False})
        )
        self.assertIsInstance(provider, IsolatedKokoroProvider)
        calls: list[list[str]] = []

        def fake_child(
            command: list[str], **_kwargs: Any
        ) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            request_path = Path(command[-2])
            response_path = Path(command[-1])
            request_value = json.loads(request_path.read_text(encoding="utf-8"))
            output_root = Path(request_value["output_dir"])
            output_root.mkdir(parents=True, exist_ok=True)
            segments: list[dict[str, Any]] = []
            for index, text_value in enumerate(request_value["sentences"], start=1):
                audio_path = output_root / f"line-{index:04d}.wav"
                audio_path.write_bytes(wav_bytes(0.05))
                segments.append(
                    {
                        "index": index,
                        "text": text_value,
                        "path": str(audio_path),
                        "duration_seconds": 0.05,
                        "voice": request_value["voice"],
                        "provider": "local_kokoro",
                    }
                )
            response_path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "result": {
                            "segments": segments,
                            "provider": "local_kokoro",
                            "model": "kokoro",
                        },
                    }
                ),
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as folder, patch(
            "storyforge.providers.tts.run_cancellable_process",
            side_effect=fake_child,
        ):
            result = provider.synthesize(
                ["First sentence.", "Second sentence."],
                folder,
                voice="af_heart",
            )

        self.assertEqual(len(calls), 1)
        self.assertEqual(len(result.segments), 2)
        self.assertAlmostEqual(result.duration_seconds, 0.1, places=3)

    def test_kokoro_cli_is_configurable_and_runner_is_injectable(self) -> None:
        audio = wav_bytes(0.05)

        def fake_runner(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
            Path(command[-1]).write_bytes(audio)
            return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

        provider = create_tts_provider(
            ProviderConfig(
                name="kokoro_cli",
                options={
                    "command": [sys.executable, "--output", "{output}"],
                    "cache_enabled": False,
                },
            ),
            runner=fake_runner,
        )
        with tempfile.TemporaryDirectory() as folder:
            result = provider.synthesize("Hello.", Path(folder) / "voice.wav")
            self.assertTrue(Path(result.path).is_file())
            self.assertGreater(result.duration_seconds, 0)


if __name__ == "__main__":
    unittest.main()
