import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

const root = fileURLToPath(new URL('../', import.meta.url));

function bicep(command, file) {
  const result = spawnSync('az', ['bicep', command, '--file', file, '--stdout', '--only-show-errors'], {
    cwd: root, encoding: 'utf8',
  });
  assert.equal(result.status, 0, result.stderr);
  return JSON.parse(result.stdout);
}

test('shared deployment contains only four child resources and a conditional profile', () => {
  const template = bicep('build', 'infra/modules/frontDoor.bicep');
  const profile = template.resources.find(resource => resource.type === 'Microsoft.Cdn/profiles');
  assert.equal(profile.condition, "[parameters('createProfile')]");
  assert.equal(template.parameters.createProfile.defaultValue, true);
  assert.equal(template.parameters.endpointName.defaultValue, "[format('{0}-ep', parameters('frontDoorName'))]");
  assert.equal(template.parameters.originGroupName.defaultValue, 'storage-origin-group');
  assert.equal(template.parameters.originName.defaultValue, 'storage-blob-origin');
  assert.deepEqual(template.resources.filter(resource => resource !== profile).map(resource => resource.type).sort(), [
    'Microsoft.Cdn/profiles/afdEndpoints',
    'Microsoft.Cdn/profiles/afdEndpoints/routes',
    'Microsoft.Cdn/profiles/originGroups',
    'Microsoft.Cdn/profiles/originGroups/origins',
  ]);
  const origin = template.resources.find(resource => resource.type.endsWith('/origins'));
  assert.equal(origin.properties.sharedPrivateLinkResource.groupId, 'blob');
  assert.equal(origin.properties.enforceCertificateNameCheck, true);
  const route = template.resources.find(resource => resource.type.endsWith('/routes'));
  assert.equal(route.properties.forwardingProtocol, 'HttpsOnly');
  assert.deepEqual(route.properties.patternsToMatch, ['/*']);
  assert.deepEqual(route.properties.supportedProtocols, ['Https']);
});

test('DocIntel parameters cannot target Sora existing child names or create its profile', () => {
  const result = bicep('build-params', 'infra/shared-frontDoor.docintel.bicepparam');
  const parameters = JSON.parse(result.parametersJson).parameters;
  assert.equal(parameters.createProfile.value, false);
  assert.equal(parameters.frontDoorName.value, 'afd-sora-video-dev-erik2');
  assert.equal(parameters.endpointName.value, 'docintel-dev-erik3-ep');
  assert.equal(parameters.originGroupName.value, 'docintel-storage-origin-group');
  assert.equal(parameters.originName.value, 'docintel-storage-blob-origin');
  assert.equal(parameters.storageAccountHostName.value, 'stc4ryis6hullf4.blob.core.windows.net');
  assert.equal(parameters.storageAccountLocation.value, 'eastus');
});

test('main template scopes shared children to the existing profile without creating it', () => {
  const template = bicep('build', 'infra/main.bicep');
  assert.equal(template.parameters.sharedFrontDoorProfileId.defaultValue, '');
  const deployment = template.resources.find(resource => resource.properties?.parameters?.createProfile);
  assert.equal(deployment.properties.parameters.createProfile.value, "[not(variables('useSharedFrontDoor'))]");
  assert.match(deployment.subscriptionId, /split\(parameters\('sharedFrontDoorProfileId'\), '\/'\)\[2\]/);
  assert.match(deployment.resourceGroup, /split\(parameters\('sharedFrontDoorProfileId'\), '\/'\)\[4\]/);
  assert.match(deployment.name, /frontDoor-\{0\}/);
  const profile = deployment.properties.template.resources.find(resource => resource.type === 'Microsoft.Cdn/profiles');
  assert.equal(profile.condition, "[parameters('createProfile')]");
});

const yaml = fs.readFileSync(path.join(root, 'azure.yaml'), 'utf8');
const hook = yaml.split('    posix:\n')[1].split('    windows:\n')[0]
  .split('      run: |\n')[1].split('\n').map(line => line.replace(/^        /, '')).join('\n');

function runHook(t, connections, failCommand = '') {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'frontdoor-hook-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const log = path.join(dir, 'calls');
  fs.writeFileSync(path.join(dir, 'azd'), `#!/bin/sh
case "$3" in
  AZURE_STORAGE_ACCOUNT_ID) printf '%s\\n' /subscriptions/sub/resourceGroups/rg/providers/Microsoft.Storage/storageAccounts/storage ;;
  AZURE_FRONT_DOOR_PRIVATE_LINK_REQUEST_MESSAGE) printf '%s\\n' 'Front Door private link: shared/docintel-group/docintel-origin' ;;
  *) exit 1 ;;
esac
`, { mode: 0o700 });
  fs.writeFileSync(path.join(dir, 'az'), `#!/bin/sh
printf '%s\\n' "$*" >> "$CALL_LOG"
if [ "$1 $2 $3" = "storage account show" ]; then
  [ "$FAIL_COMMAND" != read ] || exit 1
  printf '%s' "$CONNECTIONS"
elif [ "$1 $2 $3" = "network private-endpoint-connection approve" ]; then
  [ "$FAIL_COMMAND" != approve ] || exit 1
else
  exit 1
fi
`, { mode: 0o700 });
  const result = spawnSync('sh', ['-c', hook], {
    encoding: 'utf8',
    env: { ...process.env, PATH: `${dir}:${process.env.PATH}`, CALL_LOG: log, CONNECTIONS: connections, FAIL_COMMAND: failCommand },
  });
  return { ...result, calls: fs.existsSync(log) ? fs.readFileSync(log, 'utf8') : '' };
}

test('approval is scoped to the storage ID, pending status and exact request message', t => {
  const result = runHook(t, '/storage/privateEndpointConnections/new-link\n');
  assert.equal(result.status, 0, result.stderr);
  assert.match(result.calls, /--ids \/subscriptions\/sub\/resourceGroups\/rg\/providers\/Microsoft.Storage\/storageAccounts\/storage/);
  assert.match(result.calls, /status=='Pending' && privateLinkServiceConnectionState.description=='Front Door private link: shared\/docintel-group\/docintel-origin'/);
  assert.match(result.calls, /approve --id \/storage\/privateEndpointConnections\/new-link --description/);
});

test('no matching link causes no approval', t => {
  const result = runHook(t, '');
  assert.equal(result.status, 0, result.stderr);
  assert.doesNotMatch(result.calls, /approve/);
});

test('multiple matching links fail without approving either', t => {
  const result = runHook(t, '/storage/privateEndpointConnections/one\n/storage/privateEndpointConnections/two\n');
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /Multiple matching/);
  assert.doesNotMatch(result.calls, /approve/);
});

for (const command of ['read', 'approve']) {
  test(`${command} failure is surfaced`, t => {
    const result = runHook(t, '/storage/privateEndpointConnections/new-link\n', command);
    assert.notEqual(result.status, 0);
    assert.doesNotMatch(result.stdout, /connection approved/);
  });
}
