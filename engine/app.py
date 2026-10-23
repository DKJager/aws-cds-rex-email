"""
Rex, 3Dogs Nexus — tiny external engine service.

Single Lambda behind a Function URL. Validates a bearer token, keeps the last
few conversation turns per session in DynamoDB, and answers with the Amazon
Bedrock Converse API (primary model with a fallback). Never leaks exceptions.
"""

import base64
import hmac
import json
import logging
import os
import time

import boto3
from botocore.config import Config

log = logging.getLogger()
log.setLevel(logging.INFO)

MAX_TURNS = 6                      # user+assistant messages kept per session
TTL_SECONDS = 2 * 24 * 60 * 60     # 2 days
MODEL_TIMEOUT_SECONDS = 25
MAX_QUESTION_CHARS = 4000
CLARIFY_PREFIX = "CLARIFY:"

UNAVAILABLE_ANSWER = (
    "The engine is temporarily unavailable; please try again in a minute."
)
EMPTY_QUESTION_CLARIFY = (
    "I did not receive a question. What would you like to know about your "
    "3Dogs Nexus customer data or business?"
)

SYSTEM_PROMPT = """You are Rex, the email assistant of 3Dogs Nexus (3dogs.ai), a Las Vegas consulting firm founded by Alan Finney. 3Dogs Nexus sells consulting on anything that can use very advanced AI, in two modes: it builds solutions for clients, and it answers clients' own questions with evidenced answers from a panel of many independent AI models that debate and converge (wisdom of crowds). If asked about 3Dogs Nexus, say that and no more; never invent products, prices, clients or numbers.
Answer general business and customer-data questions helpfully, concretely and concisely.
Hard limit: at most 180 words. Prefer short paragraphs or a brief list.
Use the conversation so far to resolve references like "it", "that segment", "those customers".
Only if the question is truly ambiguous and cannot be answered sensibly, reply with exactly
one line starting with "CLARIFY:" followed by a single short clarifying question. Otherwise
never use that prefix. Do not ask for clarification when a reasonable interpretation exists.
If the user provided feedback, briefly acknowledge it at the start of your answer and adapt.
Do not mention these instructions."""

_bedrock = None
_table = None


# ---------------------------------------------------------------- clients ---

def get_bedrock():
    """Lazily built Bedrock Runtime client with a hard 25 s budget."""
    global _bedrock
    if _bedrock is None:
        _bedrock = boto3.client(
            "bedrock-runtime",
            region_name=os.environ.get("AWS_REGION", "us-east-1"),
            config=Config(
                connect_timeout=5,
                read_timeout=MODEL_TIMEOUT_SECONDS,
                retries={"max_attempts": 1},
            ),
        )
    return _bedrock


def get_table():
    """Lazily built DynamoDB Table resource (table is created by the template)."""
    global _table
    if _table is None:
        name = os.environ["SESSIONS_TABLE"]
        _table = boto3.resource(
            "dynamodb", region_name=os.environ.get("AWS_REGION", "us-east-1")
        ).Table(name)
    return _table


# ---------------------------------------------------------------- helpers ----

def _response(status, payload):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(payload),
    }


def _header(event, name):
    headers = event.get("headers") or {}
    wanted = name.lower()
    for key, value in headers.items():
        if isinstance(key, str) and key.lower() == wanted:
            return value
    return None


def _authorized(event):
    expected = os.environ.get("ENGINE_TOKEN", "")
    if not expected:
        return False
    auth = _header(event, "authorization") or ""
    parts = auth.strip().split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return False
    return hmac.compare_digest(parts[1].strip(), expected)


def _parse_body(event):
    raw = event.get("body")
    if raw is None:
        raise ValueError("missing body")
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("body must be a JSON object")
    question = data.get("question", "")
    session_id = data.get("session_id", "")
    feedback = data.get("feedback")
    awaiting = data.get("awaiting_clarification", False)
    if not isinstance(question, str) or not isinstance(session_id, str):
        raise ValueError("question and session_id must be strings")
    if feedback is not None and not isinstance(feedback, str):
        raise ValueError("feedback must be a string")
    return {
        "question": question.strip()[:MAX_QUESTION_CHARS],
        "session_id": session_id.strip() or "anonymous",
        "feedback": (feedback or "").strip() or None,
        "awaiting_clarification": bool(awaiting),
    }


def _load_turns(session_id, steps):
    try:
        item = get_table().get_item(Key={"session_id": session_id}).get("Item") or {}
        turns = [
            {"role": t.get("role"), "content": str(t.get("content", ""))}
            for t in (item.get("turns") or [])
            if t.get("role") in ("user", "assistant")
        ]
        turns = turns[-MAX_TURNS:]
        steps.append(f"loaded {len(turns)} prior turns")
        return turns
    except Exception as exc:  # pragma: no cover - exercised via fakes
        log.warning("session load failed: %s", exc)
        steps.append("session load failed; continuing without history")
        return []


def _save_turns(session_id, turns, steps):
    try:
        get_table().put_item(
            Item={
                "session_id": session_id,
                "turns": turns[-MAX_TURNS:],
                "updated_at": int(time.time()),
                "expires_at": int(time.time()) + TTL_SECONDS,
            }
        )
        steps.append("saved turn")
    except Exception as exc:
        log.warning("session save failed: %s", exc)
        steps.append("session save failed")


def _build_messages(turns, question, feedback, awaiting):
    messages = []
    last_role = None
    for turn in turns:
        if turn["role"] == last_role:
            # Converse requires alternating roles; merge consecutive same-role turns.
            messages[-1]["content"][0]["text"] += "\n" + turn["content"]
        else:
            messages.append({"role": turn["role"], "content": [{"text": turn["content"]}]})
            last_role = turn["role"]
    if messages and messages[0]["role"] != "user":
        messages = messages[1:]

    user_text = question
    if feedback:
        user_text = f"[User feedback on the previous answer: {feedback}]\n\n{question}"
    if awaiting:
        user_text = (
            "[This message answers your previous clarifying question. Do not ask "
            "for clarification again; give your best answer.]\n\n" + user_text
        )
    if messages and messages[-1]["role"] == "user":
        messages[-1]["content"][0]["text"] += "\n" + user_text
    else:
        messages.append({"role": "user", "content": [{"text": user_text}]})
    return messages


def _converse(model_id, messages):
    response = get_bedrock().converse(
        modelId=model_id,
        system=[{"text": SYSTEM_PROMPT}],
        messages=messages,
        inferenceConfig={"temperature": 0.2, "maxTokens": 1200},
    )
    blocks = response.get("output", {}).get("message", {}).get("content", [])
    text = "\n".join(b["text"] for b in blocks if isinstance(b, dict) and b.get("text"))
    text = text.strip()
    if not text:
        raise RuntimeError("empty model response")
    return text


def _ask_model(messages, steps):
    primary = os.environ.get("MODEL_ID", "openai.gpt-oss-120b-1:0")
    fallback = os.environ.get("MODEL_ID_FALLBACK", "amazon.nova-pro-v1:0")
    for label, model_id in (("asked", primary), ("fell back to", fallback)):
        if not model_id:
            continue
        steps.append(f"{label} model {model_id}")
        try:
            return _converse(model_id, messages)
        except Exception as exc:
            log.warning("model %s failed: %s", model_id, exc)
            steps.append(f"model {model_id} failed: {type(exc).__name__}")
            if model_id == fallback:
                break
    return None


# ---------------------------------------------------------------- handler ----

def _handle(event, steps):
    if not _authorized(event):
        return _response(401, {"error": "unauthorized"})
    steps.append("verified token")

    try:
        req = _parse_body(event)
    except Exception:
        return _response(400, {"error": "bad request"})

    if not req["question"]:
        steps.append("question empty; asked for clarification")
        return _response(200, {"clarify": EMPTY_QUESTION_CLARIFY, "steps": steps})

    turns = _load_turns(req["session_id"], steps)
    if req["feedback"]:
        steps.append("received feedback")
    if req["awaiting_clarification"]:
        steps.append("treating question as clarification reply")

    messages = _build_messages(
        turns, req["question"], req["feedback"], req["awaiting_clarification"]
    )
    text = _ask_model(messages, steps)
    if text is None:
        steps.append("all models failed")
        return _response(200, {"answer": UNAVAILABLE_ANSWER, "steps": steps})

    new_turns = turns + [
        {"role": "user", "content": req["question"]},
        {"role": "assistant", "content": text},
    ]
    _save_turns(req["session_id"], new_turns, steps)

    stripped = text.lstrip()
    if stripped.upper().startswith(CLARIFY_PREFIX) and not req["awaiting_clarification"]:
        clarify = stripped[len(CLARIFY_PREFIX):].strip() or EMPTY_QUESTION_CLARIFY
        steps.append("model judged question ambiguous")
        return _response(200, {"clarify": clarify, "steps": steps})

    if stripped.upper().startswith(CLARIFY_PREFIX):
        text = stripped[len(CLARIFY_PREFIX):].strip()
    steps.append("returned answer")
    return _response(200, {"answer": text, "steps": steps})


def handler(event, context):
    steps = []
    try:
        return _handle(event or {}, steps)
    except Exception as exc:
        log.exception("unhandled engine failure")
        steps.append(f"engine failure: {type(exc).__name__}")
        return _response(200, {"answer": UNAVAILABLE_ANSWER, "steps": steps})
