from __future__ import annotations

"""Repeatable, offline acceptance test for StoryForge's real render pipeline.

This is deliberately not a unit-test mock of FFmpeg.  It creates real 60 FPS
H.264 source clips, a real MP3 music track and a cover image, then invokes the
same :class:`PipelineRunner` used by production.  Only the text and TTS
providers are deterministic offline fakes so the result does not depend on an
API key, a network connection, or a large local speech model.

Examples::

    python scripts/stability_render_acceptance.py --quick --ffprobe C:\\ffmpeg\\bin\\ffprobe.exe
    python scripts/stability_render_acceptance.py --stress --stress-seconds 120 \
        --app-root "D:\\StoryForge Studio"
"""

import argparse
import json
import math
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import time
import wave


SCRIPT_DIR = Path(__file__).resolve().parent
SOURCE_ROOT = SCRIPT_DIR.parent
if (SOURCE_ROOT / "storyforge").is_dir() and str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from storyforge.models import AppSettings, PlatformProfile, RenderJob  # noqa: E402
from storyforge.pipeline import (  # noqa: E402
    PipelineRunner,
    UsageLedger,
    job_workspace_directory,
)
from storyforge.providers.text import TextResult  # noqa: E402
from storyforge.providers.tts import SpeechSegment, TTSResult  # noqa: E402
from storyforge.services.quality import resolve_ffprobe  # noqa: E402
from storyforge.system import resolve_ffmpeg  # noqa: E402


class AcceptanceError(RuntimeError):
    pass


def _default_root() -> Path:
    configured = str(os.environ.get("STORYFORGE_ACCEPTANCE_ROOT") or "").strip()
    if configured:
        return Path(configured)
    if os.name == "nt" and Path("D:/").is_dir():
        return Path("D:/StoryForgeBuildTemp/acceptance")
    return SOURCE_ROOT / ".runtime-acceptance"


def _find_named_binary(app_root: Path | None, names: tuple[str, ...]) -> Path | None:
    if app_root is None or not app_root.is_dir():
        return None
    direct_roots = (app_root, app_root / "_internal", app_root / "bin")
    for root in direct_roots:
        for name in names:
            candidate = root / name
            if candidate.is_file():
                return candidate.resolve()
    for pattern in names:
        matches = sorted(app_root.rglob(pattern), key=lambda item: len(item.parts))
        if matches:
            return matches[0].resolve()
    return None


def resolve_tools(args: argparse.Namespace) -> tuple[Path, Path]:
    app_root = args.app_root.expanduser().resolve() if args.app_root else None
    ffmpeg = args.ffmpeg.expanduser().resolve() if args.ffmpeg else None
    if ffmpeg is None:
        ffmpeg = _find_named_binary(
            app_root,
            ("ffmpeg.exe", "ffmpeg", "ffmpeg-win-*.exe", "ffmpeg-*.exe"),
        )
    if ffmpeg is None:
        ffmpeg = resolve_ffmpeg()
    if ffmpeg is None or not ffmpeg.is_file():
        raise AcceptanceError(
            "FFmpeg was not found. Pass --ffmpeg or --app-root pointing at the "
            "unpacked StoryForge application."
        )

    ffprobe = args.ffprobe.expanduser().resolve() if args.ffprobe else None
    if ffprobe is None:
        ffprobe = _find_named_binary(app_root, ("ffprobe.exe", "ffprobe"))
    if ffprobe is None:
        ffprobe = resolve_ffprobe(ffmpeg)
    if ffprobe is None or not ffprobe.is_file():
        raise AcceptanceError(
            "ffprobe is required for acceptance verification. Pass --ffprobe "
            "or use --app-root with a package containing ffprobe."
        )
    return ffmpeg, ffprobe


def run_command(command: list[str], *, timeout: float = 180.0) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=timeout,
        creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
    )
    if completed.returncode:
        detail = (completed.stderr or completed.stdout or "unknown command error")[-3000:]
        raise AcceptanceError(
            f"Command exited with {completed.returncode}: {subprocess.list2cmdline(command)}\n{detail}"
        )
    return completed


def create_video(ffmpeg: Path, path: Path, *, duration: float, color: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    run_command(
        [
            str(ffmpeg),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:s=360x640:r=60:d={duration:.3f}",
            "-vf",
            "drawbox=x=mod(t*90\\,300):y=180:w=60:h=160:color=white@0.7:t=fill",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            "30",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(path),
        ]
    )


def create_music(ffmpeg: Path, path: Path, *, duration: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    run_command(
        [
            str(ffmpeg),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=220:sample_rate=48000:duration={duration:.3f}",
            "-af",
            "volume=0.08",
            "-c:a",
            "libmp3lame",
            "-b:a",
            "128k",
            str(path),
        ]
    )


def create_cover(ffmpeg: Path, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    run_command(
        [
            str(ffmpeg),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=0x18243A:s=720x1280:d=0.1",
            "-frames:v",
            "1",
            "-c:v",
            "png",
            "-threads",
            "1",
            str(path),
        ]
    )


def write_tone_wav(path: Path, duration: float, *, frequency: float) -> None:
    rate = 48_000
    frames = max(1, round(duration * rate))
    amplitude = 1_600
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(rate)
        block = bytearray()
        for index in range(frames):
            value = round(amplitude * math.sin(2.0 * math.pi * frequency * index / rate))
            block.extend(struct.pack("<h", value))
            if len(block) >= 192_000:
                stream.writeframesraw(block)
                block.clear()
        if block:
            stream.writeframesraw(block)


class OfflineTextProvider:
    def polish(self, request) -> TextResult:
        return TextResult(
            polished_text=request.text,
            hook="The message on her phone changed everything.",
            ending_cta=(
                f"Download {request.platform} and search code {request.code} "
                "to continue reading."
            ),
            mood="suspense",
            provider="acceptance-offline-text",
            model="deterministic",
            retention_ratio=1.0,
        )


class OfflineTTSProvider:
    def __init__(self, target_seconds: float) -> None:
        self.target_seconds = max(2.0, float(target_seconds))

    def synthesize(self, sentences, output_dir, *, voice, speed, file_stem) -> TTSResult:
        spoken = [str(item).strip() for item in sentences if str(item).strip()]
        if not spoken:
            raise AcceptanceError("The production pipeline supplied no narration sentences.")
        seconds = self.target_seconds / len(spoken)
        segments: list[SpeechSegment] = []
        for index, sentence in enumerate(spoken, start=1):
            path = Path(output_dir) / f"{file_stem}-{index:04d}.wav"
            write_tone_wav(path, seconds, frequency=170.0 + index * 11.0)
            segments.append(
                SpeechSegment(
                    index=index,
                    text=sentence,
                    path=str(path),
                    duration_seconds=seconds,
                    voice=voice or "acceptance-female",
                    provider="acceptance-offline-tts",
                )
            )
        return TTSResult(tuple(segments), provider="acceptance-offline-tts")


def probe(ffprobe: Path, path: Path) -> dict[str, object]:
    completed = run_command(
        [
            str(ffprobe),
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        timeout=60.0,
    )
    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as error:
        raise AcceptanceError(f"ffprobe returned invalid JSON for {path}") from error
    if not isinstance(payload, dict):
        raise AcceptanceError(f"ffprobe returned an invalid payload for {path}")
    return payload


def _rate(value: object) -> float:
    text = str(value or "")
    if "/" in text:
        top, bottom = text.split("/", 1)
        return float(top) / float(bottom) if float(bottom) else 0.0
    return float(text or 0.0)


def verify_video(ffprobe: Path, path: Path) -> dict[str, object]:
    payload = probe(ffprobe, path)
    streams = [item for item in payload.get("streams", []) if isinstance(item, dict)]
    videos = [item for item in streams if item.get("codec_type") == "video"]
    audios = [item for item in streams if item.get("codec_type") == "audio"]
    if not videos or not audios:
        raise AcceptanceError(f"{path.name} must contain video and audio streams")
    video = videos[0]
    duration = float((payload.get("format") or {}).get("duration") or 0.0)
    fps = _rate(video.get("avg_frame_rate") or video.get("r_frame_rate"))
    expected = {
        "codec": str(video.get("codec_name") or "").casefold(),
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "fps": fps,
        "duration": duration,
        "video_streams": len(videos),
        "audio_streams": len(audios),
    }
    if expected["codec"] != "h264":
        raise AcceptanceError(f"Expected H.264, got {expected['codec']!r}")
    if (expected["width"], expected["height"]) != (1080, 1920):
        raise AcceptanceError(
            f"Expected 1080x1920, got {expected['width']}x{expected['height']}"
        )
    if abs(float(expected["fps"]) - 60.0) > 0.2:
        raise AcceptanceError(f"Expected 60 FPS, got {expected['fps']}")
    if duration <= 0.25 or path.stat().st_size <= 1_024:
        raise AcceptanceError(f"Rendered video is empty or too short: {path}")
    return expected


def verify_mp3(ffprobe: Path, path: Path) -> dict[str, object]:
    payload = probe(ffprobe, path)
    streams = [item for item in payload.get("streams", []) if isinstance(item, dict)]
    audios = [item for item in streams if item.get("codec_type") == "audio"]
    videos = [item for item in streams if item.get("codec_type") == "video"]
    duration = float((payload.get("format") or {}).get("duration") or 0.0)
    codec = str(audios[0].get("codec_name") or "").casefold() if audios else ""
    if len(audios) != 1 or videos or codec not in {"mp3", "mp3float"}:
        raise AcceptanceError(f"Narration is not a standalone MP3 stream: {path}")
    if duration <= 0.25 or path.stat().st_size <= 1_024:
        raise AcceptanceError(f"Narration MP3 is empty or too short: {path}")
    return {"codec": codec, "duration": duration, "audio_streams": len(audios)}


def prepare_inputs(ffmpeg: Path, root: Path, *, narration_seconds: float) -> dict[str, Path]:
    input_root = root / "inputs"
    source = input_root / "B73165_Acceptance Story.txt"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(
        "Chapter 1\n"
        "At ten o'clock, Mara received a message from her husband's number. "
        "The sender knew a secret that no stranger should have known.\n\n"
        "Chapter 2\n"
        "She opened the locked drawer and found the proof. "
        "Then the front door clicked open behind her.",
        encoding="utf-8",
    )
    cover = input_root / "cover.png"
    create_cover(ffmpeg, cover)

    single_video = input_root / "single-videos" / "suspense" / "single.mp4"
    create_video(
        ffmpeg,
        single_video,
        duration=max(6.0, min(15.0, narration_seconds + 1.0)),
        color="0x23497A",
    )

    multi_root = input_root / "multi-videos" / "suspense"
    colors = ("0x7A2349", "0x236A55", "0x58418A", "0x8A5A24")
    for index, color in enumerate(colors, start=1):
        create_video(
            ffmpeg,
            multi_root / f"clip-{index}.mp4",
            duration=1.0,
            color=color,
        )

    music = input_root / "music" / "suspense" / "acceptance-music.mp3"
    create_music(ffmpeg, music, duration=max(3.0, min(30.0, narration_seconds)))
    (input_root / "no-music").mkdir(parents=True, exist_ok=True)
    return {
        "source": source,
        "cover": cover,
        "single_videos": single_video.parents[1],
        "multi_videos": multi_root.parents[0],
        "music": music.parents[1],
        "no_music": input_root / "no-music",
    }


def render_scenario(
    *,
    name: str,
    root: Path,
    ffmpeg: Path,
    ffprobe: Path,
    inputs: dict[str, Path],
    narration_seconds: float,
    encoder: str,
    multi: bool,
    bgm: bool,
) -> dict[str, object]:
    scenario_root = root / name
    output_root = scenario_root / "output"
    work_root = scenario_root / "private-work"
    settings = AppSettings(
        narration_wpm=240,
        output_width=1080,
        output_height=1920,
        output_fps=60,
        output_mode="video_and_mp3",
        video_encoder=encoder,
        bgm_mode="auto" if bgm else "none",
        bgm_volume=0.18,
        caption_mode="semantic",
        subtitle_preset="clear_outline",
        subtitle_animation="none",
        video_template="classic",
        cover_animation="gentle_push",
        cover_outro_enabled=True,
        end_card_seconds=1.0,
        render_mode="compatibility",
    )
    settings.providers.text_provider = "local"
    settings.providers.tts_provider = "local_kokoro"
    job = RenderJob(
        id=f"acceptance-{name}",
        batch_id=f"acceptance-{name}",
        platform_id="goodnovel",
        source_file=str(inputs["source"]),
        title=f"Acceptance {name}",
        code="B73165",
        promo_code_snapshot="B73165",
        video_folder=str(inputs["multi_videos"] if multi else inputs["single_videos"]),
        music_folder=str(inputs["music"] if bgm else inputs["no_music"]),
        output_folder=str(output_root),
        novel_id="acceptance-novel",
        revision_id="acceptance-revision",
        episode_id="acceptance-episode",
        episode_ids=("acceptance-episode",),
        episode_label="E001",
        production_draft_id=f"acceptance-draft-{name}",
        production_run_id=f"acceptance-run-{name}",
        episode_number=1,
        episode_count=1,
        is_final_episode=True,
        variant_seed=73,
        cover_path=str(inputs["cover"]),
        locked_voice_provider="local_kokoro",
        locked_voice_id="af_heart",
        story_mood="suspense",
        settings_snapshot=settings.to_dict(),
    )
    platform = PlatformProfile(
        id="goodnovel",
        name="GoodNovel",
        search_template="Search {platform}: {code}",
        ending_template="Download {platform} and search code {code} to continue reading.",
    )
    stages: list[dict[str, object]] = []

    def progress(status, value: float, label: str) -> None:
        event = {"status": status.value, "progress": round(float(value), 3), "label": label}
        stages.append(event)
        print(json.dumps({"scenario": name, **event}, ensure_ascii=False), flush=True)

    runner = PipelineRunner(
        lambda: settings,
        ffmpeg_path=ffmpeg,
        text_provider_factory=lambda _config: OfflineTextProvider(),
        tts_provider_factory=lambda _config: OfflineTTSProvider(narration_seconds),
        usage_ledger=UsageLedger(scenario_root / "usage.json"),
        work_root=work_root,
    )
    started = time.perf_counter()
    video_path = Path(runner(job, platform, progress)).resolve()
    elapsed = time.perf_counter() - started
    audio_path = Path(job.narration_audio_file).resolve()
    video_probe = verify_video(ffprobe, video_path)
    audio_probe = verify_mp3(ffprobe, audio_path)

    job_dir = job_workspace_directory(job, work_root)
    manifest_path = job_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    selected_videos = [Path(item) for item in manifest["media"]["videos"]]
    distinct_videos = {str(item.resolve()).casefold() for item in selected_videos}
    if multi and len(distinct_videos) < 4:
        raise AcceptanceError(
            f"Multi-source scenario used {len(distinct_videos)} distinct clips; expected at least 4."
        )
    if not multi and len(distinct_videos) != 1:
        raise AcceptanceError("Single-source scenario did not remain on one source clip.")
    if str(manifest["media"].get("bgm_mode")) != ("auto" if bgm else "none"):
        raise AcceptanceError("Manifest BGM mode does not match the scenario.")
    if not bgm and manifest["media"].get("music"):
        raise AcceptanceError("No-BGM scenario unexpectedly selected a music track.")

    subtitle_path = job_dir / ".work" / "subtitles.ass"
    subtitle_text = subtitle_path.read_text(encoding="utf-8-sig")
    if "Dialogue:" not in subtitle_text or "B73165" not in subtitle_text:
        raise AcceptanceError("ASS subtitles were not created with the search code.")
    command_text = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in sorted(job_dir.glob("render-command*.txt"))
    )
    if "subtitles.ass" not in command_text.replace("\\\\", "\\"):
        raise AcceptanceError("The real FFmpeg render command did not burn the ASS subtitles.")

    return {
        "name": name,
        "ok": True,
        "elapsed_seconds": round(elapsed, 2),
        "video": str(video_path),
        "video_bytes": video_path.stat().st_size,
        "video_probe": video_probe,
        "mp3": str(audio_path),
        "mp3_bytes": audio_path.stat().st_size,
        "mp3_probe": audio_probe,
        "distinct_source_clips": len(distinct_videos),
        "bgm_mode": manifest["media"]["bgm_mode"],
        "safe_serial_render": bool(manifest["media"].get("safe_serial_render")),
        "subtitle_burn_command_verified": True,
        "stages": stages,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Offline real-render acceptance test for StoryForge."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--quick", action="store_true", help="Render two short acceptance videos (default).")
    mode.add_argument("--stress", action="store_true", help="Render longer videos for sustained-load testing.")
    parser.add_argument("--stress-seconds", type=float, default=90.0)
    parser.add_argument("--root", type=Path, default=_default_root())
    parser.add_argument("--app-root", type=Path)
    parser.add_argument("--ffmpeg", type=Path)
    parser.add_argument("--ffprobe", type=Path)
    parser.add_argument(
        "--encoder",
        choices=("libx264", "auto", "h264_nvenc", "h264_qsv", "h264_amf"),
        default="libx264",
        help="libx264 is the deterministic cross-machine acceptance default.",
    )
    parser.add_argument("--json-report", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    ffmpeg, ffprobe = resolve_tools(args)
    narration_seconds = max(20.0, float(args.stress_seconds)) if args.stress else 5.0
    run_id = time.strftime("%Y%m%d-%H%M%S")
    run_root = args.root.expanduser().resolve() / run_id
    run_root.mkdir(parents=True, exist_ok=False)
    inputs = prepare_inputs(ffmpeg, run_root, narration_seconds=narration_seconds)
    started = time.perf_counter()
    results = [
        render_scenario(
            name="single-with-bgm",
            root=run_root,
            ffmpeg=ffmpeg,
            ffprobe=ffprobe,
            inputs=inputs,
            narration_seconds=narration_seconds,
            encoder=args.encoder,
            multi=False,
            bgm=True,
        ),
        render_scenario(
            name="multi-four-no-bgm",
            root=run_root,
            ffmpeg=ffmpeg,
            ffprobe=ffprobe,
            inputs=inputs,
            narration_seconds=narration_seconds,
            encoder=args.encoder,
            multi=True,
            bgm=False,
        ),
    ]
    report = {
        "ok": True,
        "mode": "stress" if args.stress else "quick",
        "elapsed_seconds": round(time.perf_counter() - started, 2),
        "run_root": str(run_root),
        "ffmpeg": str(ffmpeg),
        "ffprobe": str(ffprobe),
        "encoder": args.encoder,
        "scenarios": results,
    }
    report_path = (
        args.json_report.expanduser().resolve()
        if args.json_report
        else run_root / "acceptance-report.json"
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({**report, "report": str(report_path)}, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (AcceptanceError, OSError, subprocess.TimeoutExpired, ValueError) as error:
        print(
            json.dumps(
                {"ok": False, "error": f"{type(error).__name__}: {error}"},
                ensure_ascii=False,
            ),
            file=sys.stderr,
            flush=True,
        )
        raise SystemExit(1)
