'use strict';
// Decode only SGR mouse packets claimed by our visible panel. Ordinary bytes
// pass through; a fragmented Escape prefix waits at most one display frame.
module.exports=function({bytes,mouse,suppressed}){
  let pending=Buffer.alloc(0),timer;
  function flush(){clearTimeout(timer);timer=undefined;if(pending.length){const data=pending;pending=Buffer.alloc(0);bytes(data);}}
  function feed(data){
    clearTimeout(timer);timer=undefined;
    data=Buffer.concat([pending,data]);pending=Buffer.alloc(0);
    while(data.length){
      if(suppressed()){bytes(data);return;}
      const esc=data.indexOf(27);
      if(esc<0){bytes(data);return;}
      if(esc){bytes(data.subarray(0,esc));data=data.subarray(esc);continue;}
      const text=data.toString('latin1'),match=/^\x1b\[<(\d+);(\d+);(\d+)([mM])/.exec(text);
      if(match){mouse(+match[1],+match[2],+match[3],match[4]==='m',data.subarray(0,match[0].length));data=data.subarray(match[0].length);continue;}
      const csi=/^\x1b\[[0-?]*[ -/]*[@-~]/.exec(text);
      if(csi){bytes(data.subarray(0,csi[0].length));data=data.subarray(csi[0].length);continue;}
      if(data.length<=64&&(/^\x1b(?:\[<?[\d;]*)?$/.test(text))){pending=data;timer=setTimeout(flush,8);return;}
      bytes(data.subarray(0,1));data=data.subarray(1);
    }
  }
  return {feed,flush,pending:()=>pending.length>0};
};
