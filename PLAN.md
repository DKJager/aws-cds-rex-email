> **Status note (23 Oct 2026).** This file is the original 19 Oct plan and is kept for the record. The submitted entry is **email only**: no SMS number is provisioned or claimed, and the engine is this repository's own `engine/` Lambda on Amazon Bedrock Converse (not an external service). Where this plan mentions SMS or an external engine, `README.md` and `docs/architecture.svg` are authoritative.

# PLAN -- awscds thin-wrapper build

Thin-wrapper hackathon demo. All core reasoning lives in the Nexus
engine (shipped in `engine/`); this repo transports messages, manages
session state, and renders the demo. The engine Lambda calls Amazon
Bedrock Converse directly; no Bedrock AgentCore, no EventBridge.

## Sec. 1 Scope

- Inbound: SMS via AWS End User Messaging (pinpoint-sms-voice-v2 events)
  and email via Amazon SES receipt rule on `judges@cds.3dogs.ai`.
- Orchestrator: AWS Lambda in `src/orchestrator.py`.
- Engine: shipped in `engine/`; `engine/app.py` is a Python 3.12 Lambda
  that calls Amazon Bedrock Converse via `bedrock-runtime` (no Bedrock
  AgentCore); configured solely by `NEXUS_ENDPOINT` / `NEXUS_TOKEN`
  environment variables on the orchestrator side. The deployed demo uses
  the engine's Lambda Function URL.
- Outbound: SMS via pinpoint-sms-voice-v2 `SendTextMessage`, email via
  SES v2 `SendEmail`, both sent from `judges@cds.3dogs.ai` and signed
  "Rex, 3Dogs Nexus".

## Sec. 2 Architecture authority (residual fixes #850/#851)

The orchestrator is `src/orchestrator.py` only; it does not call Amazon
Bedrock AgentCore and no EventBridge resources are used. This sentence is
binding: `docs/architecture.svg` draws only the components the code
actually uses (inbound SMS/SES, the Lambda orchestrator, the session
store, the external Nexus engine client, outbound SMS v2 / SES v2) and
must not show Amazon Bedrock AgentCore or EventBridge. The word
"agentic" in this repo refers to the plan-call-clarify-reply loop
implemented by the orchestrator delegating reasoning to the external
Nexus engine, not to any specific AWS agent service.

The engine that performs the reasoning is shipped in this repository
under `engine/`. It is an AWS SAM application (`engine/template.yaml`)
that deploys a Lambda (`awscds-engine`) and a DynamoDB session table
(`awscds-engine-sessions`). The Lambda (`engine/app.py`) calls the
Amazon Bedrock Converse API with models `openai.gpt-oss-120b-1:0`
(primary) and `amazon.nova-pro-v1:0` (fallback), temperature 0.2, 25 s
timeout. It keeps the last 6 turns per `session_id` in DynamoDB (TTL
2 days), answers as 'Rex, 3Dogs Nexus' in at most about 180 words, and
returns exactly one of `{"answer","steps"}` or `{"clarify","steps"}`.

## Sec. 3 Engine contract (request/response schema -- binding)

The wrapper and the test stubs both implement against exactly this
schema so neither can diverge from the real Nexus service.

### Request (orchestrator -> Nexus, POST `NEXUS_ENDPOINT`, bearer `NEXUS_TOKEN`)

```json
{
  "session_id": "string, stable per customer per channel",
  "channel": "sms | email",
  "customer_address": "string (E.164 number or email address)",
  "message": "string, the inbound customer text",
  "feedback": {
    "present": false,
    "prior_answer_id": "string or null",
    "correction": "string or null"
  }
}
```

`feedback.present` is `true` only when the inbound message is a
"that's wrong"-style correction; `prior_answer_id` and `correction`
are then populated so the next answer improves (RSI loop).

### Response (Nexus -> orchestrator)

```json
{
  "answer_id": "string, unique id for feedback correlation",
  "action": "answer | clarify",
  "text": "string, the reply body to send on the same channel",
  "feedback_acknowledged": false
}
```

Discriminator rules (binding):

- `action` is the single clarify-vs-answer discriminator. Exactly two
  values are valid: `"answer"` and `"clarify"`. Any other value, or a
  missing `action` field, is treated as an error and the orchestrator
  replies with a generic retry message.
- When `action` is `"clarify"`, `text` is a clarifying question and the
  orchestrator stores `answer_id` in session state as pending.
- `feedback_acknowledged` is `true` iff the request carried
  `feedback.present: true` and the engine accepted the feedback.

`tests/test_orchestrator.py` (item 10) MUST assert, using stubs
implementing this schema verbatim:

1. a response with `action: "clarify"` causes the orchestrator to send
   the clarifying question on the same channel and store the pending
   `answer_id`;
2. a "that's wrong" inbound message causes the orchestrator to forward
   `feedback` (with `prior_answer_id` and `correction`) to the engine and
   to handle `feedback_acknowledged` in the response;
3. field names above are asserted literally (schema conformance), so a
   stub drifting from the real service fails the test.

## Sec. 4 Module layout

- `src/orchestrator.py` -- Lambda entry points (SMS event, SES event);
  session handling; calls the channels module for all sends.
- `src/channels.py` -- the only module that imports and calls
  `pinpoint_sms_voice_v2` (SendTextMessage) and `sesv2` (SendEmail).
- `src/nexus_client.py` -- HTTPS client for Sec. 3; endpoint/token only from
  environment variables.
- `src/sessions.py` -- session/pending-answer persistence.
- `engine/app.py` -- Lambda that calls Amazon Bedrock Converse; keeps the
  last 6 turns per `session_id` in DynamoDB; returns `{"answer","steps"}`
  or `{"clarify","steps"}`.
- `engine/template.yaml` -- SAM stack `awscds-engine`: Lambda `awscds-engine`,
  DynamoDB table `awscds-engine-sessions` (PAY_PER_REQUEST, TTL on `expires_at`),
  IAM role `awscds-engine-role`.
- `infra/template.yaml` -- Lambda, SES receipt rule (Recipients exactly
  `["judges@cds.3dogs.ai"]`), End User Messaging / pinpoint-sms-voice-v2
  and SES v2 resources only; no `AWS::Pinpoint::*` (v1) resources.

## Sec. 5 Tests

- `tests/test_orchestrator.py` -- clarify flow, feedback forwarding, and
  schema conformance per Sec. 3.
- `tests/test_nexus_client.py` -- tests for the external engine HTTPS client.
- `tests/test_engine_app.py` -- engine Lambda tests (Bedrock and DynamoDB
  mocked, no network).
- `tests/test_receipt_rule.py` -- parses `infra/template.yaml` and asserts
  the SES receipt-rule Recipients list is exactly
  `["judges@cds.3dogs.ai"]`; negative control: any other recipient
  (including `sender@example.org` or any apex `3dogs.ai` mailbox) fails.
- No dependency installation, shell execution, or outbound calls in
  tests; all AWS and Nexus calls are stubbed.

## Sec. 6 Acceptance criteria

1. `docs/architecture.svg` exists and matches Sec. 2 exactly.
2. `infra/template.yaml` uses only pinpoint-sms-voice-v2 and SES v2
   resources and IAM actions.
3. The wrapper (via `src/channels.py`, called by the orchestrator)
   imports and calls both `pinpoint-sms-voice-v2` SendTextMessage and
   `sesv2` SendEmail at runtime.
4. README.md judge-access section: email `judges@cds.3dogs.ai`; "The
   judges' SMS number is added at submission."; references
   `docs/architecture.svg` as the diagram; no placeholder phone number,
   no example.com mailbox, no mention of `docs/architecture.md`.
5. No credentials, tokens, keys, or production data in the repo; all
   secrets via environment variables.

## Sec. 7 Safety and risk (unchanged)

(Content in this section is preserved exactly as originally written; no
changes requested or applied.)
