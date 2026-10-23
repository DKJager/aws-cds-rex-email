import base64
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "engine"))

os.environ.setdefault("ENGINE_TOKEN", "test-token-0123456789")
os.environ.setdefault("SESSIONS_TABLE", "awscds-engine-sessions")
os.environ.setdefault("AWS_REGION", "us-east-1")

import app  # noqa: E402

TOKEN = os.environ["ENGINE_TOKEN"]
PRIMARY = "openai.gpt-oss-120b-1:0"
FALLBACK = "amazon.nova-pro-v1:0"


class FakeTable:
    def __init__(self, item=None):
        self.item = item
        self.put_calls = []

    def get_item(self, Key):
        return {"Item": self.item} if self.item else {}

    def put_item(self, Item):
        self.put_calls.append(Item)
        return {}


class FakeBedrock:
    def __init__(self, text="Hello from Rex.", fail_models=()):
        self.text = text
        self.fail_models = set(fail_models)
        self.calls = []

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs["modelId"] in self.fail_models:
            raise RuntimeError("model unavailable")
        return {"output": {"message": {"role": "assistant",
                                       "content": [{"text": self.text}]}}}


def _event(body, token=TOKEN, b64=False):
    raw = body if isinstance(body, str) else json.dumps(body)
    if b64:
        raw = base64.b64encode(raw.encode()).decode()
    return {
        "headers": {"authorization": f"Bearer {token}", "content-type": "application/json"},
        "body": raw,
        "isBase64Encoded": b64,
    }


@pytest.fixture
def fakes(monkeypatch):
    monkeypatch.setenv("ENGINE_TOKEN", TOKEN)
    monkeypatch.setenv("MODEL_ID", PRIMARY)
    monkeypatch.setenv("MODEL_ID_FALLBACK", FALLBACK)
    table = FakeTable()
    bedrock = FakeBedrock()
    monkeypatch.setattr(app, "get_table", lambda: table)
    monkeypatch.setattr(app, "get_bedrock", lambda: bedrock)
    return {"table": table, "bedrock": bedrock}


def test_bad_token_returns_401(fakes):
    resp = app.handler(_event({"question": "hi", "session_id": "s1"}, token="wrong"), None)
    assert resp["statusCode"] == 401
    assert json.loads(resp["body"]) == {"error": "unauthorized"}
    assert fakes["bedrock"].calls == []


def test_missing_authorization_header_returns_401(fakes):
    event = _event({"question": "hi", "session_id": "s1"})
    event["headers"] = {}
    resp = app.handler(event, None)
    assert resp["statusCode"] == 401


def test_happy_path_answer(fakes):
    fakes["bedrock"].text = "Your top segment is repeat buyers."
    resp = app.handler(
        _event({"question": "Which customer segment buys most?", "session_id": "s1",
                "awaiting_clarification": False}), None)
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert set(body) == {"answer", "steps"}
    assert body["answer"] == "Your top segment is repeat buyers."
    assert "loaded 0 prior turns" in body["steps"]
    assert f"asked model {PRIMARY}" in body["steps"]
    assert "saved turn" in body["steps"]
    call = fakes["bedrock"].calls[0]
    assert call["modelId"] == PRIMARY
    assert call["inferenceConfig"]["temperature"] == 0.2
    saved = fakes["table"].put_calls[0]
    assert saved["session_id"] == "s1"
    assert [t["role"] for t in saved["turns"]] == ["user", "assistant"]
    assert "expires_at" in saved


def test_history_is_loaded_and_trimmed(fakes):
    turns = []
    for i in range(5):
        turns.append({"role": "user", "content": f"q{i}"})
        turns.append({"role": "assistant", "content": f"a{i}"})
    fakes["table"].item = {"session_id": "s2", "turns": turns}
    resp = app.handler(_event({"question": "and now?", "session_id": "s2"}), None)
    body = json.loads(resp["body"])
    assert "loaded 6 prior turns" in body["steps"]
    assert len(fakes["table"].put_calls[0]["turns"]) == app.MAX_TURNS
    messages = fakes["bedrock"].calls[0]["messages"]
    roles = [m["role"] for m in messages]
    assert roles[0] == "user" and roles[-1] == "user"
    assert all(roles[i] != roles[i + 1] for i in range(len(roles) - 1))


def test_feedback_is_passed_to_model(fakes):
    app.handler(_event({"question": "retry that", "session_id": "s3",
                        "feedback": "too vague"}), None)
    last_user = fakes["bedrock"].calls[0]["messages"][-1]["content"][0]["text"]
    assert "too vague" in last_user


def test_clarify_on_empty_question(fakes):
    resp = app.handler(_event({"question": "   ", "session_id": "s1"}), None)
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert set(body) == {"clarify", "steps"}
    assert body["clarify"]
    assert fakes["bedrock"].calls == []


def test_clarify_when_model_flags_ambiguity(fakes):
    fakes["bedrock"].text = "CLARIFY: Which time period do you mean?"
    resp = app.handler(_event({"question": "how many?", "session_id": "s1"}), None)
    body = json.loads(resp["body"])
    assert set(body) == {"clarify", "steps"}
    assert body["clarify"] == "Which time period do you mean?"


def test_no_clarify_when_awaiting_clarification(fakes):
    fakes["bedrock"].text = "CLARIFY: still unsure"
    resp = app.handler(_event({"question": "last quarter", "session_id": "s1",
                               "awaiting_clarification": True}), None)
    body = json.loads(resp["body"])
    assert "answer" in body and "clarify" not in body


def test_bad_json_returns_400(fakes):
    resp = app.handler(_event("{not json"), None)
    assert resp["statusCode"] == 400
    assert "error" in json.loads(resp["body"])


def test_base64_body_is_decoded(fakes):
    resp = app.handler(_event({"question": "hi", "session_id": "s9"}, b64=True), None)
    assert resp["statusCode"] == 200
    assert "answer" in json.loads(resp["body"])


def test_fallback_model_on_first_failure(fakes):
    fakes["bedrock"].fail_models = {PRIMARY}
    fakes["bedrock"].text = "Answer from fallback."
    resp = app.handler(_event({"question": "What is churn?", "session_id": "s4"}), None)
    body = json.loads(resp["body"])
    assert body["answer"] == "Answer from fallback."
    assert [c["modelId"] for c in fakes["bedrock"].calls] == [PRIMARY, FALLBACK]
    assert any(PRIMARY in s and "failed" in s for s in body["steps"])
    assert f"fell back to model {FALLBACK}" in body["steps"]


def test_total_failure_never_leaks(fakes):
    fakes["bedrock"].fail_models = {PRIMARY, FALLBACK}
    resp = app.handler(_event({"question": "What is churn?", "session_id": "s5"}), None)
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["answer"] == app.UNAVAILABLE_ANSWER
    assert "all models failed" in body["steps"]
    assert fakes["table"].put_calls == []
