// Replay the shipped signal/socket boundary; no native process or operating config.
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const root=process.argv[2],plugin=fs.readFileSync(path.join(root,'adapters/opencode/tui/hearting-session-panel.tsx'),'utf8');
const a=plugin.indexOf('  const [view,setView]'),b=plugin.indexOf('  function Panel(',a);
assert(a>=0&&b>a);
// This prefix contains the production signal/socket handlers, no JSX. Remove
// TypeScript-only generic/annotation/non-null syntax, not runtime statements.
const prefix=plugin.slice(a,b).replace('createSignal<any>','createSignal').replace('process.env.HEARTING_SHELL_SOCKET!','process.env.HEARTING_SHELL_SOCKET').replace('(name: string)','(name)');
const observations=[];
const handlers={},wire={on(event,fn){handlers[event]=fn;return this;},write(){},destroy(){handlers.close?.();}};
const c={net:{connect:()=>wire},createSignal:value=>[()=>value,next=>{value=next;}],Buffer,JSON,
 process:{env:{HEARTING_SHELL_SOCKET:'private-fixture'}},proof:{},rpc(value){observations.push(value);},ctx:{ui:{router:{current:()=>({sessionID:'fixture'})}}}};
vm.createContext(c);vm.runInContext(prefix+'\nthis.peek=view;',c);
handlers.data(Buffer.from(JSON.stringify({sid:'fixture',visible:true,rows:[[{text:'Question fixture'}]],hits:[]})+'\n'));
assert.equal(c.peek().visible,true);
console.log('PASS normal subscriber data paints its view');
const host=fs.readFileSync(path.join(root,'utilities/session-shell/host.cjs'),'utf8');
const start=host.indexOf("conn.on('close',()=>{panelSubscribers.delete(conn);");
const end=host.indexOf(';publishPanel()',start);assert(start>=0&&end>start);
const server={conn:{on(event,fn){this.close=fn;}},panelSubscribers:new Set(),sidebarMounted:true,queueLayout(){}};
server.panelSubscribers.add(server.conn);vm.createContext(server);vm.runInContext(host.slice(start,end)+';',server);
server.conn.close();
if(process.argv[3]!=='close-only')handlers.error(new Error('fixture lost persistent display stream'));handlers.close?.();
console.log('DISCONNECTED',JSON.stringify({hostSidebarMounted:server.sidebarMounted,hostBottomFallback:!server.sidebarMounted,pluginViewStillVisible:c.peek().visible,pluginRetainedRows:c.peek().rows.length,hasCloseHandler:!!handlers.close}));
try{assert.equal(c.peek().visible,false,'native sidebar retains stale visible panel after host falls back to bottom');console.log('PASS disconnect clears native panel');}
catch(error){if(error.code!=='ERR_ASSERTION')throw error;console.log('ASSERT_FAIL',error.message);process.exitCode=1;}

assert.equal(c.peek().rows.length,0);assert.equal(c.peek().hits.length,0);
assert.equal(observations.length,1);assert.equal(observations[0].visible,false);
handlers.data(Buffer.from(JSON.stringify({sid:'fixture',visible:true,rows:[[{text:'late stale data'}]],hits:[]})+'\n'));
assert.equal(c.peek().visible,false);assert.equal(c.peek().rows.length,0);
console.log('PASS late data stays hidden and fallback is notified once');
