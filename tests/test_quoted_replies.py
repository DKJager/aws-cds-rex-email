
"""Judge tests for quoted-reply handling on the email channel.

Two live defects, both confirmed by the XXL audit:

D1 (quoted history re-triggers feedback): channels._extract_text_from_mime
returns the whole text/plain part including the quoted history. Once a
judge has written "that's wrong", every later in-thread Gmail reply
quotes it ("On <date> ... wrote:" + "> that's wrong"), orchestrator
_is_feedback matches the quoted line, the correction is stored again and
the judge's NEW question never reaches the engine.

D2 (loop guard drops Outlook replies): orchestrator._is_own_or_automated
ignores any message containing a line equal to "-- Rex, 3Dogs Nexus".
Outlook (desktop and web) quotes the previous mail WITHOUT ">" prefixes
under a "From: / Sent: / To: / Subject:" header block (web: after a
"________________________________" line), so a judge's in-thread
follow-up carries our signature line unprefixed and is silently dropped.

Naming contract: test_fails_before_fix_* encode the defects (fail on the
current code, pass once fixed); test_guard_* pin behaviour the fix must
keep (pass on the current code). Every test asserts on observable calls
only: engine payloads, SES sends and stored session state. Each message
is built as a realistic raw RFC 822 email, base64-encoded into the SES
event ``content`` exactly as tests/test_orchestrator.py does.
"""

import base64
import json
import os
import quopri
import re
import sys
import unittest
import urllib.request  # noqa: F401  (explicit import; the module is patched)
import zlib
from pathlib import Path
from unittest.mock import MagicMock, patch

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

JUDGE_ADDRESS = "judges@cds.3dogs.ai"
REX_HEADER = "Rex <judges@cds.3dogs.ai>"
SIGNATURE_LINE = "-- Rex, 3Dogs Nexus"

# A judge writing in from a mailbox distinct from our own identity (and
# not pre-listed in ALLOWED_DESTINATIONS, as real judges cannot be).
JUDGE_SENDER = "alex.judge@example.org"
JUDGE_HEADER = "Alex Judge <alex.judge@example.org>"
JUDGE_SESSION = "email:%s" % JUDGE_SENDER

SUBJECT = "Premium plan question"
FIRST_QUESTION = "What is the SLA for the premium plan?"
FIRST_ANSWER = "The premium plan SLA is 99.9% monthly uptime."
ACK_TEXT = "Thanks - noted. Please ask your question again and I'll do better."


def _import_orchestrator():
    """Import src.orchestrator regardless of the caller's cwd (issue 57)."""
    repo_root_str = str(_REPO_ROOT)
    inserted = False
    if repo_root_str not in sys.path:
        sys.path.insert(0, repo_root_str)
        inserted = True
    try:
        from src import orchestrator  # noqa: WPS433
    except ImportError:
        src_str = str(_REPO_ROOT / "src")
        if src_str not in sys.path:
            sys.path.insert(0, src_str)
        import orchestrator  # noqa: WPS433
    else:
        return orchestrator
    if not inserted and repo_root_str not in sys.path:
        raise ImportError("could not locate src/orchestrator module")
    return orchestrator


# --- Raw RFC 822 builders ---------------------------------------------------

def _crlf(text):
    """Normalise line endings to CRLF as they appear on the wire."""
    return text.replace("\r\n", "\n").replace("\n", "\r\n")


def _qp(text):
    """Quoted-printable encode a body (soft breaks at 76 columns, CRLF)."""
    encoded = quopri.encodestring(text.encode("utf-8")).decode("ascii")
    return _crlf(encoded)


def _headers(sender_header, subject, extra):
    message_id = zlib.crc32((subject + sender_header).encode("utf-8"))
    lines = [
        "From: %s" % sender_header,
        "To: %s" % REX_HEADER,
        "Subject: %s" % subject,
        "Date: Mon, 19 Oct 2026 15:10:44 +0000",
        "Message-ID: <judge-%08x@example.org>" % message_id,
        "In-Reply-To: <rex-1431@cds.3dogs.ai>",
        "References: <rex-1431@cds.3dogs.ai>",
        "MIME-Version: 1.0",
    ]
    lines.extend(extra)
    return "\r\n".join(lines) + "\r\n\r\n"


def plain_message(sender_header, subject, body):
    """A simple single-part text/plain mail (no quoting)."""
    raw = _headers(
        sender_header, subject,
        ["Content-Type: text/plain; charset=utf-8",
         "Content-Transfer-Encoding: 7bit"],
    ) + _crlf(body) + "\r\n"
    return raw.encode("utf-8")


def gmail_reply_message(new_text, quoted_lines, wrap_attribution=False):
    """Gmail in-thread reply: new text, blank line, attribution line,
    then "> "-prefixed quoted history. Gmail sometimes wraps the
    attribution so that "wrote:" lands on its own line."""
    attribution = "On Mon, Oct 19, 2026 at 3:02 PM %s wrote:" % REX_HEADER
    if wrap_attribution:
        attribution = "On Mon, Oct 19, 2026 at 3:02 PM %s\nwrote:" % REX_HEADER
    body = new_text + "\n\n" + attribution + "\n\n" + "\n".join(quoted_lines) + "\n"
    raw = _headers(
        JUDGE_HEADER, "Re: " + SUBJECT,
        ["Content-Type: text/plain; charset=\"UTF-8\"",
         "Content-Transfer-Encoding: 7bit"],
    ) + _crlf(body)
    return raw.encode("utf-8")


_OUTLOOK_HEADER_BLOCK = (
    "From: %s\n"
    "Sent: Monday, October 19, 2026 3:02 PM\n"
    "To: %s\n"
    "Subject: Re: %s\n"
) % (REX_HEADER, JUDGE_HEADER, SUBJECT)


def _outlook_quoted(previous_text):
    """Outlook quotes the previous mail verbatim, no ">" prefixes, with
    our signature line unprefixed inside the quoted part."""
    return "%s\n%s\n\n%s\n" % (_OUTLOOK_HEADER_BLOCK, previous_text, SIGNATURE_LINE)


def outlook_desktop_message(new_text, previous_text):
    """Outlook desktop: multipart/alternative, quoted-printable text/plain
    part with the header block directly under the new text."""
    boundary = "_000_AM0PR0102MB3011E4A1B2C3D4E5F6_"
    plain = new_text + "\n\n" + _outlook_quoted(previous_text)
    html = (
        "<html><head><meta http-equiv=\"Content-Type\" "
        "content=\"text/html; charset=utf-8\"></head><body>"
        "<div>%s</div><div><br></div><div><b>From:</b> %s<br><b>Sent:</b> "
        "Monday, October 19, 2026 3:02 PM<br><b>To:</b> %s<br><b>Subject:</b> "
        "Re: %s</div><div>%s</div><div>%s</div></body></html>\n"
    ) % (new_text, REX_HEADER, JUDGE_HEADER, SUBJECT, previous_text, SIGNATURE_LINE)
    raw = _headers(
        JUDGE_HEADER, "RE: " + SUBJECT,
        ["Content-Type: multipart/alternative; boundary=\"%s\"" % boundary,
         "X-Mailer: Microsoft Outlook 16.0"],
    )
    raw += (
        "--%s\r\n"
        "Content-Type: text/plain; charset=\"utf-8\"\r\n"
        "Content-Transfer-Encoding: quoted-printable\r\n\r\n"
        "%s"
        "--%s\r\n"
        "Content-Type: text/html; charset=\"utf-8\"\r\n"
        "Content-Transfer-Encoding: quoted-printable\r\n\r\n"
        "%s"
        "--%s--\r\n"
    ) % (boundary, _qp(plain), boundary, _qp(html), boundary)
    return raw.encode("utf-8")


def outlook_web_message(new_text, previous_text):
    """Outlook web: an underscore separator line, then the same header
    block; single quoted-printable text/plain part."""
    plain = (
        new_text + "\n\n"
        + "________________________________\n"
        + _outlook_quoted(previous_text)
    )
    raw = _headers(
        JUDGE_HEADER, "RE: " + SUBJECT,
        ["Content-Type: text/plain; charset=\"utf-8\"",
         "Content-Transfer-Encoding: quoted-printable"],
    ) + _qp(plain)
    return raw.encode("utf-8")


def make_ses_raw_event(sender, subject, raw_message):
    """Synthetic SES inbound event whose ``content`` is the base64 of a
    raw RFC 822 message, mirroring tests/test_orchestrator.py."""
    encoded_content = base64.b64encode(raw_message).decode("ascii")
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


# --- Fakes (same as tests/test_orchestrator.py) ------------------------------

class FakeDynamoTable(MagicMock):
    """In-memory DynamoDB table honouring the flat SET/REMOVE expressions
    the orchestrator emits (see tests/test_orchestrator.py)."""

    _SET_ASSIGNMENT_RE = re.compile(
        r"(#[A-Za-z0-9_]+|[A-Za-z_][A-Za-z0-9_.]*)\s*=\s*"
        r"(:[A-Za-z0-9_]+)"
    )
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
    """Records send_text_message calls for later inspection."""

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


class TestQuotedReplies(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._env_patch = patch.dict(os.environ, _TEST_ENV)
        cls._env_patch.start()
        cls.addClassCleanup(cls._env_patch.stop)

        cls.orchestrator = _import_orchestrator()

        cls._urlopen_patch = patch(
            "%s.urlopen" % cls.orchestrator.__name__
        )
        cls.urlopen_mock = cls._urlopen_patch.start()
        cls.addClassCleanup(cls._urlopen_patch.stop)

    def setUp(self):
        orchestrator = self.orchestrator

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

        boto3_resource = orchestrator.sessions.boto3 = MagicMock()
        self.fake_table = FakeDynamoTable()
        boto3_resource.resource.return_value.Table.return_value = self.fake_table

        self.engine_calls = []
        self.engine_responses = []
        self.fake_table._items.clear()
        self.fake_sms.sends.clear()
        self.fake_ses.sends.clear()

        def _urlopen_side_effect(*args, **kwargs):
            request = args[0] if args else None
            self.engine_calls.append(request)
            if not self.engine_responses:
                raise AssertionError(
                    "unexpected engine call #%d: the orchestrator made "
                    "more engine requests than the test queued responses "
                    "for" % len(self.engine_calls)
                )
            resp = self.engine_responses.pop(0)
            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps(resp).encode("utf-8")
            return mock_resp

        self.urlopen_mock.side_effect = _urlopen_side_effect
        self.urlopen_mock.reset_mock()

    # --- helpers ------------------------------------------------------------

    def _get_session(self, session_id):
        return self.fake_table.get_item(Key={"session_id": session_id}).get(
            "Item"
        )

    def _engine_payloads(self):
        return [json.loads(r.data) for r in self.engine_calls]

    def _reply_body(self, send):
        return send["Content"]["Simple"]["Body"]["Text"]["Data"]

    def _first_turn(self):
        """Judge asks FIRST_QUESTION, engine answers FIRST_ANSWER."""
        self.engine_responses = [{"answer": FIRST_ANSWER}]
        res = self.orchestrator.handler(
            make_ses_raw_event(
                JUDGE_SENDER, SUBJECT,
                plain_message(JUDGE_HEADER, SUBJECT, FIRST_QUESTION),
            ),
            None,
        )
        self.assertEqual(res["statusCode"], 200)
        self.assertEqual(len(self.engine_calls), 1)
        self.assertEqual(len(self.fake_ses.sends), 1)

    def _correction_turn(self):
        """Judge replies with a bare "that's wrong"; stored, not forwarded."""
        self.engine_responses = []
        res = self.orchestrator.handler(
            make_ses_raw_event(
                JUDGE_SENDER, "Re: " + SUBJECT,
                plain_message(JUDGE_HEADER, "Re: " + SUBJECT, "that's wrong"),
            ),
            None,
        )
        self.assertEqual(res["statusCode"], 200)
        session = self._get_session(JUDGE_SESSION)
        self.assertIsNotNone(session)
        self.assertIn("pending_feedback", session)

    def _assert_no_quoted_history(self, question):
        for line in question.splitlines():
            self.assertFalse(
                line.lstrip().startswith(">"),
                "quoted line leaked into the engine question: %r" % line,
            )
        self.assertNotIn("wrote:", question,
                         "attribution line leaked into the engine question")
        self.assertNotIn("On Mon, Oct 19, 2026", question,
                         "attribution line leaked into the engine question")

    # --- D1: quoted history re-triggers feedback (fails before fix) ---------

    def test_fails_before_fix_gmail_followup_after_correction_reaches_engine(self):
        """(a) The session already holds a correction. The judge's next
        Gmail reply asks a NEW question above the quoted history, which
        contains the earlier "> that's wrong". The engine must be called
        with the new question only (no quoted history), the stored
        correction is forwarded, and no new feedback is stored."""
        self._first_turn()
        self._correction_turn()

        new_question = "Thanks. New question: does that SLA also cover the EU region?"
        quoted = [
            "> " + ACK_TEXT,
            ">",
            "> " + SIGNATURE_LINE,
            ">",
            "> On Mon, Oct 19, 2026 at 2:58 PM %s wrote:" % JUDGE_HEADER,
            "> > that's wrong",
        ]
        self.engine_responses = [{"answer": "Yes, the SLA covers all regions."}]
        res = self.orchestrator.handler(
            make_ses_raw_event(
                JUDGE_SENDER, "Re: " + SUBJECT,
                gmail_reply_message(new_question, quoted),
            ),
            None,
        )
        self.assertEqual(res["statusCode"], 200)

        self.assertEqual(
            len(self.engine_calls), 2,
            "the judge's new question must reach the engine; a quoted "
            "'> that's wrong' must not re-trigger feedback (#1431 B2)",
        )
        payload = self._engine_payloads()[1]
        self.assertEqual(payload["question"], new_question)
        self.assertNotIn("that's wrong", payload["question"])
        self._assert_no_quoted_history(payload["question"])
        self.assertEqual(payload.get("feedback"), "that's wrong")
        self.assertEqual(payload.get("session_id"), JUDGE_SESSION)

        # The reply is the engine answer, not the feedback acknowledgement.
        self.assertEqual(len(self.fake_ses.sends), 3)
        last = self.fake_ses.sends[-1]
        self.assertEqual(last["Destination"]["ToAddresses"], [JUDGE_SENDER])
        self.assertIn("Yes, the SLA covers all regions.", self._reply_body(last))
        self.assertNotIn(ACK_TEXT, self._reply_body(last))

        # No new feedback stored: the forwarded correction was consumed
        # by the successful answer and nothing containing the new
        # question was recorded in its place.
        session = self._get_session(JUDGE_SESSION)
        self.assertIsNotNone(session)
        self.assertNotIn(new_question, session.get("pending_feedback") or "")
        self.assertNotIn("pending_feedback", session)

    def test_fails_before_fix_gmail_engine_question_has_no_quoted_lines(self):
        """(d) A Gmail reply (wrapped attribution: "wrote:" on its own
        line) forwards only the new text: no "> " lines and no
        attribution line reach the engine."""
        self._first_turn()

        new_question = "Is that SLA measured per month or per quarter?"
        quoted = [
            "> " + FIRST_ANSWER,
            ">",
            "> " + SIGNATURE_LINE,
        ]
        self.engine_responses = [{"answer": "Per calendar month."}]
        res = self.orchestrator.handler(
            make_ses_raw_event(
                JUDGE_SENDER, "Re: " + SUBJECT,
                gmail_reply_message(new_question, quoted, wrap_attribution=True),
            ),
            None,
        )
        self.assertEqual(res["statusCode"], 200)
        self.assertEqual(len(self.engine_calls), 2)
        payload = self._engine_payloads()[1]
        self._assert_no_quoted_history(payload["question"])
        self.assertNotIn(SIGNATURE_LINE, payload["question"])
        self.assertNotIn(FIRST_ANSWER, payload["question"])
        self.assertEqual(payload["question"], new_question)
        self.assertEqual(len(self.fake_ses.sends), 2)
        self.assertIn("Per calendar month.", self._reply_body(self.fake_ses.sends[-1]))

    # --- D2: loop guard drops Outlook replies (fails before fix) ------------

    def test_fails_before_fix_outlook_desktop_followup_is_answered(self):
        """(b) Outlook desktop quotes our previous mail under a
        "From:/Sent:/To:/Subject:" block with NO ">" prefixes, so our
        signature line appears unprefixed in the quoted part. The judge's
        follow-up must still be answered: engine called once, exactly
        one reply to the judge."""
        new_question = "Could you also confirm the renewal date for the premium plan?"
        self.engine_responses = [{"answer": "The renewal date is 1 January."}]
        res = self.orchestrator.handler(
            make_ses_raw_event(
                JUDGE_SENDER, "RE: " + SUBJECT,
                outlook_desktop_message(new_question, FIRST_ANSWER),
            ),
            None,
        )
        self.assertEqual(res["statusCode"], 200)

        self.assertEqual(
            len(self.engine_calls), 1,
            "an Outlook follow-up quoting our signature unprefixed must "
            "not be dropped by the loop guard (#1431 B3)",
        )
        payload = self._engine_payloads()[0]
        self.assertEqual(payload["question"], new_question)
        self.assertNotIn(SIGNATURE_LINE, payload["question"])
        self.assertNotIn("Sent: Monday, October 19, 2026", payload["question"])

        self.assertEqual(len(self.fake_ses.sends), 1)
        send = self.fake_ses.sends[0]
        self.assertEqual(send["FromEmailAddress"], JUDGE_ADDRESS)
        self.assertEqual(send["Destination"]["ToAddresses"], [JUDGE_SENDER])
        self.assertIn("The renewal date is 1 January.", self._reply_body(send))

    def test_fails_before_fix_outlook_web_followup_is_answered(self):
        """(c) Outlook web variant: a "________________________________"
        line precedes the header block; otherwise as (b)."""
        new_question = "Is the premium plan SLA also valid for the EU region?"
        self.engine_responses = [{"answer": "Yes, the EU region is covered."}]
        res = self.orchestrator.handler(
            make_ses_raw_event(
                JUDGE_SENDER, "RE: " + SUBJECT,
                outlook_web_message(new_question, FIRST_ANSWER),
            ),
            None,
        )
        self.assertEqual(res["statusCode"], 200)

        self.assertEqual(
            len(self.engine_calls), 1,
            "an Outlook web follow-up quoting our signature unprefixed "
            "must not be dropped by the loop guard (#1431 B3)",
        )
        payload = self._engine_payloads()[0]
        self.assertEqual(payload["question"], new_question)
        self.assertNotIn(SIGNATURE_LINE, payload["question"])
        self.assertNotIn("________________________________", payload["question"])

        self.assertEqual(len(self.fake_ses.sends), 1)
        send = self.fake_ses.sends[0]
        self.assertEqual(send["Destination"]["ToAddresses"], [JUDGE_SENDER])
        self.assertIn("Yes, the EU region is covered.", self._reply_body(send))

    # --- Guards: behaviour the fix must keep (pass before and after) --------

    def test_guard_own_unquoted_reply_is_still_ignored(self):
        """(e) Our own reply coming back UNQUOTED (body = an answer ending
        with the signature line, no quote header) is ignored by the loop
        guard: no engine call, no reply. Checked both when it bounces
        back from our own mailbox (mail.source and the From header are
        both judges@cds.3dogs.ai) and when it is relayed verbatim from
        another mailbox (mail.source and From header both that mailbox).
        In each sub-test the From header matches the SES mail.source."""
        body = FIRST_ANSWER + "\n\n" + SIGNATURE_LINE
        for sender, from_header in (
            (JUDGE_ADDRESS, REX_HEADER),
            (JUDGE_SENDER, JUDGE_HEADER),
        ):
            with self.subTest(sender=sender):
                self.engine_responses = []
                res = self.orchestrator.handler(
                    make_ses_raw_event(
                        sender, "Re: " + SUBJECT,
                        plain_message(from_header, "Re: " + SUBJECT, body),
                    ),
                    None,
                )
                self.assertEqual(res["statusCode"], 200)
                self.assertEqual(
                    len(self.engine_calls), 0,
                    "our own signed reply must never reach the engine",
                )
                self.assertEqual(len(self.fake_ses.sends), 0)
                self.assertEqual(len(self.fake_sms.sends), 0)

    def test_guard_bare_correction_is_still_stored_as_feedback(self):
        """(f) A bare "that's wrong" (no quote) is stored as feedback, the
        engine is not called, and the acknowledgement is sent."""
        self._first_turn()
        self._correction_turn()

        self.assertEqual(len(self.engine_calls), 1,
                         "feedback handler must not call the engine")
        session = self._get_session(JUDGE_SESSION)
        stored = json.loads(session["pending_feedback"])
        self.assertEqual(stored.get("text"), "that's wrong")
        self.assertEqual(stored.get("question"), FIRST_QUESTION)
        self.assertEqual(len(self.fake_ses.sends), 2)
        ack = self.fake_ses.sends[-1]
        self.assertEqual(ack["Destination"]["ToAddresses"], [JUDGE_SENDER])
        self.assertIn(ACK_TEXT, self._reply_body(ack))

    def test_guard_quote_only_correction_is_still_feedback(self):
        """(g) Issue 138: a body that is only "> that's wrong" is still
        treated as feedback (stored, no engine call, acknowledged)."""
        self._first_turn()

        self.engine_responses = []
        res = self.orchestrator.handler(
            make_ses_raw_event(
                JUDGE_SENDER, "Re: " + SUBJECT,
                plain_message(JUDGE_HEADER, "Re: " + SUBJECT, "> that's wrong"),
            ),
            None,
        )
        self.assertEqual(res["statusCode"], 200)
        self.assertEqual(len(self.engine_calls), 1,
                         "a quote-only correction must not call the engine")
        session = self._get_session(JUDGE_SESSION)
        self.assertIsNotNone(session)
        self.assertIn("pending_feedback", session)
        self.assertIn("that's wrong", session["pending_feedback"])
        self.assertEqual(len(self.fake_ses.sends), 2)
        self.assertIn(ACK_TEXT, self._reply_body(self.fake_ses.sends[-1]))

    def test_guard_plain_question_reaches_engine_unchanged(self):
        """(h) A plain new question with no quoting reaches the engine
        unchanged and is answered with exactly one reply."""
        question = "Which regions support the premium plan?"
        self.engine_responses = [{"answer": "All public regions."}]
        res = self.orchestrator.handler(
            make_ses_raw_event(
                JUDGE_SENDER, SUBJECT,
                plain_message(JUDGE_HEADER, SUBJECT, question),
            ),
            None,
        )
        self.assertEqual(res["statusCode"], 200)
        self.assertEqual(len(self.engine_calls), 1)
        payload = self._engine_payloads()[0]
        self.assertEqual(payload["question"], question)
        self.assertEqual(payload.get("feedback"), None)
        self.assertEqual(payload.get("session_id"), JUDGE_SESSION)
        self.assertEqual(len(self.fake_ses.sends), 1)
        send = self.fake_ses.sends[0]
        self.assertEqual(send["FromEmailAddress"], JUDGE_ADDRESS)
        self.assertEqual(send["Destination"]["ToAddresses"], [JUDGE_SENDER])
        self.assertIn("All public regions.", self._reply_body(send))
        self.assertIn("Rex, 3Dogs Nexus", self._reply_body(send))


if __name__ == "__main__":
    unittest.main()
