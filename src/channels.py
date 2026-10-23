"""Inbound payload parsing and outbound sending for SMS and email.

Qualifying CDS clients (imported AND called at runtime):
  - boto3 "pinpoint-sms-voice-v2" SendTextMessage (SMS)
  - boto3 "sesv2" SendEmail (email)
  - boto3 "s3" GetObject (optional: retrieval of the raw inbound MIME
    message stored by an SES S3 receipt action, when SES_INBOUND_BUCKET
    is configured)

Outbound messages are signed "Rex, 3Dogs Nexus". The signature is
DISPLAY TEXT ONLY, appended to the message body; it is not a sending
identity and no sender@example.org (or any apex 3dogs.ai) mailbox is used
anywhere. Email replies are sent from the judges' mailbox (SES_FROM,
judges@cds.3dogs.ai). An allowlist guard (ALLOWED_DESTINATIONS)
ensures replies go only to verified test identities.

Email replies go ONLY to the SES-authenticated inbound sender (the
envelope sender SES received, `mail.source`). Reply-To and From headers
never redirect a reply anywhere else; see _extract_reply_target() and
send_reply() for the exact rules, which exist so the stack can never be
used as a relay on our SES identity.

Inbound email bodies have their quoted thread history removed (see
_strip_quoted_history()) before they become InboundMessage.text, so a
judge's follow-up is read as the new text only: the quoted previous
turn cannot re-trigger the feedback check, trip the loop guard with our
own quoted signature, or reach the engine as part of the question.

Naming note: SMS_ORIGINATION_ID (env var) configures OUR outbound
origination identity (phone number / pool ARN) used by send_sms().
The `originationNumber` / `origination` fields read by
parse_sns_sms_event() are the INBOUND sender's number as reported by
the End User Messaging event payload. The similar words describe
opposite directions; do not conflate them when walking through the
configuration.
"""

import base64
import email
import email.policy
import json
import logging
import os
import re
import threading

import boto3

logger = logging.getLogger(__name__)

REX_SIGNATURE = "Rex, 3Dogs Nexus"

_sms_client = None
_ses_client = None
_s3_client = None
# Guards lazy client construction so concurrent first-use invocations
# (e.g. in a local multi-threaded simulation) cannot race to build two
# clients or observe a half-initialized global.
_client_lock = threading.Lock()

_DISPLAY_NAME_RE = re.compile(r"<([^<>\s]+@[^<>\s]+)>")

# Matches a full display-name token such as `"Doe, John" <j@x>` or
# `Doe, John <j@x>` and captures only the mailbox inside the brackets.
# Used to collapse display names BEFORE splitting the allowlist on
# commas, so commas inside quoted/display-name parts cannot corrupt
# entries.
_DISPLAY_NAME_TOKEN_RE = re.compile(
    r'(?:"[^"]*"|[^,<>"]*)<([^<>\s]+@[^<>\s]+)>'
)

# Minimal shape checks used to keep channel-specific destinations apart:
# an E.164 phone number for SMS and a mailbox for email. These are
# deliberately permissive (no full RFC 5322 parsing); they exist only so
# a misconfigured ALLOWED_DESTINATIONS mix (e.g. an email address sent
# down the SMS path) is refused here with a clear error instead of
# failing opaquely inside the AWS service call.
_E164_RE = re.compile(r"^\+[1-9]\d{1,14}$")
_MAILBOX_RE = re.compile(r"^[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+$")

# Control characters (except tab/newline/carriage return) are stripped
# from outbound text and subjects so a demo payload containing stray
# control bytes cannot corrupt an SES header or the SMS body.
_CONTROL_CHARS_RE = re.compile(r"[^\S\n\t\r\n]|[^\x20-\U0010FFFF]")

# Hard caps on outbound sizes. SES header values must stay well under
# RFC 5322 limits and SMS bodies are billed per segment; a guard here
# fails fast with a clear error instead of a confusing service error.
_MAX_SUBJECT_CHARS = 200
# SES v2 SendEmail Text data accepts far more than 4000 chars in practice
# (SES has a much higher limit).  The SMS cap is kept at 4000; email
# replies are capped at a high value so the engine's full answer + steps
# block always fits.  If a body still exceeds this cap it is truncated
# with a note rather than raising.
_MAX_BODY_CHARS_SMS = 4000
_MAX_BODY_CHARS_EMAIL = 16000

# Cap on the raw MIME message size fetched from S3, so a malformed or
# hostile inbound object cannot exhaust Lambda memory while parsing.
_MAX_MIME_BYTES = 5 * 1024 * 1024

# SES receipt verdict status meaning the check succeeded.
_VERDICT_PASS = "PASS"

# Attribute set by parse_ses_email_event() on the InboundMessage: the
# reply target derived by the guard rules from the SES event (including
# the receipt verdicts). send_reply() uses it only when the sender is
# empty or our own mailbox, where the sender alone cannot authorise a
# target.
_GUARDED_TARGET_ATTR = "guarded_reply_target"

# Quote-boundary markers used by _strip_quoted_history() to separate a
# judge's new text from the quoted thread history mail clients append.
#   - "-----Original Message-----" (Outlook / many clients)
#   - a rule of five or more underscores (Outlook web, ahead of the
#     quoted header block)
#   - the header lines of an Outlook-style quoted header block
_ORIGINAL_MESSAGE_RE = re.compile(
    r"^-+\s*Original Message\s*-+$", re.IGNORECASE
)
_UNDERSCORE_RULE_RE = re.compile(r"^_{5,}$")
_OUTLOOK_HEADER_LINE_RE = re.compile(r"^(?:From|Sent|Date|To|Cc|Bcc|Subject):")
# How many lines after a "From:" line (or an underscore rule) the
# confirming "Sent:" / "Date:" (or "From:") line may appear.
_QUOTE_HEADER_LOOKAHEAD = 3

_TRUNCATED_NOTE = "[truncated]"


def _sanitize_text(value, field, max_chars):
    """Strip control characters and enforce a length cap on outbound text.

    Returns the cleaned string. Raises ValueError when the result is
    empty or exceeds `max_chars`, so a malformed demo payload fails fast
    with a clear message rather than producing a confusing AWS service
    error. Newlines, tabs and carriage returns are preserved; all other
    control characters are removed.
    """
    if value is None:
        return None
    cleaned = "".join(
        ch
        for ch in str(value)
        if ch in ("\n", "\r", "\t") or (ch.isprintable() and ch != "\x7f")
    )
    if len(cleaned) > max_chars:
        raise ValueError(
            "%s exceeds %d characters after sanitization (%d)"
            % (field, max_chars, len(cleaned))
        )
    return cleaned


def _sanitize_and_fit(value, field, max_chars):
    """Strip control characters and fit within a length cap.

    Returns the cleaned string, truncated to `max_chars` with a
    '[truncated]' note appended when the cleaned text exceeds the cap.
    Raises ValueError only when the cleaned result is empty; an
    over-length body is merely truncated so a long engine answer never
    blocks the reply.
    """
    if value is None:
        return None
    cleaned = "".join(
        ch
        for ch in str(value)
        if ch in ("\n", "\r", "\t") or (ch.isprintable() and ch != "\x7f")
    )
    if len(cleaned) <= max_chars:
        return cleaned
    # Reserve room for the truncation note so the final body does not
    # exceed the cap.
    keep = max_chars - (len(_TRUNCATED_NOTE) + 1)
    if keep < 0:
        keep = 0
    return cleaned[:keep] + "\n" + _TRUNCATED_NOTE


def _get_region(service):
    """Resolve the AWS region for a service client.

    Reads the standard AWS_REGION / AWS_DEFAULT_REGION variables first,
    then a service-specific override (SMS_REGION / SES_REGION /
    S3_REGION). Raises a clear error when no region is configured so
    client construction does not fail later with an opaque boto3
    message.
    """
    region = (
        os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or os.environ.get("%s_REGION" % service.upper())
    )
    if not region or not region.strip():
        raise RuntimeError(
            "no AWS region configured for %s: set AWS_REGION "
            "(or %s_REGION)" % (service, service.upper())
        )
    return region.strip()


def _get_sms_client():
    return boto3.client(
        "pinpoint-sms-voice-v2",
        region_name=_get_region("sms"),
    )


def _get_ses_client():
    return boto3.client("sesv2", region_name=_get_region("ses"))


def _get_s3_client():
    return boto3.client("s3", region_name=_get_region("s3"))


def validate_config():
    """Lightweight startup guard for required outbound configuration.

    Checks that the region is resolvable and that the allowlist and the
    per-channel origination settings are present. Logs a warning for each
    gap and returns the list of problem descriptions, so the Lambda
    handler (or a cold-start init hook) can surface misconfiguration
    before the first inbound event rather than deep inside send_sms() /
    send_email(). Never raises; an empty list means configuration is OK.

    Note: an empty ALLOWED_DESTINATIONS also fails fast at first send
    with a RuntimeError naming the variable (see _require_allowlist),
    so a missing env var produces an explicit configuration error rather
    than a confusing series of PermissionError refusals mid-demo.
    """
    problems = []
    try:
        _get_region("sms")
    except RuntimeError as exc:
        problems.append(str(exc))
    if not allowed_destinations():
        problems.append(
            "ALLOWED_DESTINATIONS is not set or empty: all outbound "
            "replies will be refused; set it to the comma-separated "
            "verified test destinations (E.164 numbers and/or mailboxes)"
        )
    if not os.environ.get("SMS_ORIGINATION_ID", "").strip():
        problems.append(
            "SMS_ORIGINATION_ID is not set: SMS replies will fail"
        )
    if not os.environ.get("SES_FROM", "").strip():
        problems.append("SES_FROM is not set: email replies will fail")
    for problem in problems:
        logger.warning("configuration problem: %s", problem)
    return problems


def _require_allowlist():
    """Fail fast when ALLOWED_DESTINATIONS is unset or empty.

    Without this guard a missing env var surfaces as a PermissionError
    per destination, which reads like an allowlist mismatch rather than
    the real cause (the variable was never configured). Raising a
    RuntimeError that names the variable makes the demo outage
    immediately diagnosable. Called at first use by every outbound
    send path; validate_config() surfaces the same gap at startup.
    """
    if not allowed_destinations():
        raise RuntimeError(
            "ALLOWED_DESTINATIONS environment variable is not set or "
            "empty: set it to the comma-separated verified test "
            "destinations before sending replies"
        )


def _normalize_destination(destination):
    """Normalize a destination for allowlist comparison.

    Email addresses may arrive in display-name form ("Name <a@b>") and are
    case-insensitive, so the mailbox part is extracted and lowercased.

    Phone numbers are canonicalized to E.164 ("+" followed by digits
    only) on BOTH the inbound side (SNS `originationNumber`, which can
    arrive with or without the leading "+", or with formatting such as
    spaces, dashes, dots or parentheses) and the allowlist side
    (ALLOWED_DESTINATIONS entries). Because both sides pass through this
    same normalization, "+15551234567" and "15551234567" and
    "+1 (555) 123-4567" all compare equal, so a formatting variant from
    the carrier cannot cause a false allowlist refusal. No country-code
    guessing is performed beyond preserving the digits as supplied.
    """
    if destination is None:
        return ""
    value = str(destination).strip()
    match = _DISPLAY_NAME_RE.search(value)
    if match:
        value = match.group(1)
    if "@" in value:
        return value.strip().lower()
    # Phone number: reduce to digits and canonicalize to "+<digits>".
    digits = re.sub(r"\D", "", value)
    if not digits:
        return ""
    return "+%s" % digits


def _normalize_mailbox(value):
    """Normalize a mailbox header value for the reply-target guard.

    Display names, angle brackets, case and surrounding whitespace are
    removed ("Jürgen Ä <J@Example.TEST>" becomes "j@example.test").
    Returns an empty string for None, empty, non-string or malformed
    values (anything that is not a single mailbox address), so a bad
    header is ignored rather than raising or being mistaken for an
    address.
    """
    if value is None:
        return ""
    try:
        normalized = _normalize_destination(value)
    except Exception as exc:  # defensive: never raise on a bad header
        logger.warning(
            "ignoring unparseable mailbox value %.120r: %s", value, exc
        )
        return ""
    if "@" not in normalized or not _MAILBOX_RE.match(normalized):
        return ""
    return normalized


def _own_address():
    """Our own sending mailbox (SES_FROM), normalized; "" when unset."""
    return _normalize_mailbox(os.environ.get("SES_FROM", ""))


def _first_mailbox(values):
    """First usable mailbox in a commonHeaders list (or single string)."""
    if values is None:
        return ""
    if isinstance(values, str):
        values = [values]
    try:
        items = list(values)
    except TypeError:
        return ""
    for value in items:
        normalized = _normalize_mailbox(value)
        if normalized:
            return normalized
    return ""


def _verdict_status(receipt, key):
    """Return the upper-cased SES receipt verdict status, or None if absent."""
    if not isinstance(receipt, dict):
        return None
    verdict = receipt.get(key)
    if not isinstance(verdict, dict):
        return None
    status = verdict.get("status")
    if status is None:
        return None
    status = str(status).strip().upper()
    return status or None


def allowed_destinations():
    """Return the set of verified test destinations permitted for replies.

    Display-name tokens (e.g. `"Doe, John" <john@example.org>`) are
    collapsed to their bare mailbox before the value is split on commas,
    so a comma inside a quoted or unquoted display name cannot split a
    single allowlist entry into fragments. Non-string members (which can
    only appear if a caller injects them into the environment from a
    structured source) are coerced via str() rather than crashing with
    an AttributeError that would mask the real configuration problem.
    """
    raw = os.environ.get("ALLOWED_DESTINATIONS", "")
    collapsed = _DISPLAY_NAME_TOKEN_RE.sub(
        lambda match: match.group(1), raw
    )
    entries = set()
    for item in collapsed.split(","):
        if not isinstance(item, str):
            logger.warning(
                "ignoring non-string ALLOWED_DESTINATIONS entry: %r", item
            )
            continue
        if item.strip():
            entries.add(_normalize_destination(item))
    return entries


def is_allowed(destination):
    return _normalize_destination(destination) in allowed_destinations()


def _validate_destination_for_channel(channel, destination):
    """Refuse a destination whose shape does not fit the channel.

    The allowlist stores phone numbers and email mailboxes together, so
    a misconfigured mix (e.g. a mailbox passed to the SMS path, or a
    phone number passed to the email path) would pass the allowlist
    check and then fail deep inside the service call. This shape check
    fails fast with a clear ValueError so the misconfiguration surfaces
    where it was introduced.
    """
    normalized = _normalize_destination(destination)
    if channel == "sms":
        if not _E164_RE.match(normalized):
            raise ValueError(
                "SMS destination is not an E.164 phone number: %r"
                % (normalized,)
            )
    elif channel == "email":
        if not _MAILBOX_RE.match(normalized):
            raise ValueError(
                "email destination is not a mailbox address: %r"
                % (normalized,)
            )


class InboundMessage:
    """Normalized inbound message from any supported channel."""

    def __init__(self, channel, sender, text, reply_target=None, subject=None):
        self.channel = channel  # "sms" or "email"
        self.sender = sender
        self.text = text
        self.subject = subject
        # For email: the address to reply to. For SMS: the phone number.
        self.reply_target = reply_target if reply_target is not None else sender

    def __repr__(self):
        """Debug-friendly rendering for tracing message flow in logs.

        The body text is truncated so a long message cannot flood the
        log line; channel, sender and reply target are always shown in
        full since they are the fields needed to trace a demo exchange.
        """
        return (
            "InboundMessage(channel=%r, sender=%r, reply_target=%r, "
            "subject=%r, text=%.80r)"
            % (
                self.channel,
                self.sender,
                self.reply_target,
                self.subject,
                self.text,
            )
        )

    __str__ = __repr__


def parse_sns_sms_event(event):
    """Parse an SNS-record event carrying an inbound SMS payload.

    Returns an InboundMessage or None if no SMS record is found.
    Records that are not usable (non-SNS sources, non-JSON payloads,
    missing fields) are logged so inbound loss is diagnosable rather
    than silently skipped.

    Note the naming distinction from the module docstring: the
    `originationNumber` field here is the INBOUND sender's phone number
    as reported by the event, not the SMS_ORIGINATION_ID env var that
    configures our OUTBOUND origination identity in send_sms().
    """
    for record in event.get("Records", []):
        if record.get("EventSource") != "aws:sns":
            continue
        raw = record.get("Sns", {}).get("Message", "")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            # JSONDecodeError is a ValueError subclass; catching the
            # precise type documents intent while still logging every
            # malformed payload as a warning instead of swallowing it.
            logger.warning(
                "skipping SNS record with non-JSON Message: %.120r", raw
            )
            continue
        origination = payload.get("originationNumber") or payload.get("origination")
        body = payload.get("messageBody") or payload.get("message") or ""
        if origination and body:
            return InboundMessage("sms", origination, body)
        logger.warning(
            "skipping SNS record missing originationNumber/messageBody: "
            "%.120r",
            payload,
        )
    return None


def _extract_reply_target(mail, receipt=None):
    """Determine the address an email reply may go to (reply-target guard).

    Rules (committee thread 1792446407.868059), with SOURCE = normalized
    `mail.source` (the SMTP envelope sender SES received), OWN = our own
    mailbox (SES_FROM), FROM = normalized commonHeaders From, and SPF /
    DKIM = the receipt verdict statuses when the event carries them:

      1. SPF and DKIM both present and neither "PASS": no reply target
         (the message is unauthenticated).
      2. Else, SOURCE non-empty and not OWN: the reply target is SOURCE.
         Reply-To and From are ignored unless equal to SOURCE, so they
         can never redirect the reply to a third party.
      3. Else (SOURCE empty or OWN, the trigger set-up that reports our
         own address): the reply target is FROM only when DKIM is
         "PASS"; otherwise there is no reply target.
      4. The reply target is never OWN (no loop to our own mailbox).

    Display names, angle brackets, case and whitespace never change the
    decision; empty or malformed header values are ignored. Every
    refusal is logged with the sender and the reason, and nothing raises
    on a bad header.

    Returns an empty string when no reply target is permitted; callers
    and send_reply() treat that as "no reply target" and refuse to send.
    """
    if not isinstance(mail, dict):
        logger.warning("reply target refused: SES mail object is not a dict")
        return ""
    if receipt is None:
        receipt = mail.get("receipt")
    raw_source = mail.get("source")
    source = _normalize_mailbox(raw_source)
    own = _own_address()
    headers = mail.get("commonHeaders") or {}
    if not isinstance(headers, dict):
        headers = {}
    spf = _verdict_status(receipt, "spfVerdict")
    dkim = _verdict_status(receipt, "dkimVerdict")

    # Rule 1: unauthenticated mail gets no reply at all.
    if (
        spf is not None
        and dkim is not None
        and spf != _VERDICT_PASS
        and dkim != _VERDICT_PASS
    ):
        logger.warning(
            "reply target refused for sender %r: SPF=%s and DKIM=%s "
            "(message is unauthenticated)",
            raw_source,
            spf,
            dkim,
        )
        return ""

    if raw_source is not None and str(raw_source).strip() and not source:
        logger.warning(
            "reply target refused for sender %r: mail.source is not a "
            "mailbox address",
            raw_source,
        )
        return ""

    # Rule 2: the SES-authenticated envelope sender is the only target.
    if source and source != own:
        for key in ("replyTo", "from"):
            header_value = _first_mailbox(headers.get(key))
            if header_value and header_value != source:
                logger.info(
                    "ignoring %s header %r on mail from %r: replies go "
                    "only to the inbound sender",
                    key,
                    header_value,
                    source,
                )
        return source

    # Rule 3: SOURCE is empty or our own mailbox; only a DKIM-verified
    # From header may be used, and only when it is not OWN (rule 4).
    if dkim != _VERDICT_PASS:
        logger.warning(
            "reply target refused for sender %r: mail.source is empty or "
            "our own mailbox and DKIM=%s (From header is not verified)",
            raw_source,
            dkim,
        )
        return ""
    from_address = _first_mailbox(headers.get("from"))
    if not from_address:
        logger.warning(
            "reply target refused for sender %r: mail.source is empty or "
            "our own mailbox and the From header carries no usable "
            "mailbox",
            raw_source,
        )
        return ""
    if from_address == own:
        logger.warning(
            "reply target refused for sender %r: From header is our own "
            "mailbox %r (no reply loop)",
            raw_source,
            own,
        )
        return ""
    return from_address


def _outlook_header_end(lines, start):
    """End index (exclusive) of an Outlook quoted header block at `start`.

    `lines[start]` is a "From:" line. The block is confirmed only when a
    "Sent:" or "Date:" line follows within _QUOTE_HEADER_LOOKAHEAD
    lines; the block then extends over the consecutive header lines
    (To:, Cc:, Subject:, ...) that Outlook prints beneath it. Returns
    None when the From: line is not followed by such a confirmation,
    so an ordinary sentence beginning with "From:" is left alone.
    """
    n = len(lines)
    end = None
    for j in range(start + 1, min(start + 1 + _QUOTE_HEADER_LOOKAHEAD, n)):
        candidate = lines[j].strip()
        if candidate.startswith("Sent:") or candidate.startswith("Date:"):
            end = j + 1
            break
    if end is None:
        return None
    while end < n and _OUTLOOK_HEADER_LINE_RE.match(lines[end].strip()):
        end += 1
    return end


def _find_quote_boundary(lines):
    """Locate the first quoted-history boundary in a list of body lines.

    Returns a (start, end) span of line indexes covering the boundary
    lines themselves (end exclusive), or None when no boundary exists.
    Recognised boundaries:

      (a) a Gmail/Apple attribution: a line starting with "On " that
          ends with "wrote:", or a line starting with "On " whose next
          line ends with "wrote:" (Gmail wraps the attribution);
      (b) an Outlook header block: a "From:" line followed within three
          lines by a "Sent:" or "Date:" line;
      (c) a rule of five or more underscores followed within three
          lines by such a "From:" line (Outlook web);
      (d) a "-----Original Message-----" line.
    """
    n = len(lines)
    for index in range(n):
        line = lines[index].strip()
        if line.startswith("On "):
            if line.endswith("wrote:"):
                return index, index + 1
            if index + 1 < n and lines[index + 1].strip().endswith("wrote:"):
                return index, index + 2
        if _ORIGINAL_MESSAGE_RE.match(line):
            return index, index + 1
        if line.startswith("From:"):
            end = _outlook_header_end(lines, index)
            if end is not None:
                return index, end
        if _UNDERSCORE_RULE_RE.match(line):
            for j in range(index + 1, min(index + 1 + _QUOTE_HEADER_LOOKAHEAD, n)):
                if lines[j].strip().startswith("From:"):
                    end = _outlook_header_end(lines, j)
                    if end is not None:
                        return index, end
                    break
    return None


def _strip_quoted_history(text):
    """Remove the quoted thread history from an inbound email body.

    Mail clients append the previous turn beneath a reply, either as
    ">"-prefixed lines under an attribution ("On ... wrote:") or, in
    Outlook, unprefixed beneath a header block ("From: / Sent: / To: /
    Subject:"). Left in place, that history re-triggers the feedback
    check on an old "that's wrong", trips the loop guard on our own
    quoted signature and reaches the engine as part of the question.

    Behaviour:

      1. Find the first quote boundary (see _find_quote_boundary()).
      2. If there is non-empty text before the boundary, keep only that
         text (top-posted reply).
      3. Otherwise drop the boundary lines and every line starting with
         ">", but only when at least one non-empty line that does not
         start with ">" remains; a body made only of quoted lines (for
         example "> that's wrong") is kept as it is.
      4. Trailing blank lines and whitespace are trimmed.

    Never raises on odd input: a None or non-string value, or any
    unexpected failure, degrades to the input (as a string, trailing
    whitespace trimmed) and an empty result stays empty.
    """
    if text is None:
        return ""
    if not isinstance(text, str):
        try:
            text = str(text)
        except Exception:  # defensive: never raise on odd input
            return ""
    try:
        lines = text.splitlines()
        span = _find_quote_boundary(lines)
        if span is not None:
            start, end = span
            before = lines[:start]
            if any(line.strip() for line in before):
                return "\n".join(before).strip()
        remaining = []
        for index, line in enumerate(lines):
            if span is not None and span[0] <= index < span[1]:
                continue
            if line.lstrip().startswith(">"):
                continue
            remaining.append(line)
        if span is None and len(remaining) == len(lines):
            # Nothing quoted at all: the plain text is unchanged apart
            # from trailing whitespace.
            return text.rstrip()
        if any(line.strip() for line in remaining):
            return "\n".join(remaining).strip()
        # Only quoted lines (and boundary lines) exist: keep the body as
        # it is so a quote-only correction is still read as feedback.
        return text.rstrip()
    except Exception as exc:  # defensive: never raise on odd input
        logger.warning("failed to strip quoted history from body: %s", exc)
        return text.rstrip()


def _extract_text_from_mime(raw_bytes):
    """Extract the plain-text body from a raw MIME message.

    Prefers the first text/plain part; falls back to a stripped
    text/html part when no plain-text part exists so an HTML-only judge
    email still yields usable text. Returns an empty string when no
    textual part is found. Parsing uses the stdlib email package with
    the default policy; decoding errors are logged and result in an
    empty string rather than an exception, so a malformed MIME part
    degrades to the subject-line fallback instead of dropping the
    message.
    """
    try:
        message = email.message_from_bytes(
            raw_bytes, policy=email.policy.default
        )
    except Exception as exc:  # stdlib raises a variety of parse errors
        logger.warning("failed to parse inbound MIME message: %s", exc)
        return ""
    plain_parts = []
    html_parts = []
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_maintype() == "multipart":
                continue
            content_type = part.get_content_type()
            if content_type == "text/plain":
                plain_parts.append(part)
            elif content_type == "text/html":
                html_parts.append(part)
    else:
        content_type = message.get_content_type()
        if content_type == "text/plain":
            plain_parts.append(message)
        elif content_type == "text/html":
            html_parts.append(message)
    for part in plain_parts:
        try:
            text = part.get_content()
        except Exception as exc:
            logger.warning("failed to decode text/plain part: %s", exc)
            continue
        if text and text.strip():
            return text.strip()
    for part in html_parts:
        try:
            html = part.get_content()
        except Exception as exc:
            logger.warning("failed to decode text/html part: %s", exc)
            continue
        if html:
            # Strip tags for a plain-text fallback; this is a
            # deliberately minimal transformation for the demo path.
            text = re.sub(r"<[^>]+>", " ", str(html))
            text = re.sub(r"\s+", " ", text).strip()
            if text:
                logger.info(
                    "no text/plain part in inbound MIME; using stripped "
                    "text/html as body"
                )
                return text
    return ""


def _fetch_ses_body_from_s3(mail):
    """Fetch the raw inbound MIME message from S3 and extract its text.

    SES inbound receipt notifications delivered to Lambda contain only
    headers/metadata (messageId, source, commonHeaders); the message
    body is NOT in the event. When the receipt rule places an S3 action
    ahead of the Lambda action, the raw message is stored under the
    bucket configured in SES_INBOUND_BUCKET (object key:
    SES_INBOUND_PREFIX + mail.messageId; SES commonly uses the
    messageId as the object key). When the bucket variable is not set,
    this returns an empty string and the caller falls back to the
    documented header-only behavior.

    Failures (missing object, access denied, oversize or unparseable
    message) are logged and degrade to an empty string so the subject
    fallback still applies; they never abort the inbound flow. The
    fetched object is capped at _MAX_MIME_BYTES.
    """
    bucket = os.environ.get("SES_INBOUND_BUCKET", "").strip()
    if not bucket:
        return ""
    message_id = (mail.get("messageId") or "").strip()
    if not message_id:
        logger.warning(
            "SES_INBOUND_BUCKET is set but the SES record has no "
            "mail.messageId; cannot retrieve the stored message"
        )
        return ""
    prefix = os.environ.get("SES_INBOUND_PREFIX", "").strip()
    key = "%s%s" % (prefix, message_id)
    try:
        response = _get_s3_client().get_object(Bucket=bucket, Key=key)
        raw = response["Body"].read(_MAX_MIME_BYTES + 1)
    except Exception as exc:
        logger.warning(
            "failed to fetch inbound message s3://%s/%s: %s",
            bucket,
            key,
            exc,
        )
        return ""
    if len(raw) > _MAX_MIME_BYTES:
        logger.warning(
            "inbound message s3://%s/%s exceeds %d bytes; refusing to "
            "parse and falling back to the subject line",
            bucket,
            key,
            _MAX_MIME_BYTES,
        )
        return ""
    return _extract_text_from_mime(raw)


def _is_auto_submitted(mail):
    """True when the inbound mail is machine-generated (RFC 3834 Auto-Submitted, bulk/junk/auto_reply precedence)."""
    for header in mail.get("headers") or []:
        name = str(header.get("name", "")).strip().lower()
        value = str(header.get("value", "")).strip().lower()
        if name == "auto-submitted" and value and value != "no":
            return True
        if name == "precedence" and value in ("bulk", "junk", "auto_reply", "list"):
            return True
    return False


def parse_ses_email_event(event):
    """Parse an SES inbound receipt event delivered to Lambda.

    The SES->Lambda receipt event carries headers only
    (mail.commonHeaders); it does NOT include the MIME body. The body
    resolution order is:

      1. An optional `content` field on the record, when present (e.g.
         injected by a preprocessing step in a test harness).
      2. The raw MIME message fetched from S3 via
         _fetch_ses_body_from_s3(), when SES_INBOUND_BUCKET is
         configured (i.e. the receipt rule stores the message in S3
         ahead of the Lambda action). This is the production path for
         full email content.
      3. The subject line, with the degraded fallback logged loudly so
         a header-only deployment is never mistaken for a functional
         email channel.
      4. A placeholder when the subject is also blank, so blank-subject
         emails are not dropped.

    A body obtained from the MIME message (steps 1 and 2) has its
    quoted thread history removed by _strip_quoted_history() before it
    becomes the message text, so only the judge's new text is read by
    the feedback check, the loop guard and the engine.

    The reply target is derived by _extract_reply_target() from
    mail.source and the receipt verdicts (never from Reply-To / From
    alone) and recorded on the message so send_reply() can verify it.

    Returns an InboundMessage or None if no email record is found.
    """
    for record in event.get("Records", []):
        ses = record.get("ses")
        if not ses:
            continue
        mail = ses.get("mail", {})
        receipt = ses.get("receipt") or {}
        sender = (mail.get("source") or "").strip()
        if not sender:
            logger.warning("skipping SES record with no mail.source")
            continue
        subject = (mail.get("commonHeaders", {}).get("subject") or "").strip()
        content = (ses.get("content") or mail.get("content") or "").strip()
        body = ""
        if content:
            try:
                raw = base64.b64decode(content)
            except Exception as exc:
                logger.warning(
                    "failed to base64-decode SES record content: %s", exc
                )
            else:
                body = _extract_text_from_mime(raw)
        if not body:
            stored = _fetch_ses_body_from_s3(mail)
            if stored:
                body = stored
        if body:
            # Drop the quoted thread history so the previous turn (old
            # "that's wrong", our own quoted signature, the old answer)
            # is not read as part of this message. A body that cannot
            # be reduced is kept as it is.
            body = _strip_quoted_history(body) or body
        if not body:
            if subject:
                logger.warning(
                    "SES record has no message body (receipt events carry "
                    "headers only and no stored message was retrievable); "
                    "using subject line as body. Configure an S3 receipt "
                    "action and set SES_INBOUND_BUCKET for full content."
                )
                body = subject
            else:
                logger.warning(
                    "SES record has neither body nor subject; using a "
                    "placeholder so the message is not dropped"
                )
                body = "(no subject)"
        reply_target = _extract_reply_target(mail, receipt)
        if not reply_target:
            logger.warning(
                "SES record from %r yielded no usable reply target; "
                "a reply on this message will be refused",
                sender,
            )
        msg = InboundMessage(
            "email", sender, body, reply_target=reply_target, subject=subject
        )
        msg.auto_submitted = _is_auto_submitted(mail)
        setattr(msg, _GUARDED_TARGET_ATTR, reply_target)
        return msg
    return None


def parse_inbound_event(event):
    """Normalize any supported inbound event into an InboundMessage.

    Both parsers are run (no short-circuit) so a composite event that
    somehow carries both SMS and SES records never silently shadows one
    channel: the presence of a second parseable record is logged loudly.
    The SMS message is preferred when both are present, since the
    orchestrator replies on a single channel per invocation.
    """
    sms_message = parse_sns_sms_event(event)
    email_message = parse_ses_email_event(event)
    if sms_message is not None and email_message is not None:
        logger.warning(
            "composite event contained both SMS and SES records; "
            "processing SMS %r and dropping email %r",
            sms_message,
            email_message,
        )
    return sms_message if sms_message is not None else email_message


def _refusal(destination):
    """Build a PermissionError naming the refused destination.

    The normalized destination is included so the operator can see
    exactly which identity is missing from ALLOWED_DESTINATIONS without
    guessing at the pre-normalization form.
    """
    return PermissionError(
        "destination not in ALLOWED_DESTINATIONS: reply refused for %r"
        % (_normalize_destination(destination),)
    )


def send_sms(destination_number, text, allow_unlisted=False):
    """Send an SMS reply via pinpoint-sms-voice-v2 SendTextMessage.

    Refuses destinations outside ALLOWED_DESTINATIONS and any destination
    that is not an E.164 phone number, so a mailbox address misplaced in
    the allowlist fails fast here instead of inside the service call.
    The body text is sanitized (control characters stripped, length cap)
    before sending. Returns the service response dict. The body is built
    with an f-string so a literal percent sign in `text` is passed
    through unchanged. When allow_unlisted is True, only the
    ALLOWED_DESTINATIONS check is skipped (all other validation stays);
    send_reply() uses this because a judge's reply address cannot be
    known in advance.
    """
    _require_allowlist()
    _validate_destination_for_channel("sms", destination_number)
    if not allow_unlisted and not is_allowed(destination_number):
        raise _refusal(destination_number)
    body_text = _sanitize_text(text, "SMS body", _MAX_BODY_CHARS_SMS)
    if not body_text or not body_text.strip():
        raise ValueError("SMS body is empty after sanitization")
    origination_id = os.environ.get("SMS_ORIGINATION_ID", "").strip()
    if not origination_id:
        raise RuntimeError("SMS_ORIGINATION_ID environment variable is required")

    client = _get_sms_client()
    logger.info(
        "sending SMS reply to %r via origination %r",
        _normalize_destination(destination_number),
        origination_id,
    )
    response = client.send_text_message(
        DestinationPhoneNumber=_normalize_destination(destination_number),
        OriginationIdentity=origination_id,
        MessageBody=f"{body_text}\n\n-- {REX_SIGNATURE}",
    )
    logger.info(
        "SMS dispatched: MessageId=%r",
        response.get("MessageId"),
    )
    return response


def send_email(to_address, subject, text, allow_unlisted=False):
    """Send an email reply via sesv2 SendEmail from the judges' mailbox.

    Refuses destinations outside ALLOWED_DESTINATIONS and any destination
    that is not a mailbox address, so a phone number misplaced in the
    allowlist fails fast here instead of inside the service call. When
    allow_unlisted is True, only the ALLOWED_DESTINATIONS check is
    skipped (all other validation stays); send_reply() uses this because
    a judge's reply address cannot be known in advance.
    Subject and body are sanitized (control characters stripped, length
    caps) so a stray control byte in a demo payload cannot corrupt the
    SES Subject header. The body is fit to the email cap (truncated with
    a note when too long) rather than raising, so a long engine answer
    never blocks the reply. Returns the service response dict. The body
    is built with an f-string so a literal percent sign in `text` is
    passed through unchanged.

    Only the "Simple" content type is supported: this thin wrapper sends
    plain-text replies and intentionally does not build Raw MIME or
    Template messages. If formatted (HTML/template) replies are ever
    required, add a separate function rather than widening this one, so
    the demo path stays minimal and auditable.

    The default subject is applied only when `subject` is None; an
    explicitly provided subject (including an empty string, if the
    caller truly intends one) is passed through unchanged. Callers that
    have no original subject (such as send_reply() for a subject-less
    inbound email) should pass None and let this default apply rather
    than duplicating the fallback text.
    """
    _require_allowlist()
    _validate_destination_for_channel("email", to_address)
    if not allow_unlisted and not is_allowed(to_address):
        raise _refusal(to_address)
    body_text = _sanitize_and_fit(text, "email body", _MAX_BODY_CHARS_EMAIL)
    if not body_text or not body_text.strip():
        raise ValueError("email body is empty after sanitization")
    if subject is not None:
        subject = _sanitize_text(subject, "email subject", _MAX_SUBJECT_CHARS)
    from_address = os.environ.get("SES_FROM", "").strip()
    if not from_address:
        raise RuntimeError("SES_FROM environment variable is required")

    client = _get_ses_client()
    logger.info(
        "sending email reply to %r from %r (subject=%r)",
        _normalize_destination(to_address),
        from_address,
        subject,
    )
    response = client.send_email(
        FromEmailAddress=from_address,
        Destination={"ToAddresses": [_normalize_destination(to_address)]},
        Content={
            "Simple": {
                "Subject": {
                    "Data": (
                        subject if subject is not None else "Re: your question"
                    )
                },
                "Body": {
                    "Text": {"Data": f"{body_text}\n\n-- {REX_SIGNATURE}"}
                },
            }
        },
    )
    logger.info(
        "email dispatched: MessageId=%r",
        response.get("MessageId"),
    )
    return response


def _authorised_email_reply_target(message):
    """The reply target the guard rules permit for this message, or "".

    When the message's sender (mail.source) is a mailbox other than our
    own, the rules permit exactly that sender. When the sender is empty
    or our own mailbox, only the target recorded by
    parse_ses_email_event() from the SES event (DKIM-verified From) is
    permitted; a hand-built message cannot authorise anything else.
    """
    own = _own_address()
    sender = _normalize_mailbox(getattr(message, "sender", None))
    if sender and sender != own:
        return sender
    return _normalize_mailbox(getattr(message, _GUARDED_TARGET_ATTR, None))


def _email_reply_refusal(message, reply_target):
    """Return the reason an email reply to `reply_target` must be refused, or None."""
    own = _own_address()
    if own and reply_target == own:
        return "reply target %r is our own mailbox (no reply loop)" % (
            reply_target,
        )
    authorised = _authorised_email_reply_target(message)
    if not authorised:
        return (
            "no reply target is authorised for this sender (unauthenticated "
            "or unverifiable); requested %r" % (reply_target,)
        )
    if reply_target != authorised:
        return (
            "reply target %r differs from the authorised inbound sender %r "
            "(Reply-To / forged From never redirect replies)"
            % (reply_target, authorised)
        )
    return None


def send_reply(message, text):
    """Reply on the same channel the inbound message arrived on.

    Replies to the inbound message's own authenticated sender
    (reply_target as produced by the reply-target guard rules, see
    _extract_reply_target) are always allowed, even when that address
    is not in ALLOWED_DESTINATIONS, because judges' addresses cannot be
    known in advance and so cannot be pre-listed. ALLOWED_DESTINATIONS
    gates every other outbound send (direct send_sms / send_email calls
    to any other destination still raise PermissionError).

    For email, allow_unlisted is granted ONLY to a target produced by
    the guard rules: an InboundMessage whose reply_target differs from
    what the rules give for its sender (for example one built by hand
    with a third-party reply_target), or whose target is our own
    mailbox, is refused (logged with sender and reason, returns None, no
    service call). SMS replies (originationNumber) are unchanged.

    Returns the service response dict on success. Returns None (a
    sentinel) when the message carries no usable reply target (None or
    empty after normalization), so an unexpected or unparseable sender
    is logged and skipped instead of producing an invalid service call
    (e.g. ToAddresses: [""]) or aborting the rest of the inbound flow.

    For email, the original subject carried on the InboundMessage is
    threaded into the reply as "Re: <original>" so judge-facing replies
    keep the conversation context; when the inbound message carried no
    subject, None is passed so send_email() applies its own default in
    exactly one place.

    An unsupported channel is logged with full message context before
    the ValueError is raised, so a presenter tracing a failed demo
    invocation can see exactly which message triggered it.
    """
    reply_target = _normalize_destination(
        getattr(message, "reply_target", None)
    )
    if not reply_target:
        logger.warning(
            "reply skipped: inbound message from %r on channel %r has no "
            "usable reply target",
            getattr(message, "sender", None),
            getattr(message, "channel", None),
        )
        return None
    if getattr(message, "channel", None) == "email":
        reason = _email_reply_refusal(message, reply_target)
        if reason:
            logger.warning(
                "reply refused for email sender %r: %s",
                getattr(message, "sender", None),
                reason,
            )
            return None
    try:
        if message.channel == "sms":
            return send_sms(reply_target, text, allow_unlisted=True)
        if message.channel == "email":
            original = (message.subject or "").strip()
            subject = f"Re: {original}" if original else None
            return send_email(reply_target, subject, text, allow_unlisted=True)
    except PermissionError as exc:
        logger.warning(
            "reply refused for %s sender %r: %s",
            message.channel,
            message.reply_target,
            exc,
        )
        return None
    logger.error(
        "cannot send reply: unsupported channel on inbound message %r",
        message,
    )
    raise ValueError(
        "unsupported channel %r on inbound message from %r"
        % (message.channel, message.sender)
    )
