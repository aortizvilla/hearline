"""LLM provider interface plus Anthropic and OpenAI implementations.

Pipeline code builds provider-neutral `LLMRequest`s; providers translate them (system prompt
caching, web search tool, structured output, effort) and return normalized usage for costing.
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

log = logging.getLogger(__name__)
T = TypeVar("T")


class LLMError(Exception):
    """Non-retryable LLM failure (bad request, auth, invalid model...)."""


class TransientLLMError(LLMError):
    """Retryable failure (rate limit, overload, network, 5xx)."""


class RefusalError(LLMError):
    """The model declined the request."""


@dataclass
class WebSearchSpec:
    max_uses: int = 5
    allowed_domains: list[str] = field(default_factory=list)
    blocked_domains: list[str] = field(default_factory=list)


@dataclass
class LLMRequest:
    # research | research_fix | clip_write | clip_adapt | clip_verify | topic_resolve | phrase_write
    purpose: str
    model: str
    system: str  # static instructions: cached (Anthropic cache_control / OpenAI prefix cache)
    messages: list[dict[str, Any]]
    max_tokens: int = 4000
    effort: str | None = None
    json_schema: dict[str, Any] | None = None  # strict structured output when set
    web_search: WebSearchSpec | None = None
    refusal_fallbacks: str | None = None
    context: dict[str, Any] = field(default_factory=dict)  # structured data for mock providers


@dataclass
class Usage:
    input_tokens: int = 0  # uncached input tokens
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    searches: int = 0

    def add(self, other: Usage) -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.cache_write_tokens += other.cache_write_tokens
        self.searches += other.searches


@dataclass
class LLMResponse:
    text: str
    model: str
    usage: Usage
    stop_reason: str = "end_turn"
    batched: bool = False
    provider: str = ""


class LLMProvider(ABC):
    name: str = "base"
    supports_batches: bool = False

    @abstractmethod
    def complete(self, req: LLMRequest) -> LLMResponse:
        """Run one request to completion."""

    def complete_many(
        self, reqs: list[LLMRequest], batch: bool = False
    ) -> list[LLMResponse | Exception]:
        """Run many requests; failures are returned in place, never raised."""
        out: list[LLMResponse | Exception] = []
        for req in reqs:
            try:
                out.append(self.complete(req))
            except Exception as exc:  # noqa: BLE001 - reported per item
                out.append(exc)
        return out


def with_retries(fn: Callable[[], T], attempts: int = 3, base_delay: float = 2.0) -> T:
    """Call fn, retrying TransientLLMError with exponential backoff + jitter."""
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except TransientLLMError as exc:
            if attempt == attempts:
                raise
            delay = base_delay * 2 ** (attempt - 1) * (0.8 + 0.4 * random.random())
            log.warning("Transient LLM error (%s); retry %d in %.1fs", exc, attempt, delay)
            time.sleep(delay)
    raise AssertionError("unreachable")


def extract_json(text: str) -> Any:
    """Parse the first JSON object/array in `text` (tolerates prose or code fences around it)."""
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fenced:
        text = fenced.group(1).strip()
    starts = [i for i in (text.find("{"), text.find("[")) if i >= 0]
    if not starts:
        raise ValueError("no JSON found in model output")
    start = min(starts)
    obj, _ = json.JSONDecoder().raw_decode(text[start:])
    return obj


# ---------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicLLM(LLMProvider):
    name = "anthropic"
    supports_batches = True

    def __init__(
        self,
        web_search_tool_type: str,
        api_key: str | None = None,
        max_pause_turns: int = 3,
        batch_poll_sec: float = 30.0,
        batch_timeout_sec: int = 86400,
    ) -> None:
        import anthropic

        self._anthropic = anthropic
        self.client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
        self.tool_type = web_search_tool_type
        self.max_pause_turns = max_pause_turns
        self.batch_poll_sec = batch_poll_sec
        self.batch_timeout_sec = batch_timeout_sec

    # -- request building ---------------------------------------------------
    def _params(self, req: LLMRequest, messages: list[dict[str, Any]]) -> dict[str, Any]:
        params: dict[str, Any] = {
            "model": req.model,
            "max_tokens": req.max_tokens,
            "messages": messages,
        }
        if req.system:
            params["system"] = [
                {"type": "text", "text": req.system, "cache_control": {"type": "ephemeral"}}
            ]
        output_config: dict[str, Any] = {}
        if req.effort:
            output_config["effort"] = req.effort
        if req.json_schema:
            output_config["format"] = {"type": "json_schema", "schema": req.json_schema}
        if output_config:
            params["output_config"] = output_config
        if req.web_search:
            tool: dict[str, Any] = {
                "type": self.tool_type,
                "name": "web_search",
                "max_uses": req.web_search.max_uses,
            }
            if req.web_search.allowed_domains:
                tool["allowed_domains"] = req.web_search.allowed_domains
            elif req.web_search.blocked_domains:
                tool["blocked_domains"] = req.web_search.blocked_domains
            params["tools"] = [tool]
        return params

    def _convert_error(self, exc: Exception) -> LLMError:
        a = self._anthropic
        if isinstance(exc, a.RateLimitError | a.APIConnectionError | a.InternalServerError):
            return TransientLLMError(str(exc))
        if isinstance(exc, a.APIStatusError):
            if exc.status_code >= 500 or exc.status_code in (408, 409, 529):
                return TransientLLMError(str(exc))
            return LLMError(f"{exc.status_code}: {exc.message}")
        return LLMError(str(exc))

    @staticmethod
    def _usage(msg: Any) -> Usage:
        u = msg.usage
        stu = getattr(u, "server_tool_use", None)
        return Usage(
            input_tokens=u.input_tokens or 0,
            output_tokens=u.output_tokens or 0,
            cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
            searches=(getattr(stu, "web_search_requests", 0) or 0) if stu else 0,
        )

    @staticmethod
    def _text(msg: Any) -> str:
        return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")

    def _create(self, req: LLMRequest, messages: list[dict[str, Any]]) -> Any:
        params = self._params(req, messages)
        try:
            if req.refusal_fallbacks:
                return self.client.beta.messages.create(
                    **params, betas=[FALLBACK_BETA], fallbacks=req.refusal_fallbacks
                )
            return self.client.messages.create(**params)
        except self._anthropic.APIError as exc:
            raise self._convert_error(exc) from exc

    def _finish(
        self, req: LLMRequest, msg: Any, usage: Usage, text: str, batched: bool
    ) -> LLMResponse:
        """Resume pause_turn responses (server-tool loop limit) and check for refusals."""
        messages = list(req.messages)
        turns = 0
        while msg.stop_reason == "pause_turn" and turns < self.max_pause_turns:
            messages = [*messages, {"role": "assistant", "content": msg.content}]
            msg = self._create(req, messages)
            usage.add(self._usage(msg))
            text += self._text(msg)
            turns += 1
        if msg.stop_reason == "refusal":
            details = getattr(msg, "stop_details", None)
            raise RefusalError(f"model refused ({getattr(details, 'category', None)})")
        return LLMResponse(
            text=text,
            model=msg.model,
            usage=usage,
            stop_reason=msg.stop_reason or "",
            batched=batched,
            provider=self.name,
        )

    def complete(self, req: LLMRequest) -> LLMResponse:
        msg = self._create(req, req.messages)
        return self._finish(req, msg, self._usage(msg), self._text(msg), batched=False)

    # -- Message Batches API --------------------------------------------------
    def complete_many(
        self, reqs: list[LLMRequest], batch: bool = False
    ) -> list[LLMResponse | Exception]:
        if not batch or len(reqs) < 2:
            return super().complete_many(reqs)
        from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
        from anthropic.types.messages.batch_create_params import Request

        # Batches reject `fallbacks`, so batched requests go without it.
        requests = [
            Request(
                custom_id=f"r{i}",
                params=MessageCreateParamsNonStreaming(**self._params(r, r.messages)),  # type: ignore[typeddict-item]
            )
            for i, r in enumerate(reqs)
        ]
        try:
            created = self.client.messages.batches.create(requests=requests)
            deadline = time.monotonic() + self.batch_timeout_sec
            while True:
                status = self.client.messages.batches.retrieve(created.id)
                if status.processing_status == "ended":
                    break
                if time.monotonic() > deadline:
                    self.client.messages.batches.cancel(created.id)
                    raise TransientLLMError(f"batch {created.id} timed out")
                time.sleep(self.batch_poll_sec)
            results = {r.custom_id: r for r in self.client.messages.batches.results(created.id)}
        except self._anthropic.APIError as exc:
            raise self._convert_error(exc) from exc

        out: list[LLMResponse | Exception] = []
        for i, req in enumerate(reqs):
            res = results.get(f"r{i}")
            if res is None:
                out.append(TransientLLMError("missing batch result"))
                continue
            kind = res.result.type
            if kind == "succeeded":
                msg = res.result.message
                try:
                    out.append(
                        self._finish(req, msg, self._usage(msg), self._text(msg), batched=True)
                    )
                except Exception as exc:  # noqa: BLE001
                    out.append(exc)
            elif kind == "errored" and res.result.error.type == "invalid_request":
                out.append(LLMError(f"batch item invalid: {res.result.error}"))
            else:
                out.append(TransientLLMError(f"batch item {kind}"))
        return out


# ---------------------------------------------------------------------------
# OpenAI (Responses API)
# ---------------------------------------------------------------------------


class OpenAILLM(LLMProvider):
    """OpenAI Responses API. Batches (50% off) go through the Batch API with /v1/responses;
    requests that use web search always run directly."""

    name = "openai"
    supports_batches = True

    def __init__(self, web_search_tool_type: str = "web_search", api_key: str | None = None,
                 batch_poll_sec: float = 30.0, batch_timeout_sec: int = 86400) -> None:
        import openai
        from openai.types.responses import Response

        self._openai = openai
        self._response_type = Response
        self.client = openai.OpenAI(api_key=api_key) if api_key else openai.OpenAI()
        self.tool_type = web_search_tool_type
        self.batch_poll_sec = batch_poll_sec
        self.batch_timeout_sec = batch_timeout_sec

    def _convert_error(self, exc: Exception) -> LLMError:
        o = self._openai
        if isinstance(exc, o.RateLimitError | o.APIConnectionError | o.InternalServerError):
            return TransientLLMError(str(exc))
        if isinstance(exc, o.APIStatusError):
            if exc.status_code >= 500 or exc.status_code in (408, 409):
                return TransientLLMError(str(exc))
            return LLMError(f"{exc.status_code}: {exc.message}")
        return LLMError(str(exc))

    def _params(self, req: LLMRequest) -> dict[str, Any]:
        params: dict[str, Any] = {
            "model": req.model,
            "instructions": req.system,
            "input": [
                {"role": m["role"], "content": m["content"]}
                for m in req.messages
                if isinstance(m.get("content"), str)
            ],
            "max_output_tokens": req.max_tokens,
            "prompt_cache_key": f"hearline-{req.purpose}",
        }
        if req.effort:
            params["reasoning"] = {"effort": req.effort}
        if req.json_schema:
            params["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": req.purpose,
                    "schema": req.json_schema,
                    "strict": True,
                }
            }
        if req.web_search:
            tool: dict[str, Any] = {"type": self.tool_type}
            if req.web_search.allowed_domains:
                tool["filters"] = {"allowed_domains": req.web_search.allowed_domains}
            params["tools"] = [tool]
            params["max_tool_calls"] = req.web_search.max_uses
        return params

    def _to_response(self, req: LLMRequest, resp: Any, batched: bool) -> LLMResponse:
        searches = 0
        for item in resp.output or []:
            if item.type == "web_search_call":
                searches += 1
            if item.type == "message":
                for part in item.content:
                    if getattr(part, "type", "") == "refusal":
                        raise RefusalError(getattr(part, "refusal", "refused"))
        u = resp.usage
        cached = (u.input_tokens_details.cached_tokens or 0) if u and u.input_tokens_details else 0
        usage = Usage(
            input_tokens=(u.input_tokens - cached) if u else 0,
            output_tokens=u.output_tokens if u else 0,
            cache_read_tokens=cached,
            searches=searches,
        )
        return LLMResponse(
            text=resp.output_text or "",
            model=resp.model or req.model,
            usage=usage,
            stop_reason=resp.status or "",
            batched=batched,
            provider=self.name,
        )

    def complete(self, req: LLMRequest) -> LLMResponse:
        try:
            resp = self.client.responses.create(**self._params(req))
        except self._openai.APIError as exc:
            raise self._convert_error(exc) from exc
        return self._to_response(req, resp, batched=False)

    def complete_many(
        self, reqs: list[LLMRequest], batch: bool = False
    ) -> list[LLMResponse | Exception]:
        """With batch=True, requests without web search go through the Batch API."""
        to_batch = [i for i, r in enumerate(reqs) if batch and not r.web_search]
        out: list[LLMResponse | Exception] = [LLMError("not run")] * len(reqs)
        direct = [i for i in range(len(reqs)) if i not in set(to_batch)]
        for i, res in zip(direct, super().complete_many([reqs[i] for i in direct]), strict=True):
            out[i] = res
        if to_batch:
            for i, res in zip(to_batch, self._run_batch([reqs[i] for i in to_batch]),
                              strict=True):
                out[i] = res
        return out

    def _run_batch(self, reqs: list[LLMRequest]) -> list[LLMResponse | Exception]:
        """Upload a JSONL file, start a batch, wait for it, and map results back in order."""
        lines = [json.dumps({"custom_id": str(i), "method": "POST", "url": "/v1/responses",
                             "body": self._params(r)}) for i, r in enumerate(reqs)]
        try:
            upload = self.client.files.create(
                file=("hearline-batch.jsonl", "\n".join(lines).encode()), purpose="batch")
            job = self.client.batches.create(input_file_id=upload.id, endpoint="/v1/responses",
                                             completion_window="24h")
            waited = 0.0
            while job.status not in ("completed", "failed", "expired", "cancelled"):
                if waited >= self.batch_timeout_sec:
                    self.client.batches.cancel(job.id)
                    raise TransientLLMError(f"batch {job.id} timed out")
                time.sleep(self.batch_poll_sec)
                waited += self.batch_poll_sec
                job = self.client.batches.retrieve(job.id)
            rows: dict[str, dict[str, Any]] = {}
            for file_id in (job.output_file_id, job.error_file_id):
                if file_id:
                    for line in self.client.files.content(file_id).text.splitlines():
                        if line.strip():
                            row = json.loads(line)
                            rows[row["custom_id"]] = row
        except self._openai.APIError as exc:
            err = self._convert_error(exc)
            return [err for _ in reqs]

        out: list[LLMResponse | Exception] = []
        for i, req in enumerate(reqs):
            row = rows.get(str(i))
            result = (row or {}).get("response") or {}
            if not row or row.get("error") or result.get("status_code") != 200:
                detail = (row or {}).get("error") or result.get("body") or f"batch {job.status}"
                code = result.get("status_code") or 0
                out.append(LLMError(f"batch item failed: {detail}") if 400 <= code < 500
                           and code not in (408, 409, 429)
                           else TransientLLMError(f"batch item failed: {detail}"))
                continue
            try:
                # lenient, like the SDK's own parsing: tolerate fields added or omitted
                resp = self._response_type.construct(**result["body"])
                out.append(self._to_response(req, resp, batched=True))
            except Exception as exc:  # noqa: BLE001 - reported per item
                out.append(exc)
        return out
