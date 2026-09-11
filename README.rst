=========
astrolabe
=========

An OpenStack Horizon plugin that shows the **CLI and REST equivalents of what
you just did in the dashboard**. Perform an admin action — create a flavor,
delete a network — and Astrolabe records the equivalent ``openstack`` command
and ``curl`` request, ready to copy into a script.

It exists to shorten the gap between clicking through Horizon and automating
the same work.

Design goals
============

* **No backend.** No models, no migrations, no API endpoints, no daemons, no
  extra runtime dependencies. The Python side is one
  ``TemplateView`` subclass whose entire body is a template name.
* **Django only.** No AngularJS. Horizon is moving toward removing Angular
  altogether, so Astrolabe hooks the Django form path exclusively and will
  outlive that removal.
* **Encapsulated.** It adds no dashboard and no panel, and patches nothing in
  Horizon. It contributes one JavaScript file and one header template.
* **Admin only.** The header template gates on ``request.user.is_superuser``,
  which openstack_auth derives from the operator's roles in the current scope.
  Non-admins never get the marker, so the recorder never activates for them.
* **Read-only, and offline.** Astrolabe calls no OpenStack API. It only reads
  form values the operator has already submitted, and renders text.

How it works
============

Django Horizon submits its modal forms and its table row/batch actions as
ordinary form submissions — ``horizon.modals.js`` delegates ``.modal form``
submits through ``$.ajax``, and table actions post the table's own form. A
single capturing ``submit`` listener therefore observes every create and delete
the operator performs, together with the exact values they entered.

Each submission is matched against a rule table. **A submission that matches no
rule is ignored and never stored** — Astrolabe only ever retains fields for the
handful of forms it explicitly understands.

On top of that, secret-looking field names are dropped before anything is read.
``password``, ``secret``, ``token`` and ``credential`` match anywhere in the
name (this is what catches Django's run-together ``csrfmiddlewaretoken``);
``pass``, ``key`` and ``auth`` match only on word boundaries, so ``passthrough``
and ``keystone_url`` survive. Names are split on camelCase first, so Nova's
``adminPass`` is caught too.

Entries live in ``sessionStorage``, capped at 50, scoped to the browser tab and
gone when it closes. Nothing is sent anywhere.

A note on ``/api/*``
--------------------

An earlier sketch of this plugin recorded Horizon's ``/api/nova/...`` REST proxy
traffic. That turns out to be incompatible with a Django-only design: every
caller of those endpoints lives in
``openstack_dashboard/static/app/core/openstack-service-api/``, which *is* the
AngularJS service layer. On a de-Angularized Horizon there is no ``/api/*``
traffic to record. Form submissions are the Django-native equivalent, and they
carry the operator's actual input rather than a proxy's reshaping of it.

What v1 covers
==============

Three admin panels, chosen because they are unambiguously admin-scoped (no
policy or project-scoping subtleties) and have flat, well-understood forms:

============================== ====================================
Action                         Rendered as
============================== ====================================
Create flavor                  ``openstack flavor create``
Create volume type             ``openstack volume type create``
Create network (admin)         ``openstack network create``
Delete, on any of those three  ``openstack <resource> delete``
============================== ====================================

Deletes are handled by one generic rule that decodes Horizon's
``<table>__<action>__<id>`` action encoding, so row actions and multi-select
batch deletes both work.

Each entry renders two things: the ``openstack`` command, and the equivalent
REST call as ``curl``.

Endpoints and the token
-----------------------

The browser cannot know your service catalog, so commands are rendered against
shell variables that you set once::

    export OS_TOKEN=$(openstack token issue -f value -c id)
    export OS_COMPUTE_API=https://your-cloud/compute/v2.1
    export OS_VOLUME_API=https://your-cloud/volume/v3/$OS_PROJECT_ID
    export OS_NETWORK_API=https://your-cloud:9696/v2.0

**Your Keystone token is never written into a rendered command** — only the
literal string ``$OS_TOKEN``. This is deliberate: these panels get screenshotted
into tickets.

Installation
============

These steps assume a **source checkout of Horizon** (e.g. devstack) with the
``horizon`` and ``astrolabe`` trees side by side::

    parent/
    ├── horizon/      # the Horizon source tree (has manage.py)
    └── astrolabe/    # this repo

A Horizon plugin is *not* copied into the ``horizon`` tree. ``pip install``
puts the ``astrolabe`` package on Horizon's Python path; a single "enabled" file
wires it in, and Horizon imports the rest from the installed package. The only
file that lands in the ``horizon`` tree is that enabled file.

Every command must use **the Python environment that runs your Horizon**. If
your checkout uses a virtualenv, activate it first; otherwise use whatever
``python``/``pip`` you launch Horizon with.

Run these from the parent directory that holds both trees.

1. **Install the plugin into Horizon's Python environment**::

     cd astrolabe
     pip install .

   Then confirm it imported into the right place::

     python -c "import astrolabe; print('ok', astrolabe.__file__)"

   You should see ``ok`` and a path ending in
   ``.../site-packages/astrolabe/__init__.py``. If it errors, the wrong
   ``pip``/``python`` was used — fix that before going on.

   (Use ``pip install -e .`` instead if you want edits in ``astrolabe`` to take
   effect on restart without reinstalling.)

2. **Register the plugin** — copy the one enabled file into Horizon's local
   enabled directory::

     cp astrolabe/enabled/_9020_astrolabe.py \
        ../horizon/openstack_dashboard/local/enabled/

   (The ``_9020_`` prefix controls load order; leave it unless it collides with
   an existing file.)

3. **Collect the static asset.** Unlike a panel that can inline its assets,
   Astrolabe's JavaScript has to reach *every* page, so it goes through
   ``ADD_JS_FILES`` and Horizon's static pipeline::

     cd ../horizon
     python manage.py collectstatic --noinput
     python manage.py compress --force

   The ``compress`` step is only needed when ``COMPRESS_OFFLINE`` is enabled
   (typical for a production deployment); the dev server does not require it.

4. **Restart Horizon.**

   * Dev server: stop it and re-run ``python manage.py runserver`` from the
     ``horizon`` dir.
   * Apache/mod_wsgi (packaged installs)::

       sudo systemctl restart httpd          # RHEL/CentOS/Fedora
       # or
       sudo systemctl restart apache2        # Ubuntu/Debian

5. **Verify.** Log in as an admin-capable user. An **Astrolabe** item appears in
   the top navigation bar. Go to *Admin > Compute > Flavors*, create a flavor,
   then click **Astrolabe** — the drawer opens with the equivalent
   ``openstack flavor create`` command and ``curl`` request.

   If the item does not appear, check that your user actually holds an admin
   role in the current scope, and that step 3 ran against the same checkout.

Uninstalling
============

Remove the enabled file and reinstall the static assets::

    rm ../horizon/openstack_dashboard/local/enabled/_9020_astrolabe.py
    pip uninstall astrolabe

Then re-run step 3 and restart Horizon.

Extending the rule table
========================

Adding a panel means adding one entry to ``RULES`` in
``astrolabe/static/astrolabe/js/astrolabe.js``. A rule is a ``match(url,
fields)`` predicate and a ``build(fields, url)`` that returns a title, a CLI
string and a list of REST calls. The field names come straight from the Django
form class — read them out of the relevant
``openstack_dashboard/dashboards/.../forms.py`` or ``workflows.py``.

Keep new rules admin-scoped for now. Project-scoped panels bring policy and
project-id questions that v1 deliberately avoids.

Tests
=====

The translation layer has a dependency-free test harness — no npm install, no
jsdom, just Node::

    node tests/test_rules.js

It stubs the DOM only far enough for the source to evaluate, then drives the
rules directly with the field names taken from Horizon's own form classes.

Known limits
============

* Only the four actions listed above are recognised. Everything else is
  silently ignored, by design.
* Astrolabe records what was *submitted*, not what succeeded. A form the API
  rejects still produces an entry.
* Panels switched to their AngularJS variants via ``ANGULAR_FEATURES`` (flavors
  has one) bypass the Django form path and are not recorded.
* The rendered ``curl`` targets the real service APIs, not Horizon's proxy, so
  it needs a token and endpoints from your own shell.

License
=======

Apache License 2.0. See ``LICENSE``.
