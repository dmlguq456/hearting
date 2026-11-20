'use strict';
// Selecting text in the native area with the mouse, for a native that does not read the mouse
// itself. Pure helpers over the host's own VT copy of the native screen: nothing here opens a
// process, a file or a timer. The cells are the native's (column 0 is its first column; the
// columns a side panel took on the left are the host's business, not counted here).
const MULTI_CLICK_MS=450,MAX_COPY=100000;

// A selection is two cells (`a` where it was pressed, `b` where it is now) and a `mode`:
// `cell` (a drag), `word` or `line` (a double or triple press). `ordered` puts the earlier cell first.
function ordered(a,b){return a.y<b.y||a.y===b.y&&a.x<=b.x?[a,b]:[b,a];}

// The cell range a row shows flipped: [from,to] inclusive, widened so a wide character is taken
// whole (its second cell is empty and carries nothing of its own), or null for a row outside.
function rowSpan(view,selection,y){
  const [a,b]=ordered(selection.a,selection.b);
  if(y<a.y||y>b.y)return null;
  const line=view.buffer.active.getLine(view.buffer.active.viewportY+y);
  let from=y===a.y?a.x:0,to=y===b.y?b.x:view.cols-1;
  if(line){
    if(from>0&&line.getCell(from)?.getWidth()===0)from--;
    if(to+1<view.cols&&line.getCell(to)?.getWidth()===2)to++;
  }
  return from>to?null:[from,to];
}

const kind=ch=>/\s/u.test(ch)?0:/[\p{L}\p{N}_\-./~:@%+#]/u.test(ch)?1:2;
// The word under a cell (a run of letters, digits and path-like marks; a run of spaces or of
// other marks is its own kind of word), on its own row.
function wordAt(view,x,y){
  const line=view.buffer.active.getLine(view.buffer.active.viewportY+y);
  if(!line)return {a:{x,y},b:{x,y}};
  const at=i=>{const cell=line.getCell(i);return !cell?' ':cell.getWidth()===0?null:cell.getChars()||' ';};
  // A cell that is the second half of a wide character stands for the first half.
  let c=x;while(c>0&&at(c)===null)c--;
  const want=kind(at(c)||' ');
  let from=c,to=c;
  for(let i=c-1;i>=0;i--){const ch=at(i);if(ch===null)continue;if(kind(ch)!==want)break;from=i;}
  for(let i=c+1;i<view.cols;i++){const ch=at(i);if(ch===null){to=i;continue;}if(kind(ch)!==want)break;to=i;}
  return {a:{x:from,y},b:{x:to,y}};
}
// The whole logical line under a row: the rows a long line wrapped onto are one line.
function lineAt(view,y){
  const buffer=view.buffer.active,top=buffer.viewportY;
  let first=y,last=y;
  while(first>0&&buffer.getLine(top+first)?.isWrapped)first--;
  while(last+1<view.rows&&buffer.getLine(top+last+1)?.isWrapped)last++;
  return {a:{x:0,y:first},b:{x:view.cols-1,y:last}};
}

// The cells a row's text takes, from its first cell on (a blank row takes none).
function used(line,cols){
  for(let x=cols-1;x>=0;x--){const cell=line.getCell(x);if(cell?.getChars())return x+Math.max(1,cell.getWidth());}
  return 0;
}

// The text of the selection as the person reads it: a row that wrapped onto the next joins it
// without a break, any other row ends its line, and the blanks at the end of a line are cut.
function textOf(view,selection){
  const buffer=view.buffer.active,[a,b]=ordered(selection.a,selection.b);
  let out='';
  for(let y=a.y;y<=b.y&&y<view.rows;y++){
    const line=buffer.getLine(buffer.viewportY+y);
    if(!line)continue;
    const span=rowSpan(view,selection,y);
    if(!span)continue;
    let part=line.translateToString(false,span[0],span[1]+1);
    const continues=y<b.y&&!!buffer.getLine(buffer.viewportY+y+1)?.isWrapped;
    if(!continues&&(span[1]>=view.cols-1||span[1]>=used(line,view.cols)-1))part=part.replace(/[ \t]+$/,'');
    out+=part+(y<b.y&&!continues?'\n':'');
  }
  return out;
}

// What the outer terminal is told to copy: OSC 52 to the clipboard, the text in base64.
function copySequence(text){
  if(!text)return '';
  let bytes=Buffer.from(text,'utf8');
  // A copy this long is cut on a character boundary (the cut half-character decodes to U+FFFD).
  if(bytes.length>MAX_COPY)bytes=Buffer.from(bytes.subarray(0,MAX_COPY).toString('utf8').replace(/\uFFFD+$/,''),'utf8');
  return '\x1b]52;c;'+bytes.toString('base64')+'\x07';
}

// A clipboard write the native itself asked for (`Pc;Pd`): only a write whose data is plain base64
// goes on. A read (`?`), an empty or a malformed one goes nowhere.
function nativeCopy(data){
  const at=String(data).indexOf(';');
  if(at<0)return '';
  const target=data.slice(0,at),payload=data.slice(at+1);
  if(!payload||payload.length>1048576||!/^[A-Za-z0-9+/]+={0,2}$/.test(payload)||payload.length%4===1)return '';
  return '\x1b]52;'+(/^[cpqs0-7]+$/.test(target)?target:'c')+';'+payload+'\x07';
}

// Presses of the same cell within a short time count up: one cell, two a word, three a line.
function clicks(previous,x,y,now){
  return previous&&previous.x===x&&previous.y===y&&now-previous.time<=MULTI_CLICK_MS?previous.count%3+1:1;
}

module.exports={ordered,rowSpan,wordAt,lineAt,textOf,copySequence,nativeCopy,clicks,MAX_COPY};
