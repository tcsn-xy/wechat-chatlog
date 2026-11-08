import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import net from 'node:net';
import {apply,inject} from './index.mjs';

test('authenticated local adapter uses native queue and rejects wrong bindings',async()=>{
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'cl-dsh-'));fs.chmodSync(root,0o700);
  const tokenFile=path.join(root,'token');fs.writeFileSync(tokenFile,'synthetic-local-token',{mode:0o600});
  const socketPath=path.join(root,'rpc.sock'),calls=[],dispose=[];
  const workspace={id:'w',path:root,title:'Fixture',sessionIds:['s']};
  const ctx={connection:{createPeer:()=>({dispose(){}})},sessions:{get:()=>({header:{cwd:root}})},sessionController:{inspect:async()=>({meta:{cwd:root},events:[]})},workspaceRegistry:{list:()=>[workspace]},effect:fn=>dispose.push(fn()),typertGateway:{invoke:async request=>{
    calls.push(request);
    if(request.method==='list')return {items:[{sessionId:'s',cwd:root,projections:{values:{title:'Bridge fixture'}}}]};
    if(request.method==='prompt')return {accepted:true};
    return {sessionId:'s'};
  }}};
  await apply(ctx,{profile:'web',tokenFile,socketPath});
  const request=(method,params={},token='synthetic-local-token')=>new Promise((resolve,reject)=>{
    const s=net.createConnection(socketPath);let text='';s.setEncoding('utf8');s.on('error',reject);
    s.on('connect',()=>s.write(JSON.stringify({id:'fixture',method,params,token})+'\n'));
    s.on('data',chunk=>text+=chunk);s.on('end',()=>resolve(JSON.parse(text)));
  });
  try{
    assert.ok(inject.includes('sessions'));assert.equal(fs.statSync(socketPath).mode&0o777,0o600);
    assert.equal((await request('health')).result.ready,true);
    assert.equal((await request('health',{},'wrong')).error.code,'unauthorized');
    assert.equal((await request('catalog')).result.sessions[0].title,'Bridge fixture');
    const result=await request('prompt',{session_id:'s',workspace:root,request_id:'fixture-1',text:'Unicode 问题 🦋'});
    assert.equal(result.result.accepted,true);assert.equal(calls.at(-1).args.request.mode,'queue');assert.equal(calls.at(-1).args.request.requestId,'fixture-1');
    const before=calls.length;
    assert.equal((await request('prompt',{session_id:'other',workspace:root,request_id:'fixture-2',text:'wrong'})).error.code,'dsh_binding_mismatch');
    assert.equal(calls.length,before);
    assert.equal((await request('shell')).error.code,'method_not_allowed');
  }finally{for(const close of dispose)await close();fs.rmSync(root,{recursive:true,force:true});}
});
