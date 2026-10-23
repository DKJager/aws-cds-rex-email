"""Inbound judge email: the message BODY must reach the engine.

SES receipt events delivered to Lambda carry headers only. The receipt
rule stores the raw MIME message in S3 first (infra/template.yaml,
S3Action with ObjectKeyPrefix inbound/), and src/channels.py fetches it
from SES_INBOUND_BUCKET + SES_INBOUND_PREFIX + mail.messageId.

These tests use a stub S3 client whose stored message has a body that
differs from the subject, so a subject-as-body regression cannot pass.
"""

import os
import sys
import unittest
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import MagicMock, patch

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_ENV = {
    "SES_FROM": "judges@cds.3dogs.ai",
    "SMS_ORIGINATION_ID": "test-orig-id",
    "NEXUS_ENDPOINT": "https://nexus.example.com/query",
    "NEXUS_TOKEN": "test-token",
    "SESSIONS_TABLE": "test-table",
    "AWS_REGION": "us-east-1",
    "DYNAMODB_REGION": "us-east-1",
    "SES_REGION": "us-east-1",
    "SMS_REGION": "us-east-1",
    "ALLOWED_DESTINATIONS": "judges@cds.3dogs.ai,customer@example.org",
    "SES_INBOUND_BUCKET": "inbound-test-bucket",
    "SES_INBOUND_PREFIX": "inbound/",
}

SUBJECT = "Quick question"
BODY = "Which three vendors had the largest invoice disputes in Q3?"
MESSAGE_ID = "o3vrnil0e2ic28trm7dfhrc2v0clambda4nbp0g1"
SENDER = "customer@example.org"


def _raw_mime():
    msg = EmailMessage()
    msg["From"] = SENDER
    msg["To"] = "judges@cds.3dogs.ai"
    msg["Subject"] = SUBJECT
    msg["Message-ID"] = "<%s@example.org>" % MESSAGE_ID
    msg.set_content(BODY)
    return msg.as_bytes()


def _ses_event():
    """A header-only SES receipt event, as SES delivers it to Lambda."""
    return {
        "Records": [
            {
                "eventSource": "aws:ses",
                "eventVersion": "1.0",
                "ses": {
                    "mail": {
                        "source": SENDER,
                        "messageId": MESSAGE_ID,
                        "destination": ["judges@cds.3dogs.ai"],
                        "commonHeaders": {
                            "from": [SENDER],
                            "to": ["judges@cds.3dogs.ai"],
                            "subject": SUBJECT,
                        },
                    },
                    "receipt": {"recipients": ["judges@cds.3dogs.ai"]},
                },
            }
        ]
    }


class _StubBody:
    def __init__(self, data):
        self._data = data

    def read(self, n=-1):
        return self._data if n is None or n < 0 else self._data[:n]


class _StubS3:
    def __init__(self, data):
        self.data = data
        self.calls = []

    def get_object(self, Bucket, Key):
        self.calls.append((Bucket, Key))
        if (Bucket, Key) != ("inbound-test-bucket", "inbound/" + MESSAGE_ID):
            raise KeyError("NoSuchKey: %s/%s" % (Bucket, Key))
        return {"Body": _StubBody(self.data)}


class S3InboundBodyTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, _ENV, clear=False)
        env.start()
        self.addCleanup(env.stop)
        from src import channels, orchestrator  # noqa: WPS433
        self.channels = channels
        self.orchestrator = orchestrator

    def _patch_s3(self, data):
        stub = _StubS3(data)
        p = patch.object(self.channels, "_get_s3_client", return_value=stub)
        p.start()
        self.addCleanup(p.stop)
        return stub

    def test_parser_reads_body_from_s3_not_subject(self):
        stub = self._patch_s3(_raw_mime())
        msg = self.channels.parse_inbound_event(_ses_event())
        self.assertIsNotNone(msg)
        self.assertEqual(msg.channel, "email")
        self.assertEqual(msg.text.strip(), BODY)
        self.assertNotEqual(msg.text.strip(), SUBJECT)
        self.assertEqual(stub.calls, [("inbound-test-bucket", "inbound/" + MESSAGE_ID)])

    def test_handler_sends_body_to_engine(self):
        self._patch_s3(_raw_mime())
        orch = self.orchestrator
        engine = MagicMock(return_value={"answer": "ok"})
        with patch.object(orch.nexus_client, "query_engine", engine), \
                patch.object(orch, "_safe_load_session", return_value=None), \
                patch.object(orch, "_safe_send_reply", return_value=True), \
                patch.object(orch, "sessions", MagicMock()):
            result = orch.handler(_ses_event(), None)
        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(engine.call_count, 1)
        question = engine.call_args.kwargs["question"]
        self.assertEqual(question.strip(), BODY)
        self.assertNotIn(SUBJECT, question)

    def test_negative_control_without_bucket_falls_back_to_subject(self):
        # Proves the stub is what supplies the body: with no bucket
        # configured the documented degraded path (subject) is used and
        # S3 is never called.
        stub = self._patch_s3(_raw_mime())
        with patch.dict(os.environ, {"SES_INBOUND_BUCKET": ""}):
            msg = self.channels.parse_inbound_event(_ses_event())
        self.assertEqual(msg.text.strip(), SUBJECT)
        self.assertEqual(stub.calls, [])

    def test_missing_object_degrades_to_subject(self):
        stub = self._patch_s3(_raw_mime())
        event = _ses_event()
        event["Records"][0]["ses"]["mail"]["messageId"] = "does-not-exist"
        msg = self.channels.parse_inbound_event(event)
        self.assertEqual(msg.text.strip(), SUBJECT)
        self.assertEqual(len(stub.calls), 1)


if __name__ == "__main__":
    unittest.main()
