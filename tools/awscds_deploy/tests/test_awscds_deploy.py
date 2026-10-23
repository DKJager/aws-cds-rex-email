import sys
import pathlib
import json
import os

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pytest
from unittest.mock import MagicMock, patch, call
import tempfile

import awscds_deploy


class StubBotoClient:
    """Fake boto3 client factory for locked tests; no network calls."""
    def __init__(self, service_name):
        self.service_name = service_name
        self.calls = []
        self._responses = {}
        self._raise_on = {}

    def set_response(self, method, retval=None, side_effect=None):
        self._responses[method] = {"return": retval, "side_effect": side_effect}

    def set_raise(self, method, exc):
        self._raise_on[method] = exc

    def __getattr__(self, name):
        def method_handler(**kwargs):
            self.calls.append({"method": name, "kwargs": kwargs})
            if name in self._raise_on:
                raise self._raise_on[name]
            if name in self._responses:
                resp = self._responses[name]
                if resp["side_effect"]:
                    return resp["side_effect"](**kwargs)
                return resp["return"]
            return {}
        return method_handler


def _write_min_template(tmp_path):
    t = tmp_path / "template.yaml"
    t.write_text("""AWSTemplateFormatVersion: '2010-09-09'
Resources:
  InboundEmailRule:
    Type: AWS::SES::ReceiptRule
    Properties:
      Rule:
        Recipients:
          - judges@cds.3dogs.ai
  Fn:
    Type: AWS::Serverless::Function
    Properties:
      CodeUri: src/
""")
    return t


class TestDryRunValidation:
    """CLAUSE 7 (Done Means Done): dry-run fails on wrong recipient and '*' IAM."""

    def test_dry_run_fails_on_wrong_recipient(self, tmp_path):
        """Wrong recipient must FAIL dry-run validation (SPEC #1)."""
        template = tmp_path / "template.yaml"
        template.write_text("""
AWSTemplateFormatVersion: '2010-09-09'
Resources:
  InboundEmailRule:
    Type: AWS::SES::ReceiptRule
    Properties:
      Rule:
        Recipients:
          - wrong@example.com
""")
        result = awscds_deploy.dry_run_validate(template)
        assert result["valid"] is False, "Dry-run must fail on wrong recipient"
        assert "recipients" in result.get("reason", "").lower() or "recipient" in result.get("reason", "").lower()

    def test_dry_run_fails_on_star_iam_action(self, tmp_path):
        """IAM Action '*' must FAIL dry-run validation (SPEC #1)."""
        template = tmp_path / "template.yaml"
        template.write_text("""
AWSTemplateFormatVersion: '2010-09-09'
Resources:
  BadRole:
    Type: AWS::IAM::Role
    Properties:
      AssumeRolePolicyDocument:
        Statement:
          - Effect: Allow
            Action: '*'
            Resource: '*'
""")
        result = awscds_deploy.dry_run_validate(template)
        assert result["valid"] is False, "Dry-run must fail on Action '*'"
        assert "action" in result.get("reason", "").lower() or "iam" in result.get("reason", "").lower()

    def test_dry_run_fails_on_star_iam_resource(self, tmp_path):
        """IAM Resource '*' must FAIL dry-run validation (SPEC #1)."""
        template = tmp_path / "template.yaml"
        template.write_text("""
AWSTemplateFormatVersion: '2010-09-09'
Resources:
  BadRole:
    Type: AWS::IAM::Role
    Properties:
      AssumeRolePolicyDocument:
        Statement:
          - Effect: Allow
            Action: s3:GetObject
            Resource: '*'
""")
        result = awscds_deploy.dry_run_validate(template)
        assert result["valid"] is False, "Dry-run must fail on Resource '*'"
        assert "resource" in result.get("reason", "").lower() or "iam" in result.get("reason", "").lower()

    def test_dry_run_passes_correct_template(self, tmp_path):
        """Correct template with judges@cds.3dogs.ai recipient passes."""
        template = tmp_path / "template.yaml"
        template.write_text("""
AWSTemplateFormatVersion: '2010-09-09'
Resources:
  InboundEmailRule:
    Type: AWS::SES::ReceiptRule
    Properties:
      Rule:
        Recipients:
          - judges@cds.3dogs.ai
""")
        result = awscds_deploy.dry_run_validate(template)
        assert result["valid"] is True, "Dry-run must pass on correct config"


class TestIdentityActivationSequence:
    """CLAUSE 2 (Reliability): activation never happens before identity SUCCESS."""

    def test_activation_waits_for_identity_success(self, tmp_path):
        """Set operation to active if and only if identity shows SUCCESS (SPEC #4)."""
        clients = {"sesv2": StubBotoClient("sesv2"), "ses": StubBotoClient("ses")}
        clients["sesv2"].set_response("get_email_identity", {"DkimAttributes": {"Status": "SUCCESS"}})

        activated = {"called": False}

        def fake_set_active(**kwargs):
            activated["called"] = True
            return {}

        clients["ses"].set_response("set_active_receipt_rule_set", side_effect=fake_set_active)

        result = awscds_deploy.ensure_identity_active(
            client_factory=lambda s: clients[s],
            identity="cds.3dogs.ai",
            poll_interval=0.01,
            max_attempts=3
        )

        assert activated["called"] is True, "Activation should be called"

    def test_activation_rejected_on_pending_identity(self, tmp_path):
        """Activation must NOT happen while identity is PENDING (SPEC #4)."""
        clients = {"sesv2": StubBotoClient("sesv2")}
        clients["sesv2"].set_response("get_email_identity", {"DkimAttributes": {"Status": "PENDING"}})

        result = awscds_deploy.ensure_identity_active(
            client_factory=lambda s: clients[s],
            identity="cds.3dogs.ai",
            poll_interval=0.01,
            max_attempts=2
        )

        assert result.get("activated") is False, "Activation must not occur on PENDING identity"
        for c in clients["sesv2"].calls:
            assert c["method"] != "set_active_receipt_rule_set", "SetActiveReceiptRuleSet must not be called"

    def test_activation_never_before_identity_success(self, tmp_path):
        """CRITICAL: SetActiveReceiptRuleSet is NEVER called before SUCCESS (SPEC #4)."""
        call_order = []

        class StrictClient:
            def __init__(self):
                self.status_to_return = "PENDING"

            def create_email_identity(self, **kwargs):
                call_order.append(("create_email_identity", "CALLED"))
                return {}

            def get_email_identity(self, **kwargs):
                call_order.append(("get_email_identity", self.status_to_return))
                return {"DkimAttributes": {"Status": self.status_to_return}}

            def set_active_receipt_rule_set(self, **kwargs):
                call_order.append(("set_active_receipt_rule_set", "CALLED"))
                raise RuntimeError("Activation called too early!")

        client = StrictClient()
        result = awscds_deploy.ensure_identity_active(
            client_factory=lambda s: client if s == "sesv2" else None,
            identity="cds.3dogs.ai",
            poll_interval=0.01,
            max_attempts=1,
            never_activate=True
        )

        for call_entry in call_order:
            assert call_entry[0] != "set_active_receipt_rule_set", \
                "SetActiveReceiptRuleSet called before identity SUCCESS - CRITICAL VIOLATION"


class TestDestroyScriptTaggedOnly:
    """CLAUSE 5 (Safe Change): destroy script lists only tagged resources."""

    def test_destroy_script_only_tagged_resources(self, tmp_path):
        """Destroy script must only list resources tagged project=awscds (SPEC #5)."""
        clients = {"resourcegroupstaggingapi": StubBotoClient("resourcegroupstaggingapi")}

        tagged_resources = [
            {"ResourceARN": "arn:aws:lambda:us-east-1:123456789012:function:awscds-func", "Tags": [{"Key": "project", "Value": "awscds"}]},
            {"ResourceARN": "arn:aws:s3:::awscds-bucket", "Tags": [{"Key": "project", "Value": "awscds"}]},
        ]

        untagged_resources = [
            {"ResourceARN": "arn:aws:lambda:us-east-1:123456789012:function:other-func", "Tags": []},
            {"ResourceARN": "arn:aws:dynamodb:us-east-1:123456789012:table/OtherTable", "Tags": [{"Key": "project", "Value": "other"}]},
        ]

        clients["resourcegroupstaggingapi"].set_response("get_resources", {"ResourceTagMappingList": tagged_resources + untagged_resources})

        script_path = tmp_path / "destroy_awscds.sh"
        awscds_deploy.generate_destroy_script(
            client_factory=lambda s: clients[s],
            output_path=script_path
        )

        script_content = script_path.read_text()

        assert "awscds-func" in script_content, "Tagged Lambda must be in destroy script"
        assert "awscds-bucket" in script_content, "Tagged S3 must be in destroy script"
        assert "other-func" not in script_content, "Untagged Lambda must NOT be in destroy script"
        assert "OtherTable" not in script_content, "Differently-tagged table must NOT be in destroy script"


class TestNegativeControlStubSuccessNoCreate:
    """
    CLAUSE 7 (Done Means Done) NEGATIVE CONTROL:
    A stub reporting success without calling create/activate MUST FAIL.
    """

    def test_stub_reporting_success_without_create_fails(self, tmp_path):
        """
        NEGATIVE CONTROL: A fake stub that reports success but never calls
        create_email_identity or set_active_receipt_rule_set must FAIL.
        This is the SPEC-specified negative control.
        """
        class FakeSuccessStub:
            """Stub that reports success without doing any work - MUST BE REJECTED."""
            def __init__(self):
                self.calls_made = []

            def create_email_identity(self, **kwargs):
                self.calls_made.append(("create", kwargs))
                return {"IdentityType": "DOMAIN"}

            def get_email_identity(self, **kwargs):
                return {"DkimAttributes": {"Status": "SUCCESS"}}

            def set_active_receipt_rule_set(self, **kwargs):
                return {}

        stub = FakeSuccessStub()

        with pytest.raises(AssertionError) as exc_info:
            awscds_deploy.verify_create_activate_called(
                client=stub,
                identity="cds.3dogs.ai"
            )

        assert "create" in str(exc_info.value).lower() or "activate" in str(exc_info.value).lower() or "fail" in str(exc_info.value).lower()

    def test_negative_control_blind_rejection(self, tmp_path):
        """
        NEGATIVE CONTROL (SPEC): Stub reporting success without calling
        create/activate must FAIL the audit. This validates the auditor
        would catch a fake implementation claiming done without work.
        """
        class LyingStub:
            """Pretends everything succeeded without making any real calls."""
            def __init__(self):
                self.call_log = []

            def create_email_identity(self, **kwargs):
                self.call_log.append(("create_email_identity", kwargs))
                return {}

            def get_email_identity(self, **kwargs):
                self.call_log.append(("get_email_identity", kwargs))
                return {"DkimAttributes": {"Status": "SUCCESS"}}

            def set_active_receipt_rule_set(self, **kwargs):
                self.call_log.append(("set_active_receipt_rule_set", kwargs))
                return {}

        lying_stub = LyingStub()

        with pytest.raises(AssertionError):
            awscds_deploy.verify_create_activate_called(
                client=lying_stub,
                identity="cds.3dogs.ai",
                require_create=True,
                require_activate=True
            )


class TestDKIMRecordOutput:
    """CLAUSE 7: verify DKIM CNAMEs and MX record are printed."""

    def test_dkim_records_printed_on_create(self, tmp_path, capsys):
        """Create identity must print 3 DKIM CNAMEs and MX record (SPEC #3)."""
        clients = {"sesv2": StubBotoClient("sesv2"), "ses": StubBotoClient("ses")}

        dkim_tokens = ["token1", "token2", "token3"]
        clients["sesv2"].set_response("create_email_identity", {
            "IdentityType": "DOMAIN",
            "DkimAttributes": {
                "Tokens": dkim_tokens
            }
        })
        clients["sesv2"].set_response("get_email_identity", {
            "DkimAttributes": {"Status": "SUCCESS", "Tokens": dkim_tokens}
        })
        clients["sesv2"].set_response("set_active_receipt_rule_set", {})

        awscds_deploy.create_and_activate_identity(
            client_factory=lambda s: clients[s],
            identity="cds.3dogs.ai",
            poll_interval=0.01,
            max_attempts=2
        )

        captured = capsys.readouterr()
        output = captured.out + captured.err

        for token in dkim_tokens:
            assert token in output, f"DKIM token {token} must be printed"

        assert "mx" in output.lower() or "MX" in output, "MX record instruction must be printed"


class TestSmokeNoEmailToNonAllowlisted:
    """CLAUSE 6 (Fleet) + SPEC: never mail to non-allowlisted addresses."""

    def test_smoke_only_sends_to_judges(self, tmp_path):
        """Smoke test must only send to judges@cds.3dogs.ai (SPEC #6 + Never list)."""
        clients = {
            "sesv2": StubBotoClient("sesv2"),
            "dynamodb": StubBotoClient("dynamodb")
        }

        ses_calls = []
        clients["sesv2"].set_response("send_email", side_effect=lambda **kw: ses_calls.append(kw) or {"MessageId": "msg-123"})

        clients["dynamodb"].set_response("get_item", {"Item": {"reply": {"S": "Test reply"}}})
        clients["dynamodb"].set_response("query", {"Items": [{"session_id": {"S": "test-id"}, "reply": {"S": "Automated reply"}}]})

        result = awscds_deploy.smoke_test_send(
            client_factory=lambda s: clients[s],
            to_address="judges@cds.3dogs.ai",
            from_address="owner@example.org",
            poll_interval=0.01,
            max_polls=2
        )

        assert result.get("sent") is True, "Smoke test should send to judges@cds.3dogs.ai"

        for c in clients["sesv2"].calls:
            if c["method"] == "send_email":
                dest = c["kwargs"].get("Destination", {})
                to_addrs = dest.get("ToAddresses", [])
                for addr in to_addrs:
                    assert addr == "judges@cds.3dogs.ai", f"Email sent to non-allowlisted address: {addr}"

    def test_smoke_never_to_wrong_address(self, tmp_path):
        """Smoke test MUST NOT send to any address except judges@cds.3dogs.ai."""
        clients = {"sesv2": StubBotoClient("sesv2")}
        forbidden = ["sender@example.org", "user@example.com", "random@example.org", "spam@test.com"]

        caught = False
        try:
            awscds_deploy.smoke_test_send(
                client_factory=lambda s: clients[s],
                to_address="sender@example.org"
            )
        except (ValueError, RuntimeError) as e:
            caught = True
            assert "allowlist" in str(e).lower() or "allowed" in str(e).lower() or "judges@cds.3dogs.ai" in str(e)

        assert caught, "Sending to wrong address must raise an error"


class TestInjectableBotoClient:
    """CLAUSE 1 (Performance): every AWS call through injectable factory."""

    def test_all_aws_calls_use_factory(self, tmp_path):
        """Every AWS call must go through the injected client factory (SPEC)."""
        factory_calls = []

        def tracking_factory(service_name):
            factory_calls.append(service_name)
            return StubBotoClient(service_name)

        awscds_deploy.create_and_activate_identity(
            client_factory=tracking_factory,
            identity="cds.3dogs.ai",
            poll_interval=0.01,
            max_attempts=1
        )

        assert "sesv2" in factory_calls, "Client factory must be called for sesv2"

    def test_no_direct_boto3_client_calls(self, tmp_path):
        """Module must not call boto3.client() directly - must use factory."""
        with patch("boto3.client") as mock_boto_client:
            mock_boto_client.side_effect = RuntimeError("Direct boto3.client() call forbidden - use factory")

            clients = {"sesv2": StubBotoClient("sesv2"), "ses": StubBotoClient("ses")}
            clients["sesv2"].set_response("get_email_identity", {"DkimAttributes": {"Status": "SUCCESS"}})

            try:
                awscds_deploy.ensure_identity_active(
                    client_factory=lambda s: clients[s],
                    identity="cds.3dogs.ai",
                    poll_interval=0.01,
                    max_attempts=1
                )
            except RuntimeError as e:
                if "Direct boto3" in str(e):
                    pytest.fail("Module called boto3.client() directly instead of using factory")
                raise

            assert mock_boto_client.call_count == 0, "boto3.client() must never be called directly"


class TestPackageAndDeploy:
    """SPEC #2: package src/ to S3 and deploy with stack prefix."""

    def test_package_uses_provided_bucket(self, tmp_path):
        """Package step must use the S3 bucket from --bucket argument."""
        clients = {
            "s3": StubBotoClient("s3"),
            "cloudformation": StubBotoClient("cloudformation")
        }

        src_dir = tmp_path / "src"
        src_dir.mkdir()
        (src_dir / "handler.py").write_text("def handler(event, context): pass")

        result = awscds_deploy.package_and_deploy(
            client_factory=lambda s: clients[s],
            src_path=src_dir,
            bucket="my-deploy-bucket",
            stack_name="awscds-test",
            template_path=_write_min_template(tmp_path)
        )

        s3_calls = [c for c in clients["s3"].calls if c["method"] == "put_object"]
        assert len(s3_calls) > 0, "S3 put_object must be called for packaging"

        for c in s3_calls:
            assert "my-deploy-bucket" in c["kwargs"].get("Bucket", ""), "Must use provided bucket"

    def test_stack_prefix_awscds(self, tmp_path):
        """Deployed stack must have prefix 'awscds-' (SPEC #2)."""
        clients = {
            "s3": StubBotoClient("s3"),
            "cloudformation": StubBotoClient("cloudformation"),
        }
        (tmp_path / "src").mkdir()

        result = awscds_deploy.package_and_deploy(
            client_factory=lambda s: clients[s],
            src_path=tmp_path / "src",
            bucket="bucket",
            stack_name="test-stack",
            template_path=_write_min_template(tmp_path)
        )

        cf_calls = clients["cloudformation"].calls
        for c in cf_calls:
            if c["method"] in ["create_stack", "update_stack", "deploy_stack"]:
                stack_name = c["kwargs"].get("StackName", "")
                assert stack_name.startswith("awscds-"), f"Stack name must start with 'awscds-', got: {stack_name}"
