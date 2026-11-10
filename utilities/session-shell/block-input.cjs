'use strict';
// Local presentation and drafts only. The canonical questions API owns every effect.
const {StringDecoder}=require('node:string_decoder');
module.exports=function({changed,answer,action}) {
  let focused=false,items=[],index=0,frozen,stale=false,busy=false,scroll=0;
  let decoder=new StringDecoder('utf8'),escape='',pasting=false,escapeTimer;
  let labels={},styles={},help='';
  const entries=new Map();
  const flat=()=>items.flatMap(item=>(item.questions||[]).map(question=>({item,question})));
  const current=()=>frozen||flat()[index];
  const key=item=>item.id+'@'+item.revision;
  const state=item=>{
    if(!entries.has(key(item)))entries.set(key(item),{answers:{},drafts:{},typing:{},notice:'',state:null});
    return entries.get(key(item));
  };
  const text=(name,fallback)=>labels[name]||fallback;
  const span=(value,style='dim')=>({text:String(value),...(styles[style]||{dim:style==='dim'})});
  const choices=row=>row.item.answer_kind==='decision'?['approve','reject','defer']:row.question.choices||[];
  const value=choice=>typeof choice==='object'?choice.value??choice.label:choice;
  function resetParser(){clearTimeout(escapeTimer);escapeTimer=undefined;decoder=new StringDecoder('utf8');escape='';pasting=false;}
  function update(rows,guidance,presentation={}) {
    labels=presentation.labels||labels;styles=presentation.styles||styles;help=guidance||help;
    const previous=current();items=Array.isArray(rows)?rows.slice(0,8):[];
    const listed=flat();
    for(const item of items){
      if(['submitted','received','applied','expired','cancelled'].includes(item.request_state)){
        const local=state(item);if(local.state!=='applied')local.state=item.request_state;if(item.request_state==='applied')local.notice='';
      }
    }
    const found=previous?listed.findIndex(row=>key(row.item)===key(previous.item)&&row.question.id===previous.question.id):-1;
    const firstOpen=listed.findIndex(row=>row.item.answerable===true&&!state(row.item).state);
    if(!focused&&previous&&state(previous.item).state&&firstOpen>=0){index=firstOpen;frozen=undefined;stale=false;scroll=0;}
    else if(found>=0){index=found;frozen=undefined;stale=false;}
    else if(previous&&focused&&!state(previous.item).state&&(state(previous.item).drafts[previous.question.id]||Object.keys(state(previous.item).answers).length)){
      frozen=previous;stale=true;state(previous.item).notice=text('changed','question changed · Tab next');
    }else{frozen=undefined;stale=false;index=Math.min(index,Math.max(0,listed.length-1));scroll=0;}
    const active=new Set(listed.map(row=>key(row.item)));if(frozen)active.add(key(frozen.item));
    // Bounded volatile history, never a new question/answer store.
    for(const old of entries.keys())if(!active.has(old)&&entries.size>8)entries.delete(old);
    changed();
  }
  function enter(){focused=true;scroll=0;changed();}
  function leave(){focused=false;resetParser();scroll=0;changed();}
  function move(delta){
    const count=flat().length;index=(index+delta+count+1)%(count+1);
    frozen=undefined;stale=false;scroll=0;resetParser();changed();
  }
  function terminalState(row){return row.item.request_state||state(row.item).state||'open';}
  function active(row){return !stale&&row.item.answerable===true&&(!state(row.item).state||state(row.item).state==='open');}
  function formValues(row){
    const local=state(row.item),result={};
    for(const question of row.item.questions||[]){
      const id=question.id;
      if(local.typing[id]){
        const draft=local.drafts[id]||'';
        if(draft.trim())result[id]=[draft];
      }else if(local.answers[id]?.length)result[id]=[...local.answers[id]];
    }
    return result;
  }
  function complete(row,values){return row.item.answer_kind==='decision'?!!values[row.question.id]:(row.item.questions||[]).every(q=>values[q.id]);}
  function references(){return items.slice(0,8).map(item=>({id:item.id,revision:item.revision}));}
  function preview({budget=10,columns=100}={}) {
    const row=current();if(!row)return focused?[[span(help||text('navigate','Tab next · Esc/Ctrl+] back'))]]:[];
    const local=state(row.item),id=row.question.id,view=row.question.display;
    const status=local.state||row.item.request_state||(row.item.answerable===true?'open':'unavailable');
    let head=view?.head||[span('◑ '+(index+1)+'/'+flat().length+' · '+text(status,status),'g_blocked')];
    if(view){head=head.map((part,i)=>i===3?span(text(status,status),status==='applied'?'lvl_g':status==='open'?'g_blocked':'dim'):part);}
    if(focused)head=[span('› ','g_blocked'),...head];
    const body=view?.body||[[span(row.question.question||'?','loc_repo')]];
    const options=status!=='open'?(row.item.reason?[[span(row.item.reason)]]:[]):view?.options||(choices(row).length?[[span(choices(row).map((c,i)=>`${i+1} ${typeof c==='object'?c.label||c.value:c}`).join(' · '),'loc_repo')]]:[[span(text('text','type answer'))]]);
    const content=[...body,...options,...(view?.source||[])];
    const tail=[];
    const picked=local.answers[id];
    if(focused&&local.typing[id]){
      // Keep the editing caret visible; the original UTF-8 answer stays intact.
      const draft=local.drafts[id]||'',safe=row.question.secret?'•'.repeat([...draft].length):draft;
      const chars=[...safe];let shown='',width=0;
      for(let i=chars.length-1;i>=0;i--){const w=chars[i].charCodeAt(0)<128?1:2;if(width+w>Math.max(1,columns-9))break;shown=chars[i]+shown;width+=w;}
      tail.push([span('› '+(shown!==safe?'…':'')+shown,'loc_repo')]);
    }else if(picked){
      const selected=picked.map(v=>{if(row.question.secret)return '•'.repeat([...String(v)].length);const n=choices(row).findIndex(c=>value(c)===v);return (n>=0?String(n+1)+' · ':'')+v;});
      tail.push([span('› '+selected.join(', '),'loc_repo')]);
    }
    if(local.notice)tail.push([span(local.notice,stale?'lvl_r':'dim')]);
    if(focused){
      const ready=complete(row,formValues(row));
      const instruction=active(row)?(ready?text('send','Enter send'):local.typing[id]||!choices(row).length?text('text','type answer'):text('choose','1–9 choose · t text')):text(status,status);
      const navigate=text('navigate','Tab next · Esc/Ctrl+] back');
      if(columns<64){if(active(row))tail.push([span(instruction)]);tail.push([span(navigate)]);}
      else tail.push([span((active(row)?instruction+' · ':'')+navigate)]);
    }else if(status==='open')tail.push([span('Ctrl+] · '+text('open','answer needed'))]);
    const limit=Math.max(2,budget),tailLimit=Math.min(tail.length,Math.max(1,limit-2));
    const footer=tail.slice(-tailLimit),space=Math.max(0,limit-1-footer.length);
    if(content.length<=space){scroll=0;return [head,...content,...footer].slice(0,limit);}
    const page=Math.max(1,space-1);scroll=Math.max(0,Math.min(scroll,Math.max(0,content.length-page)));
    const more=[span(`+${Math.max(0,content.length-page)} · ${text('more','more · focus and ↑↓ to read')} ${scroll+1}/${content.length}`)];
    if(space<=1)head=[...head,span(` +${content.length-page} ↑↓`)];
    return [head,...content.slice(scroll,scroll+page),...(space>1?[more]:[]),...footer].slice(0,limit);
  }
  async function submit(){
    const row=current();if(!row)return;
    const local=state(row.item),id=row.question.id;
    if(busy){local.notice=text('busy','action pending · wait for receipt');changed();return;}
    if(!active(row)){local.notice=stale?text('changed','question changed · Tab next'):row.item.reason||text(terminalState(row),'answer unavailable');changed();return;}
    if(local.typing[id]){
      const draft=local.drafts[id]||'';
      if(!draft.trim()){local.notice=text('empty','empty answer');changed();return;}
      local.typing[id]=false;local.answers[id]=[draft];
    }
    const values=formValues(row);local.answers=values;
    if(!complete(row,values)){local.notice=text('complete','answer every field · Tab next');changed();return;}
    busy=true;local.state='submitted';local.notice='';changed();
    try{
      const result=await answer({question_id:row.item.id,revision:row.item.revision,...(row.item.answer_kind==='decision'?{decision:values[id][0]}:{answers:values})});
      if(['submitted','received','applied'].includes(result.state))local.state=result.state;
      else{local.state=null;local.notice=result.reason||text('unknown','unknown · check receipt');}
    }catch{local.state='unknown';local.notice=text('unknown','unknown · check receipt');}
    finally{busy=false;changed();}
  }
  function character(c){
    const row=current(),local=row&&state(row.item),id=row?.question.id;
    if(pasting){if(row&&active(row)&&Buffer.byteLength((local.drafts[id]||'')+c)<=4096){local.typing[id]=true;local.drafts[id]=(local.drafts[id]||'')+c;}return;}
    if(c==='\x1d'||c==='\x1b'){leave();return;}
    if(c==='\t'){move(1);return;}
    if(c==='\r'||c==='\n'){if(row)submit();else action('\r');return;}
    if(!row){if('rwf'.includes(c))action(c);return;}
    if(!active(row))return;
    if(c==='\x7f'||c==='\b'){local.drafts[id]=[...(local.drafts[id]||'')].slice(0,-1).join('');return;}
    if(local.typing[id]){if(c>=' '&&Buffer.byteLength((local.drafts[id]||'')+c)<=4096)local.drafts[id]=(local.drafts[id]||'')+c;return;}
    if(c==='t'){local.typing[id]=true;return;}
    if(/^[1-9]$/.test(c)&&choices(row).length){
      const choice=choices(row)[Number(c)-1];if(choice!==undefined){local.answers[id]=[value(choice)];local.notice='';}return;
    }
    if('rwf'.includes(c)&&choices(row).length){action(c);return;}
    if(!choices(row).length&&c>=' '){local.typing[id]=true;local.drafts[id]=c;}
  }
  function input(data){
    for(let i=0;i<data.length;i++){
      if(!focused){changed();return data.subarray(i);}
      const byte=data[i],c=String.fromCharCode(byte);
      if(escape){
        clearTimeout(escapeTimer);escapeTimer=undefined;
        if(escape==='\x1b'&&c!=='['){if(pasting){escape='';character('\x1b');character(c);continue;}leave();return data.subarray(i);}
        escape+=c;
        if(escape==='\x1b[200~'){pasting=true;escape='';}
        else if(escape==='\x1b[201~'){pasting=false;escape='';}
        else if(escape.length>2&&byte>=0x40&&byte<=0x7e){
          if(!pasting){if(escape==='\x1b[Z')move(-1);else if(escape==='\x1b[A'||escape==='\x1b[5~')scroll-=escape.includes('5')?4:1;else if(escape==='\x1b[B'||escape==='\x1b[6~')scroll+=escape.includes('6')?4:1;}
          escape='';
        }else if(escape.length>64)escape='';
      }else if(byte===0x1b){escape=c;if(!pasting)escapeTimer=setTimeout(()=>{if(escape==='\x1b')leave();},25);}
      else if(byte===0x1d&&!pasting){leave();return data.subarray(i+1);}
      else for(const char of decoder.write(data.subarray(i,i+1)))character(char);
    }
    changed();return Buffer.alloc(0);
  }
  return {get focused(){return focused;},get pasting(){return pasting;},enter,leave,update,input,detail:preview,references};
};
