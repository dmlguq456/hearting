'use strict';
// Only peer transactions use this channel. Normal keys still use the PTY FD.
const fs=require('node:fs'),net=require('node:net'),readline=require('node:readline');
module.exports=function inputOwner(fd,heldFD,entered){
  if(!Number.isSafeInteger(fd)||fd<3||!Number.isSafeInteger(heldFD)||heldFD<3)throw Error('invalid input owner');
  const channel=new net.Socket({fd,readable:true,writable:true}),calls=new Map();let id=0;
  channel.on('error',()=>process.exit(1));channel.on('end',()=>process.exit(1));
  readline.createInterface({input:channel}).on('line',line=>{
    const value=JSON.parse(line);
    if(value.event==='entered'){entered(value.ref);return;}
    const resolve=calls.get(value.id);if(resolve){calls.delete(value.id);resolve(value);}
  });
  function request(op,fields){return new Promise(resolve=>{
    const key=++id;calls.set(key,resolve);channel.write(JSON.stringify({id:key,op,...fields})+'\n');
  });}
  return {
    begin:(ref,text)=>request('begin',{ref,text}),
    release:(ref,cancel)=>request('release',{ref,cancel}),
    hold:data=>{
      if(fs.fstatSync(heldFD).size+data.length>131072)throw Error('held input capacity exceeded');
      // Commit bytes before held_bytes can advertise them. This anonymous FD
      // belongs to the launcher too, even if the display dies before notifying it.
      let offset=0;while(offset<data.length)offset+=fs.writeSync(heldFD,data,offset);
      channel.write('{"op":"held"}\n');
    }
  };
};
