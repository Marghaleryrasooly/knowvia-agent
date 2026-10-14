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
DEFAULT_GEMINI_INFERENCE_MODEL = "gemini-3.8-flash"
DEFAULT_GROQ_MODEL = "openai/gpt-oss-120b"


class GroqGenerationError(RuntimeError):
    """Allowlisted diagnostics only; no remote bodies, credentials or prompts."""

    def __init__(self, category: str, status: int | None = None) -> None:
        self.category, self.status = category, status
        super().__init__(f"Groq {category} failure" + (f" (status {status})" if status else ""))


def _groq_post_json(url, payload, timeout, headers):
    from urllib.request import HTTPRedirectHandler, build_opener

    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, response_headers, newurl):
            return None

    request = Request(url, data=json.dumps(payload).encode("utf-8"),
                      headers={**headers, "User-Agent": "knowvia-agent/1.0"}, method="POST")
    with build_opener(NoRedirect()).open(request, timeout=timeout) as response:
        body = response.read(2_000_001)
        if len(body) > 2_000_000:
            raise GroqGenerationError("invalid_response")
        return json.loads(body.decode("utf-8"))


class GroqGenerationProvider:
    """Optional non-streaming text adapter. No tools, retries or fallback."""

    def __init__(self, api_key: str, model: str = DEFAULT_GROQ_MODEL,
                 timeout: float = 120.0, temperature: float = 0.1,
                 max_output_tokens: int = 1024, reasoning_effort: str = "low",
                 transport: Callable | None = None) -> None:
        if not isinstance(api_key, str) or not api_key or any(ord(c) <= 32 for c in api_key):
            raise GroqGenerationError("configuration")
        if model != DEFAULT_GROQ_MODEL or reasoning_effort not in {"low", "medium", "high"}:
            raise GroqGenerationError("configuration")
        if (not math.isfinite(timeout) or not 0 < timeout <= 300
                or not math.isfinite(temperature) or not 0 <= temperature <= 2
                or type(max_output_tokens) is not int or not 1 <= max_output_tokens <= 65536):
            raise GroqGenerationError("configuration")
        self._model, self._timeout, self._token = model, timeout, api_key
        self._temperature, self._max_output_tokens = temperature, max_output_tokens
        self.reasoning_effort = reasoning_effort
        self._transport = transport or _groq_post_json
        self.last_call: dict[str, Any] = {}

    @property
    def model_name(self) -> str:
        return self._model

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        from time import perf_counter
        start = perf_counter()
        self.last_call = {"http_status": None, "finish_reason": None, "token_usage": {},
                          "error_category": None}
        try:
            payload = {"model": self._model,
                       "messages": [{"role": "system", "content": system_prompt},
                                    {"role": "user", "content": user_prompt}],
                       "stream": False, "n": 1, "temperature": self._temperature,
                       "max_completion_tokens": self._max_output_tokens,
                       "reasoning_effort": self.reasoning_effort, "include_reasoning": False,
                       "tool_choice": "none"}
            try:
                response = self._transport("https://api.groq.com/openai/v1/chat/completions",
                                           payload, self._timeout,
                                           {"Content-Type": "application/json",
                                            "Authorization": f"Bearer {self._token}"})
            except HTTPError as error:
                category = ("authentication" if error.code in (401, 403) else
                            "quota" if error.code == 429 else "billing" if error.code == 402 else
                            "invalid_request" if error.code == 400 else "api_access" if error.code == 404 else
                            "server" if 500 <= error.code <= 599 else "http")
                raise GroqGenerationError(category, error.code) from None
            except (TimeoutError,):
                raise GroqGenerationError("timeout") from None
            except URLError as error:
                raise GroqGenerationError("timeout" if isinstance(error.reason, TimeoutError) else "transport") from None
            except (json.JSONDecodeError, UnicodeError):
                raise GroqGenerationError("invalid_response") from None
            except GroqGenerationError:
                raise
            except Exception:
                raise GroqGenerationError("transport") from None
            self.last_call["http_status"] = 200
            if not isinstance(response, dict) or response.get("error"):
                raise GroqGenerationError("invalid_response")
            usage = response.get("usage")
            if isinstance(usage, dict):
                for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                    value = usage.get(key)
                    if type(value) is int and value >= 0:
                        self.last_call["token_usage"][key] = value
                details = usage.get("completion_tokens_details")
                if isinstance(details, dict) and type(details.get("reasoning_tokens")) is int and details["reasoning_tokens"] >= 0:
                    self.last_call["token_usage"]["reasoning_tokens"] = details["reasoning_tokens"]
            choices = response.get("choices")
            if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
                raise GroqGenerationError("invalid_response")
            choice = choices[0]
            finish = choice.get("finish_reason")
            self.last_call["finish_reason"] = finish if finish in ("stop", "length", "content_filter", "tool_calls", "function_call") else None
            if finish != "stop":
                raise GroqGenerationError("incomplete_output" if finish == "length" else
                                          "blocked" if finish == "content_filter" else "invalid_response")
            message = choice.get("message")
            if not isinstance(message, dict) or message.get("role") != "assistant":
                raise GroqGenerationError("invalid_response")
            if message.get("tool_calls") or message.get("function_call"):
                raise GroqGenerationError("invalid_response")
            if message.get("refusal"):
                raise GroqGenerationError("blocked")
            content = message.get("content")
            if not isinstance(content, str):
                raise GroqGenerationError("invalid_response")
            if not content.strip():
                raise GroqGenerationError("empty_response")
            return content.strip()
        except GroqGenerationError as error:
            self.last_call["error_category"] = error.category
            if error.status is not None:
                self.last_call["http_status"] = error.status
            raise
        except Exception:
            self.last_call["error_category"] = "invalid_response"
            raise GroqGenerationError("invalid_response") from None
        finally:
            self.last_call["elapsed_seconds"] = round(perf_counter() - start, 3)


class GeminiGenerationError(RuntimeError):
    """Sanitized inference failure; never contains prompts or credentials."""


class GeminiGenerationProvider:
    """Independent Developer API text client, with no tools, retries or fallback."""

    def __init__(self, api_key: str, model: str = DEFAULT_GEMINI_INFERENCE_MODEL,
                 timeout: float = 120.0, temperature: float = 0.1,
                 max_output_tokens: int = 220, client=None) -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise GeminiGenerationError("Gemini inference credentials are missing")
        if not isinstance(model, str) or not re.fullmatch(r"gemini-[a-zA-Z0-9_.-]+", model):
            raise GeminiGenerationError("Gemini inference model configuration is invalid")
        if not math.isfinite(timeout) or not 0 < timeout <= 300:
            raise GeminiGenerationError("Gemini inference timeout configuration is invalid")
        self._model, self._timeout = model, timeout
        self._temperature, self._max_output_tokens = temperature, max_output_tokens
        self.thinking_level = "high"
        if client is None:
            try:
                from google import genai
                # Explicit Developer API, independent of Vertex environment settings.
                client = genai.Client(api_key=api_key, vertexai=False,
                                      http_options={"timeout": int(timeout * 1000),
                                                    "retry_options": {"attempts": 1}})
            except Exception:
                raise GeminiGenerationError("Gemini inference client initialization failed") from None
        self._client = client

    @property
    def model_name(self) -> str:
        return self._model

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        try:
            response = self._client.models.generate_content(
                model=self._model, contents=user_prompt,
                config={"system_instruction": system_prompt,
                        "temperature": self._temperature,
                        "max_output_tokens": self._max_output_tokens,
                        "candidate_count": 1,
                        "thinking_config": {"thinking_level": self.thinking_level,
                                            "include_thoughts": False}})
        except Exception as error:
            code = getattr(error, "code", None)
            category = ("authentication" if code in (401, 403) else
                        "rate/quota" if code == 429 else
                        "server" if isinstance(code, int) and 500 <= code <= 599 else
                        "timeout" if isinstance(error, TimeoutError) or "timeout" in type(error).__name__.lower() else
                        "transport")
            raise GeminiGenerationError(f"Gemini inference {category} failure") from None
        try:
            feedback = getattr(response, "prompt_feedback", None)
            reason = getattr(feedback, "block_reason", None)
            if reason and getattr(reason, "value", reason) != "BLOCK_REASON_UNSPECIFIED":
                raise ValueError
            candidates = response.candidates
            if not candidates or len(candidates) != 1:
                raise ValueError
            candidate = candidates[0]
            if getattr(candidate.finish_reason, "value", candidate.finish_reason) != "STOP":
                raise ValueError
            parts = candidate.content.parts
            if not parts:
                raise ValueError
            texts = []
            for part in parts:
                if any(getattr(part, field, None) is not None for field in (
                    "function_call", "function_response", "inline_data", "file_data",
                    "executable_code", "code_execution_result")):
                    raise ValueError
                if getattr(part, "thought", False):
                    continue
                if not isinstance(part.text, str):
                    raise ValueError
                texts.append(part.text)
            text = "".join(texts).strip()
            if not text:
                raise ValueError
            return text
        except Exception:
            raise GeminiGenerationError("Gemini inference blocked, incomplete or invalid response") from None


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
