"""Offline end-to-end checks that tie the template, the Lambda code and the
channels together.  Nothing here touches the network or AWS.

These tests exist because unit tests that mock each module in isolation
cannot see contract mismatches between them (Lambda package layout, the
DynamoDB key schema, the environment the template really provides).
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src import channels, nexus_client, orchestrator, sessions  # noqa: E402

TEMPLATE = (ROOT / "infra" / "template.yaml").read_text(encoding="utf-8")


def _section(name, next_name):
    start = TEMPLATE.index(f"\n  {name}:\n")
    end = TEMPLATE.index(f"\n  {next_name}:\n", start + 1)
    return TEMPLATE[start:end]


class FakeTable:
    """DynamoDB table stand-in that enforces the key schema declared in the template."""

    def __init__(self, key_names):
        self.key_names = set(key_names)
        self.items = {}

    def _check(self, key):
        if set(key) != self.key_names:
            raise AssertionError(
                f"Key {sorted(key)} does not match table key schema {sorted(self.key_names)}"
            )
        return key["session_id"]

    def get_item(self, Key, **_):
        item = self.items.get(self._check(Key))
        return {"Item": dict(item)} if item else {}

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues, **_):
        item = self.items.setdefault(self._check(Key), {"session_id": Key["session_id"]})
        body = UpdateExpression.strip()
        assert body.startswith("SET "), body
        for target, rhs in re.findall(
            r"(\w+) = (if_not_exists\(\w+, :\w+\) \+ :\w+|:\w+)", body[4:]
        ):
            if rhs.startswith("if_not_exists"):
                zero, one = re.findall(r":\w+", rhs)
                item[target] = item.get(target, ExpressionAttributeValues[zero]) + ExpressionAttributeValues[one]
            else:
                item[target] = ExpressionAttributeValues[rhs]
        return {}


class FakeSes:
    def __init__(self):
        self.sent = []

    def send_email(self, **kwargs):
        import botocore.session
        from botocore.validate import validate_parameters

        shape = botocore.session.get_session().get_service_model("sesv2").operation_model("SendEmail").input_shape
        validate_parameters(kwargs, shape)  # real parameter contract of SESv2 SendEmail
        self.sent.append(kwargs)
        return {"MessageId": "m-%d" % len(self.sent)}


def ses_event(sender, subject="question", auto=False):
    headers = [{"name": "Auto-Submitted", "value": "auto-replied"}] if auto else []
    return {"Records": [{"ses": {"mail": {
        "source": sender, "messageId": "abc", "headers": headers,
        "commonHeaders": {"from": [sender], "subject": subject}}}}]}


class TestLambdaPackageLayout(unittest.TestCase):
    def test_modules_import_when_src_is_the_zip_root(self):
        """CodeUri: src/ places the modules at the zip root; the handler must still import."""
        with tempfile.TemporaryDirectory() as tmp:
            for py in (ROOT / "src").glob("*.py"):
                Path(tmp, py.name).write_text(py.read_text())
            code = ("import sys; sys.path.insert(0, sys.argv[1]); import orchestrator; "
                    "print(callable(orchestrator.handler))")
            out = subprocess.run([sys.executable, "-I", "-c", code, tmp], capture_output=True, text=True, cwd=tmp)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertIn("True", out.stdout)

    def test_negative_control_package_style_import_fails_in_that_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "m.py").write_text("from src import channels\n")
            code = "import sys; sys.path.insert(0, sys.argv[1]); import m"
            out = subprocess.run([sys.executable, "-I", "-c", code, tmp], capture_output=True, text=True, cwd=tmp)
            self.assertNotEqual(out.returncode, 0)


class TestTemplateContract(unittest.TestCase):
    def test_sessions_table_key_schema_matches_code(self):
        block = _section("SessionsTable", "InboundMailBucket")
        keys = re.findall(r"KeySchema:(.*?)TimeToLiveSpecification", block, re.S)[0]
        names = re.findall(r"AttributeName: (\w+)", keys)
        self.assertEqual(names, ["session_id"])
        table = FakeTable(names)
        with mock.patch.object(sessions, "_table", return_value=table):
            sessions.save_session("email:a@example.org", "q", "a")
            self.assertEqual(sessions.load_session("email:a@example.org")["last_answer"], "a")

    def test_negative_control_composite_key_is_detected(self):
        table = FakeTable(["session_id", "channel"])
        with mock.patch.object(sessions, "_table", return_value=table):
            with self.assertRaises(Exception):
                sessions.save_session("email:a@example.org", "q", "a")

    def test_template_supplies_the_environment_replies_need(self):
        env = dict(re.findall(r"^\s{10}([A-Z_]+): (\S+)$", TEMPLATE, re.M))
        self.assertEqual(env["SES_FROM"], "judges@cds.3dogs.ai")
        self.assertEqual(env["AGENT_STEPS"], '"1"')
        with mock.patch.dict(os.environ, {"ALLOWED_DESTINATIONS": env["ALLOWED_DESTINATIONS"]}):
            self.assertIn("judges@cds.3dogs.ai", channels.allowed_destinations())

    def test_rule_set_has_the_fixed_name_the_deploy_tool_activates(self):
        block = _section("InboundEmailRuleSet", "InboundEmailRule")
        self.assertIn("RuleSetName: awscds-inbound", block)

    def test_no_bare_wildcard_in_template(self):
        code = "\n".join(l for l in TEMPLATE.splitlines() if not l.strip().startswith("#"))
        self.assertNotIn('"*"', code)
        self.assertNotRegex(code, r"(?m)^\s*(Action|Resource):\s*['\"]?\*['\"]?\s*$")


class TestEmailRoundTrip(unittest.TestCase):
    def setUp(self):
        self.table = FakeTable(["session_id"])
        self.ses = FakeSes()
        self.engine_calls = []

        def fake_engine(**kwargs):
            self.engine_calls.append(kwargs)
            return {"answer": "The answer is 4."}

        env = {"SES_FROM": "judges@cds.3dogs.ai", "ALLOWED_DESTINATIONS": "judges@cds.3dogs.ai",
               "SESSIONS_TABLE": "t", "AWS_REGION": "us-east-1", "AGENT_STEPS": "1"}
        patches = [
            mock.patch.dict(os.environ, env),
            mock.patch.object(sessions, "_table", return_value=self.table),
            mock.patch.object(channels, "_get_ses_client", return_value=self.ses),
            mock.patch.object(channels, "_fetch_ses_body_from_s3", return_value="What is 2+2?"),
            mock.patch.object(nexus_client, "query_engine", side_effect=fake_engine),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_two_turns_reply_with_steps_and_a_session_record(self):
        for turn in (1, 2):
            result = orchestrator.handler(ses_event("owner@example.org"), None)
            self.assertEqual(result["statusCode"], 200)
        self.assertEqual(len(self.ses.sent), 2)
        first, second = (m["Content"]["Simple"]["Body"]["Text"]["Data"] for m in self.ses.sent)
        for body in (first, second):
            self.assertIn("The answer is 4.", body)
            self.assertIn("Agent steps:", body)
            self.assertIn("-- Rex, 3Dogs Nexus", body)
        self.assertIn("new conversation", first)
        self.assertIn("returning conversation, 1 earlier turn", second)
        self.assertEqual(self.ses.sent[0]["Destination"]["ToAddresses"], ["owner@example.org"])
        self.assertEqual(self.ses.sent[0]["FromEmailAddress"], "judges@cds.3dogs.ai")
        item = self.table.items["email:owner@example.org"]
        self.assertEqual(item["turn_count"], 2)
        self.assertEqual(item["conv_channel"], "email")
        steps = json.loads(item["last_steps"])
        self.assertEqual([s["step"] for s in steps][:2], ["received", "session"])
        self.assertIn("engine", [s["step"] for s in steps])

    def test_steps_block_is_off_unless_enabled(self):
        with mock.patch.dict(os.environ, {"AGENT_STEPS": "0"}):
            orchestrator.handler(ses_event("owner@example.org"), None)
        body = self.ses.sent[0]["Content"]["Simple"]["Body"]["Text"]["Data"]
        self.assertNotIn("Agent steps:", body)

    def test_our_own_reply_coming_back_is_ignored(self):
        orchestrator.handler(ses_event("owner@example.org"), None)
        echoed = self.ses.sent[0]["Content"]["Simple"]["Body"]["Text"]["Data"]
        self.ses.sent.clear()
        self.engine_calls.clear()
        with mock.patch.object(channels, "_fetch_ses_body_from_s3", return_value=echoed):
            result = orchestrator.handler(ses_event("judges@cds.3dogs.ai"), None)
        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(self.ses.sent, [])
        self.assertEqual(self.engine_calls, [])

    def test_negative_control_loop_guard_is_what_stops_it(self):
        orchestrator.handler(ses_event("judges@cds.3dogs.ai"), None)  # same sender, no signature in the body
        self.assertEqual(len(self.engine_calls), 1)

    def test_auto_submitted_header_is_ignored(self):
        orchestrator.handler(ses_event("owner@example.org", auto=True), None)
        self.assertEqual(self.ses.sent, [])
        self.assertEqual(self.engine_calls, [])

    def test_automated_senders_are_ignored(self):
        for sender in ("mailer-daemon@example.org", "no-reply@example.org", "bounce@amazonses.com"):
            orchestrator.handler(ses_event(sender), None)
        self.assertEqual(self.ses.sent, [])


if __name__ == "__main__":
    unittest.main()
