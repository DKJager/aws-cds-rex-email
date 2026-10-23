"""Lambda orchestrator for the "Rex" thin-wrapper agentic demo.

Flow: parse inbound event -> load/create session -> detect feedback ->
call the external Nexus engine via nexus_client -> store session ->
reply on the same channel via channels (pinpoint-sms-voice-v2 or sesv2).

The engine is only ever called as an external HTTPS service. This module
contains no engine logic, prompts, model/provider names, or credentials.

Clarification correlation (issue 6): when the engine returns a
clarifying question, the session is flagged ``awaiting_clarification``
and the flag is persisted with the session. The next inbound message
from the same sender is then forwarded to the engine with that context
marker (``awaiting_clarification=True``) so the engine can correlate it
with the pending clarification, instead of treating it as a new,
independent question. The flag is cleared once a non-clarify answer
turn completes.

Consent guard: inbound messages from ANY sender (including judge
addresses/numbers not yet allowlisted) are always accepted and processed
here. The ALLOWED_DESTINATIONS allowlist gates only the outbound send
inside channels.send_reply; it is owner-updated at submission time (via
the Lambda environment, no code change) to include the judge
destinations (judges@cds.3dogs.ai and the judges' SMS number), so
replies to judges pass the guard.

ALLOWED_DESTINATIONS format (issue 7): the variable is a JSON array of
strings, e.g. ["+15551234567", "judges@cds.3dogs.ai"]. Each entry is an
E.164 phone number (SMS destinations) or a verified email address
(email destinations). Matching is exact (case-insensitive for email);
no regex or wildcards. An unset variable or an empty array "[]" refuses
all outbound sends. The parse and enforcement live in src/channels.py.

Return-code policy (issue 141): this Lambda is invoked by SES/SNS event
sources, so a non-200 return has no effect on the caller but may trigger
notification retries, causing duplicate engine calls and duplicate
replies for the same inbound message. Every path therefore returns 200;
failures are surfaced through logging only.
"""

import json
import logging
import os
import re
import time

try:  # package layout (tests, repo root on sys.path)
    from src.nexus_client import urlopen
    from src import channels, nexus_client, sessions
except ImportError:  # Lambda layout: CodeUri src/ puts these modules at the zip root
    from nexus_client import urlopen
    import channels, nexus_client, sessions

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Common reply/forward prefixes (issue 138): email clients and SMS
# threads frequently prepend "Re:", "Fwd:", or similar markers (plus
# leading quotes/emoji) before the user's actual text. These are
# stripped before feedback detection so a wrapped correction such as
# "Re: that's wrong" is still recognised as feedback instead of being
# forwarded to the engine as a fresh question.
REPLY_PREFIX_PATTERN = re.compile(
    r"^(?:\s*(?:re|fw|fwd|aw|sv)\s*:\s*)+",
    re.IGNORECASE,
)

# Feedback detection (issue 138): match the marker phrases as whole
# words, not arbitrary substrings, and search anywhere in the text (not
# only at the start), so a leading emoji, quote, or stripped reply
# prefix cannot defeat detection. A plain substring match would
# misclassify legitimate questions such as "Incorrect PIN attempts on
# my account" or "incorrect billing charge?" as feedback and silently
# store them. The word-boundary regex only fires on the standalone
# correction phrases ("that's wrong", "that is wrong", "this is
# incorrect", ...), so a question merely containing the word "incorrect"
# inside a longer word or unrelated context is still routed to the
# engine as a question.
FEEDBACK_PATTERN = re.compile(
    r"\b(?:that(?:'s| is| was)|this(?:'s| is| was)|it(?:'s| is| was)) wrong\b"
    r"|\bthat(?:'s| is| was)?\s*(?:not right|incorrect|not correct)\b"
    r"|\bthis is incorrect\b",
    re.IGNORECASE,
)

# Engine-failure short-circuit window in seconds (issue 141): after an
# engine 5xx/unreachable failure the session records the failure epoch;
# further inbound turns within this window skip the engine call and go
# straight to the apology reply. SES/SNS retries of the same delivery
# therefore cannot cause a tight retry storm against the engine.
ENGINE_FAILURE_BACKOFF_SECONDS = 60

FALLBACK_REPLY = (
    "Sorry, the answer service returned an unexpected response. "
    "Please try again."
)


def _strip_reply_prefixes(text):
    """Remove leading reply/forward markers and surrounding quotes.

    Issue 138: corrections arriving as "Re: that's wrong", "> that's
    wrong", or with a leading emoji/quote must still be detected. We
    iteratively strip "Re:"/"Fwd:"-style prefixes and leading quote or
    decorative characters, then the word-boundary search in
    FEEDBACK_PATTERN finds the marker wherever it appears.
    """
    stripped = text or ""
    previous = None
    while previous != stripped:
        previous = stripped
        stripped = REPLY_PREFIX_PATTERN.sub("", stripped)
        stripped = stripped.lstrip(">\"' \t")
    return stripped


def _is_feedback(text):
    """Word-boundary match (issue 138/140): catch "that's wrong, you
    said X" and "no, that's wrong", including when wrapped in reply
    prefixes ("Re: that's wrong") or leading emoji/quotes, without
    misclassifying questions that merely mention the word "incorrect"
    (e.g. "Incorrect PIN attempts...")."""
    if not text:
        return False
    if FEEDBACK_PATTERN.search(text):
        return True
    stripped = _strip_reply_prefixes(text)
    if stripped != text and FEEDBACK_PATTERN.search(stripped):
        return True
    return False


def _normalize(text):
    return " ".join((text or "").strip().lower().split())


def _pack_feedback(question, text):
    return json.dumps({"question": question, "text": text})


def _session_id_for(message):
    """Session key is channel:sender (tests and spec require this exact format)."""
    return "%s:%s" % (message.channel, message.sender)


def _feedback_for(session, question):
    """Return the pending feedback text for the next engine call (issue 172); it is cleared after a successful answer."""
    if not session:
        return None
    raw = session.get("pending_feedback")
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return raw
    if not isinstance(payload, dict):
        return None
    return payload.get("text")


def _safe_load_session(session_id):
    """Guard the session store like the engine call (issue 142).

    A session-store failure (e.g. DynamoDB throttling) must not abort the
    handler before the engine call; degrade to a fresh, empty session.
    A ``None`` or non-dict return from the store (first contact for a
    new sender) is also coerced to ``{}`` so every downstream ``.get``
    is always safe.
    """
    try:
        session = sessions.load_session(session_id)
    except Exception as exc:  # noqa: BLE001 - store boundary guard
        logger.error("session load failed for %s: %s", session_id, exc)
        return {}
    if not isinstance(session, dict):
        return {}
    return session


def _steps_enabled():
    return os.environ.get("AGENT_STEPS", "0") == "1"


_AUTOMATED_LOCALPARTS = {"mailer-daemon", "postmaster", "no-reply", "noreply", "donotreply", "do-not-reply"}


def _is_own_or_automated(message):
    """Mail-loop guard: never answer auto-submitted mail, our own signed replies, or a robot sender."""
    if getattr(message, "channel", None) != "email":
        return False
    if getattr(message, "auto_submitted", False):
        return True
    # Our own replies end with the signature line; an inbound message carrying it is our reply
    # coming back (e.g. mail addressed to our own mailbox), so answering it would loop forever.
    signature = "-- " + str(getattr(channels, "REX_SIGNATURE", "\0"))
    if any(line.strip() == signature for line in (message.text or "").splitlines()):
        return True
    for addr in (getattr(message, "sender", None), getattr(message, "reply_target", None)):
        addr = (addr or "").strip().lower()
        if not addr:
            continue
        if addr.split("@")[0] in _AUTOMATED_LOCALPARTS or addr.endswith("amazonses.com"):
            return True
    return False


def _step(steps, name, detail):
    steps.append({"step": name, "detail": detail})


def _sanitize_step_text(text):
    """Remove control characters so the step text is safe for plain-text email."""
    if not isinstance(text, str):
        return ""
    # Remove ASCII control characters except space, \n, \r, \t
    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    return cleaned


def _with_steps(message, reply_text, steps):
    """Append the visible agent-steps block to an email reply when enabled."""
    if message.channel != "email" or not _steps_enabled() or not steps:
        return reply_text
    lines = ["", "Agent steps:"]
    for i, s in enumerate(steps, 1):
        lines.append("  %d. %s: %s" % (i, s["step"], s["detail"]))
    return reply_text + "\n" + "\n".join(lines)


def _safe_save_session(session_id, question, reply_text,
                       awaiting_clarification=False, steps=None):
    """Best-effort session save; a store failure must not mask the turn.

    ``awaiting_clarification`` (issue 6) persists the clarify state so a
    later turn on the same session is correlated with the pending
    clarification.
    """
    try:
        extra = {"steps": json.dumps(steps)} if (steps and _steps_enabled()) else {}
        sessions.save_session(
            session_id,
            question,
            reply_text,
            awaiting_clarification=awaiting_clarification,
            **extra
        )
        return True
    except Exception as exc:  # noqa: BLE001 - store boundary guard
        logger.error("session save failed for %s: %s", session_id, exc)
        return False


def _engine_in_backoff(session):
    """True when a recent engine failure short-circuits new calls (141)."""
    if not isinstance(session, dict):
        return False
    failed_at = session.get("engine_failed_at")
    if not failed_at:
        return False
    try:
        failed_at = float(failed_at)
    except (TypeError, ValueError):
        return False
    return (time.time() - failed_at) < ENGINE_FAILURE_BACKOFF_SECONDS


def _mark_engine_failure(session_id):
    """Record an engine failure epoch for the backoff window (issue 141).

    Best-effort: if the store does not expose a dedicated field writer,
    fall back to a plain session save marker via save_engine_marker when
    available; failures here must never mask the turn.
    """
    try:
        marker = getattr(sessions, "save_engine_marker", None)
        if callable(marker):
            marker(session_id, "engine_failed_at", str(time.time()))
    except Exception as exc:  # noqa: BLE001 - store boundary guard
        logger.error("engine-failure marker save failed for %s: %s", session_id, exc)


def _clear_engine_failure(session_id):
    """Clear the backoff marker after a successful engine call (141)."""
    try:
        clearer = getattr(sessions, "clear_engine_marker", None)
        if callable(clearer):
            clearer(session_id, "engine_failed_at")
    except Exception as exc:  # noqa: BLE001 - store boundary guard
        logger.error("engine-failure marker clear failed for %s: %s", session_id, exc)


def _clear_feedback_if_unchanged(session_id, expected_raw):
    """Clear pending feedback only if it still holds the value forwarded.

    Concurrent-delivery guard (issue 136): two deliveries for the same
    session_id can interleave; an unconditional clear could erase a
    newer correction recorded by a concurrent turn before it is ever
    forwarded. The session is re-read and the clear is skipped when the
    stored payload no longer matches the value this turn forwarded, so
    a concurrently recorded correction always survives. Where the
    session store exposes a conditional write
    (sessions.clear_feedback_conditional), that atomic compare-and-clear
    is preferred over the read-then-clear fallback.
    """
    try:
        conditional = getattr(sessions, "clear_feedback_conditional", None)
        if callable(conditional):
            conditional(session_id, expected_raw)
            return True
        current = sessions.load_session(session_id)
        if not isinstance(current, dict):
            return False
        if current.get("pending_feedback") != expected_raw:
            logger.info(
                "pending feedback changed concurrently for %s; not clearing",
                session_id,
            )
            return False
        sessions.clear_feedback(session_id)
        return True
    except Exception as exc:  # noqa: BLE001 - store boundary guard
        logger.error("feedback clear failed for %s: %s", session_id, exc)
        return False


def _is_conditional_check_failed_exception(exc):
    """Return True when *exc* (or its cause) represents a DynamoDB
    ConditionalCheckFailedException, in whatever form sessions raises it.

    We accept:
      - A botocore ClientError whose Error Code is
        ``ConditionalCheckFailedException``.
      - The dedicated wrapper exception ``sessions.ConditionCheckFailedError``
        if it exists.
    """
    # Check the wrapper exception type first.
    wrapper_cls = getattr(sessions, "ConditionCheckFailedError", None)
    if wrapper_cls is not None and isinstance(exc, wrapper_cls):
        return True
    # Check for a botocore ClientError.
    client_error_cls = None
    try:
        from botocore.exceptions import ClientError
        client_error_cls = ClientError
    except ImportError:
        pass
    if client_error_cls is not None:
        if isinstance(exc, client_error_cls):
            code = (exc.response or {}).get("Error", {}).get("Code", "")
            if code == "ConditionalCheckFailedException":
                return True
        # Also walk the exception chain via __cause__.
        cause = exc.__cause__
        while cause is not None:
            if isinstance(cause, client_error_cls):
                code = (cause.response or {}).get("Error", {}).get("Code", "")
                if code == "ConditionalCheckFailedException":
                    return True
            cause = cause.__cause__
    return False


def _safe_send_reply(message, reply_text):
    """Best-effort outbound send; never raise to the caller (issue 138).

    A failed channel send is logged with the session context so the
    turn can be retried externally, while the Lambda still returns a
    controlled result instead of surfacing a raw exception.
    """
    try:
        channels.send_reply(message, reply_text)
        return True
    except Exception as exc:  # noqa: BLE001 - deliberate broad guard at the boundary
        logger.error(
            "outbound send failed for %s:%s: %s",
            message.channel,
            message.sender,
            exc,
        )
        return False


def handler(event, context):
    """Lambda entry point for SES inbound email and SNS inbound SMS events.

    Always returns 200 (issue 141): for SES/SNS event-source invocations
    a non-200 status cannot reach the caller and may trigger notification
    retries, duplicating engine calls and replies. Failures are logged.
    """
    message = channels.parse_inbound_event(event)
    if message is None:
        logger.info("no supported inbound payload in event; ignoring")
        return {"statusCode": 200, "body": "ignored"}

    if _is_own_or_automated(message):
        logger.info("ignoring auto-submitted or automated mail (loop guard)")
        return {"statusCode": 200, "body": "ignored"}

    steps = []
    _step(steps, "received", "%s message, %d characters" % (message.channel, len(message.text or "")))
    session_id = _session_id_for(message)
    session = _safe_load_session(session_id)
    earlier = 0
    try:
        earlier = int((session or {}).get("turn_count") or 0)
    except (TypeError, ValueError):
        earlier = 0
    _step(steps, "session", ("returning conversation, %d earlier turn%s" % (earlier, "" if earlier == 1 else "s"))
          if earlier else "new conversation")

    feedback_text = _feedback_for(session, message.text)
    feedback_raw = (session or {}).get("pending_feedback") if feedback_text else None

    if _is_feedback(message.text):
        # Record the correction together with the question it was about;
        # it is forwarded with the next engine call whatever the wording
        # of the next question is (issue 172); the wrapper only stores
        # state (engine-side improvement).
        prior_question = (session or {}).get("last_question") or ""
        # [MAJOR fix #1475] Feedback guard retries save_session after ANY
        # exception can clobber an existing session.  Only the missing-session
        # condition (ConditionalCheckFailedException, in whatever form
        # sessions.save_feedback surfaces it) triggers session creation and
        # one retry.  Every other exception is logged and swallowed; no
        # save_session call and no record overwrite.
        try:
            sessions.save_feedback(
                session_id, _pack_feedback(prior_question, message.text)
            )
        except Exception as exc:
            if _is_conditional_check_failed_exception(exc):
                logger.info(
                    "save_feedback failed with ConditionalCheckFailed for %s; "
                    "creating session and retrying once",
                    session_id,
                )
                try:
                    # Create the session record so the feedback can be stored.
                    sessions.save_session(
                        session_id, prior_question, "", awaiting_clarification=False
                    )
                    sessions.save_feedback(
                        session_id,
                        _pack_feedback(prior_question, message.text),
                    )
                except Exception as exc2:
                    logger.error(
                        "save_feedback retry still failed for %s: %s",
                        session_id, exc2,
                    )
                    # Logged and swallowed – handler still returns 200.
            else:
                # Any other exception: throttling, internal error, etc.
                # MUST NOT call save_session – no record overwrite.
                logger.error(
                    "save_feedback failed for %s with non-conditional error: %s",
                    session_id, exc,
                )
                # Swallowed; acknowledgement reply is still sent.

        reply = "Thanks - noted. Please ask your question again and I'll do better."
        if not _safe_send_reply(message, reply):
            logger.error("feedback acknowledgement send failed for %s", session_id)
        return {"statusCode": 200, "body": "feedback recorded"}

    # Input validation (issue 140): an empty or whitespace-only payload
    # that is not feedback must never consume an engine call. Reply with
    # guidance and exit before nexus_client is touched, so no billing
    # call is burned and no engine error detail can leak to the user.
    if not (message.text or "").strip():
        logger.info("empty inbound text for %s; not calling engine", session_id)
        _safe_send_reply(
            message,
            "I didn't receive any text in your message. "
            "Please send your question as plain text.",
        )
        return {"statusCode": 200, "body": "empty input"}

    # Engine-failure backoff (issue 141): if the engine returned a 5xx
    # within the backoff window, skip the call entirely and reply with
    # the apology. Retried SES/SNS deliveries therefore short-circuit
    # here instead of hammering the engine in a tight retry storm.
    if _engine_in_backoff(session):
        logger.info(
            "engine in backoff window for %s; short-circuiting", session_id
        )
        _safe_send_reply(
            message, "Sorry, I couldn't reach the answer service just now."
        )
        return {"statusCode": 200, "body": "engine backoff"}

    # Clarification correlation (issue 6): if the previous turn was a
    # clarifying question, flag this engine call with that context so
    # the engine treats the reply as the answer to its clarification
    # rather than as a new, independent question.
    awaiting_clarification = bool((session or {}).get("awaiting_clarification"))
    _step(steps, "feedback", "stored correction forwarded with the question" if feedback_text
          else "no pending correction")
    engine_started = time.monotonic()

    previous_urlopen = getattr(nexus_client, "urlopen", None)
    nexus_client.urlopen = globals()["urlopen"]
    try:
        result = nexus_client.query_engine(
            question=message.text,
            session_id=session_id,
            feedback=feedback_text,
            awaiting_clarification=awaiting_clarification,
        )
    except nexus_client.NexusCallError as exc:
        logger.error("engine call failed: %s", exc)
        _mark_engine_failure(session_id)
        _safe_send_reply(
            message, "Sorry, I couldn't reach the answer service just now."
        )
        return {"statusCode": 200, "body": "engine unavailable"}
    except Exception as exc:  # noqa: BLE001 - boundary guard (issue 135)
        # NexusCallError is the expected failure type, but a raw network
        # exception (timeout, connection error, HTTP error) leaking from
        # the client must not surface as an unhandled 5xx. Coerce every
        # engine-path failure into the same controlled reply so the user
        # always gets a friendly answer and the handler still returns 200
        # (issue 141: no notification retries / duplicate replies).
        logger.error("engine call failed with unexpected error: %s", exc)
        _mark_engine_failure(session_id)
        _safe_send_reply(
            message, "Sorry, I couldn't reach the answer service just now."
        )
        return {"statusCode": 200, "body": "engine unavailable"}
    finally:
        nexus_client.urlopen = previous_urlopen

    if not isinstance(result, dict):
        logger.error("engine returned non-dict payload: %r", type(result))
        result = {}

    _clear_engine_failure(session_id)
    _step(steps, "engine", "%s in %d ms" % ("clarifying question" if result.get("clarify") else "answer",
                                         int((time.monotonic() - engine_started) * 1000)))

    # Merge engine-reported steps into the reply-visible steps block so
    # the agentic loop is visible in the judge's inbox.
    # The engine (engine/app.py) returns steps as a LIST OF STRINGS,
    # e.g. ["loaded 2 prior turns", "asked model m1"].
    # Each non-empty str is appended as a step named "engine" whose detail
    # is the text with control characters removed and truncated to 120
    # characters; at most 6 of them; anything that is not a str is ignored;
    # a missing/non-list `steps` changes nothing.
    engine_steps = result.get("steps")
    if isinstance(engine_steps, list):
        appended = 0
        for s in engine_steps:
            if appended >= 6:
                break
            if not isinstance(s, str) or not s.strip():
                continue
            detail = _sanitize_step_text(s.strip())[:120]
            if detail:
                steps.append({"step": "engine", "detail": detail})
                appended += 1

    _step(steps, "saved", "conversation %s updated" % session_id)

    # Per-branch feedback lifecycle (issue 143): feedback is cleared only
    # on a successful answer. On a clarify turn the pending feedback is
    # deliberately left untouched so the correction survives the clarify
    # loop and is forwarded again when the same question is next asked;
    # the transcript turn is logged as a clarify so pending_feedback and
    # the last-turn transcript cannot silently drift.
    if result.get("clarify"):
        reply_text = result["clarify"]
        logger.info(
            "clarify turn for %s; pending feedback retained: %s",
            session_id,
            bool(feedback_text),
        )
        # Issue 6: flag the session as awaiting clarification so the
        # next inbound message is forwarded to the engine with the
        # awaiting_clarification context marker, not treated as a new
        # independent question.
        _safe_save_session(
            session_id, message.text, reply_text, awaiting_clarification=True,
            steps=steps,
        )
    else:
        reply_text = result.get("answer")
        if not reply_text:
            logger.error("engine payload missing both 'clarify' and 'answer'")
            reply_text = FALLBACK_REPLY
            # Fallback is not a successful answer: keep pending feedback
            # so the correction is forwarded on the next matching turn.
            _safe_save_session(session_id, message.text, reply_text, steps=steps)
        else:
            if feedback_text:
                # A successful answer consumes any pending feedback. The
                # clear is conditional on the forwarded value (issue 136)
                # so a concurrently recorded newer correction survives.
                _clear_feedback_if_unchanged(session_id, feedback_raw)
            # Issue 6: a completed (non-clarify) answer turn clears the
            # awaiting_clarification flag by saving without it.
            _safe_save_session(session_id, message.text, reply_text, steps=steps)

    # Reply is addressed to the inbound sender on the same channel; the
    # outbound allowlist (ALLOWED_DESTINATIONS, a JSON array of E.164
    # numbers and verified email addresses) is enforced in channels and
    # is owner-updated at submission time for judge destinations.
    _step(steps, "reply", "sent to the sender on the same channel")
    if not _safe_send_reply(message, _with_steps(message, reply_text, steps)):
        logger.error("reply send failed for %s", session_id)
        return {"statusCode": 200, "body": "reply send failed"}
    return {"statusCode": 200, "body": "ok"}
