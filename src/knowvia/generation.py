from __future__ import annotations

import json
import math
import re
from typing import Any, Callable, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DEFAULT_OLLAMA_MODEL = "llama3.2:latest"
DEFAULT_OLLAMA_HOST = "http://localhost:11434"
DEFAULT_CLOUDFLARE_MODEL = "@cf/meta/llama-3.2-3b-instruct"


class CloudflareGenerationError(RuntimeError):
    """Sanitized remote inference failure; never includes provider response bodies."""


def _cloudflare_post_json(url, payload, timeout, headers):
    from urllib.request import HTTPRedirectHandler, build_opener
    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, response_headers, newurl):
            return None
    request = Request(url, data=json.dumps(payload).encode("utf-8"),
                      headers=headers, method="POST")
    with build_opener(NoRedirect()).open(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


class CloudflareGenerationProvider:
    """Non-streaming Workers AI REST adapter. No retries or provider fallback."""

    def __init__(self, account_id: str, api_token: str,
                 model: str = DEFAULT_CLOUDFLARE_MODEL, timeout: float = 120.0,
                 temperature: float = 0.1, max_output_tokens: int = 220,
                 transport: Callable | None = None) -> None:
        if not isinstance(account_id, str) or not re.fullmatch(r"[a-fA-F0-9]{32}", account_id):
            raise CloudflareGenerationError("Cloudflare account configuration is invalid")
        if not isinstance(api_token, str) or not api_token or any(ord(c) <= 32 for c in api_token):
            raise CloudflareGenerationError("Cloudflare token configuration is invalid")
        if not re.fullmatch(r"@cf/[a-zA-Z0-9_-]+/[a-zA-Z0-9_.-]+", model):
            raise CloudflareGenerationError("Cloudflare model configuration is invalid")
        if not math.isfinite(timeout) or not 0 < timeout <= 300:
            raise CloudflareGenerationError("Cloudflare timeout configuration is invalid")
        self._model = model
        self._url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/{model}"
        self._token = api_token
        self._timeout = timeout
        self._temperature = temperature
        self._max_output_tokens = max_output_tokens
        self._transport = transport or _cloudflare_post_json

    @property
    def model_name(self) -> str:
        return self._model

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        payload = {"messages": [{"role": "system", "content": system_prompt},
                                {"role": "user", "content": user_prompt}],
                   "stream": False, "temperature": self._temperature,
                   "max_tokens": self._max_output_tokens}
        try:
            response = self._transport(self._url, payload, self._timeout,
                                       {"Content-Type": "application/json",
                                        "Authorization": f"Bearer {self._token}"})
        except HTTPError as error:
            category = ("authentication" if error.code in (401, 403) else
                        "rate/quota" if error.code == 429 else
                        "server" if 500 <= error.code <= 599 else "HTTP")
            raise CloudflareGenerationError(f"Cloudflare {category} failure (status {error.code})") from None
        except (TimeoutError,):
            raise CloudflareGenerationError("Cloudflare timeout") from None
        except URLError as error:
            category = "timeout" if isinstance(error.reason, TimeoutError) else "connection failure"
            raise CloudflareGenerationError(f"Cloudflare {category}") from None
        except (json.JSONDecodeError, UnicodeError):
            raise CloudflareGenerationError("Cloudflare malformed JSON response") from None
        except OSError:
            raise CloudflareGenerationError("Cloudflare connection failure") from None
        except Exception:
            raise CloudflareGenerationError("Cloudflare transport failure") from None
        if not isinstance(response, dict) or response.get("success") is not True or response.get("errors"):
            raise CloudflareGenerationError("Cloudflare unsuccessful response envelope")
        result = response.get("result")
        if not isinstance(result, dict):
            raise CloudflareGenerationError("Cloudflare malformed response")
        if result.get("tool_calls") or response.get("tool_calls"):
            raise CloudflareGenerationError("Cloudflare unexpected tool-call output")
        text = result.get("response")
        # Workers AI may materialize a model-emitted JSON object in its REST
        # envelope. Preserve it as text for the unchanged downstream validator.
        if isinstance(text, dict) and text:
            if text.get("tool_calls"):
                raise CloudflareGenerationError("Cloudflare unexpected tool-call output")
            text = json.dumps(text, ensure_ascii=False)
        if not isinstance(text, str) or not text.strip():
            raise CloudflareGenerationError("Cloudflare empty or invalid response text")
        return text.strip()


class GenerationProvider(Protocol):
    """Generates text without coupling the answering pipeline to one runtime."""

    @property
    def model_name(self) -> str: ...

    def generate(self, system_prompt: str, user_prompt: str) -> str: ...


def _post_json(url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


class OllamaGenerationProvider:
    """Local text generation through Ollama's chat API."""

    def __init__(
        self,
        model: str = DEFAULT_OLLAMA_MODEL,
        host: str = DEFAULT_OLLAMA_HOST,
        timeout: float = 120.0,
        temperature: float = 0.1,
        max_output_tokens: int = 220,
        transport: Callable[[str, dict[str, Any], float], dict[str, Any]] | None = None,
        response_schema: dict[str, Any] | None = None,
    ) -> None:
        self._model = model
        self._host = host.rstrip("/")
        self._timeout = timeout
        self._temperature = temperature
        self._max_output_tokens = max_output_tokens
        self._transport = transport or _post_json
        self._response_schema = response_schema

    @property
    def model_name(self) -> str:
        return self._model

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "stream": False,
            "options": {
                "temperature": self._temperature,
                "num_predict": self._max_output_tokens,
            },
        }
        if self._response_schema is not None:
            payload["format"] = self._response_schema
        try:
            response = self._transport(
                f"{self._host}/api/chat", payload, self._timeout
            )
        except HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Ollama request failed ({error.code}): {detail}") from error
        except (URLError, TimeoutError, OSError) as error:
            raise RuntimeError(
                f"Could not connect to Ollama at {self._host}. "
                "Make sure Ollama is installed and running."
            ) from error

        content = response.get("message", {}).get("content", "")
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("Ollama returned an empty or invalid answer")
        return content.strip()
