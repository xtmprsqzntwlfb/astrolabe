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

Five layers, in increasing order of what they need to be present:

1. The rules are well formed and internally consistent. Needs nothing.
2. The interpreter turns them into the expected commands, and the session
   store behaves. Needs nothing.
3. The middleware records the right things and only for the right people,
   and the panel shows back what it recorded. Needs Django, but not Horizon:
   the one class the views borrow from Horizon is stood in for when there is
   no Horizon to borrow it from.
4. The rules still match the Horizon forms they target. Needs a Horizon
   checkout; skipped with a note when one is not importable.
5. The commands the rules render still exist, with the flags they use. Needs
   python-openstackclient; skipped with a note when it is absent.

Layers 4 and 5 watch the two upstreams a rule straddles, and they fail
differently. Horizon drift stops a rule firing; openstackclient drift leaves
it firing and rendering a command that no longer works.

Run it directly (``python3 tests/test_rules.py``) or under pytest. To include
layer 4, run it with the interpreter that has Horizon on its path::

    cd ../horizon
    DJANGO_SETTINGS_MODULE=openstack_dashboard.test.settings PYTHONPATH=. \\
        ./.tox/runserver/bin/python ../astrolabe/tests/test_rules.py

Layer 5 wants an interpreter with the CLI on it, which Horizon's does not
have::

    python3 -m venv /tmp/osc
    /tmp/osc/bin/pip install python-openstackclient
    /tmp/osc/bin/python tests/test_rules.py
"""

import json
import os
import re
import sys
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

from astrolabe import rules  # noqa: E402
from astrolabe import store  # noqa: E402
from astrolabe import translate  # noqa: E402

KINDS = {"value", "flag", "multi", "positional", "redacted",
         "choice", "target", "item", "lines", "pairs", "parent"}


def every_spec():
    """(rule, spec) for every rule and every follow-up step it carries.

    A step renders its own command and its own body, so nearly everything
    the rule table has to be right about, a step has to be right about too.
    Sweeping only the rules would leave the steps unchecked.
    """
    for form in rules.FORMS:
        for spec in rules.specs(form):
            yield form, spec


def every_field():
    """(rule, spec, field) for every field in the table, steps included."""
    for form, spec in every_spec():
        for field in spec["fields"]:
            yield form, spec, field


def cli(out):
    """The one command an entry renders, for the rules that render one.

    Most rules are one form, one command. Asserting that here rather than
    indexing blindly means a rule that quietly grows a second command shows
    up in the test that was written when it had one.
    """
    commands = out["commands"]
    if len(commands) != 1:
        raise AssertionError(
            "expected one command, got %d: %r" % (len(commands), commands))
    return commands[0]


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

    def test_every_rule_names_the_panels_it_serves(self):
        # Without a route a rule's url pattern is unfalsifiable: nothing can
        # tell whether it still points at a page that exists.
        for form in rules.FORMS:
            with self.subTest(form["id"]):
                self.assertTrue(form["routes"])
                for name in form["routes"]:
                    self.assertTrue(name.startswith("horizon:"), name)

    def test_every_table_names_the_class_it_came_from(self):
        for key, table in rules.TABLES.items():
            with self.subTest(key):
                self.assertIn(":", table["table"])

    def test_field_kinds_are_known(self):
        for form, _spec, field in every_field():
            with self.subTest(form["id"], field=field["field"]):
                self.assertIn(field["kind"], KINDS)

    def test_each_form_has_exactly_one_trailing_argument(self):
        # The interpreter appends these last; more than one would make
        # argument order depend on rule order, which is too subtle to allow.
        # A create names its subject (positional), an edit names the resource
        # it is editing (target), and no rule does both.
        #
        # Follow-up steps are the exception, and have a test of their own:
        # "aggregate add host" takes two, and which is which is the order
        # they are written in.
        for form in rules.FORMS:
            trailing = [f for f in form["fields"]
                        if f["kind"] in ("positional", "target")]
            with self.subTest(form["id"]):
                self.assertEqual(len(trailing), 1)

    def test_a_rule_captures_an_id_exactly_when_it_needs_one(self):
        # The id reaches the command through a target field and the REST path
        # through the endpoint's {id}, and both read the same capture group.
        # Half of that arrangement is worse than none: an endpoint left with
        # a literal "{id}" in it, or a command missing its resource.
        for form in rules.FORMS:
            captures = "(?P<id>" in form["url"]
            targets = any(f["kind"] == "target" for f in form["fields"])
            in_endpoint = "{id}" in form["endpoint"]
            with self.subTest(form["id"]):
                self.assertEqual(captures, targets)
                self.assertEqual(captures, in_endpoint)

    def test_targets_carry_no_api_key(self):
        # An id says which resource is being changed, not what it should
        # become. Neutron rejects it as an attribute of the body.
        for form, _spec, field in every_field():
            if field["kind"] != "target":
                continue
            with self.subTest(form["id"]):
                self.assertNotIn("api", field)

    def test_api_keys_do_not_collide(self):
        # Per spec rather than per rule, because a step builds a body of its
        # own. Redacted fields and command-only positionals have no api key
        # at all: they contribute nothing to any body, on purpose.
        #
        # Two fields may share a key when they cannot both fire, which is
        # how a subnet's gateway is written either from the box the operator
        # typed in or from the one that disables it. The exclusion has to be
        # declared, not assumed: one of them says "unless" of the other.
        for form, spec in every_spec():
            seen = {}
            for field in spec["fields"]:
                key = field.get("api")
                if not key:
                    continue
                with self.subTest(form["id"], api=key):
                    for other in seen.get(key, []):
                        self.assertTrue(
                            field["field"] in
                            (other.get("unless") or []) or
                            other["field"] in (field.get("unless") or []),
                            "%r is written by two fields that can both "
                            "fire" % key)
                seen.setdefault(key, []).append(field)

    def test_no_api_key_is_nested_under_another(self):
        # _put walks a dotted path with setdefault, so a rule writing both
        # "gateway" and "gateway.id" would hit a string where it wanted a
        # dict and raise. Catch that here rather than on a live submission.
        for form, spec in every_spec():
            keys = {field["api"] for field in spec["fields"]
                    if field.get("api")}
            for key in keys:
                parents = {".".join(key.split(".")[:n])
                           for n in range(1, key.count(".") + 1)}
                with self.subTest(form["id"], key=key):
                    self.assertFalse(parents & keys)

    def test_choices_map_to_distinct_flags(self):
        # Two options sharing a flag would render the same command for two
        # different bodies, which is exactly the confusion Astrolabe exists
        # to remove.
        for form, _spec, field in every_field():
            if field["kind"] != "choice":
                continue
            flags = [one["flag"] for one in field["choices"].values()
                     if one["flag"]]
            with self.subTest(form["id"], field=field["field"]):
                self.assertTrue(flags)
                self.assertEqual(len(flags), len(set(flags)))

    def test_redacted_fields_carry_no_api_key(self):
        for form, _spec, field in every_field():
            if field["kind"] != "redacted":
                continue
            with self.subTest(form["id"], field=field["field"]):
                self.assertNotIn("api", field)
                # The whole point is that the name is one read_fields
                # refuses to read.
                self.assertTrue(translate.is_secret(field["field"]))

    def test_booleans_emit_at_least_one_flag(self):
        # A boolean with neither an on nor an off flag would silently affect
        # the REST body while leaving the command line wrong.
        for form, _spec, field in every_field():
            if field["kind"] != "flag":
                continue
            with self.subTest(form["id"], field=field["field"]):
                self.assertTrue(field["on"] or field["off"])

    def test_flags_look_like_flags(self):
        for form, _spec, field in every_field():
            for key in ("flag", "on", "off"):
                value = field.get(key)
                if value:
                    with self.subTest(form["id"], flag=value):
                        self.assertTrue(value.startswith("--"))

    def test_only_an_edit_rule_clears_a_field(self):
        # On a create an empty box means nothing was supplied, so there is
        # nothing to clear and no earlier value to clear it from. A rule
        # edits something exactly when it carries a target, and a follow-up
        # step never clears anything: it is a call Horizon made, not a field
        # the operator emptied.
        for form, spec, field in every_field():
            if not field.get("clearable"):
                continue
            edits = any(f["kind"] == "target" for f in form["fields"])
            with self.subTest(form["id"], field=field["field"]):
                self.assertTrue(edits)
                self.assertIs(spec, form)

    def test_a_clearable_field_holds_text_and_names_no_sentinel(self):
        # An empty string casts to nothing as an int, and absent_when is a
        # second, different way of spelling "not supplied" -- a field using
        # both leaves it ambiguous which one an empty box meant.
        for form, _spec, field in every_field():
            if not field.get("clearable"):
                continue
            with self.subTest(form["id"], field=field["field"]):
                self.assertEqual(field["cast"], "str")
                self.assertIsNone(field["absentWhen"])
                self.assertIsNone(field["omitWhen"])

    def test_every_follow_up_step_is_complete(self):
        # A step is a rule minus what places it, so it needs the same keys
        # bar those. envelope may be None, which is how a rule says the call
        # carries no body at all.
        steps = [(form, spec) for form, spec in every_spec()
                 if spec is not form]
        self.assertTrue(steps, "nothing here exercises follow-up steps")
        for form, step in steps:
            with self.subTest(form["id"],
                              command=" ".join(step["command"])):
                for key in ("method", "endpoint", "envelope", "command",
                            "fields"):
                    self.assertIn(key, step)
                self.assertTrue(step["fields"])
                self.assertTrue(step["command"])
                # And none of what places a rule, which would read as though
                # a step could be matched on its own. It cannot.
                for key in ("url", "routes", "title", "id"):
                    self.assertNotIn(key, step)

    def test_a_step_ends_its_command_with_something(self):
        # Unlike a rule a step may take more than one trailing argument --
        # "aggregate add host" takes the aggregate and the host -- but it
        # must take at least one, or the command names no resource.
        for form, spec in every_spec():
            if spec is form:
                continue
            with self.subTest(form["id"],
                              command=" ".join(spec["command"])):
                self.assertTrue(rules.takes_positional(spec))

    def test_a_step_naming_a_new_id_has_one_to_name(self):
        # The id of the thing the first call made reaches a later call two
        # ways: {new} in the path, for a call made *against* it, and a
        # parent field in the body, for a resource made *inside* it. Either
        # way it resolves from the rule's creates, and without that the
        # endpoint renders with a literal "{new}" still in it.
        for form, spec in every_spec():
            needs = ("{new}" in spec["endpoint"] or
                     any(f["kind"] == "parent" for f in spec["fields"]))
            if not needs:
                continue
            with self.subTest(form["id"], endpoint=spec["endpoint"]):
                self.assertTrue(form.get("creates"))
                self.assertTrue(form["creates"].startswith("$NEW_"))

    def test_creates_is_only_on_rules_that_follow_up(self):
        # It exists to be substituted into a later call. A rule making one
        # call has nothing to substitute it into, and declaring it there
        # would be a promise the panel footer could not keep.
        for form in rules.FORMS:
            if not form.get("creates"):
                continue
            with self.subTest(form["id"]):
                self.assertTrue(form.get("then"))
                self.assertTrue(any(
                    "{new}" in step["endpoint"] or
                    any(f["kind"] == "parent" for f in step["fields"])
                    for step in form["then"]))

    def test_a_repeating_step_names_the_form_its_list_lives_on(self):
        # per names a field built at runtime, which base_fields cannot
        # confirm. validate() re-derives it from the action class instead,
        # and needs to be told which class that is.
        for form, spec in every_spec():
            if not spec.get("per"):
                continue
            with self.subTest(form["id"], per=spec["per"]):
                self.assertIn(":", spec.get("form") or "")
                self.assertTrue(any(field["kind"] == "item"
                                    for field in spec["fields"]))

    def test_the_flag_list_finds_every_flag_in_the_table(self):
        # Layer 5 checks whatever flags() returns, so a field kind whose flag
        # key flags() did not know about would be checked by nobody, and the
        # layer would stay green while covering less than it claims. Derived
        # a second way here, straight out of the raw table, so the two have
        # to agree.
        listed = {flag for form in rules.FORMS for spec in rules.specs(form)
                  for flag in rules.flags(spec)}
        written = set(re.findall(r'"(--[a-z0-9-]+)"', json.dumps(rules.FORMS)))
        self.assertEqual(sorted(written - listed), [])
        self.assertTrue(listed)

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
            cli(out),
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
        self.assertNotIn("--id", cli(out))
        self.assertNotIn("id", out["calls"][0]["body"]["flavor"])

    def test_flavor_create_omits_zero_ephemeral_and_swap(self):
        out = translate.translate("/admin/flavors/create/", {
            "name": "tiny", "flavor_id": "auto", "vcpus": "1",
            "memory_mb": "512", "disk_gb": "1", "eph_gb": "0", "swap_mb": "0",
        })
        self.assertNotIn("--ephemeral", cli(out))
        self.assertNotIn("--swap", cli(out))
        # Zero is still meaningful in the API body, unlike on the command line.
        body = out["calls"][0]["body"]["flavor"]
        self.assertEqual(body["OS-FLV-EXT-DATA:ephemeral"], 0)
        self.assertEqual(body["swap"], 0)

    def test_names_needing_quotes_are_shell_escaped(self):
        out = translate.translate("/admin/flavors/create/", {
            "name": "it's big", "flavor_id": "", "vcpus": "8",
            "memory_mb": "16384", "disk_gb": "100",
        })
        self.assertTrue(cli(out).endswith("'it'\\''s big'"), cli(out))
        self.assertNotIn("--id", cli(out))
        self.assertNotIn("id", out["calls"][0]["body"]["flavor"])

    def test_volume_type_defaults_to_public(self):
        out = translate.translate("/admin/volume_types/create_type", {
            "name": "ssd", "vol_type_description": "Fast disks",
            "is_public": "on",
        })
        self.assertEqual(
            cli(out),
            "openstack volume type create --description 'Fast disks' ssd")
        self.assertIs(
            out["calls"][0]["body"]["volume_type"][
                "os-volume-type-access:is_public"], True)

    def test_volume_type_without_the_checkbox_is_private(self):
        out = translate.translate("/admin/volume_types/create_type",
                                  {"name": "ssd"})
        self.assertEqual(cli(out),
                         "openstack volume type create --private ssd")
        self.assertIs(
            out["calls"][0]["body"]["volume_type"][
                "os-volume-type-access:is_public"], False)

    def test_volume_type_update_uses_cinders_other_spelling(self):
        # The create call takes "os-volume-type-access:is_public" and the
        # update call takes a plain "is_public". Getting that backwards is a
        # 400 the operator only finds out about by running the command.
        out = translate.translate("/admin/volume_types/vt-1/update_type/", {
            "name": "ssd", "description": "Fast disks", "is_public": "on",
        })
        self.assertEqual(
            cli(out),
            "openstack volume type set --name ssd --description 'Fast disks' "
            "--public vt-1")
        body = out["calls"][0]["body"]["volume_type"]
        self.assertIs(body["is_public"], True)
        self.assertNotIn("os-volume-type-access:is_public", body)
        self.assertEqual(out["calls"][0]["url"], "$OS_VOLUME_API/types/vt-1")

    def test_volume_type_update_does_not_poach_the_encryption_panel(self):
        # update_type_encryption sits beside update_type on the same id, and
        # is a different resource entirely.
        self.assertIsNone(translate.translate(
            "/admin/volume_types/vt-1/update_type_encryption/", {"name": "x"}))

    def test_network_create_maps_provider_attributes(self):
        out = translate.translate("/admin/networks/create/", {
            "name": "provider1", "tenant_id": "abc123", "network_type": "vlan",
            "physical_network": "physnet1", "segmentation_id": "101",
            "mtu": "9000", "admin_state": "on", "shared": "on",
            "external": "on", "az_hints": ["nova", "az2"],
        })
        self.assertEqual(
            cli(out),
            "openstack network create --project abc123 "
            "--provider-network-type vlan "
            "--provider-physical-network physnet1 --provider-segment 101 "
            "--mtu 9000 --share --external --availability-zone-hint nova "
            "--availability-zone-hint az2 provider1")
        body = out["calls"][0]["body"]["network"]
        self.assertEqual(body["provider:segmentation_id"], 101)
        self.assertIs(body["admin_state_up"], True)
        self.assertEqual(body["availability_zone_hints"], ["nova", "az2"])

    def test_network_update_takes_its_subject_from_the_url(self):
        out = translate.translate(
            "/admin/networks/9f2c7b1e-aaaa-bbbb-cccc-0123456789ab/update/", {
                "name": "renamed", "admin_state": "on", "shared": "on",
                "external": "on",
            })
        self.assertEqual(
            cli(out),
            "openstack network set --name renamed --enable --share "
            "--external 9f2c7b1e-aaaa-bbbb-cccc-0123456789ab")
        self.assertEqual(out["title"],
                         "Update network 9f2c7b1e-aaaa-bbbb-cccc-0123456789ab")

    def test_network_update_puts_the_id_in_the_path_not_the_body(self):
        out = translate.translate("/admin/networks/net-7/update/",
                                  {"name": "renamed", "admin_state": "on"})
        call = out["calls"][0]
        self.assertEqual(call["method"], "PUT")
        self.assertEqual(call["url"], "$OS_NETWORK_API/networks/net-7")
        body = call["body"]["network"]
        self.assertEqual(body["name"], "renamed")
        self.assertNotIn("id", body)
        self.assertNotIn("{id}", json.dumps(out))

    def test_an_unticked_box_on_an_edit_is_an_explicit_off(self):
        # UpdateNetwork.handle sends all four keys every time, so unticking
        # "Shared" on an edit means unshare. The same empty box on a create
        # only means the operator left the default alone.
        out = translate.translate("/admin/networks/net-7/update/",
                                  {"name": "quiet"})
        self.assertEqual(
            cli(out),
            "openstack network set --name quiet --disable --no-share "
            "--internal net-7")
        body = out["calls"][0]["body"]["network"]
        self.assertIs(body["shared"], False)
        self.assertIs(body["router:external"], False)
        self.assertIs(body["admin_state_up"], False)

    def test_the_update_rule_does_not_poach_nested_update_urls(self):
        # Both networks panels also edit subnets and ports, one path segment
        # deeper. Matching those would build a network command against a
        # subnet id.
        for path in ("/admin/networks/net-7/subnets/sub-1/update",
                     "/admin/networks/net-7/ports/port-1/update",
                     "/project/networks/net-7/subnets/sub-1/update",
                     "/project/networks/net-7/ports/port-1/update"):
            with self.subTest(path):
                self.assertIsNone(translate.translate(path, {"name": "x"}))

    def test_a_project_network_with_a_subnet_is_two_commands(self):
        out = translate.translate("/project/networks/create", {
            "net_name": "web", "admin_state": "on", "shared": "on",
            "mtu": "1450", "with_subnet": "on",
            "subnet_name": "web-v4", "cidr": "192.168.1.0/24",
            "ip_version": "4", "gateway_ip": "192.168.1.1",
            "enable_dhcp": "on",
            "dns_nameservers": "8.8.8.8\n1.1.1.1\n",
            "allocation_pools": "192.168.1.100,192.168.1.120",
            "host_routes": "10.0.0.0/8,192.168.1.254",
        })
        self.assertEqual(out["commands"], [
            "openstack network create --mtu 1450 --share web",
            "openstack subnet create --network web "
            "--subnet-range 192.168.1.0/24 --ip-version 4 "
            "--gateway 192.168.1.1 --dhcp "
            "--dns-nameserver 8.8.8.8 --dns-nameserver 1.1.1.1 "
            "--allocation-pool start=192.168.1.100,end=192.168.1.120 "
            "--host-route destination=10.0.0.0/8,gateway=192.168.1.254 "
            "web-v4",
        ])
        subnet = out["calls"][1]["body"]["subnet"]
        self.assertEqual(subnet["dns_nameservers"], ["8.8.8.8", "1.1.1.1"])
        self.assertEqual(subnet["allocation_pools"],
                         [{"start": "192.168.1.100", "end": "192.168.1.120"}])
        # The CLI calls the second half a gateway and Neutron calls it a
        # nexthop. Same value, two spellings, and the rule knows both.
        self.assertEqual(subnet["host_routes"],
                         [{"destination": "10.0.0.0/8",
                           "nexthop": "192.168.1.254"}])

    def test_the_subnet_belongs_to_a_network_with_no_id_yet(self):
        out = translate.translate("/project/networks/create", {
            "net_name": "web", "with_subnet": "on", "subnet_name": "s",
            "cidr": "10.0.0.0/24", "enable_dhcp": "on",
        })
        # The command names the network the operator just named; only the
        # body needs an id, and there is no id to have.
        self.assertIn("--network web", out["commands"][1])
        self.assertEqual(out["calls"][1]["body"]["subnet"]["network_id"],
                         "$NEW_NETWORK_ID")
        self.assertNotIn("$NEW_", out["commands"][1])

    def test_an_unticked_create_subnet_box_is_one_command(self):
        out = translate.translate("/project/networks/create",
                                  {"net_name": "plain", "admin_state": "on"})
        self.assertEqual(cli(out), "openstack network create plain")
        self.assertEqual(len(out["calls"]), 1)

    def test_disabling_the_gateway_beats_a_value_left_in_the_box(self):
        # "Disable Gateway" hides the gateway input rather than removing it,
        # so a value typed before the box was ticked is still posted. Both
        # fields write the same body key, and without the gate the command
        # would carry two --gateway flags and contradict itself.
        out = translate.translate("/project/networks/create", {
            "net_name": "n", "with_subnet": "on", "subnet_name": "s",
            "cidr": "10.0.0.0/24", "gateway_ip": "10.0.0.1",
            "no_gateway": "on", "enable_dhcp": "on",
        })
        self.assertEqual(out["commands"][1].count("--gateway"), 1)
        self.assertIn("--gateway none", out["commands"][1])
        self.assertIsNone(out["calls"][1]["body"]["subnet"]["gateway_ip"])

    def test_a_prefix_length_needs_a_pool_to_apply_to(self):
        without = translate.translate("/project/networks/create", {
            "net_name": "n", "with_subnet": "on", "subnet_name": "s",
            "prefixlen": "26", "enable_dhcp": "on",
        })
        self.assertNotIn("--prefix-length", without["commands"][1])
        self.assertNotIn("prefixlen", without["calls"][1]["body"]["subnet"])
        with_pool = translate.translate("/project/networks/create", {
            "net_name": "n", "with_subnet": "on", "subnet_name": "s",
            "prefixlen": "26", "subnetpool": "pool-1", "enable_dhcp": "on",
        })
        self.assertIn("--subnet-pool pool-1 --prefix-length 26",
                      with_pool["commands"][1])

    def test_blank_lines_in_a_textarea_are_dropped(self):
        out = translate.translate("/project/networks/create", {
            "net_name": "n", "with_subnet": "on", "subnet_name": "s",
            "dns_nameservers": "\n 8.8.8.8 \n\n  \n9.9.9.9\n",
        })
        self.assertEqual(
            out["calls"][1]["body"]["subnet"]["dns_nameservers"],
            ["8.8.8.8", "9.9.9.9"])

    def test_an_empty_textarea_contributes_nothing(self):
        out = translate.translate("/project/networks/create", {
            "net_name": "n", "with_subnet": "on", "subnet_name": "s",
            "dns_nameservers": "", "allocation_pools": "", "host_routes": "",
        })
        subnet = out["calls"][1]["body"]["subnet"]
        for key in ("dns_nameservers", "allocation_pools", "host_routes"):
            self.assertNotIn(key, subnet)
        self.assertNotIn("--dns-nameserver", out["commands"][1])

    def test_the_two_network_create_panels_stay_apart(self):
        # Different forms, different field names: a submission from one
        # must not be read by the other's rule. net_name against the admin
        # rule would render a nameless network.
        admin = translate.translate("/admin/networks/create/",
                                    {"name": "a", "admin_state": "on"})
        project = translate.translate("/project/networks/create",
                                      {"net_name": "p", "admin_state": "on"})
        self.assertEqual(cli(admin), "openstack network create a")
        self.assertEqual(cli(project), "openstack network create p")

    def test_a_project_network_edit_says_nothing_about_external(self):
        # The reason these are two rules and not one. The project form has
        # no external box, so there is no unticked box to read, and a save
        # from this panel must not claim external routing was turned off.
        out = translate.translate("/project/networks/net-7/update",
                                  {"name": "quiet"})
        self.assertEqual(
            cli(out),
            "openstack network set --name quiet --disable --no-share net-7")
        body = out["calls"][0]["body"]["network"]
        self.assertNotIn("router:external", body)
        self.assertIs(body["shared"], False)

    def test_both_network_edit_panels_render_their_own_fields(self):
        submitted = {"name": "web", "admin_state": "on", "shared": "on"}
        admin = translate.translate("/admin/networks/net-7/update/", submitted)
        project = translate.translate("/project/networks/net-7/update",
                                      submitted)
        # Same panel, same intent, and the admin form carries one more box.
        self.assertEqual(
            cli(admin),
            "openstack network set --name web --enable --share --internal "
            "net-7")
        self.assertEqual(
            cli(project),
            "openstack network set --name web --enable --share net-7")
        for out in (admin, project):
            self.assertEqual(out["calls"][0]["url"],
                             "$OS_NETWORK_API/networks/net-7")

    def test_creating_and_updating_a_network_stay_separate(self):
        created = translate.translate("/admin/networks/create/",
                                      {"name": "n1", "admin_state": "on"})
        self.assertEqual(created["calls"][0]["method"], "POST")
        self.assertIn("network create", cli(created))

    def test_unchecked_admin_state_becomes_disable(self):
        out = translate.translate("/admin/networks/create/", {
            "name": "down", "tenant_id": "abc123", "network_type": "geneve",
        })
        self.assertIn("--disable", cli(out))
        self.assertNotIn("--share", cli(out))
        self.assertNotIn("--external", cli(out))
        body = out["calls"][0]["body"]["network"]
        self.assertIs(body["admin_state_up"], False)
        self.assertNotIn("availability_zone_hints", body)

    def test_aggregate_create_maps_the_availability_zone(self):
        out = translate.translate("/admin/aggregates/create/", {
            "name": "gpu-nodes", "availability_zone": "az-gpu",
        })
        self.assertEqual(
            cli(out), "openstack aggregate create --zone az-gpu gpu-nodes")
        self.assertEqual(out["calls"][0]["body"]["aggregate"],
                         {"availability_zone": "az-gpu", "name": "gpu-nodes"})

    def test_an_aggregate_without_a_zone_omits_the_flag(self):
        out = translate.translate("/admin/aggregates/create/",
                                  {"name": "spare"})
        self.assertEqual(cli(out), "openstack aggregate create spare")

    def test_an_aggregate_with_hosts_is_one_entry_of_several_commands(self):
        # One saved form, four calls, one entry: that is one thing the
        # operator did, and splitting it would lose the order they happen in.
        out = translate.translate("/admin/aggregates/create/", {
            "name": "gpu-nodes", "availability_zone": "az-gpu",
            "add_host_to_aggregate_role_member": ["cmp1", "cmp2", "cmp3"],
        })
        self.assertEqual(out["commands"], [
            "openstack aggregate create --zone az-gpu gpu-nodes",
            "openstack aggregate add host gpu-nodes cmp1",
            "openstack aggregate add host gpu-nodes cmp2",
            "openstack aggregate add host gpu-nodes cmp3",
        ])
        self.assertEqual(out["title"], "Create host aggregate gpu-nodes")
        self.assertEqual(len(out["calls"]), 4)

    def test_the_host_calls_name_an_id_astrolabe_cannot_know(self):
        # Nova wants the aggregate's id, which only exists once the first
        # call has returned, and Astrolabe never sees a response. The
        # command gets by on the name; the REST path says so instead of
        # inventing something.
        out = translate.translate("/admin/aggregates/create/", {
            "name": "gpu-nodes",
            "add_host_to_aggregate_role_member": ["cmp1"],
        })
        self.assertEqual(out["calls"][1], {
            "method": "POST",
            "url": "$OS_COMPUTE_API/os-aggregates/$NEW_AGGREGATE_ID/action",
            "body": {"add_host": {"host": "cmp1"}},
        })
        # The command half needs no such thing, which is the point of it.
        self.assertNotIn("$NEW_", out["commands"][1])

    def test_an_aggregate_with_no_hosts_is_one_command_as_before(self):
        out = translate.translate("/admin/aggregates/create/", {
            "name": "spare", "add_host_to_aggregate_role_member": [],
        })
        self.assertEqual(cli(out), "openstack aggregate create spare")
        self.assertEqual(len(out["calls"]), 1)

    def test_aggregate_update_names_the_aggregate_from_the_url(self):
        out = translate.translate("/admin/aggregates/7/update/", {
            "name": "gpu-nodes", "availability_zone": "az-gpu",
        })
        self.assertEqual(
            cli(out),
            "openstack aggregate set --name gpu-nodes --zone az-gpu 7")
        self.assertEqual(out["calls"][0]["url"],
                         "$OS_COMPUTE_API/os-aggregates/7")
        self.assertEqual(out["calls"][0]["method"], "PUT")

    def test_aggregate_update_does_not_poach_the_hosts_panel(self):
        self.assertIsNone(
            translate.translate("/admin/aggregates/7/manage_hosts/", {}))

    def test_aggregate_deletes_use_horizons_host_aggregates_table(self):
        out = translate.translate("/admin/aggregates/", {
            "action": "host_aggregates__delete__7",
        })
        self.assertEqual(cli(out), "openstack aggregate delete 7")
        self.assertTrue(out["calls"][0]["url"].endswith("/os-aggregates/7"))

    def test_router_create_maps_every_field_it_covers(self):
        out = translate.translate("/admin/routers/create/", {
            "name": "edge-r1", "tenant_id": "p-9", "admin_state_up": "on",
            "external_network": "net-ext", "mode": "distributed",
            "ha": "enabled", "az_hints": ["az1", "az2"],
        })
        self.assertEqual(
            cli(out),
            "openstack router create --project p-9 "
            "--external-gateway net-ext --distributed --ha "
            "--availability-zone-hint az1 --availability-zone-hint az2 "
            "edge-r1")
        self.assertEqual(out["calls"][0]["body"]["router"], {
            "tenant_id": "p-9",
            "admin_state_up": True,
            "external_gateway_info": {"network_id": "net-ext"},
            "distributed": True,
            "ha": True,
            "availability_zone_hints": ["az1", "az2"],
            "name": "edge-r1",
        })

    def test_router_server_defaults_send_no_key_and_no_flag(self):
        # Horizon omits distributed and ha entirely on the sentinel, so a
        # rendered command that named either would be a different request.
        out = translate.translate("/admin/routers/create/", {
            "name": "plain", "mode": "server_default",
            "ha": "server_default", "admin_state_up": "on",
        })
        self.assertEqual(
            cli(out), "openstack router create plain")
        body = out["calls"][0]["body"]["router"]
        self.assertNotIn("distributed", body)
        self.assertNotIn("ha", body)

    def test_router_centralized_and_no_ha_are_not_silence(self):
        # The other half of the sentinel: picking centralized is a real
        # choice and has to reach both the command and the body as False.
        out = translate.translate("/admin/routers/create/", {
            "name": "legacy", "mode": "centralized", "ha": "disabled",
            "admin_state_up": "on",
        })
        self.assertEqual(
            cli(out),
            "openstack router create --centralized --no-ha legacy")
        body = out["calls"][0]["body"]["router"]
        self.assertIs(body["distributed"], False)
        self.assertIs(body["ha"], False)

    def test_a_router_without_a_gateway_grows_no_nested_key(self):
        out = translate.translate("/admin/routers/create/", {
            "name": "internal", "external_network": "",
            "mode": "server_default", "ha": "server_default",
        })
        self.assertEqual(
            cli(out), "openstack router create --disable internal")
        self.assertNotIn("external_gateway_info",
                         out["calls"][0]["body"]["router"])

    def test_routers_are_recorded_from_either_dashboard(self):
        # Astrolabe gates on the operator being an admin, not on the URL
        # starting with /admin/, so an admin creating a router from the
        # project dashboard has to land on the same rule.
        for url in ("/admin/routers/create/", "/project/routers/create/"):
            with self.subTest(url):
                out = translate.translate(url, {"name": "r1"})
                self.assertTrue(cli(out).startswith(
                    "openstack router create"))

    def test_a_project_side_router_carries_no_project_flag(self):
        # Only the admin form offers tenant_id. Its absence must read as
        # "not supplied" rather than as an empty --project.
        out = translate.translate("/project/routers/create/", {
            "name": "mine", "admin_state_up": "on",
        })
        self.assertEqual(cli(out), "openstack router create mine")
        self.assertNotIn("tenant_id", out["calls"][0]["body"]["router"])

    def test_router_update_is_recorded_from_either_dashboard(self):
        for url in ("/admin/routers/r-1/update", "/project/routers/r-1/update"):
            with self.subTest(url):
                out = translate.translate(url, {
                    "name": "edge", "admin_state": "on", "mode": "distributed",
                })
                self.assertEqual(
                    cli(out),
                    "openstack router set --name edge --enable --distributed "
                    "r-1")
                self.assertEqual(out["calls"][0]["url"],
                                 "$OS_NETWORK_API/routers/r-1")

    def test_router_update_says_nothing_about_ha(self):
        # The form declares ha and then deletes it on every request, so it is
        # never submitted. A rule mapping it would stamp --no-ha onto every
        # router edit an operator makes.
        out = translate.translate("/admin/routers/r-1/update",
                                  {"name": "edge", "ha": "on"})
        self.assertNotIn("ha", cli(out))
        self.assertNotIn("ha", out["calls"][0]["body"]["router"])

    def test_a_router_edit_without_dvr_leaves_the_mode_alone(self):
        # Horizon deletes the mode field where DVR is not permitted, and sends
        # no "distributed" key. An absent select must read the same way.
        out = translate.translate("/admin/routers/r-1/update",
                                  {"name": "edge", "admin_state": "on"})
        self.assertEqual(cli(out),
                         "openstack router set --name edge --enable r-1")
        self.assertNotIn("distributed", out["calls"][0]["body"]["router"])

    def test_router_deletes_come_from_the_routers_table(self):
        out = translate.translate("/admin/routers/", {
            "action": "routers__delete",
            "object_ids": ["r-1", "r-2"],
        })
        self.assertEqual(cli(out), "openstack router delete r-1 r-2")
        self.assertEqual(
            [call["url"].rsplit("/", 1)[-1] for call in out["calls"]],
            ["r-1", "r-2"])

    def test_project_create_carries_the_domain_id_not_the_domain_name(self):
        out = translate.translate("/identity/create", {
            "name": "engineering", "domain_id": "d-1", "domain_name": "Default",
            "description": "R&D", "enabled": "on",
        })
        self.assertEqual(
            cli(out),
            "openstack project create --domain d-1 --description 'R&D' "
            "engineering")
        body = out["calls"][0]["body"]["project"]
        self.assertEqual(body["domain_id"], "d-1")
        self.assertIs(body["enabled"], True)
        self.assertNotIn("domain_name", body)

    def test_an_unticked_project_is_created_disabled(self):
        out = translate.translate("/identity/create",
                                  {"name": "dormant", "domain_id": "d-1"})
        self.assertEqual(
            cli(out),
            "openstack project create --domain d-1 --disable dormant")
        self.assertIs(out["calls"][0]["body"]["project"]["enabled"], False)

    def test_domain_create_needs_no_domain_of_its_own(self):
        out = translate.translate("/identity/domains/create", {
            "name": "partners", "description": "Third parties",
            "enabled": "on",
        })
        self.assertEqual(
            cli(out),
            "openstack domain create --description 'Third parties' partners")
        self.assertEqual(out["calls"][0]["body"]["domain"]["name"], "partners")

    def test_group_create_renders_name_and_description(self):
        out = translate.translate("/identity/groups/create", {
            "name": "operators", "description": "On call",
        })
        self.assertEqual(
            cli(out),
            "openstack group create --description 'On call' operators")

    def test_role_create_is_nothing_but_a_name(self):
        out = translate.translate("/identity/roles/create", {"name": "auditor"})
        self.assertEqual(cli(out), "openstack role create auditor")
        self.assertEqual(out["calls"][0]["body"]["role"], {"name": "auditor"})

    def test_user_create_prompts_for_the_password_it_never_read(self):
        out = translate.translate("/identity/users/create/", {
            "name": "alice", "domain_id": "d-1", "project": "p-1",
            "email": "alice@example.com", "description": "SRE",
            "enabled": "on",
        })
        self.assertEqual(
            cli(out),
            "openstack user create --domain d-1 --project p-1 "
            "--email alice@example.com --description SRE --password-prompt "
            "alice")
        body = out["calls"][0]["body"]["user"]
        self.assertEqual(body["default_project_id"], "p-1")
        self.assertIs(body["enabled"], True)
        self.assertNotIn("password", body)

    def test_a_user_given_a_role_gets_the_second_command_too(self):
        out = translate.translate("/identity/users/create/", {
            "name": "alice", "domain_id": "d-1", "project": "p-1",
            "role_id": "r-9", "enabled": "on",
        })
        self.assertEqual(out["commands"][1],
                         "openstack role add --user alice --project p-1 r-9")
        self.assertEqual(out["calls"][1], {
            "method": "PUT",
            "url": "$OS_IDENTITY_API/projects/p-1/users/$NEW_USER_ID"
                   "/roles/r-9",
            # Keystone grants a role with an empty PUT. A body key with
            # nothing in it would be a different request.
            "body": None,
        })

    def test_the_role_grant_appears_only_when_horizon_would_make_it(self):
        # Horizon calls add_tenant_user_role only if a project and a role
        # were both chosen. Either on its own means nothing, and a command
        # for it would be a call the dashboard never made.
        for submitted in ({"project": "p-1"}, {"role_id": "r-9"}, {}):
            with self.subTest(**submitted):
                out = translate.translate(
                    "/identity/users/create/",
                    dict({"name": "alice"}, **submitted))
                self.assertEqual(len(out["commands"]), 1, out["commands"])
                self.assertNotIn("role add", " ".join(out["commands"]))

    def test_a_role_grant_curl_carries_no_body(self):
        out = translate.translate("/identity/users/create/", {
            "name": "alice", "project": "p-1", "role_id": "r-9",
        })
        curl = translate.curl_for(out["calls"][1])
        self.assertIn("-X PUT", curl)
        self.assertNotIn(" -d ", curl)
        self.assertFalse(curl.rstrip().endswith("\\"), curl)

    def test_a_submitted_password_survives_nowhere(self):
        """The end-to-end path, not just the rule: read_fields then translate.

        Passing the raw submission through both is the only version of this
        test that would notice the secret filter being loosened.
        """
        out = translate.translate(
            "/identity/users/create/",
            translate.read_fields({
                "name": ["alice"], "domain_id": ["d-1"],
                "password": ["hunter2"], "confirm_password": ["hunter2"],
                "csrfmiddlewaretoken": ["abcdef"],
            }))
        rendered = cli(out) + json.dumps(out["calls"])
        self.assertNotIn("hunter2", rendered)
        self.assertNotIn("abcdef", rendered)
        self.assertIn("--password-prompt", cli(out))

    def test_the_prompt_flag_appears_even_with_nothing_submitted(self):
        # A dropped field and an empty one are indistinguishable here, so the
        # flag has to be unconditional or it would be unreliable.
        out = translate.translate("/identity/users/create/", {"name": "bob"})
        self.assertIn("--password-prompt", cli(out))

    def test_each_identity_url_reaches_its_own_rule(self):
        """The identity paths nest, so the patterns must not poach.

        ``/identity/create`` is the project form because projects are the
        dashboard's default panel; every other panel adds a slug.
        """
        expected = {
            "/identity/create": "openstack project create",
            "/identity/domains/create": "openstack domain create",
            "/identity/groups/create": "openstack group create",
            "/identity/roles/create": "openstack role create",
            "/identity/users/create/": "openstack user create",
        }
        for url, prefix in expected.items():
            with self.subTest(url=url):
                out = translate.translate(url, {"name": "x"})
                self.assertTrue(cli(out).startswith(prefix), cli(out))

    def test_emptying_a_description_on_an_edit_clears_it(self):
        # The operator deleted the text and saved. Horizon sends "" and the
        # description goes away; a command that left the flag off would
        # quietly keep the old one, and a replayed script would rebuild
        # something the operator had removed.
        out = translate.translate("/identity/groups/g-1/update/", {
            "name": "operators", "description": "",
        })
        self.assertEqual(
            cli(out),
            "openstack group set --name operators --description '' g-1")
        self.assertEqual(out["calls"][0]["body"]["group"]["description"], "")

    def test_a_field_the_form_never_carried_is_not_a_clear(self):
        # The difference this rests on: absent is not empty. A form that
        # does not include the field at all, because the panel hid it or an
        # older Horizon did not have it, must not read as a deletion.
        out = translate.translate("/identity/groups/g-1/update/",
                                  {"name": "operators"})
        self.assertEqual(cli(out),
                         "openstack group set --name operators g-1")
        self.assertNotIn("description", out["calls"][0]["body"]["group"])

    def test_every_clearable_field_renders_an_empty_flag(self):
        # Applied straight to each rule, so a clearable added later cannot
        # quietly do nothing on a panel nobody wrote a test for.
        clearable = [(form, field) for form in rules.FORMS
                     for field in form["fields"] if field.get("clearable")]
        # A loop over nothing passes, and a keyword that stopped being read
        # would look exactly like this test being happy.
        self.assertGreaterEqual(len(clearable), 5)
        for form, field in clearable:
            with self.subTest(form["id"], field=field["field"]):
                out = translate._apply_form(
                    form, {field["field"]: ""}, {"id": "x"})
                self.assertIn("%s ''" % field["flag"], cli(out))
                body = out["calls"][0]["body"][form["envelope"]]
                self.assertEqual(body[field["api"]], "")

    def test_no_value_field_speaks_when_it_was_not_submitted(self):
        # The other half, over every rule rather than every clearable one:
        # an empty submission must put no --flag on any command, whatever
        # the rule says about clearing. Follow-up steps included, which is
        # also a check that a submission naming nothing produces none.
        for form in rules.FORMS:
            out = translate._apply_form(form, {}, {"id": "x"})
            rendered = " ".join(out["commands"])
            for spec in rules.specs(form):
                for field in spec["fields"]:
                    if field["kind"] != "value":
                        continue
                    with self.subTest(form["id"], field=field["field"]):
                        self.assertNotIn(field["flag"], rendered)

    def test_a_create_still_treats_an_empty_box_as_nothing_supplied(self):
        out = translate.translate("/identity/groups/create",
                                  {"name": "operators", "description": ""})
        self.assertEqual(cli(out), "openstack group create operators")
        self.assertNotIn("description", out["calls"][0]["body"]["group"])

    def test_clearing_the_primary_project_is_not_rendered(self):
        # There is no way to say it: openstackclient has no "user unset" and
        # no --no-project, and --project '' would go looking for a project
        # named "". Saying nothing beats rendering a command that fails.
        out = translate.translate("/identity/users/u-1/update/", {
            "name": "alice", "project": "", "email": "alice@example.com",
        })
        self.assertNotIn("--project", cli(out))
        self.assertNotIn("default_project_id",
                         out["calls"][0]["body"]["user"])

    def test_each_identity_edit_url_reaches_its_own_rule(self):
        """The edit side of the nesting, which is tighter than the create side.

        ``/identity/<id>/update/`` is the project form, and it is one segment
        short of every other panel's edit path. A pattern that reached any of
        them would rename a user by issuing ``project set``.
        """
        expected = {
            "/identity/p-1/update/": "openstack project set",
            "/identity/domains/d-1/update/": "openstack domain set",
            "/identity/groups/g-1/update/": "openstack group set",
            "/identity/roles/r-1/update/": "openstack role set",
            "/identity/users/u-1/update/": "openstack user set",
        }
        for url, prefix in expected.items():
            with self.subTest(url=url):
                out = translate.translate(url, {"name": "renamed"})
                self.assertTrue(cli(out).startswith(prefix), cli(out))
                # The id is the last word, and it is the one in the path.
                self.assertEqual(cli(out).rsplit(" ", 1)[-1],
                                 url.split("/")[-3])

    def test_a_project_edit_turns_both_sides_of_the_enabled_box(self):
        enabled = translate.translate("/identity/p-1/update/", {
            "name": "engineering", "description": "R&D", "enabled": "on",
        })
        self.assertEqual(
            cli(enabled),
            "openstack project set --name engineering --description 'R&D' "
            "--enable p-1")
        self.assertEqual(enabled["calls"][0]["method"], "PATCH")
        disabled = translate.translate("/identity/p-1/update/",
                                       {"name": "engineering"})
        self.assertIn("--disable", cli(disabled))
        self.assertIs(disabled["calls"][0]["body"]["project"]["enabled"],
                      False)

    def test_a_project_edit_never_claims_to_move_the_domain(self):
        # The form submits domain_id read-only and handle discards it.
        # Rendering --domain would describe a change Keystone does not allow.
        out = translate.translate("/identity/p-1/update/", {
            "name": "engineering", "domain_id": "d-1", "domain_name": "Default",
            "enabled": "on",
        })
        self.assertNotIn("--domain", cli(out))
        self.assertNotIn("domain_id", out["calls"][0]["body"]["project"])

    def test_a_group_edit_takes_the_id_from_the_path_not_the_hidden_field(self):
        # Both carry it. Only one of them is a place validate() can check.
        out = translate.translate("/identity/groups/g-1/update/", {
            "group_id": "g-stale", "name": "operators",
            "description": "On call",
        })
        self.assertEqual(
            cli(out),
            "openstack group set --name operators --description 'On call' g-1")
        self.assertEqual(out["calls"][0]["url"], "$OS_IDENTITY_API/groups/g-1")
        self.assertNotIn("g-stale", json.dumps(out))

    def test_a_user_edit_renders_what_horizon_sends_and_nothing_else(self):
        out = translate.translate("/identity/users/u-1/update/", {
            "id": "u-1", "name": "alice", "project": "p-2",
            "email": "alice@example.com", "description": "SRE",
            "domain_id": "d-1", "domain_name": "Default",
        })
        self.assertEqual(
            cli(out),
            "openstack user set --name alice --project p-2 "
            "--email alice@example.com --description SRE u-1")
        body = out["calls"][0]["body"]["user"]
        self.assertEqual(body["default_project_id"], "p-2")
        self.assertNotIn("domain_id", body)
        self.assertNotIn("id", body)

    def test_a_password_change_is_not_a_user_edit(self):
        # change_password is its own panel on the same id, and its field is
        # one read_fields drops anyway. It must not be mistaken for an edit.
        self.assertIsNone(translate.translate(
            "/identity/users/u-1/change_password/",
            translate.read_fields({"password": ["hunter2"]})))

    def test_project_deletes_come_from_horizons_tenants_table(self):
        out = translate.translate("/identity/", {
            "action": "tenants__delete", "object_ids": ["p1", "p2"],
        })
        self.assertEqual(cli(out), "openstack project delete p1 p2")
        self.assertTrue(out["calls"][0]["url"].endswith("/projects/p1"))
        self.assertEqual(out["title"], "Delete projects (2)")

    def test_row_delete_uses_the_id_in_the_action_field(self):
        out = translate.translate("/admin/flavors/",
                                  {"action": "flavors__delete__abc-1"})
        self.assertEqual(cli(out), "openstack flavor delete abc-1")
        self.assertEqual(len(out["calls"]), 1)
        self.assertEqual(out["calls"][0]["method"], "DELETE")
        self.assertTrue(out["calls"][0]["url"].endswith("/flavors/abc-1"))

    def test_batch_delete_expands_object_ids(self):
        out = translate.translate("/admin/networks/", {
            "action": "networks__delete", "object_ids": ["n1", "n2"],
        })
        self.assertEqual(cli(out), "openstack network delete n1 n2")
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

    def test_a_value_substituted_into_a_url_is_percent_encoded(self):
        # The url goes inside a double-quoted curl argument. A quote or a
        # space in a submitted value would otherwise end the argument early
        # and turn the rest of the path into separate words.
        out = translate.translate("/identity/users/create/", {
            "name": "alice", "project": 'a b"c', "role_id": "r-9",
        })
        url = out["calls"][1]["url"]
        self.assertIn("/projects/a%20b%22c/", url)
        curl = translate.curl_for(out["calls"][1])
        self.assertEqual(curl.count('"'), 4, curl)

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
            {"title": "Create network b",
             "commands": ["openstack network create b"], "ok": False},
            {"title": "Create network a",
             "commands": ["openstack network create a"], "ok": True},
        ]
        text = translate.script_for(entries)
        self.assertTrue(text.startswith("#!/bin/sh"))
        self.assertLess(text.index("create a"), text.index("create b"))
        self.assertIn("# NOTE: this action was rejected", text)
        self.assertEqual(text.count("# NOTE:"), 1)

    def test_a_multi_command_action_writes_every_line_in_order(self):
        # The aggregate has to exist before a host goes into it, so these
        # go out in the order the dashboard made them, under one heading.
        entry = translate.translate("/admin/aggregates/create/", {
            "name": "gpu", "add_host_to_aggregate_role_member": ["c1", "c2"],
        })
        text = translate.script_for([entry])
        self.assertEqual(text.count("# Create host aggregate gpu"), 1)
        self.assertLess(text.index("aggregate create"),
                        text.index("add host gpu c1"))
        self.assertLess(text.index("add host gpu c1"),
                        text.index("add host gpu c2"))

    def test_a_script_still_renders_an_entry_from_an_older_astrolabe(self):
        # Entries used to hold a single "cli" string. A session on the cache
        # backend outlives a restart, so an upgrade finds them and must
        # write them out rather than skip the line and leave a bare comment.
        text = translate.script_for([
            {"title": "Create network a", "cli": "openstack network create a"},
        ])
        self.assertIn("openstack network create a", text)

    def test_commands_of_tolerates_an_entry_with_neither_key(self):
        self.assertEqual(translate.commands_of({"title": "odd"}), [])


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

    def test_one_action_can_outgrow_a_whole_cookie_session(self):
        """The claim store.py makes to operators, kept true by measurement.

        A number written into a docstring rots quietly. This asserts the
        shape of the advice rather than the figure: that a single recorded
        action can exceed the ~4KB a ``signed_cookies`` session gets, so
        lowering the entry cap is necessary without being sufficient.
        """
        one_call = translate.translate("/admin/flavors/create/", {
            "name": "m1.small", "vcpus": "1", "memory_mb": "2048",
            "disk_gb": "20",
        })
        many_calls = translate.translate("/admin/aggregates/create/", {
            "name": "gpu-nodes", "availability_zone": "az-gpu",
            "add_host_to_aggregate_role_member":
                ["compute-%02d.example.net" % n for n in range(30)],
        })
        for entry in (one_call, many_calls):
            entry["at"] = 1757000000.0
            entry["ok"] = True
        self.assertLess(len(json.dumps(one_call)), 1024,
                        "an ordinary entry is meant to be small")
        self.assertGreater(len(json.dumps(many_calls)), 4096,
                           "store.py tells operators this one does not fit; "
                           "if it now does, go and fix the docstring")

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


# ------------------------------------------------------- 3b. the panel views
#
# The panel is the other half of the middleware: one writes an entry shape
# into the session, the other reads it back out and renders it. Tested apart,
# both can be right while disagreeing about what an entry looks like, so
# these tests put a real translate() result through the real template.
#
# Two things stand between that and a bare Django. The views inherit from
# Horizon's HorizonTemplateView, and the template extends Horizon's
# base.html; both are replaced below with the smallest stand-in that still
# exercises our own code.


def _use_stub_horizon():
    """Make ``astrolabe.views`` importable without a Horizon on the path.

    Only when there is genuinely no Horizon: with a real one installed the
    real base class is used, so this substitution can never be what hides a
    change in what the panel inherits.
    """
    try:
        import horizon.views  # noqa: F401
        return False
    except ImportError:
        pass

    from django.views import generic
    stub = types.ModuleType("horizon")
    stub_views = types.ModuleType("horizon.views")
    # HorizonTemplateView is a TemplateView plus a page-title mixin, and the
    # title is rendered by base.html, which is stubbed too.
    stub_views.HorizonTemplateView = generic.TemplateView
    stub.views = stub_views
    sys.modules.setdefault("horizon", stub)
    sys.modules.setdefault("horizon.views", stub_views)
    return True


def _panel_templates():
    """A template setup that can find our template and nothing else.

    ``base.html`` is Horizon's, and rendering the real one means standing up
    a dashboard, a navigation tree and a request context this plugin has no
    part in. What is ours is what goes inside ``{% block main %}``, so that
    is the only block the stand-in keeps.
    """
    from astrolabe import rules as _rules
    templates = os.path.join(os.path.dirname(_rules.__file__), "templates")
    return [{
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [templates],
        "OPTIONS": {
            "loaders": [
                ("django.template.loaders.locmem.Loader",
                 {"base.html": "{% block main %}{% endblock %}"}),
                "django.template.loaders.filesystem.Loader",
            ],
        },
    }]


def _panel_urlconf():
    """The URL names Horizon would build, without building a Horizon.

    Horizon composes ``horizon:<dashboard slug>:<panel slug>:<name>`` from
    ``Astrolabe.slug`` and ``Commands.slug``. Both of those modules import
    horizon, so this mirrors the result instead. The namespace is worth
    getting right: views.clear and the template hard-code it, and a rename on
    either side is a 500 on a page that otherwise looks fine.
    """
    name = "astrolabe_panel_urls_for_tests"
    if name not in sys.modules:
        from django.urls import include
        from django.urls import re_path
        panel = include(("astrolabe.panel_urls", "commands"))
        dashboard = include(([re_path(r"^commands/", panel)], "astrolabe"))
        site = include(([re_path(r"^astrolabe/", dashboard)], "horizon"))
        module = types.ModuleType(name)
        module.urlpatterns = [re_path(r"^", site)]
        sys.modules[name] = module
    return name


class TestPanelViews(unittest.TestCase):
    """What the middleware recorded is what the panel shows."""

    def setUp(self):
        try:
            import django
            from django.conf import settings
        except ImportError:
            self.skipTest("Django is not on this interpreter's path")
        if not settings.configured:
            settings.configure(DEBUG=True)
        # Needed before anything renders: the i18n tags in the template walk
        # the app registry looking for locale directories.
        django.setup()
        _use_stub_horizon()

        from django.test import override_settings
        # Overridden rather than configured, so these tests behave the same
        # whether the baseline settings are the bare ones above or Horizon's,
        # which is what layer 4 runs under.
        overrides = override_settings(TEMPLATES=_panel_templates(),
                                      ROOT_URLCONF=_panel_urlconf())
        overrides.enable()
        self.addCleanup(overrides.disable)

        from astrolabe import views
        self.views = views

    def _request(self, method="get", path="/astrolabe/commands/"):
        from django.test import RequestFactory
        request = getattr(RequestFactory(), method)(path)
        request.session = Session()
        return request

    def _recorded(self, name="m1.small", ok=True):
        """An entry exactly as the middleware would have written it."""
        entry = translate.translate("/admin/flavors/create/", {
            "name": name, "vcpus": "1", "memory_mb": "2048", "disk_gb": "20",
        })
        entry["at"] = 1757000000.0
        entry["ok"] = ok
        return entry

    def _page(self, entries=(), session=None):
        """Render the panel over a session holding ``entries``, oldest first."""
        request = self._request()
        if session is not None:
            request.session = session
        for entry in entries:
            store.add(request.session, entry)
        response = self.views.IndexView.as_view()(request)
        return response.render().content.decode("utf-8")

    def test_the_panel_renders_what_the_middleware_recorded(self):
        page = self._page([self._recorded()])
        self.assertIn("openstack flavor create", page)
        self.assertIn("m1.small", page)
        # And the REST half, which only exists on the page: the session
        # stores calls in structured form and the view renders them.
        self.assertIn("$OS_COMPUTE_API/flavors", page)
        self.assertIn("X-Auth-Token: $OS_TOKEN", page)

    def test_the_panel_lists_the_newest_action_first(self):
        page = self._page([self._recorded("m1.older"),
                           self._recorded("m1.newer")])
        self.assertLess(page.index("m1.newer"), page.index("m1.older"),
                        "the panel shows newest first; only the script "
                        "download runs in the order the operator worked")

    def test_a_rejected_action_is_marked_as_rejected(self):
        self.assertIn("rejected by the dashboard",
                      self._page([self._recorded(ok=False)]))
        self.assertNotIn("rejected by the dashboard",
                         self._page([self._recorded(ok=True)]))

    def test_an_entry_written_by_an_older_astrolabe_still_renders(self):
        # Sessions survive a restart on the cache backend, so an upgrade can
        # find entries in the session that predate whatever keys the current
        # code expects. None of them may raise, and a missing ``ok`` in
        # particular must not read as "the dashboard refused this".
        page = self._page([{"title": "Create flavor m1.old",
                            "cli": "openstack flavor create m1.old"}])
        self.assertIn("openstack flavor create m1.old", page)
        self.assertNotIn("rejected by the dashboard", page)

    def test_an_empty_log_says_so_and_offers_nothing_to_clear(self):
        page = self._page()
        self.assertIn("Nothing recorded yet", page)
        self.assertNotIn("commands/clear", page)
        # The download stays, because an empty script is still valid output.
        self.assertIn("commands/script", page)

    def test_a_command_is_escaped_before_it_reaches_the_page(self):
        # Every rendered command is built from text the operator typed into a
        # Horizon form, and the panel hands it straight back to them.
        entry = self._recorded(name="<img src=x onerror=alert(1)>")
        page = self._page([entry])
        self.assertNotIn("<img", page)
        self.assertIn("&lt;img", page)

    def test_the_footer_explains_every_placeholder_a_command_can_hold(self):
        # A new rule with a new endpoint is the easy way to leave an operator
        # looking at a placeholder the page never mentions.
        used = {"$OS_TOKEN"}
        for rule in rules.FORMS:
            for spec in rules.specs(rule):
                used.update(re.findall(r"\$OS_[A-Z_]+", spec["endpoint"]))
        for table in rules.TABLES.values():
            used.update(re.findall(r"\$OS_[A-Z_]+", table["path"]))
        page = self._page([self._recorded()])
        listed = {name for name, _example in self.views.ENVIRONMENT}
        self.assertEqual(sorted(used - listed), [],
                         "these appear in rendered commands but are not in "
                         "views.ENVIRONMENT")
        for name in sorted(used):
            self.assertIn(name, page)

    def test_the_footer_explains_the_id_it_could_not_know(self):
        # $NEW_… is not something to set up front like the others, so it is
        # not in ENVIRONMENT. The page still has to say what it is, or an
        # operator meets one in a REST path with nothing to go on.
        page = self._page([self._recorded()])
        self.assertIn("$NEW_", page)
        self.assertIn("id of the resource the first call created", page)

    def test_the_panel_shows_every_command_of_a_multi_call_action(self):
        entry = translate.translate("/admin/aggregates/create/", {
            "name": "gpu-nodes",
            "add_host_to_aggregate_role_member": ["cmp1", "cmp2"],
        })
        entry["at"] = 1757000000.0
        entry["ok"] = True
        page = self._page([entry])
        for command in entry["commands"]:
            self.assertIn(command, page)
        # One heading, not one per command: it was one thing the operator
        # did, and the panel says so.
        self.assertEqual(page.count("Create host aggregate gpu-nodes"), 1)
        self.assertIn("$NEW_AGGREGATE_ID", page)

    def test_the_panel_states_the_cap_it_is_keeping(self):
        from django.test import override_settings
        with override_settings(ASTROLABE_MAX_ENTRIES=3):
            page = self._page([self._recorded()])
        self.assertIn("most recent 3 actions", page)

    def test_the_download_is_a_shell_script_named_for_the_moment(self):
        request = self._request()
        store.add(request.session, self._recorded())
        response = self.views.script(request)
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/x-shellscript", response["Content-Type"])
        self.assertRegex(response["Content-Disposition"],
                         r'^attachment; filename="astrolabe-'
                         r'\d{8}-\d{6}\.sh"$')
        body = response.content.decode("utf-8")
        self.assertTrue(body.startswith("#!/bin/sh"))
        # Unescaped, unlike the page: this one is meant to be run.
        self.assertIn("openstack flavor create", body)

    def test_the_download_of_an_empty_log_is_still_a_script(self):
        body = self.views.script(self._request()).content.decode("utf-8")
        self.assertTrue(body.startswith("#!/bin/sh"))

    def test_clear_empties_the_log_and_returns_to_the_panel(self):
        request = self._request("post", "/astrolabe/commands/clear/")
        store.add(request.session, self._recorded())
        response = self.views.clear(request)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/astrolabe/commands/")
        self.assertEqual(store.load(request.session), [])

    def test_clear_refuses_a_get(self):
        # It is a link away from being wiped by a browser prefetch or
        # anything else that follows a URL without being asked to.
        request = self._request("get", "/astrolabe/commands/clear/")
        store.add(request.session, self._recorded())
        # Asserted as well as caught: Django logs the refusal, and letting it
        # through would print a warning in the middle of a passing run.
        with self.assertLogs("django.request", level="WARNING"):
            response = self.views.clear(request)
        self.assertEqual(response.status_code, 405)
        self.assertEqual(len(store.load(request.session)), 1)


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

    def test_validate_detects_a_moved_panel(self):
        # The failure this whole mechanism exists for: the form is untouched,
        # every field is where the rule says, and the panel has moved, so the
        # rule never fires again. Checking fields alone would call this fine.
        form = dict(rules.FORMS[0])
        form["url"] = r"/admin/flavors/somewhere_else/?$"
        original = rules.FORMS
        rules.FORMS = [form]
        try:
            problems = rules.validate()
        finally:
            rules.FORMS = original
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("does not match", problems[0])
        self.assertEqual(rules.validate(), [])

    def test_validate_detects_a_route_that_stopped_reversing(self):
        form = dict(rules.FORMS[0])
        form["routes"] = ["horizon:admin:flavors:no_such_view"]
        original = rules.FORMS
        rules.FORMS = [form]
        try:
            problems = rules.validate()
        finally:
            rules.FORMS = original
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("cannot fire", problems[0])
        self.assertEqual(rules.validate(), [])

    def test_validate_detects_an_id_captured_from_the_wrong_segment(self):
        # An edit rule can match the path while its group lands on the wrong
        # part of it -- Horizon adding a segment is all it takes. The command
        # then looks entirely correct and names the wrong resource, which is
        # worse than not matching. Matching is not enough; the capture has to
        # line up with the id the route was reversed with.
        rule = dict(next(one for one in rules.FORMS
                         if one["id"] == "network-update-admin"))
        rule["url"] = r"/(?P<id>admin)/networks/[^/]+/update/?$"
        original = rules.FORMS
        rules.FORMS = [rule]
        try:
            problems = rules.validate()
        finally:
            rules.FORMS = original
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("rather than the resource", problems[0])
        self.assertEqual(rules.validate(), [])

    def test_validate_detects_a_renamed_membership_field(self):
        # The one a step brings with it. A MembershipAction names its
        # multi-select after its own slug at runtime, so base_fields never
        # sees it and the ordinary field check cannot. If that name moves,
        # the step reads an empty list and the follow-up calls simply stop
        # appearing, with the first command still perfectly correct.
        rule = dict(next(one for one in rules.FORMS
                         if one["id"] == "aggregate-create"))
        step = dict(rule["then"][0], per="hosts_by_some_other_name")
        rule["then"] = [step]
        original = rules.FORMS
        rules.FORMS = [rule]
        try:
            problems = rules.validate()
        finally:
            rules.FORMS = original
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("stop being recorded", problems[0])
        self.assertIn("add_host_to_aggregate_role_member", problems[0])
        self.assertEqual(rules.validate(), [])

    def test_validate_detects_a_renamed_field_on_a_step(self):
        rule = dict(next(one for one in rules.FORMS
                         if one["id"] == "user-create"))
        rule["then"] = [dict(rule["then"][0],
                             fields=[rules.opt("user_name", "--user")])]
        original = rules.FORMS
        rules.FORMS = [rule]
        try:
            problems = rules.validate()
        finally:
            rules.FORMS = original
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("user_name", problems[0])
        self.assertIn("step role_add", problems[0])
        self.assertEqual(rules.validate(), [])

    def test_a_steps_fields_may_come_from_either_action_class(self):
        # A workflow posts every step at once, so the aggregate's name and
        # its host list arrive together even though they are declared on
        # different classes. Checking a step against only its own class
        # would report the name as missing.
        rule = next(one for one in rules.FORMS
                    if one["id"] == "aggregate-create")
        step = rule["then"][0]
        own = rules._load_form(step["form"])
        self.assertNotIn("name", getattr(own, "base_fields", {}),
                         "if this ever holds, the test proves nothing")
        self.assertEqual(rules.validate(), [])

    def test_validate_detects_a_renamed_table(self):
        # A renamed table is the delete-side version of a moved panel: the
        # action field stops carrying the name we match on, and deletes go
        # unrecorded with nothing said.
        original = dict(rules.TABLES["roles"])
        rules.TABLES["roles"] = dict(
            original, table=rules.IDENT + "users.tables:UsersTable")
        try:
            problems = rules.validate()
        finally:
            rules.TABLES["roles"] = original
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("stop being recorded", problems[0])
        self.assertEqual(rules.validate(), [])

    def test_every_route_reverses_to_something_its_rule_matches(self):
        # validate() covers this, but a failure there names one rule among
        # many. This reports each panel separately.
        for form in rules.FORMS:
            wants_id = "(?P<id>" in form["url"]
            for name in form["routes"]:
                with self.subTest(form["id"], route=name):
                    path, stand_in = rules._reverse_route(name, wants_id)
                    self.assertIsNotNone(path, "%s no longer reverses" % name)
                    self.assertRegex(path, form["url"])
                    if stand_in is not None:
                        match = re.search(form["url"], path)
                        self.assertEqual(match.group("id"), stand_in)

    def test_uncovered_fields_are_reported_for_review(self):
        # Not a failure: plenty of fields are deliberately unmapped. Printed
        # so an upgrade review can see what a rule is choosing to ignore.
        report = rules.uncovered()
        for form_id, fields in sorted(report.items()):
            print("  note: %s does not map %s" % (form_id, ", ".join(fields)))


# ------------------------------------------------- 5. against openstackclient


class TestAgainstTheCLI(unittest.TestCase):
    """The commands the rules render are commands that exist.

    The other half of layer 4, and the half nothing else would notice. A rule
    can be perfectly aligned with Horizon -- right URL, right fields, right
    table -- and render ``--no-share`` long after openstackclient stopped
    taking it. The panel looks correct, the recording looks correct, and the
    command fails in the operator's shell.
    """

    def setUp(self):
        try:
            import openstackclient  # noqa: F401
        except ImportError:
            self.skipTest(
                "python-openstackclient is not on this interpreter's path; "
                "see the module docstring for the command that includes this "
                "layer")

    def _staged(self, forms):
        """Run verify_cli over a replacement rule table."""
        original = rules.FORMS
        rules.FORMS = forms
        try:
            return rules.verify_cli()
        finally:
            rules.FORMS = original

    def test_every_rendered_command_still_exists(self):
        self.assertEqual(rules.verify_cli(), [])

    def test_the_check_is_looking_at_a_real_number_of_flags(self):
        # verify_cli reports nothing when it finds nothing to check, and an
        # entry point layout it did not expect would look exactly like a
        # clean run. Assert it is actually resolving commands and flags.
        self.assertGreater(len(rules._cli_commands()), 100)
        self.assertGreater(
            sum(len(rules.flags(form)) for form in rules.FORMS), 50)

    def test_verify_cli_detects_a_withdrawn_flag(self):
        form = dict(rules.FORMS[0])
        form["fields"] = [rules.opt("name", "--no-such-flag")]
        problems = self._staged([form])
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("no longer accepts --no-such-flag", problems[0])
        self.assertEqual(rules.verify_cli(), [])

    def test_verify_cli_detects_a_renamed_command(self):
        form = dict(rules.FORMS[0], command=["openstack", "flavor", "forge"])
        problems = self._staged([form])
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("no longer an openstack command", problems[0])
        self.assertEqual(rules.verify_cli(), [])

    def test_verify_cli_detects_a_command_that_lost_its_positional(self):
        # Every rule ends in one: a create names its subject, an edit names
        # the resource it is editing. A command that stopped taking one would
        # leave that name dangling on the end, which argparse rejects.
        # "token issue" is a real command that genuinely takes none.
        form = dict(rules.FORMS[0], command=["openstack", "token", "issue"],
                    fields=[rules.arg("name")])
        problems = self._staged([form])
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("nowhere to go", problems[0])
        self.assertEqual(rules.verify_cli(), [])

    def test_a_follow_up_step_is_checked_like_any_other_command(self):
        # "aggregate add host" and "role add" are commands in their own
        # right, and a rule naming one wrongly fails the same way.
        rule = dict(next(one for one in rules.FORMS
                         if one["id"] == "aggregate-create"))
        rule["then"] = [dict(rule["then"][0],
                             command=["openstack", "aggregate", "attach"])]
        problems = self._staged([rule])
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("no longer an openstack command", problems[0])
        self.assertIn("step aggregate_attach", problems[0])
        self.assertEqual(rules.verify_cli(), [])

    def test_the_steps_in_the_table_are_real_commands(self):
        # Named directly, so this layer cannot pass by never reaching them.
        commands = rules._cli_commands()
        named = {rules.cli_name(spec["command"])
                 for form in rules.FORMS for spec in rules.specs(form)
                 if spec is not form}
        self.assertEqual(named,
                         {"aggregate_add_host", "role_add", "subnet_create"})
        for name in sorted(named):
            with self.subTest(name):
                self.assertIn(name, commands)

    def test_the_newest_api_version_of_a_command_is_the_one_checked(self):
        # Several commands are registered once per API version. Horizon talks
        # Keystone v3 and Cinder v3, and the v2 parsers are missing flags the
        # rules legitimately use, so checking against one of those would
        # report drift that is not there.
        for name in ("user_set", "project_set", "volume_type_set"):
            with self.subTest(name):
                entry = rules._cli_commands()[name]
                self.assertRegex(entry.group, r"\.v3$")


if __name__ == "__main__":
    unittest.main(verbosity=2)
