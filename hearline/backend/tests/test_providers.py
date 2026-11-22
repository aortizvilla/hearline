"""Real-API providers tested with fake clients: OpenAI batches, Amazon Polly, pricing lookups."""

import io
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from app.config import pipeline_cfg
from app.providers.llm import LLMError, LLMRequest, OpenAILLM, TransientLLMError, WebSearchSpec
from app.providers.tts import PollyTTSProvider, split_text
from app.tracking.costs import llm_cost, tts_cost


def _response_body(text: str, model: str = "gpt-5.4-mini-2026-03-17") -> dict[str, Any]:
    return {
        "id": "resp_1", "object": "response", "created_at": 0, "model": model,
        "status": "completed", "parallel_tool_calls": True, "tool_choice": "auto", "tools": [],
        "output": [{"type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
                    "content": [{"type": "output_text", "text": text, "annotations": []}]}],
        "usage": {"input_tokens": 1000, "output_tokens": 200, "total_tokens": 1200,
                  "input_tokens_details": {"cached_tokens": 400, "cache_write_tokens": 0},
                  "output_tokens_details": {"reasoning_tokens": 0}},
    }


class FakeOpenAI:
    """Just enough of the OpenAI client for the Batch API flow."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows, self.uploaded, self.direct_calls = rows, "", 0
        self.files = SimpleNamespace(create=self._upload, content=self._content)
        self.batches = SimpleNamespace(create=self._create, retrieve=self._retrieve)
        self.responses = SimpleNamespace(create=self._direct)

    def _upload(self, file: tuple[str, bytes], purpose: str) -> Any:
        assert purpose == "batch"
        self.uploaded = file[1].decode()
        return SimpleNamespace(id="file_in")

    def _create(self, input_file_id: str, endpoint: str, completion_window: str) -> Any:
        assert endpoint == "/v1/responses"
        return SimpleNamespace(id="batch_1", status="in_progress")

    def _retrieve(self, batch_id: str) -> Any:
        return SimpleNamespace(id=batch_id, status="completed", output_file_id="file_out",
                               error_file_id=None)

    def _content(self, file_id: str) -> Any:
        return SimpleNamespace(text="\n".join(json.dumps(r) for r in self.rows))

    def _direct(self, **params: Any) -> Any:
        from openai.types.responses import Response

        self.direct_calls += 1
        return Response.model_validate(_response_body("searched"))


def _req(purpose: str, web: bool = False) -> LLMRequest:
    return LLMRequest(purpose=purpose, model="gpt-5.4-mini", system="s",
                      messages=[{"role": "user", "content": "hi"}], max_tokens=100,
                      web_search=WebSearchSpec(max_uses=3) if web else None)


def test_openai_batch_maps_results_back_in_order_and_keeps_web_search_direct() -> None:
    llm = OpenAILLM(api_key="test", batch_poll_sec=0)
    rows = [  # returned out of order, one item failed
        {"custom_id": "1", "response": {"status_code": 400, "body": {"error": "bad"}}},
        {"custom_id": "0", "response": {"status_code": 200, "body": _response_body("first")}},
    ]
    llm.client = FakeOpenAI(rows)  # type: ignore[assignment]
    out = llm.complete_many([_req("clip_write"), _req("clip_verify"), _req("research", web=True)],
                            batch=True)

    first = out[0]
    assert not isinstance(first, Exception)
    assert first.text == "first" and first.batched
    assert (first.usage.input_tokens, first.usage.cache_read_tokens) == (600, 400)
    assert isinstance(out[1], LLMError) and not isinstance(out[1], TransientLLMError)
    assert not isinstance(out[2], Exception) and not out[2].batched  # web search ran directly
    assert llm.client.direct_calls == 1  # type: ignore[attr-defined]
    sent = [json.loads(line) for line in llm.client.uploaded.splitlines()]  # type: ignore[attr-defined]
    assert [s["body"]["model"] for s in sent] == ["gpt-5.4-mini", "gpt-5.4-mini"]


class FakePolly:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def synthesize_speech(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(kw)
        mp3 = (Path(__file__).parent / "data" / "silence.mp3").read_bytes()
        return {"AudioStream": io.BytesIO(mp3)}


def test_polly_picks_the_language_voice_splits_long_text_and_is_priced(tmp_path: Path) -> None:
    fake = FakePolly()
    tts = PollyTTSProvider("neural", pipeline_cfg().tts.voices, client=fake)
    long_text = "Una frase corta. " * 400  # ~6,800 chars: over one request's limit
    result = tts.synthesize(long_text, tmp_path / "clip", language="es", voice="leo")

    assert len(fake.calls) == 3 and all(len(c["Text"]) <= tts.MAX_CHARS for c in fake.calls)
    assert {(c["VoiceId"], c["Engine"], c["OutputFormat"]) for c in fake.calls} == {
        ("Sergio", "neural", "mp3")}
    assert result.path.suffix == ".mp3" and result.duration_sec > 0
    assert tts.name == "polly-neural" and tts_cost(tts.name, 1_000_000) == 16.0


def test_split_text_keeps_sentences_whole() -> None:
    assert split_text("One. Two. Three.", 10) == ["One. Two.", "Three."]


def test_dated_openai_snapshots_use_the_base_model_price() -> None:
    assert llm_cost("gpt-5.4-mini-2026-03-17", 1_000_000, 0) == 0.75
    assert llm_cost("gpt-5.4-mini", 0, 1_000_000, batched=True) == 2.25
    assert llm_cost("gpt-5.4-nano", 1_000_000, 0) == 0.20  # not confused with gpt-5.4-mini
