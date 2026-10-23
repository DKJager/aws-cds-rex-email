"""Validate the external engine client behaviour.

Checks that:
- configuration errors are raised when env vars are missing
- the request payload shape matches expectations (POST + JSON)
- engine response validation works (answer/clarify)
- the return value surfaces the engine's answer/clarify payload
- a timeout is always passed to the transport call (no hang risk)
- network/transport failures (URLError, HTTPError 4xx/5xx, socket
  timeout, connection-refused OSError) surface uniformly as
  NexusCallError
- pre-existing NEXUS_ENDPOINT/NEXUS_TOKEN environment values are
  preserved exactly across a test run (no state leakage)
- no secrets or tokens appear in code (negative assertion, scanning
  for common secret patterns, not only the test fake token)
"""

import inspect
import json
import os
import re
import socket
import unittest
import urllib.error
from unittest.mock import MagicMock, patch

from src import nexus_client

FAKE_ENDPOINT = "https://nexus.example.com/query"
FAKE_TOKEN = "test-token-abc123"

# Interface contract (shared by every test in this module):
# src/nexus_client performs its single outbound HTTP call via
# urllib.request.urlopen, imported into the module's own namespace.
# Patching the symbol as imported/used inside src/nexus_client (rather
# than the global urllib.request module) guarantees the mock is always
# hit regardless of how the client references urlopen. The contract
# test below fails loudly if the client ever stops exposing urlopen,
# so the suite can never pass vacuously against a client that switched
# to a different HTTP library.
URLOPEN_TARGET = "src.nexus_client.urlopen"

# Common secret patterns that must never appear in the client source.
# Each entry is (human-readable label, compiled regex). These are
# deliberately generic shapes (long bearer-like literals, private-key
# blocks, AWS-style access key IDs, hard-coded password/token
# assignments) rather than any real credential value.
SECRET_PATTERNS = (
    ("PEM private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("AWS-style access key id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    (
        "hard-coded token assignment",
        re.compile(
            r"(?i)\b(?:api[_-]?key|secret|password|passwd|token)\b"
            r"\s*=\s*['\"][^'\"]{8,}['\"]"
        ),
    ),
    (
        "hard-coded bearer literal",
        re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]{16,}"),
    ),
)


class TestNexusClient(unittest.TestCase):
    def setUp(self):
        # Save any pre-existing values so we can restore them afterwards
        # instead of blindly popping (which would destroy a developer's
        # real environment values).
        self._saved = {}
        for key in ("NEXUS_ENDPOINT", "NEXUS_TOKEN"):
            self._saved[key] = os.environ.get(key)
        self.addCleanup(self._restore_env)

        os.environ["NEXUS_ENDPOINT"] = FAKE_ENDPOINT
        os.environ["NEXUS_TOKEN"] = FAKE_TOKEN

    def _restore_env(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _all_headers(self, request):
        """Return a lowercase-keyed dict of all headers on the request.

        urllib stores headers with capitalized keys, but clients may set
        them with any casing (e.g. http.client style "authorization").
        Comparing case-insensitively keeps this test correct for any
        functionally-correct client implementation.
        """
        headers = {}
        for source in (
            getattr(request, "headers", {}) or {},
            getattr(request, "unredirected_hdrs", {}) or {},
        ):
            for key, value in source.items():
                headers[key.lower()] = value
        return headers

    def test_missing_endpoint_raises_config_error(self):
        """When NEXUS_ENDPOINT is missing, NexusConfigError is raised."""
        os.environ.pop("NEXUS_ENDPOINT", None)
        with self.assertRaises(nexus_client.NexusConfigError):
            nexus_client.query_engine("q", "s1")

    def test_missing_token_raises_config_error(self):
        """When NEXUS_TOKEN is missing, NexusConfigError is raised."""
        os.environ.pop("NEXUS_TOKEN", None)
        with self.assertRaises(nexus_client.NexusConfigError):
            nexus_client.query_engine("q", "s1")

    def test_client_exposes_urlopen_interface(self):
        """Contract guard: the client must expose a urlopen callable.

        Every patch(URLOPEN_TARGET) test in this suite depends on the
        client calling urllib's urlopen through its own module namespace.
        If the client is ever rewritten to use another HTTP library this
        test fails, forcing the patch target to be re-confirmed rather
        than letting the other tests pass vacuously.
        """
        self.assertTrue(
            callable(getattr(nexus_client, "urlopen", None)),
            "src.nexus_client must import and expose urlopen; "
            "update URLOPEN_TARGET to the client's real HTTP callable",
        )

    def _assert_request_common(self, request):
        """Shared assertions on the captured urlopen request object."""
        # The client must post to the configured endpoint, never a
        # hard-coded or wrong URL.
        self.assertEqual(request.full_url, FAKE_ENDPOINT)
        # The request must be an HTTP POST; a client that silently sends
        # GET (e.g. by omitting the body) would be rejected by the real
        # engine and must fail here.
        self.assertEqual(request.get_method(), "POST")
        # The body must be JSON with a JSON content type
        # (case-insensitive header lookup).
        self.assertEqual(
            self._all_headers(request).get("content-type"), "application/json"
        )
        # Byte-decode the body as UTF-8 to prove it is encoded JSON.
        payload = json.loads(request.data.decode("utf-8"))
        return payload

    @patch(URLOPEN_TARGET)
    def test_request_payload_contains_required_fields(self, mock_urlopen):
        """The HTTP request body must include question and session_id."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = (
            b'{"answer": "test answer"}'
        )
        mock_urlopen.return_value = mock_resp

        nexus_client.query_engine("What is 2+2?", "sess-01")

        # Retrieve the request object passed to urlopen
        request = mock_urlopen.call_args[0][0]
        payload = self._assert_request_common(request)
        self.assertEqual(payload["question"], "What is 2+2?")
        self.assertEqual(payload["session_id"], "sess-01")
        self.assertNotIn("feedback", payload)  # no feedback passed

    @patch(URLOPEN_TARGET)
    def test_request_includes_feedback_when_provided(self, mock_urlopen):
        """When feedback is supplied it must be in the payload.

        The feedback fixture deliberately contains non-ASCII text so that
        a client which encodes the body incorrectly (e.g. as ASCII or
        Latin-1) is caught: the round-trip decoded string must match the
        original exactly.
        """
        mock_resp = MagicMock()
        mock_resp.read.return_value = (
            b'{"answer": "corrected"}'
        )
        mock_urlopen.return_value = mock_resp

        feedback = "that's wrong \u2014 r\u00e9ponse incorrecte \u4e2d\u6587"
        nexus_client.query_engine("q", "s1", feedback=feedback)

        request = mock_urlopen.call_args[0][0]
        payload = self._assert_request_common(request)
        # Compare the decoded strings, not just that the key exists: any
        # mis-encoding of the non-ASCII characters would change the value.
        self.assertEqual(payload["feedback"], feedback)

    @patch(URLOPEN_TARGET)
    def test_authorization_header(self, mock_urlopen):
        """The Authorization header must be Bearer NEXUS_TOKEN.

        The lookup is case-insensitive: a client may spell the header
        "Authorization" or "authorization"; both are functionally
        equivalent on the wire.
        """
        mock_resp = MagicMock()
        mock_resp.read.return_value = (
            b'{"answer": "ok"}'
        )
        mock_urlopen.return_value = mock_resp

        nexus_client.query_engine("q", "s1")

        request = mock_urlopen.call_args[0][0]
        self.assertEqual(
            self._all_headers(request).get("authorization"),
            "Bearer " + FAKE_TOKEN,
        )

    @patch(URLOPEN_TARGET)
    def test_timeout_is_passed_to_urlopen(self, mock_urlopen):
        """The client must pass an explicit timeout to urlopen.

        Without a timeout the call can hang indefinitely on an
        unresponsive engine, stalling the orchestrator. A client that
        calls urlopen with no timeout keyword (or timeout=None) must
        fail this test. Any positive numeric timeout is accepted; the
        exact value is a client configuration choice.
        """
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"answer": "ok"}'
        mock_urlopen.return_value = mock_resp

        nexus_client.query_engine("q", "s1")

        _, kwargs = mock_urlopen.call_args
        self.assertIn(
            "timeout",
            kwargs,
            "query_engine must pass an explicit timeout to urlopen",
        )
        timeout = kwargs["timeout"]
        self.assertIsNotNone(timeout, "timeout must not be None")
        self.assertIsInstance(
            timeout,
            (int, float),
            "timeout must be a number of seconds",
        )
        self.assertGreater(timeout, 0, "timeout must be positive")

    @patch(URLOPEN_TARGET)
    def test_answer_response_returned(self, mock_urlopen):
        """A valid {"answer": ...} response is returned/handled."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = (
            b'{"answer": "the answer"}'
        )
        mock_urlopen.return_value = mock_resp

        result = nexus_client.query_engine("q", "s1")

        # The result must surface the answer text (as a parsed dict or
        # as the raw decoded payload, depending on client design).
        if isinstance(result, dict):
            self.assertEqual(result.get("answer"), "the answer")
        else:
            self.assertIn("the answer", str(result))

    @patch(URLOPEN_TARGET)
    def test_answer_response_return_value_shape(self, mock_urlopen):
        """The return value must equal the parsed {"answer": ...} dict.

        A client that swallows the answer field, returns raw bytes, or
        returns a differently-shaped object must fail this test: the
        caller relies on receiving exactly the decoded JSON mapping.
        """
        mock_resp = MagicMock()
        mock_resp.read.return_value = (
            b'{"answer": "test answer"}'
        )
        mock_urlopen.return_value = mock_resp

        result = nexus_client.query_engine("q", "s1")

        self.assertIsInstance(result, dict)
        self.assertEqual(result, {"answer": "test answer"})

    @patch(URLOPEN_TARGET)
    def test_clarify_response_returned(self, mock_urlopen):
        """A valid {"clarify": ...} response must be accepted, not rejected.

        Engine validation supports answer OR clarify; a client that
        rejects a valid clarify response must fail this test. This is
        the positive counterpart to
        test_engine_response_must_have_answer_or_clarify: together they
        pin both the success and failure branches of response
        validation, so neither can pass vacuously.
        """
        mock_resp = MagicMock()
        mock_resp.read.return_value = (
            b'{"clarify": "Which account do you mean?"}'
        )
        mock_urlopen.return_value = mock_resp

        result = nexus_client.query_engine("q", "s1")

        if isinstance(result, dict):
            self.assertEqual(
                result.get("clarify"), "Which account do you mean?"
            )
        else:
            self.assertIn("Which account do you mean?", str(result))

    @patch(URLOPEN_TARGET)
    def test_clarify_response_return_value_shape(self, mock_urlopen):
        """The return value must equal the parsed {"clarify": ...} dict.

        Mirrors test_answer_response_return_value_shape for the clarify
        branch: the caller must receive exactly the decoded JSON mapping,
        with the clarify text intact, so an agent can relay the
        clarifying question to the user on the same channel.
        """
        mock_resp = MagicMock()
        mock_resp.read.return_value = (
            b'{"clarify": "Which account do you mean?"}'
        )
        mock_urlopen.return_value = mock_resp

        result = nexus_client.query_engine("q", "s1")

        self.assertIsInstance(result, dict)
        self.assertEqual(result, {"clarify": "Which account do you mean?"})

    @patch(URLOPEN_TARGET)
    def test_engine_response_must_have_answer_or_clarify(self, mock_urlopen):
        """Responses without 'answer' or 'clarify' raise NexusCallError."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"other_field": true}'
        mock_urlopen.return_value = mock_resp

        with self.assertRaises(nexus_client.NexusCallError):
            nexus_client.query_engine("q", "s1")

    @patch(URLOPEN_TARGET)
    def test_non_json_response_raises_error(self, mock_urlopen):
        """A non-JSON response body must raise NexusCallError."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"plain text not json"
        mock_urlopen.return_value = mock_resp

        with self.assertRaises(nexus_client.NexusCallError):
            nexus_client.query_engine("q", "s1")

    @patch(URLOPEN_TARGET)
    def test_url_error_raises_nexus_call_error(self, mock_urlopen):
        """A transport-level URLError (e.g. connection refused) must
        surface as NexusCallError, never bubble up raw."""
        mock_urlopen.side_effect = urllib.error.URLError("connection refused")

        with self.assertRaises(nexus_client.NexusCallError):
            nexus_client.query_engine("q", "s1")

    @patch(URLOPEN_TARGET)
    def test_http_error_raises_nexus_call_error(self, mock_urlopen):
        """An HTTPError with a 5xx status must surface as NexusCallError.

        Consumers of the client rely on a single, uniform exception type
        for all engine-call failures; a raw urllib.error.HTTPError leaking
        out of query_engine must fail this test.
        """
        mock_urlopen.side_effect = urllib.error.HTTPError(
            FAKE_ENDPOINT, 502, "Bad Gateway", {}, None
        )

        with self.assertRaises(nexus_client.NexusCallError):
            nexus_client.query_engine("q", "s1")

    @patch(URLOPEN_TARGET)
    def test_http_4xx_error_raises_nexus_call_error(self, mock_urlopen):
        """An HTTPError with a 4xx status must also surface as
        NexusCallError (not re-raised raw, not treated as success)."""
        mock_urlopen.side_effect = urllib.error.HTTPError(
            FAKE_ENDPOINT, 429, "Too Many Requests", {}, None
        )

        with self.assertRaises(nexus_client.NexusCallError):
            nexus_client.query_engine("q", "s1")

    @patch(URLOPEN_TARGET)
    def test_timeout_raises_nexus_call_error(self, mock_urlopen):
        """A socket timeout must surface as NexusCallError."""
        mock_urlopen.side_effect = socket.timeout("timed out")

        with self.assertRaises(nexus_client.NexusCallError):
            nexus_client.query_engine("q", "s1")

    @patch(URLOPEN_TARGET)
    def test_connection_refused_oserror_raises_nexus_call_error(self,
                                                                mock_urlopen):
        """A low-level OSError (connection refused) must surface as
        NexusCallError so transient network faults never crash callers
        with an unhandled exception type."""
        mock_urlopen.side_effect = ConnectionRefusedError(
            111, "Connection refused"
        )

        with self.assertRaises(nexus_client.NexusCallError):
            nexus_client.query_engine("q", "s1")

    def test_environment_is_restored_after_test(self):
        """A pre-existing NEXUS_* environment must survive a test run.

        This guards the save/restore behaviour of setUp/_restore_env:
        simulate a developer environment that already defines the
        variables, run a nested fixture instance through its lifecycle,
        and assert the originals are intact afterwards (no state leakage
        into subsequent suites).
        """
        orig_endpoint = os.environ.get("NEXUS_ENDPOINT")
        orig_token = os.environ.get("NEXUS_TOKEN")
        try:
            os.environ["NEXUS_ENDPOINT"] = "https://dev-env.example.com/q"
            os.environ["NEXUS_TOKEN"] = "dev-env-token"

            nested = TestNexusClient("test_missing_endpoint_raises_config_error")
            nested.setUp()
            try:
                # Inside the fixture the fake values are in force.
                self.assertEqual(os.environ["NEXUS_ENDPOINT"], FAKE_ENDPOINT)
                self.assertEqual(os.environ["NEXUS_TOKEN"], FAKE_TOKEN)
            finally:
                nested.doCleanups()

            # After cleanup the developer's original values are restored
            # exactly, not deleted.
            self.assertEqual(
                os.environ.get("NEXUS_ENDPOINT"), "https://dev-env.example.com/q"
            )
            self.assertEqual(os.environ.get("NEXUS_TOKEN"), "dev-env-token")
        finally:
            # Restore this test's own fixture values for the outer cleanup.
            if orig_endpoint is None:
                os.environ.pop("NEXUS_ENDPOINT", None)
            else:
                os.environ["NEXUS_ENDPOINT"] = orig_endpoint
            if orig_token is None:
                os.environ.pop("NEXUS_TOKEN", None)
            else:
                os.environ["NEXUS_TOKEN"] = orig_token

    def test_no_secret_patterns_in_source(self):
        """Guard: the client source must contain no embedded secrets.

        Scans the module source for common secret shapes (private-key
        blocks, AWS-style access key IDs, hard-coded token/password
        assignments, inline Bearer literals) rather than only checking
        that this suite's fake token is absent, so the test name does
        not overpromise: any realistic hard-coded credential fails here.

        The source is obtained via inspect.getsource on the imported
        module rather than by opening a file through a relative path,
        so no path traversal or missing-file ambiguity is involved: if
        the module is importable its source is inspected, and if it is
        not importable every other test in this suite already fails.
        """
        source = inspect.getsource(nexus_client)
        # The suite's own fake token must never appear either; a client
        # that happened to embed this exact value would be reading test
        # fixtures rather than the environment.
        self.assertNotIn(FAKE_TOKEN, source)
        for label, pattern in SECRET_PATTERNS:
            self.assertIsNone(
                pattern.search(source),
                "src/nexus_client.py contains a %s; secrets must come "
                "from environment variables only" % label,
            )


if __name__ == "__main__":
    unittest.main()
