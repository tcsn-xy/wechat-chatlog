// Local Cordis adapter. No browser credential, public route or second agent.
import net from 'node:net';
import fs from 'node:fs';
import path from 'node:path';
import { timingSafeEqual, randomUUID } from 'node:crypto';

export const name = 'chatlog-local-bridge';
export const inject = ['connection', 'typertGateway', 'workspaceRegistry', 'sessionController', 'sessions'];
export async function apply(ctx, config) {
  const secretStat = fs.lstatSync(config.tokenFile);
  if (!secretStat.isFile() || secretStat.isSymbolicLink() || (secretStat.mode & 0o077) || secretStat.uid !== process.getuid()) throw new Error('chatlog token permissions invalid');
  const secret = fs.readFileSync(config.tokenFile, 'utf8').trim();
  const peer = ctx.connection.createPeer({ principal: { kind: 'owner' }, interactions: 'none' });
  const invoke = (namespace, method, request) => ctx.typertGateway.invoke({ namespace, method, args: { request }, peer });
  const workspaces = () => ctx.workspaceRegistry.list().map(w => ({ id:w.id, path:w.path, title:w.title, session_ids:[...w.sessionIds] }));
  const inspect = async id => ctx.sessionController.inspect(id);
  const binding = async (id, workspace) => {
    const w = workspaces().find(w => w.path === workspace && w.session_ids.includes(id));
    if (!w) throw new Error('dsh_binding_mismatch');
    const live = ctx.sessions?.get(id);
    const meta = live?.header ?? (await inspect(id)).meta;
    if (!meta) throw new Error('dsh_session_metadata_missing');
    if (meta.cwd !== workspace) throw new Error('dsh_binding_mismatch');
    return w;
  };
  async function call(method, params) {
    if (method === 'health') return { ready:true, profile:config.profile, pid:process.pid, protocol:1 };
    if (method === 'catalog') {
      let list;
      // list's reserved argument is named _request in this installed cohort.
      list = await ctx.typertGateway.invoke({namespace:'session',method:'list',args:{_request:{}},peer});
      const ws = workspaces();
      return {workspaces:ws, sessions:list.items.filter(s => s.origin !== 'subagent').map(s => ({id:s.sessionId,workspace:s.cwd,title:s.projections?.values?.title ?? s.sessionId,running:s.running}))};
    }
    if (method === 'workspace_create') return invoke('workspace','create',{path:params.path});
    if (method === 'session_create') {
      const result = await invoke('session','create',{workspaceId:params.workspace_id});
      if (params.title) await invoke('session','rename',{sessionId:result.sessionId,title:params.title});
      return result;
    }
    if (method === 'binding') { await binding(params.session_id,params.workspace);return {valid:true}; }
    if (method === 'prompt') {
      await binding(params.session_id,params.workspace);
      if (typeof params.text !== 'string' || !params.text.trim() || Buffer.byteLength(params.text)>65536 || !/^[A-Za-z0-9_-]{1,128}$/.test(params.request_id)) throw new Error('invalid_prompt');
      const result=await invoke('session','prompt',{sessionId:params.session_id,requestId:params.request_id,mode:'queue',content:[{type:'text',text:params.text}],clientTimeZone:'Asia/Shanghai'});
      return {accepted:result.accepted,request_id:params.request_id};
    }
    if (method === 'inspect') {
      await binding(params.session_id,params.workspace);
      const result=await inspect(params.session_id);
      return {session_id:params.session_id,events:result.events.filter(e=>['assistant/message','user/message','turn/end','agent/inbox/spliced'].includes(e.type)).slice(-80)};
    }
    throw new Error('method_not_allowed');
  }
  const sockets = new Set();
  const server=net.createServer(socket=>{
    sockets.add(socket);let buffer='';socket.setEncoding('utf8');socket.setTimeout(30000,()=>socket.destroy());
    socket.on('close',()=>sockets.delete(socket));socket.on('error',()=>{});
    socket.on('data',chunk=>{
      buffer+=chunk.toString('utf8');if(Buffer.byteLength(buffer)>131072){socket.destroy();return;}
      let newline;
      while((newline=buffer.indexOf('\n'))>=0){
        const line=buffer.slice(0,newline);buffer=buffer.slice(newline+1);
        let method;
        Promise.resolve().then(async()=>{
          const req=JSON.parse(line), supplied=Buffer.from(typeof req.token==='string'?req.token:'');
          method=req.method;
          if(supplied.length!==Buffer.byteLength(secret)||!timingSafeEqual(supplied,Buffer.from(secret)))throw new Error('unauthorized');
          return {id:req.id,result:await call(req.method,req.params??{})};
        }).catch(error=>({error:{code:typeof error.code==='string'?error.code: /^[a-z_]+$/.test(error.message)?error.message:'dsh_call_failed',detail:method==='binding'?String(error.message).slice(0,160):undefined}}))
          .then(result=>{if(!socket.destroyed)socket.end(JSON.stringify(result)+'\n');});
      }
    });
  });
  fs.mkdirSync(path.dirname(config.socketPath),{recursive:true,mode:0o700});
  if(fs.existsSync(config.socketPath)){
    const probe=net.createConnection(config.socketPath);
    const active=await new Promise(resolve=>{probe.once('connect',()=>{probe.destroy();resolve(true);});probe.once('error',()=>resolve(false));});
    if(active)throw new Error('chatlog_socket_already_in_use');
    fs.unlinkSync(config.socketPath);
  }
  await new Promise((resolve,reject)=>{server.once('error',reject);server.listen(config.socketPath,resolve);});
  fs.chmodSync(config.socketPath,0o600);
  ctx.effect(()=>async()=>{for(const s of sockets)s.destroy();await new Promise(resolve=>server.close(resolve));try{fs.unlinkSync(config.socketPath);}catch{}peer.dispose?.();},'chatlog IPC listener');
}
