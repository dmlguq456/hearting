'use strict';
// Local presentation and drafts only. The canonical questions API owns every effect.
const {StringDecoder}=require('node:string_decoder');
module.exports=function({changed,answer,action,page}) {
  let focused=false,items=[],index=0,frozen,stale=false,busy=false,scroll=0,follow=true;
  let decoder=new StringDecoder('utf8'),escape='',pasting=false,escapeTimer;
  let labels={},styles={},help='';
  const entries=new Map();
  const flat=()=>items.flatMap(item=>(item.questions||[]).map(question=>({item,question})));
  const current=()=>frozen||flat()[index];
  const key=item=>item.id+'@'+item.revision;
  const state=item=>{
    if(!entries.has(key(item)))entries.set(key(item),{answers:{},drafts:{},typing:{},cursor:{},notice:'',state:null});
    return entries.get(key(item));
  };
  const text=(name,fallback)=>labels[name]||fallback;
  const span=(value,style='dim')=>({text:String(value),...(styles[style]||{dim:style==='dim'})});
  const choices=row=>row.item.answer_kind==='decision'?['approve','reject','defer']:row.question.choices||[];
  const value=choice=>typeof choice==='object'?choice.value??choice.label:choice;
  // Spec rule 18: the highlighted choice, moved by ↑↓ and sent by Enter as
  // Claude's own question does. It starts on the recommended choice, else the
  // first. A decision (approve/reject/defer) keeps its explicit choice: nothing
  // is highlighted until ↑↓ or a number picks one (-1).
  function cursorOf(row){
    const local=state(row.item),id=row.question.id,count=choices(row).length;
    if(Number.isInteger(local.cursor[id]))return Math.max(0,Math.min(count-1,local.cursor[id]));
    const picked=local.answers[id]?.[0],at=choices(row).findIndex(c=>value(c)===picked);
    if(at>=0)return at;
    if(row.item.answer_kind==='decision')return -1;
    const recommended=row.question.recommended;
    return Number.isInteger(recommended)&&recommended>=0&&recommended<count?recommended:0;
  }
  // ↑↓ choose only while this question takes a choice; otherwise they scroll as before.
  function choosing(row){return !!row&&focused&&active(row)&&!state(row.item).typing[row.question.id]&&choices(row).length>0;}
  function resetParser(){clearTimeout(escapeTimer);escapeTimer=undefined;decoder=new StringDecoder('utf8');escape='';pasting=false;}
  // Correction 3 (11-13): a settled question (answered, expired, cancelled) is kept only while
  // the person reads its receipt in the focused editor; unfocused it is let go, so it is no
  // longer asked for as a retained reference and leaves the panel.
  const settled=item=>item.answerable!==true&&(['applied','expired','cancelled'].includes(item.request_state)||state(item).state==='applied');
  function release(){if(!focused)items=items.filter(item=>!settled(item));}
  function update(rows,guidance,presentation={}) {
    labels=presentation.labels||labels;styles=presentation.styles||styles;help=guidance||help;
    const previous=current();items=Array.isArray(rows)?rows.slice(0,8):[];
    for(const item of items){
      if(['submitted','received','applied','expired','cancelled'].includes(item.request_state)){
        const local=state(item);if(local.state!=='applied')local.state=item.request_state;if(item.request_state==='applied')local.notice='';
      }
    }
    release();
    const listed=flat();
    const found=previous?listed.findIndex(row=>key(row.item)===key(previous.item)&&row.question.id===previous.question.id):-1;
    const firstOpen=listed.findIndex(row=>row.item.answerable===true&&!state(row.item).state);
    if(!focused&&previous&&state(previous.item).state&&firstOpen>=0){index=firstOpen;frozen=undefined;stale=false;scroll=0;follow=true;}
    else if(found>=0){index=found;frozen=undefined;stale=false;}
    else if(previous&&focused&&!state(previous.item).state&&(state(previous.item).drafts[previous.question.id]||Object.keys(state(previous.item).answers).length||Object.keys(state(previous.item).cursor).length)){
      frozen=previous;stale=true;state(previous.item).notice=text('changed','question changed · Tab next');
    }else{frozen=undefined;stale=false;index=Math.min(index,Math.max(0,listed.length-1));scroll=0;follow=true;}
    const active=new Set(listed.map(row=>key(row.item)));if(frozen)active.add(key(frozen.item));
    // Bounded volatile history, never a new question/answer store.
    for(const old of entries.keys())if(!active.has(old)&&entries.size>8)entries.delete(old);
    changed();
  }
  function enter(){focused=true;scroll=0;follow=true;changed();}
  function leave(){focused=false;resetParser();scroll=0;follow=true;release();index=Math.min(index,Math.max(0,flat().length-1));changed();}
  function move(delta){
    const count=flat().length;index=(index+delta+count+1)%(count+1);
    frozen=undefined;stale=false;scroll=0;follow=true;resetParser();changed();
  }
  function terminalState(row){return row.item.request_state||state(row.item).state||'open';}
  // The backend's word for a wait not answerable here (spec E9 rule 54); without one, the
  // session's own window below, where an answer always goes.
  function unanswerable(row){return row.item.answerable===true?'':row.item.answer_state||'answer_below';}
  function active(row){return !stale&&row.item.answerable===true&&(!state(row.item).state||state(row.item).state==='open');}
  function waitingForReceipt(){
    const row=current();
    return focused&&!!row&&!busy&&!stale&&['submitted','received','applied'].includes(state(row.item).state);
  }
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
    const status=local.state||row.item.request_state||(row.item.answerable===true?'open':unanswerable(row));
    let head=view?.head||[span('◑ '+(index+1)+'/'+flat().length+' · '+text(status,status),'g_blocked')];
    if(view){head=head.map((part,i)=>i===3?span(text(status,status),status==='applied'?'lvl_g':status==='open'?'g_blocked':'dim'):part);}
    if(focused)head=[span('› ','g_blocked'),...head];
    const body=view?.body||[[span(row.question.question||'?','loc_repo')]];
    // Spec E9 rule 54: a wait not answerable here shows the backend's whole words for its
    // reason (never the machine reason); a send state of its own shows nothing more.
    const options=status!=='open'?(status===unanswerable(row)&&view?.options?view.options:[]):view?.options||(choices(row).length?choices(row).map((c,i)=>[span(`${i+1} `),span(typeof c==='object'?c.label||c.value:c,'loc_repo')]):[[span(text('text','type answer'))]]);
    const marked=status==='open'&&choices(row).length?highlight(options,choosing(row)?cursorOf(row):-1,choosing(row)):options;
    const content=[...body,...marked,...(view?.source||[])];
    const tail=[];
    const picked=local.answers[id];
    if(focused&&local.typing[id]){
      // Keep the editing caret visible; the original UTF-8 answer stays intact.
      const draft=local.drafts[id]||'',safe=row.question.secret?'•'.repeat([...draft].length):draft;
      const chars=[...safe];let shown='',width=0;
      for(let i=chars.length-1;i>=0;i--){const w=chars[i].charCodeAt(0)<128?1:2;if(width+w>Math.max(1,columns-9))break;shown=chars[i]+shown;width+=w;}
      tail.push([span('› '+(shown!==safe?'…':'')+shown,'loc_repo')]);
    }else if(picked&&!choosing(row)){
      const selected=picked.map(v=>{if(row.question.secret)return '•'.repeat([...String(v)].length);const n=choices(row).findIndex(c=>value(c)===v);return (n>=0?String(n+1)+' · ':'')+v;});
      tail.push([span('› '+selected.join(', '),'loc_repo')]);
    }
    if(local.notice)tail.push([span(local.notice,stale?'lvl_r':'dim')]);
    if(focused){
      const ready=complete(row,formValues(row));
      const instruction=active(row)?(choosing(row)?text('choose','↑↓ choose · Enter send · t text'):ready?text('send','Enter send'):text('text','type answer')):text(status,status);
      const navigate=text('navigate','Tab next · Esc/Ctrl+] back');
      if(columns<64){if(active(row))tail.push([span(instruction)]);tail.push([span(navigate)]);}
      else tail.push([span((active(row)?instruction+' · ':'')+navigate)]);
    }else if(status==='open')tail.push([span('Ctrl+] · '+text('open','answer needed'))]);
    const limit=Math.max(2,budget),tailLimit=Math.min(tail.length,Math.max(1,limit-2));
    const footer=tail.slice(-tailLimit),space=Math.max(0,limit-1-footer.length);
    if(content.length<=space){scroll=0;return [head,...content,...footer].slice(0,limit);}
    const page=Math.max(1,space-1);
    // The highlighted choice comes onto the page when it changes (↑↓, a number);
    // PgUp/PgDn then read the body from its first row to its last, and the page
    // stays where the reading left it until the choice changes again.
    const lit=choosing(row)?body.length+marked.findIndex(parts=>parts[0]?.text==='▸'):-1;
    if(follow&&lit>=body.length){if(lit<scroll)scroll=lit;else if(lit>=scroll+page)scroll=lit-page+1;}
    scroll=Math.max(0,Math.min(scroll,Math.max(0,content.length-page)));
    // While ↑↓ choose, the body pages with PgUp/PgDn: the hint names the keys that read it.
    const reading=choosing(row)?text('more_choose','more · PgUp/PgDn to read'):text('more','more · focus and ↑↓ to read');
    const more=[span(`+${Math.max(0,content.length-page)} · ${reading} ${scroll+1}/${content.length}`)];
    if(space<=1)head=[...head,span(` +${content.length-page} ${choosing(row)?'PgUp/PgDn':'↑↓'}`)];
    return [head,...content.slice(scroll,scroll+page),...(space>1?[more]:[]),...footer].slice(0,limit);
  }
  // The option rows of one choice start with its number (`2 `); its wrapped rows
  // with spaces. Spec E9 rule 53: every option row leads with a two-cell column for the
  // mark (blank, so focusing shifts nothing). While ↑↓ choose, the chosen choice's first row
  // carries `▸` in this session's harness color, bold, and every row of it (each row of a
  // wrapped one) has its words bold in the bright body color; every other choice reads dim
  // grey. Without color the mark and the weight alone tell them apart.
  function highlight(options,at,choosing){
    let n=-1;
    return options.map(parts=>{
      const lead=String(parts[0]?.text??''),number=/^(\d+) $/.exec(lead);
      if(number)n=Number(number[1])-1;else if(lead.trim())n=-1;
      if(!choosing||at<0)return [span('  ',''),...parts];
      if(n!==at)return [span('  ',''),...parts.map(part=>({...part,...(styles.dim||{dim:true}),bold:false}))];
      const words=parts.map(part=>({...part,...(styles.loc_repo||{}),bold:true,dim:false}));
      return number?[{...span('▸','harness'),bold:true,role:'choice-mark'},span(' ',''),...words]:[span('  ',''),...words];
    });
  }
  async function submit(){
    const row=current();if(!row)return;
    const local=state(row.item),id=row.question.id;
    if(busy){local.notice=text('busy','action pending · wait for receipt');changed();return;}
    if(!active(row)){const word=terminalState(row)==='open'?unanswerable(row):terminalState(row);
      local.notice=stale?text('changed','question changed · Tab next'):text(word,word);changed();return;}
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
    if(c==='\r'||c==='\n'){
      if(!row){action('\r');return;}
      // Enter sends the highlighted choice, the same value its number key picks.
      if(choosing(row)&&cursorOf(row)>=0){
        local.answers[id]=[value(choices(row)[cursorOf(row)])];local.notice='';
        if(!complete(row,formValues(row))&&(row.item.questions||[]).length>1){move(1);return;}
      }
      submit();return;
    }
    if(!row){if('rwf'.includes(c))action(c);return;}
    if(!active(row))return;
    if(c==='\x7f'||c==='\b'){local.drafts[id]=[...(local.drafts[id]||'')].slice(0,-1).join('');return;}
    if(local.typing[id]){if(c>=' '&&Buffer.byteLength((local.drafts[id]||'')+c)<=4096)local.drafts[id]=(local.drafts[id]||'')+c;return;}
    if(c==='t'){local.typing[id]=true;return;}
    if(/^[1-9]$/.test(c)&&choices(row).length){
      const choice=choices(row)[Number(c)-1];if(choice!==undefined){local.answers[id]=[value(choice)];local.cursor[id]=Number(c)-1;local.notice='';follow=true;}return;
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
        // An application-cursor terminal sends arrows as SS3 (`ESC O A`).
        if(escape==='\x1b'&&c!=='['&&!(c==='O'&&!pasting)){if(pasting){escape='';character('\x1b');character(c);continue;}leave();return data.subarray(i);}
        escape+=c;
        if(escape==='\x1b[200~'){pasting=true;escape='';}
        else if(escape==='\x1b[201~'){pasting=false;escape='';}
        else if(escape.length>2&&byte>=0x40&&byte<=0x7e||escape.length===3&&escape[1]==='O'){
          if(!pasting){
            const up=escape==='\x1b[A'||escape==='\x1bOA',down=escape==='\x1b[B'||escape==='\x1bOB',row=current();
            if(escape==='\x1b[Z')move(-1);
            else if((up||down)&&choosing(row)){const local=state(row.item),n=choices(row).length;const at=cursorOf(row);local.cursor[row.question.id]=at<0?0:Math.max(0,Math.min(n-1,at+(up?-1:1)));scroll=0;follow=true;}
            // With no question to read, PgUp/PgDn page the route box: the same
            // parse, so a key split across reads pages it as a whole one does.
            else if(!row&&page&&(escape==='\x1b[5~'||escape==='\x1b[6~'))page(escape==='\x1b[5~'?-1:1);
            else if(up||escape==='\x1b[5~'){scroll-=escape.includes('5')?4:1;follow=false;}else if(down||escape==='\x1b[6~'){scroll+=escape.includes('6')?4:1;follow=false;}
          }
          escape='';
        }else if(escape.length>64)escape='';
      }else if(byte===0x1b){escape=c;if(!pasting)escapeTimer=setTimeout(()=>{if(escape==='\x1b')leave();},25);}
      else if(byte===0x1d&&!pasting){leave();return data.subarray(i+1);}
      else for(const char of decoder.write(data.subarray(i,i+1)))character(char);
    }
    changed();return Buffer.alloc(0);
  }
  return {get focused(){return focused;},get waitingForReceipt(){return waitingForReceipt();},get pasting(){return pasting;},enter,leave,update,input,detail:preview,references};
};
