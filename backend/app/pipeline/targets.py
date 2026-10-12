from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from app.errors import TargetSpecError
from app.models.target import TargetSpec

TARGETS_DIR = Path(__file__).resolve().parents[3] / "targets"
SPEC_SUFFIXES = (".yaml", ".yml")


def load_target_spec(path: Path) -> TargetSpec:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise TargetSpecError(str(path), f"cannot read file: {exc}") from exc
    except yaml.YAMLError as exc:
        raise TargetSpecError(str(path), f"not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise TargetSpecError(str(path), "top level must be a mapping")
    try:
        return TargetSpec.model_validate(raw)
    except ValidationError as exc:
        raise TargetSpecError(str(path), str(exc)) from exc


def spec_paths(directory: Path | None = None) -> list[Path]:
    root = directory or TARGETS_DIR
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.suffix in SPEC_SUFFIXES)


def load_target_specs(directory: Path | None = None) -> list[TargetSpec]:
    return [load_target_spec(path) for path in spec_paths(directory)]


def find_target_spec(target_id: str, directory: Path | None = None) -> TargetSpec | None:
    for spec in load_target_specs(directory):
        if spec.id == target_id:
            return spec
    return None
