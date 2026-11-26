'use strict';
// Local presentation and drafts only. The canonical questions API owns every effect.
const {StringDecoder}=require('node:string_decoder');
const {keyRows}=require('./panel-view.cjs');
// 11-16 (the person: "답 보냄도 한 5~10분은 유지하도록 해두자"): a sent answer's line stays SENT_KEEP_MS after it
// was sent, whether or not its session has it yet (it used to go at that receipt, within seconds); it
// leaves at the first update or paint past that, on the path it always left by (no timer of its own).
const SENT_KEEP_MS=10*60*1000;
const ANSWER_FULL=Symbol('answer full');
// Home and End as terminals send them: `ESC [ H/F`, SS3, and the `~` forms of tmux/screen and rxvt.
const START=['\x1b[H','\x1bOH','\x1b[1~','\x1b[7~'],END=['\x1b[F','\x1bOF','\x1b[4~','\x1b[8~'];
// 11-26 (the person: "질문 답변에서 줄바꿈이 안되네.. 내가 자유도 높이라고 했지 않나?"): a written answer takes lines.
// Enter alone still sends it. A line break goes in with every key the native input below takes for one in
// this shell (the outer terminal's own bytes reach it unchanged): Ctrl+J (LF, apart from Enter's CR in the
// raw terminal), Alt/Option+Enter (`ESC CR`, also what a terminal set up for Shift+Enter often sends), `\`
// then Enter, and a modified Enter where the terminal reports one (kitty `CSI 13;<mods>u`, xterm
// modifyOtherKeys `CSI 27;<mods>;13~`, kitty's Ctrl+J `CSI 106;5u`).
const NEWLINE_CSI=/^\x1b\[(?:13;(?:[2-9]|1\d)u|27;(?:[2-9]|1\d);13~|106;5u)$/;
// The word keys (Alt/Ctrl+←→, Alt+B/F, Ctrl+W, Alt+Backspace, Alt+D, Ctrl+Delete) step over one run of letters
// and digits and what stands before it, as the native inputs do.
const WORD_LEFT=['\x1b[1;3D','\x1b[1;5D'],WORD_RIGHT=['\x1b[1;3C','\x1b[1;5C'],WORD_DELETE=['\x1b[3;3~','\x1b[3;5~'];
const TEXT_START=['\x1b[1;5H'],TEXT_END=['\x1b[1;5F'];
// 11-26: a paste whose end mark never comes is over this long after its last byte (Fleet's `_PASTE_IDLE`).
const PASTE_IDLE=1000;
const wordChar=c=>/[\p{L}\p{N}\p{M}_]/u.test(c||'');
module.exports=function({changed,answer,action,cellWidth}) {
  let focused=false,items=[],index=0,frozen,stale=false,busy=false,scroll=0,follow=true,moves=0,seat=null,answeredAt=0,escapedAt=0;
  let decoder=new StringDecoder('utf8'),escape='',pasting=false,escapeTimer,skipTail=false,changeArmed=false,pasteCR=false;
  // 11-26: a paste whose end mark never comes ends a second after its last byte (Fleet's rule), and the line
  // breaks a paste ends a secret with are held until more of it comes (a pasted key keeps its inner lines).
  let pasteTimer,heldBreaks=0;
  // 11-26: the answer's rows are laid out at the popup's width when painted; ↑↓ move between those rows (the
  // cell column held while they repeat), and the rows shown follow the place when the answer is taller than its room.
  let answerColumns=60,goal=null,answerTop=0,answerSeat=null;
  let labels={},styles={},help='';
  const entries=new Map();
  const key=item=>item.id+'@'+item.revision;
  const state=item=>{
    if(!entries.has(key(item)))entries.set(key(item),{answers:{},drafts:{},typing:{},cursor:{},caret:{},notice:'',state:null,sentAt:0});
    return entries.get(key(item));
  };
  // Rule 5 (11-14): the moment the person's answer is sent it has left their hands. While it
  // travels (`submitted`) the question is one dim line, never one to choose again; once its
  // session has it (`received`, `applied`) the line stays until SENT_KEEP_MS after it was sent
  // (11-16), then goes. A failed delivery leaves these states and the question comes back with
  // its existing word.
  const phase=item=>{
    const entry=state(item),local=entry.state,remote=item.request_state;
    if(['received','applied'].includes(remote)||['received','applied'].includes(local)){
      // Its age decides: a clock behind the send (a negative age) leaves no age, so the line goes.
      const age=Date.now()-entry.sentAt;
      return entry.sentAt&&age>=0&&age<SENT_KEEP_MS?'sent':'done';
    }
    return remote==='submitted'||local==='submitted'?'sent':null;
  };
  const flat=()=>items.filter(item=>!phase(item)).flatMap(item=>(item.questions||[]).map(question=>({item,question})));
  const current=()=>frozen||flat()[index];
  const text=(name,fallback)=>labels[name]||fallback;
  const span=(value,style='dim')=>({text:String(value),...(styles[style]||{dim:style==='dim'})});
  const choices=row=>row.item.answer_kind==='decision'?['approve','reject','defer']:row.question.choices||[];
  const writable=row=>!!row&&row.item.answer_kind!=='decision'&&(row.question.free_text===true||(row.question.free_text===undefined&&!choices(row).length));
  const measure=cellWidth|| (s=>[...s].reduce((n,c)=>n+(/\p{Mark}/u.test(c)?0:c.codePointAt(0)>=0x2e80?2:1),0));
  const value=choice=>typeof choice==='object'?choice.value??choice.label:choice;
  // 11-22: the place inside the typed text is a screen value only (`local.caret[id]`, a count of characters
  // before it; absent = the end). One step is one character as the person sees it: a character with the
  // marks that join it (the unit Backspace removes and the arrows cross). The draft, the sent answer and
  // the questions API never see it.
  const joins=c=>/[\p{M}\u200d\u{1f3fb}-\u{1f3ff}\u1160-\u11ff\ud7b0-\ud7ff]/u.test(c);
  function after(chars,at){let i=at+1;while(i<chars.length&&joins(chars[i])){const zwj=chars[i]==='\u200d';i++;if(zwj&&i<chars.length)i++;}return Math.min(i,chars.length);}
  function before(chars,at){let i=at-1;while(i>0&&(joins(chars[i])||chars[i-1]==='\u200d'))i--;return Math.max(i,0);}
  function caretAt(local,id){const n=[...(local.drafts[id]||'')].length,c=local.caret[id];return Number.isInteger(c)?Math.max(0,Math.min(n,c)):n;}
  // The characters as the answer row shows them: a line break stays one, a secret is bullets, any other
  // control character one blank cell. One shown character stands for each typed one, so places agree.
  const shownOf=(draft,secret)=>[...draft].map(c=>c==='\n'?'\n':secret?'•':/[\x00-\x1f\x7f-\x9f]/.test(c)?' ':c);
  // The answer's rows at a width: each `{start,end}` of characters, broken at a line break (which no row
  // shows) and where the next character no longer fits after the two-cell lead (`› ` or blank).
  function answerRows(chars,columns){
    const rows=[],limit=Math.max(3,columns-1);let start=0,width=2;
    for(let i=0;i<chars.length;i++){
      if(chars[i]==='\n'){rows.push({start,end:i});start=i+1;width=2;continue;}
      const w=measure(chars[i]);
      if(width+w>limit&&i>start){rows.push({start,end:i});start=i;width=2;}
      width+=w;
    }
    rows.push({start,end:chars.length});
    return rows;
  }
  // The row holding the place: the last row starting at or before it.
  function rowOf(rows,at){let home=0;for(let i=0;i<rows.length;i++)if(rows[i].start<=at)home=i;return home;}
  const cells=list=>measure(list.join(''));
  const lineStart=(chars,at)=>{let i=at;while(i>0&&chars[i-1]!=='\n')i--;return i;};
  const lineEnd=(chars,at)=>{let i=at;while(i<chars.length&&chars[i]!=='\n')i++;return i;};
  function wordLeft(chars,at){let i=at;while(i>0&&!wordChar(chars[before(chars,i)]))i=before(chars,i);while(i>0&&wordChar(chars[before(chars,i)]))i=before(chars,i);return i;}
  function wordRight(chars,at){let i=at;while(i<chars.length&&!wordChar(chars[i]))i=after(chars,i);while(i<chars.length&&wordChar(chars[i]))i=after(chars,i);return i;}
  // Spec rule 18: the highlighted choice, moved by ↑↓ and sent by Enter as
  // Claude's own question does. It starts on the recommended choice, else the
  // first. A decision (approve/reject/defer) keeps its explicit choice: nothing
  // is highlighted until ↑↓ or a number picks one (-1).
  function cursorOf(row){
    const local=state(row.item),id=row.question.id,count=choices(row).length;
    if(Number.isInteger(local.cursor[id]))return Math.max(-1,Math.min(count-1,local.cursor[id]));
    const picked=local.answers[id]?.[0],at=choices(row).findIndex(c=>value(c)===picked);
    if(at>=0)return at;
    if(row.item.answer_kind==='decision'||!count)return -1;
    const recommended=row.question.recommended;
    return Number.isInteger(recommended)&&recommended>=0&&recommended<count?recommended:0;
  }
  // ↑↓ choose only while this question takes a choice; otherwise they scroll as before.
  function choosing(row){return !!row&&focused&&active(row)&&!state(row.item).typing[row.question.id]&&choices(row).length>0;}
  function resetParser(){clearTimeout(escapeTimer);escapeTimer=undefined;endPaste();decoder=new StringDecoder('utf8');escape='';skipTail=false;goal=null;}
  function endPaste(){clearTimeout(pasteTimer);pasteTimer=undefined;pasting=false;pasteCR=false;heldBreaks=0;}
  function pasteGoesOn(){clearTimeout(pasteTimer);pasteTimer=setTimeout(()=>{if(pasting){endPaste();escape='';changed();}},PASTE_IDLE);}
  // Correction 3 (11-13): a settled question (answered, expired, cancelled) is kept only while
  // the person reads its receipt in the focused editor; unfocused it is let go, so it is no
  // longer asked for as a retained reference and leaves the panel.
  // (A sent answer's line still standing, 11-16, is not let go.)
  const settled=item=>item.answerable!==true&&phase(item)!=='sent'&&(['applied','expired','cancelled'].includes(item.request_state)||state(item).state==='applied');
  function release(){if(!focused)items=items.filter(item=>!settled(item));}
  function update(rows,guidance,presentation={}) {
    labels=presentation.labels||labels;styles=presentation.styles||styles;help=guidance||help;
    const previous=current();items=Array.isArray(rows)?rows.slice(0,8):[];
    for(const item of items){
      if(['submitted','received','applied','expired','cancelled'].includes(item.request_state)){
        const local=state(item);if(local.state!=='applied')local.state=item.request_state;if(item.request_state==='applied')local.notice='';
        // An answer seen on its way (sent here, or from another place) keeps its line from then.
        if(item.request_state==='submitted'&&!local.sentAt)local.sentAt=Date.now();
      }else if(item.request_state==='unknown'&&state(item).state==='submitted'&&!state(item).sending){
        // The delivery could not be confirmed: the question returns with its existing word.
        state(item).state='unknown';
      }
    }
    items=items.filter(item=>phase(item)!=='done');
    release();
    const listed=flat();
    const found=previous?listed.findIndex(row=>key(row.item)===key(previous.item)&&row.question.id===previous.question.id):-1;
    const firstOpen=listed.findIndex(row=>row.item.answerable===true&&!state(row.item).state);
    if(!focused&&previous&&state(previous.item).state&&firstOpen>=0){index=firstOpen;frozen=undefined;stale=false;scroll=0;follow=true;}
    else if(found>=0){index=found;frozen=undefined;stale=false;}
    else if(previous&&focused&&!state(previous.item).state&&(state(previous.item).drafts[previous.question.id]||Object.keys(state(previous.item).answers).length||Object.keys(state(previous.item).cursor).length)){
      frozen=previous;stale=true;state(previous.item).notice=text('changed','question changed · Tab next');
    }else{
      frozen=undefined;stale=false;index=Math.min(index,Math.max(0,listed.length-1));scroll=0;follow=true;
      // The question being read left with nothing typed to it and another stands in its place:
      // say so once, and the first Enter after only clears the word, never answers the new one.
      if(previous&&focused&&listed.length&&!state(previous.item).sending){
        state(listed[index].item).notice=text('changed','question changed · Tab next');changeArmed=true;
      }
    }
    const active=new Set(listed.map(row=>key(row.item)));if(frozen)active.add(key(frozen.item));
    // Bounded volatile history, never a new question/answer store.
    for(const old of entries.keys())if(!active.has(old)&&entries.size>8)entries.delete(old);
    changed();
  }
  // Ctrl+] opens on a question the person can answer now (11-14): when the one shown is not
  // (a receipt, a changed one), the first answerable one comes up instead.
  function enter(){
    focused=true;scroll=0;follow=true;answeredAt=0;escapedAt=0;changeArmed=false;
    const row=current();
    if(!frozen&&(!row||!active(row))){const open=flat().findIndex(r=>active(r));if(open>=0)index=open;}
    changed();
  }
  function forgetPlaces(){for(const entry of entries.values())entry.caret={};}
  function leave(){forgetPlaces();clearFullNotice(current());focused=false;escapedAt=0;changeArmed=false;resetParser();scroll=0;follow=true;release();index=Math.min(index,Math.max(0,flat().length-1));changed();}
  const seatOf=row=>row?key(row.item)+'\0'+row.question.id:null;
  function move(delta){
    const count=flat().length;index=(index+delta+count+1)%(count+1);
    moves++;changeArmed=false;forgetPlaces();seat=seatOf(flat()[index]);
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
      const selected=local.answers[id]||[],draft=local.drafts[id]||'';
      const values=[...selected,...(writable({item:row.item,question})&&draft.trim()?[draft]:[])];
      if(values.length)result[id]=values;
    }
    return result;
  }
  function complete(row,values){return row.item.answer_kind==='decision'?!!values[row.question.id]:(row.item.questions||[]).every(q=>values[q.id]);}
  function references(){return items.slice(0,8).map(item=>({id:item.id,revision:item.revision}));}
  function preview({budget=10,columns=100}={}) {
    // Every key line is packed by the popup's one rule: broken between its ` · ` pieces, the closing key last.
    const keys=(pieces,rows)=>keyRows(pieces.flatMap(piece=>String(piece).split(' · ')),columns,{rows,cellWidth}).map(line=>[span(line)]);
    const row=current();if(!row)return focused?keys([help||text('navigate','Tab next · Esc/Ctrl+] back')],Math.max(1,budget)):[];
    const local=state(row.item),id=row.question.id,view=row.question.display;
    const status=local.sending?'submitted':local.state||row.item.request_state||(row.item.answerable===true?'open':unanswerable(row));
    let head=view?.head||[span('◑ '+(index+1)+'/'+flat().length+' · '+text(status,status),'g_blocked')];
    if(view){head=head.map((part,i)=>i===3?span(text(status,status),status==='applied'?'lvl_g':status==='open'?'g_blocked':'dim'):part);}
    if(focused)head=[span('› ','g_blocked'),...head];
    const body=view?.body||[[span(row.question.question||'?','loc_repo')]];
    // Spec E9 rule 54: a wait not answerable here shows the backend's whole words for its
    // reason (never the machine reason); a send state of its own shows nothing more.
    const options=status!=='open'?(status===unanswerable(row)&&view?.options?view.options:[]):view?.options||(choices(row).length?choices(row).map((c,i)=>[span(`${i+1}  `,'g_blocked'),span(typeof c==='object'?c.label||c.value:c,'loc_repo')]):[[span(text('text','type answer'))]]);
    const selected=choosing(row)||focused&&writable(row)&&cursorOf(row)>=0;
    const marked=status==='open'&&choices(row).length?highlight(options,selected?cursorOf(row):-1,!!selected,writable(row)):options;
    // 11-17 (the person: "질문이랑 선지가 잘 구분되게" / "구분선 위에 여백도 하나 두고"): a question with choices
    // ends its words with one empty row and a thin line (the frame's own color, dim where the frame is bright)
    // before its choices; a question answered in words has neither. Short of rows the empty row goes first.
    const split=status==='open'&&choices(row).length?[[],[{text:'─'.repeat(Math.max(0,columns)),popupLine:true,dim:true}]]:[];
    let gap=split;
    const lines=()=>[...body,...gap,...marked,...(view?.source||[])];
    let content=lines();
    const tail=[];let keyLines=[];
    const picked=local.answers[id];
    if(status==='open'&&writable(row)){
      const shownChars=shownOf(local.drafts[id]||'',row.question.secret),rows=answerRows(shownChars,columns);
      answerColumns=columns;
      const live=focused&&!!local.typing[id],at=caretAt(local,id),home=rowOf(rows,at);
      // 11-26: the answer grows a row per line (and per wrap) while the question's first words, its choices
      // and the key line stay in sight; taller than that, the rows shown follow the place (one unbroken
      // stretch; only the answer's first row leads with `› `, so a missing one says the start is above).
      const words=body.length+(view?.source||[]).length;
      const room=Math.max(1,Math.max(2,budget)-1-Math.min(words,2)-(words>2?1:0)-(choices(row).length?marked.length+2:0)-1-(local.notice?1:0));
      const size=Math.min(rows.length,room),here=seatOf(row);
      if(answerSeat!==here){answerSeat=here;answerTop=0;}
      if(!live)answerTop=0;else if(home<answerTop)answerTop=home;else if(home>=answerTop+size)answerTop=home-size+1;
      answerTop=Math.max(0,Math.min(answerTop,rows.length-size));
      for(let i=answerTop;i<answerTop+size;i++){
        const one=rows[i],lead=i===0?'› ':'  ';
        const mark={...span(lead+shownChars.slice(one.start,one.end).join(''),'loc_repo'),answerRow:true};
        if(!live||i!==home){tail.push(chosenRow([mark],live));continue;}
        const front=lead+shownChars.slice(one.start,at).join(''),rest=shownChars.slice(at,one.end).join('');
        tail.push(chosenRow([{...mark,text:front,caret:true},...(rest?[{...span(rest,'loc_repo'),answerRow:true}]:[])],true));
      }
    }else if(picked&&!choosing(row)){
      const selected=picked.map(v=>{if(row.question.secret)return '•'.repeat([...String(v)].length);const n=choices(row).findIndex(c=>value(c)===v);return (n>=0?String(n+1)+' · ':'')+v;});
      tail.push(chosenRow([span('› '+selected.join(', '),'loc_repo')],active(row)));
    }
    const notice=local.notice?[span(local.notice===ANSWER_FULL?text('full','answer is full · 4,096 bytes'):local.notice,stale?'lvl_r':'dim')]:null;
    if(notice)tail.push(notice);
    if(focused){
      const ready=complete(row,formValues(row));
      const instruction=active(row)?(choosing(row)?text('choose','↑↓ choose · Enter send'):ready?text('send','Enter send'):text('text','type answer')):text(status,status);
      const editing=active(row)&&writable(row);
      const navigate=text(editing?'navigate_write':'navigate','Tab next · Esc/Ctrl+] back');
      const writeHint=local.typing[id]?(choices(row).length?text('writing_choices','Enter send · Ctrl+J newline · ↑ choices'):text('writing','Enter send · Ctrl+J newline')):text('choose_write','↑↓ choose · type to write · Enter send');
      keyLines=keys([...(active(row)?[writable(row)?writeHint:instruction]:[]),navigate],editing?1:Math.max(1,Math.max(2,budget)-2));
      tail.push(...keyLines);
    }else if(status==='open'){keyLines=[[span('Ctrl+] · '+text('open','answer needed'))]];tail.push(...keyLines);}
    const limit=Math.max(2,budget),tailLimit=Math.min(tail.length,Math.max(1,limit-2));
    let footer=tail.slice(-tailLimit);
    const edit=tail.find(line=>line.some(part=>part.caret))||tail.findLast(line=>line.some(part=>part.answerRow));
    // At small heights notices and key hints yield before the live input cell.
    if(edit&&!footer.includes(edit)){
      // The rows that stay are one unbroken stretch of the answer holding the place, then the end of the tail
      // (notice, key hints) as far as it fits: the answer's rows are never shown with some left out between.
      const last=tail.findLastIndex(line=>line.some(part=>part.answerRow)),ends=tail.slice(last+1);
      let keep=tailLimit>1?ends.slice(-(tailLimit-1)):[];
      if(keep.length===1&&local.notice===ANSWER_FULL)keep=[notice];
      const size=tailLimit-keep.length,from=Math.max(0,Math.min(tail.indexOf(edit),last+1-size));
      footer=[...tail.slice(from,from+size),...keep];
    }
    const space=Math.max(0,limit-1-footer.length);
    if(edit&&!space)return [head,...footer].slice(0,limit);
    // The reading hint names the keys that page the body.
    const readingDown=focused&&local.typing[id]&&choices(row).length>0;
    const readingWords=()=>choosing(row)?text('more_choose','more · PgUp/PgDn to read'):
      readingDown?text('more_down','more · ↓ to read'):
      !focused&&active(row)&&choices(row).length?text('more_page','more · focus and PgUp/PgDn to read'):
      text('more','more · focus and ↑↓ to read');
    // 11-22 (the person: "선지가 선택이 안되고 그냥 글에 있는 경우도 있다는거야. 그게 거슬린다"): a question with
    // choices always shows its choice rows (and its answer row), focused or not. Only the question's words page
    // (`+N · more …`, PgUp/PgDn, the wheel). Short of rows it gives up, in this order: the key hints, the empty
    // row, then the thin line, then the words down to one row and the `more` line, and last the choices
    // themselves, which then slide to keep the highlighted one in sight with a line saying there are more.
    if(status==='open'&&choices(row).length>0){
      const words=[...body,...(view?.source||[])],lessKeys=footer.filter(line=>!keyLines.includes(line));
      if(edit&&!lessKeys.includes(edit))lessKeys.splice(0,lessKeys.length,edit);
      const configs=[[gap,footer],[gap,lessKeys],[gap.slice(1),lessKeys],[[],lessKeys]];
      const fit=configs.find(([g,f])=>limit-1-f.length-g.length-marked.length>=Math.min(words.length,2));
      if(fit){
        const [g,f]=fit,room=limit-1-f.length-g.length-marked.length;
        if(words.length<=room){scroll=0;return [head,...words,...g,...marked,...f].slice(0,limit);}
        const page=room-1;
        scroll=Math.max(0,Math.min(scroll,words.length-page));
        const more=[span(`+${words.length-page} · ${readingWords()} ${scroll+1}/${words.length}`)];
        return [head,...words.slice(scroll,scroll+page),more,...g,...marked,...f].slice(0,limit);
      }
      // Not even a row of words, its `more` line and the choices fit: the words keep one row and the `more` line
      // (PgUp/PgDn read them on), and the choices slide in what is left, the highlighted one in sight with a line
      // saying there are more. Tighter still the words keep one row with the head saying they page, and last
      // (one row left, or a limit of 2) the choices alone; with no row of words shown the head promises none.
      const f=lessKeys,room=limit-1-f.length;
      if(room<=0)return [head,...f].slice(0,limit);
      const wordRows=Math.min(words.length,room>=3?2:1),left=room-wordRows;
      const showWords=wordRows>0&&left>=1,spare=showWords?left:room,page=wordRows===2&&words.length>2?1:wordRows;
      let top=[];
      if(showWords){
        scroll=Math.max(0,Math.min(scroll,words.length-page));
        top=words.slice(scroll,scroll+page);
        if(wordRows===2&&page===1)top.push([span(`+${words.length-page} · ${readingWords()} ${scroll+1}/${words.length}`)]);
        else if(words.length>page)head=[...head,span(` +${words.length-page} ${choosing(row)?'PgUp/PgDn':readingDown?'↓':'↑↓'}`)];
      }else scroll=0;
      if(marked.length<=spare)return [head,...top,...marked,...f].slice(0,limit);
      const size=spare>=2?spare-1:spare,lit=Math.max(0,marked.findIndex(parts=>parts[0]?.text==='▸'));
      const from=Math.max(0,Math.min(marked.length-size,lit-Math.floor(size/2)));
      return [head,...top,...marked.slice(from,from+size),...(spare>=2?[[span(`+${marked.length-size} · ${text('more_choices','more choices · ↑↓')}`)]]:[]),...f].slice(0,limit);
    }
    if(content.length>space&&gap.length>1){gap=gap.slice(1);content=lines();}
    if(content.length<=space){scroll=0;return [head,...content,...footer].slice(0,limit);}
    const page=Math.max(1,space-1);
    // The highlighted choice comes onto the page when it changes (↑↓, a number);
    // PgUp/PgDn then read the body from its first row to its last, and the page
    // stays where the reading left it until the choice changes again.
    const mark=choosing(row)?marked.findIndex(parts=>parts[0]?.text==='▸'):-1;
    const lit=mark>=0?body.length+gap.length+mark:-1;
    if(follow&&lit>=0){if(lit<scroll)scroll=lit;else if(lit>=scroll+page)scroll=lit-page+1;}
    scroll=Math.max(0,Math.min(scroll,Math.max(0,content.length-page)));
    // While ↑↓ choose, the body pages with PgUp/PgDn: the hint names the keys that read it.
    // A question with choices reads with PgUp/PgDn once focused (↑↓ choose): the hint names
    // those keys before focus too (11-14), as it does while choosing.
    const reading=readingWords();
    const more=[span(`+${Math.max(0,content.length-page)} · ${reading} ${scroll+1}/${content.length}`)];
    if(space<=1)head=[...head,span(` +${content.length-page} ${choosing(row)?'PgUp/PgDn':readingDown?'↓':'↑↓'}`)];
    return [head,...content.slice(scroll,scroll+page),...(space>1?[more]:[]),...footer].slice(0,limit);
  }
  // The option rows of one choice start with its number (`2 `); its wrapped rows
  // with spaces. Spec E9 rule 53: every option row leads with a two-cell column for the
  // mark (blank, so focusing shifts nothing). While ↑↓ choose, the chosen choice's first row
  // carries `▸` in this session's harness color, bold, and every row of it (each row of a
  // wrapped one) has its words bold in the bright body color; every other choice reads dim
  // grey. Without color the mark and the weight alone tell them apart. 11-15 ("질문 선지 선택 효과를
  // 틴트로 해 명확하게"): every row of the chosen one is marked `tint`, so the popup lays the chosen ground
  // across it (one ground, no other inside it); the answer being typed, and an answer picked, are
  // marked alike while this question is answered here.
  const chosenRow=(parts,on=true)=>on?parts.map(part=>({...part,tint:true})):parts;
  // 11-17: the number is the question's rose and bold on every row (the mark `◑`'s own color; where there is no
  // color, its weight tells it from the words), two cells before the words.
  function highlight(options,at,choosing,canPick){
    let n=-1;
    const bold=part=>({...part,bold:true});
    return options.map(parts=>{
      const lead=String(parts[0]?.text??''),number=/^(\d+)\s+$/.exec(lead);
      if(number)n=Number(number[1])-1;else if(lead.trim())n=-1;
      const [head,...rest]=number?[bold(parts[0]),...parts.slice(1)]:[null,...parts];
      const first=head?[{...head,...(canPick?{choiceIndex:n}:{})}]:[];
      if(!choosing||at<0)return [span('  ',''),...first,...rest];
      if(n!==at)return [span('  ',''),...first,...rest.map(part=>({...part,...(styles.dim||{dim:true}),bold:false}))];
      const words=rest.map(part=>({...part,...(styles.loc_repo||{}),bold:true,dim:false}));
      return chosenRow(number?[{...span('▸','harness'),bold:true,role:'choice-mark'},span(' ',''),...first,...words]:[span('  ',''),...words]);
    });
  }
  // Rule 5: one line per answer on its way, `✓ answer sent · <what was sent>` (11-15 panel polish
  // 2: `✓ answer sent` in the passing green of a route's `✓`, the `·` dim, what was sent in plain
  // text); the panel draws it where the question box stood, never boxed or filled. The answer
  // record's `sent` (secrets already hidden) comes first: when another place's claim won, it is
  // what was sent, not this panel's pick (review 1633, follow-up 3).
  function sentLines(){
    const label=(item,question,v)=>{
      const listed=item.answer_kind==='decision'?[]:question.choices||[];
      const choice=listed.find(c=>value(c)===v);
      return choice===undefined?undefined:String(typeof choice==='object'?choice.label||choice.value:v);
    };
    return items.filter(item=>phase(item)==='sent').map(item=>{
      const local=state(item),shown=[];
      if(Array.isArray(item.sent)&&item.sent.length){
        for(const v of item.sent.map(String)){
          const named=(item.questions||[]).filter(q=>!q.secret).map(q=>label(item,q,v)).find(x=>x!==undefined);
          shown.push(named??v);
        }
      }else for(const question of item.questions||[]){
        for(const v of local.answers[question.id]||[]){
          if(question.secret){shown.push('•••');continue;}
          shown.push(label(item,question,v)??String(v));
        }
      }
      const said=shown.join(', ').replace(/\s+/g,' ').trim();
      return [span(text('sent','✓ answer sent'),'lvl_g'),...(said?[span(' · '),span(said,'loc_repo')]:[])];
    });
  }
  async function submit(){
    const row=current();if(!row)return;
    const local=state(row.item),id=row.question.id;
    if(busy){local.notice=text('busy','action pending · wait for receipt');changed();return;}
    if(!active(row)){const word=terminalState(row)==='open'?unanswerable(row):terminalState(row);
      local.notice=stale?text('changed','question changed · Tab next'):text(word,word);changed();return;}
    const values=formValues(row);
    if(writable(row)&&!values[id]){local.notice=text('empty','empty answer');changed();return;}
    if(!complete(row,values)){local.notice=text('complete','answer every field · Tab next');changed();return;}
    busy=true;local.sending=true;local.notice='';changed();
    let sent=false;const started=moves;
    try{
      const result=await answer({question_id:row.item.id,revision:row.item.revision,...(row.item.answer_kind==='decision'?{decision:values[id][0]}:{answers:values})});
      if(['submitted','received','applied'].includes(result.state)){local.answers=values;local.drafts={};local.typing={};local.caret={};local.state=result.state;sent=true;local.sentAt=Date.now();}
      else{local.state=null;local.notice=result.reason||text('unknown','unknown · check receipt');}
    }catch{local.state='unknown';local.notice=text('unknown','unknown · check receipt');}
    finally{
      busy=false;local.sending=false;
      // Rule 5: the answered question folds and the person is back at the native input; the
      // next question to answer is the panel's first one (Ctrl+] opens on it), so a second
      // Enter never answers it unseen. Had the person moved to another question while this
      // one was sent, they stay there. A refused answer stays in sight with its word.
      const at=sent&&moves!==started&&seat?flat().findIndex(r=>seatOf(r)===seat):-1;
      if(sent&&at<0&&focused){index=Math.max(0,flat().findIndex(r=>active(r)));answeredAt=Date.now();leave();return;}
      if(at>=0)index=at;
      else if(!sent&&moves===started){const back=flat().findIndex(r=>seatOf(r)===seatOf(row));if(back>=0)index=back;}
      changed();
    }
  }
  function pick(row,at,toggle=false){
    const local=state(row.item),id=row.question.id,choice=choices(row)[at];
    if(!active(row)||choice===undefined)return;
    if(toggle&&writable(row)&&local.cursor[id]===at&&local.answers[id]?.length){local.answers[id]=[];local.cursor[id]=-1;}
    else{local.answers[id]=[value(choice)];local.cursor[id]=at;}
    local.typing[id]=false;delete local.caret[id];local.notice='';follow=true;
  }
  function clearFullNotice(row){
    if(row&&state(row.item).notice===ANSWER_FULL)state(row.item).notice='';
  }
  // A cursor without an answer is the default, not a choice the person made.
  // Clear it on the first accepted character, including paste or a clicked row.
  function append(row,c){
    const local=state(row.item),id=row.question.id,chars=[...(local.drafts[id]||'')],at=caretAt(local,id);
    const joined=chars.slice(0,at).join('')+c+chars.slice(at).join('');
    if(Buffer.byteLength(joined)>4096){local.notice=ANSWER_FULL;return;}
    if(!Number.isInteger(local.cursor[id])||local.cursor[id]<0){local.cursor[id]=-1;local.answers[id]=[];}
    local.typing[id]=true;local.drafts[id]=joined;local.caret[id]=at+[...c].length;
  }
  // The place moves and deletes only in a row being typed to that has text (11-22).
  function placing(row){return !!row&&active(row)&&writable(row)&&!!state(row.item).typing[row.question.id]&&!!state(row.item).drafts[row.question.id];}
  // Home/End (Ctrl+A/E) go to the ends of the line holding the place, Ctrl+Home/End to the ends of the text.
  function place(row,to){
    const local=state(row.item),id=row.question.id,chars=[...local.drafts[id]],at=caretAt(local,id);
    local.caret[id]=to==='home'?lineStart(chars,at):to==='end'?lineEnd(chars,at):to==='start'?0:to==='finish'?chars.length:
      to==='wordLeft'?wordLeft(chars,at):to==='wordRight'?wordRight(chars,at):to>0?after(chars,at):at>0?before(chars,at):0;
  }
  function cut(row,from,to){
    const local=state(row.item),id=row.question.id,chars=[...local.drafts[id]];
    if(to<=from)return;
    chars.splice(from,to-from);local.drafts[id]=chars.join('');local.caret[id]=from;
  }
  function removeAt(row){
    const local=state(row.item),id=row.question.id,chars=[...local.drafts[id]],at=caretAt(local,id);
    if(at<chars.length)cut(row,at,after(chars,at));
  }
  // Ctrl+W / Alt+Backspace the word before the place, Alt+D / Ctrl+Delete the one after it; Ctrl+U the line
  // before it, Ctrl+K the line after it.
  function cutWord(row,dir){
    const local=state(row.item),chars=[...local.drafts[row.question.id]],at=caretAt(local,row.question.id);
    if(dir<0)cut(row,wordLeft(chars,at),at);else cut(row,at,wordRight(chars,at));
  }
  function cutLine(row,dir){
    const local=state(row.item),chars=[...local.drafts[row.question.id]],at=caretAt(local,row.question.id);
    if(dir<0)cut(row,lineStart(chars,at),at);else cut(row,at,lineEnd(chars,at));
  }
  // 11-26: a line break goes in a written answer that is no secret; in a secret every line-break key is Enter,
  // as Ctrl+J was before (a line break in a password is a slip). `\` then Enter follows the same rule.
  function breaks(row){return !!row&&active(row)&&writable(row)&&!row.question.secret;}
  function newline(row){if(breaks(row))append(row,'\n');else if(row&&active(row)&&writable(row))enterKey(row);}
  function enterKey(row){
    const local=state(row.item),id=row.question.id;
    if(changeArmed){changeArmed=false;local.notice='';changed();return;}
    // `\` then Enter (the native input's own way): the `\` before the place becomes a line break.
    if(breaks(row)&&local.typing[id]){
      const chars=[...(local.drafts[id]||'')],at=caretAt(local,id);
      if(at>0&&chars[at-1]==='\\'){chars[at-1]='\n';local.drafts[id]=chars.join('');local.caret[id]=at;return;}
    }
    // Enter sends the highlighted choice, the same value its number key picks.
    if(active(row)&&cursorOf(row)>=0){
      local.answers[id]=[value(choices(row)[cursorOf(row)])];local.notice='';
      if(!complete(row,formValues(row))&&(row.item.questions||[]).length>1){move(1);return;}
    }
    submit();
  }
  // ↑↓ inside a written answer of several rows: the place goes to the row above or below, at the cell column
  // it stood in (held while ↑↓ repeat). False on the first or last row, where ↑↓ keep their meaning.
  function vertical(row,dir){
    const local=state(row.item),id=row.question.id,chars=shownOf(local.drafts[id]||'',row.question.secret),at=caretAt(local,id);
    const rows=answerRows(chars,answerColumns),home=rowOf(rows,at),to=home+dir;
    if(to<0||to>=rows.length)return false;
    if(goal===null)goal=cells(chars.slice(rows[home].start,at));
    const target=rows[to];let i=target.start,width=0;
    while(i<target.end){const j=after(chars,i),w=cells(chars.slice(i,j));if(width+w>goal)break;width+=w;i=j;}
    // A wrapped row's end is the next row's start: the place stays on this row, before its last character.
    if(i===target.end&&i>target.start&&to+1<rows.length&&rows[to+1].start===target.end)i=before(chars,i);
    local.caret[id]=i;return true;
  }
  function character(c){
    const row=current(),local=row&&state(row.item),id=row?.question.id;
    goal=null;
    if(pasting){
      // 11-26: a pasted text goes in whole: its line breaks (CR, LF or CR LF, as terminals paste them) are
      // line breaks, never Enter, and a tab stays; any other control character is left out (the delivery
      // to the session refuses them).
      if(row&&active(row)&&writable(row)){
        if(c==='\n'&&pasteCR){pasteCR=false;return;}
        pasteCR=c==='\r';
        if(c==='\r'||c==='\n'){if(row.question.secret)heldBreaks++;else append(row,'\n');}
        else if(c==='\t'||!/[\x00-\x1f\x7f-\x9f]/.test(c)){
          // A secret's line breaks stay only between its characters: the ones it ends with are dropped.
          if(heldBreaks){const held=heldBreaks;heldBreaks=0;append(row,'\n'.repeat(held));}
          append(row,c);
        }
      }
      return;
    }
    clearFullNotice(row);
    if(c==='\x1d'||c==='\x1b'){leave();return;}
    if(c==='\t'){move(1);return;}
    // Ctrl+J (LF) puts a line break in a written answer; on a question answered only by a choice, and in a
    // secret, it is Enter, as before.
    if(c==='\n'&&breaks(row)){newline(row);return;}
    if(c==='\r'||c==='\n'){if(row)enterKey(row);return;}
    if(!row){if('rwfp'.includes(c))action(c);return;}
    if(!active(row))return;
    if(c==='\x01'||c==='\x05'){if(placing(row))place(row,c==='\x01'?'home':'end');return;}
    if(c==='\x17'||c==='\x15'||c==='\x0b'){if(placing(row)){if(c==='\x17')cutWord(row,-1);else cutLine(row,c==='\x15'?-1:1);}return;}
    if(c==='\x7f'||c==='\b'){
      const chars=[...(local.drafts[id]||'')],at=caretAt(local,id),from=before(chars,at);
      chars.splice(from,at-from);local.drafts[id]=chars.join('');local.caret[id]=from;return;
    }
    if(local.typing[id]){if(c>=' ')append(row,c);return;}
    if(/^[1-9]$/.test(c)&&choices(row).length){
      pick(row,Number(c)-1,true);return;
    }
    if(writable(row)&&c>=' '){append(row,c);return;}
    if('rwfp'.includes(c)){action(c);return;}
  }
  function input(data){
    for(let i=0;i<data.length;i++){
      if(!focused){changed();return data.subarray(i);}
      const byte=data[i],c=String.fromCharCode(byte);
      if(skipTail){if((byte&0xc0)===0x80)continue;skipTail=false;}
      if(escape){
        clearTimeout(escapeTimer);escapeTimer=undefined;
        // An application-cursor terminal sends arrows as SS3 (`ESC O A`).
        if(escape==='\x1b'&&c!=='['&&!(c==='O'&&!pasting)){
          // In a paste the Esc is left out and what follows is text (its bytes read whole, as Fleet reads them);
          // another Esc may start a sequence.
          if(pasting){pasteGoesOn();if(byte===0x1b){escape=c;continue;}escape='';for(const char of decoder.write(data.subarray(i,i+1)))character(char);continue;}
          const row=current();clearFullNotice(row);goal=null;
          // What follows an Esc in this read (or within the Esc window) other than `[` or `O` (an arrow or
          // function key) is an Alt combination. 11-26: Alt/Option+Enter (`ESC CR`, `ESC LF`) puts a line break
          // in a written answer, Alt+B/F step a word, Alt+D and Alt+Backspace delete one. Any other one (Esc
          // then a control byte too) is dropped, never the native's (an Enter there would submit its text, a ^C
          // interrupt its turn); the popup stands. Only a second Esc closes it, and is dropped.
          escape='';
          if(byte===0x1b){leave();escapedAt=Date.now();return data.subarray(i+1);}
          if(byte===0x0d||byte===0x0a)newline(row);
          else if(c==='b'||c==='f'){if(placing(row))place(row,c==='b'?'wordLeft':'wordRight');}
          else if(c==='d'){if(placing(row))cutWord(row,1);}
          else if(byte===0x7f||byte===0x08){if(placing(row))cutWord(row,-1);}
          else if(byte>=0xc0)skipTail=true;
          continue;
        }
        escape+=c;
        if(pasting)pasteGoesOn();
        if(escape==='\x1b[200~'){if(!pasting){clearFullNotice(current());pasteCR=false;heldBreaks=0;}pasting=true;escape='';pasteGoesOn();}
        else if(escape==='\x1b[201~'){endPaste();escape='';}
        else if(escape.length>2&&byte>=0x40&&byte<=0x7e||escape.length===3&&escape[1]==='O'){
          if(!pasting){
            const up=escape==='\x1b[A'||escape==='\x1bOA',down=escape==='\x1b[B'||escape==='\x1bOB',row=current();
            clearFullNotice(row);
            if(!up&&!down)goal=null;
            if(escape==='\x1b[Z')move(-1);
            else if(NEWLINE_CSI.test(escape))newline(row);
            else if(WORD_LEFT.includes(escape)||WORD_RIGHT.includes(escape)){if(placing(row))place(row,WORD_LEFT.includes(escape)?'wordLeft':'wordRight');}
            else if(TEXT_START.includes(escape)||TEXT_END.includes(escape)){if(placing(row))place(row,TEXT_START.includes(escape)?'start':'finish');}
            else if(WORD_DELETE.includes(escape)){if(placing(row))cutWord(row,1);}
            else if(escape==='\x1b[C'||escape==='\x1b[D'||escape==='\x1bOC'||escape==='\x1bOD'){
              // 11-22 (the person: "구획을 안옮겨도 되는데. 굳이"): in the question input ←→ only move the place in
              // the text; with no text they do nothing, and no longer change sections.
              if(placing(row))place(row,escape.endsWith('C')?1:-1);
            }
            else if(START.includes(escape)||END.includes(escape)){if(placing(row))place(row,START.includes(escape)?'home':'end');}
            else if(escape==='\x1b[3~'){if(placing(row))removeAt(row);}
            // 11-26: in a written answer of several rows ↑↓ go between its rows; from its first row ↑ goes to the
            // choices (or reads the question) and from its last ↓ reads on, as they did.
            else if((up||down)&&placing(row)&&vertical(row,up?-1:1)){/* the place moved a row */}
            else if(up&&row&&state(row.item).typing[row.question.id]&&choices(row).length){state(row.item).typing[row.question.id]=false;delete state(row.item).caret[row.question.id];scroll=0;follow=true;}
            else if((up||down)&&choosing(row)){const local=state(row.item),id=row.question.id,n=choices(row).length,at=cursorOf(row);
              if(down&&at===n-1&&writable(row))local.typing[id]=true;
              else{local.cursor[id]=at<0?0:Math.max(0,Math.min(n-1,at+(up?-1:1)));local.answers[id]=[value(choices(row)[local.cursor[id]])];}
              scroll=0;follow=true;}
            else if(up||escape==='\x1b[5~'){scroll-=escape.includes('5')?4:1;follow=false;}else if(down||escape==='\x1b[6~'){scroll+=escape.includes('6')?4:1;follow=false;}
          }
          escape='';
        }else if(escape.length>64)escape='';
      }else if(byte===0x1b){escape=c;if(pasting)pasteGoesOn();else escapeTimer=setTimeout(()=>{if(escape==='\x1b'){leave();escapedAt=Date.now();}},25);}
      // Ctrl+] leaves the popup; 11-26: also in a paste whose end mark has not come, when it is pressed (a read of
      // its own). One inside the pasted bytes is their control character, left out: the rest of that paste never
      // goes on to the native.
      else if(byte===0x1d&&(!pasting||data.length===1)){leave();return data.subarray(i+1);}
      else{if(pasting)pasteGoesOn();for(const char of decoder.write(data.subarray(i,i+1)))character(char);}
    }
    changed();return Buffer.alloc(0);
  }
  // The first input after an answer returned focus: a lone Esc within a few seconds was meant
  // for the popup it left (rule 5), so it is taken here, and so is a lone Enter (11-15: the
  // second press of an Enter meant for the popup never submits the native input's text); any
  // other input goes on as typed. The same goes for the lone Esc within a second of the Esc that closed
  // the popup (a second press of the key that closed it is no interrupt or cancel of the native's).
  function answeredEscape(data){
    const now=Date.now(),fresh=answeredAt&&now-answeredAt<10000,closed=escapedAt&&now-escapedAt<1000;answeredAt=escapedAt=0;
    return data.length===1&&(!!fresh&&[0x1b,0x0d,0x0a].includes(data[0])||!!closed&&data[0]===0x1b);
  }
  // 11-15 (popup design A): the panel's summary of the question Ctrl+] opens on (the one shown,
  // else the first answerable one): how many there are, its two-row and one-row forms and its
  // whole first line, from the backend's rows at the panel's width. The popup reads it whole.
  // `answerable` (panel polish 1): it is one a press answers here, now (the panel paints it so).
  function summary(){
    const listed=flat();if(!listed.length)return null;
    let row=current();
    if(!row||!active(row)){const open=listed.find(r=>active(r));if(open)row=open;}
    const view=(row||listed[0]).question.display||{};
    const fallback=[[span('◑ ','g_blocked'),span((row||listed[0]).question.question||'?','loc_repo')]];
    return {count:listed.length,label:text('questions','QUESTIONS'),rows:view.brief||fallback,one:view.brief_one||fallback.slice(0,1),
      line:view.brief_line||fallback[0].slice(1),choices:choices(row||listed[0]).length,answerable:active(row||listed[0])};
  }
  return {answeredEscape,escaped(){escapedAt=Date.now();},get focused(){return focused;},get waitingForReceipt(){return waitingForReceipt();},get pasting(){return pasting;},
    get question(){return !!current();},
    get typing(){const row=current();return focused&&!!row&&!!state(row.item).typing[row.question.id];},
    enter,leave,update,input,detail:preview,summary,sent:sentLines,references,
    choose(at){const row=current();if(focused&&row){pick(row,at);changed();}},
    write(){const row=current();if(focused&&row&&active(row)&&writable(row)){state(row.item).typing[row.question.id]=true;changed();}},
    // The wheel over the popup reads the question on, a row at a time, as ↑↓ and PgUp/PgDn do.
    wheel(delta){if(!focused||!current())return;scroll+=delta;follow=false;changed();}};
};
