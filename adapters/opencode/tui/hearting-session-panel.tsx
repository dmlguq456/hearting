// Official additive slot only; the native sidebar keeps its size and sections.
import net from 'node:net';
import {createSignal, Index, Show, onMount, onCleanup} from 'solid-js';
import {TextAttributes} from '@opentui/core';

function color(n: number | undefined) {
  if(n===undefined||n<0)return undefined;
  const basic=['#000000','#800000','#008000','#808000','#000080','#800080','#008080','#c0c0c0','#808080','#ff0000','#00ff00','#ffff00','#0000ff','#ff00ff','#00ffff','#ffffff'];
  if(n<16)return basic[n];
  if(n>=232){const v=(8+10*(n-232)).toString(16).padStart(2,'0');return '#'+v.repeat(3);}
  const v=n-16,levels=[0,95,135,175,215,255];
  return '#'+[Math.floor(v/36),Math.floor(v/6)%6,v%6].map(x=>levels[x].toString(16).padStart(2,'0')).join('');
}
export function sessionPanel(ctx: any, proof: any, rpc: (request: any)=>void) {
  const [view,setView]=createSignal<any>({rows:[],hits:[],sid:''});
  let raw='',closed=false,lastModel='',streamLost=false,mounted=false;
  const reportGeometry=()=>{
    if(!closed)rpc({op:'display-observation',visible:mounted&&!streamLost,
      native_columns:ctx.renderer.width,native_rows:ctx.renderer.height});
  };
  const resized=()=>queueMicrotask(reportGeometry);
  ctx.renderer.on('resize',resized);
  const socket=net.connect(process.env.HEARTING_SHELL_SOCKET!);
  function unavailable(){
    if(streamLost)return;streamLost=true;raw='';
    setView({visible:false,rows:[],hits:[],sid:''});socket.destroy();
    if(!closed)rpc({op:'display-observation',visible:false});
  }
  socket.on('connect',()=>{socket.setEncoding('utf8');socket.write(JSON.stringify({...proof,op:'display-subscribe'})+'\n');});
  socket.on('error',unavailable);socket.on('close',unavailable);
  socket.on('data',bytes=>{
    if(streamLost)return;
    raw+=bytes.toString();if(Buffer.byteLength(raw)>131072){socket.destroy();return;}
    let index;while((index=raw.indexOf('\n'))>=0){const body=raw.slice(0,index);raw=raw.slice(index+1);try{setView(JSON.parse(body));}catch{}}
  });
  const action=(name: string)=>rpc({op:'display-action',action:name,sid:ctx.ui.router.current()?.sessionID||''});
  function Panel(props: any){
    onMount(()=>{mounted=true;reportGeometry();});
    onCleanup(()=>{mounted=false;reportGeometry();});
    return <Show when={view().visible&&(!view().sid||view().sid===props.sessionID)}>
      <box flexDirection="column" flexShrink={0} paddingLeft={1} paddingRight={1}>
        <Index each={view().rows}>{(row: any,index: number)=>{
          let surface: any;
          return <box ref={(value: any)=>surface=value} width="100%" height={1} flexShrink={0} flexDirection="row" onMouseDown={(event: any)=>{
            if(event.isDragging||event.button>0)return;
            const x=event.x-(surface?.x||0);
            const hit=view().hits.find((h: any)=>h.row===index&&x>=h.left&&x<=h.right);
            if(!hit)return;
            event.stopPropagation();action(hit.action);
          }}><Index each={row()}>{(part: any)=><text width={part().columns} flexShrink={0} wrapMode="none"
            fg={color(part().foreground)} attributes={(part().bold?TextAttributes.BOLD:0)|(part().dim?TextAttributes.DIM:0)}>{part().text}</text>}</Index></box>;
        }}</Index>
      </box>
    </Show>;
  }
  const unmount=ctx.ui.slot({prepend:'sidebar.content',render:(props: any)=><Panel sessionID={props.sessionID}/>});
  return {
    observe(){
      const current=ctx.ui.model.current();
      const next=JSON.stringify(current||null);if(next===lastModel)return;lastModel=next;
      if(current)rpc({op:'display-observation',model:current.modelID,effort:current.variant||''});
    },
    dispose(){closed=true;ctx.renderer.off('resize',resized);unmount();socket.destroy();}
  };
}
