"""
awscds_deploy.py - deployment orchestration for the AWS CDS Agentic AI Partner Hackathon entry.

Design rules
  * Every AWS call goes through an injectable ``client_factory(service_name)``.
    The real boto3 factory is only built inside ``main()`` for an explicit,
    confirmed (``--yes``) action; ``--dry-run`` never creates a boto3 client.
  * ``--dry-run`` parses the real CloudFormation template (YAML with intrinsic
    tags), enforces the recipient allowlist and the no-wildcard IAM rule, then
    runs the real deploy / identity / smoke code paths against a recording
    client whose every call is checked against the botocore service model
    (unknown operation or invalid parameters fail the dry-run).
  * Third party dependencies: PyYAML (template parsing) and botocore (call
    validation, dry-run only).  boto3 is imported lazily for real actions.
"""

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import yaml

PROJECT_TAG_KEY = "project"
PROJECT_TAG_VALUE = "awscds"
RULE_SET_NAME = "awscds-inbound"
DEFAULT_IDENTITY = "cds.3dogs.ai"
_REQUIRED_RECIPIENTS = ["judges@cds.3dogs.ai"]
_ALLOWED_TO_ADDRESSES = {"judges@cds.3dogs.ai"}


class DeployRefused(RuntimeError):
    """Raised when a safety check blocks a deploy step."""


# ---------------------------------------------------------------------------
# Template parsing and validation
# ---------------------------------------------------------------------------

class _CfnLoader(yaml.SafeLoader):
    """SafeLoader that turns CloudFormation short tags (!Sub, !Ref ...) into dicts."""


def _cfn_tag(loader, suffix, node):
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node, deep=True)
    else:
        value = loader.construct_mapping(node, deep=True)
    return {("Ref" if suffix == "Ref" else "Fn::" + suffix): value}


_CfnLoader.add_multi_constructor("!", _cfn_tag)


def load_template(template_path: Path) -> dict:
    data = yaml.load(Path(template_path).read_text(encoding="utf-8"), Loader=_CfnLoader)
    if not isinstance(data, dict):
        raise ValueError("template is not a YAML mapping")
    return data


def _as_list(value: Any) -> list:
    return value if isinstance(value, list) else [value]


def _string_of(value: Any) -> Optional[str]:
    """String content of a literal or an Fn::Sub / Fn::Join-free intrinsic, else None."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and len(value) == 1:
        (key, inner), = value.items()
        if key == "Fn::Sub":
            return inner if isinstance(inner, str) else (inner[0] if inner and isinstance(inner[0], str) else None)
    return None


_SERVICE_WIDE_ARN = re.compile(r"^arn:[^:]*:[^:]*:[^:]*:[^:]*:\*$")


def _iam_statements(node: Any, path: str = ""):
    """Yield (path, statement) for every dict that looks like an IAM statement (has Effect)."""
    if isinstance(node, dict):
        if "Effect" in node and any(k in node for k in ("Action", "NotAction", "Resource", "NotResource", "Principal")):
            yield path, node
        for key, value in node.items():
            yield from _iam_statements(value, f"{path}/{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _iam_statements(value, f"{path}[{index}]")


def _wildcard_findings(template: dict) -> List[str]:
    findings: List[str] = []
    for path, st in _iam_statements(template.get("Resources", {}), "Resources"):
        if "NotAction" in st or "NotResource" in st:
            findings.append(f"IAM NotAction/NotResource used at {path}")
        for action in _as_list(st.get("Action", [])):
            text = _string_of(action)
            if text is None or text == "*" or text.endswith(":*"):
                findings.append(f"IAM wildcard Action {action!r} at {path}")
        for resource in _as_list(st.get("Resource", [])):
            text = _string_of(resource)
            if text is None:
                continue  # intrinsic reference (GetAtt/Ref): a concrete resource
            if text == "*" or _SERVICE_WIDE_ARN.match(text):
                findings.append(f"IAM wildcard Resource {text!r} at {path}")
        principal = st.get("Principal")
        if principal == "*" or (isinstance(principal, dict) and "*" in _as_list(principal.get("AWS", []))):
            findings.append(f"IAM wildcard Principal at {path}")
    return findings


def _receipt_rule_recipients(template: dict) -> Optional[List[List[str]]]:
    rules = []
    params = template.get("Parameters", {}) or {}
    for name, res in (template.get("Resources", {}) or {}).items():
        if not isinstance(res, dict) or res.get("Type") != "AWS::SES::ReceiptRule":
            continue
        recipients = ((res.get("Properties") or {}).get("Rule") or {}).get("Recipients")
        if not isinstance(recipients, list):
            rules.append([f"<unresolved:{name}>"])
            continue
        resolved = []
        for item in recipients:
            if isinstance(item, dict) and "Ref" in item:
                item = (params.get(item["Ref"]) or {}).get("Default", f"<unresolved:{item['Ref']}>")
            resolved.append(item if isinstance(item, str) else f"<unresolved:{name}>")
        rules.append(resolved)
    return rules or None


def dry_run_validate(template_path: Path) -> dict:
    """Parse the template and enforce recipient allowlist + no-wildcard IAM."""
    errors: List[str] = []
    try:
        template = load_template(Path(template_path))
    except Exception as exc:  # unreadable or unparsable template
        return {"valid": False, "reason": f"Template could not be parsed: {exc}", "errors": [str(exc)]}

    rules = _receipt_rule_recipients(template)
    if rules is None:
        errors.append("No SES ReceiptRule Recipients entry found in template")
    else:
        for recipients in rules:
            if recipients != _REQUIRED_RECIPIENTS:
                errors.append(
                    f"Receipt-rule recipients must be exactly {_REQUIRED_RECIPIENTS}, got {recipients}"
                )
    errors.extend(_wildcard_findings(template))
    return {
        "valid": not errors,
        "reason": "; ".join(errors),
        "errors": errors,
        "receipt_rules": rules or [],
    }


def required_parameters(template: dict) -> List[str]:
    return [k for k, v in (template.get("Parameters", {}) or {}).items() if isinstance(v, dict) and "Default" not in v]


def noecho_parameters(template: dict) -> set:
    return {k for k, v in (template.get("Parameters", {}) or {}).items() if isinstance(v, dict) and v.get("NoEcho")}


# ---------------------------------------------------------------------------
# Botocore-backed call validation and the recording client used by --dry-run
# ---------------------------------------------------------------------------

def validate_call(service: str, method: str, kwargs: dict) -> None:
    """Raise AttributeError for an operation that does not exist, or
    ParamValidationError for parameters that do not match the service model."""
    import botocore.session
    from botocore import xform_name
    from botocore.validate import validate_parameters

    model = botocore.session.get_session().get_service_model(service)
    for op_name in model.operation_names:
        if xform_name(op_name) == method:
            shape = model.operation_model(op_name).input_shape
            if shape is not None:
                validate_parameters(kwargs, shape)
            return
    raise AttributeError(f"{service} has no operation {method!r}")


class RecordingClient:
    """Records and validates every call, returns canned responses; never touches the network."""

    def __init__(self, service: str, log: list, responder: Callable[[str, str, dict], dict]):
        self._service, self._log, self._responder = service, log, responder

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        service = self._service

        def call(**kwargs):
            validate_call(service, name, kwargs)
            self._log.append({"service": service, "method": name, "kwargs": kwargs})
            return self._responder(service, name, kwargs)

        return call


def recording_factory(log: list, responder: Optional[Callable] = None):
    responder = responder or default_responder()
    return lambda service: RecordingClient(service, log, responder)


def default_responder() -> Callable[[str, str, dict], dict]:
    state = {"scan": 0}

    def respond(service, method, kwargs):
        if (service, method) == ("sesv2", "get_email_identity"):
            return {"DkimAttributes": {"Status": "SUCCESS", "Tokens": ["tokenA", "tokenB", "tokenC"]}}
        if (service, method) == ("ses", "describe_receipt_rule_set"):
            return {"Metadata": {"Name": kwargs.get("RuleSetName")}, "Rules": []}
        if (service, method) == ("sesv2", "send_email"):
            return {"MessageId": "dry-run-message-id"}
        if (service, method) == ("dynamodb", "scan"):
            state["scan"] += 1
            return {"Items": [{"session_id": {"S": "email:dry-run"}}] if state["scan"] >= 2 else []}
        if service == "cloudformation" and method == "create_stack":
            return {"StackId": "dry-run-stack-id"}
        return {}

    return respond


def _summarize(entry: dict) -> dict:
    kwargs = {}
    for key, value in entry["kwargs"].items():
        if isinstance(value, (bytes, bytearray)):
            value = {"bytes": len(value), "sha256": hashlib.sha256(value).hexdigest()[:16]}
        kwargs[key] = value
    return {"call": f"{entry['service']}.{entry['method']}", "params": kwargs}


# ---------------------------------------------------------------------------
# Identity creation / activation
# ---------------------------------------------------------------------------

def _region() -> str:
    return os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"


def _error_code(exc: Exception) -> str:
    return getattr(exc, "response", {}).get("Error", {}).get("Code", type(exc).__name__)


def create_and_activate_identity(
    *,
    client_factory: Callable[[str], Any],
    identity: str,
    poll_interval: float = 5.0,
    max_attempts: int = 24,
    never_activate: bool = False,
    rule_set_name: str = RULE_SET_NAME,
    replace_active: bool = False,
    region: Optional[str] = None,
) -> dict:
    """Create the SES domain identity, wait for DKIM SUCCESS, then activate the rule set.

    Receipt rule sets are an SES (v1) API: activation uses the ``ses`` client,
    identities use ``sesv2``.  Activation never happens before SUCCESS, never
    replaces a different already-active rule set unless ``replace_active``.
    """
    sesv2 = client_factory("sesv2")
    try:
        sesv2.create_email_identity(EmailIdentity=identity)
    except Exception as exc:
        if _error_code(exc) != "AlreadyExistsException":
            raise

    first = sesv2.get_email_identity(EmailIdentity=identity)
    tokens = first.get("DkimAttributes", {}).get("Tokens", [])
    if tokens:
        print("DKIM CNAME records (add these to your DNS):")
        for i, token in enumerate(tokens, 1):
            print(f"  {i}. Name: {token}._domainkey.{identity}")
            print("     Type: CNAME")
            print(f"     Value: {token}.dkim.amazonses.com")
    print("MX record (inbound mail from Amazon SES):")
    print(f"  Name: {identity}")
    print("  Type: MX")
    print(f"  Value: 10 inbound-smtp.{region or _region()}.amazonaws.com")

    status = "PENDING"
    for attempt in range(1, max_attempts + 1):
        resp = sesv2.get_email_identity(EmailIdentity=identity)
        status = resp.get("DkimAttributes", {}).get("Status", "PENDING")
        if status == "SUCCESS":
            break
        if attempt < max_attempts:
            time.sleep(poll_interval)
    else:
        return {
            "activated": False,
            "reason": f"Identity {identity} did not reach SUCCESS after {max_attempts} attempts",
        }

    if never_activate:
        return {"activated": False, "identity_status": status, "reason": "never_activate set"}

    ses = client_factory("ses")
    try:
        ses.describe_receipt_rule_set(RuleSetName=rule_set_name)
    except Exception as exc:
        if _error_code(exc) == "RuleSetDoesNotExist":
            return {"activated": False, "identity_status": status,
                    "reason": f"Rule set {rule_set_name} does not exist yet; deploy the stack first"}
        raise
    active = (ses.describe_active_receipt_rule_set() or {}).get("Metadata", {}).get("Name")
    if active and active != rule_set_name and not replace_active:
        return {"activated": False, "identity_status": status,
                "reason": f"Another rule set ({active}) is active; refusing to replace it"}
    ses.set_active_receipt_rule_set(RuleSetName=rule_set_name)
    return {"activated": True, "identity_status": status, "rule_set": rule_set_name}


def ensure_identity_active(**kwargs) -> dict:
    """Legacy entry-point; delegates to create_and_activate_identity."""
    return create_and_activate_identity(**kwargs)


# ---------------------------------------------------------------------------
# Inventory and destroy script
# ---------------------------------------------------------------------------

def _fetch_tagged_resources(client: Any, tag_key: str, tag_value: str) -> List[str]:
    tagged, token = [], None
    while True:
        params: dict = {"TagFilters": [{"Key": tag_key, "Values": [tag_value]}], "ResourcesPerPage": 100}
        if token:
            params["PaginationToken"] = token
        resp = client.get_resources(**params)
        for mapping in resp.get("ResourceTagMappingList", []):
            tags = {t["Key"]: t["Value"] for t in mapping.get("Tags", [])}
            if tags.get(tag_key) == tag_value:
                tagged.append(mapping["ResourceARN"])
        token = resp.get("PaginationToken", "")
        if not token:
            break
    return tagged


def classify_arn(arn: str, region: str) -> dict:
    """Return {"arn", "service", "kind", "name", "command"}; command is '' when it must be done by hand."""
    parts = arn.split(":")
    out = {"arn": arn, "service": parts[2] if len(parts) > 2 else "", "kind": "", "name": "", "command": ""}
    if len(parts) < 6:
        return out
    service, tail = parts[2].lower(), ":".join(parts[5:])
    r = f"--region {region}"
    if service == "lambda" and tail.startswith("function:"):
        out.update(kind="function", name=parts[6], command=f"aws lambda delete-function --function-name {parts[6]} {r}")
    elif service == "s3":
        out.update(kind="bucket", name=parts[5], command=f"aws s3 rb s3://{parts[5]} --force")
    elif service == "dynamodb" and tail.startswith("table/"):
        name = tail.split("/")[1]
        out.update(kind="table", name=name, command=f"aws dynamodb delete-table --table-name {name} {r}")
    elif service == "sns":
        out.update(kind="topic", name=parts[5], command=f"aws sns delete-topic --topic-arn {arn} {r}")
    elif service == "sqs":
        url = f"https://sqs.{parts[3]}.amazonaws.com/{parts[4]}/{parts[5]}"
        out.update(kind="queue", name=parts[5], command=f"aws sqs delete-queue --queue-url {url} {r}")
    elif service == "cloudformation" and tail.startswith("stack/"):
        name = tail.split("/")[1]
        out.update(kind="stack", name=name, command=f"aws cloudformation delete-stack --stack-name {name} {r}")
    elif service == "logs" and tail.startswith("log-group:"):
        name = tail[len("log-group:"):]
        name = name[:-2] if name.endswith(":*") else name
        out.update(kind="log-group", name=name, command=f"aws logs delete-log-group --log-group-name {name} {r}")
    elif service == "cloudwatch" and tail.startswith("alarm:"):
        name = tail[len("alarm:"):]
        out.update(kind="alarm", name=name, command=f"aws cloudwatch delete-alarms --alarm-names {name} {r}")
    return out


def inventory(*, client_factory: Callable[[str], Any], region: str = "us-east-1") -> List[dict]:
    tagging = client_factory("resourcegroupstaggingapi")
    return [classify_arn(a, region) for a in _fetch_tagged_resources(tagging, PROJECT_TAG_KEY, PROJECT_TAG_VALUE)]


def generate_destroy_script(
    *,
    client_factory: Callable[[str], Any],
    output_path: Optional[Path] = None,
    region: str = "us-east-1",
    rule_set_name: str = RULE_SET_NAME,
) -> Path:
    """Write a destroy script covering only resources tagged project=awscds.

    Order matters: deactivate our SES rule set, empty buckets (a stack cannot
    delete a non-empty bucket), delete stacks and wait, then sweep leftovers.
    """
    items = inventory(client_factory=client_factory, region=region)
    output_path = output_path or (Path.cwd() / "destroy_awscds.sh")
    lines = [
        "#!/usr/bin/env bash",
        "# destroy_awscds.sh - remove only resources tagged project=awscds",
        "set -uo pipefail",
        f"# region: {region}   resources found: {len(items)}",
        "",
        "# 1. stop inbound mail: deactivate our rule set only if it is the active one",
        f'if [ "$(aws ses describe-active-receipt-rule-set --region {region} --query Metadata.Name --output text 2>/dev/null)" = "{rule_set_name}" ]; then',
        f"  aws ses set-active-receipt-rule-set --region {region}",
        "fi",
        "",
    ]
    owned, skipped, buckets, stacks, rest = [], [], [], [], []
    for item in items:
        if "awscds" not in item["arn"].lower():
            skipped.append(f"# SKIPPED (name does not contain awscds): {item['arn']}")
        elif not item["command"]:
            rest.append(f"# MANUAL (no automatic delete for this type): {item['arn']}")
        elif item["kind"] == "bucket":
            buckets.append(item)
        elif item["kind"] == "stack":
            stacks.append(item)
        else:
            rest.append(f"{item['command']} 2>/dev/null || true")
    lines.append("# 2. empty buckets so stack deletion cannot fail on them")
    for b in buckets:
        lines.append(f"aws s3 rm s3://{b['name']} --recursive 2>/dev/null || true")
    lines.append("")
    lines.append("# 3. delete stacks (removes everything the stack created)")
    for s in stacks:
        lines.append(s["command"])
        lines.append(f"aws cloudformation wait stack-delete-complete --stack-name {s['name']} --region {region} || true")
    lines.append("")
    lines.append("# 4. sweep anything tagged that is still left (idempotent)")
    for b in buckets:
        lines.append(f"{b['command']} 2>/dev/null || true")
    lines.extend(rest)
    lines.extend(skipped)
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    output_path.chmod(0o755)
    return output_path


def _arn_to_delete_command(arn: str) -> str:
    """Kept for callers of the previous API."""
    return classify_arn(arn, _region())["command"]


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------

def smoke_test_send(
    *,
    client_factory: Callable[[str], Any],
    to_address: str,
    from_address: Optional[str] = None,
    poll_interval: float = 10.0,
    max_polls: int = 12,
    question: str = "What is 2+2?",
) -> dict:
    """Send ONE mail to the judges mailbox from a verified owner address, then look for
    the conversation in the sessions table (matched by a unique nonce in the question).

    The orchestrator ignores mail from its own mailbox (loop guard), so the sender
    must be a different verified identity."""
    if to_address not in _ALLOWED_TO_ADDRESSES:
        raise ValueError(
            f"Recipient {to_address!r} is not allowlisted; only {sorted(_ALLOWED_TO_ADDRESSES)} are permitted"
        )
    from_address = from_address or os.environ.get("AWSCDS_SMOKE_FROM")
    if not from_address or from_address.strip().lower() in {a.lower() for a in _ALLOWED_TO_ADDRESSES}:
        raise ValueError(
            "smoke test needs a verified sender other than the judges mailbox "
            "(the orchestrator ignores its own mailbox to prevent mail loops)"
        )

    sesv2 = client_factory("sesv2")
    dynamodb = client_factory("dynamodb")
    table_name = os.environ.get("SESSIONS_TABLE", "awscds-SessionsTable")
    nonce = "smoke-" + uuid.uuid4().hex[:10]

    sent = sesv2.send_email(
        FromEmailAddress=from_address,
        Destination={"ToAddresses": [to_address]},
        Content={"Simple": {
            "Subject": {"Data": f"AWSCDS smoke test {nonce}"},
            "Body": {"Text": {"Data": f"{question} ({nonce})"}},
        }},
    )
    for _ in range(max_polls):
        resp = dynamodb.scan(
            TableName=table_name,
            FilterExpression="contains(last_question, :n)",
            ExpressionAttributeValues={":n": {"S": nonce}},
        )
        if resp.get("Items"):
            return {"sent": True, "reply_found": True, "nonce": nonce, "message_id": (sent or {}).get("MessageId")}
        time.sleep(poll_interval)
    return {"sent": True, "reply_found": False, "nonce": nonce, "message_id": (sent or {}).get("MessageId")}


# ---------------------------------------------------------------------------
# Package and deploy
# ---------------------------------------------------------------------------

_CODEURI_LINE = re.compile(r"^(\s*CodeUri:\s*)(\S+)\s*$", re.MULTILINE)


def build_code_zip(src_path: Path) -> bytes:
    with tempfile.TemporaryDirectory() as tmp:
        zip_path = Path(tmp) / "code.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, dirs, files in os.walk(src_path):
                dirs[:] = sorted(d for d in dirs if d != "__pycache__")
                for file in sorted(files):
                    if file.endswith((".pyc", ".pyo")):
                        continue
                    full = Path(root) / file
                    zf.write(full, full.relative_to(src_path))
        return zip_path.read_bytes()


def package_and_deploy(
    *,
    client_factory: Callable[[str], Any],
    src_path: Path,
    bucket: str,
    stack_name: str,
    template_path: Path,
    parameters: Optional[Dict[str, str]] = None,
    region: Optional[str] = None,
) -> dict:
    """Validate the template, upload code zip + rewritten template, create the tagged stack."""
    if not stack_name.startswith("awscds-"):
        stack_name = f"awscds-{stack_name}"
    template_path = Path(template_path)

    verdict = dry_run_validate(template_path)
    if not verdict["valid"]:
        raise DeployRefused(f"template failed validation: {verdict['reason']}")
    template = load_template(template_path)
    parameters = dict(parameters or {})
    missing = [p for p in required_parameters(template) if p not in parameters]
    if missing:
        raise DeployRefused(f"required template parameters not supplied: {missing}")
    unknown = [p for p in parameters if p not in (template.get("Parameters") or {})]
    if unknown:
        raise DeployRefused(f"parameters not declared in template: {unknown}")

    region = region or _region()
    code = build_code_zip(Path(src_path))
    code_key = f"{stack_name}/code-{hashlib.sha256(code).hexdigest()[:12]}.zip"
    text = template_path.read_text(encoding="utf-8")
    text, n = _CODEURI_LINE.subn(lambda m: f"{m.group(1)}s3://{bucket}/{code_key}", text)
    if n != 1:
        raise DeployRefused(f"expected exactly one CodeUri line in template, found {n}")
    body = text.encode("utf-8")
    template_key = f"{stack_name}/template-{hashlib.sha256(body).hexdigest()[:12]}.yaml"

    s3 = client_factory("s3")
    cfn = client_factory("cloudformation")
    s3.put_object(Bucket=bucket, Key=code_key, Body=code)
    s3.put_object(Bucket=bucket, Key=template_key, Body=body)
    cfn.create_stack(
        StackName=stack_name,
        TemplateURL=f"https://{bucket}.s3.{region}.amazonaws.com/{template_key}",
        Parameters=[{"ParameterKey": k, "ParameterValue": v} for k, v in sorted(parameters.items())],
        Capabilities=["CAPABILITY_IAM", "CAPABILITY_AUTO_EXPAND"],
        Tags=[{"Key": PROJECT_TAG_KEY, "Value": PROJECT_TAG_VALUE}],
    )
    return {"stack_name": stack_name, "bucket": bucket, "key": code_key, "template_key": template_key}


# ---------------------------------------------------------------------------
# Negative control helpers
# ---------------------------------------------------------------------------

def verify_create_activate_called(*, client: Any, identity: str, require_create: bool = True,
                                  require_activate: bool = True) -> None:
    """Auditor check: the client's call log must show create and activate calls."""
    call_log = (getattr(client, "call_log", None) or getattr(client, "calls_made", None)
                or getattr(client, "calls", None) or [])
    created = activated = False
    for entry in call_log:
        if isinstance(entry, tuple):
            method = entry[0]
        elif isinstance(entry, dict):
            method = entry.get("method", "")
        else:
            continue
        created = created or method in ("create", "create_email_identity")
        activated = activated or method in ("activate", "set_active_receipt_rule_set")
    if require_create and not created:
        raise AssertionError("NEGATIVE CONTROL FAILED: create_email_identity was never called")
    if require_activate and not activated:
        raise AssertionError("NEGATIVE CONTROL FAILED: set_active_receipt_rule_set was never called")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def run_dry_run(args) -> int:
    verdict = dry_run_validate(Path(args.template))
    print(f"TEMPLATE {args.template}")
    print(f"  valid: {verdict['valid']}")
    for rule in verdict.get("receipt_rules", []):
        print(f"  receipt-rule recipients: {rule}")
    for err in verdict.get("errors", []):
        print(f"  ERROR: {err}")
    if not verdict["valid"]:
        return 2
    log: list = []
    factory = recording_factory(log)
    params = dict(p.split("=", 1) for p in args.param)
    template = load_template(Path(args.template))
    for name in required_parameters(template):
        params.setdefault(name, "https://dry-run.invalid/v1" if name == "NexusEndpoint" else "dry-run")
    package_and_deploy(client_factory=factory, src_path=Path(args.src), bucket=args.bucket or "dry-run-bucket",
                       stack_name=args.stack_name, template_path=Path(args.template), parameters=params,
                       region=args.region)
    create_and_activate_identity(client_factory=factory, identity=args.domain, poll_interval=0, max_attempts=1,
                                 region=args.region)
    if args.smoke_from:
        smoke_test_send(client_factory=factory, to_address="judges@cds.3dogs.ai", from_address=args.smoke_from,
                        poll_interval=0, max_polls=3)
    noecho = noecho_parameters(template)
    print("PLANNED AWS CALLS (validated against the botocore service model, none executed):")
    for i, entry in enumerate(log, 1):
        summary = _summarize(entry)
        if entry["method"] == "create_stack":
            for p in summary["params"].get("Parameters", []):
                if p["ParameterKey"] in noecho:
                    p["ParameterValue"] = "****"
        print(f"  {i:2d}. {summary['call']} {json.dumps(summary['params'], sort_keys=True, default=str)}")
    print(f"TOTAL {len(log)} planned calls; write operations: "
          + ", ".join(sorted({f"{e['service']}.{e['method']}" for e in log if not e['method'].startswith(('get_', 'describe_', 'list_', 'scan', 'query'))})))
    return 0


def _real_factory(region: str):
    import boto3  # imported lazily: only real, confirmed actions need it
    return lambda service: boto3.client(service, region_name=region)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="AWS CDS entry deploy helper")
    act = ap.add_mutually_exclusive_group(required=True)
    act.add_argument("--dry-run", action="store_true", help="validate template and print planned AWS calls")
    act.add_argument("--deploy", action="store_true")
    act.add_argument("--activate-identity", action="store_true", help="create SES identity, wait, activate rule set")
    act.add_argument("--smoke", action="store_true")
    act.add_argument("--inventory", action="store_true")
    act.add_argument("--destroy-script", metavar="PATH")
    ap.add_argument("--template", default="infra/template.yaml")
    ap.add_argument("--src", default="src")
    ap.add_argument("--bucket")
    ap.add_argument("--stack-name", default="awscds-demo")
    ap.add_argument("--region", default=_region())
    ap.add_argument("--domain", default=DEFAULT_IDENTITY)
    ap.add_argument("--param", action="append", default=[], metavar="KEY=VALUE")
    ap.add_argument("--smoke-from", help="verified sender for the smoke mail (not the judges mailbox)")
    ap.add_argument("--yes", action="store_true", help="required for any action that calls AWS")
    args = ap.parse_args(argv)
    if args.dry_run:
        return run_dry_run(args)
    if not args.yes:
        print("refusing to call AWS without --yes", file=sys.stderr)
        return 3
    factory = _real_factory(args.region)
    if args.deploy:
        if not args.bucket:
            print("--bucket is required for --deploy", file=sys.stderr)
            return 3
        params = dict(p.split("=", 1) for p in args.param)
        print(json.dumps(package_and_deploy(client_factory=factory, src_path=Path(args.src), bucket=args.bucket,
                                            stack_name=args.stack_name, template_path=Path(args.template),
                                            parameters=params, region=args.region)))
    elif args.activate_identity:
        print(json.dumps(create_and_activate_identity(client_factory=factory, identity=args.domain,
                                                      region=args.region)))
    elif args.smoke:
        print(json.dumps(smoke_test_send(client_factory=factory, to_address="judges@cds.3dogs.ai",
                                         from_address=args.smoke_from)))
    elif args.inventory:
        print(json.dumps(inventory(client_factory=factory, region=args.region), indent=2))
    elif args.destroy_script:
        print(generate_destroy_script(client_factory=factory, output_path=Path(args.destroy_script),
                                      region=args.region))
    return 0


if __name__ == "__main__":
    sys.exit(main())
