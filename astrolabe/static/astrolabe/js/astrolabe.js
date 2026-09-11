/**
 * Astrolabe - show the CLI and REST equivalents of Horizon actions.
 *
 * Licensed under the Apache License, Version 2.0 (the "License"); you may
 * not use this file except in compliance with the License. You may obtain
 * a copy of the License at
 *
 *    http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
 * WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
 * License for the specific language governing permissions and limitations
 * under the License.
 *
 * How this works
 * --------------
 * Django Horizon submits its modal forms and its table row/batch actions as
 * ordinary form submissions (horizon.modals.js delegates `.modal form` submits
 * through $.ajax; table actions post the table's own form). A single capturing
 * `submit` listener therefore sees every create and delete the operator
 * performs, along with the exact field values they entered.
 *
 * Each submission is matched against the rule table below. A submission that
 * matches no rule is ignored and never stored - Astrolabe only ever retains
 * fields for forms it explicitly understands.
 *
 * Nothing here calls OpenStack. The recorder reads what the operator already
 * submitted and renders the equivalent `openstack` command and REST request.
 */

(function () {
  'use strict';

  var STORE_KEY = 'astrolabe.log';
  var MAX_ENTRIES = 50;

  // Field names whose values are never read, let alone stored.
  //
  // Long, unambiguous words match anywhere in the name - that is what catches
  // Django's run-together "csrfmiddlewaretoken". Short words match only on
  // boundaries, so "passthrough" and "keystone_url" are not dropped by
  // accident.
  var SECRET_ANYWHERE = /password|passwd|secret|token|credential/i;
  var SECRET_WORD = /(^|_)(pass|key|auth)(_|$)/i;

  function isSecret(name) {
    // Split camelCase into underscore-separated words first, so boundary
    // matching also catches names like Nova's "adminPass".
    var normalized = String(name).replace(/([a-z0-9])([A-Z])/g, '$1_$2');
    return SECRET_ANYWHERE.test(normalized) || SECRET_WORD.test(normalized);
  }

  // Service endpoints are not knowable from the browser, so commands are
  // rendered against these shell variables. Documented in the drawer footer.
  var COMPUTE = '$OS_COMPUTE_API';
  var VOLUME = '$OS_VOLUME_API';
  var NETWORK = '$OS_NETWORK_API';

  // ---------------------------------------------------------------- helpers

  /** Quote a value for a POSIX shell, leaving safe words bare. */
  function shq(value) {
    var s = value === null || value === undefined ? '' : String(value);
    if (s !== '' && /^[A-Za-z0-9_@%+=:,.\/-]+$/.test(s)) {
      return s;
    }
    return "'" + s.replace(/'/g, "'\\''") + "'";
  }

  function num(value) {
    if (value === null || value === undefined || value === '') {
      return null;
    }
    var n = Number(value);
    return isNaN(n) ? null : n;
  }

  /** Django renders unchecked booleans by omitting them entirely. */
  function checked(value) {
    return value === 'on' || value === 'true' || value === 'True';
  }

  /** Append `--flag value`, skipping empties and an optional default. */
  function flag(parts, name, value, skipWhen) {
    if (value === null || value === undefined || value === '') {
      return;
    }
    if (skipWhen !== undefined && String(value) === String(skipWhen)) {
      return;
    }
    parts.push(name, shq(value));
  }

  /** Drop null/undefined/'' so the rendered JSON body stays readable. */
  function prune(obj) {
    Object.keys(obj).forEach(function (k) {
      var v = obj[k];
      if (v === null || v === undefined || v === '') {
        delete obj[k];
      }
    });
    return obj;
  }

  function asList(value) {
    if (value === null || value === undefined || value === '') {
      return [];
    }
    return Array.isArray(value) ? value : [value];
  }

  // ------------------------------------------------------------ rule table
  //
  // Each rule: match(url, fields) -> bool, build(fields, url) -> record|null.
  // A record is { title, cli, calls: [{ method, url, body }] }.

  // Table actions encode themselves as "<table>__<action>[__<id>]" in a field
  // named "action". Only tables listed here are recognised.
  var TABLES = {
    flavors: { noun: 'flavor', path: COMPUTE + '/flavors' },
    volume_types: { noun: 'volume type', path: VOLUME + '/types' },
    networks: { noun: 'network', path: NETWORK + '/networks' }
  };

  var RULES = [
    {
      // Admin > Compute > Flavors > Create Flavor
      match: function (url) {
        return /\/admin\/flavors\/create\/?$/.test(url);
      },
      build: function (f) {
        var parts = ['openstack', 'flavor', 'create'];
        flag(parts, '--id', f.flavor_id);
        flag(parts, '--vcpus', f.vcpus);
        flag(parts, '--ram', f.memory_mb);
        flag(parts, '--disk', f.disk_gb);
        flag(parts, '--ephemeral', f.eph_gb, '0');
        flag(parts, '--swap', f.swap_mb, '0');
        parts.push(shq(f.name));
        return {
          title: 'Create flavor ' + (f.name || ''),
          cli: parts.join(' '),
          calls: [{
            method: 'POST',
            url: COMPUTE + '/flavors',
            body: {
              flavor: prune({
                name: f.name,
                id: f.flavor_id,
                vcpus: num(f.vcpus),
                ram: num(f.memory_mb),
                disk: num(f.disk_gb),
                'OS-FLV-EXT-DATA:ephemeral': num(f.eph_gb),
                swap: num(f.swap_mb)
              })
            }
          }]
        };
      }
    },
    {
      // Admin > Volume > Volume Types > Create Volume Type
      match: function (url) {
        return /\/admin\/volume_types\/create_type\/?$/.test(url);
      },
      build: function (f) {
        var parts = ['openstack', 'volume', 'type', 'create'];
        flag(parts, '--description', f.vol_type_description);
        // The form ships is_public checked by default; only the private case
        // needs a flag.
        if (!checked(f.is_public)) {
          parts.push('--private');
        }
        parts.push(shq(f.name));
        return {
          title: 'Create volume type ' + (f.name || ''),
          cli: parts.join(' '),
          calls: [{
            method: 'POST',
            url: VOLUME + '/types',
            body: {
              volume_type: prune({
                name: f.name,
                description: f.vol_type_description,
                'os-volume-type-access:is_public': checked(f.is_public)
              })
            }
          }]
        };
      }
    },
    {
      // Admin > Network > Networks > Create Network
      match: function (url) {
        return /\/admin\/networks\/create\/?$/.test(url);
      },
      build: function (f) {
        var parts = ['openstack', 'network', 'create'];
        flag(parts, '--project', f.tenant_id);
        flag(parts, '--provider-network-type', f.network_type);
        flag(parts, '--provider-physical-network', f.physical_network);
        flag(parts, '--provider-segment', f.segmentation_id);
        flag(parts, '--mtu', f.mtu);
        if (checked(f.shared)) {
          parts.push('--share');
        }
        if (checked(f.external)) {
          parts.push('--external');
        }
        // admin_state is a checkbox that defaults to up; absent means down.
        if (!checked(f.admin_state)) {
          parts.push('--disable');
        }
        asList(f.az_hints).forEach(function (az) {
          flag(parts, '--availability-zone-hint', az);
        });
        parts.push(shq(f.name));
        return {
          title: 'Create network ' + (f.name || ''),
          cli: parts.join(' '),
          calls: [{
            method: 'POST',
            url: NETWORK + '/networks',
            body: {
              network: prune({
                name: f.name,
                tenant_id: f.tenant_id,
                'provider:network_type': f.network_type,
                'provider:physical_network': f.physical_network,
                'provider:segmentation_id': num(f.segmentation_id),
                mtu: num(f.mtu),
                shared: checked(f.shared),
                'router:external': checked(f.external),
                admin_state_up: checked(f.admin_state),
                availability_zone_hints: asList(f.az_hints)
              })
            }
          }]
        };
      }
    },
    {
      // Table row and batch deletes, for any table named in TABLES.
      match: function (url, f) {
        return typeof f.action === 'string' &&
          /^[a-z0-9_]+__delete(__|$)/.test(f.action);
      },
      build: function (f) {
        var bits = f.action.split('__');
        var table = TABLES[bits[0]];
        if (!table) {
          return null;
        }
        // Row action carries the id in the third segment; batch action puts
        // the selected ids in repeated object_ids fields.
        var ids = bits.length > 2 ? [bits.slice(2).join('__')]
          : asList(f.object_ids);
        if (!ids.length) {
          return null;
        }
        return {
          title: 'Delete ' + table.noun + (ids.length > 1 ? 's' : '') +
            ' (' + ids.length + ')',
          cli: 'openstack ' + table.noun + ' delete ' +
            ids.map(shq).join(' '),
          calls: ids.map(function (id) {
            return {
              method: 'DELETE',
              url: table.path + '/' + id,
              body: null
            };
          })
        };
      }
    }
  ];

  // ------------------------------------------------------------- rendering

  function curlFor(call) {
    var parts = [
      'curl -sS -X ' + call.method + ' "' + call.url + '"',
      '-H "X-Auth-Token: $OS_TOKEN"'
    ];
    if (call.body) {
      parts.push('-H "Content-Type: application/json"');
      parts.push("-d '" + JSON.stringify(call.body).replace(/'/g, "'\\''") + "'");
    }
    return parts.join(' \\\n  ');
  }

  // --------------------------------------------------------------- storage

  function load() {
    try {
      return JSON.parse(window.sessionStorage.getItem(STORE_KEY)) || [];
    } catch (e) {
      return [];
    }
  }

  function save(entries) {
    try {
      window.sessionStorage.setItem(
        STORE_KEY, JSON.stringify(entries.slice(0, MAX_ENTRIES)));
    } catch (e) {
      // Storage disabled or over quota. Recording is best-effort and must
      // never interfere with the action the operator is performing.
    }
  }

  // --------------------------------------------------------------- capture

  function readFields(form, submitter) {
    var data;
    try {
      // The two-argument form folds in the clicked button's name/value, which
      // is how Horizon table actions carry "action".
      data = new window.FormData(form, submitter);
    } catch (e) {
      data = new window.FormData(form);
    }
    var fields = {};
    data.forEach(function (value, name) {
      if (isSecret(name)) {
        return;
      }
      if (typeof window.File !== 'undefined' && value instanceof window.File) {
        value = '<file:' + value.name + '>';
      }
      if (Object.prototype.hasOwnProperty.call(fields, name)) {
        fields[name] = asList(fields[name]).concat([value]);
      } else {
        fields[name] = value;
      }
    });
    // Older browsers ignore the second FormData argument.
    if (submitter && submitter.name && !(submitter.name in fields) &&
        !isSecret(submitter.name)) {
      fields[submitter.name] = submitter.value;
    }
    return fields;
  }

  function record(form, submitter) {
    var fields = readFields(form, submitter);
    var url = form.getAttribute('action') || window.location.pathname;
    for (var i = 0; i < RULES.length; i++) {
      if (!RULES[i].match(url, fields)) {
        continue;
      }
      var built = RULES[i].build(fields, url);
      if (!built) {
        return;
      }
      var entries = load();
      entries.unshift({
        at: Date.now(),
        title: built.title,
        cli: built.cli,
        calls: built.calls || []
      });
      save(entries);
      return;
    }
  }

  // ------------------------------------------------------------------- UI

  var STYLE = [
    '.astrolabe-toggle{cursor:pointer;white-space:nowrap}',
    '.astrolabe-count{display:inline-block;min-width:1.5em;margin-left:.4em;',
    'padding:0 .4em;border-radius:.8em;background:#337ab7;color:#fff;',
    'font-size:.75em;text-align:center}',
    '.astrolabe-drawer{position:fixed;top:0;right:0;bottom:0;width:41rem;',
    'max-width:100vw;z-index:1100;background:#fff;color:#333;',
    'box-shadow:-2px 0 8px rgba(0,0,0,.3);display:flex;flex-direction:column;',
    'font-size:13px}',
    '.astrolabe-drawer[hidden]{display:none}',
    '.astrolabe-head{display:flex;align-items:center;gap:.5rem;',
    'padding:.75rem 1rem;border-bottom:1px solid #ddd}',
    '.astrolabe-head h3{margin:0;font-size:15px;flex:1}',
    '.astrolabe-body{flex:1;overflow:auto;padding:.5rem 1rem}',
    '.astrolabe-foot{padding:.6rem 1rem;border-top:1px solid #ddd;',
    'color:#666;font-size:11px;line-height:1.5}',
    '.astrolabe-entry{border-bottom:1px solid #eee;padding:.75rem 0}',
    '.astrolabe-entry h4{margin:0 0 .35rem;font-size:13px;font-weight:600}',
    '.astrolabe-when{color:#888;font-weight:400;font-size:11px;',
    'margin-left:.5rem}',
    '.astrolabe-block{position:relative;margin:.35rem 0}',
    '.astrolabe-block pre{margin:0;padding:.5rem 4.5rem .5rem .6rem;',
    'background:#f5f5f5;border:1px solid #e3e3e3;border-radius:3px;',
    'white-space:pre-wrap;word-break:break-all;font-size:12px}',
    '.astrolabe-copy{position:absolute;top:.3rem;right:.3rem}',
    '.astrolabe-empty{color:#888;padding:1rem 0}'
  ].join('');

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) {
      node.className = className;
    }
    if (text !== undefined && text !== null) {
      node.textContent = text;
    }
    return node;
  }

  /** A copyable code block. Values are set as text, never as markup. */
  function block(label, text) {
    var wrap = el('div', 'astrolabe-block');
    var pre = el('pre', null, text);
    pre.setAttribute('aria-label', label);
    var copy = el('button', 'btn btn-default btn-xs astrolabe-copy', 'Copy');
    copy.type = 'button';
    copy.addEventListener('click', function () {
      var done = function () {
        copy.textContent = 'Copied';
        window.setTimeout(function () { copy.textContent = 'Copy'; }, 1200);
      };
      if (window.navigator.clipboard) {
        window.navigator.clipboard.writeText(text).then(done, function () {});
      } else {
        // http:// origins get no async clipboard; fall back to a selection.
        var area = document.createElement('textarea');
        area.value = text;
        document.body.appendChild(area);
        area.select();
        try { document.execCommand('copy'); done(); } catch (e) { /* noop */ }
        document.body.removeChild(area);
      }
    });
    wrap.appendChild(pre);
    wrap.appendChild(copy);
    return wrap;
  }

  function build(toggle) {
    var style = el('style');
    style.textContent = STYLE;
    document.head.appendChild(style);

    var drawer = el('div', 'astrolabe-drawer');
    drawer.hidden = true;
    drawer.setAttribute('role', 'dialog');
    drawer.setAttribute('aria-label', 'Astrolabe');

    var head = el('div', 'astrolabe-head');
    head.appendChild(el('h3', null, 'Astrolabe'));
    var clear = el('button', 'btn btn-default btn-xs', 'Clear');
    clear.type = 'button';
    var close = el('button', 'btn btn-default btn-xs', 'Close');
    close.type = 'button';
    head.appendChild(clear);
    head.appendChild(close);

    var body = el('div', 'astrolabe-body');

    var foot = el('div', 'astrolabe-foot', null);
    foot.appendChild(document.createTextNode(
      'Commands are rendered against $OS_TOKEN, ' + COMPUTE + ' (e.g. ' +
      'https://host/compute/v2.1), ' + VOLUME + ' (e.g. ' +
      'https://host/volume/v3/$OS_PROJECT_ID) and ' + NETWORK + ' (e.g. ' +
      'https://host:9696/v2.0). Your token is never written into a command. ' +
      'Recorded in this browser tab only.'));

    drawer.appendChild(head);
    drawer.appendChild(body);
    drawer.appendChild(foot);
    document.body.appendChild(drawer);

    var count = document.getElementById('astrolabe-count');

    function render() {
      var entries = load();

      if (count) {
        count.textContent = String(entries.length);
        count.hidden = entries.length === 0;
      }

      body.textContent = '';
      if (!entries.length) {
        body.appendChild(el('div', 'astrolabe-empty',
          'No actions recorded yet. Create or delete a flavor, volume type ' +
          'or network and it will show up here.'));
        return;
      }

      entries.forEach(function (entry) {
        var item = el('div', 'astrolabe-entry');
        var title = el('h4', null, entry.title);
        title.appendChild(el('span', 'astrolabe-when',
          new Date(entry.at).toLocaleTimeString()));
        item.appendChild(title);
        item.appendChild(block('CLI', entry.cli));
        (entry.calls || []).forEach(function (call) {
          item.appendChild(block('REST', curlFor(call)));
        });
        body.appendChild(item);
      });
    }

    function open() {
      render();
      drawer.hidden = false;
    }

    toggle.addEventListener('click', function (evt) {
      evt.preventDefault();
      // Horizon binds its own click handler to header sections to reorder
      // them; don't let a toggle click trigger that.
      evt.stopPropagation();
      if (drawer.hidden) { open(); } else { drawer.hidden = true; }
    });
    toggle.addEventListener('keydown', function (evt) {
      if (evt.key === 'Enter' || evt.key === ' ') {
        evt.preventDefault();
        toggle.click();
      }
    });
    close.addEventListener('click', function () { drawer.hidden = true; });
    clear.addEventListener('click', function () { save([]); render(); });

    document.addEventListener('keydown', function (evt) {
      if (evt.key === 'Escape' && !drawer.hidden) {
        drawer.hidden = true;
      }
    });

    // Capture phase, so the entry is stored before horizon.modals.js hands the
    // form to $.ajax and before a full-page action submit unloads us.
    document.addEventListener('submit', function (evt) {
      var form = evt.target;
      if (!form || form.tagName !== 'FORM') {
        return;
      }
      try {
        record(form, evt.submitter);
        if (!drawer.hidden) {
          render();
        } else if (count) {
          var n = load().length;
          count.textContent = String(n);
          count.hidden = n === 0;
        }
      } catch (e) {
        // Never let a recording bug block the operator's action.
        if (window.console && window.console.warn) {
          window.console.warn('astrolabe: could not record submission', e);
        }
      }
    }, true);

    render();
  }

  // Exposed for the dependency-free harness in tests/, and so an operator can
  // inspect what Astrolabe parsed from the browser console. Not an interface
  // anything else should depend on.
  window.astrolabe = {
    rules: RULES,
    curlFor: curlFor,
    readFields: readFields
  };

  // The header section arrives by AJAX after page load (see
  // horizon.extensible_header.js), and only for admins. Wait for it: no
  // marker means no drawer and no recording.
  function whenReady(callback) {
    var found = document.getElementById('astrolabe-toggle');
    if (found) {
      callback(found);
      return;
    }
    var observer = new window.MutationObserver(function () {
      var node = document.getElementById('astrolabe-toggle');
      if (node) {
        observer.disconnect();
        callback(node);
      }
    });
    observer.observe(document.documentElement, {
      childList: true, subtree: true
    });
  }

  var started = false;
  whenReady(function (toggle) {
    if (started) {
      return;
    }
    started = true;
    build(toggle);
  });
}());
