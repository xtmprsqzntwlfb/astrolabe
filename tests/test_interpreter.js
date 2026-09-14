/**
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
 * Tests for the JavaScript interpreter: rules plus form fields in, CLI and
 * REST out.
 *
 * The rules are not duplicated here. They are passed in as JSON produced by
 * astrolabe/rules.py, so these assertions run against the real rule set:
 *
 *     python3 tests/test_rules.py        # writes the JSON, then runs this
 *     node tests/test_interpreter.js <rules.json>
 *
 * Dependency-free on purpose - a Horizon plugin gets installed into whatever
 * environment the deployment already has. The DOM is stubbed only far enough
 * for the source file to evaluate.
 */

'use strict';

var assert = require('assert');
var fs = require('fs');
var path = require('path');
var vm = require('vm');

var rulesPath = process.argv[2];
if (!rulesPath) {
  console.error('usage: node tests/test_interpreter.js <rules.json>');
  console.error('(or just run: python3 tests/test_rules.py)');
  process.exit(2);
}
var rulesJson = fs.readFileSync(rulesPath, 'utf8');

// ---------------------------------------------------------------- stub DOM

/**
 * Minimal FormData, constructed from a fake form carrying `_pairs`.
 *
 * It deliberately ignores the second (submitter) argument, simulating a
 * browser without two-argument FormData. That exercises readFields' manual
 * submitter fallback, which is the fragile path of the two.
 */
function FakeFormData(form) {
  this._pairs = (form && form._pairs) || [];
}
FakeFormData.prototype.forEach = function (fn) {
  this._pairs.forEach(function (pair) { fn(pair[1], pair[0]); });
};

var sandbox = {
  console: console,
  setTimeout: setTimeout,
  Date: Date,
  JSON: JSON,
  Array: Array,
  Object: Object,
  Number: Number,
  RegExp: RegExp,
  isNaN: isNaN,
  document: {
    getElementById: function (id) {
      // The rule set is present, but the admin marker never is, so the UI
      // never builds. That is the non-admin path, and it keeps this harness
      // to the pure logic.
      return id === 'astrolabe-rules' ? { textContent: rulesJson } : null;
    },
    documentElement: {},
    createElement: function () {
      return {
        style: {}, setAttribute: function () {}, appendChild: function () {},
        addEventListener: function () {}
      };
    },
    addEventListener: function () {},
    head: { appendChild: function () {} },
    body: { appendChild: function () {}, removeChild: function () {} }
  },
  MutationObserver: function () {
    this.observe = function () {};
    this.disconnect = function () {};
  }
};
sandbox.window = sandbox;
sandbox.window.FormData = FakeFormData;
sandbox.window.File = function () {};
sandbox.window.MutationObserver = sandbox.MutationObserver;
sandbox.window.setTimeout = setTimeout;
sandbox.window.navigator = {};
sandbox.window.location = { pathname: '/' };
sandbox.window.sessionStorage = {
  _v: null,
  getItem: function () { return this._v; },
  setItem: function (k, v) { this._v = v; }
};

vm.createContext(sandbox);
vm.runInContext(
  fs.readFileSync(
    path.join(__dirname, '..', 'astrolabe', 'static', 'astrolabe', 'js',
      'astrolabe.js'), 'utf8'),
  sandbox);

var astrolabe = sandbox.window.astrolabe;
assert.ok(astrolabe, 'source did not evaluate');

var spec = astrolabe.loadSpec();
assert.ok(spec && spec.forms.length, 'rule set did not load');

function translate(url, fields) {
  return astrolabe.translate(spec, url, fields);
}

/**
 * Objects built inside the vm have a different Object.prototype, so
 * deepStrictEqual would reject them on realm alone. Compare values instead.
 */
function sameValue(actual, expected, message) {
  assert.deepStrictEqual(
    JSON.parse(JSON.stringify(actual)),
    JSON.parse(JSON.stringify(expected)), message);
}

// ------------------------------------------------------------------- tests

var tests = {};

tests['flavor create renders every flag'] = function () {
  var out = translate('/admin/flavors/create/', {
    name: 'm1.small', flavor_id: 'auto', vcpus: '1', memory_mb: '2048',
    disk_gb: '20', eph_gb: '5', swap_mb: '512'
  });
  assert.strictEqual(out.cli,
    'openstack flavor create --id auto --vcpus 1 --ram 2048 --disk 20 ' +
    '--ephemeral 5 --swap 512 m1.small');
  sameValue(out.calls[0].body.flavor, {
    id: 'auto', vcpus: 1, ram: 2048, disk: 20,
    'OS-FLV-EXT-DATA:ephemeral': 5, swap: 512, name: 'm1.small'
  });
  assert.strictEqual(out.calls[0].method, 'POST');
  assert.strictEqual(out.title, 'Create flavor m1.small');
};

tests['flavor create omits zero ephemeral and swap'] = function () {
  var out = translate('/admin/flavors/create/', {
    name: 'tiny', flavor_id: 'auto', vcpus: '1', memory_mb: '512',
    disk_gb: '1', eph_gb: '0', swap_mb: '0'
  });
  assert.ok(!/--ephemeral/.test(out.cli), 'ephemeral 0 should be omitted');
  assert.ok(!/--swap/.test(out.cli), 'swap 0 should be omitted');
  // Zero is still meaningful in the API body, unlike on the command line.
  assert.strictEqual(out.calls[0].body.flavor['OS-FLV-EXT-DATA:ephemeral'], 0);
  assert.strictEqual(out.calls[0].body.flavor.swap, 0);
};

tests['names needing quotes are shell-escaped'] = function () {
  var out = translate('/admin/flavors/create/', {
    name: "it's big", flavor_id: '', vcpus: '8', memory_mb: '16384',
    disk_gb: '100'
  });
  assert.ok(out.cli.endsWith("'it'\\''s big'"), 'got: ' + out.cli);
  assert.ok(!/--id/.test(out.cli), 'blank id should be omitted');
  assert.ok(!('id' in out.calls[0].body.flavor), 'blank id should not be sent');
};

tests['volume type defaults to public'] = function () {
  var out = translate('/admin/volume_types/create_type', {
    name: 'ssd', vol_type_description: 'Fast disks', is_public: 'on'
  });
  assert.strictEqual(out.cli,
    "openstack volume type create --description 'Fast disks' ssd");
  assert.strictEqual(
    out.calls[0].body.volume_type['os-volume-type-access:is_public'], true);
};

tests['volume type without the checkbox is private'] = function () {
  var out = translate('/admin/volume_types/create_type', { name: 'ssd' });
  assert.strictEqual(out.cli, 'openstack volume type create --private ssd');
  assert.strictEqual(
    out.calls[0].body.volume_type['os-volume-type-access:is_public'], false);
};

tests['network create maps provider attributes'] = function () {
  var out = translate('/admin/networks/create/', {
    name: 'provider1', tenant_id: 'abc123', network_type: 'vlan',
    physical_network: 'physnet1', segmentation_id: '101', mtu: '9000',
    admin_state: 'on', shared: 'on', external: 'on',
    az_hints: ['nova', 'az2']
  });
  assert.strictEqual(out.cli,
    'openstack network create --project abc123 --provider-network-type vlan ' +
    '--provider-physical-network physnet1 --provider-segment 101 --mtu 9000 ' +
    '--share --external --availability-zone-hint nova ' +
    '--availability-zone-hint az2 provider1');
  var body = out.calls[0].body.network;
  assert.strictEqual(body['provider:segmentation_id'], 101);
  assert.strictEqual(body.admin_state_up, true);
  sameValue(body.availability_zone_hints, ['nova', 'az2']);
};

tests['unchecked admin_state becomes --disable'] = function () {
  var out = translate('/admin/networks/create/', {
    name: 'down', tenant_id: 'abc123', network_type: 'geneve'
  });
  assert.ok(/--disable/.test(out.cli), 'got: ' + out.cli);
  assert.ok(!/--share|--external/.test(out.cli));
  assert.strictEqual(out.calls[0].body.network.admin_state_up, false);
  assert.ok(!('availability_zone_hints' in out.calls[0].body.network),
    'an empty multi-select should not be sent');
};

tests['row delete uses the id in the action field'] = function () {
  var out = translate('/admin/flavors/', { action: 'flavors__delete__abc-1' });
  assert.strictEqual(out.cli, 'openstack flavor delete abc-1');
  assert.strictEqual(out.calls.length, 1);
  assert.strictEqual(out.calls[0].method, 'DELETE');
  assert.ok(out.calls[0].url.endsWith('/flavors/abc-1'));
};

tests['batch delete expands object_ids'] = function () {
  var out = translate('/admin/networks/', {
    action: 'networks__delete', object_ids: ['n1', 'n2']
  });
  assert.strictEqual(out.cli, 'openstack network delete n1 n2');
  assert.strictEqual(out.calls.length, 2);
  assert.ok(/\(2\)/.test(out.title));
};

tests['unknown tables and actions are ignored'] = function () {
  assert.strictEqual(
    translate('/admin/images/', { action: 'images__delete__x' }), null,
    'a table with no mapping must not be recorded');
  assert.strictEqual(
    translate('/admin/flavors/', { action: 'flavors__update__x' }), null,
    'a non-delete action must not match the delete rule');
  assert.strictEqual(
    translate('/project/instances/', { name: 'vm1' }), null,
    'an unmapped form must not be recorded');
};

tests['secret-looking fields are never read'] = function () {
  var fields = astrolabe.readFields({
    _pairs: [
      ['csrfmiddlewaretoken', 'nope'],
      ['admin_password', 'hunter2'],
      ['secret_key', 'nope'],
      ['api_token', 'nope'],
      ['adminPass', 'nope'],
      ['name', 'keep-me']
    ]
  });
  sameValue(Object.keys(fields), ['name']);
};

tests['ordinary fields that merely contain a secret word are kept'] = function () {
  var fields = astrolabe.readFields({
    _pairs: [
      ['passthrough', 'a'], ['monkey', 'b'], ['keystone_url', 'c'],
      ['author', 'd'], ['bypass_check', 'e']
    ]
  });
  sameValue(Object.keys(fields).sort(),
    ['author', 'bypass_check', 'keystone_url', 'monkey', 'passthrough']);
};

tests['the clicked button supplies the action field'] = function () {
  var fields = astrolabe.readFields(
    { _pairs: [['object_ids', 'n1']] },
    { name: 'action', value: 'networks__delete' });
  assert.strictEqual(fields.action, 'networks__delete');
};

tests['curl never contains a real token'] = function () {
  var out = translate('/admin/flavors/create/', {
    name: 'x', vcpus: '1', memory_mb: '1', disk_gb: '1'
  });
  var curl = astrolabe.curlFor(out.calls[0]);
  assert.ok(curl.indexOf('X-Auth-Token: $OS_TOKEN') !== -1, curl);
  assert.ok(curl.indexOf('$OS_COMPUTE_API/flavors') !== -1, curl);
  assert.ok(curl.indexOf('-d \'{"flavor"') !== -1, curl);
};

tests['curl omits a body for deletes'] = function () {
  var out = translate('/admin/flavors/', { action: 'flavors__delete__z' });
  var curl = astrolabe.curlFor(out.calls[0]);
  assert.ok(!/-d /.test(curl), curl);
  assert.ok(!/\\\s*$/.test(curl), 'no dangling continuation: ' + curl);
};

// ------------------------------------------------------------------ runner

var failed = 0;
Object.keys(tests).forEach(function (name) {
  try {
    tests[name]();
    console.log('  ok   ' + name);
  } catch (e) {
    failed++;
    console.log('  FAIL ' + name + '\n       ' + e.message);
  }
});

console.log('\n' + (Object.keys(tests).length - failed) + '/' +
  Object.keys(tests).length + ' interpreter tests passed');
process.exit(failed ? 1 : 0);
