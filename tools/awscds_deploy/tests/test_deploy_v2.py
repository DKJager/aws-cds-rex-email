"""Audit-round tests for awscds_deploy: real template, botocore-checked calls, negative controls.

Nothing here calls AWS.  Every stubbed call is validated against the botocore
service model, so a method or parameter that the real API does not have fails here.
"""
import io
import json
import pathlib
import subprocess
import sys
import zipfile
from unittest import mock

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import awscds_deploy as d  # noqa: E402

STAGE = pathlib.Path(__file__).resolve().parents[3]
# The helper now ships inside the repository (tools/awscds_deploy); fall back to the
# older stage layout (stage/tools + stage/repo) when run from there.
REPO = STAGE if (STAGE / "infra" / "template.yaml").exists() else STAGE / "repo"
TEMPLATE = REPO / "infra" / "template.yaml"


def make_factory(responder=None):
    log = []
    return d.recording_factory(log, responder), log


def methods(log):
    return [f"{e['service']}.{e['method']}" for e in log]


def mutated(tmp_path, old, new, count=1):
    text = TEMPLATE.read_text()
    assert text.count(old) == count, (old, text.count(old))
    out = tmp_path / "t.yaml"
    out.write_text(text.replace(old, new))
    return out


class ClientError(Exception):
    def __init__(self, code):
        self.response = {"Error": {"Code": code}}


# ---------------------------------------------------------------- real template
class TestRealTemplate:
    def test_real_template_is_valid(self):
        v = d.dry_run_validate(TEMPLATE)
        assert v["valid"], v["reason"]
        assert v["receipt_rules"] == [["judges@cds.3dogs.ai"]]

    def test_list_form_star_resource_is_caught(self, tmp_path):
        # the pre-audit line-based check missed exactly this form (it was in the real template)
        t = tmp_path / "t.yaml"
        t.write_text('Resources:\n  P:\n    Type: AWS::IAM::Policy\n    Properties:\n      PolicyDocument:\n'
                     '        Statement:\n          - Effect: Allow\n            Action:\n              - xray:PutTraceSegments\n'
                     '            Resource:\n              - "*"\n')
        v = d.dry_run_validate(t)
        assert not v["valid"] and "Resource" in v["reason"]

    @pytest.mark.parametrize("old,new", [
        ("                - sqs:SendMessage\n", '                - sqs:SendMessage\n                - "sqs:*"\n'),
        ("              Resource: !GetAtt OrchestratorDLQ.Arn\n", '              Resource: "*"\n'),
        ("        Recipients:\n          - judges@cds.3dogs.ai\n", "        Recipients:\n          - wrong@cds.3dogs.ai\n"),
        ("        Recipients:\n          - judges@cds.3dogs.ai\n",
         "        Recipients:\n          - judges@cds.3dogs.ai\n          - other@cds.3dogs.ai\n"),
        ("            Principal:\n              Service: ses.amazonaws.com\n", '            Principal: "*"\n'),
    ])
    def test_negative_controls_on_the_real_template(self, tmp_path, old, new):
        v = d.dry_run_validate(mutated(tmp_path, old, new))
        assert v["valid"] is False and v["errors"]

    def test_unparsable_template_is_invalid(self, tmp_path):
        t = tmp_path / "bad.yaml"
        t.write_text("a: [unclosed\n")
        assert d.dry_run_validate(t)["valid"] is False

    def test_template_without_receipt_rule_is_invalid(self, tmp_path):
        t = tmp_path / "t.yaml"
        t.write_text("Resources:\n  B:\n    Type: AWS::S3::Bucket\n")
        assert "No SES ReceiptRule" in d.dry_run_validate(t)["reason"]


# ---------------------------------------------------------------- CLI --dry-run
class TestDryRunCli:
    def run(self, capsys, *extra, template=TEMPLATE):
        with mock.patch("boto3.client", side_effect=RuntimeError("boto3 client must not be built in dry-run")):
            rc = d.main(["--dry-run", "--template", str(template), "--src", str(REPO / "src"), *extra])
        return rc, capsys.readouterr().out

    def test_dry_run_plans_the_full_deploy_in_order(self, capsys):
        rc, out = self.run(capsys, "--smoke-from", "owner@example.org")
        assert rc == 0 and "PLANNED AWS CALLS" in out
        order = [line.split()[1] for line in out.splitlines() if line.strip()[:2].rstrip(".").isdigit()]
        for call in ("s3.put_object", "cloudformation.create_stack", "sesv2.create_email_identity",
                     "ses.describe_receipt_rule_set", "ses.set_active_receipt_rule_set", "sesv2.send_email",
                     "dynamodb.scan"):
            assert call in order, call
        assert order.index("ses.set_active_receipt_rule_set") > order.index("cloudformation.create_stack")
        assert order.index("ses.set_active_receipt_rule_set") > max(
            i for i, c in enumerate(order) if c == "sesv2.get_email_identity")
        assert "****" in out and "https://dry-run.invalid" not in out

    def test_dry_run_fails_on_bad_template_and_plans_nothing(self, capsys, tmp_path):
        bad = mutated(tmp_path, "        Recipients:\n          - judges@cds.3dogs.ai\n",
                      "        Recipients:\n          - wrong@cds.3dogs.ai\n")
        rc, out = self.run(capsys, template=bad)
        assert rc == 2 and "ERROR" in out and "PLANNED AWS CALLS" not in out

    def test_aws_actions_need_yes(self, capsys):
        assert d.main(["--inventory"]) == 3


# ---------------------------------------------------------------- package/deploy
class TestPackageAndDeploy:
    def deploy(self, **kw):
        factory, log = make_factory()
        args = dict(client_factory=factory, src_path=REPO / "src", bucket="b", stack_name="demo",
                    template_path=TEMPLATE, parameters={"NexusEndpoint": "https://e.invalid/v1"})
        args.update(kw)
        return d.package_and_deploy(**args), log

    def test_create_stack_uses_the_template_not_the_zip_and_tags_the_stack(self):
        result, log = self.deploy()
        assert methods(log) == ["s3.put_object", "s3.put_object", "cloudformation.create_stack"]
        create = log[-1]["kwargs"]
        assert create["TemplateURL"].endswith(result["template_key"]) and result["template_key"].endswith(".yaml")
        assert create["Tags"] == [{"Key": "project", "Value": "awscds"}]
        assert create["StackName"] == "awscds-demo"
        assert {"CAPABILITY_IAM", "CAPABILITY_AUTO_EXPAND"} == set(create["Capabilities"])
        zbytes = log[0]["kwargs"]["Body"]
        names = zipfile.ZipFile(io.BytesIO(zbytes)).namelist()
        assert "orchestrator.py" in names and not any(n.endswith(".pyc") for n in names)
        body = log[1]["kwargs"]["Body"].decode()
        assert f"CodeUri: s3://b/{result['key']}" in body and "CodeUri: src/" not in body

    def test_missing_required_parameter_is_refused_before_any_call(self):
        factory, log = make_factory()
        with pytest.raises(d.DeployRefused):
            d.package_and_deploy(client_factory=factory, src_path=REPO / "src", bucket="b", stack_name="x",
                                 template_path=TEMPLATE, parameters={})
        assert log == []

    def test_wildcard_template_is_refused_before_any_call(self, tmp_path):
        bad = mutated(tmp_path, "                - sqs:SendMessage\n", '                - sqs:SendMessage\n                - "sqs:*"\n')
        factory, log = make_factory()
        with pytest.raises(d.DeployRefused):
            d.package_and_deploy(client_factory=factory, src_path=REPO / "src", bucket="b", stack_name="x",
                                 template_path=bad, parameters={"NexusEndpoint": "https://e.invalid"})
        assert log == []

    def test_botocore_rejects_an_invalid_call(self):
        with pytest.raises(Exception):
            d.validate_call("cloudformation", "create_stack", {"StackName": "x", "Bogus": 1})


# ---------------------------------------------------------------- identity
class TestIdentity:
    def responder(self, pending=2, active=None, rule_set_exists=True):
        state = {"n": 0}

        def respond(service, method, kwargs):
            if (service, method) == ("sesv2", "get_email_identity"):
                state["n"] += 1
                return {"DkimAttributes": {"Status": "PENDING" if state["n"] <= pending else "SUCCESS",
                                           "Tokens": ["t1", "t2", "t3"]}}
            if (service, method) == ("ses", "describe_receipt_rule_set"):
                if not rule_set_exists:
                    raise ClientError("RuleSetDoesNotExist")
                return {"Metadata": {"Name": kwargs["RuleSetName"]}}
            if (service, method) == ("ses", "describe_active_receipt_rule_set"):
                return {"Metadata": {"Name": active}} if active else {}
            return {}
        return respond

    def test_sesv2_has_no_activation_operation(self):
        with pytest.raises(AttributeError):
            d.validate_call("sesv2", "set_active_receipt_rule_set", {"RuleSetName": "x"})
        d.validate_call("ses", "set_active_receipt_rule_set", {"RuleSetName": "x"})

    def test_activation_happens_once_after_success_on_the_ses_client(self):
        factory, log = make_factory(self.responder(pending=2))
        r = d.create_and_activate_identity(client_factory=factory, identity="cds.3dogs.ai", poll_interval=0,
                                           max_attempts=5)
        m = methods(log)
        assert r["activated"] is True and m.count("ses.set_active_receipt_rule_set") == 1
        assert m.index("ses.set_active_receipt_rule_set") > max(i for i, c in enumerate(m) if c == "sesv2.get_email_identity")
        assert log[[i for i, c in enumerate(m) if c == "ses.set_active_receipt_rule_set"][0]]["kwargs"] == {
            "RuleSetName": "awscds-inbound"}

    def test_never_activates_while_pending(self):
        factory, log = make_factory(self.responder(pending=99))
        r = d.create_and_activate_identity(client_factory=factory, identity="cds.3dogs.ai", poll_interval=0,
                                           max_attempts=3)
        assert r["activated"] is False and "ses.set_active_receipt_rule_set" not in methods(log)

    def test_refuses_to_replace_another_active_rule_set(self):
        factory, log = make_factory(self.responder(pending=0, active="someone-elses"))
        r = d.create_and_activate_identity(client_factory=factory, identity="cds.3dogs.ai", poll_interval=0)
        assert r["activated"] is False and "someone-elses" in r["reason"]
        assert "ses.set_active_receipt_rule_set" not in methods(log)
        factory, log = make_factory(self.responder(pending=0, active="someone-elses"))
        assert d.create_and_activate_identity(client_factory=factory, identity="cds.3dogs.ai", poll_interval=0,
                                              replace_active=True)["activated"] is True

    def test_missing_rule_set_is_reported_not_activated(self):
        factory, log = make_factory(self.responder(pending=0, rule_set_exists=False))
        r = d.create_and_activate_identity(client_factory=factory, identity="cds.3dogs.ai", poll_interval=0)
        assert r["activated"] is False and "deploy the stack first" in r["reason"]

    def test_real_flow_passes_the_auditor_negative_control(self):
        factory, log = make_factory(self.responder(pending=0))
        d.create_and_activate_identity(client_factory=factory, identity="cds.3dogs.ai", poll_interval=0)

        class Probe:
            call_log = [(e["method"], e["kwargs"]) for e in log]
        d.verify_create_activate_called(client=Probe(), identity="cds.3dogs.ai")
        Probe.call_log = [c for c in Probe.call_log if c[0] != "set_active_receipt_rule_set"]
        with pytest.raises(AssertionError):
            d.verify_create_activate_called(client=Probe(), identity="cds.3dogs.ai")


# ---------------------------------------------------------------- inventory / destroy
ACCT = "123456789012"


def tagging_factory(arns, pages=1):
    calls = {"n": 0}

    def get_resources(**kw):
        calls["n"] += 1
        chunk = arns[(calls["n"] - 1)::pages] if pages > 1 else arns
        return {"ResourceTagMappingList": [{"ResourceARN": a, "Tags": [{"Key": "project", "Value": "awscds"}]}
                                           for a in chunk],
                "PaginationToken": "next" if calls["n"] < pages else ""}
    stub = mock.Mock()
    stub.get_resources.side_effect = get_resources
    return lambda s: stub


class TestInventoryAndDestroy:
    ARNS = [
        f"arn:aws:cloudformation:us-east-1:{ACCT}:stack/awscds-demo/8a1f-uuid",
        f"arn:aws:s3:::awscds-demo-inboundmailbucket-x",
        f"arn:aws:lambda:us-east-1:{ACCT}:function:awscds-demo-OrchestratorFunction-1",
        f"arn:aws:dynamodb:us-east-1:{ACCT}:table/awscds-demo-SessionsTable-1",
        f"arn:aws:sqs:us-east-1:{ACCT}:awscds-demo-OrchestratorDLQ-1",
        f"arn:aws:cloudwatch:us-east-1:{ACCT}:alarm:awscds-demo-orchestrator-errors",
        f"arn:aws:iam::{ACCT}:role/awscds-demo-role",
        f"arn:aws:s3:::someone-elses-bucket",
    ]

    def test_stack_name_is_not_the_uuid(self):
        c = d.classify_arn(self.ARNS[0], "us-east-1")
        assert c["name"] == "awscds-demo" and "awscds-demo --region" in c["command"] and "uuid" not in c["command"]

    def test_inventory_paginates(self):
        inv = d.inventory(client_factory=tagging_factory(self.ARNS, pages=2))
        assert {i["arn"] for i in inv} == set(self.ARNS)

    def test_script_order_and_scope(self, tmp_path):
        out = d.generate_destroy_script(client_factory=tagging_factory(self.ARNS), output_path=tmp_path / "x.sh")
        text = out.read_text()
        lines = text.splitlines()
        idx = lambda needle: next(i for i, l in enumerate(lines) if needle in l and not l.startswith("#"))
        assert idx("set-active-receipt-rule-set") < idx("s3 rm s3://awscds-demo") < idx("delete-stack") \
            < idx("wait stack-delete-complete") < idx("s3 rb s3://awscds-demo")
        assert "someone-elses-bucket" in text and "SKIPPED" in text
        assert not any("someone-elses-bucket" in l for l in lines if not l.startswith("#"))
        assert any(l.startswith("# MANUAL") and "role/awscds-demo-role" in l for l in lines)
        assert subprocess.run(["bash", "-n", str(out)]).returncode == 0

    def test_untagged_resources_never_appear(self, tmp_path):
        stub = mock.Mock()
        stub.get_resources.return_value = {"ResourceTagMappingList": [
            {"ResourceARN": f"arn:aws:lambda:us-east-1:{ACCT}:function:awscds-x", "Tags": []},
            {"ResourceARN": f"arn:aws:lambda:us-east-1:{ACCT}:function:awscds-y",
             "Tags": [{"Key": "project", "Value": "other"}]}]}
        out = d.generate_destroy_script(client_factory=lambda s: stub, output_path=tmp_path / "x.sh")
        assert "awscds-x" not in out.read_text() and "awscds-y" not in out.read_text()


# ---------------------------------------------------------------- smoke
class TestSmoke:
    def responder(self, found_after):
        state = {"n": 0}

        def respond(service, method, kwargs):
            if method == "send_email":
                return {"MessageId": "id-1"}
            state["n"] += 1
            return {"Items": [{"session_id": {"S": "email:x"}}]} if state["n"] >= found_after else {"Items": []}
        return respond

    def test_wrong_recipient_is_refused_before_any_client_is_built(self):
        factory = mock.Mock()
        with pytest.raises(ValueError):
            d.smoke_test_send(client_factory=factory, to_address="owner@example.org", from_address="a@example.org")
        factory.assert_not_called()

    @pytest.mark.parametrize("sender", [None, "judges@cds.3dogs.ai", "JUDGES@cds.3dogs.ai"])
    def test_sender_must_not_be_the_judges_mailbox(self, sender):
        with pytest.raises(ValueError):
            d.smoke_test_send(client_factory=mock.Mock(), to_address="judges@cds.3dogs.ai", from_address=sender)

    def test_found_by_nonce_and_exactly_one_mail_sent(self):
        factory, log = make_factory(self.responder(found_after=2))
        r = d.smoke_test_send(client_factory=factory, to_address="judges@cds.3dogs.ai", from_address="owner@example.org",
                              poll_interval=0, max_polls=5)
        assert r["reply_found"] is True and methods(log).count("sesv2.send_email") == 1
        scan = next(e for e in log if e["method"] == "scan")["kwargs"]
        assert scan["ExpressionAttributeValues"][":n"]["S"] == r["nonce"]
        assert log[0]["kwargs"]["Destination"]["ToAddresses"] == ["judges@cds.3dogs.ai"]

    def test_not_found_is_reported_as_not_found(self):
        factory, _ = make_factory(self.responder(found_after=99))
        r = d.smoke_test_send(client_factory=factory, to_address="judges@cds.3dogs.ai", from_address="owner@example.org",
                              poll_interval=0, max_polls=2)
        assert r["sent"] is True and r["reply_found"] is False
