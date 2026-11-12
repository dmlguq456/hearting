'use strict';
// The launcher retains this master. No broker hop is added to normal key bytes.
const fs=require('node:fs'),tty=require('node:tty'),net=require('node:net'),readline=require('node:readline');
module.exports=function adopt(pty,fd,pid,exitFD,paused=false){
  if(!Number.isSafeInteger(fd)||fd<3||!Number.isSafeInteger(pid)||pid<=1
     ||!Number.isSafeInteger(exitFD)||exitFD<3||!fs.fstatSync(fd).isCharacterDevice())throw Error('invalid owned PTY');
  const input=new tty.ReadStream(fd);input.setEncoding('utf8');
  // A successor adopts the master without reading it: bytes stay in the kernel until resume().
  if(paused)input.pause();
  const queue=[];let writing=false,exitHandler,exitStatus,closed=false,drainTimer;
  function finish(){if(exitStatus&&closed&&exitHandler){const cb=exitHandler;exitHandler=undefined;clearTimeout(drainTimer);cb(exitStatus);}}
  input.on('error',error=>{if(error.code==='EIO'){closed=true;finish();}else if(error.code!=='EAGAIN')process.exit(1);});
  input.on('close',()=>{closed=true;finish();});
  readline.createInterface({input:new net.Socket({fd:exitFD,readable:true,writable:false})}).on('line',line=>{
    try{exitStatus=JSON.parse(line);}catch{process.exit(1);}
    drainTimer=setTimeout(()=>{closed=true;input.destroy();finish();},200);finish();
  });
  function flush(){
    if(!queue.length){writing=false;return;}
    const task=queue[0];writing=true;
    fs.write(fd,task.data,task.offset,(error,written)=>{
      if(error){if(error.code==='EAGAIN'){setImmediate(flush);return;}process.exit(1);return;}
      task.offset+=written;if(task.offset===task.data.length)queue.shift();flush();
    });
  }
  // Node's own readStop/readStart, as net.Socket does for a paused handle, so a
  // byte stays in the kernel for whichever display reads next.
  function pause(){
    input.pause();
    if(input._handle&&input._handle.reading){input._readableState.reading=false;input._handle.reading=false;input._handle.readStop();}
  }
  function resume(){
    input.resume();
    if(input._handle&&!input._handle.reading&&!input.destroyed){input._handle.reading=true;input._handle.readStart();}
  }
  return {pid,pause,resume,onData:callback=>input.on('data',callback),
    onExit:callback=>{exitHandler=callback;finish();},
    write:data=>{const bytes=Buffer.from(data);if(bytes.length){queue.push({data:bytes,offset:0});if(!writing)flush();}},
    resize:(cols,rows)=>{if(!Number.isInteger(cols)||!Number.isInteger(rows)||cols<=0||rows<=0)throw Error('invalid PTY dimensions');pty.native.resize(fd,cols,rows);}};
};
