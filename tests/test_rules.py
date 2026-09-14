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

"""Tests for Astrolabe.

Four layers, in increasing order of what they need to be present:

1. The rules are well formed and internally consistent. Needs nothing.
2. The interpreter turns them into the expected commands, and the session
   store behaves. Needs nothing.
3. The middleware records the right things, and only for the right people.
   Needs Django, but not Horizon.
4. The rules still match the Horizon forms they target. Needs a Horizon
   checkout; skipped with a note when one is not importable.

Run it directly (``python3 tests/test_rules.py``) or under pytest. To include
layer 4, run it with the interpreter that has Horizon on its path::

    cd ../horizon
    DJANGO_SETTINGS_MODULE=openstack_dashboard.test.settings PYTHONPATH=. \\
        ./.tox/runserver/bin/python ../astrolabe/tests/test_rules.py
"""

import json
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

from astrolabe import rules  # noqa: E402
from astrolabe import store  # noqa: E402
from astrolabe import translate  # noqa: E402

KINDS = {"value", "flag", "multi", "positional"}


class Post(dict):
    """The bit of Django's QueryDict interface that read_fields uses."""

    def lists(self):
        for name, value in self.items():
            yield name, value if isinstance(value, list) else [value]


class Session(dict):
    """The bit of Django's SessionBase contract the store relies on.

    SessionBase marks itself dirty on ``__setitem__`` and ``pop``, and Django
    saves it only when that flag is set. Mirroring it here means the store
    cannot quietly start relying on in-place mutation, which would be saved in
    testing and lost in production.
    """

    modified = False

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        self.modified = True

    def pop(self, key, default=None):
        self.modified = self.modified or key in self
        return super().pop(key, default)


# --------------------------------------------------------- 1. the rule table


class TestRuleSet(unittest.TestCase):
    """The rules are well formed, with no Horizon required."""

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
        blob = json.dumps({"forms": rules.FORMS, "tables": rules.TABLES})
        self.assertNotIn("http://", blob)
        self.assertNotIn("https://", blob)


# ------------------------------------------- 2. the interpreter and the store


class TestReadFields(unittest.TestCase):

    def test_secret_looking_fields_are_never_read(self):
        fields = translate.read_fields(Post({
            "csrfmiddlewaretoken": "nope",
            "admin_password": "hunter2",
            "secret_key": "nope",
            "api_token": "nope",
            "adminPass": "nope",
            "name": "keep-me",
        }))
        self.assertEqual(list(fields), ["name"])

    def test_fields_that_merely_contain_a_secret_word_are_kept(self):
        fields = translate.read_fields(Post({
            "passthrough": "a", "monkey": "b", "keystone_url": "c",
            "author": "d", "bypass_check": "e",
        }))
        self.assertEqual(sorted(fields), ["author", "bypass_check",
                                          "keystone_url", "monkey",
                                          "passthrough"])

    def test_repeated_fields_become_lists(self):
        fields = translate.read_fields(Post({
            "object_ids": ["n1", "n2"], "action": "networks__delete",
        }))
        self.assertEqual(fields["object_ids"], ["n1", "n2"])
        self.assertEqual(fields["action"], "networks__delete")

    def test_a_plain_dict_works_too(self):
        # Not every caller has a QueryDict; the fallback must not silently
        # produce nothing.
        self.assertEqual(translate.read_fields({"name": "x"}), {"name": "x"})


class TestTranslate(unittest.TestCase):
    """Rules plus submitted fields in, CLI and REST out."""

    def test_flavor_create_renders_every_flag(self):
        out = translate.translate("/admin/flavors/create/", {
            "name": "m1.small", "flavor_id": "small-1", "vcpus": "1",
            "memory_mb": "2048", "disk_gb": "20", "eph_gb": "5",
            "swap_mb": "512",
        })
        self.assertEqual(
            out["cli"],
            "openstack flavor create --id small-1 --vcpus 1 --ram 2048 "
            "--disk 20 --ephemeral 5 --swap 512 m1.small")
        self.assertEqual(out["calls"][0]["body"]["flavor"], {
            "id": "small-1", "vcpus": 1, "ram": 2048, "disk": 20,
            "OS-FLV-EXT-DATA:ephemeral": 5, "swap": 512, "name": "m1.small",
        })
        self.assertEqual(out["calls"][0]["method"], "POST")
        self.assertEqual(out["title"], "Create flavor m1.small")

    def test_an_auto_flavor_id_reaches_neither_the_command_nor_the_body(self):
        # Horizon's form defaults to "auto", and novaclient turns that into an
        # omitted id. Passing it through would send a flavor literally called
        # "auto" and the command would fail the second time it was run.
        out = translate.translate("/admin/flavors/create/", {
            "name": "m1.small", "flavor_id": "auto", "vcpus": "1",
            "memory_mb": "2048", "disk_gb": "20",
        })
        self.assertNotIn("--id", out["cli"])
        self.assertNotIn("id", out["calls"][0]["body"]["flavor"])

    def test_flavor_create_omits_zero_ephemeral_and_swap(self):
        out = translate.translate("/admin/flavors/create/", {
            "name": "tiny", "flavor_id": "auto", "vcpus": "1",
            "memory_mb": "512", "disk_gb": "1", "eph_gb": "0", "swap_mb": "0",
        })
        self.assertNotIn("--ephemeral", out["cli"])
        self.assertNotIn("--swap", out["cli"])
        # Zero is still meaningful in the API body, unlike on the command line.
        body = out["calls"][0]["body"]["flavor"]
        self.assertEqual(body["OS-FLV-EXT-DATA:ephemeral"], 0)
        self.assertEqual(body["swap"], 0)

    def test_names_needing_quotes_are_shell_escaped(self):
        out = translate.translate("/admin/flavors/create/", {
            "name": "it's big", "flavor_id": "", "vcpus": "8",
            "memory_mb": "16384", "disk_gb": "100",
        })
        self.assertTrue(out["cli"].endswith("'it'\\''s big'"), out["cli"])
        self.assertNotIn("--id", out["cli"])
        self.assertNotIn("id", out["calls"][0]["body"]["flavor"])

    def test_volume_type_defaults_to_public(self):
        out = translate.translate("/admin/volume_types/create_type", {
            "name": "ssd", "vol_type_description": "Fast disks",
            "is_public": "on",
        })
        self.assertEqual(
            out["cli"],
            "openstack volume type create --description 'Fast disks' ssd")
        self.assertIs(
            out["calls"][0]["body"]["volume_type"][
                "os-volume-type-access:is_public"], True)

    def test_volume_type_without_the_checkbox_is_private(self):
        out = translate.translate("/admin/volume_types/create_type",
                                  {"name": "ssd"})
        self.assertEqual(out["cli"],
                         "openstack volume type create --private ssd")
        self.assertIs(
            out["calls"][0]["body"]["volume_type"][
                "os-volume-type-access:is_public"], False)

    def test_network_create_maps_provider_attributes(self):
        out = translate.translate("/admin/networks/create/", {
            "name": "provider1", "tenant_id": "abc123", "network_type": "vlan",
            "physical_network": "physnet1", "segmentation_id": "101",
            "mtu": "9000", "admin_state": "on", "shared": "on",
            "external": "on", "az_hints": ["nova", "az2"],
        })
        self.assertEqual(
            out["cli"],
            "openstack network create --project abc123 "
            "--provider-network-type vlan "
            "--provider-physical-network physnet1 --provider-segment 101 "
            "--mtu 9000 --share --external --availability-zone-hint nova "
            "--availability-zone-hint az2 provider1")
        body = out["calls"][0]["body"]["network"]
        self.assertEqual(body["provider:segmentation_id"], 101)
        self.assertIs(body["admin_state_up"], True)
        self.assertEqual(body["availability_zone_hints"], ["nova", "az2"])

    def test_unchecked_admin_state_becomes_disable(self):
        out = translate.translate("/admin/networks/create/", {
            "name": "down", "tenant_id": "abc123", "network_type": "geneve",
        })
        self.assertIn("--disable", out["cli"])
        self.assertNotIn("--share", out["cli"])
        self.assertNotIn("--external", out["cli"])
        body = out["calls"][0]["body"]["network"]
        self.assertIs(body["admin_state_up"], False)
        self.assertNotIn("availability_zone_hints", body)

    def test_row_delete_uses_the_id_in_the_action_field(self):
        out = translate.translate("/admin/flavors/",
                                  {"action": "flavors__delete__abc-1"})
        self.assertEqual(out["cli"], "openstack flavor delete abc-1")
        self.assertEqual(len(out["calls"]), 1)
        self.assertEqual(out["calls"][0]["method"], "DELETE")
        self.assertTrue(out["calls"][0]["url"].endswith("/flavors/abc-1"))

    def test_batch_delete_expands_object_ids(self):
        out = translate.translate("/admin/networks/", {
            "action": "networks__delete", "object_ids": ["n1", "n2"],
        })
        self.assertEqual(out["cli"], "openstack network delete n1 n2")
        self.assertEqual(len(out["calls"]), 2)
        self.assertIn("(2)", out["title"])

    def test_unknown_tables_and_actions_are_ignored(self):
        self.assertIsNone(
            translate.translate("/admin/images/",
                                {"action": "images__delete__x"}),
            "a table with no mapping must not be recorded")
        self.assertIsNone(
            translate.translate("/admin/flavors/",
                                {"action": "flavors__update__x"}),
            "a non-delete action must not match the delete rule")
        self.assertIsNone(
            translate.translate("/project/instances/", {"name": "vm1"}),
            "an unmapped form must not be recorded")

    def test_a_batch_delete_with_nothing_selected_is_ignored(self):
        self.assertIsNone(
            translate.translate("/admin/networks/",
                                {"action": "networks__delete"}))


class TestRendering(unittest.TestCase):

    def test_curl_never_contains_a_real_token(self):
        out = translate.translate("/admin/flavors/create/", {
            "name": "x", "vcpus": "1", "memory_mb": "1", "disk_gb": "1",
        })
        curl = translate.curl_for(out["calls"][0])
        self.assertIn('X-Auth-Token: $OS_TOKEN', curl)
        self.assertIn('$OS_COMPUTE_API/flavors', curl)
        self.assertIn('-d \'{"flavor"', curl)

    def test_curl_omits_a_body_for_deletes(self):
        out = translate.translate("/admin/flavors/",
                                  {"action": "flavors__delete__z"})
        curl = translate.curl_for(out["calls"][0])
        self.assertNotIn(" -d ", curl)
        self.assertFalse(curl.rstrip().endswith("\\"),
                         "no dangling continuation: " + curl)

    def test_script_runs_oldest_first_and_flags_rejections(self):
        # The panel shows newest first; a script has to run in the order the
        # operator worked, or dependent resources come out backwards.
        entries = [
            {"title": "Create network b", "cli": "openstack network create b",
             "ok": False},
            {"title": "Create network a", "cli": "openstack network create a",
             "ok": True},
        ]
        text = translate.script_for(entries)
        self.assertTrue(text.startswith("#!/bin/sh"))
        self.assertLess(text.index("create a"), text.index("create b"))
        self.assertIn("# NOTE: this action was rejected", text)
        self.assertEqual(text.count("# NOTE:"), 1)


class TestStore(unittest.TestCase):
    """Entries survive a round trip and stay inside the cap."""

    def test_entries_come_back_newest_first(self):
        session = Session()
        store.add(session, {"title": "one"}, limit=10)
        store.add(session, {"title": "two"}, limit=10)
        self.assertEqual([e["title"] for e in store.load(session)],
                         ["two", "one"])
        self.assertTrue(session.modified)

    def test_the_cap_discards_the_oldest(self):
        session = Session()
        for n in range(5):
            store.add(session, {"title": n}, limit=3)
        self.assertEqual([e["title"] for e in store.load(session)],
                         [4, 3, 2])

    def test_clear_empties_the_log(self):
        session = Session()
        store.add(session, {"title": "one"}, limit=10)
        store.clear(session)
        self.assertEqual(store.load(session), [])

    def test_a_junk_session_value_does_not_crash_the_panel(self):
        self.assertEqual(store.load(Session({store.SESSION_KEY: "junk"})), [])

    def test_entries_are_json_serialisable(self):
        # Django's default session serialiser is JSON, so an entry carrying
        # anything else would fail at save time, inside the operator's action.
        built = translate.translate("/admin/flavors/create/", {
            "name": "m1.small", "vcpus": "1", "memory_mb": "2048",
            "disk_gb": "20",
        })
        built["at"] = 1757000000.0
        built["ok"] = True
        self.assertEqual(json.loads(json.dumps(built)), built)


# ------------------------------------------------------------ 3. the recorder


class TestMiddleware(unittest.TestCase):
    """The right submissions are recorded, and only for admins."""

    def setUp(self):
        try:
            from django.conf import settings
        except ImportError:
            self.skipTest("Django is not on this interpreter's path")
        if not settings.configured:
            settings.configure(DEBUG=True)
        from astrolabe import middleware
        self.middleware = middleware

    # Minimal stand-ins: the middleware only ever touches these attributes,
    # and building them by hand keeps this layer free of Horizon settings.
    class _User(object):
        is_authenticated = True

        def __init__(self, is_superuser=True):
            self.is_superuser = is_superuser

    class _Request(object):
        def __init__(self, path, post, user, method="POST"):
            self.path = path
            self.POST = Post(post)
            self.user = user
            self.method = method
            self.session = Session()

    class _Response(object):
        def __init__(self, status_code=302):
            self.status_code = status_code

    def _run(self, request, response=None):
        response = response or self._Response()
        handler = self.middleware.AstrolabeMiddleware(lambda r: response)
        returned = handler(request)
        self.assertIs(returned, response, "the response must pass through")
        return store.load(request.session)

    def test_an_admin_create_is_recorded(self):
        request = self._Request("/admin/flavors/create/", {
            "name": "m1.small", "vcpus": "1", "memory_mb": "2048",
            "disk_gb": "20", "csrfmiddlewaretoken": "nope",
        }, self._User())
        entries = self._run(request)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["title"], "Create flavor m1.small")
        self.assertTrue(entries[0]["ok"])
        self.assertNotIn("nope", json.dumps(entries[0]))

    def test_a_non_admin_is_never_recorded(self):
        request = self._Request("/admin/flavors/create/", {
            "name": "m1.small", "vcpus": "1", "memory_mb": "1",
            "disk_gb": "1",
        }, self._User(is_superuser=False))
        self.assertEqual(self._run(request), [])

    def test_an_anonymous_request_is_never_recorded(self):
        user = self._User()
        user.is_authenticated = False
        request = self._Request("/admin/flavors/create/", {"name": "x"}, user)
        self.assertEqual(self._run(request), [])

    def test_get_requests_are_ignored(self):
        request = self._Request("/admin/flavors/create/", {},
                                self._User(), method="GET")
        self.assertEqual(self._run(request), [])

    def test_an_unmatched_post_is_never_stored(self):
        request = self._Request("/project/instances/", {"name": "vm1"},
                                self._User())
        self.assertEqual(self._run(request), [])

    def test_a_redisplayed_form_is_recorded_as_rejected(self):
        # Horizon re-renders the modal with errors (200) when validation fails
        # *or* when the API call raises, so this covers both.
        request = self._Request("/admin/flavors/create/", {
            "name": "dupe", "vcpus": "1", "memory_mb": "1", "disk_gb": "1",
        }, self._User())
        entries = self._run(request, self._Response(status_code=200))
        self.assertFalse(entries[0]["ok"])

    def test_an_error_message_marks_a_redirect_as_rejected(self):
        # A table action that the API refuses still redirects, so the queued
        # message is the only evidence that it failed.
        request = self._Request("/admin/networks/",
                                {"action": "networks__delete__n1"},
                                self._User())
        request._messages = _MessageStorage([_Message(40, "Unable to delete")])
        entries = self._run(request)
        self.assertEqual(len(entries), 1)
        self.assertFalse(entries[0]["ok"])

    def test_an_info_message_leaves_a_redirect_successful(self):
        request = self._Request("/admin/networks/",
                                {"action": "networks__delete__n1"},
                                self._User())
        request._messages = _MessageStorage([_Message(25, "Deleted network")])
        self.assertTrue(self._run(request)[0]["ok"])

    def test_a_recording_failure_never_breaks_the_operators_action(self):
        request = self._Request("/admin/flavors/create/", {
            "name": "x", "vcpus": "1", "memory_mb": "1", "disk_gb": "1",
        }, self._User())
        # A session that refuses to be written is the realistic version of
        # "something in here went wrong".
        request.session = _BrokenSession()
        response = self._Response()
        handler = self.middleware.AstrolabeMiddleware(lambda r: response)
        with self.assertLogs("astrolabe.middleware", level="WARNING"):
            self.assertIs(handler(request), response)

    def test_the_middleware_can_be_switched_off(self):
        from django.conf import settings
        from django.core.exceptions import MiddlewareNotUsed
        settings.ASTROLABE_ENABLED = False
        try:
            with self.assertRaises(MiddlewareNotUsed):
                self.middleware.AstrolabeMiddleware(lambda r: None)
        finally:
            del settings.ASTROLABE_ENABLED


class _Message(object):
    def __init__(self, level, text):
        self.level = level
        self.message = text


class _MessageStorage(object):
    def __init__(self, queued):
        self._queued_messages = queued


class _BrokenSession(Session):
    def __setitem__(self, key, value):
        raise RuntimeError("session backend is down")


# ---------------------------------------------------------- 4. against Horizon


class TestAgainstHorizon(unittest.TestCase):
    """The rules still describe the Horizon forms they target.

    This is the check no browser-side implementation could make: if a future
    Horizon release renames a field, the rule silently stops contributing a
    flag. Here it fails loudly instead.
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
