# Demo Guide -- "Rex" by 3Dogs Nexus

## Overview

This guide explains how judges can interact with the **Rex** demo for the AWS CDS Agentic AI Partner Hackathon. Rex is a thin-wrapper agentic concierge that responds to questions over email. The reasoning engine runs in the `engine/` Lambda (`awscds-engine`) and is called over HTTPS at the Function URL configured via `NEXUS_ENDPOINT` with the bearer token `NEXUS_TOKEN`.

All interactions are sandboxed. No messages are sent to real users without explicit owner approval.

---

## Channels

### Email

- **Send to**: [`judges@cds.3dogs.ai`](mailto:judges@cds.3dogs.ai)
- **Response from**: `judges@cds.3dogs.ai` (signed "Rex, 3Dogs Nexus"; every reply body ends with the exact signature block `-- Rex, 3Dogs Nexus`, as shown in the sample flows below)

---

## Consent Guard and the Judge Flow

Outbound replies are gated by the `ALLOWED_DESTINATIONS` allowlist (verified test identities only). This does not block judges:

- **Inbound always works**: a message from any sender -- including a judge address not yet on the allowlist -- is received, processed, and answered; the reply goes back to the inbound sender on the same channel.
- **Allowlist updated at submission**: when the judges' email address is added at submission, the owner adds `judges@cds.3dogs.ai` and the judges' email destination to `ALLOWED_DESTINATIONS` in the Lambda environment, so outbound replies to the judges pass the guard.

---

## Sample Flows

### 1. Basic Q&A

**Email**:
> What is 2+2?

**Rex replies**:
> The answer is 4.  
>   
> -- Rex, 3Dogs Nexus

---

### 2. Clarifying Question (Two Turns)

**Turn 1 -- Email**:
> Tell me about your products.

**Rex replies**:
> Which product are you asking about?  
>   
> -- Rex, 3Dogs Nexus

**Turn 2 -- Email** (the judge answers the clarifying question):
> The pet tracker.

**Rex replies** (the clarification is combined with the original question and re-asked to the engine):
> The pet tracker is our device that provides live location updates for your pets. What would you like to know about it?  
>   
> -- Rex, 3Dogs Nexus

This two-turn exchange demonstrates the agentic loop: Rex plans, asks for clarification when the request is ambiguous, and answers on the same channel once clarified.

---

### 3. Feedback Loop

**Email**:
> that's wrong

**Rex replies**:
> Thanks - noted.  
>   
> -- Rex, 3Dogs Nexus

On the immediately following question, the stored feedback is attached and forwarded to the engine so the retry improves (see the feedback lifecycle below).

---

## Behind the Scenes

- **Engine**: The engine Lambda in `engine/` (`awscds-engine`, Python 3.12) calls the Amazon Bedrock Converse API (`bedrock-runtime`) and is reached over HTTPS at the Function URL configured via the `NEXUS_ENDPOINT` environment variable, with the bearer token `NEXUS_TOKEN`. Both are read from environment variables at runtime; `NEXUS_TOKEN` is a secret supplied via environment variable and is **never committed to this repo** -- no token value exists anywhere in the repository, so judges should not look for one here.
- **Session Store**: DynamoDB
- **Channels**: `sesv2`
- **Feedback lifecycle**:
  1. When a message matches the feedback pattern (e.g. "that's wrong"), the raw text of that feedback message is stored in the sender's DynamoDB session record as a pending-feedback field. Nothing is sent to the engine on that turn beyond the acknowledgment reply above.
  2. On the sender's **immediately following question**, the orchestrator attaches that stored raw feedback text together with the new question (and the session's prior question context) in the single HTTPS request to the engine Lambda, so the engine can produce an improved retry.
  3. The retry answer returned by the engine is treated as the successful answer for that turn: once it is delivered back to the sender, the pending-feedback field is cleared from the session record.
  4. Feedback therefore persists for exactly one retry; it does not carry into later turns. If no follow-up question arrives, the stored feedback simply remains pending until one does.

---

## Sandbox Limitations

- Outbound replies only go to allowlisted test identities (`ALLOWED_DESTINATIONS`, owner-updated at submission time to include the judge destinations).
- Inbound messages from any sender are always accepted and processed; the allowlist gates outbound sends only.
- No SMS, WhatsApp or RCS in this version.

---

## Source Code

All source code is in the `src/` directory. Tests are in `tests/`. The architecture diagram is in `docs/architecture.svg`.

For questions, contact the owner: Alan Finney, 3Dogs Nexus.
