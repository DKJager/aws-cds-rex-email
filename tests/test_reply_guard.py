"""Test suite for the reply-target guard in src/channels.py.

Implements tests (a)-(j) from the SPEC and covers all rules 1-7.
These tests must FAIL on the current (unfixed) src/channels.py where
the old behaviour differs (the vulnerable code prefers Reply-To, then From).

Patterns copied from existing tests:
- sys.path setup from test_s3_inbound.py
- SES event structure from test_orchestrator.py _ses_event
- Mock client setup from test_orchestrator.py setUp
"""

import base64
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Add repo root and src/ to sys.path exactly like test_s3_inbound.py
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
# Also ensure src/ is importable (some layouts need this)
if str(_REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "src"))

# Import channels module after path setup
from src import channels

# Test environment matching existing patterns from test_orchestrator.py
_TEST_ENV = {
    "SES_FROM": "judges@cds.3dogs.ai",
    "SMS_ORIGINATION_ID": "test-orig-id",
    "ALLOWED_DESTINATIONS": "judges@cds.3dogs.ai,+15551234567",
    "AWS_REGION": "us-east-1",
    "SES_REGION": "us-east-1",
    "SMS_REGION": "us-east-1",
    "DYNAMODB_REGION": "us-east-1",  # Needed by orchestrator imports
    "NEXUS_ENDPOINT": "https://nexus.example.com/query",
    "NEXUS_TOKEN": "test-token",
    "SESSIONS_TABLE": "test-table",
}


def _ses_event(source, from_headers=None, replyto_headers=None,
               subject="Test", spf_status="PASS", dkim_status="PASS",
               body_text="Test body"):
    """Build a synthetic SES event exactly like test_orchestrator.py's _ses_event.

    Args match the existing helper but add SPF/DKIM verdicts per the SPEC.
    The 'receipt' field is placed inside 'ses' next to 'mail' as required.
    """
    if from_headers is None:
        from_headers = [source]
    if replyto_headers is None:
        replyto_headers = []

    # Build minimal MIME message exactly like test_orchestrator.py
    raw_message = (
        "From: {sender}\r\n"
        "To: {judge}\r\n"
        "Subject: {subject}\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        "\r\n"
        "{body}\r\n"
    ).format(
        sender=from_headers[0] if from_headers else source,
        judge="judges@cds.3dogs.ai",
        subject=subject,
        body=body_text,
    ).encode('utf-8')

    encoded_content = base64.b64encode(raw_message).decode('ascii')

    # Structure matches exactly: receipt inside ses next to mail
    return {
        "Records": [{
            "ses": {
                "mail": {
                    "source": source,
                    "commonHeaders": {
                        "from": from_headers,
                        "replyTo": replyto_headers,
                        "subject": subject,
                    },
                },
                "receipt": {
                    "spfVerdict": {"status": spf_status},
                    "dkimVerdict": {"status": dkim_status},
                },
                "content": encoded_content,
            }
        }]
    }


class TestReplyGuard(unittest.TestCase):
    """Test the reply-target guard rules 1-7."""

    def setUp(self):
        # Patch environment exactly like test_orchestrator.py setUpClass
        self.env_patch = patch.dict(os.environ, _TEST_ENV)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

        # Mock SES client to record send_email calls (like FakeSesClient)
        self.ses_sends = []
        self.mock_ses_client = MagicMock()
        self.mock_ses_client.send_email = MagicMock(
            side_effect=lambda **kwargs: self.ses_sends.append(kwargs) or {"MessageId": "test"}
        )

        # Mock SMS client (not used but needed for imports to work)
        self.mock_sms_client = MagicMock()
        self.mock_sms_client.send_text_message = MagicMock(return_value={"MessageId": "test"})

        # Patch boto3.client exactly like test_orchestrator.py setUp
        self.boto3_patch = patch.object(channels, 'boto3')
        mock_boto3 = self.boto3_patch.start()
        self.addCleanup(self.boto3_patch.stop)

        def client_selector(service, *args, **kwargs):
            if service == "sesv2":
                return self.mock_ses_client
            if service == "pinpoint-sms-voice-v2":
                return self.mock_sms_client
            raise ValueError(f"Unexpected service: {service}")

        mock_boto3.client.side_effect = client_selector

    def _clear_sends(self):
        """Clear recorded send calls."""
        self.ses_sends.clear()
        self.mock_ses_client.reset_mock()

    def test_a_replyto_third_party_source_attacker_sends_to_attacker(self):
        """(a) Reply-To=third party, From=SOURCE=attacker -> reply only to attacker.
        
        Rule 2: SOURCE is non-empty and not OWN -> reply target is SOURCE.
        Reply-To and From are ignored unless equal to SOURCE.
        Must fail on unfixed code (prefers Reply-To).
        """
        event = _ses_event(
            source="attacker@evil.test",
            from_headers=["attacker@evil.test"],
            replyto_headers=["victim@third.test"],
            spf_status="PASS",
            dkim_status="PASS"
        )

        msg = channels.parse_inbound_event(event)
        self.assertIsNotNone(msg)
        result = channels.send_reply(msg, "Test reply")

        # Should send to attacker (SOURCE), not to victim (Reply-To)
        self.assertIsNotNone(result)
        self.assertEqual(len(self.ses_sends), 1)
        send = self.ses_sends[0]
        to_addrs = send["Destination"]["ToAddresses"]
        # Only assert the destination we want; will fail on unfixed code
        # because unfixed code sends to victim@third.test
        self.assertEqual(to_addrs[0].lower(), "attacker@evil.test")
        # Additional safety: verify the mock was never called with victim address
        for send_call in self.ses_sends:
            self.assertNotIn("victim@third.test", str(send_call).lower())

    def test_b_forged_from_third_party_source_attacker_sends_to_attacker(self):
        """(b) forged From=third party, SOURCE=attacker -> attacker.
        
        Rule 2: SOURCE is non-empty and not OWN -> reply target is SOURCE.
        Forged From header is ignored.
        Must fail on unfixed code (prefers From when Reply-To absent).
        """
        event = _ses_event(
            source="attacker@evil.test",
            from_headers=["victim@third.test"],  # forged
            replyto_headers=[],
            spf_status="PASS",
            dkim_status="PASS"
        )

        msg = channels.parse_inbound_event(event)
        self.assertIsNotNone(msg)
        result = channels.send_reply(msg, "Test reply")

        self.assertIsNotNone(result)
        self.assertEqual(len(self.ses_sends), 1)
        send = self.ses_sends[0]
        to_addrs = send["Destination"]["ToAddresses"]
        self.assertEqual(to_addrs[0].lower(), "attacker@evil.test")
        self.assertNotIn("victim@third.test", str(send).lower())

    def test_c_normal_judge_mail_sends_to_judge(self):
        """(c) normal judge mail -> judge (Rule 2: SOURCE is the judge, not our own mailbox)."""
        event = _ses_event(
            source="judge@example.test",
            from_headers=["Judge Person <judge@example.test>"],
            replyto_headers=[],
            spf_status="PASS",
            dkim_status="PASS",
        )
        msg = channels.parse_inbound_event(event)
        self.assertIsNotNone(msg)
        result = channels.send_reply(msg, "Test reply")
        self.assertIsNotNone(result)
        self.assertEqual(len(self.ses_sends), 1)
        to_addrs = self.ses_sends[0]["Destination"]["ToAddresses"]
        self.assertEqual([a.lower() for a in to_addrs], ["judge@example.test"])

    def test_k_refusal_is_logged_with_sender(self):
        """Rule 7 (+ Rule 1): an unauthenticated message (SPF and DKIM both FAIL) gets no reply, and the refusal is
        logged with the sender."""
        event = _ses_event(
            source="victim@third.test",
            from_headers=["victim@third.test"],
            replyto_headers=[],
            spf_status="FAIL",
            dkim_status="FAIL",
        )
        with self.assertLogs(level="WARNING") as logs:
            msg = channels.parse_inbound_event(event)
            result = channels.send_reply(msg, "Test reply") if msg is not None else None
        self.assertIsNone(result)
        self.assertEqual(len(self.ses_sends), 0)
        self.assertTrue(any("victim@third.test" in line for line in logs.output), logs.output)
    def test_d_replyto_equals_source_sends_to_source(self):
        """(d) Reply-To equal to SOURCE -> SOURCE.
        
        Rule 2: SOURCE is non-empty and not OWN -> reply target is SOURCE.
        Reply-To equals SOURCE, so it's fine.
        """
        event = _ses_event(
            source="sender@example.test",
            from_headers=["sender@example.test"],
            replyto_headers=["sender@example.test"],  # Same as SOURCE
            spf_status="PASS",
            dkim_status="PASS"
        )

        msg = channels.parse_inbound_event(event)
        self.assertIsNotNone(msg)
        result = channels.send_reply(msg, "Test reply")

        self.assertIsNotNone(result)
        self.assertEqual(len(self.ses_sends), 1)
        send = self.ses_sends[0]
        to_addrs = send["Destination"]["ToAddresses"]
        self.assertEqual(to_addrs[0].lower(), "sender@example.test")

    def test_e_source_own_dkim_pass_sends_to_from(self):
        """(e) SOURCE=OWN with DKIM PASS -> From.
        
        Rule 3: SOURCE is empty or OWN -> reply target is FROM only when DKIM is PASS.
        """
        event = _ses_event(
            source="judges@cds.3dogs.ai",  # OWN
            from_headers=["judge@example.test"],
            replyto_headers=[],
            spf_status="PASS",
            dkim_status="PASS"  # PASS -> use FROM
        )

        msg = channels.parse_inbound_event(event)
        self.assertIsNotNone(msg)
        result = channels.send_reply(msg, "Test reply")

        self.assertIsNotNone(result)
        self.assertEqual(len(self.ses_sends), 1)
        send = self.ses_sends[0]
        to_addrs = send["Destination"]["ToAddresses"]
        self.assertEqual(to_addrs[0].lower(), "judge@example.test")

    def test_f_source_own_dkim_fail_no_send(self):
        """(f) SOURCE=OWN with DKIM FAIL -> no send.
        
        Rule 3: If DKIM is not PASS, there is no reply target.
        Must fail on unfixed code (ignores DKIM).
        """
        event = _ses_event(
            source="judges@cds.3dogs.ai",  # OWN
            from_headers=["judge@example.test"],
            replyto_headers=[],
            spf_status="PASS",
            dkim_status="FAIL"  # FAIL -> no target
        )

        msg = channels.parse_inbound_event(event)
        self.assertIsNotNone(msg)
        result = channels.send_reply(msg, "Test reply")

        self.assertIsNone(result)  # No reply target -> send_reply returns None
        self.assertEqual(len(self.ses_sends), 0)

    def test_g_spf_fail_and_dkim_fail_no_send(self):
        """(g) SPF FAIL and DKIM FAIL -> no send.
        
        Rule 1: If SPF and DKIM are BOTH present and NEITHER is "PASS": no reply target.
        Must fail on unfixed code (ignores verdicts).
        """
        event = _ses_event(
            source="attacker@evil.test",
            from_headers=["attacker@evil.test"],
            replyto_headers=[],
            spf_status="FAIL",
            dkim_status="FAIL"  # Neither PASS
        )

        msg = channels.parse_inbound_event(event)
        self.assertIsNotNone(msg)
        result = channels.send_reply(msg, "Test reply")

        self.assertIsNone(result)
        self.assertEqual(len(self.ses_sends), 0)

    def test_h_empty_malformed_replyto_sends_to_source(self):
        """(h) empty / malformed Reply-To -> SOURCE.
        
        Rule 2: SOURCE is non-empty and not OWN -> reply target is SOURCE.
        Malformed headers are ignored (Rule 7).
        """
        # Empty Reply-To
        event = _ses_event(
            source="sender@example.test",
            from_headers=["sender@example.test"],
            replyto_headers=[""],  # Empty
            spf_status="PASS",
            dkim_status="PASS"
        )

        msg = channels.parse_inbound_event(event)
        self.assertIsNotNone(msg)
        result = channels.send_reply(msg, "Test reply")

        self.assertIsNotNone(result)
        self.assertEqual(len(self.ses_sends), 1)
        send = self.ses_sends[0]
        to_addrs = send["Destination"]["ToAddresses"]
        self.assertEqual(to_addrs[0].lower(), "sender@example.test")

        # Malformed Reply-To (no @)
        self._clear_sends()
        event2 = _ses_event(
            source="sender@example.test",
            from_headers=["sender@example.test"],
            replyto_headers=["not-an-email"],
            spf_status="PASS",
            dkim_status="PASS"
        )

        msg2 = channels.parse_inbound_event(event2)
        result2 = channels.send_reply(msg2, "Test reply")

        self.assertIsNotNone(result2)
        self.assertEqual(len(self.ses_sends), 1)
        send2 = self.ses_sends[0]
        to_addrs2 = send2["Destination"]["ToAddresses"]
        self.assertEqual(to_addrs2[0].lower(), "sender@example.test")

    def test_i_non_ascii_display_name_correct_target(self):
        """(i) non-ASCII display name -> correct target.
        
        Rule 6: Normalization handles display names, angle brackets, case.
        """
        event = _ses_event(
            source="sender@example.test",
            from_headers=['"Jürgen Ä" <sender@example.test>'],  # Display name
            replyto_headers=[],
            spf_status="PASS",
            dkim_status="PASS"
        )

        msg = channels.parse_inbound_event(event)
        self.assertIsNotNone(msg)
        result = channels.send_reply(msg, "Test reply")

        self.assertIsNotNone(result)
        self.assertEqual(len(self.ses_sends), 1)
        send = self.ses_sends[0]
        to_addrs = send["Destination"]["ToAddresses"]
        # Address extracted, lowercased
        self.assertEqual(to_addrs[0].lower(), "sender@example.test")

        # Case insensitivity
        self._clear_sends()
        event2 = _ses_event(
            source="SENDER@EXAMPLE.TEST",  # Uppercase source
            from_headers=["Sender@Example.Test"],
            replyto_headers=[],
            spf_status="PASS",
            dkim_status="PASS"
        )

        msg2 = channels.parse_inbound_event(event2)
        result2 = channels.send_reply(msg2, "Test reply")

        self.assertIsNotNone(result2)
        self.assertEqual(len(self.ses_sends), 1)
        send2 = self.ses_sends[0]
        to_addrs2 = send2["Destination"]["ToAddresses"]
        self.assertEqual(to_addrs2[0].lower(), "sender@example.test")

    def test_j_handbuilt_message_third_party_refused(self):
        """(j) hand-built InboundMessage with a third-party reply_target -> send_reply refuses.
        
        Rule 5: send_reply() with allow_unlisted=True ONLY to a target produced by rules.
        Must fail on unfixed code (allow_unlisted=True permits any target).
        """
        # Build message bypassing parse_ses_email_event (simulating tampered reply_target)
        msg = channels.InboundMessage(
            channel="email",
            sender="attacker@evil.test",  # Real sender
            text="Test",
            reply_target="victim@third.test"  # Third party (not equal to sender)
        )

        result = channels.send_reply(msg, "Test reply")

        # Should refuse (return None, no service call)
        self.assertIsNone(result)
        self.assertEqual(len(self.ses_sends), 0)

    def test_l_multipart_quoted_printable_body_replyto_third_party(self):
        """(l) multipart/alternative with a quoted-printable text/plain part + Reply-To third party.

        Rule 2: SOURCE=attacker -> reply only to SOURCE; the QP body is decoded
        (non-ASCII restored, soft line break joined) and the HTML part is not used.
        Must fail on unfixed code (prefers Reply-To -> third party).
        """
        source = "attacker@evil.test"
        third_party = "victim@third.test"
        raw_message = (
            b"From: attacker@evil.test\r\n"
            b"To: judges@cds.3dogs.ai\r\n"
            b"Reply-To: victim@third.test\r\n"
            b"Subject: Order\r\n"
            b"MIME-Version: 1.0\r\n"
            b"Content-Type: multipart/alternative; boundary=\"b1\"\r\n"
            b"\r\n"
            b"--b1\r\n"
            b"Content-Type: text/plain; charset=\"utf-8\"\r\n"
            b"Content-Transfer-Encoding: quoted-printable\r\n"
            b"\r\n"
            b"Caf=C3=A9 question: where is my =\r\n"
            b"order?\r\n"
            b"--b1\r\n"
            b"Content-Type: text/html; charset=\"utf-8\"\r\n"
            b"Content-Transfer-Encoding: 7bit\r\n"
            b"\r\n"
            b"<html><body><p>Caf&eacute; question: where is my order?</p></body></html>\r\n"
            b"--b1--\r\n"
        )
        # Reuse the existing helper for the event skeleton (receipt inside ses
        # next to mail) and replace only `content` with the real multipart MIME.
        event = _ses_event(
            source=source,
            from_headers=[source],
            replyto_headers=[third_party],
            subject="Order",
            spf_status="PASS",
            dkim_status="PASS",
        )
        event["Records"][0]["ses"]["content"] = base64.b64encode(
            raw_message
        ).decode("ascii")

        msg = channels.parse_ses_email_event(event)
        self.assertIsNotNone(msg)
        self.assertEqual(msg.reply_target.lower(), source)

        # Decoded plain-text body: QP decoded, soft line break joined, no HTML.
        self.assertEqual(msg.text, "Café question: where is my order?")
        self.assertIn("Café", msg.text)
        self.assertNotIn("=C3=A9", msg.text)
        self.assertNotIn("=\r\n", msg.text)
        self.assertNotIn("<", msg.text)
        self.assertNotIn("html", msg.text.lower())

        result = channels.send_reply(msg, "answer")
        self.assertIsNotNone(result)
        self.assertEqual(len(self.ses_sends), 1)
        self.assertEqual(self.mock_ses_client.send_email.call_count, 1)
        to_addrs = self.ses_sends[0]["Destination"]["ToAddresses"]
        self.assertEqual([a.lower() for a in to_addrs], [source])
        for send_call in self.ses_sends:
            self.assertNotIn(third_party, str(send_call).lower())

    def test_m_rfc2047_encoded_headers_do_not_redirect(self):
        """(m) RFC 2047 encoded-word display names on From / Reply-To never redirect the reply.

        Rule 2 + Rule 6: SOURCE=attacker -> reply only to SOURCE; encoded-word
        display names are irrelevant to the decision and nothing raises.
        Must fail on unfixed code (prefers Reply-To -> Third.Party@Example.TEST).
        """
        source = "attacker@evil.test"
        third_party = "third.party@example.test"
        from_b64 = base64.b64encode("Jürgen Ä".encode("utf-8")).decode("ascii")
        from_header = "=?utf-8?b?" + from_b64 + "?= <attacker@evil.test>"
        replyto_header = "=?utf-8?q?J=C3=BCrgen_=C3=84?= <Third.Party@Example.TEST>"

        event = _ses_event(
            source=source,
            from_headers=[from_header],
            replyto_headers=[replyto_header],
            spf_status="PASS",
            dkim_status="PASS",
        )

        msg = channels.parse_ses_email_event(event)
        self.assertIsNotNone(msg)
        self.assertEqual(msg.reply_target.lower(), source)

        result = channels.send_reply(msg, "Test reply")
        self.assertIsNotNone(result)
        self.assertEqual(len(self.ses_sends), 1)
        self.assertEqual(self.mock_ses_client.send_email.call_count, 1)
        to_addrs = self.ses_sends[0]["Destination"]["ToAddresses"]
        self.assertEqual([a.lower() for a in to_addrs], [source])
        for send_call in self.ses_sends:
            self.assertNotIn(third_party, str(send_call).lower())


if __name__ == "__main__":
    unittest.main()
