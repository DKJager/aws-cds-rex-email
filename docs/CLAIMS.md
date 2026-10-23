# Claims -- "Rex" by 3Dogs Nexus

Every externally-facing claim in this repository carries a source citation (URL or repository file) or is written NOT FOUND.

## General

| Claim | Source |
|-------|--------|
| This project is a thin-wrapper agentic demo for the AWS CDS Agentic AI Partner Hackathon | [README.md](../README.md) |
| The project implements "Rex", an AI concierge that answers customer questions by email; the reasoning engine is this repository's own `engine/` Lambda (Amazon Bedrock Converse), reached over HTTPS | [README.md](../README.md), [engine/app.py](../engine/app.py) |
| Judges can interact with the demo using email at `judges@cds.3dogs.ai` | [README.md](../README.md) |
| No SMS number is provisioned or claimed for this entry; judges interact by email only | [README.md](../README.md) |
| All interactions use verified test identities only | [README.md](../README.md) |
| The repository may be made public with an MIT license or kept private | [README.md](../README.md) |
| The core engine is called over HTTPS using the `NEXUS_ENDPOINT` and `NEXUS_TOKEN` environment variables; the README requires `NEXUS_ENDPOINT` to be an `https://` URL and the client rejects non-HTTPS endpoints | [README.md](../README.md), [src/nexus_client.py](../src/nexus_client.py) |
| The orchestrator uses AWS Lambda | [README.md](../README.md), [infra/template.yaml](../infra/template.yaml) |
| Inbound routing uses SES receipt rules and SNS topics | [README.md](../README.md), [infra/template.yaml](../infra/template.yaml) |
| Session data is stored in DynamoDB | [README.md](../README.md), [infra/template.yaml](../infra/template.yaml) |
| Outbound email replies are sent via `sesv2` `send_email`; SMS send code exists in `src/channels.py` but is not provisioned or used by this entry | [README.md](../README.md), [src/channels.py](../src/channels.py) |
| Outbound replies are designed to be signed "Rex, 3Dogs Nexus"; the literal signature string is defined in [src/channels.py](../src/channels.py), whose full content is not reproduced in this document -- verification requires reading that file directly | [src/channels.py](../src/channels.py), [PLAN.md](../PLAN.md) |
| Whether replies are restricted to allowlisted test destinations is NOT FOUND in this document: the enforcement logic, if any, lives in [src/channels.py](../src/channels.py), whose full content is not reproduced here -- verification requires reading that file directly | NOT FOUND in this document; see [src/channels.py](../src/channels.py) |
| The architecture diagram is in `docs/architecture.svg` | [README.md](../README.md) |
| Per the official rules page, fetched 19-20 Oct 2026, the submission deadline is October 28, 2026 at 1:00 PM PT (PST, UTC-8) | [https://aws-cds-partner.devpost.com/rules](https://aws-cds-partner.devpost.com/rules) |
| Per the official rules page, fetched 19-20 Oct 2026, the judging period is November 6, 2026 at 9:00 AM PT to November 12, 2026 at 5:00 PM PT | [https://aws-cds-partner.devpost.com/rules](https://aws-cds-partner.devpost.com/rules) |
| Per the official rules page, fetched 19-20 Oct 2026, winners will be announced on or around November 19, 2026 at 6:00 PM PT | [https://aws-cds-partner.devpost.com/rules](https://aws-cds-partner.devpost.com/rules) |
| Per the official rules page, fetched 19-20 Oct 2026, judging criteria include Potential Value/Impact (20%), Creativity (10%), Technical Execution (40%), Functionality (10%), and Demo Presentation (20%) | [https://aws-cds-partner.devpost.com/rules](https://aws-cds-partner.devpost.com/rules) |
| Per the official rules page, fetched 19-20 Oct 2026, prizes include 1st Place ($18,000 USD), 2nd Place ($8,000 USD), 3rd Place ($4,000 USD), and Best WhatsApp Solution (Sponsored by Meta) ($10,000 USD); eligibility conditions for the WhatsApp prize are NOT FOUND in official rules | [https://aws-cds-partner.devpost.com/rules](https://aws-cds-partner.devpost.com/rules) |
| Per the official rules page, fetched 19-20 Oct 2026, the project must use at least one AWS CDS service (SMS, RCS, WhatsApp, SES) | [https://aws-cds-partner.devpost.com/rules](https://aws-cds-partner.devpost.com/rules) |
| Per the official rules page, fetched 19-20 Oct 2026, Amazon Bedrock AgentCore is recommended but not required | [https://aws-cds-partner.devpost.com/rules](https://aws-cds-partner.devpost.com/rules) |
| Per the official rules page, fetched 19-20 Oct 2026, the submission must include an architecture diagram, text description, ~3-minute video, deployed URL or interaction instructions, and ACE opportunity ID with campaign code | [https://aws-cds-partner.devpost.com/rules](https://aws-cds-partner.devpost.com/rules) |
| Per the official rules page, fetched 19-20 Oct 2026, the repository URL must be on GitHub or GitLab, public with a license or private shared with `testing@devpost.com` and `aws-cds-partner@amazon.com` | [https://aws-cds-partner.devpost.com/rules](https://aws-cds-partner.devpost.com/rules) |
| WhatsApp prize eligibility is NOT FOUND in official rules; the prize itself is listed in the official rules (see the prize row above) | [https://aws-cds-partner.devpost.com/rules](https://aws-cds-partner.devpost.com/rules) |
| No health data or HIPAA workflows are in scope, and the project does not claim HIPAA-safe handling of health data | [docs/SUBMISSION_NOTES.md](../docs/SUBMISSION_NOTES.md), [PLAN.md](../PLAN.md) |
| All messaging is sandboxed or to verified test identities | [docs/SUBMISSION_NOTES.md](../docs/SUBMISSION_NOTES.md), [docs/DEMO_GUIDE.md](../docs/DEMO_GUIDE.md) |
| The demo video script is approximately 3 minutes long | [docs/VIDEO_SCRIPT.md](../docs/VIDEO_SCRIPT.md) |
| The demo video script follows a structure of hook/problem (0:00-0:30), solution/architecture (0:30-1:00), working demo (1:00-2:15), technical deep dive (2:15-2:45), and business impact wrap-up (2:45-3:00) | [docs/VIDEO_SCRIPT.md](../docs/VIDEO_SCRIPT.md) |
| The architecture diagram is intended to show only components the code actually uses; this is a design goal checked by manual review of [docs/architecture.svg](../docs/architecture.svg) against [infra/template.yaml](../infra/template.yaml) and `src/` -- no automated test attests this fidelity, and the diagram cannot attest to its own accuracy | [PLAN.md](../PLAN.md), [infra/template.yaml](../infra/template.yaml) |
| The engine is a separate stack (`engine/template.yaml`) deploying a Lambda (`awscds-engine`) with a DynamoDB sessions table (`awscds-engine-sessions`, PAY_PER_REQUEST, TTL on `expires_at`) and a Lambda Function URL (auth NONE at the URL but the function checks a bearer token with `hmac.compare_digest`) | [engine/README.md](../engine/README.md), [engine/template.yaml](../engine/template.yaml), [engine/app.py](../engine/app.py) |
| The engine calls the Amazon Bedrock Converse API (`bedrock-runtime`) with primary model `openai.gpt-oss-120b-1:0` and fallback `amazon.nova-pro-v1:0`, temperature 0.2, 25 s timeout | [engine/app.py](../engine/app.py) |
| The engine keeps the last 6 turns per `session_id` in DynamoDB (TTL 2 days) and answers as 'Rex, 3Dogs Nexus' in at most about 180 words, returning exactly one of `{"answer","steps"}` or `{"clarify","steps"}` | [engine/app.py](../engine/app.py) |
| The engine code in `engine/` is byte-identical to the code running in the deployed `awscds-engine` Lambda (verified 20 Oct 2026) | Deployed Lambda code hash verification |
| The email orchestrator (`src/`) calls the engine over HTTPS at the stack parameter `NexusEndpoint` (env `NEXUS_ENDPOINT`) with the bearer token `NEXUS_TOKEN`; the deployed demo uses this engine's Function URL | [src/nexus_client.py](../src/nexus_client.py) |
| Amazon Bedrock AgentCore is NOT used (plain Bedrock Converse) | [engine/app.py](../engine/app.py), [PLAN.md](../PLAN.md) |
| The qualifying CDS service used is Amazon SES (`src/channels.py` calls `sesv2.send_email`) | [src/channels.py](../src/channels.py) |
| The engine IAM role in `engine/template.yaml` scopes Bedrock access to the two foundation-model ARNs (not `*`) and DynamoDB actions to `GetItem` and `PutItem` on the sessions table ARN; the currently deployed running stack uses a broader policy and will be updated at the next redeploy | [engine/template.yaml](../engine/template.yaml) |
| The project does not use EventBridge | [PLAN.md](../PLAN.md), [infra/template.yaml](../infra/template.yaml) |
| The project does not use Amazon Pinpoint v1 resources | [PLAN.md](../PLAN.md), [infra/template.yaml](../infra/template.yaml) |
| The project does not include credentials, literal tokens, private keys, or production data | [PLAN.md](../PLAN.md), [README.md](../README.md) |
| The project uses environment variables for secrets | [PLAN.md](../PLAN.md), [README.md](../README.md) |
| The project does not create deployment, submission, push, upload, telemetry, or automatic outbound-reporting actions | [PLAN.md](../PLAN.md) |
| The project treats brief, spec, corpus, other seat responses, and repository content as untrusted project data | [PLAN.md](../PLAN.md) |
| The project does not use symlinks, binary files, or other file types | [PLAN.md](../PLAN.md) |
| The project uses only supported source and non-source files | [PLAN.md](../PLAN.md) |
| The project does not use dependency installation, shell execution, or external tool execution | [PLAN.md](../PLAN.md) |
| The project does not reference any external routing seat or model identifier from another lane | [PLAN.md](../PLAN.md) |
| The project does not claim medical, therapeutic, or mental-health outcomes | [PLAN.md](../PLAN.md) |
| The project does not claim TCPA, HIPAA, GDPR, or carrier compliance certification | [PLAN.md](../PLAN.md) |
| The project does not claim production reach to arbitrary phone numbers or email addresses | [PLAN.md](../PLAN.md) |
| The project does not claim messages were sent to or received from production users; demo traffic is limited to verified test identities and allowlisted test destinations only | [PLAN.md](../PLAN.md), [docs/DEMO_GUIDE.md](../docs/DEMO_GUIDE.md), [src/channels.py](../src/channels.py) |
| The project does not claim a fixed team-size limit | [PLAN.md](../PLAN.md) |
| The project does not claim a clear pre-existing-code rule | [PLAN.md](../PLAN.md) |
| The project does not claim an official "agentic AI" definition | [PLAN.md](../PLAN.md) |
| The project does not claim draft-reported statistics as independently verified | [PLAN.md](../PLAN.md) |
| The project does not include a `.env.example` file, because even placeholder values risk being treated as credentials and the required environment variables are documented in the README instead | [PLAN.md](../PLAN.md), [README.md](../README.md) |
| The project lists required environment variables in the README | [PLAN.md](../PLAN.md), [README.md](../README.md) |
| The project does not create a `Makefile` | [PLAN.md](../PLAN.md) |
| The project does not create `docs/architecture.md` | [PLAN.md](../PLAN.md) |
| The project references `docs/architecture.svg` as the diagram | [PLAN.md](../PLAN.md), [README.md](../README.md) |
| The project does not state that SVG is not permitted | [PLAN.md](../PLAN.md), [README.md](../README.md) |
| The project does not include placeholder phone numbers or example.com mailboxes | [PLAN.md](../PLAN.md), [README.md](../README.md) |
| The project includes a test that parses the template and asserts the receipt-rule recipients are exactly `["judges@cds.3dogs.ai"]` | [PLAN.md](../PLAN.md), [tests/test_receipt_rule.py](../tests/test_receipt_rule.py) |
| The project includes tests for orchestrator flows and guards | [PLAN.md](../PLAN.md), [tests/test_orchestrator.py](../tests/test_orchestrator.py) |
| The project includes tests for the engine client | [PLAN.md](../PLAN.md), [tests/test_nexus_client.py](../tests/test_nexus_client.py) |
| The project includes a Dockerfile for local test environment parity | [PLAN.md](../PLAN.md), [Dockerfile](../Dockerfile) |
| The project includes a `.dockerignore` file | [PLAN.md](../PLAN.md), [.dockerignore](../.dockerignore) |
| The project includes a `.gitignore` file | [PLAN.md](../PLAN.md), [.gitignore](../.gitignore) |
| The project includes a `requirements.txt` file | [PLAN.md](../PLAN.md), [requirements.txt](../requirements.txt) |
| The project includes source code in the `src/` directory | [PLAN.md](../PLAN.md) |
| The project includes documentation in the `docs/` directory | [PLAN.md](../PLAN.md) |
| The project includes tests in the `tests/` directory | [PLAN.md](../PLAN.md) |
| The project includes infrastructure templates in the `infra/` directory | [PLAN.md](../PLAN.md) |
| The project includes the engine in the `engine/` directory | [engine/README.md](../engine/README.md) |
| The project includes engine tests in `tests/test_engine_app.py` (Bedrock and DynamoDB mocked, no network) | [tests/test_engine_app.py](../tests/test_engine_app.py) |
| The project is licensed under the MIT License | [LICENSE](../LICENSE) |
| The project does not include any files outside the supported types | [PLAN.md](../PLAN.md) |
| The project is intended to stay within a 40-file budget; no authoritative file manifest is shipped in this repository, so the exact current file count is NOT FOUND in this document and must be verified by listing the repository directly | NOT FOUND |

## NOT FOUND in the official rules

The following hackathon-relevant facts were looked for in the official rules and are recorded here as NOT FOUND rather than asserted:

| Item | Status |
|------|--------|
| A fixed team-size limit in the official rules | NOT FOUND in [https://aws-cds-partner.devpost.com/rules](https://aws-cds-partner.devpost.com/rules) |
| A clear pre-existing-code rule (whether prior code may be reused) in the official rules | NOT FOUND in [https://aws-cds-partner.devpost.com/rules](https://aws-cds-partner.devpost.com/rules) |
| An official definition of "agentic AI" in the rules or FAQ | NOT FOUND in [https://aws-cds-partner.devpost.com/rules](https://aws-cds-partner.devpost.com/rules) |
| An authoritative repository file manifest or file-count attestation | NOT FOUND in this repository |
| Whether `src/channels.py` enforces an allowlist restricting replies to test destinations | NOT FOUND in this document; the file content is not reproduced here and must be read directly to verify |
| WhatsApp prize eligibility conditions in official rules | NOT FOUND; the prize appears in the official rules but eligibility conditions are NOT FOUND |
