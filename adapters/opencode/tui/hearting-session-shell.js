// Optional selected-session lifecycle; no question, peer or prompt classification.
import net from 'node:net';
import {readFileSync} from 'node:fs';
export default {id:'hearting.session-shell',setup(ctx) {
  const endpoint=process.env.HEARTING_SHELL_SOCKET,token=process.env.HEARTING_SHELL_TOKEN;
  if(!endpoint||!token)return;
  const stat=readFileSync('/proc/self/stat','utf8');
  const start=stat.slice(stat.lastIndexOf(') ')+2).trim().split(/\s+/)[19];
  let sid='',disposed=false,generation=0,chain=Promise.resolve();
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
    else if(event.type==='session.execution.succeeded')send('stop');
    else if(['form.created','permission.asked','session.execution.failed','session.execution.interrupted'].includes(event.type))send('blocked');
  });
  // The native app slot runs inside its reactive route tree, including home.
  // Reading the selected route here observes navigation with no polling loop.
  const unmount=ctx.ui.slot({append:'app',render:()=>{select();return null;}});
  select();
  return ()=>{disposed=true;generation++;unmount();off();if(sid)send('deselect');};
}};
