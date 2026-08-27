from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_MODEL_SUFFIXES = {".gguf", ".onnx", ".safetensors", ".bin", ".partial"}


def _is_release_source(path: Path) -> bool:
    relative = path.relative_to(ROOT)
    return not any(
        part in {"tests", "experimental", ".git", ".venv", "node_modules", "__pycache__"}
        or part.startswith(".pytest-")
        for part in relative.parts
    )


def test_local_model_catalog_is_archived_outside_the_release_boundary() -> None:
    catalog = json.loads(
        (ROOT / "experimental" / "local_models" / "catalog.v1.json").read_text(
            encoding="utf-8"
        )
    )

    assert catalog == {"catalog_version": 1, "allowed_hosts": [], "packages": []}


def test_release_sources_contain_no_model_weights_or_partial_downloads() -> None:
    bundled_assets = [
        path.relative_to(ROOT).as_posix()
        for path in ROOT.rglob("*")
        if path.is_file() and _is_release_source(path) and path.suffix.lower() in FORBIDDEN_MODEL_SUFFIXES
    ]

    assert bundled_assets == []
