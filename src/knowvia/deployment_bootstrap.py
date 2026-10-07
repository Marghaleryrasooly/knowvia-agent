from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from knowvia.runtime_paths import PROJECT_ROOT, RuntimePaths


class DeploymentBootstrapError(RuntimeError):
    """Required immutable deployment resources are missing or invalid."""


@dataclass(frozen=True)
class DeploymentResources:
    documents_dir: Path
    bundled_index: Path
    static_dir: Path

    @classmethod
    def from_environment(cls, environment: Mapping[str, str] | None = None) -> "DeploymentResources":
        values = os.environ if environment is None else environment
        return cls(
            documents_dir=Path(values.get("KNOWVIA_DOCUMENTS_DIR", PROJECT_ROOT / "knowvia_m1_dataset")).resolve(),
            bundled_index=Path(values.get("KNOWVIA_BUNDLED_INDEX", PROJECT_ROOT / "data" / "knowvia_index.json")).resolve(),
            static_dir=Path(__file__).with_name("static").resolve(),
        )


def validate_index(path: Path) -> None:
    if not path.is_file():
        raise DeploymentBootstrapError(f"Required vector index is missing: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DeploymentBootstrapError(f"Vector index is unreadable or invalid: {path}") from exc
    if payload.get("version") != 1 or not isinstance(payload.get("embedding_model"), str):
        raise DeploymentBootstrapError(f"Vector index metadata is invalid: {path}")
    chunks = payload.get("chunks")
    if not isinstance(chunks, list) or not chunks:
        raise DeploymentBootstrapError(f"Vector index contains no knowledge chunks: {path}")


def validate_immutable_resources(resources: DeploymentResources) -> None:
    if not resources.documents_dir.is_dir() or not any(resources.documents_dir.glob("*.md")):
        raise DeploymentBootstrapError(
            f"Knowledge documents are missing: {resources.documents_dir}"
        )
    for name in ("index.html", "app.css", "app.js"):
        if not (resources.static_dir / name).is_file():
            raise DeploymentBootstrapError(f"Required static asset is missing: {name}")
    validate_index(resources.bundled_index)


def bootstrap_index(paths: RuntimePaths, resources: DeploymentResources) -> bool:
    """Copy the immutable seed index once; never rebuild or overwrite runtime state."""
    paths.ensure_writable()
    validate_immutable_resources(resources)
    if paths.index.exists():
        validate_index(paths.index)
        return False
    if paths.index.resolve() == resources.bundled_index.resolve():
        validate_index(paths.index)
        return False
    try:
        shutil.copyfile(resources.bundled_index, paths.index)
    except OSError as exc:
        raise DeploymentBootstrapError(
            f"Could not initialize runtime vector index: {paths.index}"
        ) from exc
    validate_index(paths.index)
    return True
