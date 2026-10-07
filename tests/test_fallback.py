import json
from types import SimpleNamespace

import pytest
from google.genai import errors

from agent.llm import spec_extractor as se
from agent.observability import LLM_FALLBACKS

OK = json.dumps({"partner_name": "Moloco", "partner_type": "DSP", "metrics": ["latency"], "alerts": []})


class FakeClient:
    def __init__(self, behaviour):
        self.behaviour = behaviour  # model -> list of exceptions/"ok"
        self.calls = []
        self.models = SimpleNamespace(generate_content=self._gen)

    def _gen(self, *, model, **_):
        self.calls.append(model)
        step = self.behaviour[model].pop(0)
        if isinstance(step, Exception):
            raise step
        return SimpleNamespace(text=OK, usage_metadata=SimpleNamespace(prompt_token_count=10, candidates_token_count=5))


def quota():
    return errors.ClientError(429, {"error": {"code": 429, "message": "quota", "status": "RESOURCE_EXHAUSTED"}})


def unavailable():
    return errors.ServerError(503, {"error": {"code": 503, "message": "busy", "status": "UNAVAILABLE"}})


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(se.time, "sleep", lambda *_: None)


def test_primary_success():
    c = FakeClient({"a": ["ok"]})
    r = se.extract_spec_with_lineage("Onboard DSP Moloco latency", models=["a", "b"], client=c)
    assert r.model == "a" and not r.fallback_used and c.calls == ["a"]
    assert len(r.trace_id) == 32


def test_quota_falls_back_immediately_without_retry():
    before = LLM_FALLBACKS.labels(from_model="a", to_model="b", reason="quota_exhausted")._value.get()
    c = FakeClient({"a": [quota()], "b": ["ok"]})
    r = se.extract_spec_with_lineage("x", models=["a", "b"], client=c)
    assert r.model == "b" and r.fallback_used and r.attempted_models == ["a", "b"]
    assert c.calls == ["a", "b"]  # no retry on 429
    assert LLM_FALLBACKS.labels(from_model="a", to_model="b", reason="quota_exhausted")._value.get() == before + 1


def test_503_retries_then_falls_back():
    n = se.MODEL_CONFIG["max_attempts_per_model"]
    c = FakeClient({"a": [unavailable() for _ in range(n)], "b": ["ok"]})
    r = se.extract_spec_with_lineage("x", models=["a", "b"], client=c)
    assert r.model == "b" and c.calls == ["a"] * n + ["b"]


def test_all_fail():
    c = FakeClient({"a": [quota()], "b": [quota()]})
    with pytest.raises(se.AllModelsFailed):
        se.extract_spec_with_lineage("x", models=["a", "b"], client=c)


def test_non_retryable_error_is_raised():
    c = FakeClient({"a": [errors.ClientError(400, {"error": {"code": 400, "message": "bad"}})], "b": ["ok"]})
    with pytest.raises(errors.ClientError):
        se.extract_spec_with_lineage("x", models=["a", "b"], client=c)
    assert c.calls == ["a"]


def test_model_config_env_overrides(monkeypatch):
    monkeypatch.setenv("GEMINI_MODEL", "gemini-x")
    monkeypatch.setenv("GEMINI_FALLBACK_MODELS", "none")
    cfg = se.load_model_config()
    assert cfg["primary"] == "gemini-x" and cfg["fallbacks"] == []
    monkeypatch.setenv("GEMINI_MODEL", "")
    monkeypatch.setenv("GEMINI_FALLBACK_MODELS", "")
    cfg = se.load_model_config()
    assert cfg["primary"] == "gemini-3.5-flash" and cfg["fallbacks"] == ["gemini-2.5-flash"]
