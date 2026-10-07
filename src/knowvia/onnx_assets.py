"""Explicit build/setup download of checksum-pinned assets; never used by requests."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import tempfile
from urllib.request import urlopen

from knowvia.embeddings import DEFAULT_LOCAL_MODEL, MINILM_REVISION, ONNX_ARTIFACTS, validate_onnx_assets


def prepare_assets(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    try:
        validate_onnx_assets(directory)
        return
    except RuntimeError:
        pass
    with tempfile.TemporaryDirectory(dir=directory) as temporary:
        staging = Path(temporary)
        for name, (remote, _) in ONNX_ARTIFACTS.items():
            url = f"https://huggingface.co/{DEFAULT_LOCAL_MODEL}/resolve/{MINILM_REVISION}/{remote}"
            with urlopen(url, timeout=120) as response, (staging / name).open("wb") as output:
                shutil.copyfileobj(response, output)
        validate_onnx_assets(staging)
        for name in ONNX_ARTIFACTS:
            os.replace(staging / name, directory / name)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    prepare_assets(parser.parse_args().directory)


if __name__ == "__main__":
    main()
