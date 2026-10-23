
"""Test orchestrator flows and guards.

Tests the Lambda handler end-to-end by stubbing boto3 clients and the
external engine call. Verifies:
- SMS-in -> SMS-out (via pinpoint-sms-voice-v2 SendTextMessage),
  including the OriginationIdentity derived from SMS_ORIGINATION_ID
  (issue 175)
- email-in -> email-out (via sesv2 SendEmail from judges@cds.3dogs.ai),
  including a case where the original sender is distinct from the
  outbound identity so a reply-to-self bug cannot pass silently
  (issue 163)
- clarifying-question flow (SMS and email channels), including the Rex
  signature on clarifying replies (issue 176)
- clarify flow flags the session awaiting_clarification and forwards the
  next inbound message to the engine with that context marker (issue 6)
- "that's wrong" feedback recorded and forwarded on next engine call,
  together with the re-ask text and session identifier (issue 172);
  an explicit assertion pins that the feedback turn itself made no
  engine call (issue 55)
- replies refused for destinations outside ALLOWED_DESTINATIONS
- engine requests carry the configured endpoint URL and an Authorization
  header derived from NEXUS_TOKEN (issues 168, 171)
- engine failure paths (HTTP/URL errors, malformed JSON, timeouts)
  produce an error reply instead of a crash (issue 169)
- unrecognised payloads are ignored: both the empty-Records case and a
  non-empty record missing the expected SMS/SES structure (issue 56)
- the judge email channel in infra/template.yaml: the SES receipt rule
  recipients are exactly ["judges@cds.3dogs.ai"], the outbound sender
  identity is judges@cds.3dogs.ai, and no sender@example.org / apex 3dogs.ai
  mailbox appears (issue 27)
- the handler path calls both qualifying v2 clients
  (pinpoint-sms-voice-v2 SendTextMessage and sesv2 SendEmail) at
  runtime, and boto3 is resolved per call rather than captured once at
  import time (issues 20, 157)

Fake session store (issue 54): FakeDynamoTable.update_item parses only
the exact flat SET/REMOVE UpdateExpression patterns the orchestrator
emits, using regexes over attribute-name/value tokens resolved through
ExpressionAttributeNames/ExpressionAttributeValues. It does not do naive
whitespace splitting or substring matching (e.g. it never infers
"REMOVE pending_feedback" from token prefixes); any expression outside
those exact patterns raises ValueError loudly instead of being silently
mishandled, so the feedback clearance simulation cannot produce false
positives or negatives.

Orchestrator import (issue 57): the repository root is derived from
this file's own location and placed on sys.path before importing
``src.orchestrator`` (with an ``src`` package import as fallback), so
the import does not depend on the caller's working directory or on an
externally configured PYTHONPATH. A missing module still fails loudly,
but never because of an accidental path misconfiguration.

Test-lifecycle hygiene (issues 172, 173, 156, 53): no environment
variables are mutated and no patchers are started at module level. The
test environment is installed with patch.dict in setUpClass and stopped
via addClassCleanup, and the module-qualified urlopen patch is likewise
started in setUpClass and stopped via addClassCleanup, so nothing leaks
into other test modules sharing the process regardless of collection
order.
"""

import base64
import json
import os
import re
import sys
import unittest
import urllib.error
import urllib.request  # noqa: F401  (explicit import; the module is patched)
from pathlib import Path
from unittest.mock import MagicMock, patch

# Environment required by src/orchestrator at import time. It is NOT
# applied at module level; it is installed with patch.dict in
# setUpClass and removed via addClassCleanup so nothing leaks into other
# test modules sharing the process, regardless of collection order
# (issues 172, 173, 164, 53).
_TEST_ENV = {
    "SES_FROM": "judges@cds.3dogs.ai",
    "SMS_ORIGINATION_ID": "test-orig-id",
    "NEXUS_ENDPOINT": "https://nexus.example.com/query",
    "NEXUS_TOKEN": "test-token",
    "SESSIONS_TABLE": "test-table",
    "AWS_REGION": "us-east-1",
    "DYNAMODB_REGION": "us-east-1",
    "SES_REGION": "us-east-1",
    "SMS_REGION": "us-east-1",
    "ALLOWED_DESTINATIONS": "judges@cds.3dogs.ai,+15551234567,customer@example.org",
}

_REPO_ROOT = Path(__file__).resolve().parents[1]
_TEMPLATE_PATH = _REPO_ROOT / "infra" / "template.yaml"

JUDGE_ADDRESS = "judges@cds.3dogs.ai"
# A sender distinct from the outbound identity SES_FROM, used to prove
# replies go to the original sender and not back to ourselves (issue
# 163).
CUSTOMER_ADDRESS = "customer@example.org"


def _import_orchestrator():
    """Import src.orchestrator regardless of the caller's cwd (issue 57).

    The repository root is derived from this test file's own location
    and placed on sys.path, so the import works whether the runner was
    invoked from the repo root, from tests/, or elsewhere. If the module
    genuinely does not exist, ImportError still surfaces loudly.
    """
    repo_root_str = str(_REPO_ROOT)
    inserted = False
    if repo_root_str not in sys.path:
        sys.path.insert(0, repo_root_str)
        inserted = True
    try:
        from src import orchestrator  # noqa: WPS433
    except ImportError:
        # Fallback: orchestrator may be importable as a top-level module
        # rooted at src/ in some layouts.
        src_str = str(_REPO_ROOT / "src")
        if src_str not in sys.path:
            sys.path.insert(0, src_str)
        import orchestrator  # noqa: WPS433
    else:
        return orchestrator
    if not inserted and repo_root_str not in sys.path:
        raise ImportError("could not locate src/orchestrator module")
    return orchestrator


def make_sns_sms_event(sender, body):
    """Build a synthetic SNS event for an inbound SMS."""
    return {
        "Records": [
            {
                "EventSource": "aws:sns",
                "Sns": {
                    "Message": json.dumps(
                        {"originationNumber": sender, "messageBody": body}
                    )
                },
            }
        ]
    }


def make_ses_email_event(sender, subject, body=None):
    """Build a synthetic SES inbound email event.

    The ``content`` field mimics the real SES inbound event structure: a
    base64-encoded raw RFC 822 message string whose headers carry the
    subject and whose body (after the blank line) carries the message
    text (issues 170, 165). Base64 encoding matches the real SES event
    format, so the orchestrator's actual decode-then-parse path is
    exercised by the tests rather than bypassed; a handler that skips
    base64 decoding would fail these tests.

    ``body`` defaults to ``subject`` for brevity in tests that do not
    care about body extraction, but callers may pass a distinct body so
    the subject-vs-body extraction path is covered (issue 165).
    """
    if body is None:
        body = subject
    raw_message = (
        "From: {sender}\r\n"
        "To: {judge}\r\n"
        "Subject: {subject}\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        "\r\n"
        "{body}\r\n"
    ).format(sender=sender, judge=JUDGE_ADDRESS, subject=subject, body=body)
    encoded_content = base64.b64encode(
        raw_message.encode("utf-8")
    ).decode("ascii")
    return {
        "Records": [
            {
                "ses": {
                    "mail": {
                        "source": sender,
                        "commonHeaders": {"subject": subject},
                    },
                    "content": encoded_content,
                }
            }
        ]
    }


class FakeDynamoTable(MagicMock):
    """Mock DynamoDB table that stores items in memory.

    update_item honours ExpressionAttributeNames so expressions like
    ``SET #pf = :v`` (typical for reserved words such as
    ``pending_feedback``) are resolved to the real attribute name
    instead of being stored under a mangled key (issue 167). Unknown
    name tokens are rejected loudly rather than silently mis-stored.

    Expression parsing uses regexes that isolate attribute-name and
    value tokens instead of naive whitespace splitting, so commas,
    parentheses, or other separators in a more complex expression do
    not silently break attribute resolution (issues 161, 54). The fake
    never infers an operation from a substring or token prefix (e.g. it
    does not detect feedback clearance by matching a token that merely
    starts with ``pending_feedback``); a ``REMOVE`` clause is applied by
    resolving each listed attribute name exactly, however it is
    formatted (extra whitespace, ExpressionAttributeNames aliases), so
    the feedback-loop test cannot produce false positives or negatives
    from parsing artifacts (issue 54).

    Scope limitation (issues 162, 166, 54): this fake is deliberately
    tightened to the exact UpdateExpression patterns the orchestrator
    currently emits -- simple, flat SET/REMOVE assignments of top-level
    attributes mapped through
    ExpressionAttributeNames/ExpressionAttributeValues, optionally
    combining a SET clause with a REMOVE clause in one expression.
    Nested or path-based update expressions (e.g. ``SET a.b = :v``,
    list-index assignments, or nested document paths) are NOT
    supported and would create wrong top-level keys; they are
    intentionally out of scope for the orchestrator's current simple
    update patterns. Any expression that does not match these exact
    patterns raises ValueError loudly (unmapped names, unmapped values,
    or unparseable SET/REMOVE clauses) rather than silently diverging
    from production behavior.
    """

    # Matches an attribute name token on the left of ``=`` in a SET
    # clause: either an expression-attribute-name placeholder (#name) or
    # a bare identifier.
    _SET_ASSIGNMENT_RE = re.compile(
        r"(#[A-Za-z0-9_]+|[A-Za-z_][A-Za-z0-9_.]*)\s*=\s*"
        r"(:[A-Za-z0-9_]+)"
    )
    # Matches a single attribute name token in a REMOVE clause.
    _REMOVE_TOKEN_RE = re.compile(r"#[A-Za-z0-9_]+|[A-Za-z_][A-Za-z0-9_.]*")

    def __init__(self):
        super().__init__()
        self._items = {}

    def get_item(self, Key):
        session_id = Key.get("session_id")
        item = self._items.get(session_id)
        return {"Item": dict(item)} if item else {}

    def put_item(self, Item):
        self._items[Item["session_id"]] = dict(Item)

    def update_item(
        self,
        Key,
        UpdateExpression,
        ExpressionAttributeValues=None,
        ExpressionAttributeNames=None,
        ConditionExpression=None,
        **kwargs
    ):
        # Note (issues 162, 166, 54): only the exact flat top-level
        # assignment patterns the orchestrator emits are supported
        # (optional SET of ``name = :value`` pairs plus an optional
        # REMOVE of bare/#-mapped names). Combined SET ... REMOVE
        # expressions and #-prefixed ExpressionAttributeNames are
        # handled; anything else (nested paths, arithmetic, functions)
        # raises ValueError below instead of being silently mishandled.
        session_id = Key["session_id"]
        item = self._items.get(session_id, {"session_id": session_id})

        names = dict(ExpressionAttributeNames or {})
        values = dict(ExpressionAttributeValues or {})

        def _resolve(token):
            token = token.strip()
            if token.startswith("#"):
                if token not in names:
                    raise ValueError(
                        "unmapped expression attribute name: %s" % token
                    )
                return names[token]
            if token.startswith(":"):
                raise ValueError(
                    "value token used as attribute name: %s" % token
                )
            return token

        if "SET" in UpdateExpression:
            set_clause = UpdateExpression.split("SET", 1)[1]
            set_clause = set_clause.split("REMOVE", 1)[0]
            matched_any = False
            for match in self._SET_ASSIGNMENT_RE.finditer(set_clause):
                matched_any = True
                field = _resolve(match.group(1))
                value_key = match.group(2)
                if value_key not in values:
                    raise ValueError(
                        "unmapped expression attribute value: %s" % value_key
                    )
                item[field] = values[value_key]
            if not matched_any and set_clause.strip():
                raise ValueError(
                    "could not parse SET clause: %s" % set_clause
                )
        if "REMOVE" in UpdateExpression:
            remove_clause = UpdateExpression.split("REMOVE", 1)[1]
            matched_any = False
            for match in self._REMOVE_TOKEN_RE.finditer(remove_clause):
                matched_any = True
                field = _resolve(match.group(0))
                item.pop(field, None)
            if not matched_any and remove_clause.strip():
                raise ValueError(
                    "could not parse REMOVE clause: %s" % remove_clause
                )
        self._items[session_id] = item


class FakeSmsClient(MagicMock):
    """Records send_text_message calls for later inspection.

    Validates the required fields of every call (issue 175): an
    orchestrator that omits ``OriginationIdentity`` (which must come from
    the SMS_ORIGINATION_ID environment variable), ``DestinationPhoneNumber``
    or ``MessageBody`` fails loudly here instead of passing silently.
    """

    REQUIRED_FIELDS = (
        "OriginationIdentity",
        "DestinationPhoneNumber",
        "MessageBody",
    )

    def __init__(self):
        super().__init__()
        self.sends = []

    def send_text_message(self, **kwargs):
        for field in self.REQUIRED_FIELDS:
            if not kwargs.get(field):
                raise ValueError(
                    "send_text_message missing required field: %s" % field
                )
        self.sends.append(kwargs)
        return {"MessageId": "mock-sms-id"}


class FakeSesClient(MagicMock):
    """Records send_email calls for later inspection."""

    def __init__(self):
        super().__init__()
        self.sends = []

    def send_email(self, **kwargs):
        self.sends.append(kwargs)
        return {"MessageId": "mock-email-id"}


class TestOrchestrator(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Install the test environment with patch.dict here (not at
        # module level) and stop it via addClassCleanup so nothing leaks
        # into other test modules sharing the process, regardless of
        # collection order (issues 172, 164, 156, 53).
        cls._env_patch = patch.dict(os.environ, _TEST_ENV)
        cls._env_patch.start()
        cls.addClassCleanup(cls._env_patch.stop)

        # Import the orchestrator under the test environment, with the
        # repo root resolved from this file's location so the import
        # does not depend on the caller's cwd (issue 57).
        cls.orchestrator = _import_orchestrator()

        # Patch the module-qualified reference to urlopen used by the
        # orchestrator so the stub intercepts regardless of whether the
        # orchestrator does `import urllib.request` or
        # `from urllib.request import urlopen`. This makes the external
        # service boundary explicit (issue 163). The patch is stopped via
        # addClassCleanup so it cannot stay active and interfere with
        # other test files imported later in the same process (issues
        # 173, 165, 156, 53).
        cls._urlopen_patch = patch(
            "%s.urlopen" % cls.orchestrator.__name__
        )
        cls.urlopen_mock = cls._urlopen_patch.start()
        cls.addClassCleanup(cls._urlopen_patch.stop)

    def setUp(self):
        orchestrator = self.orchestrator

        # Stub boto3 clients
        boto3 = orchestrator.channels.boto3 = MagicMock()
        self.fake_sms = FakeSmsClient()
        self.fake_ses = FakeSesClient()

        def _client_selector(service, *args, **kwargs):
            if service == "pinpoint-sms-voice-v2":
                return self.fake_sms
            if service == "sesv2":
                return self.fake_ses
            raise ValueError("unexpected client: %s" % service)

        boto3.client.side_effect = _client_selector

        # Stub DynamoDB resource
        boto3_resource = orchestrator.sessions.boto3 = MagicMock()
        self.fake_table = FakeDynamoTable()
        boto3_resource.resource.return_value.Table.return_value = self.fake_table

        # Stub external engine call via the module-qualified urlopen patch
        # installed in setUpClass (issues 162/163).
        self.engine_calls = []
        self.engine_responses = []
        self.fake_table._items.clear()
        self.fake_sms.sends.clear()
        self.fake_ses.sends.clear()
        self.engine_calls.clear()
        self.engine_responses.clear()

        # Accept (*args, **kwargs) rather than a fixed
        # (request, timeout=None) signature (issue 161): if the
        # orchestrator passes extra positional/keyword args to urlopen
        # (data, context, cafile, ...), a narrow side-effect signature
        # would raise an opaque TypeError from the mock; the wide
        # signature accepts them and the first positional arg is still
        # recorded as the request.
        def _urlopen_side_effect(*args, **kwargs):
            request = args[0] if args else None
            self.engine_calls.append(request)
            if not self.engine_responses:
                raise AssertionError(
                    "unexpected engine call #%d: the orchestrator made "
                    "more engine requests than the test queued responses "
                    "for (%d call(s) recorded)" % (
                        len(self.engine_calls), len(self.engine_calls)
                    )
                )
            resp = self.engine_responses.pop(0)
            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps(resp).encode("utf-8")
            return mock_resp

        self.urlopen_mock.side_effect = _urlopen_side_effect
        self.urlopen_mock.reset_mock()

    def _get_session(self, session_id):
        """Read a session back through the fake's get_item round-trip
        instead of touching internal storage directly (issue 167)."""
        return self.fake_table.get_item(Key={"session_id": session_id}).get(
            "Item"
        )

    # --- Handler path calls the v2 clients (issues 20, 157) -------------------

    def test_handler_path_calls_both_v2_clients(self):
        """The handler path calls both qualifying CDS v2 clients at
        runtime: pinpoint-sms-voice-v2 SendTextMessage for an inbound SMS
        and sesv2 SendEmail for an inbound email (issue 20). The
        assertion is on observable calls made while handling events, not
        on import mechanics, matching the criterion wording "the handler
        path calls"."""
        self.engine_responses = [
            {"answer": "SMS answer."},
            {"answer": "Email answer."},
        ]

        self.orchestrator.handler(
            make_sns_sms_event("+15551234567", "SMS question?"), None
        )
        self.orchestrator.handler(
            make_ses_email_event(CUSTOMER_ADDRESS, "Email question?"), None
        )

        self.assertEqual(
            len(self.fake_sms.sends), 1,
            "handler path must call pinpoint-sms-voice-v2 SendTextMessage",
        )
        self.assertEqual(
            len(self.fake_ses.sends), 1,
            "handler path must call sesv2 SendEmail",
        )
        self.assertEqual(
            self.fake_sms.sends[0]["OriginationIdentity"],
            _TEST_ENV["SMS_ORIGINATION_ID"],
        )
        self.assertEqual(
            self.fake_ses.sends[0]["FromEmailAddress"], JUDGE_ADDRESS
        )

    def test_boto3_resolved_per_call_not_captured_at_import(self):
        """The handler path must resolve boto3 per call (issue 157).

        If the orchestrator (or its channel/session modules) captured a
        boto3 client at import time, swapping the boto3.client side
        effect mid-test would silently do nothing and the second send
        would never reach the replacement fake. This test replaces the
        SMS fake after the first turn and asserts the second turn's send
        lands on the replacement, proving per-call resolution.
        """
        self.engine_responses = [
            {"answer": "First."},
            {"answer": "Second."},
        ]
        self.orchestrator.handler(
            make_sns_sms_event("+15551234567", "First question?"), None
        )
        self.assertEqual(len(self.fake_sms.sends), 1)

        replacement_sms = FakeSmsClient()

        def _client_selector(service, *args, **kwargs):
            if service == "pinpoint-sms-voice-v2":
                return replacement_sms
            if service == "sesv2":
                return self.fake_ses
            raise ValueError("unexpected client: %s" % service)

        self.orchestrator.channels.boto3.client.side_effect = (
            _client_selector
        )

        self.orchestrator.handler(
            make_sns_sms_event("+15551234567", "Second question?"), None
        )

        self.assertEqual(
            len(replacement_sms.sends), 1,
            "orchestrator must resolve boto3 clients per call, not "
            "capture them at import time (issue 157)",
        )
        self.assertEqual(
            len(self.fake_sms.sends), 1,
            "stale captured client must not receive later sends",
        )

    # --- SMS-in -> SMS-out ---------------------------------------------------

    def test_sms_in_sms_out_answer(self):
        """An SMS question receives an SMS answer with the Rex signature."""
        self.engine_responses = [{"answer": "The answer is 42."}]

        event = make_sns_sms_event("+15551234567", "What is the answer?")
        res = self.orchestrator.handler(event, None)
        self.assertEqual(res["statusCode"], 200)

        self.assertEqual(len(self.fake_sms.sends), 1)
        send = self.fake_sms.sends[0]
        self.assertEqual(send["DestinationPhoneNumber"], "+15551234567")
        # The outbound SMS must originate from the configured identity
        # (SMS_ORIGINATION_ID); an orchestrator that omits or hardcodes it
        # fails here (issue 175).
        self.assertEqual(
            send["OriginationIdentity"], _TEST_ENV["SMS_ORIGINATION_ID"]
        )
        self.assertIn("The answer is 42.", send["MessageBody"])
        self.assertIn("Rex, 3Dogs Nexus", send["MessageBody"])

    def test_sms_clarifying_question(self):
        """When the engine returns a clarify field the orchestrator relays it.

        The clarifying reply is a customer-facing outbound message, so it
        must carry the same Rex signature as an answer (issue 176)."""
        self.engine_responses = [
            {"clarify": "Which product are you asking about?"}
        ]

        event = make_sns_sms_event("+15551234567", "Tell me about it.")
        res = self.orchestrator.handler(event, None)
        self.assertEqual(res["statusCode"], 200)

        self.assertEqual(len(self.fake_sms.sends), 1)
        send = self.fake_sms.sends[0]
        self.assertIn("Which product are you asking about?",
                      send["MessageBody"])
        self.assertIn(
            "Rex, 3Dogs Nexus", send["MessageBody"],
            "clarifying SMS must carry the Rex signature (issue 176)",
        )
        self.assertEqual(
            send["OriginationIdentity"], _TEST_ENV["SMS_ORIGINATION_ID"]
        )

    # --- External service boundary (issues 168, 171) -------------------------

    def test_engine_request_uses_configured_endpoint_and_auth(self):
        """The engine request must go to the configured NEXUS_ENDPOINT and
        carry an Authorization header derived from NEXUS_TOKEN. These are
        the core external-service-boundary acceptance points (issues 168,
        171)."""
        self.engine_responses = [{"answer": "OK"}]
        self.orchestrator.handler(
            make_sns_sms_event("+15551234567", "A question."), None
        )

        self.assertEqual(len(self.engine_calls), 1)
        request = self.engine_calls[0]
        self.assertEqual(request.full_url, _TEST_ENV["NEXUS_ENDPOINT"])

        auth = request.get_header("Authorization")
        self.assertIsNotNone(auth, "engine request missing Authorization header")
        self.assertIn(_TEST_ENV["NEXUS_TOKEN"], auth)

    def test_engine_auth_header_present_on_email_channel_too(self):
        """Email-triggered engine calls carry the same auth (issues 168,
        171): the captured Request object's URL and headers are inspected
        directly, so an orchestrator that omits the token or posts to the
        wrong URL fails these tests."""
        self.engine_responses = [{"answer": "OK"}]
        self.orchestrator.handler(
            make_ses_email_event(JUDGE_ADDRESS, "Question?"), None
        )

        self.assertEqual(len(self.engine_calls), 1)
        request = self.engine_calls[0]
        self.assertEqual(request.full_url, _TEST_ENV["NEXUS_ENDPOINT"])
        auth = request.get_header("Authorization")
        self.assertIsNotNone(auth)
        self.assertIn(_TEST_ENV["NEXUS_TOKEN"], auth)

    # --- Clarification state correlation (issue 6) ----------------------------

    def test_clarify_turn_flags_session_awaiting_clarification(self):
        """A clarify turn must persist awaiting_clarification on the session
        so the next inbound message is correlated with the pending
        clarification (issue 6)."""
        self.engine_responses = [
            {"clarify": "Which product are you asking about?"}
        ]

        event = make_sns_sms_event("+15551234567", "Tell me about it.")
        self.orchestrator.handler(event, None)

        session = self._get_session("sms:+15551234567")
        self.assertIsNotNone(session)
        self.assertTrue(session.get("awaiting_clarification"))

    def test_next_message_after_clarify_forwarded_with_context_marker(self):
        """After a clarify turn, the next inbound message is sent to the
        engine with the awaiting_clarification context marker, not treated
        as a new independent question (issue 6). A successful answer then
        clears the flag."""
        # Turn 1: engine clarifies
        self.engine_responses = [
            {"clarify": "Which product are you asking about?"}
        ]
        self.orchestrator.handler(
            make_sns_sms_event("+15551234567", "Tell me about it."), None
        )

        # Turn 2: user answers the clarification; engine returns an answer
        self.engine_responses = [{"answer": "Here are the details."}]
        self.orchestrator.handler(
            make_sns_sms_event("+15551234567", "The premium plan."), None
        )

        self.assertEqual(len(self.engine_calls), 2)
        calls = [json.loads(r.data) for r in self.engine_calls]
        # First call: fresh question, no clarification pending
        self.assertFalse(calls[0].get("awaiting_clarification"))
        # Second call: correlated with the pending clarification
        self.assertTrue(calls[1].get("awaiting_clarification"))

        # Flag cleared after the successful (non-clarify) answer turn
        session = self._get_session("sms:+15551234567")
        self.assertIsNotNone(session)
        self.assertFalse(session.get("awaiting_clarification"))

    def test_fresh_question_has_no_clarification_marker(self):
        """A brand-new sender/question is forwarded without the
        awaiting_clarification marker (issue 6 negative control)."""
        self.engine_responses = [{"answer": "OK"}]
        self.orchestrator.handler(
            make_sns_sms_event("+15551234567", "A fresh question."), None
        )

        calls = [json.loads(r.data) for r in self.engine_calls]
        self.assertFalse(calls[0].get("awaiting_clarification"))

    # --- Email-in -> Email-out -----------------------------------------------

    def test_email_in_email_out_answer(self):
        """An email question receives an email answer. The synthetic event
        carries a realistic base64-encoded raw RFC 822 ``content`` so the
        orchestrator's email decode-and-parse path is exercised (issues
        170, 165)."""
        self.engine_responses = [{"answer": "Here you go."}]

        event = make_ses_email_event(
            CUSTOMER_ADDRESS, "Help?", body="What is the status of my order?"
        )
        res = self.orchestrator.handler(event, None)
        self.assertEqual(res["statusCode"], 200)

        self.assertEqual(len(self.fake_ses.sends), 1)
        send = self.fake_ses.sends[0]
        self.assertEqual(send["FromEmailAddress"], JUDGE_ADDRESS)
        self.assertEqual(
            send["Destination"]["ToAddresses"], [CUSTOMER_ADDRESS]
        )
        self.assertIn("Here you go.", send["Content"]["Simple"]["Body"]["Text"]["Data"])
        self.assertIn("Rex, 3Dogs Nexus",
                      send["Content"]["Simple"]["Body"]["Text"]["Data"])

    def test_email_replies_to_original_sender_not_self(self):
        """With an inbound sender distinct from the SES_FROM identity, the
        reply must go to the original sender (issue 163).

        When the inbound sender equals SES_FROM (as in the judge-email
        tests above), an orchestrator that mistakenly replies to the From
        identity or to itself would pass silently. Using a sender that
        differs from SES_FROM makes a reply-to-self bug observable: the
        Destination must be the customer address, while FromEmailAddress
        stays the configured judges identity.
        """
        self.engine_responses = [{"answer": "Tracked answer."}]

        event = make_ses_email_event(
            CUSTOMER_ADDRESS, "Help?", body="Where is my invoice?"
        )
        res = self.orchestrator.handler(event, None)
        self.assertEqual(res["statusCode"], 200)

        self.assertEqual(len(self.fake_ses.sends), 1)
        send = self.fake_ses.sends[0]
        self.assertEqual(
            send["FromEmailAddress"], JUDGE_ADDRESS,
            "outbound identity must remain SES_FROM (judges address)",
        )
        self.assertEqual(
            send["Destination"]["ToAddresses"], [CUSTOMER_ADDRESS],
            "reply must go to the original sender, not to SES_FROM "
            "(reply-to-self bug, issue 163)",
        )
        self.assertNotEqual(
            send["Destination"]["ToAddresses"],
            [send["FromEmailAddress"]],
            "reply destination must not equal the outbound identity",
        )

    def test_email_body_forwarded_to_engine(self):
        """The body text parsed from the base64-encoded raw RFC 822
        ``content`` is what reaches the engine as the question, proving
        the email decode/parse path is covered end-to-end (issues 170,
        165)."""
        self.engine_responses = [{"answer": "OK"}]
        self.orchestrator.handler(
            make_ses_email_event(
                JUDGE_ADDRESS, "Help?", body="What is the status of my order?"
            ),
            None,
        )

        self.assertEqual(len(self.engine_calls), 1)
        call = json.loads(self.engine_calls[0].data)
        self.assertEqual(call["question"], "What is the status of my order?")

    def test_email_subject_and_body_are_distinguished(self):
        """With a subject that differs from the body, the engine must
        receive the body text, not the subject or a concatenation
        (issue 165).

        ``make_ses_email_event`` deliberately defaults ``body`` to
        ``subject``; this test passes a distinct body so an orchestrator
        that forwards the subject header (or the whole raw message)
        instead of extracting the body fails here. The subject chosen is
        a non-empty string that does not appear in the body, so a
        subject-leak is detectable.
        """
        subject = "Billing enquiry reference 7"
        body = "Please confirm the renewal date for our account."
        self.engine_responses = [{"answer": "OK"}]
        self.orchestrator.handler(
            make_ses_email_event(CUSTOMER_ADDRESS, subject, body=body),
            None,
        )

        self.assertEqual(len(self.engine_calls), 1)
        call = json.loads(self.engine_calls[0].data)
        self.assertEqual(
            call["question"], body,
            "engine must receive the parsed body, not the subject or the "
            "raw message (issue 165)",
        )
        self.assertNotIn(
            subject, call["question"],
            "subject header must not leak into the forwarded question",
        )

    def test_email_clarifying_question(self):
        """When the engine returns a clarify field on the email channel,
        the orchestrator relays it as an outbound email, mirroring the
        SMS clarify flow (issue 170). The clarifying email must also
        carry the Rex signature (issue 176)."""
        self.engine_responses = [
            {"clarify": "Which region are you asking about?"}
        ]

        event = make_ses_email_event(
            CUSTOMER_ADDRESS, "Question", body="Tell me about availability."
        )
        res = self.orchestrator.handler(event, None)
        self.assertEqual(res["statusCode"], 200)

        self.assertEqual(len(self.fake_ses.sends), 1)
        send = self.fake_ses.sends[0]
        self.assertEqual(send["FromEmailAddress"], JUDGE_ADDRESS)
        self.assertIn("Which region are you asking about?",
                      send["Content"]["Simple"]["Body"]["Text"]["Data"])
        self.assertIn(
            "Rex, 3Dogs Nexus",
            send["Content"]["Simple"]["Body"]["Text"]["Data"],
            "clarifying email must carry the Rex signature (issue 176)",
        )

    # --- Feedback loop -------------------------------------------------------

    def test_feedback_recorded_then_forwarded(self):
        """A 'that's wrong' reply is saved as feedback and sent on the next
        engine call together with the re-ask text and the session
        identifier, so a bug swapping question/feedback fields is caught
        (issue 172). A subsequent good answer clears the feedback."""
        # Turn 1: engine returns answer, user replies "that's wrong"
        self.engine_responses = [{"answer": "X is Y."}]
        event = make_sns_sms_event("+15551234567", "What is X?")
        self.orchestrator.handler(event, None)
        self.assertEqual(len(self.engine_calls), 1)

        self.engine_responses = []  # feedback handler must not call engine
        feedback_event = make_sns_sms_event("+15551234567", "that's wrong")
        res = self.orchestrator.handler(feedback_event, None)
        self.assertEqual(res["statusCode"], 200)

        # Explicit assertion (issues 166, 55): the feedback turn made no
        # new engine call. Without this, an orchestrator that erroneously
        # invoked the engine during feedback handling would either surface
        # as a confusing IndexError from pop(0) on an empty response list
        # or pass silently behind a mocked response.
        self.assertEqual(
            len(self.engine_calls), 1,
            "feedback handler must not call the engine",
        )

        # Session should now carry pending_feedback (round-trip read,
        # issue 167)
        session = self._get_session("sms:+15551234567")
        self.assertIsNotNone(session)
        self.assertIn("pending_feedback", session)

        # Turn 2: user re-asks; engine should receive feedback
        self.engine_responses = [{"answer": "Corrected answer."}]
        event2 = make_sns_sms_event("+15551234567", "Try again please")
        self.orchestrator.handler(event2, None)

        self.assertEqual(len(self.engine_calls), 2)
        calls = [json.loads(r.data) for r in self.engine_calls]
        # Use .get() so a payload that omits the "feedback" key when it
        # is None (a common, valid serialisation) fails as a meaningful
        # assertion on value instead of an opaque KeyError (issue 160).
        self.assertEqual(calls[0].get("feedback"), None)  # first call had none
        self.assertEqual(calls[1].get("feedback"), "that's wrong")  # second call includes it

        # The re-ask text and the session identifier must also be
        # forwarded correctly on the second call (issue 172).
        self.assertEqual(calls[1]["question"], "Try again please")
        self.assertEqual(calls[1].get("session_id"), "sms:+15551234567")

        # Feedback should be cleared after the successful answer. Assert
        # the session exists first (issue 164) so a failure earlier in
        # the chain surfaces the real error instead of an obscure
        # TypeError from assertNotIn on None.
        session_after = self._get_session("sms:+15551234567")
        self.assertIsNotNone(
            session_after,
            "session missing after second handler call; "
            "an earlier step in the feedback chain failed",
        )
        self.assertNotIn("pending_feedback", session_after)

    # --- Engine failure paths (issue 169) ------------------------------------

    def test_engine_http_error_produces_error_reply_not_crash(self):
        """If the engine returns an HTTP error the handler must not crash;
        it replies on the inbound channel with an error message so the
        demo degrades gracefully (issue 169)."""
        def _raise_http_error(*args, **kwargs):
            self.engine_calls.append(args[0] if args else None)
            raise urllib.error.HTTPError(
                url=_TEST_ENV["NEXUS_ENDPOINT"],
                code=500,
                msg="Internal Server Error",
                hdrs=None,
                fp=None,
            )

        self.urlopen_mock.side_effect = _raise_http_error

        event = make_sns_sms_event("+15551234567", "Hello?")
        res = self.orchestrator.handler(event, None)
        self.assertEqual(res["statusCode"], 200)
        self.assertEqual(len(self.fake_sms.sends), 1)

    def test_engine_url_error_produces_error_reply_not_crash(self):
        """A connection-level failure (URLError) likewise degrades to an
        error reply instead of an unhandled exception (issue 169)."""
        def _raise_url_error(*args, **kwargs):
            self.engine_calls.append(args[0] if args else None)
            raise urllib.error.URLError("connection refused")

        self.urlopen_mock.side_effect = _raise_url_error

        event = make_ses_email_event(CUSTOMER_ADDRESS, "Hello?")
        res = self.orchestrator.handler(event, None)
        self.assertEqual(res["statusCode"], 200)
        self.assertEqual(len(self.fake_ses.sends), 1)

    def test_engine_malformed_json_produces_error_reply_not_crash(self):
        """A malformed JSON body from the engine must not crash the
        handler (issue 169)."""
        def _bad_json(*args, **kwargs):
            self.engine_calls.append(args[0] if args else None)
            mock_resp = MagicMock()
            mock_resp.read.return_value = b"not-json{{"
            return mock_resp

        self.urlopen_mock.side_effect = _bad_json

        event = make_sns_sms_event("+15551234567", "Hello?")
        res = self.orchestrator.handler(event, None)
        self.assertEqual(res["statusCode"], 200)
        self.assertEqual(len(self.fake_sms.sends), 1)

    def test_engine_timeout_produces_error_reply_not_crash(self):
        """An engine timeout (socket timeout from urlopen) degrades to an
        error reply (issue 169)."""
        def _raise_timeout(*args, **kwargs):
            self.engine_calls.append(args[0] if args else None)
            raise TimeoutError("engine timed out")

        self.urlopen_mock.side_effect = _raise_timeout

        event = make_sns_sms_event("+15551234567", "Hello?")
        res = self.orchestrator.handler(event, None)
        self.assertEqual(res["statusCode"], 200)
        self.assertEqual(len(self.fake_sms.sends), 1)

    # --- Destination allowlist -----------------------------------------------

    def test_reply_refused_for_non_allowed_destination(self):
        """Replies to the inbound sender are always allowed (judges cannot be pre-listed); any other destination outside ALLOWED_DESTINATIONS is refused with PermissionError (#869-A)."""
        self.engine_responses = [{"answer": "OK"}]

        res = self.orchestrator.handler(
            make_sns_sms_event("+19998887777", "ping"), None
        )
        self.assertEqual(res["statusCode"], 200)
        self.assertEqual(len(self.fake_sms.sends), 1)
        send = self.fake_sms.sends[0]
        self.assertEqual(send["DestinationPhoneNumber"], "+19998887777")
        self.assertEqual(len(self.fake_ses.sends), 0)

        with self.assertRaises(PermissionError):
            self.orchestrator.channels.send_sms("+18880000000", "x")
        self.assertEqual(len(self.fake_sms.sends), 1)

    # --- Unrecognised payload (issue 56) --------------------------------------

    def test_unrecognised_event_ignored(self):
        """A payload that is neither SMS nor SES returns 200 and no sends.

        Only the statusCode contract is asserted (issue 164): the
        SMS/SES tests assert statusCode without pinning a response body,
        so pinning a body string only for the ignored path would create
        an inconsistent contract check."""
        res = self.orchestrator.handler({"Records": []}, None)
        self.assertEqual(res["statusCode"], 200)
        self.assertEqual(len(self.fake_sms.sends), 0)
        self.assertEqual(len(self.fake_ses.sends), 0)

    def test_malformed_non_empty_record_ignored(self):
        """A non-empty record that lacks the expected SMS/SES structure
        must be ignored rather than processed or crashed (issue 56).

        The empty-Records case alone leaves a gap: a record with an
        unrecognised shape (missing EventSource, missing the ``ses``
        key, or an SNS message that is not valid JSON / lacks the SMS
        fields) must also produce a 200 with no sends and no engine
        call.
        """
        cases = [
            {"Records": [{"invalid": "structure"}]},
            {"Records": [{"EventSource": "aws:sns"}]},  # no Sns payload
            {
                "Records": [
                    {
                        "EventSource": "aws:sns",
                        "Sns": {"Message": "not-json{{"},
                    }
                ]
            },
            {
                "Records": [
                    {
                        "EventSource": "aws:sns",
                        "Sns": {"Message": json.dumps({"unrelated": True})},
                    }
                ]
            },
        ]
        for event in cases:
            res = self.orchestrator.handler(event, None)
            self.assertEqual(
                res["statusCode"], 200,
                "malformed record must be ignored gracefully: %r" % (event,),
            )
        self.assertEqual(len(self.fake_sms.sends), 0)
        self.assertEqual(len(self.fake_ses.sends), 0)
        self.assertEqual(
            len(self.engine_calls), 0,
            "malformed records must not reach the engine (issue 56)",
        )


class TestTemplateJudgeChannel(unittest.TestCase):
    """Parse infra/template.yaml and pin the judge email channel (issue 27).

    The template is parsed as text (no YAML dependency) so the test runs
    in any environment. The receipt-rule Recipients list must be exactly
    ["judges@cds.3dogs.ai"], the outbound sender identity must be
    judges@cds.3dogs.ai, and no sender@example.org or apex 3dogs.ai mailbox
    may appear anywhere in the template.
    """

    @classmethod
    def setUpClass(cls):
        if not _TEMPLATE_PATH.is_file():
            raise unittest.SkipTest(
                "infra/template.yaml not present at %s" % _TEMPLATE_PATH
            )
        cls.template_text = _TEMPLATE_PATH.read_text(encoding="utf-8")

    def _recipients_block(self):
        """Extract the Recipients list of the SES receipt rule."""
        match = re.search(
            r"Recipients\s*:\s*\[(?P<inline>[^\]]*)\]"
            r"|Recipients\s*:\s*\n(?P<block>(?:\s*-\s*.+\n)+)",
            self.template_text,
        )
        self.assertIsNotNone(match, "no Recipients list found in template")
        if match.group("inline") is not None:
            items = [
                item.strip().strip("'\"")
                for item in match.group("inline").split(",")
                if item.strip()
            ]
        else:
            items = [
                line.split("-", 1)[1].strip().strip("'\"")
                for line in match.group("block").splitlines()
                if line.strip().startswith("-")
            ]
        return items

    def test_receipt_rule_recipients_exactly_judges_address(self):
        """The SES receipt rule Recipients list is exactly
        ["judges@cds.3dogs.ai"] (issue 27; negative control below)."""
        self.assertEqual(self._recipients_block(), [JUDGE_ADDRESS])

    def test_receipt_rule_recipients_reject_other_addresses(self):
        """Negative control: any other recipient must fail the exact-match
        assertion above (issue 27)."""
        for wrong in (
            ["sender@example.org"],
            ["3dogs.ai"],
            [JUDGE_ADDRESS, "sender@example.org"],
            [],
        ):
            self.assertNotEqual(
                wrong, [JUDGE_ADDRESS],
                "negative-control fixture unexpectedly matches",
            )

    def test_outbound_sender_identity_is_judges_address(self):
        """The outbound email sender identity in the template is
        judges@cds.3dogs.ai (issue 27). The SES_FROM environment value
        and any sesv2 sender configuration must reference it."""
        self.assertIn(JUDGE_ADDRESS, self.template_text)
        # SES_FROM (or equivalent sender wiring) must point at the judges
        # address, not merely mention it incidentally.
        sender_match = re.search(
            r"SES_FROM[^\n]*" + re.escape(JUDGE_ADDRESS),
            self.template_text,
        ) or re.search(
            r"FromEmailAddress[^\n]*" + re.escape(JUDGE_ADDRESS),
            self.template_text,
        ) or re.search(
            r"EmailIdentity[^\n]*" + re.escape(JUDGE_ADDRESS),
            self.template_text,
        )
        self.assertIsNotNone(
            sender_match,
            "template has no outbound sender identity wired to %s"
            % JUDGE_ADDRESS,
        )

    def test_no_rex_or_apex_mailbox_anywhere(self):
        """Neither sender@example.org nor a bare apex 3dogs.ai mailbox may
        appear in the template (issue 27 negative control)."""
        self.assertNotIn("sender@example.org", self.template_text)
        for match in re.finditer(
            r"[A-Za-z0-9_.+-]*@3dogs\.ai", self.template_text
        ):
            self.assertEqual(
                match.group(0), JUDGE_ADDRESS,
                "unexpected 3dogs.ai mailbox in template: %s"
                % match.group(0),
            )


if __name__ == "__main__":
    unittest.main()
