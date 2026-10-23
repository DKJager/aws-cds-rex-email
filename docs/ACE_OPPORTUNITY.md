# ACE opportunity text -- paste into AWS Partner Central

Owner action. Create a NET-NEW opportunity for this entry; do not reuse or edit any existing opportunity. Fields below are what the entry can truthfully say today; anything in [brackets] is a value only the owner knows.

| Partner Central field | Text |
|---|---|
| **Campaign name / campaign code** (exact, copy character for character) | `AWS CDS Agentic AI Hackathon -Sept. 2026` |
| Opportunity title | AWS CDS Agentic AI Hackathon entry: email-based AI concierge on Amazon SES |
| Customer | [OWNER: the customer entity the hackathon rules instruct entrants to use] |
| Primary needs from AWS | Co-sell / hackathon participation [OWNER: select the option the rules page names] |
| Use case | Customer engagement / AI assistants |
| Delivery model | SaaS or PaaS |
| Industry | [OWNER] |
| Expected monthly AWS revenue | [OWNER: estimate; the demo stack is pay-per-request and idles near zero] |
| Target close date | Within the submission period, on or before 28 Oct 2026 13:00 PT |

**Customer business problem**

Teams that answer customer questions by email lose context between messages, cannot show how an answer was produced, and have no safe way to feed corrections back. They need an assistant that remembers the conversation, applies corrections on the next turn, and shows its steps, without handing customer mail to an unvetted pipeline.

**Solution offered**

"Rex" by 3Dogs Nexus: an agentic email concierge built on AWS Communication Developer Services. Inbound mail arrives through an Amazon SES receipt rule (TLS required) to Amazon S3 and an AWS Lambda orchestrator. The orchestrator keeps one conversation record per sender in Amazon DynamoDB (turn count, last question and answer, stored corrections, structured agent steps, rolling TTL), calls an engine Lambda that runs on Amazon Bedrock inside the same AWS account, and replies through Amazon SES v2 with the agent steps appended so every answer is auditable. Failures are captured by an SQS dead-letter queue and an Amazon CloudWatch alarm. The solution comprises two Lambda functions (email orchestrator and Bedrock engine), two DynamoDB tables, two SAM templates (infra/template.yaml and engine/template.yaml), plus Amazon SES, S3, SQS and CloudWatch, and Amazon Bedrock (Converse). The stack uses least-privilege IAM (no wildcard actions or resources), a tag-scoped destroy script and an offline test suite.

**AWS services used**

Amazon SES (inbound receipt rule, outbound v2), Amazon S3, AWS Lambda, Amazon DynamoDB, Amazon SQS, Amazon CloudWatch, AWS CloudFormation / SAM, AWS IAM, Amazon Bedrock.

**Next steps / ask**

Demonstrate the live round trip to the hackathon judges; offer the pattern as a managed service for partners who need auditable email agents.

Before submitting: confirm the campaign string against the official rules page (the `-Sept.` spacing is a known transcription risk), and record the new opportunity ID on the Devpost form.
