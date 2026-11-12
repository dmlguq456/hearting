// Optional selected-session lifecycle; no question, peer or prompt classification.
import net from 'node:net';
import {readFileSync} from 'node:fs';
export default {id:'hearting.session-shell',setup(ctx) {
  const endpoint=process.env.HEARTING_SHELL_SOCKET,token=process.env.HEARTING_SHELL_TOKEN;
  if(!endpoint||!token)return;
  const stat=readFileSync('/proc/self/stat','utf8');
  const start=stat.slice(stat.lastIndexOf(') ')+2).trim().split(/\s+/)[19];
  let sid='',disposed=false,generation=0,chain=Promise.resolve();
  let displayObservation={},displaySequence=0;
  function display(request){
    if(disposed)return;
    // A later observation carries all known fields, so reordered one-shot
    // sockets cannot lose a preceding model update.
    if(request.op==='display-observation')request=displayObservation={...displayObservation,...request};
    const socket=net.connect(endpoint),timer=setTimeout(()=>socket.destroy(),500);
    socket.on('error',()=>{});socket.on('close',()=>clearTimeout(timer));
    socket.on('data',()=>socket.destroy());
    const seq=displaySequence=Math.max(displaySequence+1,Number(process.hrtime.bigint()/1000n));
    socket.on('connect',()=>socket.write(JSON.stringify({token,pid:process.pid,start,seq,...request})+'\n'));
  }
  // The selected model and MCP server counts are display evidence only; the
  // host paints them in its panel because the native sidebar is hidden there.
  let lastModel='',lastMcp='';
  function observeModel(){
    const current=ctx.ui.model?.current?.();
    const next=JSON.stringify(current||null);
    if(next!==lastModel){lastModel=next;if(current)display({op:'display-observation',model:current.modelID,effort:current.variant||''});}
    let servers;
    try{servers=ctx.data.location?.mcp?.server?.list(sid?ctx.data.session.get(sid)?.location:undefined);}catch{servers=undefined;}
    if(!Array.isArray(servers))return;
    const state=s=>s?.status?.status;
    const mcp={total:servers.length,connected:servers.filter(s=>state(s)==='connected').length,
      failed:servers.filter(s=>['failed','needs_auth'].includes(state(s))).length};
    const key=JSON.stringify(mcp);if(key===lastMcp)return;lastMcp=key;
    display({op:'display-observation',mcp});
  }
  function send(event,session=sid) {
    if(!session)return;
    const selected=generation,body={op:'event',event,harness:'opencode',sid:session,token,pid:process.pid,start,
      seq:Number(process.hrtime.bigint()/1000n)};
    chain=chain.then(()=>{
      if(selected!==generation||disposed&&event!=='deselect')return;
      return new Promise(resolve=>{
      let done=false;const finish=()=>{if(done)return;done=true;clearTimeout(timer);socket.destroy();resolve();};
      const socket=net.connect(endpoint),timer=setTimeout(finish,500);
      socket.on('error',finish);socket.on('end',finish);socket.on('data',finish);
      socket.on('connect',()=>socket.write(JSON.stringify(body)+'\n'));
      });
    });
  }
  function select() {
    if(disposed)return;
    const route=ctx.ui.router.current(),next=route?.type==='session'?route.sessionID:'';
    if(next===sid)return;
    const previous=sid;sid=next;generation++;
    if(sid){
      send('select');
      const native=ctx.data.session.status(sid);
      if(native==='idle')send('idle');else if(native==='running')send('working');
    }else if(previous)send('deselect',previous);
  }
  const off=ctx.data.listen(({details:event})=>{
    select();const data=event.data||{},owner=data.sessionID||data.form?.sessionID||event.sessionID;
    if(!sid||owner!==sid)return;
    if(event.type==='session.execution.started')send('submit');
    // A turn stopped by the person (Esc) or by OpenCode ends like a finished one: idle,
    // not a wait that stays until the next prompt. A failed turn keeps its wait.
    else if(['session.execution.succeeded','session.execution.interrupted'].includes(event.type))send('stop');
    else if(['form.created','permission.asked','session.execution.failed'].includes(event.type))send('blocked');
    // An answered or approved wait resumes the turn (Claude and Codex: the PostToolUse
    // that follows it): the border leaves `waiting` now, not at the turn's end. A rejected
    // permission resumes nothing; the turn's own end says what follows.
    else if(event.type==='permission.replied'&&data.reply!=='reject'||['form.replied','form.cancelled'].includes(event.type))send('working');
  });
  // The native app slot runs inside its reactive route tree, including home.
  // Reading the selected route here observes navigation with no polling loop.
  const unmount=ctx.ui.slot({append:'app',render:()=>{select();observeModel();return null;}});
  select();
  return ()=>{disposed=true;generation++;unmount();off();if(sid)send('deselect');};
}};
