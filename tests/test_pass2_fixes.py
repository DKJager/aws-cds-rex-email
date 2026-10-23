"""Test pass-2 fixes for C38 (D1) and C39 (D2).

C38: first-contact "that's wrong" crashes Lambda.
C39: long engine answers silently produce no reply.

Uses the same fakes, path setup, and SES-event construction as
tests/test_orchestrator.py. The fake DynamoDB table in this file
ENFORCES attribute_exists(session_id) conditions for update_item.
"""

import base64
import json
import os
import re
import sys
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, patch

_REPO_ROOT = Path(__file__).resolve().parents[1]

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

JUDGE_ADDRESS = "judges@cds.3dogs.ai"
CUSTOMER_ADDRESS = "customer@example.org"


def _import_orchestrator():
    repo_root_str = str(_REPO_ROOT)
    if repo_root_str not in sys.path:
        sys.path.insert(0, repo_root_str)
    try:
        from src import orchestrator
    except ImportError:
        src_str = str(_REPO_ROOT / "src")
        if src_str not in sys.path:
            sys.path.insert(0, src_str)
        import orchestrator
    else:
        return orchestrator
    return orchestrator


def make_ses_email_event(sender, subject, body=None):
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
    encoded_content = base64.b64encode(raw_message.encode("utf-8")).decode("ascii")
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


class ConditionalCheckFailedException(Exception):
    """Simulates a DynamoDB ConditionalCheckFailedException."""

    def __init__(self):
        super().__init__("The conditional request failed")
        self.response = {
            "Error": {"Code": "ConditionalCheckFailedException", "Message": "The conditional request failed"}
        }


class ConditionalFakeDynamoTable(MagicMock):
    """Fake DynamoDB table that enforces attribute_exists condition.

    update_item raises ConditionalCheckFailedException when the
    ConditionExpression is 'attribute_exists(session_id)' and the item
    does not exist.  All other operations are identical to FakeDynamoTable.
    """

    _SET_ASSIGNMENT_RE = re.compile(r"(#[A-Za-z0-9_]+|[A-Za-z_][A-Za-z0-9_.]*)\s*=\s*(:[A-Za-z0-9_]+)")
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

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues=None,
                    ExpressionAttributeNames=None, ConditionExpression=None, **kwargs):
        session_id = Key["session_id"]
        if ConditionExpression == "attribute_exists(session_id)":
            if session_id not in self._items:
                raise ConditionalCheckFailedException()

        item = self._items.get(session_id, {"session_id": session_id})
        names = dict(ExpressionAttributeNames or {})
        values = dict(ExpressionAttributeValues or {})

        def _resolve(token):
            token = token.strip()
            if token.startswith("#"):
                if token not in names:
                    raise ValueError("unmapped expression attribute name: %s" % token)
                return names[token]
            if token.startswith(":"):
                raise ValueError("value token used as attribute name: %s" % token)
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
                    raise ValueError("unmapped expression attribute value: %s" % value_key)
                item[field] = values[value_key]
            if not matched_any and set_clause.strip():
                raise ValueError("could not parse SET clause: %s" % set_clause)
        if "REMOVE" in UpdateExpression:
            remove_clause = UpdateExpression.split("REMOVE", 1)[1]
            matched_any = False
            for match in self._REMOVE_TOKEN_RE.finditer(remove_clause):
                matched_any = True
                field = _resolve(match.group(0))
                item.pop(field, None)
            if not matched_any and remove_clause.strip():
                raise ValueError("could not parse REMOVE clause: %s" % remove_clause)
        self._items[session_id] = item


class FakeSesClient(MagicMock):
    def __init__(self):
        super().__init__()
        self.sends = []

    def send_email(self, **kwargs):
        self.sends.append(kwargs)
        return {"MessageId": "mock-email-id"}


class TestPass2Fixes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._env_patch = patch.dict(os.environ, _TEST_ENV)
        cls._env_patch.start()
        cls.addClassCleanup(cls._env_patch.stop)

        cls.orchestrator = _import_orchestrator()

        cls._urlopen_patch = patch("%s.urlopen" % cls.orchestrator.__name__)
        cls.urlopen_mock = cls._urlopen_patch.start()
        cls.addClassCleanup(cls._urlopen_patch.stop)

    def setUp(self):
        orchestrator = self.orchestrator
        boto3 = orchestrator.channels.boto3 = MagicMock()
        self.fake_ses = FakeSesClient()

        def _client_selector(service, *args, **kwargs):
            if service == "sesv2":
                return self.fake_ses
            raise ValueError("unexpected client: %s" % service)

        boto3.client.side_effect = _client_selector

        boto3_resource = orchestrator.sessions.boto3 = MagicMock()
        self.fake_table = ConditionalFakeDynamoTable()
        boto3_resource.resource.return_value.Table.return_value = self.fake_table

        self.engine_calls = []
        self.engine_responses = []
        self.fake_table._items.clear()
        self.fake_ses.sends.clear()
        self.engine_calls.clear()
        self.engine_responses.clear()

        def _urlopen_side_effect(*args, **kwargs):
            request = args[0] if args else None
            self.engine_calls.append(request)
            if not self.engine_responses:
                raise AssertionError(
                    "unexpected engine call #%d: the orchestrator made "
                    "more engine requests than the test queued responses for (%d call(s) recorded)"
                    % (len(self.engine_calls), len(self.engine_calls))
                )
            resp = self.engine_responses.pop(0)
            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps(resp).encode("utf-8")
            return mock_resp

        self.urlopen_mock.side_effect = _urlopen_side_effect
        self.urlopen_mock.reset_mock()

    # --- D1 (C38) tests -------------------------------------------------------

    def test_fails_before_fix_a_first_contact_thats_wrong_no_exception(self):
        """Brand-new sender's first mail is 'that's wrong': handler returns
        normally (no exception) and exactly one SES reply is sent."""
        event = make_ses_email_event(CUSTOMER_ADDRESS, "that's wrong", body="that's wrong")
        result = self.orchestrator.handler(event, None)
        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(len(self.fake_ses.sends), 1)

    def test_fails_before_fix_b_reply_is_acknowledgement(self):
        """Same as (a) but the reply body starts with the acknowledgement text."""
        event = make_ses_email_event(CUSTOMER_ADDRESS, "that's wrong", body="that's wrong")
        self.orchestrator.handler(event, None)
        self.assertEqual(len(self.fake_ses.sends), 1)
        body = self.fake_ses.sends[0]["Content"]["Simple"]["Body"]["Text"]["Data"]
        self.assertTrue(body.startswith("Thanks - noted"), "expected 'Thanks - noted', got: %s" % body[:50])

    # --- D2 (C39) test --------------------------------------------------------

    def test_fails_before_fix_c_long_answer_truncated(self):
        """Engine returns 5000 characters: exactly one SES reply is sent,
        body contains the start of the answer and either ends with
        'Rex, 3Dogs Nexus' or contains '[truncated]'."""
        long_answer = "A" * 5000
        self.engine_responses = [{"answer": long_answer}]
        event = make_ses_email_event(CUSTOMER_ADDRESS, "Long question?", body="Give me a very long answer")
        result = self.orchestrator.handler(event, None)
        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(len(self.fake_ses.sends), 1)
        body = self.fake_ses.sends[0]["Content"]["Simple"]["Body"]["Text"]["Data"]
        # body must contain the start of the answer (first 100 chars)
        self.assertIn("A" * 100, body)
        # assert either ends with signature or contains [truncated]
        has_signature = body.rstrip().endswith("Rex, 3Dogs Nexus")
        has_truncated = "[truncated]" in body
        self.assertTrue(has_signature or has_truncated,
                        "body must end with 'Rex, 3Dogs Nexus' or contain '[truncated]'")

    # --- Guard tests (must pass on current code) ------------------------------

    def test_guard_e_existing_sender_thats_wrong_gets_ack_and_stored(self):
        """Existing sender who writes 'that's wrong' gets acknowledgement
        and the correction is stored on the session."""
        # Turn 1: engine returns an answer
        self.engine_responses = [{"answer": "X is Y."}]
        event1 = make_ses_email_event(CUSTOMER_ADDRESS, "What is X?", body="What is X?")
        self.orchestrator.handler(event1, None)
        self.assertEqual(len(self.engine_calls), 1)

        # Turn 2: "that's wrong"
        self.engine_responses = []
        event2 = make_ses_email_event(CUSTOMER_ADDRESS, "that's wrong", body="that's wrong")
        result = self.orchestrator.handler(event2, None)
        self.assertEqual(result["statusCode"], 200)
        # exactly one SES reply sent
        self.assertEqual(len(self.fake_ses.sends), 2)  # one from turn 1, one from turn 2
        last_body = self.fake_ses.sends[-1]["Content"]["Simple"]["Body"]["Text"]["Data"]
        self.assertTrue(last_body.startswith("Thanks - noted"))

        # session must have pending_feedback
        session = self.fake_table.get_item(Key={"session_id": "email:" + CUSTOMER_ADDRESS}).get("Item")
        self.assertIsNotNone(session)
        self.assertIn("pending_feedback", session)

    def test_guard_f_normal_short_question_gets_full_reply(self):
        """A normal short question gets the engine answer and the signature in the reply."""
        self.engine_responses = [{"answer": "Here is your answer."}]
        event = make_ses_email_event(CUSTOMER_ADDRESS, "Short question?", body="Short question body")
        result = self.orchestrator.handler(event, None)
        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(len(self.fake_ses.sends), 1)
        body = self.fake_ses.sends[0]["Content"]["Simple"]["Body"]["Text"]["Data"]
        self.assertIn("Here is your answer.", body)
        self.assertIn("Rex, 3Dogs Nexus", body)


if __name__ == "__main__":
    unittest.main()
