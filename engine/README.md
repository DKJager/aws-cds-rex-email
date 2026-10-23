# awscds-engine — Rex, 3Dogs Nexus

A tiny external "engine" for the AWS CDS hackathon entry: one Python 3.12 Lambda (boto3 only) behind a Lambda Function URL in us-east-1.
It validates a bearer token itself (URL auth is NONE), keeps the last 6 turns per `session_id` in DynamoDB (TTL 2 days), and answers via the Amazon Bedrock Converse API with a fallback model.

**Contract.** `POST <EngineUrl>` with header `Authorization: Bearer <token>` and body `{"question": str, "session_id": str, "feedback": optional str, "awaiting_clarification": bool}`.
Response `200` is exactly one of `{"answer": "...", "steps": [...]}` or `{"clarify": "...", "steps": [...]}`; `401 {"error":"unauthorized"}` on a bad token; `400` on bad JSON.

**Deploy with SAM:** `sam build && sam deploy --guided --region us-east-1 --parameter-overrides EngineToken=<secret>` (stack creates table `awscds-engine-sessions`, role `awscds-engine-role`, function `awscds-engine`).

**Deploy with plain CloudFormation:** `aws cloudformation package --template-file template.yaml --s3-bucket <bucket> --output-template-file out.yaml` then `aws cloudformation deploy --template-file out.yaml --stack-name awscds-engine --capabilities CAPABILITY_NAMED_IAM --parameter-overrides EngineToken=<secret> --region us-east-1`.

**Environment variables (set by the template):** `ENGINE_TOKEN` (from NoEcho parameter `EngineToken`), `MODEL_ID` (default `openai.gpt-oss-120b-1:0`), `MODEL_ID_FALLBACK` (default `amazon.nova-pro-v1:0`), `SESSIONS_TABLE` (DynamoDB table name).

Enable model access for both model ids in the Bedrock console (us-east-1) before first use. Output `EngineUrl` is the Function URL to call.

**Tests:** `pip install pytest boto3 && pytest -q` — Bedrock and DynamoDB are mocked; no network needed.
