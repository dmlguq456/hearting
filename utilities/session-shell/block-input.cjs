'use strict';
// This editor never writes to the native PTY. Its caller owns the focus boundary.
const {StringDecoder}=require('node:string_decoder');
module.exports=function({changed,answer,action}) {
  let focused=false,items=[],index=0,answers={},text='',typing=false,notice='',busy=false,frozen,stale=false;
  let decoder=new StringDecoder('utf8'),escape='',pasting=false,escapeTimer;
  const submitted=new Set();
  let help='Tab next · Enter send · Esc/Ctrl+] back · r peers · w work · f Fleet';
  const flat=()=>items.flatMap(item=>(item.questions||[]).map(question=>({item,question})));
  const current=()=>frozen||flat()[index];
  const entry=item=>item.id+'@'+item.revision;
  function reset(keepAnswers=false){clearTimeout(escapeTimer);escapeTimer=undefined;if(!keepAnswers){answers={};submitted.clear();}text='';typing=false;notice='';frozen=undefined;stale=false;decoder=new StringDecoder('utf8');escape='';pasting=false;}
  function update(rows,guidance){
    if(typeof guidance==='string')help=guidance;
    const previous=current();
    items=Array.isArray(rows)?rows.slice(0,8):[];
    const listed=flat();
    if(focused&&previous){
      const found=listed.findIndex(row=>entry(row.item)===entry(previous.item)&&row.question.id===previous.question.id);
      if(found>=0){index=found;frozen=undefined;stale=false;}
      else if(text||answers[entry(previous.item)]&&!submitted.has(entry(previous.item))){frozen=previous;stale=true;notice='question changed · Tab next · Esc back';}
      else{reset(true);index=Math.min(index,Math.max(0,listed.length-1));}
    }else{reset(focused);index=Math.min(index,Math.max(0,listed.length-1));}
    const active=new Set(listed.map(row=>entry(row.item)));
    if(frozen)active.add(entry(frozen.item));
    answers=Object.fromEntries(Object.entries(answers).filter(([key])=>active.has(key)));
    for(const key of submitted)if(!active.has(key))submitted.delete(key);
    changed();
  }
  function enter(){focused=true;notice='';changed();}
  function leave(){focused=false;reset();changed();}
  function detail(){
    if(!focused)return [];
    const row=current(),lines=[];
    if(row){
      lines.push([{text:'◑ '+(index+1)+'/'+flat().length+' · '+row.question.question}]);
      if(row.item.answerable!==true)lines.push([{text:row.item.reason||'answer unavailable',dim:true}]);
      const choices=row.item.answer_kind==='decision'?['approve','reject','defer']:row.question.choices||[];
      if(choices.length)lines.push([{text:choices.map((choice,i)=>`${i+1} ${typeof choice==='object'?choice.label||choice.value:choice}`).join(' · '),dim:true}]);
      const picked=answers[entry(row.item)]?.[row.question.id];
      lines.push([{text:typing?'› '+(row.question.secret?'•'.repeat([...text].length):text):'› '+(picked?.join(', ')||'1–9 · text: t'),dim:true}]);
    }
    if(notice)lines.push([{text:notice,dim:true}]);
    lines.push([{text:help,dim:true}]);
    return lines;
  }
  async function submit(){
    const row=current();if(!row||busy)return;
    if(stale){notice='question changed · Tab next · Esc back';changed();return;}
    if(row.item.answerable!==true){notice=row.item.reason||'answer unavailable';changed();return;}
    if(typing){
      if(!text.trim()){notice='empty answer';changed();return;}
      answers[entry(row.item)]={...(answers[entry(row.item)]||{}),[row.question.id]:[text]};typing=false;text='';
    }
    const values=answers[entry(row.item)]||{};
    if(row.item.answer_kind!=='decision'&&(row.item.questions||[]).some(q=>!values[q.id])){
      notice='answer each question · Tab next';changed();return;
    }
    const selected=row.item.id;busy=true;notice='sending…';changed();
    try{
      const result=await answer({question_id:selected,revision:row.item.revision,...(row.item.answer_kind==='decision'
        ?{decision:values[row.question.id]?.[0]}:{answers:values})});
      if(result.ok===true||['submitted','received','applied'].includes(result.state))submitted.add(entry(row.item));
      if(current()?.item.id===selected&&current()?.item.revision===row.item.revision)
        notice=result.state||result.reason||'unknown';
      // A successful transport receipt does not imply the original work completed.
    }catch{notice='unknown · check existing question receipt';}
    finally{busy=false;changed();}
  }
  function character(c){
    if(pasting){if(Buffer.byteLength(text+c)<=4096){typing=true;text+=c;}return;}
    if(c==='\x1d'||c==='\x1b'){leave();return;}
    if(c==='\t'){index=(index+1)%Math.max(1,flat().length+1);frozen=undefined;stale=false;typing=false;text='';notice='';return;}
    if(c==='\r'||c==='\n'){if(current())submit();else action('\r');return;}
    if(c==='\x7f'||c==='\b'){text=[...text].slice(0,-1).join('');return;}
    if(typing){if(c>=' '&&Buffer.byteLength(text+c)<=4096)text+=c;return;}
    if(c==='t'){typing=true;text='';return;}
    const row=current();
    if(/^[1-9]$/.test(c)&&row){
      const choices=row.item.answer_kind==='decision'?['approve','reject','defer']:row.question.choices||[];
      const choice=choices[Number(c)-1];
      if(choice!==undefined){
        const value=typeof choice==='object'?choice.value??choice.label:choice;
        answers[entry(row.item)]={...(answers[entry(row.item)]||{}),[row.question.id]:[value]};notice='Enter send';
      }
      return;
    }
    if('rwf'.includes(c)||c==='\r')action(c);
    else if(row&&!(row.question.choices||[]).length&&c>=' '){typing=true;text=c;}
  }
  function input(data){
    // Return every byte after a focus exit to the caller, without decoding it.
    // A split CSI/paste prefix must never become an Escape followed by native text.
    for(let i=0;i<data.length;i++){
      if(!focused){changed();return data.subarray(i);}
      const byte=data[i],c=String.fromCharCode(byte);
      if(escape){
        clearTimeout(escapeTimer);escapeTimer=undefined;
        if(escape==='\x1b'&&c!=='['){
          if(pasting){escape='';character('\x1b');character(c);continue;}
          leave();return data.subarray(i);
        }
        escape+=c;
        if(escape==='\x1b[200~'){pasting=true;typing=true;escape='';}
        else if(escape==='\x1b[201~'){pasting=false;escape='';}
        else if(escape.length>2&&byte>=0x40&&byte<=0x7e)escape='';
        else if(escape.length>64)escape='';
      }else if(byte===0x1b){
        escape=c;
        if(!pasting)escapeTimer=setTimeout(()=>{if(escape==='\x1b')leave();},25);
      }else if(byte===0x1d&&!pasting){
        leave();return data.subarray(i+1);
      }else{
        for(const text of decoder.write(data.subarray(i,i+1)))character(text);
      }
    }
    changed();return Buffer.alloc(0);
  }
  return {get focused(){return focused;},enter,leave,update,input,detail};
};
