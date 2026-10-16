=========
Changelog
=========

1.0.0 (2026-10-16)
==================

First release.

Astrolabe records the admin actions you perform in the OpenStack dashboard
and shows you the equivalent ``openstack`` command and ``curl`` call, so a
task done in the web UI can become a runbook entry without being
reconstructed by hand.

Recorded panels
---------------

31 actions across 14 resource types: flavors, volume types, networks,
subnets, routers, host aggregates, projects, domains, groups, roles, users,
security groups and floating IPs, plus deletes on twelve tables. Saving one
form can record more than one command where Horizon makes more than one call,
as it does for an aggregate created with hosts, a user given a primary role,
and a network created with a subnet.

What it does not do
-------------------

It adds no database tables, no API endpoints, no background services and no
JavaScript. It is admin-only and fails closed. It never reads a password, and
a rendered command carries placeholders rather than a real endpoint or token,
because these panels get screenshotted into tickets.

Several panels are deliberately left out, or left partly recorded, rather
than described wrongly. ``Known limits`` in the README says which and why.

Keeping up with upstream
------------------------

A rule can drift from either project it straddles, so both are checked.
``validate()`` confirms every rule still matches the Horizon form, URL and
table it was written against; ``verify_cli()`` confirms every command and
flag it renders still exists in ``python-openstackclient``. Each check stages
the failures it claims to catch, so neither can pass while testing nothing.

Requires Python 3.11 to 3.14, matching Horizon.
