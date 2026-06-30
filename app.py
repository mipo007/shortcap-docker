#!/usr/bin/env python3
"""
Docker entry-point wrapper for shortcap.

Reads a JSON configuration file (default: /data/config.json) and invokes
shortcap's add_captions pipeline with the parameters specified in the JSON.
Optionally composites a watermark/logo onto the final video.

Usage inside Docker:
    python app.py                       # uses /data/config.json
    python app.py /custom/config.json   # uses a custom path
"""

import json
import logging
import os
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image as PILImage

from moviepy.editor import (
    CompositeVideoClip,
    ImageClip,
    VideoFileClip,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(name)-28s  %(levelname)-8s  %(message)s",
)
logger = logging.getLogger("shortcap.docker")

# ---------------------------------------------------------------------------
# Schema defaults — used when a key is missing from config.json
# ---------------------------------------------------------------------------
DEFAULTS = {
    "groq": {
        "api_key": "",
        "base_url": "https://api.groq.com/openai/v1",
        "model": "whisper-large-v3-turbo",
    },
    "subtitles": {
        "font_path": "",
        "font_size": 80,
        "font_color": "white",
        "stroke_width": 2,
        "stroke_color": "black",
        "highlight_current_word": True,
        "word_highlight_color": "yellow",
        "line_count": 2,
        "padding": 50,
        "position": "center",
        "shadow_strength": 1.0,
        "shadow_blur": 0.1,
    },
    "watermark": {
        "enabled": False,
        "logo_path": "",
        "opacity": 0.60,
        "position_corner": "top_right",
    },
    "files": {
        "input": "/data/input.mp4",
        "output": "/data/output.mp4",
    },
}


def deep_merge(defaults: dict, overrides: dict) -> dict:
    """Recursively merge *overrides* into a copy of *defaults*."""
    merged = defaults.copy()
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------
def load_config(path: str) -> dict:
    """Load and validate the JSON configuration file."""
    config_path = Path(path)
    if not config_path.exists():
        logger.error("Configuration file not found: %s", config_path)
        sys.exit(1)

    with open(config_path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)

    config = deep_merge(DEFAULTS, raw)

    # --- Validations -------------------------------------------------------
    if not config["groq"]["api_key"]:
        logger.error("groq.api_key is required in config.json")
        sys.exit(1)

    input_file = config["files"]["input"]
    if not Path(input_file).exists():
        logger.error("Input video not found: %s", input_file)
        sys.exit(1)

    wm = config["watermark"]
    if wm["enabled"] and not Path(wm["logo_path"]).exists():
        logger.error("Watermark logo not found: %s", wm["logo_path"])
        sys.exit(1)

    return config


# ---------------------------------------------------------------------------
# Environment setup  (Groq ↔ OpenAI shim)
# ---------------------------------------------------------------------------
def configure_environment(config: dict) -> None:
    """
    Inject the Groq credentials into the environment variables that
    shortcap's config.py reads (OPENAI_API_KEY / OPENAI_BASE_URL).
    """
    groq = config["groq"]
    os.environ["OPENAI_API_KEY"] = groq["api_key"]
    os.environ["OPENAI_BASE_URL"] = groq["base_url"]
    logger.info("OpenAI env vars overridden → base_url=%s", groq["base_url"])


# ---------------------------------------------------------------------------
# Watermark compositing
# ---------------------------------------------------------------------------
CORNER_MARGIN = 20  # px from edge


def _compute_watermark_position(
    corner: str,
    video_w: int,
    video_h: int,
    logo_w: int,
    logo_h: int,
) -> tuple[int, int]:
    """Return (x, y) for the watermark based on the selected corner."""
    positions = {
        "top_left":     (CORNER_MARGIN, CORNER_MARGIN),
        "top_right":    (video_w - logo_w - CORNER_MARGIN, CORNER_MARGIN),
        "bottom_left":  (CORNER_MARGIN, video_h - logo_h - CORNER_MARGIN),
        "bottom_right": (video_w - logo_w - CORNER_MARGIN, video_h - logo_h - CORNER_MARGIN),
    }
    return positions.get(corner, positions["top_right"])


def apply_watermark(
    video_path: str,
    output_path: str,
    logo_path: str,
    opacity: float = 0.60,
    corner: str = "top_right",
) -> None:
    """
    Open the rendered video, composite a semi-transparent logo on top,
    and write the result back to *output_path* (overwrites in-place if same).
    """
    logger.info("Applying watermark from %s (corner=%s, opacity=%.2f)", logo_path, corner, opacity)

    video = VideoFileClip(video_path)

    # Load and resize logo with Pillow (avoids MoviePy's broken resize
    # which calls PIL.Image.ANTIALIAS — removed in Pillow 10+).
    pil_logo = PILImage.open(logo_path).convert("RGBA")

    max_logo_w = int(video.w * 0.15)
    if pil_logo.width > max_logo_w:
        ratio = max_logo_w / pil_logo.width
        new_h = int(pil_logo.height * ratio)
        pil_logo = pil_logo.resize((max_logo_w, new_h), PILImage.LANCZOS)

    logo_array = np.array(pil_logo)

    logo = (
        ImageClip(logo_array, ismask=False, transparent=True)
        .set_duration(video.duration)
        .set_opacity(opacity)
    )

    pos = _compute_watermark_position(corner, video.w, video.h, logo.w, logo.h)
    logo = logo.set_position(pos)

    composite = CompositeVideoClip([video, logo])

    # Write to a temporary path, then overwrite to avoid read/write on same file
    tmp_output = output_path + ".tmp.mp4"
    composite.write_videofile(
        tmp_output,
        codec="libx264",
        fps=video.fps,
        logger="bar",
    )

    # Clean up handles before renaming
    video.close()
    composite.close()

    os.replace(tmp_output, output_path)
    logger.info("Watermark applied → %s", output_path)


# ---------------------------------------------------------------------------
# Monkey-patch transcriber to force the Groq model
# ---------------------------------------------------------------------------
def patch_transcriber_model(model_name: str) -> None:
    """
    Monkey-patch ``shortcap.transcriber.transcribe_with_api`` so that
    the ``model`` parameter sent to the OpenAI-compatible endpoint is
    *model_name* (e.g. ``whisper-large-v3-turbo``) instead of the
    hard-coded ``whisper-large-v3``.
    """
    from shortcap import transcriber as _mod

    _original = _mod.transcribe_with_api

    def _patched(audio_file, prompt=None):
        import openai
        from shortcap.config import get_openai_api_key, get_openai_api_base

        client = openai.OpenAI(
            api_key=get_openai_api_key(),
            base_url=get_openai_api_base(),
        )
        logger.info("Transcribing with model=%s via %s", model_name, get_openai_api_base())

        transcript = client.audio.transcriptions.create(
            model=model_name,
            file=open(audio_file, "rb"),
            response_format="verbose_json",
            timestamp_granularities=["segment", "word"],
            prompt=prompt,
        )

        # Format response identically to the original function
        modified_words = []
        for word in transcript.words:
            modified_words.append({
                "word": " " + word.word,
                "start": word.start,
                "end": word.end,
            })

        return [{
            "start": transcript.segments[0].start,
            "end": transcript.segments[-1].end,
            "words": modified_words,
        }]

    _mod.transcribe_with_api = _patched
    logger.info("Transcriber patched → model=%s", model_name)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    config_path = sys.argv[1] if len(sys.argv) > 1 else "/data/config.json"
    logger.info("Loading configuration from %s", config_path)

    config = load_config(config_path)

    # 1. Environment (Groq shim)
    configure_environment(config)

    # 2. Patch the transcriber model *before* importing add_captions
    #    (add_captions eventually calls transcribe_with_api)
    patch_transcriber_model(config["groq"]["model"])

    # Force-reload config module so it picks up the new env vars
    import importlib
    import shortcap.config as _cfg
    importlib.reload(_cfg)

    from shortcap.add_captions import add_captions

    sub = config["subtitles"]
    files = config["files"]

    start = time.time()

    logger.info("=== Starting shortcap pipeline ===")
    logger.info("Input  : %s", files["input"])
    logger.info("Output : %s", files["output"])

    # 3. Run the captioning pipeline
    add_captions(
        video_file=files["input"],
        output_file=files["output"],
        font=sub.get("font_path", "") or "TitanOne-Regular.ttf",
        font_size=sub["font_size"],
        font_color=sub["font_color"],
        stroke_width=sub["stroke_width"],
        stroke_color=sub["stroke_color"],
        highlight_current_word=sub["highlight_current_word"],
        word_highlight_color=sub["word_highlight_color"],
        line_count=sub["line_count"],
        padding=sub.get("padding", 50),
        position=sub.get("position", "center"),
        shadow_strength=sub.get("shadow_strength", 1.0),
        shadow_blur=sub.get("shadow_blur", 0.1),
        print_info=True,
        use_local_whisper=False,  # Always use API (Groq)
    )

    # 4. Watermark (post-process)
    wm = config["watermark"]
    if wm["enabled"]:
        apply_watermark(
            video_path=files["output"],
            output_path=files["output"],
            logo_path=wm["logo_path"],
            opacity=wm["opacity"],
            corner=wm["position_corner"],
        )

    elapsed = time.time() - start
    logger.info(
        "=== Pipeline finished in %02d:%02d ===",
        int(elapsed // 60),
        int(elapsed % 60),
    )


if __name__ == "__main__":
    main()
