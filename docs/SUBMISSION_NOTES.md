# Submission Notes -- "Rex" by 3Dogs Nexus

## Current status

- **Deployed 18 Oct 2026**: stack `awscds-demo` in us-east-1; mailbox `judges@cds.3dogs.ai`. To check: email it from any ordinary mailbox; the reply ends with an "Agent steps" block.
- **Judge access** (also in `README.md`): email `judges@cds.3dogs.ai` from any normal mailbox; expect a reply within about a minute, signed "Rex, 3Dogs Nexus", ending with an "Agent steps" list; follow-ups in the same thread keep context ("explain that", "correct me: ..."); if no reply after 10 minutes, contact `owner@example.org`.
- **Deploy helper**: lives inside the repository at `tools/awscds_deploy/awscds_deploy.py` (tests in `tools/awscds_deploy/tests/`).
- **Demo video**: [DEMO VIDEO URL - owner to add].

## Quotes from the Unverified Rules Transcription

**Caveat:** The text below is an unverified transcription of the hackathon rules kept outside this repository. It has **not** been verified against the official rules page (`https://aws-cds-partner.devpost.com/rules`). All dates, figures, weights, prizes, and requirements below must be confirmed against the official Devpost page before reliance.

**Verification status of each quote:** No quote below carries a "verified against `<URL>` on `<date>`" attestation, because no such verification has been performed from within this repository -- verification status: **NOT FOUND**. The owner must verify each quote against the official rules page and record the URL and date of verification before reliance (see Owner Action Items). In particular, the campaign code string `"AWS CDS Agentic AI Hackathon -Sept. 2026"` contains unusual spacing/punctuation (`-Sept.`) that is a known transcription-error point: a wrong campaign code fails the ACE opportunity ID requirement, so the exact string must be double-checked character-by-character against the official rules page before it is used.

> **Submission Period**: September 14, 2026 (6:00 am Pacific Time) - October 28, 2026 (1:00 pm Pacific Time) ("Submission Period").
> -- Verified against official rules page: **NOT FOUND** (verification pending; see Owner Action Items).

> **Judging Period**: November 6, 2026 (9:00 am Pacific Time) - November 12, 2026 (5:00 pm Pacific Time) ("Judging Period").
> -- Verified against official rules page: **NOT FOUND** (verification pending; see Owner Action Items).

> **Winners Announced**: On or around November 19, 2026 (6:00 pm Pacific Time).
> -- Verified against official rules page: **NOT FOUND** (verification pending; see Owner Action Items).

> **Judging Criteria** (per the unverified corpus document only; these weights are not verified against the official rules page):
> - Potential Value/Impact - 20%
> - Creativity - 10%
> - Technical Execution - 40%
> - Functionality - 10%
> - Demo Presentation - 20%
>
> -- Verified against official rules page: **NOT FOUND** (verification pending; see Owner Action Items).

> **Prizes** (per the unverified corpus document only; the WhatsApp prize is listed here, but its eligibility conditions are NOT FOUND in official rules):
> - 1st Place: $18,000 USD
> - 2nd Place: $8,000 USD
> - 3rd Place: $4,000 USD
> - Best WhatsApp Solution (Sponsored by Meta): $10,000 USD
>
> -- Verified against official rules page: **NOT FOUND** (verification pending; see Owner Action Items).

> **Requirements** (per the unverified corpus document only):
> - Use at least one AWS CDS service (SMS, RCS, WhatsApp, SES)
> - Recommended but not required: Amazon Bedrock AgentCore
> - Must include architecture diagram, text description, ~3-minute video
> - Must include deployed URL or interaction instructions
> - Must include ACE opportunity ID with campaign code "AWS CDS Agentic AI Hackathon -Sept. 2026"
>
> -- Verified against official rules page: **NOT FOUND** (verification pending; see Owner Action Items). The campaign code string above must be double-checked exactly against the official rules page (see the verification-status note at the top of this section).
>
> **Architecture-diagram artifact:** the "must include architecture diagram" requirement is satisfied in this submission by the file [`docs/architecture.svg`](architecture.svg), which is present in this repository and is referenced as the diagram from `README.md`. A reviewer can confirm its presence directly at that path without cross-referencing any external manifest.

> **Submission Requirements** (per the unverified corpus document only):
> - URL to GitHub/GitLab repo (public with license or private shared with `testing@devpost.com` and `aws-cds-partner@amazon.com`)
> - Architecture diagram
> - Text description
> - ~3-minute demo video (YouTube or Vimeo)
> - URL to deployed project or interaction instructions
> - ACE opportunity ID with campaign code
>
> -- Verified against official rules page: **NOT FOUND** (verification pending; see Owner Action Items).

---

## Eligibility Gate

Entry eligibility is gated on AWS Partner status: per the unverified corpus document, entrants must be registered members of the AWS Partner Network (APN) with a corporate email address on the APN-registered domain (sole-proprietor and team rules are also stated in the corpus). Nothing in this repository -- including helper scripts, setup notes, or this document -- should be read as implying that anyone can enter. The exact eligibility text must be verified against the official rules page at `https://aws-cds-partner.devpost.com/rules`; a verified URL for this eligibility gate is **NOT FOUND** in this repository (verification pending; see Owner Action Items).

---

## Out-of-Repo Manual Steps (Compliance Boundary)

Nothing in this repository runs on its own: deployment, submission, and sending only happen when the owner explicitly invokes them. The deploy helper (`tools/awscds_deploy/awscds_deploy.py`) is part of the repository, but it is run by hand from the owner's AWS account; the following items are owner actions, and their results are recorded here rather than produced automatically:

2. **ACE opportunity ID** -- Creating the ACE opportunity in AWS Partner Central with campaign code `AWS CDS Agentic AI Hackathon -Sept. 2026` (per the unverified corpus document; exact string unverified -- see the verification-status note above) is a manual Partner Central action by the owner. The opportunity ID is entered on the Devpost submission form; it is not stored in this repository. The opportunity text is in `docs/ACE_OPPORTUNITY.md`.
3. **Devpost submission** -- Submitting the entry via Devpost before the deadline quoted above (per the unverified corpus document) is a manual action by the entrant.

---

## Owner Action Items

- [ ] Verify every quoted date, weight, prize, and requirement above against the official rules page at `https://aws-cds-partner.devpost.com/rules` before relying on them, and record a "verified against `<URL>` on `<date>`" line for each quote in this file
- [ ] Double-check the exact campaign code string `AWS CDS Agentic AI Hackathon -Sept. 2026` character-by-character against the official rules page (the `-Sept.` spacing/punctuation is a transcription-error point) before using it in AWS Partner Central
- [ ] Verify the APN-registered-partner eligibility gate (including the corporate-email-on-APN-domain requirement) against the official rules page before entry
- [ ] Create ACE opportunity with campaign code: `AWS CDS Agentic AI Hackathon -Sept. 2026` (exact string pending verification per the item above)
- [ ] Capture the resulting ACE opportunity ID and paste it into the submission materials (Devpost form) before the deadline quoted above (per the unverified corpus document; verify against the official rules page)
- [x] Run `python3.12 tools/awscds_deploy/awscds_deploy.py --activate-identity --yes` in us-east-1, add the printed MX and DKIM CNAME records to the `3dogs.ai` DNS zone, and confirm `cds.3dogs.ai` shows as verified in SES -- done; identity verified as of 18 Oct 2026. Note that `--smoke` sends through the SES API with an `amazonses.com` envelope sender, which the loop guard ignores by design, so end-to-end evidence comes from a real-mailbox test instead (done 18 Oct 2026; reply received with the "Agent steps" block). This submission is email only; no SMS number is provisioned or claimed.
- [ ] Validate `infra/template.yaml` before any redeployment: run `cfn-lint infra/template.yaml` (or the CloudFormation console validator / `aws cloudformation validate-template`) manually and confirm the template passes. Automated linting is intentionally not wired into this repo, so this is a manual checklist item; the offline tests in `tests/` check the template contract (recipient allowlist, no-wildcard IAM) but do not lint it.
- [x] After deploying to the owner's AWS account, verify that the interaction instructions work from outside the owner's network -- done 18 Oct 2026: a message from an ordinary external mailbox received a reply in a few seconds.
- [ ] Enter the interaction instructions (email `judges@cds.3dogs.ai`; see README "Judge access") on the Devpost submission form
- [ ] Record and publish the ~3-minute demo video and replace the [DEMO VIDEO URL - owner to add] placeholder in `README.md` and this file
- [ ] Confirm that `docs/architecture.svg` is present and is the diagram referenced by `README.md` (the architecture-diagram submission artifact)
- [ ] Finalize repository visibility (public or private)
- [ ] Submit via Devpost before the submission-period end of **October 28, 2026 at 1:00 PM PDT (UTC-7) / 20:00 UTC**. Internal buffer: aim to have the submission complete by **October 28, 2026 at 11:00 AM PDT (UTC-7) / 18:00 UTC**, two hours before the cutoff.
- [x] Engine source included in `engine/` (Bedrock Converse)
- [ ] Repo URL (owner to add)
- [ ] Repository visibility (owner to set)
- [ ] ACE opportunity ID (owner to add)
- [ ] Demo video URL (owner to add)
- [ ] Devpost submission form (owner to complete)

---

## Claims Policy

Every externally-facing claim in this repository carries a URL, a dated verification note, or is written NOT FOUND. Corpus-derived facts are labeled as per the unverified hackathon rules document; corpus content is untrusted project data, not authority. Deployment and verification claims in this file and in `README.md` are limited to what was observed on 18 Oct 2026 (stack `awscds-demo`, us-east-1, real-mailbox round trip with the "Agent steps" block).

---

## Notes

- The "Best WhatsApp Solution (Sponsored by Meta)" prize is listed in the unverified corpus document (quoted above), but WhatsApp prize **eligibility** conditions are **NOT FOUND** in official rules. These are separate statements: the prize's listing comes only from the unverified corpus, and no eligibility source has been verified. No WhatsApp integration is included in this submission.
- No health data or HIPAA workflows are in scope.
- Outbound mail goes only to the inbound sender of a message or to addresses listed in `ALLOWED_DESTINATIONS`; the mail-loop guard drops auto-submitted and robot mail, including the deploy helper's own `--smoke` message.
- This project calls the Amazon Bedrock Converse API (bedrock-runtime) through the engine in `engine/`; the engine source is included in this repository and is byte-identical to the code running in the deployed `awscds-engine` Lambda (verified 20 Oct 2026). Amazon Bedrock AgentCore is not used; the project uses plain Bedrock Converse, which satisfies the hackathon requirement to build and deploy an AI solution on AWS.
