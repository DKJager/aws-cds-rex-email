"""Verify that infra/template.yaml defines the correct SES receipt rule.

Binding residual fixes #850, #851 require the SES receipt rule's Recipients
list to be exactly ["judges@cds.3dogs.ai"]. Any other recipient (e.g.
sender@example.org, example.com) must cause this test to fail.

This test intentionally uses only the Python standard library (issue #168):
no third-party YAML dependency is imported, so the test runs in a clean
environment where installing packages is not possible. A small YAML-subset
reader extracts the Recipients lists from the template text; it handles both
inline-flow lists (``Recipients: ["a", "b"]``) and block lists.

Failure modes are deliberately distinguished (issue #178):

* "receipt rule missing / unparseable" -- the template contains no
  Recipients entry at all (e.g. the resource was renamed or removed);
* "recipients wrong" -- a Recipients entry exists but does not equal
  exactly ["judges@cds.3dogs.ai"].

Each condition raises a separate assertion message so a rename in
infra/template.yaml cannot masquerade as a recipients mismatch.

The negative control (issues #166, #185) loads the REAL infra/template.yaml
through the same file-loading and parsing pipeline as the positive test,
mutates a copy of the loaded data to inject a bad recipient, and asserts
that the same validation then fails. This proves the positive assertion
would actually catch a wrong recipient, instead of comparing hardcoded
literals that can never fail.

All rules in the template are checked (issue #167): the test asserts that
exactly one receipt rule exists and fails loudly if the template grows an
additional rule, so a second permissive rule cannot slip through.
"""

import copy
import os
import unittest

EXPECTED_RECIPIENTS = ["judges@cds.3dogs.ai"]

# Recipients that must never appear in any receipt rule. Loading the real
# template and asserting none of these are present makes the negative
# control exercise repository data instead of self-validating literals
# (issue #184).
FORBIDDEN_RECIPIENTS = [
    "sender@example.org",
    "3dogs.ai",
    "judges@example.com",
    "spam@example.org",
]


def _recipients_are_allowed(recipients):
    """Return True only when the Recipients list is exactly the judges mailbox.

    This is the single checking path used by both the positive test (against
    the real infra/template.yaml) and the negative-control test (against a
    mutated copy of the real loaded data), so the negative control genuinely
    exercises the same logic instead of comparing hardcoded literals
    (issues #166, #174, #185).
    """
    return recipients == EXPECTED_RECIPIENTS


def _unquote(value):
    """Strip surrounding quotes from a YAML scalar."""
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def _parse_flow_list(text):
    """Parse an inline YAML flow list like ["a", 'b'] into a list of str."""
    inner = text.strip()
    if inner.startswith("["):
        inner = inner[1:]
    if inner.endswith("]"):
        inner = inner[:-1]
    items = [item for item in inner.split(",") if item.strip()]
    return [_unquote(item) for item in items]


class ReceiptRuleMissingError(ValueError):
    """Raised when no Recipients entry exists in the template at all.

    A distinct exception type lets the positive test report "receipt rule
    resource missing" separately from "recipients wrong" (issue #178).
    """


def _extract_all_recipients(template_text):
    """Return a list with the Recipients list of every receipt rule found.

    Every Recipients entry in the template is collected (issues #167, #175):
    if the template gains additional rules (e.g. a spam-bucket or a second
    recipient), each one is returned here so the test can check them all.

    Raises ReceiptRuleMissingError if no Recipients entry is found -- this is
    the "resource missing" condition, distinct from a wrong recipients list
    (issue #178). Only the YAML subset needed for the SAM template is
    supported (block mappings, block and flow sequences, plain/quoted
    scalars).
    """
    if not isinstance(template_text, str):
        raise ValueError("Template text must be a string")
    lines = template_text.splitlines()
    all_recipients = []
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not stripped.startswith("Recipients:"):
            continue
        indent = len(line) - len(line.lstrip())
        after = stripped.split(":", 1)[1].strip()
        if after:
            # Inline flow list on the same line.
            if after.startswith("["):
                all_recipients.append(_parse_flow_list(after))
                continue
            raise ValueError("Unsupported Recipients scalar: %r" % after)
        # Block sequence on following lines.
        recipients = []
        j = idx + 1
        while j < len(lines):
            sub = lines[j]
            sub_stripped = sub.strip()
            if not sub_stripped or sub_stripped.startswith("#"):
                j += 1
                continue
            sub_indent = len(sub) - len(sub.lstrip())
            if sub_indent <= indent:
                break
            if sub_stripped.startswith("-"):
                recipients.append(_unquote(sub_stripped[1:]))
            j += 1
        all_recipients.append(recipients)
    if not all_recipients:
        raise ReceiptRuleMissingError(
            "No Recipients entry found in template -- the SES receipt rule "
            "resource is missing or was renamed"
        )
    return all_recipients


def _load_template_recipients(template_path):
    # UTF-8 is mandated project-wide; never rely on the locale default
    # codec (issues #169, #179).
    with open(template_path, "r", encoding="utf-8") as f:
        return _extract_all_recipients(f.read())


class TestSesReceiptRule(unittest.TestCase):
    def _template_path(self):
        return os.path.join(
            os.path.dirname(__file__), "..", "infra", "template.yaml"
        )

    def _load_real_recipients(self):
        """Load the real template through the production parse pipeline.

        Shared by the positive test and the negative control so both
        exercise exactly the same file-loading and parsing path
        (issues #166, #185).
        """
        template_path = self._template_path()
        self.assertTrue(
            os.path.isfile(template_path),
            "infra/template.yaml not found at expected path: %s" % template_path,
        )
        try:
            return _load_template_recipients(template_path)
        except ReceiptRuleMissingError as exc:
            self.fail(
                "SES receipt rule resource missing or renamed in "
                "infra/template.yaml: %s" % exc
            )

    def _assert_recipients_exactly_judges(self, all_recipients):
        """The full validation applied to parsed receipt-rule recipients.

        Asserts exactly one rule exists (issue #167) and that its Recipients
        list is exactly ["judges@cds.3dogs.ai"] via the shared checker.
        Raises AssertionError on any deviation, so the negative control can
        assert that a mutated input is rejected through the identical path
        (issues #166, #185).
        """
        self.assertIsInstance(
            all_recipients,
            list,
            "Parsed recipients must be a list, got %r" % type(all_recipients),
        )
        self.assertTrue(
            all_recipients, "No SES receipt-rule recipients found in template"
        )
        # Exactly one receipt rule is expected (issues #167, #183): if the
        # template grows additional rules later, the test fails loudly
        # instead of silently passing against an unintended rule.
        self.assertEqual(
            len(all_recipients),
            1,
            "Expected exactly one SES receipt rule in infra/template.yaml, "
            "found %d; refusing to check an unintended rule" % len(all_recipients),
        )
        recipients = all_recipients[0]
        self.assertIsInstance(
            recipients,
            list,
            "Each Recipients entry must be a list, got %r" % type(recipients),
        )
        # Use the shared checker so positive and negative paths cannot
        # diverge (issues #166, #174, #185).
        self.assertTrue(
            _recipients_are_allowed(recipients),
            "SES receipt rule Recipients must be exactly "
            "['judges@cds.3dogs.ai'] (resource present, recipients wrong); "
            "got %r" % (recipients,),
        )

    def test_receipt_rule_recipients_are_exactly_judges_cds_3dogs_ai(self):
        """The SES receipt rule must allow only judges@cds.3dogs.ai."""
        all_recipients = self._load_real_recipients()
        self._assert_recipients_exactly_judges(all_recipients)

    def test_no_unexpected_recipient_present_in_template(self):
        """Regression check against the real template, not self-validating
        literals (issue #184): load infra/template.yaml and assert that no
        forbidden recipient appears in any receipt rule."""
        all_recipients = self._load_real_recipients()
        flattened = {addr for recipients in all_recipients for addr in recipients}
        for forbidden in FORBIDDEN_RECIPIENTS:
            with self.subTest(forbidden_recipient=forbidden):
                self.assertNotIn(
                    forbidden,
                    flattened,
                    "Forbidden recipient %r must not appear in any SES "
                    "receipt rule" % forbidden,
                )
        # And the full flattened set must be exactly the judges mailbox.
        self.assertEqual(
            flattened,
            {"judges@cds.3dogs.ai"},
            "All recipients across all rules must be exactly "
            "{'judges@cds.3dogs.ai'}",
        )

    def test_negative_control_any_other_recipient_fails(self):
        """Negative control on the REAL loaded data (issues #166, #185).

        The real infra/template.yaml is loaded through the same file-loading
        and parsing pipeline as the positive test. A copy of the loaded data
        is then mutated to inject a bad recipient, and the identical
        validation is run expecting an AssertionError. This proves the
        positive assertion would actually catch a wrong recipient -- a
        hardcoded bad dict compared against the expected list would be
        vacuous (issue #166) and would bypass the real parse path
        (issue #185).
        """
        real_recipients = self._load_real_recipients()

        # Baseline: the unmodified real data passes validation. If this
        # fails, the negative control below is meaningless.
        self._assert_recipients_exactly_judges(copy.deepcopy(real_recipients))

        # Mutate a copy: inject a bad recipient into the single real rule.
        mutated = copy.deepcopy(real_recipients)
        self.assertEqual(
            len(mutated),
            1,
            "Negative control assumes exactly one parsed rule; got %d"
            % len(mutated),
        )
        mutated[0] = ["sender@example.org"]
        self.assertFalse(
            _recipients_are_allowed(mutated[0]),
            "Checker must reject injected recipient %r" % (mutated[0],),
        )
        with self.assertRaises(
            AssertionError,
            msg="Validation must fail when a bad recipient is injected "
            "into the real parsed template data",
        ):
            self._assert_recipients_exactly_judges(mutated)

        # Mutate a copy differently: append a bad recipient alongside the
        # correct one. Order-independent exactness must reject this too.
        mutated_extra = copy.deepcopy(real_recipients)
        mutated_extra[0] = list(mutated_extra[0]) + ["spam@example.org"]
        with self.assertRaises(
            AssertionError,
            msg="Validation must fail when an extra recipient is appended "
            "to the real parsed template data",
        ):
            self._assert_recipients_exactly_judges(mutated_extra)

        # Mutate a copy to add a second permissive rule (issue #167): the
        # exactly-one-rule assertion must reject it even though rule 0 is
        # still correct.
        mutated_two_rules = copy.deepcopy(real_recipients)
        mutated_two_rules.append(["sender@example.org"])
        with self.assertRaises(
            AssertionError,
            msg="Validation must fail when a second receipt rule is added "
            "to the real parsed template data",
        ):
            self._assert_recipients_exactly_judges(mutated_two_rules)

        # A template with no Recipients entry at all must raise the
        # "resource missing" error, not return an empty pass (issue #178).
        with self.assertRaises(ReceiptRuleMissingError):
            _extract_all_recipients(
                "Resources:\n  SomethingElse:\n    Type: AWS::S3::Bucket\n"
            )

        # Sanity: the correct list passes the checker.
        self.assertTrue(_recipients_are_allowed(["judges@cds.3dogs.ai"]))


# Discovery-compatible direct invocation (issue #186): allows both
# ``python -m unittest`` discovery and ``python tests/test_receipt_rule.py``.
if __name__ == "__main__":
    unittest.main()
