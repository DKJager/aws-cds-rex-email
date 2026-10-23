"""DynamoDB session and feedback management for the "Rex" demo.

Each session is keyed by a channel-sender tuple (e.g. "sms:+12223334444").
The session record stores the last question, last answer, and any pending
feedback text. Feedback is cleared after a successful answer is returned.

Clarification state (issue 6): when the engine returns a clarifying
question, the session is flagged ``awaiting_clarification`` so the next
inbound message from the same sender is forwarded to the engine with
that context marker instead of being treated as a new, independent
question. The flag is cleared once a non-clarify answer turn completes.

Session lifecycle (issue 5 / issue 113 / issue 147 / issue 141): every
session upsert writes an ``expires_at`` attribute (epoch seconds). The
SessionsTable in infra/template.yaml has DynamoDB TTL enabled on
``expires_at``, so idle sessions are deleted automatically and the table
cannot grow without bound during a long-running demo. Each new turn
refreshes ``expires_at``, keeping active conversations alive.

Client configuration (issue 143 in the earlier audit series): the
DynamoDB resource honours ``DYNAMODB_REGION`` and ``DYNAMODB_ENDPOINT``
environment variables so the store can be redirected (e.g. to a local
emulator or an alternate region) without code changes, keeping every
external dependency configurable.

Required environment (issue 145): ``SESSIONS_TABLE`` (table name) and a
DynamoDB region. The region is resolved from ``DYNAMODB_REGION`` first,
then the standard boto3 region variables ``AWS_REGION`` /
``AWS_DEFAULT_REGION``. If no region is available, resource creation
fails immediately with a clear ``RuntimeError`` naming the variables to
set, instead of an opaque boto3 error surfacing lazily on first call.
``DYNAMODB_ENDPOINT`` remains optional.

Session ID validation (issue 146 / issue 144): every public function
validates the ``session_id`` before touching the table. The ID must
match the documented "channel:sender" tuple format (a non-empty channel
token, a colon, and a non-empty sender token), use only allowed
characters, and stay under a length cap. Malformed or foreign-format IDs
raise ``SessionValidationError`` locally instead of becoming guessable
partition keys that could collide with or overwrite other users'
sessions.

Field validation (issue 143): ``last_question``, ``last_answer``, and
``feedback_text`` are type-checked and length-capped before any write,
so untrusted channel input cannot produce oversized items that hit
DynamoDB's 400KB per-item limit at runtime. Over-limit values raise
``SessionValidationError`` with context.

Write error handling (issue 145): every table write is wrapped so a
failed DynamoDB call raises a structured ``SessionStoreError`` carrying
the session ID and operation context, rather than letting an opaque
low-level exception propagate or be misread upstream.

Feedback race guard (issue 144 in the earlier audit series):
``save_feedback`` stamps each feedback with a unique
``feedback_version`` token and clears/reads feedback conditionally on
that token, so concurrent feedback writes are detected instead of
silently overwriting each other. ``clear_feedback`` only removes the
caller's own version and returns the affected attributes so callers can
verify what was cleared.

Feedback existence check (issue 142): ``save_feedback`` is conditional
on ``attribute_exists(session_id)`` so it can never create a bare stub
item containing only ``pending_feedback`` for a session that has no Q&A
record; writing feedback for a nonexistent session raises
``SessionStoreError`` (wrapping ``ConditionalCheckFailedException``)
instead of producing a malformed "session" that ``load_session`` would
later return.

Clear-feedback existence check (issue 148): ``clear_feedback`` is always
conditional on ``attribute_exists(session_id)`` so a caller can
distinguish "no such session / nothing to clear" (a
``SessionStoreError`` caused by ``ConditionalCheckFailedException``)
from a successful clear, instead of DynamoDB silently succeeding on a
REMOVE against a missing item.
"""

import os
import re
import time
import uuid

import boto3
from botocore.exceptions import ClientError

# NOTE (issue 5 / issue 147 / issue 141): rolling TTL window for
# sessions. Idle sessions older than this many seconds become eligible
# for DynamoDB TTL deletion; every write refreshes the expiry.
# Configurable via SESSION_TTL_SECONDS so the window can be tuned per
# deployment without code changes.
DEFAULT_SESSION_TTL_SECONDS = 7 * 24 * 60 * 60  # 7 days

# NOTE (issue 146 / issue 144): session IDs are "<channel>:<sender>"
# tuples. Allow a conservative character set and cap the length well
# below the DynamoDB primary-key attribute limit (~2KB) so invalid keys
# fail fast with a clear local error. The format itself (a channel
# token, one or more colons separating it from a sender token, no empty
# parts) is enforced so caller-controlled strings cannot impersonate or
# collide with another user's key.
SESSION_ID_MAX_LENGTH = 256
_SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9+_.:@-]+$")
_SESSION_ID_FORMAT = re.compile(
    r"^(?P<channel>[A-Za-z0-9_.@-]+):(?P<sender>[A-Za-z0-9+_.:@-]+)$"
)

# NOTE (issue 143): maximum length for any free-text field written to
# the table. Far below DynamoDB's 400KB per-item limit, yet generous
# enough for long email bodies; untrusted channel input longer than
# this is rejected locally instead of failing at the DynamoDB call.
MAX_TEXT_FIELD_LENGTH = 32 * 1024  # 32 KB


class SessionStoreError(RuntimeError):
    """Structured error raised when a DynamoDB session write fails.

    Issue 145: carries the operation name and session ID so callers and
    logs can distinguish a store failure from an engine or channel
    failure without parsing low-level exception text.
    """


class SessionValidationError(ValueError):
    """Raised when a session ID or text field fails validation."""


def _validate_session_id(session_id):
    """Validate a session ID before any table access.

    Issue 146: checks type, non-emptiness, allowed characters, and
    length so DynamoDB's cryptic key-attribute errors are never reached
    for malformed IDs.

    Issue 144: additionally enforces the documented "channel:sender"
    tuple format (non-empty channel and sender parts separated by a
    colon) so arbitrary caller-controlled strings cannot be used as
    partition keys to read or overwrite other users' sessions.
    """
    if not isinstance(session_id, str) or not session_id:
        raise SessionValidationError(
            "session_id must be a non-empty string"
        )
    if len(session_id) > SESSION_ID_MAX_LENGTH:
        raise SessionValidationError(
            "session_id exceeds maximum length of %d characters"
            % SESSION_ID_MAX_LENGTH
        )
    if not _SESSION_ID_PATTERN.match(session_id):
        raise SessionValidationError(
            "session_id contains disallowed characters: %r" % session_id
        )
    if not _SESSION_ID_FORMAT.match(session_id):
        raise SessionValidationError(
            "session_id must be a 'channel:sender' tuple with non-empty "
            "channel and sender parts: %r" % session_id
        )
    return session_id


def _validate_text_field(field_name, value, allow_none=False):
    """Type-check and length-cap a free-text field before any write.

    Issue 143: untrusted channel input flows into ``last_question``,
    ``last_answer``, and ``feedback_text``. Each must be a string (or
    None when ``allow_none``) and at most ``MAX_TEXT_FIELD_LENGTH``
    characters, so oversized payloads are rejected locally instead of
    raising at runtime against DynamoDB's 400KB per-item limit.
    """
    if value is None and allow_none:
        return value
    if not isinstance(value, str):
        raise SessionValidationError(
            "%s must be a string, got %s" % (field_name, type(value).__name__)
        )
    if len(value) > MAX_TEXT_FIELD_LENGTH:
        raise SessionValidationError(
            "%s exceeds maximum length of %d characters"
            % (field_name, MAX_TEXT_FIELD_LENGTH)
        )
    return value


def _write_with_context(operation, session_id, fn):
    """Run a DynamoDB write, raising a structured error on failure.

    Issue 145: wraps the table call so any ClientError is re-raised as a
    ``SessionStoreError`` annotated with the operation and session ID,
    instead of an uncontextualised exception propagating to the caller.
    """
    try:
        return fn()
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        message = exc.response.get("Error", {}).get("Message", "")
        raise SessionStoreError(
            "session store %s failed for session %r: %s: %s"
            % (operation, session_id, code or "UnknownException", message)
        ) from exc


def _resolve_dynamodb_region():
    """Resolve the DynamoDB region from the environment.

    Issue 145: ``DYNAMODB_REGION`` takes precedence; otherwise the
    standard boto3 region variables ``AWS_REGION`` and
    ``AWS_DEFAULT_REGION`` are honoured. If none is set, resource
    creation fails immediately here with a clear ``RuntimeError`` naming
    the variables to set, instead of boto3 raising an opaque
    ``NoRegionError`` lazily on first use.
    """
    region = (
        os.environ.get("DYNAMODB_REGION")
        or os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
    )
    if not region:
        raise RuntimeError(
            "No DynamoDB region configured. Set DYNAMODB_REGION "
            "(preferred) or the standard AWS_REGION/AWS_DEFAULT_REGION "
            "environment variable alongside SESSIONS_TABLE."
        )
    return region


def _get_dynamodb_resource():
    """Resolve the DynamoDB resource on every call.

    Issue 157: the resource is built per call (no module-level cache),
    so later patched or freshly configured resources are honoured
    instead of a stale resource created for an earlier request being
    reused.

    The ``region_name`` and ``endpoint_url`` are sourced from the
    ``DYNAMODB_REGION`` (or ``AWS_REGION``/``AWS_DEFAULT_REGION``) and
    ``DYNAMODB_ENDPOINT`` environment variables so the store endpoint is
    configurable (e.g. a local emulator for demos) rather than hardcoded
    to the default AWS endpoint.

    Issue 145: the region is resolved explicitly and the resource
    creation fails fast with a clear error when no region is configured,
    so a misconfigured deployment surfaces immediately with the names of
    the environment variables to set, not as a lazy boto3 failure on
    first call.
    """
    kwargs = {"region_name": _resolve_dynamodb_region()}
    endpoint = os.environ.get("DYNAMODB_ENDPOINT")
    if endpoint:
        kwargs["endpoint_url"] = endpoint
    return boto3.resource("dynamodb", **kwargs)


def _table():
    table_name = os.environ.get("SESSIONS_TABLE")
    if not table_name:
        raise RuntimeError("SESSIONS_TABLE environment variable is required")
    return _get_dynamodb_resource().Table(table_name)


def _session_ttl_seconds():
    raw = os.environ.get("SESSION_TTL_SECONDS")
    if not raw:
        return DEFAULT_SESSION_TTL_SECONDS
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_SESSION_TTL_SECONDS
    return value if value > 0 else DEFAULT_SESSION_TTL_SECONDS


def _expires_at():
    """Epoch seconds at which the current session write should expire."""
    return int(time.time()) + _session_ttl_seconds()


def load_session(session_id):
    """Load a session record by session ID.

    Returns the item dict or None if not found.

    Issue 145 / issue 140 / issue 149: only ResourceNotFoundException /
    ValidationException ClientErrors (the "not found / bad request"
    cases) are treated as a missing session; throttling
    (``ProvisionedThroughputExceededException``), access-denied, network,
    and other transient or configuration errors propagate so outages are
    never silently misread as an empty session (which would cause
    duplicate items or lost state upstream).
    """
    _validate_session_id(session_id)
    try:
        response = _table().get_item(Key={"session_id": session_id})
        return response.get("Item")
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("ResourceNotFoundException", "ValidationException"):
            return None
        raise


def save_session(session_id, last_question, last_answer,
                 awaiting_clarification=False, steps=None):
    """Save or update a session record with the latest Q&A.

    Writes a rolling ``expires_at`` (issue 5 / issue 147 / issue 141) so
    the DynamoDB TTL on the table eventually reclaims idle sessions
    instead of retaining them forever.

    ``awaiting_clarification`` (issue 6) records whether the last reply
    was an engine clarifying question, so the next inbound turn is
    correlated with the pending clarification instead of treated as an
    independent question.

    Issue 144 / issue 139: uses ``update_item`` (not ``put_item``) so a
    previously recorded ``pending_feedback`` attribute survives the
    normal Q&A save and is not silently destroyed before it can be
    forwarded.

    Issue 143: ``last_question`` and ``last_answer`` are type-checked
    and length-capped before the write so oversized channel input is
    rejected locally.

    Issue 145: the write is wrapped so a DynamoDB failure raises a
    structured ``SessionStoreError`` with operation and session context.
    """
    _validate_session_id(session_id)
    _validate_text_field("last_question", last_question)
    _validate_text_field("last_answer", last_answer)

    # One record per conversation: turn counter, channel, last update time and
    # (optionally) the structured agent steps of the latest turn.
    expression = (
        "SET last_question = :question, last_answer = :answer, "
        "awaiting_clarification = :awaiting, expires_at = :expires, "
        "updated_at = :now, conv_channel = :chan, "
        "turn_count = if_not_exists(turn_count, :zero) + :one"
    )
    values = {
        ":question": last_question,
        ":answer": last_answer,
        ":awaiting": bool(awaiting_clarification),
        ":expires": _expires_at(),
        ":now": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        ":chan": session_id.split(":", 1)[0],
        ":zero": 0,
        ":one": 1,
    }
    if steps is not None:
        _validate_text_field("steps", steps)
        expression += ", last_steps = :steps"
        values[":steps"] = steps

    def _do_update():
        return _table().update_item(
            Key={"session_id": session_id},
            UpdateExpression=expression,
            ExpressionAttributeValues=values,
        )

    return _write_with_context("save_session", session_id, _do_update)


def save_feedback(session_id, feedback_text, expected_version=None):
    """Record feedback text for forwarding on the next engine call.

    Also refreshes ``expires_at`` (issue 5 / issue 147 / issue 141) so a
    session with pending feedback is not TTL-deleted before the feedback
    is forwarded.

    Issue 142: the write is conditional on
    ``attribute_exists(session_id)``, so it can never create a bare stub
    item holding only ``pending_feedback`` for a session with no Q&A
    record. Saving feedback for a nonexistent session raises
    ``SessionStoreError`` (wrapping ``ConditionalCheckFailedException``)
    instead of producing a malformed "session" that ``load_session``
    would later return.

    Issue 143: ``feedback_text`` is type-checked and length-capped
    before the write so oversized channel input is rejected locally.

    Issue 144 (earlier audit series): each feedback write is stamped
    with a unique ``feedback_version`` token and is additionally
    conditional on the existing version matching ``expected_version``
    (``None`` means "only if the session exists"; pass the version read
    from ``load_session`` to enforce compare-and-swap). If the condition
    fails, a ``SessionStoreError`` is raised instead of silently
    overwriting a concurrent feedback. Returns the written attributes
    (including ``feedback_version``) so the caller can verify and later
    clear exactly its own feedback.
    """
    _validate_session_id(session_id)
    _validate_text_field("feedback_text", feedback_text)
    new_version = uuid.uuid4().hex

    def _do_update():
        values = {
            ":feedback": feedback_text,
            ":version": new_version,
            ":expires": _expires_at(),
        }
        condition = "attribute_exists(session_id)"
        if expected_version is not None:
            condition += " AND feedback_version = :expected"
            values[":expected"] = expected_version
        return _table().update_item(
            Key={"session_id": session_id},
            UpdateExpression=(
                "SET pending_feedback = :feedback, "
                "feedback_version = :version, expires_at = :expires"
            ),
            ConditionExpression=condition,
            ExpressionAttributeValues=values,
            ReturnValues="ALL_NEW",
        )

    return _write_with_context("save_feedback", session_id, _do_update)


def clear_feedback(session_id, expected_version=None):
    """Clear any recorded feedback after it has been forwarded.

    Issue 144 (earlier audit series): when ``expected_version`` is
    provided, the REMOVE is conditional on ``feedback_version``
    matching, so a caller can never clear a newer concurrent feedback
    entry by mistake; a version mismatch raises ``SessionStoreError``.
    Returns the post-update attributes so callers can verify the clear
    actually applied to their own feedback rather than a stale or
    foreign entry.

    Issue 148: the REMOVE is always conditional on
    ``attribute_exists(session_id)``, so clearing feedback on a
    nonexistent session raises ``SessionStoreError`` (wrapping
    ``ConditionalCheckFailedException``) instead of silently succeeding.
    Callers can therefore distinguish "no such session / nothing to
    clear" from a successful clear.

    Issue 145: the write is wrapped so a DynamoDB failure raises a
    structured ``SessionStoreError`` with operation and session context.
    """
    _validate_session_id(session_id)

    def _do_update():
        kwargs = {
            "Key": {"session_id": session_id},
            "UpdateExpression": (
                "REMOVE pending_feedback, feedback_version"
            ),
            "ReturnValues": "ALL_NEW",
        }
        if expected_version is not None:
            kwargs["ConditionExpression"] = (
                "attribute_exists(session_id) AND "
                "feedback_version = :expected"
            )
            kwargs["ExpressionAttributeValues"] = {
                ":expected": expected_version,
            }
        else:
            kwargs["ConditionExpression"] = "attribute_exists(session_id)"
        return _table().update_item(**kwargs)

    return _write_with_context("clear_feedback", session_id, _do_update)
