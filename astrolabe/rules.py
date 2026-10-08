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

"""What Astrolabe knows how to translate.

This is the file you edit to teach Astrolabe a new panel. Each entry maps a
Horizon form to the ``openstack`` command and REST call it corresponds to.
This module is pure data plus its own self-check; :mod:`astrolabe.translate`
holds the generic interpreter that applies these rules, and knows nothing
about any particular panel.

Because the rules live in Python, :func:`validate` can import the Horizon form
each rule targets and check the field names still exist, so a rename in a
future Horizon release surfaces as a warning instead of a silently incomplete
command.

Nothing at module level imports Django or Horizon, so this module stays
importable (and testable) on its own.
"""

import re

from importlib import import_module

# Rendered commands never carry a real endpoint or a real token: these panels
# get screenshotted into tickets. Commands render against shell variables
# instead, which the panel footer documents.
COMPUTE = "$OS_COMPUTE_API"
VOLUME = "$OS_VOLUME_API"
NETWORK = "$OS_NETWORK_API"
IDENTITY = "$OS_IDENTITY_API"

# Import prefixes, only to keep the table list below readable.
ADMIN = "openstack_dashboard.dashboards.admin."
IDENT = "openstack_dashboard.dashboards.identity."
PROJ = "openstack_dashboard.dashboards.project."


# --------------------------------------------------------------- field kinds
#
# Seven kinds cover every rule below. Each describes one form field: how it
# reaches the command line, and how it reaches the REST body. The last of them
# is the exception, and reads the URL rather than the submission.


def opt(field, flag, api=None, cast="str", omit_when=None, absent_when=None,
        clearable=False):
    """A value carried by a flag: ``--ram 2048``.

    ``omit_when`` drops the flag for a given value but keeps it in the REST
    body, which is how ``--swap 0`` stays off the command line while ``swap:
    0`` remains meaningful to the API.

    ``absent_when`` names a sentinel the operator can type that means "not
    supplied", and drops the field from the command *and* the body. Horizon's
    flavor form uses ``auto`` this way.

    ``clearable`` says an empty box means "remove this", which is only ever
    true on an edit. A create and an edit submit the same empty string and
    mean opposite things by it: nothing was supplied, against delete what is
    there. Without this the command silently leaves the old value in place,
    so a replayed script does not reproduce what the operator built.

    Set it only where emptying the field is a thing the API actually does.
    A blank the service would reject -- Nova will not let an aggregate's
    availability zone be cleared once set -- is better left unrecorded and
    written down, because a command that fails is worse than one that is
    short. An absent field is skipped either way: it is an empty one, not a
    missing one, that this is about.
    """
    return {
        "kind": "value", "field": field, "flag": flag,
        "api": api or field, "cast": cast, "omitWhen": omit_when,
        "absentWhen": absent_when, "clearable": clearable,
    }


def boolean(field, api, on=None, off=None):
    """A checkbox.

    ``on`` is the flag emitted when it is ticked, ``off`` when it is not.
    Either may be None. Django omits unticked checkboxes from the submission
    entirely, so absence means False.
    """
    return {
        "kind": "flag", "field": field, "api": api, "on": on, "off": off,
    }


def repeated(field, flag, api):
    """A multi-select, emitted as the flag once per value."""
    return {"kind": "multi", "field": field, "flag": flag, "api": api}


def lines(field, flag, api, clear=None):
    """A textarea holding one value per line, emitted as the flag per line.

    Horizon's subnet form collects DNS servers this way: a box the operator
    types into, read back with ``splitlines()``. Blank lines are dropped,
    the way Horizon drops them.

    ``clear`` is the list version of :func:`opt`'s ``clearable``: the flag
    that empties the list, for the edit forms where Horizon sends an empty
    list rather than nothing. The CLI spells that with a flag of its own --
    ``--no-dns-nameservers`` -- rather than with an empty value.
    """
    return {
        "kind": "lines", "field": field, "flag": flag, "api": api,
        "clear": clear,
    }


def pairs(field, flag, api, keys, api_keys=None, clear=None):
    """A textarea whose lines are comma-separated values with known names.

    An allocation pool is typed as ``192.168.1.100,192.168.1.120`` and goes
    to Neutron as ``{"start": ..., "end": ...}``; the CLI spells the same
    thing ``--allocation-pool start=...,end=...``. ``keys`` names the parts
    for the command line and ``api_keys`` for the body, because the two do
    not always agree -- a host route's second value is the ``gateway`` to
    the CLI and the ``nexthop`` to Neutron.

    Short lines are zipped, not rejected, which is what Horizon does with
    them: a malformed line produces a malformed request either way, and the
    form has already marked the submission rejected.
    """
    return {
        "kind": "pairs", "field": field, "flag": flag, "api": api,
        "keys": list(keys), "apiKeys": list(api_keys or keys),
        "clear": clear,
    }


def captured(group, flag, api):
    """A value out of the URL, handed to the command as a flag.

    Not every id in a path is the thing being changed. A subnet is created
    inside a network, and the network is named by the path rather than by
    the form: ``/networks/<network_id>/subnets/create``. :func:`target`
    would make it the command's trailing argument, which is the subnet's
    place; this passes it as a flag and writes it to the body, where it is
    an attribute of the new subnet rather than its identity.
    """
    return {"kind": "captured", "field": group, "flag": flag, "api": api}


def parent(field, flag, api):
    """The resource the rule's first call created, as a later step sees it.

    A subnet belongs to the network created a moment earlier. The command
    can say so plainly, because the operator typed a name and the CLI
    resolves one; the REST body cannot, because it wants the id, and the id
    only exists once the first call has returned. So the body carries the
    rule's ``creates`` placeholder, the same one ``{new}`` puts in a path.
    """
    return {"kind": "parent", "field": field, "flag": flag, "api": api}


def only(field, when=None, unless=None):
    """Gate a field on the state of others. Wraps any of the kinds above.

    Horizon's forms are full of fields that mean something only in company:
    a subnet's prefix length is sent only alongside an address pool, and its
    gateway only when "Disable Gateway" is clear. A rule that emitted them
    regardless would render two ``--gateway`` flags, or a prefix length for
    a subnet that has no pool to take it from.

    ``when`` names fields that must all carry a value, ``unless`` fields
    that must all be empty. Absent counts as empty, and an unticked checkbox
    is absent.
    """
    return dict(field, when=list(when or []), unless=list(unless or []))


def arg(field, api="name"):
    """A positional argument. Always rendered last, as the CLI expects.

    Pass ``api=None`` for a positional that names some *other* resource
    rather than describing this one -- the aggregate a host is being added
    to, say. It goes on the command line and stays out of the REST body,
    where it would read as an attribute of the thing being changed.
    """
    return {"kind": "positional", "field": field, "api": api}


def target(group="id"):
    """The resource being changed, read out of the URL rather than the form.

    An edit form does not submit the id of the thing it is editing; Horizon
    puts it in the path. Every panel names that capture group differently --
    ``id`` on flavors, ``network_id`` on networks, ``tenant_id`` on projects
    -- but Astrolabe matches with a pattern of its own rather than Horizon's,
    so a rule writes ``(?P<id>[^/]+)`` whatever upstream calls it and this
    reads it back.

    It trails the command like any other positional, and reaches the REST
    call through the ``{id}`` in the rule's endpoint. Nothing goes in the
    body: an id identifies the resource rather than describing it, and
    Neutron would reject it as an attribute. It carries no ``api`` key at
    all, for the same reason :func:`redacted` does not.
    """
    return {"kind": "target", "field": group}


def item(api=None):
    """The value a repeating step is currently on. See ``per``.

    A step with ``per`` runs once for each value in a multi-select: one
    ``aggregate add host`` per host. This stands for whichever one it is on,
    and like :func:`target` it names no form field, because the field it
    comes from is the list rather than the value.

    ``api`` places it in the REST body; leave it None to keep it on the
    command line only.
    """
    return {"kind": "item", "field": None, "api": api}


#: Kinds whose ``field`` does not name a form field: ``target`` and
#: ``captured`` name URL capture groups, and ``item`` names nothing at all.
#: validate() must not go looking for any of them in ``base_fields``.
UNSUBMITTED = frozenset(["target", "item", "captured"])


def specs(form):
    """A rule and each of its follow-up steps, which share a shape.

    A step is a rule minus the parts that place it -- no url, no routes, no
    title -- so everything that walks a rule's command, endpoint and fields
    can walk a step's too, and does.
    """
    return [form] + list(form.get("then") or [])


def choice(field, api, choices):
    """A select where each option carries its own flag and its own API value.

    ``choices`` maps the submitted value to ``(flag, api_value)``. A submitted
    value the map does not mention emits nothing and writes nothing, which is
    how Horizon's "Use Server Default" options behave: the router form sends
    ``distributed`` only once the operator has picked centralized or
    distributed. Leaving the sentinel out of the map says that, without the
    rule having to name it.

    An option's flag may carry a fixed value -- ``"--gateway none"`` -- for
    the cases where the CLI spells a choice as a word rather than a switch.
    The interpreter splits it, and :func:`flags` reports only the flag, so
    verify_cli() checks the part a parser would know about.

    Pass None as the flag for an option the CLI expresses by saying nothing.
    """
    return {
        "kind": "choice", "field": field, "api": api,
        "choices": {
            value: {"flag": flag, "value": api_value}
            for value, (flag, api_value) in choices.items()
        },
    }


def redacted(field, flag):
    """Stands in for a field Astrolabe deliberately never reads.

    ``translate.read_fields`` drops password-like names before the interpreter
    sees them, so a rule cannot know what was typed — only that the form has
    such a field. Emitting a prompting flag is the honest rendering: the
    command asks for the value when it runs, rather than quietly creating a
    user nobody can log in as.

    The flag is unconditional, because absence proves nothing here: a dropped
    field and an empty one look identical from this side. Use it only for
    fields the form requires.

    Nothing reaches the REST body — there is no value to put there, and a
    placeholder inside the single-quoted ``curl -d`` payload would not expand.
    The field name is kept so :func:`validate` goes on checking it exists.
    """
    return {"kind": "redacted", "field": field, "flag": flag}


# --------------------------------------------------------------------- rules
#
# ``form`` names the Horizon class this rule was written against, as
# "module:ClassName". It is used only by validate().
#
# A rule may carry ``then``: follow-up steps, for the panels where saving one
# form makes more than one API call. A step has a rule's command, method,
# endpoint, envelope and fields, and three keys of its own:
#
#   ``per``     a multi-select field; the step runs once per selected value,
#               which item() stands for. No selection, no step.
#   ``when``    field names that must all carry a value, for a step Horizon
#               itself only makes sometimes.
#   ``form``    the action class the step's own fields come from, where that
#               differs from the rule's. A workflow POSTs every step at once,
#               so either class's fields may appear; validate() accepts both.
#
# Follow-up calls need the id of the thing the first call created, and
# Astrolabe never sees a response, so it cannot know it. The rule says
# ``creates``, a placeholder that ``{new}`` in a step's endpoint resolves to,
# and the panel footer explains it. Only the REST half needs this: the
# commands address the new resource by the name the operator typed, which is
# what makes them runnable as they stand.

FORMS = [
    {
        "id": "flavor-create",
        "title": "Create flavor",
        "url": r"/admin/flavors/create/?$",
        "form": "openstack_dashboard.dashboards.admin.flavors.workflows"
                ":CreateFlavorInfoAction",
        "routes": ["horizon:admin:flavors:create"],
        "method": "POST",
        "endpoint": COMPUTE + "/flavors",
        "envelope": "flavor",
        "command": ["openstack", "flavor", "create"],
        "fields": [
            # Horizon defaults this to "auto" and novaclient turns "auto"
            # into an omitted id, so it must not reach the command or body.
            opt("flavor_id", "--id", api="id", absent_when="auto"),
            opt("vcpus", "--vcpus", cast="int"),
            opt("memory_mb", "--ram", api="ram", cast="int"),
            opt("disk_gb", "--disk", api="disk", cast="int"),
            opt("eph_gb", "--ephemeral", api="OS-FLV-EXT-DATA:ephemeral",
                cast="int", omit_when="0"),
            opt("swap_mb", "--swap", api="swap", cast="int", omit_when="0"),
            arg("name"),
        ],
    },
    {
        "id": "volume-type-create",
        "title": "Create volume type",
        "url": r"/admin/volume_types/create_type/?$",
        "form": "openstack_dashboard.dashboards.admin.volume_types.forms"
                ":CreateVolumeType",
        "routes": ["horizon:admin:volume_types:create_type"],
        "method": "POST",
        "endpoint": VOLUME + "/types",
        "envelope": "volume_type",
        "command": ["openstack", "volume", "type", "create"],
        "fields": [
            opt("vol_type_description", "--description", api="description"),
            # The form ships this ticked; only the private case needs a flag.
            boolean("is_public", "os-volume-type-access:is_public",
                    off="--private"),
            arg("name"),
        ],
    },
    {
        "id": "volume-type-update",
        "title": "Update volume type",
        "url": r"/admin/volume_types/(?P<id>[^/]+)/update_type/?$",
        "form": "openstack_dashboard.dashboards.admin.volume_types.forms"
                ":EditVolumeType",
        "routes": ["horizon:admin:volume_types:update_type"],
        "method": "PUT",
        "endpoint": VOLUME + "/types/{id}",
        "envelope": "volume_type",
        "command": ["openstack", "volume", "type", "set"],
        "fields": [
            opt("name", "--name"),
            # The create form calls this vol_type_description; the edit form
            # calls it description. Same field to an operator, two names to a
            # rule.
            opt("description", "--description", clearable=True),
            # Cinder spells the key two ways, and both are right: a create
            # takes "os-volume-type-access:is_public", an update takes a plain
            # "is_public". volume_type_update passes it on every edit, so both
            # sides of the box are named here.
            boolean("is_public", "is_public", on="--public", off="--private"),
            target(),
        ],
    },
    {
        "id": "network-create",
        "title": "Create network",
        "url": r"/admin/networks/create/?$",
        "form": "openstack_dashboard.dashboards.admin.networks.forms"
                ":CreateNetwork",
        "routes": ["horizon:admin:networks:create"],
        "method": "POST",
        "endpoint": NETWORK + "/networks",
        "envelope": "network",
        "command": ["openstack", "network", "create"],
        "fields": [
            opt("tenant_id", "--project"),
            opt("network_type", "--provider-network-type",
                api="provider:network_type"),
            opt("physical_network", "--provider-physical-network",
                api="provider:physical_network"),
            opt("segmentation_id", "--provider-segment",
                api="provider:segmentation_id", cast="int"),
            opt("mtu", "--mtu", cast="int"),
            boolean("shared", "shared", on="--share"),
            boolean("external", "router:external", on="--external"),
            # Defaults to up; absence means the operator unticked it.
            boolean("admin_state", "admin_state_up", off="--disable"),
            repeated("az_hints", "--availability-zone-hint",
                     "availability_zone_hints"),
            arg("name"),
        ],
    },
    {
        "id": "network-update-admin",
        "title": "Update network",
        # One of a pair, where the router rules are a single rule serving two
        # dashboards. The two network edit forms are unrelated classes and
        # the project one has no "External Network" box, so a rule matching
        # both paths would read that missing box as unticked and render
        # --internal, announcing a change the operator never made. Two rules
        # instead, each naming the fields its own form has.
        "url": r"/admin/networks/(?P<id>[^/]+)/update/?$",
        "form": "openstack_dashboard.dashboards.admin.networks.forms"
                ":UpdateNetwork",
        "routes": ["horizon:admin:networks:update"],
        "method": "PUT",
        "endpoint": NETWORK + "/networks/{id}",
        "envelope": "network",
        "command": ["openstack", "network", "set"],
        # Every boolean here names both sides. UpdateNetwork.handle builds its
        # params unconditionally from all four fields, so an unticked box on
        # an edit is a decision -- "make this not shared" -- in a way the same
        # unticked box on a create is not.
        "fields": [
            opt("name", "--name"),
            boolean("admin_state", "admin_state_up",
                    on="--enable", off="--disable"),
            boolean("shared", "shared", on="--share", off="--no-share"),
            boolean("external", "router:external",
                    on="--external", off="--internal"),
            target(),
        ],
    },
    {
        "id": "network-create-project",
        "title": "Create network",
        # The project panel is a wizard, not a plain form: one submission
        # creates the network and, if the box is ticked, a subnet inside it.
        # Field names are its own throughout -- net_name where the admin
        # form says name -- because the two forms are unrelated classes.
        "url": r"/project/networks/create/?$",
        "form": PROJ + "networks.workflows:CreateNetworkInfoAction",
        "routes": ["horizon:project:networks:create"],
        "method": "POST",
        "endpoint": NETWORK + "/networks",
        "envelope": "network",
        "command": ["openstack", "network", "create"],
        "creates": "$NEW_NETWORK_ID",
        # No provider attributes and no external box here; those belong to
        # the admin form. with_subnet is unmapped on purpose and uncovered()
        # lists it: it describes nothing about the network, it decides
        # whether the step below happens, which is what "when" reads it for.
        "fields": [
            opt("mtu", "--mtu", cast="int"),
            boolean("admin_state", "admin_state_up", off="--disable"),
            boolean("shared", "shared", on="--share"),
            repeated("az_hints", "--availability-zone-hint",
                     "availability_zone_hints"),
            arg("net_name"),
        ],
        "then": [
            {
                "when": ["with_subnet"],
                # Two action classes, posted together as one form. Neither
                # alone holds all the fields below.
                "form": [PROJ + "networks.workflows:CreateSubnetInfoAction",
                         PROJ + "networks.workflows"
                                ":CreateSubnetDetailAction"],
                "command": ["openstack", "subnet", "create"],
                "method": "POST",
                "endpoint": NETWORK + "/subnets",
                "envelope": "subnet",
                # ipv6_modes is unmapped, and uncovered() lists it. Horizon
                # sends it only for an IPv6 subnet, and splits one menu value
                # on "/" into two separate attributes. Two flags from one
                # field, conditional on a second field: more machinery than
                # one optional select is worth. See Known limits.
                "fields": [
                    parent("net_name", "--network", api="network_id"),
                    opt("cidr", "--subnet-range"),
                    opt("ip_version", "--ip-version", cast="int"),
                    opt("subnetpool", "--subnet-pool", api="subnetpool_id"),
                    # Sent only alongside a pool, which is the only thing it
                    # could apply to.
                    only(opt("prefixlen", "--prefix-length", cast="int"),
                         when=["subnetpool"]),
                    # "Disable Gateway" hides this input rather than removing
                    # it, so a gateway typed before the box was ticked is
                    # still posted. Without the gate the command would carry
                    # two --gateway flags.
                    only(opt("gateway_ip", "--gateway"),
                         unless=["no_gateway"]),
                    choice("no_gateway", "gateway_ip", {
                        "on": ("--gateway none", None),
                    }),
                    boolean("enable_dhcp", "enable_dhcp",
                            on="--dhcp", off="--no-dhcp"),
                    lines("dns_nameservers", "--dns-nameserver",
                          "dns_nameservers"),
                    pairs("allocation_pools", "--allocation-pool",
                          "allocation_pools", ("start", "end")),
                    pairs("host_routes", "--host-route", "host_routes",
                          ("destination", "gateway"),
                          api_keys=("destination", "nexthop")),
                    arg("subnet_name"),
                ],
            },
        ],
    },
    {
        "id": "subnet-create",
        "title": "Create subnet",
        # Both dashboards: the admin workflow subclasses the project one and
        # the project one subclasses the network wizard's step, so all three
        # share these field names. Validating against the admin classes
        # covers both, their base_fields being the superset.
        "url": r"/(admin|project)/networks/(?P<network>[^/]+)"
               r"/subnets/create/?$",
        "form": [ADMIN + "networks.subnets.workflows:CreateSubnetInfoAction",
                 PROJ + "networks.workflows:CreateSubnetDetailAction"],
        "routes": [
            "horizon:admin:networks:createsubnet",
            "horizon:project:networks:createsubnet",
        ],
        "method": "POST",
        "endpoint": NETWORK + "/subnets",
        "envelope": "subnet",
        "command": ["openstack", "subnet", "create"],
        # The same fields the network wizard's subnet step carries, with one
        # difference: the network already exists, so its id comes out of the
        # path rather than standing in for something not created yet.
        # Three unmapped, and uncovered() lists them. with_subnet is
        # declared and hidden here, meaning nothing to a panel whose whole
        # job is the subnet. address_source is the manual-or-pool switch
        # that decides which of cidr and subnetpool the form shows; the
        # rule reads the result rather than the switch. ipv6_modes is in
        # Known limits.
        "fields": [
            captured("network", "--network", api="network_id"),
            opt("cidr", "--subnet-range"),
            opt("ip_version", "--ip-version", cast="int"),
            opt("subnetpool", "--subnet-pool", api="subnetpool_id"),
            only(opt("prefixlen", "--prefix-length", cast="int"),
                 when=["subnetpool"]),
            only(opt("gateway_ip", "--gateway"), unless=["no_gateway"]),
            choice("no_gateway", "gateway_ip", {
                "on": ("--gateway none", None),
            }),
            boolean("enable_dhcp", "enable_dhcp",
                    on="--dhcp", off="--no-dhcp"),
            lines("dns_nameservers", "--dns-nameserver", "dns_nameservers"),
            pairs("allocation_pools", "--allocation-pool",
                  "allocation_pools", ("start", "end")),
            pairs("host_routes", "--host-route", "host_routes",
                  ("destination", "gateway"),
                  api_keys=("destination", "nexthop")),
            arg("subnet_name"),
        ],
    },
    {
        "id": "subnet-update",
        "title": "Update subnet",
        "url": r"/(admin|project)/networks/(?P<network>[^/]+)"
               r"/subnets/(?P<id>[^/]+)/update/?$",
        "form": [ADMIN + "networks.subnets.workflows:UpdateSubnetInfoAction",
                 PROJ + "networks.subnets.workflows:UpdateSubnetDetailAction"],
        "routes": [
            "horizon:admin:networks:editsubnet",
            "horizon:project:networks:editsubnet",
        ],
        "method": "PUT",
        "endpoint": NETWORK + "/subnets/{id}",
        "envelope": "subnet",
        "command": ["openstack", "subnet", "set"],
        # cidr and ip_version are on the form and never sent: the first is
        # rendered readonly and the second hidden, because Neutron will not
        # change either. uncovered() lists them, along with the fields this
        # workflow hides outright -- address_source, subnetpool, prefixlen,
        # ipv6_modes -- and with_subnet, inherited from the create action
        # and meaningless here.
        #
        # The network is in the path but takes no part: "subnet set" names
        # the subnet, and the subnet cannot move between networks. Only the
        # id is read, which is why this rule has a target and no captured.
        "fields": [
            opt("subnet_name", "--name", api="name"),
            only(opt("gateway_ip", "--gateway"), unless=["no_gateway"]),
            choice("no_gateway", "gateway_ip", {
                "on": ("--gateway none", None),
            }),
            boolean("enable_dhcp", "enable_dhcp",
                    on="--dhcp", off="--no-dhcp"),
            # Horizon sends these two as an empty list on an edit whether or
            # not anything was typed, so an empty box clears them. Allocation
            # pools are the exception: that one it sends only when set, so
            # emptying the box there says nothing and the rule says nothing.
            lines("dns_nameservers", "--dns-nameserver", "dns_nameservers",
                  clear="--no-dns-nameservers"),
            pairs("host_routes", "--host-route", "host_routes",
                  ("destination", "gateway"),
                  api_keys=("destination", "nexthop"),
                  clear="--no-host-route"),
            pairs("allocation_pools", "--allocation-pool",
                  "allocation_pools", ("start", "end")),
            target(),
        ],
    },
    {
        "id": "network-update-project",
        "title": "Update network",
        # The other half of the pair. Three fields rather than four: this
        # form offers no external box, so nothing here ever says anything
        # about external routing, which is exactly the point of keeping the
        # two apart.
        "url": r"/project/networks/(?P<id>[^/]+)/update/?$",
        "form": "openstack_dashboard.dashboards.project.networks.forms"
                ":UpdateNetwork",
        "routes": ["horizon:project:networks:update"],
        "method": "PUT",
        "endpoint": NETWORK + "/networks/{id}",
        "envelope": "network",
        "command": ["openstack", "network", "set"],
        # Both sides of each box, for the reason the admin rule gives:
        # handle() builds its params from every field on every save.
        #
        # shared is the one conditional field. handle() sends it only where
        # the update_network:shared policy passes, because Neutron answers
        # 403 otherwise. It passes for the admins Astrolabe records, who are
        # the only people it records, so the rule maps it. For anyone else
        # the form hides the widget rather than dropping it, and a hidden
        # BooleanField posts "True" or "False", both of which read correctly
        # here -- but no such submission is ever recorded to begin with.
        "fields": [
            opt("name", "--name"),
            boolean("admin_state", "admin_state_up",
                    on="--enable", off="--disable"),
            boolean("shared", "shared", on="--share", off="--no-share"),
            target(),
        ],
    },
    {
        "id": "aggregate-create",
        "title": "Create host aggregate",
        "url": r"/admin/aggregates/create/?$",
        "form": "openstack_dashboard.dashboards.admin.aggregates.workflows"
                ":SetAggregateInfoAction",
        "routes": ["horizon:admin:aggregates:create"],
        "method": "POST",
        "endpoint": COMPUTE + "/os-aggregates",
        "envelope": "aggregate",
        "command": ["openstack", "aggregate", "create"],
        "creates": "$NEW_AGGREGATE_ID",
        "fields": [
            opt("availability_zone", "--zone"),
            arg("name"),
        ],
        # The workflow's second step adds hosts, one Nova call each.
        "then": [
            {
                # A MembershipAction builds its field in __init__ rather than
                # declaring it, so base_fields has never heard of it and
                # validate() cannot look it up the usual way. It checks the
                # name against Horizon's own get_member_field_name instead,
                # which is where this spelling comes from.
                "form": ADMIN + "aggregates.workflows"
                                ":AddHostsToAggregateAction",
                "per": "add_host_to_aggregate_role_member",
                "command": ["openstack", "aggregate", "add", "host"],
                "method": "POST",
                "endpoint": COMPUTE + "/os-aggregates/{new}/action",
                "envelope": "add_host",
                "fields": [
                    # The aggregate, by the name the operator just typed.
                    # api=None because this names the resource being added
                    # to, and Nova's add_host body carries only the host.
                    arg("name", api=None),
                    item(api="host"),
                ],
            },
        ],
    },
    {
        "id": "aggregate-update",
        "title": "Update host aggregate",
        "url": r"/admin/aggregates/(?P<id>[^/]+)/update/?$",
        "form": "openstack_dashboard.dashboards.admin.aggregates.forms"
                ":UpdateAggregateForm",
        "routes": ["horizon:admin:aggregates:update"],
        "method": "PUT",
        "endpoint": COMPUTE + "/os-aggregates/{id}",
        "envelope": "aggregate",
        "command": ["openstack", "aggregate", "set"],
        # Nothing is missing here, unlike the create rule: hosts are added and
        # removed by a panel of their own, which this form does not touch.
        "fields": [
            opt("name", "--name"),
            # Nova will not let a zone be cleared once it is set, so Horizon
            # only ever sends a value, and a blank one is already skipped.
            opt("availability_zone", "--zone"),
            target(),
        ],
    },
    {
        "id": "router-create",
        "title": "Create router",
        # Both dashboards, because the admin form subclasses the project one
        # and adds nothing but tenant_id. A submission from the project side
        # simply does not carry that field, and an absent value is already
        # skipped. Validating against the admin class therefore covers both:
        # its base_fields are a superset. The cost is that a project-side
        # command names no project, because none was submitted — Horizon uses
        # whatever scope the operator is in. See Known limits.
        "url": r"/(admin|project)/routers/create/?$",
        "form": "openstack_dashboard.dashboards.admin.routers.forms"
                ":CreateForm",
        "routes": [
            "horizon:admin:routers:create",
            "horizon:project:routers:create",
        ],
        "method": "POST",
        "endpoint": NETWORK + "/routers",
        "envelope": "router",
        "command": ["openstack", "router", "create"],
        # enable_snat is deliberately unmapped, and uncovered() lists it.
        # Not for want of a way to say "only alongside a gateway network":
        # only() does exactly that. The blocker is a second one. Horizon
        # deletes this field outright where Neutron has no ext-gw-mode
        # extension, and a deleted checkbox posts what an unticked one
        # posts, which is nothing. Emitting --disable-snat on that evidence
        # would announce a change the operator never made, on a cloud that
        # could not have made it. The ticked case is safe and says only what
        # the CLI already defaults to. See Known limits, and redacted() for
        # the same trap.
        "fields": [
            opt("tenant_id", "--project"),
            # Defaults to up; absence means the operator unticked it.
            boolean("admin_state_up", "admin_state_up", off="--disable"),
            opt("external_network", "--external-gateway",
                api="external_gateway_info.network_id"),
            # Both of these offer "server_default", which Horizon reads as
            # "send no key at all"; choice() expresses that by omission.
            choice("mode", "distributed", {
                "centralized": ("--centralized", False),
                "distributed": ("--distributed", True),
            }),
            choice("ha", "ha", {
                "enabled": ("--ha", True),
                "disabled": ("--no-ha", False),
            }),
            repeated("az_hints", "--availability-zone-hint",
                     "availability_zone_hints"),
            arg("name"),
        ],
    },
    {
        "id": "router-update",
        "title": "Update router",
        # Both dashboards again: the admin form subclasses the project one and
        # overrides nothing but redirect_url, so the two submissions are
        # identical and one rule describes both.
        "url": r"/(admin|project)/routers/(?P<id>[^/]+)/update/?$",
        "form": "openstack_dashboard.dashboards.admin.routers.forms"
                ":UpdateForm",
        "routes": [
            "horizon:admin:routers:update",
            "horizon:project:routers:update",
        ],
        "method": "PUT",
        "endpoint": NETWORK + "/routers/{id}",
        "envelope": "router",
        "command": ["openstack", "router", "set"],
        # ha is deliberately unmapped, and uncovered() lists it. The form
        # declares it and then deletes it in __init__ every single time --
        # Neutron has not allowed it on a PUT since bug 1378525 -- so it is
        # never submitted, and a boolean naming its off side would put --no-ha
        # on every router edit. base_fields cannot see a field deleted at
        # runtime, so validate() would not catch that either.
        "fields": [
            opt("name", "--name"),
            boolean("admin_state", "admin_state_up",
                    on="--enable", off="--disable"),
            # Present only where the deployment permits DVR, and offering no
            # "server default": handle sends the key only in that case, which
            # is already what an unlisted value means to choice().
            choice("mode", "distributed", {
                "centralized": ("--centralized", False),
                "distributed": ("--distributed", True),
            }),
            target(),
        ],
    },
    {
        "id": "project-create",
        "title": "Create project",
        # Projects are the identity dashboard's default panel, so the panel
        # slug does not appear in the path.
        "url": r"/identity/create/?$",
        "form": "openstack_dashboard.dashboards.identity.projects.workflows"
                ":CreateProjectInfoAction",
        "routes": ["horizon:identity:projects:create"],
        "method": "POST",
        "endpoint": IDENTITY + "/projects",
        "envelope": "project",
        "command": ["openstack", "project", "create"],
        "fields": [
            # The form shows domain_name and submits domain_id; only the id
            # means anything to the API, and --domain accepts either.
            opt("domain_id", "--domain"),
            opt("description", "--description"),
            # Ships ticked. Only the unticked case needs saying.
            boolean("enabled", "enabled", off="--disable"),
            arg("name"),
        ],
    },
    {
        "id": "project-update",
        # Loosest pattern in the table, because projects are the identity
        # dashboard's default panel and their paths carry no slug. It stays
        # exact all the same: every other identity panel puts its slug between
        # /identity/ and the id, which is one segment more than [^/]+ allows.
        "title": "Update project",
        "url": r"/identity/(?P<id>[^/]+)/update/?$",
        "form": "openstack_dashboard.dashboards.identity.projects.workflows"
                ":UpdateProjectInfoAction",
        "routes": ["horizon:identity:projects:update"],
        "method": "PATCH",
        "endpoint": IDENTITY + "/projects/{id}",
        "envelope": "project",
        "command": ["openstack", "project", "set"],
        # The workflow's other steps edit member and group role assignments,
        # which are one API call per change against a membership list
        # Astrolabe never sees. Only the first step is described. See Known
        # limits.
        #
        # domain_id and domain_name are unmapped, and uncovered() lists them.
        # Keystone does not let a project change domain, and --domain on
        # "project set" only disambiguates a name, which this command does not
        # use: it names the id.
        "fields": [
            opt("name", "--name"),
            opt("description", "--description", clearable=True),
            boolean("enabled", "enabled", on="--enable", off="--disable"),
            target(),
        ],
    },
    {
        "id": "domain-create",
        "title": "Create domain",
        "url": r"/identity/domains/create/?$",
        "form": "openstack_dashboard.dashboards.identity.domains.workflows"
                ":CreateDomainInfoAction",
        "routes": ["horizon:identity:domains:create"],
        "method": "POST",
        "endpoint": IDENTITY + "/domains",
        "envelope": "domain",
        "command": ["openstack", "domain", "create"],
        "fields": [
            opt("description", "--description"),
            boolean("enabled", "enabled", off="--disable"),
            arg("name"),
        ],
    },
    {
        "id": "domain-update",
        "title": "Update domain",
        "url": r"/identity/domains/(?P<id>[^/]+)/update/?$",
        "form": "openstack_dashboard.dashboards.identity.domains.workflows"
                ":UpdateDomainInfoAction",
        "routes": ["horizon:identity:domains:update"],
        "method": "PATCH",
        "endpoint": IDENTITY + "/domains/{id}",
        "envelope": "domain",
        "command": ["openstack", "domain", "set"],
        # As with the project workflow, the user and group steps are role
        # assignments made one call at a time. See Known limits.
        "fields": [
            opt("name", "--name"),
            opt("description", "--description", clearable=True),
            boolean("enabled", "enabled", on="--enable", off="--disable"),
            target(),
        ],
    },
    {
        "id": "group-create",
        "title": "Create group",
        "url": r"/identity/groups/create/?$",
        "form": "openstack_dashboard.dashboards.identity.groups.forms"
                ":CreateGroupForm",
        "routes": ["horizon:identity:groups:create"],
        "method": "POST",
        "endpoint": IDENTITY + "/groups",
        "envelope": "group",
        "command": ["openstack", "group", "create"],
        "fields": [
            opt("description", "--description"),
            arg("name"),
        ],
    },
    {
        "id": "group-update",
        "title": "Update group",
        "url": r"/identity/groups/(?P<id>[^/]+)/update/?$",
        "form": "openstack_dashboard.dashboards.identity.groups.forms"
                ":UpdateGroupForm",
        "routes": ["horizon:identity:groups:update"],
        "method": "PATCH",
        "endpoint": IDENTITY + "/groups/{id}",
        "envelope": "group",
        "command": ["openstack", "group", "set"],
        # The form submits group_id in a hidden field too, and uncovered()
        # lists it as unmapped. The path carries the same value, and the path
        # is where every other edit rule reads it from; one way in beats two.
        # It is also the checkable one, since validate() can confirm a capture
        # group still lands on the id and cannot confirm anything about a
        # hidden field's contents.
        "fields": [
            opt("name", "--name"),
            opt("description", "--description", clearable=True),
            target(),
        ],
    },
    {
        "id": "user-create",
        "title": "Create user",
        "url": r"/identity/users/create/?$",
        "form": "openstack_dashboard.dashboards.identity.users.forms"
                ":CreateUserForm",
        "routes": ["horizon:identity:users:create"],
        "method": "POST",
        "endpoint": IDENTITY + "/users",
        "envelope": "user",
        "command": ["openstack", "user", "create"],
        # Three form fields are deliberately unmapped, and uncovered() lists
        # them: confirm_password (never read), domain_name (display only,
        # domain_id is submitted beside it) and lock_password, which is a
        # checkbox rather than a credential but whose name matches the secret
        # filter, so read_fields drops it before a rule could see it.
        # Narrowing that filter to let one boolean through is a bad trade: the
        # cost of getting it wrong is a leaked password, and the gain is
        # --enable-lock-password. role_id is covered by the follow-up step.
        "creates": "$NEW_USER_ID",
        "fields": [
            opt("domain_id", "--domain"),
            opt("project", "--project", api="default_project_id"),
            opt("email", "--email"),
            opt("description", "--description"),
            redacted("password", "--password-prompt"),
            boolean("enabled", "enabled", off="--disable"),
            arg("name"),
        ],
        # Horizon grants the primary role in a second call, and only when a
        # project and a role were both chosen -- the two fields are optional
        # and mean nothing apart.
        "then": [
            {
                "when": ["project", "role_id"],
                "command": ["openstack", "role", "add"],
                "method": "PUT",
                # No envelope: Keystone grants a role with an empty PUT, and
                # the whole request is in the path.
                "envelope": None,
                "endpoint": IDENTITY + "/projects/{project}/users/{new}"
                                       "/roles/{role_id}",
                "fields": [
                    # The user by the name just typed, which is what makes
                    # this command runnable without knowing the new id.
                    opt("name", "--user"),
                    opt("project", "--project"),
                    arg("role_id", api=None),
                ],
            },
        ],
    },
    {
        "id": "user-update",
        "title": "Update user",
        "url": r"/identity/users/(?P<id>[^/]+)/update/?$",
        "form": "openstack_dashboard.dashboards.identity.users.forms"
                ":UpdateUserForm",
        "routes": ["horizon:identity:users:update"],
        "method": "PATCH",
        "endpoint": IDENTITY + "/users/{id}",
        "envelope": "user",
        "command": ["openstack", "user", "set"],
        # Four fields are unmapped, and uncovered() lists them. id comes from
        # the path instead; domain_id and domain_name are read-only on this
        # form and handle pops both before the call, because a user cannot
        # change domain and --domain on "user set" only disambiguates a name;
        # lock_password is dropped by read_fields for the reason user-create
        # already records.
        #
        # Horizon leaves project and description out of the PATCH unless the
        # operator actually edited them. Astrolabe cannot see changed_data, so
        # it renders them whenever they hold a value. The result sets a field
        # to what it already contains, which is a longer command rather than a
        # wrong one.
        "fields": [
            opt("name", "--name"),
            # Not clearable, unlike the two below it. Emptying the select
            # means "no primary project", and the CLI has no way to say that:
            # there is no "user unset" and no --no-project, while
            # --project '' would send the CLI looking for a project named "".
            # Rendering nothing is the honest answer. See Known limits.
            opt("project", "--project", api="default_project_id"),
            opt("email", "--email", clearable=True),
            opt("description", "--description", clearable=True),
            target(),
        ],
    },
    {
        "id": "role-create",
        "title": "Create role",
        # Only reachable when ANGULAR_FEATURES['roles_panel'] is False. It
        # defaults to True, in which case the panel POSTs to /api/* instead
        # and this rule simply never matches. It is here because the flag can
        # be turned off today, and because the Angular panel is on its way out.
        "url": r"/identity/roles/create/?$",
        "form": "openstack_dashboard.dashboards.identity.roles.forms"
                ":CreateRoleForm",
        "routes": ["horizon:identity:roles:create"],
        "method": "POST",
        "endpoint": IDENTITY + "/roles",
        "envelope": "role",
        "command": ["openstack", "role", "create"],
        "fields": [
            arg("name"),
        ],
    },
    {
        "id": "role-update",
        "title": "Update role",
        # Behind the same Angular flag as role-create, and reachable on the
        # same terms.
        "url": r"/identity/roles/(?P<id>[^/]+)/update/?$",
        "form": "openstack_dashboard.dashboards.identity.roles.forms"
                ":UpdateRoleForm",
        "routes": ["horizon:identity:roles:update"],
        "method": "PATCH",
        "endpoint": IDENTITY + "/roles/{id}",
        "envelope": "role",
        "command": ["openstack", "role", "set"],
        # A hidden id is submitted as well, and uncovered() lists it. The
        # path is where this rule reads it from, as on every other edit.
        "fields": [
            opt("name", "--name"),
            target(),
        ],
    },
]

# Horizon encodes table actions as "<table>__<action>[__<id>]" in a field named
# "action". Only tables listed here are recognised; deletes on anything else
# are ignored. Row actions and multi-select batch deletes both work.
#
# The key is the name Horizon gives the table, which is what arrives in the
# action field. "table" names the DataTable class it came from, so validate()
# can confirm the name is still that class's, the way "form" lets it confirm a
# rule's fields. Deletes are dashboard-independent -- a table keeps its name
# wherever it is used -- so one class is enough even where two dashboards
# render the same table.
TABLES = {
    "flavors": {
        "noun": "flavor", "path": COMPUTE + "/flavors",
        "table": ADMIN + "flavors.tables:FlavorsTable",
    },
    "volume_types": {
        "noun": "volume type", "path": VOLUME + "/types",
        "table": ADMIN + "volume_types.tables:VolumeTypesTable",
    },
    "networks": {
        "noun": "network", "path": NETWORK + "/networks",
        "table": ADMIN + "networks.tables:NetworksTable",
    },
    "routers": {
        "noun": "router", "path": NETWORK + "/routers",
        "table": ADMIN + "routers.tables:RoutersTable",
    },
    "host_aggregates": {
        "noun": "aggregate", "path": COMPUTE + "/os-aggregates",
        "table": ADMIN + "aggregates.tables:HostAggregatesTable",
    },
    # Horizon still calls the project table "tenants", the pre-Keystone-v3
    # name. The CLI noun and the API path are both "project".
    "tenants": {
        "noun": "project", "path": IDENTITY + "/projects",
        "table": IDENT + "projects.tables:TenantsTable",
    },
    "domains": {
        "noun": "domain", "path": IDENTITY + "/domains",
        "table": IDENT + "domains.tables:DomainsTable",
    },
    "groups": {
        "noun": "group", "path": IDENTITY + "/groups",
        "table": IDENT + "groups.tables:GroupsTable",
    },
    "roles": {
        "noun": "role", "path": IDENTITY + "/roles",
        "table": IDENT + "roles.tables:RolesTable",
    },
    "users": {
        "noun": "user", "path": IDENTITY + "/users",
        "table": IDENT + "users.tables:UsersTable",
    },
}


# ---------------------------------------------------------------- validation


def _load_form(target):
    module_name, class_name = target.split(":")
    return getattr(import_module(module_name), class_name)


def _stand_ins(count):
    """Placeholders to reverse a panel's URL with, one per capture group.

    Distinct from each other, so a group that caught the wrong segment of
    the path is visible rather than coincidentally right. Two sets, because
    Horizon's id patterns are usually ``[^/]+`` but occasionally numeric,
    and a placeholder the pattern rejects would look exactly like a panel
    that moved.
    """
    return [
        tuple("%08x-0000-4000-8000-%012x" % (n + 1, n + 1)
              for n in range(count)),
        tuple(str(n + 1) for n in range(count)),
    ]


def _reverse_route(name, count):
    """The path a URL name resolves to, and the ids it was reversed with.

    ``(None, ())`` if it no longer resolves at all.
    """
    from django.urls import NoReverseMatch
    from django.urls import reverse

    for args in _stand_ins(count) if count else [()]:
        try:
            return reverse(name, args=args), args
        except NoReverseMatch:
            continue
    return None, ()


def _capture_groups(pattern):
    """The named groups a rule's url pattern declares, in path order."""
    return re.findall(r"\(\?P<(\w+)>", pattern)


def _check_routes(form, problems):
    """Confirm a rule's url pattern still reaches the panel it names.

    Checking fields is not enough. If Horizon moves a panel's path, every
    field on the form is still exactly where the rule says, and the rule
    simply stops matching -- no warning, no failure, nothing recorded. This is
    the half of the safety net that notices.

    Reversing the URL name rather than hard-coding a path is the point: the
    name is Horizon's stable handle, the path is the thing allowed to move.

    A panel whose path carries ids is reversed with stand-ins, one per group
    the rule declares, and each group is then checked to have caught its own.
    A pattern that matches the path while capturing the wrong part of it
    builds a command against the wrong resource, which is a worse outcome
    than not matching at all.

    The groups are paired with the stand-ins in path order, which holds
    because a rule names every variable segment of the path it matches. One
    that named only some would report here rather than pass quietly, which
    is the right direction for the mistake to fail in.
    """
    groups = _capture_groups(form["url"])
    for name in form["routes"]:
        path, stand_ins = _reverse_route(name, len(groups))
        if path is None:
            problems.append(
                "%s: %s no longer reverses; the rule cannot fire"
                % (form["id"], name))
            continue
        match = re.search(form["url"], path)
        if not match:
            problems.append(
                "%s: %s is now %s, which %r does not match"
                % (form["id"], name, path, form["url"]))
            continue
        for group, stand_in in zip(groups, stand_ins):
            if match.group(group) != stand_in:
                problems.append(
                    "%s: %s is now %s, where %r captures %r as %s rather "
                    "than the resource" % (form["id"], name, path,
                                           form["url"], match.group(group),
                                           group))


def _check_tables(problems):
    """Confirm each delete rule's table still goes by the name we match on."""
    for key, table in TABLES.items():
        target = table["table"]
        try:
            table_class = _load_form(target)
        except Exception as exc:  # noqa: BLE001 - reported, never raised
            problems.append(
                "table %s: cannot import %s (%s)" % (key, target, exc))
            continue
        actual = getattr(getattr(table_class, "_meta", None), "name", None)
        if actual != key:
            problems.append(
                "table %s: %s now calls itself %r, so deletes there stop "
                "being recorded" % (key, target, actual))


def _member_action(step, label, problems):
    """The class a repeating step reads its multi-select from."""
    targets = form_targets(step)
    if len(targets) != 1:
        problems.append(
            "%s: a step with per= must name exactly one form, so %r can be "
            "checked; it names %d" % (label, step["per"], len(targets)))
        return None
    return targets[0]


def _check_member_field(step, label, problems):
    """Confirm a repeating step still knows what its list is called.

    A MembershipAction builds its multi-select in ``__init__`` from its own
    slug, so the name never appears in ``base_fields`` and the usual check
    cannot see it. Horizon's own ``get_member_field_name`` is asked instead,
    with the class standing in for the instance, which it can do because the
    slug the method reads is a class attribute. That tracks upstream's naming
    rather than copying it: if the scheme changes, this says so.
    """
    target = _member_action(step, label, problems)
    if target is None:
        return
    try:
        action = _load_form(target)
        actual = action.get_member_field_name(action, "member")
    except Exception as exc:  # noqa: BLE001 - reported, never raised
        problems.append(
            "%s: cannot derive the member field of %s (%s)"
            % (label, target, exc))
        return
    if actual != step["per"]:
        problems.append(
            "%s: %s now posts its selection as %r, not %r, so the follow-up "
            "calls stop being recorded" % (label, target, actual, step["per"]))


def _check_fields(spec, label, known, problems):
    """Confirm every field a spec reads is still on the form it came from."""
    for field in spec["fields"]:
        if field["kind"] in UNSUBMITTED:
            continue
        if field["field"] not in known:
            problems.append(
                "%s: field %r is no longer on the form"
                % (label, field["field"]))


def _known_fields(targets, label, problems):
    """The field names of one or more form classes, as one set.

    More than one because a workflow posts every step at once: a follow-up
    step's fields may be declared on its own action class or on the one the
    rule targets, and from the submission they are indistinguishable.
    """
    known = set()
    for target in targets:
        try:
            form_class = _load_form(target)
        except Exception as exc:  # noqa: BLE001 - reported, never raised
            problems.append(
                "%s: cannot import %s (%s)" % (label, target, exc))
            return None
        known |= set(getattr(form_class, "base_fields", {}))
    if not known:
        problems.append(
            "%s: %s exposes no base_fields" % (label, ", ".join(targets)))
        return None
    return known


def validate():
    """Check every rule still describes the Horizon it targets.

    Three things can drift independently, so all three are checked: the form
    class and its field names, the URL the rule matches on, and the table name
    deletes arrive under. A rule can be perfectly correct about its fields and
    still never fire.

    Follow-up steps are checked the same way, plus the one thing peculiar to
    them: a repeating step's multi-select is named at runtime rather than
    declared, so its name is re-derived instead of looked up.

    Returns a list of human-readable problems, empty when all is well. Imports
    Horizon lazily, so this module remains usable without a dashboard present.
    """
    problems = []
    _check_tables(problems)
    for form in FORMS:
        _check_routes(form, problems)
        if not form.get("form"):
            continue
        for step in specs(form):
            label = form["id"]
            targets = form_targets(form)
            if step is not form:
                label = "%s step %s" % (form["id"], cli_name(step["command"]))
                if step.get("per"):
                    _check_member_field(step, label, problems)
                targets = targets + form_targets(step)
            known = _known_fields(targets, label, problems)
            if known is not None:
                _check_fields(step, label, known, problems)
    return problems


def uncovered():
    """Form fields no rule mentions, keyed by rule id.

    Informational rather than a problem: plenty of fields are deliberately
    unmapped (Horizon's ``with_subnet`` decides whether a second call
    happens rather than describing anything). Useful when reviewing a rule
    after a Horizon upgrade.

    A follow-up step's own form is examined too, under a key of its own.
    What a workflow posts is flat, but which class a field came from is
    still the thing a reviewer wants to see.
    """
    report = {}
    for form in FORMS:
        if not form.get("form"):
            continue
        for spec in specs(form):
            label = form["id"]
            targets = form_targets(form)
            if spec is not form:
                label = "%s step %s" % (form["id"], cli_name(spec["command"]))
                targets = form_targets(spec)
                if not targets:
                    continue
            known = set()
            try:
                for target in targets:
                    known |= set(getattr(_load_form(target),
                                         "base_fields", {}))
            except Exception:  # noqa: BLE001 - validate() reports this
                continue
            # Read across every spec, not just this one. A workflow posts
            # its steps as one flat submission, so a field declared on this
            # class may well be consumed by a different step -- the user
            # form's role_id is read by the role_add step, not by the rule.
            mapped = {field["field"] for one in specs(form)
                      for field in one["fields"]
                      if field["kind"] not in UNSUBMITTED}
            missing = sorted(known - mapped)
            if missing:
                report[label] = missing
    return report


# ------------------------------------------------------- the other upstream
#
# A rule straddles two projects. validate() watches the Horizon side, where
# drift stops a rule from firing or from reading a field. This watches the
# other side, where drift leaves the rule firing perfectly and rendering a
# command that no longer exists -- which an operator only finds out by pasting
# it into a shell. Nothing about the dashboard would look wrong.
#
# python-openstackclient is not a dependency and is not needed at runtime.
# These functions import it where they stand, and the test layer that calls
# them skips when it is absent, the same way the Horizon layer does.


#: The entry point groups openstackclient registers commands in, as
#: "openstack.<service>.v<N>". Several services still ship an older version
#: alongside the current one, so the number is read out and the highest wins:
#: Horizon talks Keystone v3 and Cinder v3, and checking a rule against the v2
#: parser would report flags missing that are perfectly real.
_CLI_GROUP = re.compile(r"^openstack\.[a-z_]+\.v(\d+)$")


def cli_name(command):
    """The entry point name openstackclient registers a command under.

    ``["openstack", "volume", "type", "set"]`` is ``volume_type_set``.
    """
    return "_".join(command[1:])


def flags(spec):
    """Every flag a rule or step can put on the command line.

    Spread across four keys, because a value carries one flag, a checkbox
    carries one per side and a select carries one per option.
    """
    found = []
    for field in spec["fields"]:
        for key in ("flag", "on", "off", "clear"):
            if field.get(key):
                found.append(field[key])
        for option in field.get("choices", {}).values():
            if option["flag"]:
                # A choice's flag may carry a fixed value; only the flag
                # itself is something a parser has heard of.
                found.append(option["flag"].split()[0])
    return found


def form_targets(spec):
    """The Horizon classes a rule or step was written against.

    Usually one. A follow-up step that spans two action classes of a wizard
    names both, because the submission merges them and neither alone holds
    all the fields.
    """
    target = spec.get("form")
    if not target:
        return []
    return [target] if isinstance(target, str) else list(target)


def takes_positional(spec):
    """Whether a spec ends its command with something, as all of them do."""
    return any(field["kind"] in ("positional", "target", "item")
               for field in spec["fields"])


def _entry_point_groups():
    """Entry points keyed by group, across the versions of the API.

    ``entry_points()`` returned a plain mapping until 3.10 and an object with
    ``select`` from then on. This package supports 3.9, so both are handled:
    on the old one the crash would be an AttributeError escaping verify_cli()
    rather than a problem it reports, which is the one outcome a checking
    function must not have.
    """
    from importlib.metadata import entry_points

    available = entry_points()
    if hasattr(available, "select"):
        return {group: available.select(group=group)
                for group in available.groups}
    return dict(available)


def _cli_commands():
    """Every openstack command name, mapped to its newest implementation."""
    found = {}
    for group, entries in _entry_point_groups().items():
        match = _CLI_GROUP.match(group)
        if not match:
            continue
        version = int(match.group(1))
        for entry in entries:
            seen = found.get(entry.name)
            if seen is None or version > seen[0]:
                found[entry.name] = (version, entry)
    return {name: entry for name, (_version, entry) in found.items()}


def _accepted(entry):
    """What a command's parser takes: its flags, and how many positionals.

    cliff builds the parser in ``get_parser``, which wants an invocation name
    and nothing else -- no cloud, no config, no network. argparse exposes the
    result only through ``_actions``; there is no public accessor, and an
    action with no option strings is a positional.

    Loading a command class makes osc_lib warn about one of its own modules
    being deprecated. That is upstream talking to upstream, nothing a reader
    of this report can act on, and left unfiltered it prints in the middle of
    an otherwise clean run. Silenced here only, and only for the load.
    """
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        parser = entry.load()(None, None).get_parser(entry.name)
    options = {string for action in parser._actions
               for string in action.option_strings}
    positionals = [action for action in parser._actions
                   if not action.option_strings]
    return options, len(positionals)


def _check_cli(name, label, wanted, needs_positional, commands, problems):
    """Confirm one command still exists and still takes what we give it."""
    entry = commands.get(name)
    if entry is None:
        problems.append(
            "%s: %r is no longer an openstack command" % (label, name))
        return
    try:
        options, positionals = _accepted(entry)
    except Exception as exc:  # noqa: BLE001 - reported, never raised
        problems.append(
            "%s: cannot read the parser for %r (%s)" % (label, name, exc))
        return
    for flag in wanted:
        if flag not in options:
            problems.append(
                "%s: openstack %s no longer accepts %s"
                % (label, name.replace("_", " "), flag))
    if needs_positional and not positionals:
        problems.append(
            "%s: openstack %s takes no positional argument, so the resource "
            "the rule names has nowhere to go"
            % (label, name.replace("_", " ")))


def verify_cli():
    """Check every rendered command against the real openstackclient.

    The counterpart to :func:`validate`. A renamed or withdrawn flag is
    invisible from the Horizon side: the rule still matches, still reads its
    field and still renders, and the command it renders fails only when
    somebody runs it.

    Returns a list of human-readable problems, empty when all is well.
    """
    problems = []
    commands = _cli_commands()
    for form in FORMS:
        for spec in specs(form):
            label = form["id"]
            if spec is not form:
                label = "%s step %s" % (form["id"], cli_name(spec["command"]))
            _check_cli(cli_name(spec["command"]), label, flags(spec),
                       takes_positional(spec), commands, problems)
    for key, table in TABLES.items():
        name = cli_name(["openstack"] + table["noun"].split() + ["delete"])
        _check_cli(name, "table %s" % key, [], True, commands, problems)
    return problems
