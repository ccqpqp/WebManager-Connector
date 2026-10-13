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
const preview=await readFile('connector/local_preview.py','utf8');
assert.match(preview,/def _failure_summary\(output\)/);
assert.match(preview,/미리보기 빌드 실패/);
assert.match(preview,/WORKSPACE_RETENTION_SECONDS=14\*24\*60\*60/);
assert.match(preview,/\/preview-snapshot\//);
assert.match(preview,/id="nextChange"/);
const previewShell=await readFile('connector/preview_shell.js','utf8');
assert.match(previewShell,/scheduleCalculate/);
assert.match(previewShell,/현재 운영 화면과 배포할 화면을 자동 비교합니다/);
const agent=await readFile('connector/agent.py','utf8');
for(const feature of [
  'def create_remote_link():',
  'remote_link.start(a.port,state["token"])',
  'u.path=="/v1/remote/status"',
  'u.path=="/v1/remote/probe"',
  "url+'/_wm/version',method='GET',timeout=12",
  "'threadAlive':thread_alive",
  "'lastAttempt':int(LAST_ATTEMPT*1000)",
  "'lastHttpStatus':LAST_HTTP_STATUS or None"
])assert(agent.includes(feature),'Missing required PC outbound connection feature: '+feature);
assert(agent.includes('if not self.authorized():return self.send(401'),'PC probe requires local authentication');
console.log('Connector release verified:',manifest.version.slice(0,12));
