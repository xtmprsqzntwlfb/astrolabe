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
  extra runtime dependencies. The Python side is a rule table and one
  ``TemplateView`` subclass.
* **Django only.** No AngularJS. Horizon is moving toward removing Angular
  altogether, so Astrolabe hooks the Django form path exclusively and will
  outlive that removal.
* **Logic in Python.** What to translate, and how, lives in
  ``astrolabe/rules.py``. The JavaScript is a generic interpreter with no
  knowledge of any particular panel.
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

The rules themselves live in Python, in ``astrolabe/rules.py``. The header view
serialises them to JSON, the template embeds them with Django's ``json_script``
filter, and the JavaScript reads them from the page. Nothing is fetched at
action time, and no endpoint exists to fetch from::

    rules.py  ──serialise──▶  header view  ──json_script──▶  <script> in page
                                                                    │
                                        astrolabe.js interpreter ◀──┘

Keeping the rules in Python buys one thing JavaScript cannot have.
``rules.validate()`` imports the Horizon form each rule targets and checks the
field names still exist, so a rename in a future Horizon release surfaces as a
logged warning on first render rather than a silently incomplete command. The
check is deferred to first render (the dashboard modules are not reliably
importable while Django is still assembling the app registry) and its failures
are logged, never raised — a header section that throws costs the operator
their navigation bar.

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

Adding a panel means adding one entry to ``FORMS`` in ``astrolabe/rules.py``.
No JavaScript changes. A rule names the URL it matches, the command and
endpoint it maps to, and the fields it carries. Four field kinds cover
everything so far:

``opt(field, flag, api, cast, omit_when)``
    A value carried by a flag, ``--ram 2048``. ``omit_when`` drops the flag for
    a given value but keeps it in the REST body, which is how ``--swap 0``
    stays off the command line while ``swap: 0`` still reaches the API.

``boolean(field, api, on, off)``
    A checkbox. ``on`` is the flag emitted when ticked, ``off`` when not; either
    may be omitted. That covers both ``--share`` (emitted when ticked) and
    ``--disable`` (emitted when *not* ticked).

``repeated(field, flag, api)``
    A multi-select, emitted as the flag once per value.

``arg(field, api)``
    A positional argument. Always rendered last, as the CLI expects.

Set ``form`` to the Horizon class the rule targets, as
``"module:ClassName"``. That is what lets ``validate()`` check the rule against
the real form. You do not need to look the field names up by hand::

    >>> from astrolabe import rules
    >>> rules.validate()     # [] when every field still exists
    []
    >>> rules.uncovered()    # form fields no rule mentions
    {'network-create': ['with_subnet']}

If the declarative vocabulary cannot express a new panel, extend both
``rules.py`` and the interpreter's ``applyForm`` together, and add a case to
``tests/test_interpreter.js``.

Keep new rules admin-scoped for now. Project-scoped panels bring policy and
project-id questions that v1 deliberately avoids.

Tests
=====

Dependency-free: no npm install, no jsdom, no pytest required::

    python3 tests/test_rules.py

Three layers, in increasing order of what has to be present:

1. The rules are well formed and internally consistent. Needs nothing.
2. The JavaScript interpreter turns them into the expected commands. Needs
   ``node``. The **real serialised rules** are handed to
   ``tests/test_interpreter.js``, so the rule set has exactly one definition and
   the tests cannot drift from it.
3. The rules still match the Horizon forms they target. Needs a Horizon
   checkout, and is skipped with a note when one is not importable.

To include layer 3, run it with the interpreter that has Horizon on its path::

    cd ../horizon
    DJANGO_SETTINGS_MODULE=openstack_dashboard.test.settings PYTHONPATH=. \
        ./.tox/runserver/bin/python ../astrolabe/tests/test_rules.py

That layer includes a test that deliberately stages a renamed field and asserts
``validate()`` reports it, so the safety net cannot pass vacuously.

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
