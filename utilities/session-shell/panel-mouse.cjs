'use strict';
// Decode only SGR mouse packets claimed by our visible panel. Ordinary bytes
// pass through; a fragmented Escape prefix waits at most one display frame.
// `suppressed()`: the bytes are another owner's whole (relay, Fleet): pass the read through as it is. `pasting()`: a paste
// stands; its bytes are literal up to and including its end mark (`ESC [ 201 ~`, which may be split across reads), and
// what follows the mark in the same read is read as usual (a mouse report there is still the panel's).
const PASTE_END='\x1b[201~';
module.exports=function({bytes,mouse,suppressed,pasting=()=>false}){
  let pending=Buffer.alloc(0),timer,tail='';
  function flush(){clearTimeout(timer);timer=undefined;if(pending.length){const data=pending;pending=Buffer.alloc(0);bytes(data);}}
  function feed(data){
    clearTimeout(timer);timer=undefined;
    data=Buffer.concat([pending,data]);pending=Buffer.alloc(0);
    while(data.length){
      if(suppressed()){tail='';bytes(data);return;}
      if(pasting()){
        const joined=tail+data.toString('latin1'),end=joined.indexOf(PASTE_END);
        if(end<0){tail=joined.slice(-(PASTE_END.length-1));bytes(data);return;}
        tail='';const upto=end+PASTE_END.length-(joined.length-data.length);
        bytes(data.subarray(0,upto));data=data.subarray(upto);continue;
      }
      tail='';
      const esc=data.indexOf(27);
      if(esc<0){bytes(data);return;}
      if(esc){bytes(data.subarray(0,esc));data=data.subarray(esc);continue;}
      const text=data.toString('latin1'),match=/^\x1b\[<(\d+);(\d+);(\d+)([mM])/.exec(text);
      if(match){mouse(+match[1],+match[2],+match[3],match[4]==='m',data.subarray(0,match[0].length));data=data.subarray(match[0].length);continue;}
      const csi=/^\x1b\[[0-?]*[ -/]*[@-~]/.exec(text);
      if(csi){bytes(data.subarray(0,csi[0].length));data=data.subarray(csi[0].length);continue;}
      if(data.length<=64&&(/^\x1b(?:\[<?[\d;]*)?$/.test(text))){pending=data;timer=setTimeout(flush,8);return;}
      // An Escape that starts none of the above goes with the bytes that follow it in this read (up to the next
      // Escape): `ESC b`, `ESC \r`, `ESC O A` are one key, and an ESC torn off alone would read as a lone Esc.
      const next=data.indexOf(27,1),end=next<0?data.length:next;
      bytes(data.subarray(0,end));data=data.subarray(end);
    }
  }
  return {feed,flush,pending:()=>pending.length>0};
};
