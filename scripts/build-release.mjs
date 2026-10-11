import {createHash} from 'node:crypto';
import {mkdir,readFile,rm,writeFile} from 'node:fs/promises';

const names=[
  'agent.py','local_deploy.py','local_preview.py','preview_mapping.py',
  'preview_shell.js','preview_shell.css','auto_local.py','실행.cmd',
  'WebManager-Start.vbs','WebManager-Tray.ps1','pages_deploy.py',
  'update_client.py','deploy_notices.py'
];

const crc32=buffer=>{
  let value=0xffffffff;
  for(const byte of buffer){
    value^=byte;
    for(let index=0;index<8;index++)value=(value>>>1)^((value&1)?0xedb88320:0);
  }
  return (value^0xffffffff)>>>0;
};

const pack=entries=>{
  const parts=[],central=[];
  let offset=0;
  for(const [filename,contents] of entries){
    const name=Buffer.from(filename),data=Buffer.from(contents),crc=crc32(data);
    const local=Buffer.alloc(30),directory=Buffer.alloc(46);
    local.writeUInt32LE(0x04034b50);local.writeUInt16LE(20,4);local.writeUInt16LE(0x0800,6);
    local.writeUInt32LE(crc,14);local.writeUInt32LE(data.length,18);local.writeUInt32LE(data.length,22);local.writeUInt16LE(name.length,26);
    directory.writeUInt32LE(0x02014b50);directory.writeUInt16LE(20,4);directory.writeUInt16LE(20,6);directory.writeUInt16LE(0x0800,8);
    directory.writeUInt32LE(crc,16);directory.writeUInt32LE(data.length,20);directory.writeUInt32LE(data.length,24);directory.writeUInt16LE(name.length,28);directory.writeUInt32LE(offset,42);
    parts.push(local,name,data);central.push(directory,name);offset+=local.length+name.length+data.length;
  }
  const center=Buffer.concat(central),end=Buffer.alloc(22);
  end.writeUInt32LE(0x06054b50);end.writeUInt16LE(entries.length,8);end.writeUInt16LE(entries.length,10);end.writeUInt32LE(center.length,12);end.writeUInt32LE(offset,16);
  return Buffer.concat([...parts,center,end]);
};

await rm('dist',{recursive:true,force:true});
await mkdir('dist',{recursive:true});
const sources=await Promise.all(names.map(async name=>[name,await readFile('connector/'+name)]));
const versionHash=createHash('sha256');
for(const [name,data] of sources){versionHash.update(name+'\0');versionHash.update(data);versionHash.update('\0')}
const version=versionHash.digest('hex');
const archive=pack([...sources.map(([name,data])=>['WebManager-Connector/'+name,data]),
  ['WebManager-Connector/connector-version.json',Buffer.from(JSON.stringify({hash:version})+'\n')]]);
await writeFile('dist/WebManager-Connector.zip',archive);
await writeFile('dist/connector-manifest.json',JSON.stringify({schema:1,version,archiveSha256:createHash('sha256').update(archive).digest('hex')})+'\n');
await writeFile('dist/index.html',`<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="robots" content="noindex,nofollow,noarchive"><meta name="viewport" content="width=device-width,initial-scale=1"><title>WebManager PC 연결</title></head><body><main><h1>WebManager PC 연결 프로그램</h1><p><a href="./WebManager-Connector.zip">최신 프로그램 다운로드</a></p><p>버전 ${version.slice(0,12)}</p></main></body></html>`);
await writeFile('dist/.nojekyll','');
