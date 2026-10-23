"""Test the first-contact feedback guard (committee #1475 clobber test).

Verifies that the feedback branch in src/orchestrator.py handles a
ConditionalCheckFailedException from sessions.save_feedback by creating
the session record then retrying, while transient DynamoDB errors on an
existing session do NOT trigger the create-then-retry path and therefore
do not overwrite last_answer with "".
"""

import base64
import json
import os
import re
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Environment required by src/orchestrator at import time.
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
CUSTOMER_ADDRESS = "customer@example.org"


def _import_orchestrator():
    repo_root_str = str(_REPO_ROOT)
    if repo_root_str not in sys.path:
        sys.path.insert(0, repo_root_str)
    from src import orchestrator
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

    Enforces attribute_exists(session_id) by raising a botocore
    ClientError with code "ConditionalCheckFailedException" (the same
    exception type the real code path raises).  Can inject transient
    non-conditional errors on update_item on demand.

    Expression parser handles the exact patterns the orchestrator emits:
    simple SET/REMOVE assignments with ExpressionAttributeNames/Values
    and the if_not_exists function used in save_session.
    """

    _SET_ASSIGNMENT_RE = re.compile(
        r"(#[A-Za-z0-9_]+|[A-Za-z_][A-Za-z0-9_.]*)\s*=\s*"
        r"(if_not_exists\(\s*(#[A-Za-z0-9_]+|[A-Za-z_][A-Za-z0-9_.]*)\s*,\s*(:[A-Za-z0-9_]+)\s*\)|(:[A-Za-z0-9_]+))"
    )
    _REMOVE_TOKEN_RE = re.compile(r"#[A-Za-z0-9_]+|[A-Za-z_][A-Za-z0-9_.]*")

    def __init__(self):
        super().__init__()
        self._items = {}
        self._raise_transient_on_update = False

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

        # Enforce attribute_exists(session_id) FIRST, before any
        # transient error injection, so a missing session always raises
        # the conditional failure and transient errors only fire for
        # existing sessions.
        if ConditionExpression == "attribute_exists(session_id)":
            if session_id not in self._items:
                from botocore.exceptions import ClientError
                error_response = {
                    "Error": {
                        "Code": "ConditionalCheckFailedException",
                        "Message": "The conditional request failed (simulated)",
                    }
                }
                raise ClientError(error_response, "UpdateItem")

        # Transient error injection (non-conditional) for existing
        # sessions.  The current orchestrator code wraps save_feedback
        # in a bare `except Exception`, which catches this transient
        # error and triggers the create-session-then-retry path.  That
        # path calls save_session(..., ""), overwriting last_answer.
        # The test must therefore surface this defect by raising a
        # *non-conditional* ClientError that is caught by the broad
        # `except Exception` branch.
        if self._raise_transient_on_update:
            self._raise_transient_on_update = False
            from botocore.exceptions import ClientError
            error_response = {
                "Error": {
                    "Code": "ProvisionedThroughputExceededException",
                    "Message": "Rate exceeded (simulated)",
                }
            }
            raise ClientError(error_response, "UpdateItem")

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
                value_expr = match.group(2)
                if value_expr.startswith("if_not_exists"):
                    existing_attr = _resolve(match.group(3))
                    default_val_key = match.group(4)
                    if default_val_key not in values:
                        raise ValueError("unmapped expression attribute value: %s" % default_val_key)
                    item[field] = item.get(existing_attr, values[default_val_key])
                else:
                    if value_expr not in values:
                        raise ValueError("unmapped expression attribute value: %s" % value_expr)
                    item[field] = values[value_expr]
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
    """Records send_email calls for later inspection."""

    def __init__(self):
        super().__init__()
        self.sends = []

    def send_email(self, **kwargs):
        self.sends.append(kwargs)
        return {"MessageId": "mock-email-id"}


class TestFeedbackGuard(unittest.TestCase):
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

        # Stub boto3 for channels
        boto3_channels = orchestrator.channels.boto3 = MagicMock()
        self.fake_ses = FakeSesClient()
        boto3_channels.client.return_value = self.fake_ses

        # Stub boto3 resource for sessions
        boto3_resource = orchestrator.sessions.boto3 = MagicMock()
        self.fake_table = FakeDynamoTable()
        boto3_resource.resource.return_value.Table.return_value = self.fake_table

        self.engine_calls = []
        self.fake_table._items.clear()
        self.fake_ses.sends.clear()
        self.engine_calls.clear()

        def _urlopen_side_effect(*args, **kwargs):
            request = args[0] if args else None
            self.engine_calls.append(request)
            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps({"answer": "OK"}).encode("utf-8")
            return mock_resp

        self.urlopen_mock.side_effect = _urlopen_side_effect
        self.urlopen_mock.reset_mock()

    def _set_session(self, session_id, **attrs):
        self.fake_table._items[session_id] = dict(session_id=session_id, **attrs)

    def _get_session(self, session_id):
        return self.fake_table.get_item(Key={"session_id": session_id}).get("Item")

    # --- tests that MUST FAIL on the current code ---

    def test_fails_before_fix_a_session_state_loss_on_transient_error(self):
        """Existing sender with a stored session whose last_answer is "A1":
        the table raises a transient (non-conditional) error on
        update_item; mail "that's wrong" -> handler returns without
        raising, exactly one SES reply (starting "Thanks - noted"), and
        the stored session's last_answer is STILL "A1" (never overwritten
        with "").
        """
        self._set_session(
            "email:customer@example.org",
            last_question="Q1",
            last_answer="A1",
        )
        self.fake_table._raise_transient_on_update = True

        event = make_ses_email_event(CUSTOMER_ADDRESS, "that's wrong")
        res = self.orchestrator.handler(event, None)

        # Handler must return 200 without raising
        self.assertEqual(res["statusCode"], 200)
        # Exactly one acknowledegment reply sent
        self.assertEqual(len(self.fake_ses.sends), 1)
        self.assertIn(
            "Thanks - noted",
            self.fake_ses.sends[0]["Content"]["Simple"]["Body"]["Text"]["Data"],
        )
        # Stored session's last_answer must still be "A1"
        session = self._get_session("email:customer@example.org")
        self.assertIsNotNone(session)
        self.assertEqual(
            session.get("last_answer"),
            "A1",
            "Transient error must not overwrite last_answer with ''",
        )

    def test_fails_before_fix_b_save_session_not_called_on_transient_error(self):
        """Existing sender with stored session, transient error on
        update_item: sessions.save_session is NOT called at all (spy on
        the real function object so we can observe whether the
        orchestrator invoked it).
        """
        self._set_session(
            "email:customer@example.org",
            last_question="Q1",
            last_answer="A1",
        )
        self.fake_table._raise_transient_on_update = True

        # Spy on the sessions.save_session *function* that the
        # orchestrator imports and calls.  This is an observable call
        # on the sessions module, not a private helper of the
        # orchestrator.
        from src import sessions
        with patch.object(
            sessions, "save_session", wraps=sessions.save_session
        ) as spy:
            event = make_ses_email_event(CUSTOMER_ADDRESS, "that's wrong")
            self.orchestrator.handler(event, None)
            spy.assert_not_called()

    # --- tests that MUST PASS on current code and after fix ---

    def test_guard_c_new_sender_first_feedback_creates_then_retries(self):
        """Brand-new sender, first mail 'that's wrong': no exception,
        exactly one SES reply, a session record now exists and holds the
        feedback (the conditional-failure path creates then retries).
        """
        event = make_ses_email_event(CUSTOMER_ADDRESS, "that's wrong")
        res = self.orchestrator.handler(event, None)

        self.assertEqual(res["statusCode"], 200)
        self.assertEqual(len(self.fake_ses.sends), 1)
        self.assertIn(
            "Thanks - noted",
            self.fake_ses.sends[0]["Content"]["Simple"]["Body"]["Text"]["Data"],
        )

        session = self._get_session("email:customer@example.org")
        self.assertIsNotNone(session, "Session record must exist after first-contact feedback")
        self.assertIn("pending_feedback", session, "Feedback must be stored on the new session")

    def test_guard_d_existing_sender_normal_feedback_stores_on_existing(self):
        """Existing sender, normal 'that's wrong': acknowledgement sent and
        feedback stored on the existing record, last_answer unchanged.
        """
        self._set_session(
            "email:customer@example.org",
            last_question="Q1",
            last_answer="A1",
        )
        event = make_ses_email_event(CUSTOMER_ADDRESS, "that's wrong")
        res = self.orchestrator.handler(event, None)

        self.assertEqual(res["statusCode"], 200)
        self.assertEqual(len(self.fake_ses.sends), 1)
        self.assertIn(
            "Thanks - noted",
            self.fake_ses.sends[0]["Content"]["Simple"]["Body"]["Text"]["Data"],
        )

        session = self._get_session("email:customer@example.org")
        self.assertIsNotNone(session)
        self.assertIn("pending_feedback", session)
        self.assertEqual(session.get("last_answer"), "A1",
                         "Existing last_answer must not be overwritten by normal feedback")

    def test_guard_e_new_sender_normal_question_calls_engine(self):
        """Brand-new sender asks a normal question: the engine is called
        and the answer is sent (guard does not interfere).
        """
        event = make_ses_email_event(CUSTOMER_ADDRESS, "What is the weather?")
        res = self.orchestrator.handler(event, None)

        self.assertEqual(res["statusCode"], 200)
        self.assertEqual(
            len(self.engine_calls), 1,
            "Engine must be called for a non-feedback question",
        )
        self.assertEqual(len(self.fake_ses.sends), 1,
                         "Answer reply must be sent")


if __name__ == "__main__":
    unittest.main()
