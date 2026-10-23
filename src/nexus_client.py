"""Thin HTTPS client for the 3Dogs Nexus engine.

Wrapper discipline: this module calls the Nexus engine, which lives in
the engine/ directory of this repository (or any service that implements
the same HTTPS contract). It contains no engine logic, no prompts, no
model or provider names, and no credentials. All configuration comes
from environment variables (NEXUS_ENDPOINT, NEXUS_TOKEN) at call time.

Timeout policy: the engine call timeout is a positive number of seconds.
The default comes from the NEXUS_TIMEOUT environment variable (default
30 seconds) so a hanging external engine cannot cause Lambda billing
runaway. An explicit per-call ``timeout`` argument overrides the default;
it is coerced to float and must be a finite positive number of seconds
(invalid values raise ValueError), so callers can never request an
indefinite hang or an immediate-failure timeout.

HTTPS policy: the endpoint must be an https:// URL so the bearer token is
never sent in cleartext. The only exception is the local mock policy below.

Local mock policy: for demo/reviewer convenience, NEXUS_TOKEN may be empty
when (and only when) NEXUS_ENDPOINT points at localhost/127.0.0.1 over
http, so a stub engine can be run without minting a fake token.

Redirect policy: redirects are followed, but the Authorization header is
stripped on any redirect so the engine token can never be leaked to a
redirect target host.

Response contract: the engine reply must contain exactly one of the
"answer" or "clarify" keys (mutually exclusive). A response carrying both
keys has no defined precedence and is rejected with NexusCallError so
callers never have to guess whether to answer or to ask a clarifying
question. The response body must be valid UTF-8 JSON; undecodable bodies
are rejected with NexusCallError.
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_TIMEOUT_SECONDS = 30

MAX_ERROR_BODY_SNIPPET = 120

# Upper bound on the engine response body we will read (1 MiB).
MAX_RESPONSE_BYTES = 1 << 20


class NexusConfigError(RuntimeError):
    """Raised when required engine configuration is missing or invalid."""


class NexusCallError(RuntimeError):
    """Raised when the external engine call fails or returns bad data."""


class _NoAuthRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Redirect handler that never forwards the Authorization header.

    urllib's default redirect handling re-sends the original request
    headers (including "Authorization: Bearer <token>") to the redirect
    target. A compromised or misconfigured endpoint could use a 30x
    response to exfiltrate the token, so we rebuild the redirected
    request without the Authorization header.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new_req = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new_req is None:
            return None
        new_req.remove_header("Authorization")
        return new_req


_OPENER = urllib.request.build_opener(_NoAuthRedirectHandler())

urlopen = _OPENER.open


def _is_local_endpoint(endpoint):
    """True when the endpoint targets the local machine (loopback)."""
    try:
        host = urllib.parse.urlparse(endpoint).hostname or ""
    except ValueError:
        return False
    return host.lower() in ("localhost", "127.0.0.1", "::1")


def _config():
    endpoint = os.environ.get("NEXUS_ENDPOINT", "").strip()
    token = os.environ.get("NEXUS_TOKEN", "").strip()
    if not endpoint:
        raise NexusConfigError(
            "NEXUS_ENDPOINT environment variable is required"
        )
    if endpoint.lower().startswith("https://"):
        if not token:
            raise NexusConfigError(
                "NEXUS_TOKEN environment variable is required for https endpoints"
            )
    elif endpoint.lower().startswith("http://") and _is_local_endpoint(endpoint):
        # Local mock policy: token optional for a localhost stub engine.
        pass
    else:
        raise NexusConfigError(
            "NEXUS_ENDPOINT must be an https:// URL (or http://localhost for a "
            "local mock) (got: %s)" % endpoint
        )
    _validate_token(token)
    return endpoint, token


def _validate_token(token):
    """Reject tokens containing whitespace/control characters.

    Defense in depth: the token is placed directly into an Authorization
    header, so CR/LF (or other control bytes) could enable header
    injection. Reject them before the header is constructed.
    """
    if not token:
        return
    for ch in token:
        if ord(ch) < 33 or ord(ch) == 127:
            raise NexusConfigError(
                "NEXUS_TOKEN contains whitespace or control characters"
            )


def _default_timeout():
    """Resolve the default timeout from NEXUS_TIMEOUT (default 30s)."""
    raw = os.environ.get("NEXUS_TIMEOUT", "").strip()
    if not raw:
        return DEFAULT_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        raise NexusConfigError(
            "NEXUS_TIMEOUT must be a positive number of seconds (got: %s)" % raw
        )
    if value <= 0:
        raise NexusConfigError(
            "NEXUS_TIMEOUT must be greater than 0 seconds (got: %s)" % raw
        )
    return value


def _validate_args(question, session_id, feedback):
    if not isinstance(question, str) or not question.strip():
        raise ValueError("question must be a non-empty string")
    if not isinstance(session_id, str) or not session_id.strip():
        raise ValueError("session_id must be a non-empty string")
    if feedback is not None and not isinstance(feedback, str):
        raise ValueError("feedback must be a string or None")


def _validate_timeout(timeout):
    """Coerce an explicit timeout to a bounded positive float.

    None falls back to the configured default (NEXUS_TIMEOUT, default
    DEFAULT_TIMEOUT_SECONDS). An explicit value is coerced via float()
    and must be finite and strictly positive; bools, NaN, infinity, and
    non-positive values are rejected with ValueError so no caller can
    request an indefinite hang or an immediate-failure timeout.
    """
    if timeout is None:
        return _default_timeout()
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float, str)):
        raise ValueError("timeout must be a positive number of seconds")
    try:
        value = float(timeout)
    except (TypeError, ValueError):
        raise ValueError("timeout must be a positive number of seconds")
    if value != value or value == float("inf") or value == float("-inf"):
        raise ValueError("timeout must be a finite positive number of seconds")
    if value <= 0:
        raise ValueError("timeout must be greater than 0 seconds")
    return value


def _validate_response(data):
    if not isinstance(data, dict):
        raise NexusCallError("engine response is not a JSON object")
    # A response is valid when exactly one of the keys is present. An
    # explicit empty string "answer" is a legitimate "I have nothing to
    # say" reply; a non-string value for a present key is not. The keys
    # are mutually exclusive: a response carrying both has no defined
    # precedence (answer vs. clarify), so it is rejected outright.
    has_answer = "answer" in data
    has_clarify = "clarify" in data
    if not (has_answer or has_clarify):
        raise NexusCallError(
            "engine response missing an 'answer' or 'clarify' key"
        )
    if has_answer and has_clarify:
        raise NexusCallError(
            "engine response must not contain both 'answer' and 'clarify' "
            "(mutually exclusive)"
        )
    for key in ("answer", "clarify"):
        if key in data and data[key] is not None and not isinstance(data[key], str):
            raise NexusCallError(
                "engine response key %r must be a string or null" % key
            )
    return data


def query_engine(question, session_id, feedback=None, timeout=None, awaiting_clarification=False):
    """POST a question to the external engine and return its JSON response.

    Payload: {"question": ..., "session_id": ..., "feedback": ...?,
              "awaiting_clarification": ...}
    Expected response JSON keys (exactly one required, mutually exclusive):
      - "answer":  final answer text
      - "clarify": clarifying-question text (when the engine needs more info)

    ``timeout`` is coerced to a finite positive number of seconds; when
    None it defaults to the NEXUS_TIMEOUT environment variable (default
    30 seconds). Invalid explicit values raise ValueError.

    Returns the decoded response dict. Raises NexusCallError on failure,
    including non-UTF-8 or non-JSON response bodies.
    """
    endpoint, token = _config()
    _validate_args(question, session_id, feedback)
    timeout = _validate_timeout(timeout)

    payload = {"question": question, "session_id": session_id}
    if feedback is not None:
        payload["feedback"] = feedback
    payload["awaiting_clarification"] = bool(awaiting_clarification)

    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token

    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )

    try:
        # _OPENER strips the Authorization header on any redirect so the
        # token cannot be forwarded to a redirect target host.
        response = urlopen(request, timeout=timeout)
        body_bytes = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        snippet = ""
        try:
            snippet = exc.read().decode("utf-8", errors="replace")[
                :MAX_ERROR_BODY_SNIPPET
            ]
        except Exception:
            snippet = ""
        reason = exc.reason or ""
        raise NexusCallError(
            "engine call failed: HTTP %s %s%s"
            % (exc.code, reason, (" body=" + snippet) if snippet else "")
        ) from exc
    except urllib.error.URLError as exc:
        raise NexusCallError("engine call failed: %s" % str(exc)) from exc
    except OSError as exc:
        raise NexusCallError("engine call failed: %s" % str(exc)) from exc

    if len(body_bytes) > MAX_RESPONSE_BYTES:
        raise NexusCallError(
            "engine response exceeded %d-byte limit" % MAX_RESPONSE_BYTES
        )

    try:
        body = body_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise NexusCallError(
            "engine response body is not valid UTF-8"
        ) from exc

    try:
        data = json.loads(body)
    except ValueError as exc:
        raise NexusCallError("engine returned non-JSON response") from exc

    return _validate_response(data)
