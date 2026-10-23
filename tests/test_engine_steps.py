"""Test engine-step forwarding from engine reply into the Agent steps block.

The fixed orchestrator must interleave engine-reported step strings into
the Agent steps block after the ``engine`` orchestration line, prefixed
as ``engine: <text>``. This file asserts that the unfixed orchestrator
(WITHOUT those lines) fails the three ``test_fails_before_fix_*`` tests
and that the two ``test_guard_*`` tests pass on both versions.
"""

import base64
import json
import os
import re
import sys
import unicodedata
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
    "AGENT_STEPS": "1",
}

_REPO_ROOT = Path(__file__).resolve().parents[1]
JUDGE_ADDRESS = "judges@cds.3dogs.ai"
CUSTOMER_ADDRESS = "customer@example.org"


def _import_orchestrator():
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


def make_ses_email_event(sender, subject, body=None):
    """Build a synthetic SES inbound email event (base64 raw RFC 822)."""
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


class FakeDynamoTable(MagicMock):
    """Mock DynamoDB table that stores items in memory, supporting the
    exact flat SET/REMOVE UpdateExpression patterns the orchestrator emits."""

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
        **kwargs,
    ):
        session_id = Key["session_id"]
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
    """Records send_email calls for later inspection."""

    def __init__(self):
        super().__init__()
        self.sends = []

    def send_email(self, **kwargs):
        self.sends.append(kwargs)
        return {"MessageId": "mock-email-id"}


class TestEngineSteps(unittest.TestCase):
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
            if service == "pinpoint-sms-voice-v2":
                return MagicMock()
            raise ValueError("unexpected client: %s" % service)

        boto3.client.side_effect = _client_selector

        boto3_resource = orchestrator.sessions.boto3 = MagicMock()
        self.fake_table = FakeDynamoTable()
        boto3_resource.resource.return_value.Table.return_value = (
            self.fake_table
        )

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
                    "unexpected engine call #%d" % (len(self.engine_calls),)
                )
            resp = self.engine_responses.pop(0)
            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps(resp).encode("utf-8")
            return mock_resp

        self.urlopen_mock.side_effect = _urlopen_side_effect
        self.urlopen_mock.reset_mock()

    # ------------------------------------------------------------------
    # fails-before-fix tests (MUST FAIL on current code)

    def test_fails_before_fix_a_engine_steps_appear_in_agent_steps_block(self):
        """The engine returns answer steps; the SES reply body contains each
        engine-reported step string prefixed with 'engine: ' inside the
        Agent steps block, after the line for the `engine` orchestration
        step and before the line for `saved`."""
        self.engine_responses = [
            {
                "answer": "4",
                "steps": ["loaded 2 prior turns", "asked model m1"],
            }
        ]
        self.orchestrator.handler(
            make_ses_email_event(CUSTOMER_ADDRESS, "question?"), None
        )

        self.assertEqual(len(self.fake_ses.sends), 1)
        body = self.fake_ses.sends[0]["Content"]["Simple"]["Body"]["Text"]["Data"]

        # Both engine step strings must appear in the Agent steps block
        # prefixed with 'engine: '.
        self.assertIn("engine: loaded 2 prior turns", body)
        self.assertIn("engine: asked model m1", body)

        # Relative ordering: engine orchestration line < engine steps < saved line
        engine_line_pos = body.find("engine:")
        saved_line_pos = body.find("saved:")
        engine_step_a_pos = body.find("engine: loaded 2 prior turns")
        engine_step_b_pos = body.find("engine: asked model m1")

        self.assertGreater(engine_step_a_pos, engine_line_pos)
        self.assertGreater(engine_step_b_pos, engine_line_pos)
        self.assertLess(engine_step_a_pos, saved_line_pos)
        self.assertLess(engine_step_b_pos, saved_line_pos)

    def test_fails_before_fix_b_clarify_engine_steps_forwarded(self):
        """A clarify reply from the engine also forwards its steps prefixed
        with 'engine: ' into the Agent steps block."""
        self.engine_responses = [
            {"clarify": "Which year?", "steps": ["question is ambiguous"]}
        ]
        self.orchestrator.handler(
            make_ses_email_event(CUSTOMER_ADDRESS, "question?"), None
        )

        self.assertEqual(len(self.fake_ses.sends), 1)
        body = self.fake_ses.sends[0]["Content"]["Simple"]["Body"]["Text"]["Data"]
        self.assertIn("engine: question is ambiguous", body)

        engine_line_pos = body.find("engine:")
        saved_line_pos = body.find("saved:")
        step_pos = body.find("engine: question is ambiguous")
        self.assertGreater(step_pos, engine_line_pos)
        self.assertLess(step_pos, saved_line_pos)

    def test_fails_before_fix_c_step_truncation_and_line_limits(self):
        """With 11 engine steps, including a 500-char step, the text 's0'
        DOES appear as an engine step line, at most 6 engine step lines
        appear, each engine step is clipped to at most 120 characters,
        and no line in the reply body exceeds 200 characters. Control
        characters are stripped."""
        long_step = "A" * 500
        steps = ["s%d" % i for i in range(10)] + [long_step]
        self.engine_responses = [{"answer": "ok", "steps": steps}]
        self.orchestrator.handler(
            make_ses_email_event(CUSTOMER_ADDRESS, "question?"), None
        )

        self.assertEqual(len(self.fake_ses.sends), 1)
        body = self.fake_ses.sends[0]["Content"]["Simple"]["Body"]["Text"]["Data"]

        # s0 MUST appear as an engine step line (prefixed with 'engine: ')
        self.assertIn("engine: s0", body)

        # At most 6 engine-step lines (the block line-count cap).
        engine_step_lines = [
            line for line in body.splitlines() if line.startswith("engine: ")
        ]
        self.assertLessEqual(len(engine_step_lines), 6)

        # Each engine step string must be at most 120 characters.
        for line in engine_step_lines:
            text = line.split("engine: ", 1)[1] if "engine: " in line else ""
            self.assertLessEqual(len(text), 120)

        # The 500-character step must never appear in full.
        self.assertNotIn("engine: " + long_step, body)

        # No line in the whole body exceeds 200 characters.
        for line in body.splitlines():
            self.assertLessEqual(
                len(line), 200,
                "line too long: %r" % line[:210],
            )

    # --- guards (must pass on current code and keep passing) -----------------

    def test_guard_d_orchestrator_lines_present_in_order_with_signature(self):
        """When the engine returns no steps, the six orchestrator lines are
        present in order and the Rex signature appears."""
        self.engine_responses = [{"answer": "4"}]  # no steps key at all
        self.orchestrator.handler(
            make_ses_email_event(CUSTOMER_ADDRESS, "question?"), None
        )

        self.assertEqual(len(self.fake_ses.sends), 1)
        body = self.fake_ses.sends[0]["Content"]["Simple"]["Body"]["Text"]["Data"]
        self.assertIn("received:", body)
        self.assertIn("session:", body)
        self.assertIn("feedback:", body)
        self.assertIn("engine:", body)
        self.assertIn("saved:", body)
        self.assertIn("reply:", body)
        self.assertIn("Rex, 3Dogs Nexus", body)

        pos_received = body.find("received:")
        pos_session = body.find("session:")
        pos_feedback = body.find("feedback:")
        pos_engine = body.find("engine:")
        pos_saved = body.find("saved:")
        pos_reply = body.find("reply:")
        self.assertLess(pos_received, pos_session)
        self.assertLess(pos_session, pos_feedback)
        self.assertLess(pos_feedback, pos_engine)
        self.assertLess(pos_engine, pos_saved)
        self.assertLess(pos_saved, pos_reply)

    def test_guard_e_non_string_steps_handled_gracefully(self):
        """Engine steps containing non-strings ([1, null, {"a":1}]) are
        handled gracefully; the reply is sent exactly once without
        exception."""
        self.engine_responses = [
            {"answer": "ok", "steps": [1, None, {"a": 1}]}
        ]
        self.orchestrator.handler(
            make_ses_email_event(CUSTOMER_ADDRESS, "question?"), None
        )

        self.assertEqual(len(self.fake_ses.sends), 1)
        body = self.fake_ses.sends[0]["Content"]["Simple"]["Body"]["Text"]["Data"]
        self.assertIn("Rex, 3Dogs Nexus", body)


if __name__ == "__main__":
    unittest.main()
