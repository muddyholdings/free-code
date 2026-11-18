"""The tool manifest that ships inside the CLI.

This is the offline fallback and the reference for the server manifest at
https://free.ai/api/v1/code/manifest/. The server can add tools, change a
tool's description, disable a broken tool and announce a newer CLI release
without a CLI release of its own. ``scripts/export_manifest.py`` writes this
dict out as JSON for the free.ai repo.
"""

from __future__ import annotations

from typing import Any, Dict

SCHEMA = 1


def _tool(name: str, description: str, props: Dict[str, Any], required: list,
          http: Dict[str, Any], result: Dict[str, Any], confirm: str = "none") -> Dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "parameters": {"type": "object", "properties": props, "required": required},
        "http": http,
        "result": result,
        "confirm": confirm,
    }


def _s(desc: str) -> Dict[str, str]:
    return {"type": "string", "description": desc}


BUILTIN_MANIFEST: Dict[str, Any] = {
    "schema": SCHEMA,
    "manifest_version": 1,
    "latest_version": None,
    "min_version": None,
    "default_model": None,
    "disabled_tools": [],
    "tool_notes": {},
    "tools": [
        _tool(
            "generate_image",
            "Generate an image from a text prompt with Free.ai and save it into the project "
            "(for example an icon, hero image or placeholder). Costs tokens beyond the free tier on some models.",
            {
                "prompt": _s("What the image should show"),
                "output_path": _s("Where to save it, relative to the project, e.g. assets/hero.png"),
                "model": _s("Image model id (default flux2-klein; see list of models with /models)"),
                "aspect_ratio": _s("1:1, 16:9, 9:16, 4:3 or 3:4 (default 1:1)"),
                "negative_prompt": _s("Things to avoid"),
            },
            ["prompt", "output_path"],
            {"method": "POST", "path": "/v1/image/generate/", "timeout": 300,
             "body": {"prompt": "{prompt}", "model": "{model|flux2-klein}",
                      "aspect_ratio": "{aspect_ratio|1:1}", "negative_prompt": "{negative_prompt}"}},
            {"file_url": ["image_url", "url"], "save_arg": "output_path"},
            "write",
        ),
        _tool(
            "generate_speech",
            "Turn text into speech audio with Free.ai TTS and save it into the project "
            "(for example a voice prompt or a demo clip).",
            {
                "text": _s("The text to speak"),
                "output_path": _s("Where to save the audio, e.g. assets/intro.mp3"),
                "model": _s("TTS model id (default kokoro)"),
                "voice": _s("Voice name (default af_heart)"),
                "language": _s("Language code (default en)"),
            },
            ["text", "output_path"],
            {"method": "POST", "path": "/v1/tts/", "timeout": 180,
             "body": {"text": "{text}", "model": "{model|kokoro}", "voice": "{voice|af_heart}",
                      "language": "{language|en}"}},
            {"file_url": ["audio_url", "url"], "save_arg": "output_path"},
            "write",
        ),
        _tool(
            "generate_music",
            "Generate a short music clip from a text description and save it into the project.",
            {
                "prompt": _s("Style and mood of the music"),
                "output_path": _s("Where to save the audio, e.g. assets/theme.mp3"),
                "duration": {"type": "integer", "description": "Seconds (default 10)"},
                "model": _s("Music model id"),
            },
            ["prompt", "output_path"],
            {"method": "POST", "path": "/v1/music/generate/", "timeout": 300,
             "body": {"prompt": "{prompt}", "duration": "{duration|10}", "model": "{model}"}},
            {"file_url": ["audio_url", "url"], "save_arg": "output_path"},
            "write",
        ),
        _tool(
            "generate_video",
            "Generate a short video clip from a text prompt and save it into the project. "
            "Slow: expect a minute or more.",
            {
                "prompt": _s("What happens in the video"),
                "output_path": _s("Where to save the video, e.g. assets/demo.mp4"),
                "model": _s("Video model id (default cogvideox)"),
            },
            ["prompt", "output_path"],
            {"method": "POST", "path": "/v1/video/generate/", "timeout": 600,
             "body": {"prompt": "{prompt}", "model": "{model|cogvideox}"}},
            {"file_url": ["video_url", "url"], "save_arg": "output_path"},
            "write",
        ),
        _tool(
            "transcribe_audio",
            "Transcribe an audio or video file to text with Free.ai speech to text. "
            "The file is uploaded to Free.ai.",
            {
                "path": _s("Audio or video file, relative to the project or absolute"),
                "language": _s("Language hint, e.g. en (optional)"),
                "model": _s("STT model id (default whisper)"),
            },
            ["path"],
            {"method": "POST", "path": "/v1/stt/transcribe/", "timeout": 900,
             "body": {"model": "{model|whisper}", "language": "{language}"},
             "upload": {"arg": "path", "field": "file"}},
            {"text": ["text"]},
            "upload",
        ),
        _tool(
            "ocr_image",
            "Read the text out of an image or PDF (a screenshot, a scanned page) with Free.ai OCR. "
            "The file is uploaded to Free.ai.",
            {
                "path": _s("Image or PDF file, relative to the project or absolute"),
                "language": _s("OCR language code, e.g. eng, fra, deu (default eng)"),
            },
            ["path"],
            {"method": "POST", "path": "/v1/ocr/", "timeout": 300,
             "body": {"language": "{language|eng}"},
             "upload": {"arg": "path", "field": "file"}},
            {"text": ["text"]},
            "upload",
        ),
        _tool(
            "web_search",
            "Search the web and get a short cited answer plus source links. Use it for current "
            "library versions, error messages and API docs.",
            {"query": _s("A short search query (under 120 characters)")},
            ["query"],
            {"method": "POST", "path": "/v1/search/", "timeout": 90,
             "body": {"query": "{query}", "stream": False}},
            {"text": ["answer", "sources"]},
            "none",
        ),
        _tool(
            "translate_text",
            "Translate a short piece of text (a UI string, a message) into another language. "
            "For a whole i18n file use translate_file.",
            {
                "text": _s("Text to translate"),
                "target": _s("Target language code, e.g. es, fr, ja"),
                "source": _s("Source language code (optional, auto-detected)"),
            },
            ["text", "target"],
            {"method": "POST", "path": "/v1/translate/", "timeout": 120,
             "body": {"text": "{text}", "target": "{target}", "source": "{source}"}},
            {"text": ["translated_text", "text"]},
            "none",
        ),
    ],
}
