import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {createHash} from 'node:crypto';

const manifest=JSON.parse(await readFile('dist/connector-manifest.json','utf8'));
const archive=await readFile('dist/WebManager-Connector.zip');
assert.equal(manifest.schema,1);
assert.match(manifest.version,/^[a-f0-9]{64}$/);
assert.equal(manifest.archiveSha256,createHash('sha256').update(archive).digest('hex'));
assert.ok(archive.length>1000);
const updater=await readFile('connector/update_client.py','utf8');
assert.match(updater,/https:\/\/ccqpqp\.github\.io\/WebManager-Connector/);
console.log('Connector release verified:',manifest.version.slice(0,12));
