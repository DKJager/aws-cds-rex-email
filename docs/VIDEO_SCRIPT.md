# Demo Video Script -- "Rex" by 3Dogs Nexus (email edition)
**Hackathon**: AWS CDS Agentic AI Partner Hackathon  
**Length**: ~3 minutes (about 430 spoken words at 145 wpm)  
**Recording safety**: before filming, point `NEXUS_ENDPOINT` at the real engine with a revocable token, hide environment variables in every pane, and review every frame for secrets.

---

## [0:00-0:25] Hook

> _[Visual: a judge's inbox, one unanswered question.]_

"Most AI assistants answer once and forget. We built Rex to do what a good analyst does by email: read the question, remember the conversation, take a correction, and show its work. Rex runs on Amazon SES, two Lambdas and two DynamoDB tables. The reasoning engine is one of those Lambdas calling Amazon Bedrock Converse, so everything you see here is orchestration on AWS Communication Developer Services."

## [0:25-0:55] Architecture

> _[Visual: `docs/architecture.svg`.]_

"An email to judges at cds dot 3dogs dot ai hits an SES receipt rule that requires TLS, stores the message in S3, and invokes the orchestrator Lambda. That Lambda loads the conversation from one DynamoDB table, calls the engine over HTTPS, saves the turn, and replies through SES version 2 from the same mailbox. Failed invocations go to a dead-letter queue and a CloudWatch alarm. The engine itself is a separate Lambda that calls Amazon Bedrock Converse with the primary model gpt-oss-120b and a Nova Pro fallback, keeping the last six turns per session in its own DynamoDB table. Both stacks—the email orchestrator and the engine—are defined in two SAM templates."

## [0:55-1:50] Live round trip

> _[Visual: send the email; then the reply. Zoom on the "Agent steps" block.]_

"I send: 'What is 2 plus 2?' The reply arrives with the answer, the Rex signature, and the steps the agent took: received, new conversation, no pending correction, engine answered in some milliseconds, conversation saved, reply sent.

Now I reply: 'That's wrong.' Rex acknowledges and stores the correction against the conversation. I ask the question again. This time the steps say: returning conversation, stored correction forwarded with the question. The correction travelled with the next engine call, with no redeploy and no retraining."

## [1:50-2:20] The record behind it

> _[Visual: the DynamoDB item.]_

"Here is the same conversation in DynamoDB: one item per sender and channel, with the turn count, last question and answer, the structured steps of the latest turn, and a rolling expiry so idle sessions delete themselves."

## [2:20-2:50] Technical execution

> _[Visual: terminal, `awscds_deploy.py --dry-run`, then the test run; also show engine/app.py in the code walk.]_

"The stack is two SAM templates. Our deploy helper has a dry run that parses the real template and fails if the recipient is anything but the judges' address or if any IAM statement uses a wildcard; it then lists every AWS call it would make, each checked against the AWS service model. The engine code in engine/app.py calls the Amazon Bedrock Converse API with a 25-second timeout and temperature 0.2, answers as 'Rex, 3Dogs Nexus' in at most about 180 words, and returns exactly one of an answer or a clarification with steps. Offline tests cover the Lambda package layout, the table key schema, a full email round trip, a mail-loop guard, engine reasoning with mocked Bedrock and DynamoDB, and a tag-scoped destroy script, with negative controls that break each one on purpose."

## [2:50-3:00] Close

> _[Visual: inbox plus the repo URL.]_

"Rex: stateful, visible, and safe by construction. We are 3Dogs Nexus."
