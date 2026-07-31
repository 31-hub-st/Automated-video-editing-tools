from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from storyforge.models import AppSettings, PlatformProfile, RenderJob
from storyforge.pipeline import PipelineRunner, UsageLedger


def main() -> int:
    parser = argparse.ArgumentParser(description="Render one real StoryForge smoke video.")
    parser.add_argument("--root", type=Path, default=Path(".runtime-smoke/render"))
    args = parser.parse_args()
    root = args.root.resolve()

    settings = AppSettings(
        narration_wpm=240,
        bgm_volume=0.28,
        caption_mode="semantic",
        subtitle_preset="clear_outline",
        subtitle_animation="none",
        end_card_seconds=6.0,
        render_mode="speed",
        cover_animation="gentle_push",
    )
    settings.providers.text_provider = "local"
    settings.providers.tts_provider = "local_kokoro"
    settings.video_encoder = "auto"

    job = RenderJob(
        batch_id="smoke-batch",
        platform_id="goodnovel",
        source_file=str(root / "input" / "B73165_Smoke Story.txt"),
        title="Smoke Story",
        code="B73165",
        promo_code_snapshot="B73165",
        video_folder=str(root / "videos"),
        music_folder=str(root / "music"),
        output_folder=str(root / "output"),
        novel_id="smoke-novel",
        revision_id="smoke-revision",
        episode_id="smoke-episode-1",
        listing_id="smoke-listing",
        production_draft_id="smoke-draft",
        production_run_id=f"smoke-{int(time.time())}",
        publishing_account_label="待分配",
        episode_number=1,
        variant_index=1,
        variant_count=1,
        cover_path=str(root / "cover.jpg"),
        locked_voice_provider="local_kokoro",
        locked_voice_id="af_heart",
    )
    platform = PlatformProfile(
        id="goodnovel",
        name="GoodNovel",
        search_template="Search {platform}: {code}",
        ending_template=(
            "Download {platform} and search code {code} to continue reading."
        ),
    )

    stages: list[dict[str, object]] = []

    def progress(status, value: float, label: str) -> None:
        item = {
            "status": status.value,
            "progress": round(float(value), 3),
            "label": label,
        }
        stages.append(item)
        print(json.dumps(item, ensure_ascii=False), flush=True)

    started = time.perf_counter()
    runner = PipelineRunner(
        lambda: settings,
        usage_ledger=UsageLedger(root / "usage.json"),
    )
    output = Path(runner(job, platform, progress)).resolve()
    result = {
        "ok": True,
        "elapsed_seconds": round(time.perf_counter() - started, 2),
        "output": str(output),
        "bytes": output.stat().st_size,
        "stages": stages,
    }
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
