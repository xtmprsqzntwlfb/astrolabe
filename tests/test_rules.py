#!/usr/bin/env python3
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

"""Tests for Astrolabe's rule set.

Three layers, in increasing order of what they need to be present:

1. The rules are well formed and internally consistent. Needs nothing.
2. The JavaScript interpreter turns them into the expected commands. Needs
   ``node``; the real serialised rules are handed to ``test_interpreter.js``,
   so the rule set has exactly one definition.
3. The rules still match the Horizon forms they target. Needs a Horizon
   checkout; skipped with a note when one is not importable.

Run it directly (``python3 tests/test_rules.py``) or under pytest. To include
layer 3, run it with the interpreter that has Horizon on its path::

    cd ../horizon
    DJANGO_SETTINGS_MODULE=openstack_dashboard.test.settings PYTHONPATH=. \\
        ./.tox/runserver/bin/python ../astrolabe/tests/test_rules.py
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

from astrolabe import rules  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))

KINDS = {"value", "flag", "multi", "positional"}


class TestRuleSet(unittest.TestCase):
    """The rules are well formed, with no Horizon or Node required."""

    def test_ids_are_unique(self):
        ids = [form["id"] for form in rules.FORMS]
        self.assertEqual(len(ids), len(set(ids)))

    def test_every_form_is_complete(self):
        for form in rules.FORMS:
            with self.subTest(form["id"]):
                for key in ("title", "url", "method", "endpoint",
                            "envelope", "command", "fields"):
                    self.assertIn(key, form)
                self.assertTrue(form["fields"])
                self.assertTrue(form["command"])

    def test_url_patterns_compile(self):
        for form in rules.FORMS:
            with self.subTest(form["id"]):
                re.compile(form["url"])

    def test_field_kinds_are_known(self):
        for form in rules.FORMS:
            for field in form["fields"]:
                with self.subTest(form["id"], field=field["field"]):
                    self.assertIn(field["kind"], KINDS)

    def test_each_form_has_exactly_one_positional(self):
        # The interpreter appends positionals last; more than one would make
        # argument order depend on rule order, which is too subtle to allow.
        for form in rules.FORMS:
            positionals = [f for f in form["fields"]
                           if f["kind"] == "positional"]
            with self.subTest(form["id"]):
                self.assertEqual(len(positionals), 1)

    def test_api_keys_do_not_collide(self):
        for form in rules.FORMS:
            keys = [field["api"] for field in form["fields"]]
            with self.subTest(form["id"]):
                self.assertEqual(len(keys), len(set(keys)))

    def test_booleans_emit_at_least_one_flag(self):
        # A boolean with neither an on nor an off flag would silently affect
        # the REST body while leaving the command line wrong.
        for form in rules.FORMS:
            for field in form["fields"]:
                if field["kind"] != "flag":
                    continue
                with self.subTest(form["id"], field=field["field"]):
                    self.assertTrue(field["on"] or field["off"])

    def test_flags_look_like_flags(self):
        for form in rules.FORMS:
            for field in form["fields"]:
                for key in ("flag", "on", "off"):
                    value = field.get(key)
                    if value:
                        with self.subTest(form["id"], flag=value):
                            self.assertTrue(value.startswith("--"))

    def test_tables_are_complete(self):
        for name, table in rules.TABLES.items():
            with self.subTest(name):
                self.assertTrue(table["noun"])
                self.assertTrue(table["path"].startswith("$OS_"))

    def test_endpoints_use_placeholders_not_real_hosts(self):
        # A rendered command must never imply a real endpoint, and must never
        # carry a token.
        for form in rules.FORMS:
            with self.subTest(form["id"]):
                self.assertTrue(form["endpoint"].startswith("$OS_"))
        blob = json.dumps(rules.as_dict())
        self.assertNotIn("http://", blob)
        self.assertNotIn("https://", blob)

    def test_serialises_to_json(self):
        blob = json.dumps(rules.as_dict())
        restored = json.loads(blob)
        self.assertEqual(
            [f["id"] for f in restored["forms"]],
            [f["id"] for f in rules.FORMS])
        self.assertEqual(set(restored["tables"]), set(rules.TABLES))


class TestInterpreter(unittest.TestCase):
    """The JavaScript turns the real rule set into the expected commands."""

    def test_interpreter_matches_the_rules(self):
        if not _have("node"):
            self.skipTest("node not available")
        with tempfile.NamedTemporaryFile(
                "w", suffix=".json", delete=False) as handle:
            json.dump(rules.as_dict(), handle)
            path = handle.name
        try:
            result = subprocess.run(
                ["node", os.path.join(HERE, "test_interpreter.js"), path],
                capture_output=True, text=True)
            sys.stdout.write(result.stdout)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        finally:
            os.unlink(path)


class TestAgainstHorizon(unittest.TestCase):
    """The rules still describe the Horizon forms they target.

    This is the check that a JavaScript-only implementation could not make:
    if a future Horizon release renames a field, the rule silently stops
    contributing a flag. Here it fails loudly instead.
    """

    def setUp(self):
        try:
            import openstack_dashboard  # noqa: F401
        except ImportError:
            self.skipTest(
                "Horizon is not on this interpreter's path; see the module "
                "docstring for the command that includes this layer")
        try:
            import django
            django.setup()
        except Exception as exc:  # noqa: BLE001 - misconfiguration, not a bug
            self.skipTest("Django is not configured here (%s)" % exc)

    def test_no_rule_references_a_missing_field(self):
        self.assertEqual(rules.validate(), [])

    def test_validate_detects_a_renamed_field(self):
        # Proves the check is not passing vacuously: stage the exact failure a
        # Horizon rename would cause and confirm it is reported.
        form = dict(rules.FORMS[0])
        form["fields"] = [rules.opt("memory_gb", "--ram", api="ram")]
        original = rules.FORMS
        rules.FORMS = [form]
        try:
            problems = rules.validate()
        finally:
            rules.FORMS = original
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("memory_gb", problems[0])
        # And the real rule set is still clean afterwards.
        self.assertEqual(rules.validate(), [])

    def test_uncovered_fields_are_reported_for_review(self):
        # Not a failure: plenty of fields are deliberately unmapped. Printed
        # so an upgrade review can see what a rule is choosing to ignore.
        report = rules.uncovered()
        for form_id, fields in sorted(report.items()):
            print("  note: %s does not map %s" % (form_id, ", ".join(fields)))


def _have(program):
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        candidate = os.path.join(directory, program)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return True
    return False


if __name__ == "__main__":
    unittest.main(verbosity=2)
