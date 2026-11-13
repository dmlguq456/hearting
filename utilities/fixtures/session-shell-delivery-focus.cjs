// Source-bound regression: visible shell focus must not leak keys into native delivery.
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const root=process.argv[2],src=fs.readFileSync(path.join(root,'utilities/session-shell/host.cjs'),'utf8');
function section(start,end){let a=src.indexOf(start),b=src.indexOf(end,a+start.length);assert(a>=0&&b>a);return src.slice(a,b);}
const functions=section('function openSettings(){',"let settingsTail=''")+section('function panelMouse(', 'function displayReporter(')+section('function inputBytes(', 'function exact(');
function fixture(delivery){
 let held=[],saved=[],native=[],edited=[],focused=false;
 const context={Buffer,console,currentDelivery:delivery,settings:undefined,displayConfiguration:{layout:'side',side_width:40},
  panelView:require(path.join(root,'utilities/session-shell/panel-view.cjs')),cols:133,harness:'claude',epoch:0,empty:true,
  fleetView:undefined,paste:false,relay:false,dragging:false,heldBytes:0,routeDetail:false,routeArea:null,routeScroll:0,sessionDetails:false,keyTail:'',actionLines:[],
  panelHits:[{row:0,left:30,right:38,action:'settings'}],geometry:()=>({side:true,panelColumn:93}),panelRow:()=>0,height:()=>30,panelColumns:()=>40,
  terminal:{modes:{mouseTrackingMode:'none'}},queueLayout(){},queueDisplay(){},rebuildDisplay(){},observeInput(){},openFleet(){},
  // The press targets stand for the placement on screen (#1586 review follow-up 5); a hand fold choice.
  layoutQueued:false,panelHitsPlacement:'shown',placement:()=>'shown',foldByHand(){},
  source:(op,value)=>{saved.push({op,value});return Promise.resolve({ok:true,settings:value.settings});},
  settingsInput(){saved.push({op:'settings-input'});},
  inputOwner:{hold:b=>held.push(Buffer.from(b))},child:{write:b=>native.push(Buffer.from(b))},process:{stdin:{pause(){}}},
  block:{leave(){focused=false;},enter(){focused=true;},get focused(){return focused;},input(data){edited.push(Buffer.from(data));return Buffer.alloc(0);}}};
 vm.createContext(context);vm.runInContext(functions+'\nthis.mouse=panelMouse;this.bytes=inputBytes;',context);
 return {context,held,saved,native,edited};
}
let failures=0;
function check(name,fn){try{fn();console.log('PASS',name);}catch(error){failures++;console.log('ASSERT_FAIL',name,error.message);}}
check('normal settings click routes keys to its settings editor',()=>{
 const f=fixture(undefined);assert.equal(f.context.mouse(0,130,1,false),true);assert(f.context.settings);
 f.context.bytes(Buffer.from('\r'));assert.equal(f.saved[0]?.op,'settings-input');assert.equal(f.held.length,0);
});
check('peer delivery must not show a settings editor while sending its Enter to native hold',()=>{
 const f=fixture('fixture-peer-ref');assert.equal(f.context.mouse(0,130,1,false),true);assert(f.context.settings);
 f.context.bytes(Buffer.from('\r'));
 console.log('DELIVERY_SETTINGS',JSON.stringify({settingsVisible:!!f.context.settings,settingsInputCalls:f.saved.length,heldHex:Buffer.concat(f.held).toString('hex'),nativeWrites:f.native.length}));
 assert.equal(f.held.length,0,'Enter intended for the visible settings editor entered the native delivery hold queue');
});
check('ordinary native typing remains held during peer delivery',()=>{
 const f=fixture('fixture-peer-ref');f.context.bytes(Buffer.from('native-draft'));
 assert.equal(Buffer.concat(f.held).toString(),'native-draft');assert.equal(f.saved.length,0);
});
check('mouse-opened question keeps its draft out of native hold',()=>{
 const f=fixture('fixture-peer-ref');f.context.panelAction('question');
 f.context.bytes(Buffer.from('question-draft'));assert.equal(Buffer.concat(f.edited).toString(),'question-draft');assert.equal(f.held.length,0);
 f.context.block.leave();f.context.bytes(Buffer.from('native-after'));
 assert.equal(Buffer.concat(f.held).toString(),'native-after');
});
check('settings leave retains native bytes in their original order',()=>{
 const f=fixture('fixture-peer-ref');f.context.bytes(Buffer.from('first'));
 f.context.mouse(0,130,1,false);f.context.bytes(Buffer.from('\r'));f.context.cancelSettings();
 f.context.bytes(Buffer.from('second'));
 assert.deepEqual(f.held.map(b=>b.toString()),['first','second']);assert.equal(f.native.length,0);
});
check('display failure still holds native bytes even with stale shell focus',()=>{
 const f=fixture('fixture-peer-ref');f.context.mouse(0,130,1,false);f.context.block.enter();f.context.relay=true;
 f.context.bytes(Buffer.from('raw-draft'));assert.equal(Buffer.concat(f.held).toString(),'raw-draft');assert.equal(f.saved.length,0);
});
console.log(JSON.stringify({checks:6,assertionFailures:failures,driverErrors:0,productionSourceEdited:false,operatingInput:false}));
process.exitCode=failures?1:0;
