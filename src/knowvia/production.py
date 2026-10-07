from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping

import uvicorn
from dotenv import load_dotenv

from knowvia.deployment_readiness import validate_readiness


@dataclass(frozen=True)
class ProductionServerSettings:
    host: str
    port: int
    workers: int = 1


def server_settings(environment: Mapping[str, str] | None = None) -> ProductionServerSettings:
    values = os.environ if environment is None else environment
    raw_port = values.get("PORT", "8000")
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise ValueError("PORT must be an integer") from exc
    if not 1 <= port <= 65535:
        raise ValueError("PORT must be between 1 and 65535")
    return ProductionServerSettings(host="0.0.0.0", port=port, workers=1)


def main() -> None:
    load_dotenv()
    validate_readiness()
    settings = server_settings()
    uvicorn.run(
        "knowvia.web:app",
        host=settings.host,
        port=settings.port,
        workers=settings.workers,
        reload=False,
    )


if __name__ == "__main__":
    main()
