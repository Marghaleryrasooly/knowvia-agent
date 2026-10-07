from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from dotenv import load_dotenv

from knowvia.deployment import DeploymentConfig
from knowvia.deployment_bootstrap import DeploymentResources, bootstrap_index
from knowvia.runtime_paths import RuntimePaths


async def _health_status(app) -> tuple[int, bytes]:
    messages: list[dict] = []
    sent_request = False

    async def receive():
        nonlocal sent_request
        if not sent_request:
            sent_request = True
            return {"type": "http.request", "body": b"", "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)

    await app(
        {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": "GET", "scheme": "https", "path": "/health", "raw_path": b"/health",
            "query_string": b"", "headers": [(b"host", b"localhost")],
            "client": ("127.0.0.1", 1), "server": ("localhost", 443),
        },
        receive,
        send,
    )
    status = next(message["status"] for message in messages if message["type"] == "http.response.start")
    body = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")
    return status, body


def validate_readiness(
    config: DeploymentConfig | None = None,
    paths: RuntimePaths | None = None,
    resources: DeploymentResources | None = None,
) -> dict[str, object]:
    selected_config = config or DeploymentConfig.from_environment()
    selected_config.validate()
    if selected_config.embedding_provider == "onnx":
        from knowvia.embeddings import validate_onnx_assets
        directory = os.getenv("KNOWVIA_ONNX_MODEL_DIR")
        if not directory:
            raise RuntimeError("KNOWVIA_ONNX_MODEL_DIR is required for ONNX embeddings")
        validate_onnx_assets(Path(directory))
    selected_paths = paths or RuntimePaths.from_environment()
    selected_resources = resources or DeploymentResources.from_environment()
    copied = bootstrap_index(selected_paths, selected_resources)

    from knowvia.web import create_web_app

    app = create_web_app(
        deployment_config=selected_config,
        runtime_paths=selected_paths,
    )
    status, body = asyncio.run(_health_status(app))
    if status != 200 or json.loads(body) != {"status": "ok"}:
        raise RuntimeError("Knowvia health self-check failed")
    return {
        "status": "ready",
        "environment": selected_config.environment.value,
        "index_initialized": copied,
        "health": "ok",
    }


def main() -> None:
    load_dotenv()
    print(json.dumps(validate_readiness(), sort_keys=True))


if __name__ == "__main__":
    main()
