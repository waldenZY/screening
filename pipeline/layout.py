"""Paths shared by generation, recovery and delivery validation."""
from pathlib import Path


def internal_root(path: Path) -> Path:
    path = path.expanduser().resolve()
    return path if path.name == "internal" else path / "internal"


def external_root(internal: Path) -> Path:
    return internal.parent / "external" / "evaluation-set-large"


def prompt_path(internal: Path, stage: str) -> Path:
    stage = {"admission_features": "admission", "admission_category": "category"}.get(stage, stage)
    return internal / "snapshots" / "prompts" / f"{stage}.md"
