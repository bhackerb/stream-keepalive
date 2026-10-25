const test=require('node:test');const assert=require('node:assert/strict');const vm=require('node:vm');const fs=require('node:fs');
test('external MLB source selection waits for video and only clicks once in an owned exact-host top frame',async()=>{
 for(const [owned,hostname,expected]of [[true,'roxiestreams.su',1],[false,'roxiestreams.su',0],[true,'roxiestreams.su.evil.example',0]]){
  let tick,video=null,clicks=0;const window={};window.top=window;
  const button={textContent:'Stream 2',click:()=>{clicks++;setImmediate(()=>{video={...video,muted:false,volume:1};});}};
  const document={addEventListener:()=>{},getElementById:()=>null,querySelectorAll:s=>s==='button'?[button]:s==='video'&&video?[video]:[]};
  const api={runtime:{onMessage:{addListener:()=>{}},sendMessage:async()=>({owned,fill:false,audio:{muted:true,volume:.4},audio_revision:0})}};
  vm.runInNewContext(fs.readFileSync(__dirname+'/content.js','utf8'),{browser:api,document,window,URL,location:{hostname,href:'https://'+hostname+'/mlb-streams-1?sk-source=2'},Date,setImmediate,setInterval:f=>{tick=f;}});
  await new Promise(setImmediate);assert.equal(clicks,0);
  video={paused:false,currentTime:1,videoWidth:1280,videoHeight:720,readyState:4,buffered:{length:0},seekable:{length:0}};
  await tick();await new Promise(setImmediate);await tick();assert.equal(clicks,expected);if(owned&&expected){assert.equal(video.muted,true);assert.equal(video.volume,.4);}
 }
});
test('idle cursor hides only in owned filled frames and returns on movement, Escape, parking or disconnect',async()=>{
 let tick,now=0,reply={owned:true,fill:true},style=null;const events={};
 const document={head:{append:s=>{style=s;}},createElement:()=>({remove(){style=null;}}),
  getElementById:id=>id==='streamkeeper-cursor-style'?style:null,querySelectorAll:()=>[],
  addEventListener:(name,fn)=>{(events[name]??=[]).push(fn);}};
 const api={runtime:{onMessage:{addListener:()=>{}},sendMessage:async()=>reply}};
 vm.runInNewContext(fs.readFileSync(__dirname+'/content.js','utf8'),{browser:api,document,window:{},Date:{now:()=>now},setInterval:f=>{tick=f;}});
 await new Promise(setImmediate);assert.equal(style,null);
 now=3000;await tick();assert.match(style.textContent,/cursor:none/);
 events.pointermove.forEach(f=>f({}));assert.equal(style,null);
 now=6000;await tick();assert.ok(style);
 reply.parked=true;await tick();assert.equal(style,null);
 reply.parked=false;await tick();assert.ok(style);
 reply.owned=false;await tick();assert.equal(style,null);
 reply.owned=true;reply.fill=false;await tick();assert.equal(style,null);
 reply.fill=true;await tick();assert.ok(style);
 events.keydown.forEach(f=>f({key:'Escape'}));assert.equal(style,null);
 now=9000;await tick();assert.equal(style,null);
});
async function content(owned,extra=[]){
 let listener;
 const video={muted:true,volume:1,paused:false,currentTime:1,videoWidth:1280,videoHeight:720,readyState:4,buffered:{length:0},seekable:{length:0},getVideoPlaybackQuality:()=>({totalVideoFrames:10})};
 const api={runtime:{onMessage:{addListener:f=>{listener=f;}},sendMessage:async()=>({owned,fill:false})}};
 const document={addEventListener:()=>{},querySelectorAll:s=>s==='video'||s==='video,audio'?[video,...extra]:[],getElementById:()=>null};
 vm.runInNewContext(fs.readFileSync(__dirname+'/content.js','utf8'),{browser:api,document,window:{},setInterval:()=>{},Date});
 await new Promise(r=>setImmediate(r));
 function command(m){let reply;listener({sk:true,...m},{},r=>{reply=r;});return reply;}
 return {video,command};
}
test('owned media volume and mute are read back before confirmation',async()=>{
 const h=await content(true);const r=h.command({op:'audio',muted:false,volume:.35});
 assert.equal(r.confirmed,true);assert.equal(r.media_count,1);assert.equal(h.video.muted,false);assert.equal(h.video.volume,.35);
});
test('unowned media and invalid audio payloads never change audio',async()=>{
 const h=await content(false);assert.equal(h.command({op:'audio',muted:false,volume:.35}).confirmed,false);assert.equal(h.video.muted,true);
 const owned=await content(true);for(const args of [{muted:1},{muted:false,volume:Infinity},{muted:false,volume:-1}])assert.equal(owned.command({op:'audio',...args}).confirmed,false);
 assert.equal(owned.video.muted,true);assert.equal(owned.video.volume,1);
});
test('a player rejecting requested settings cannot return confirmed audio',async()=>{
 const h=await content(true);Object.defineProperty(h.video,'volume',{get:()=>1,set:()=>{}});
 assert.equal(h.command({op:'audio',muted:false,volume:.35}).confirmed,false);
});

test('primary volume touches only the largest decoded video',async()=>{
 const ad={muted:true,volume:1,videoWidth:320,videoHeight:180};
 const h=await content(true,[ad]);const r=h.command({op:'audio',muted:false,volume:.25,primary_only:true});
 assert.equal(r.confirmed,true);assert.equal(r.media_count,1);assert.equal(r.video_count,1);
 assert.equal(h.video.volume,.25);assert.equal(h.video.muted,false);assert.equal(ad.volume,1);assert.equal(ad.muted,true);
});
test('primary volume without decoded video fails before any media mutation',async()=>{
 const h=await content(true);h.video.videoWidth=0;
 const r=h.command({op:'audio',muted:false,volume:.25,primary_only:true});
 assert.equal(r.confirmed,false);assert.equal(h.video.muted,true);assert.equal(h.video.volume,1);
});

async function sticky(owned=true,audio={muted:false,volume:.3}){
 const events={},messages=[];let tick,listener,revision=2;
 const makeVideo=()=>{const callbacks={};return {muted:true,volume:1,controls:true,tagName:'VIDEO',getAttribute:()=>null,paused:false,currentTime:1,videoWidth:1280,videoHeight:720,readyState:4,buffered:{length:0},seekable:{length:0},play:async()=>{},getVideoPlaybackQuality:()=>({totalVideoFrames:10}),addEventListener:(k,f)=>{callbacks[k]=f;},fire:k=>callbacks[k]?.()};};
 let video=makeVideo();
 const document={addEventListener:(k,f)=>{events[k]=f;},querySelectorAll:s=>s==='video'||s==='video,audio'?[video]:[],getElementById:()=>null};
 const api={runtime:{onMessage:{addListener:f=>{listener=f;}},sendMessage:async m=>{messages.push(m);if(m.op==='audio_intent'){audio=m.audio;revision++;}return {owned,fill:false,autoplay:true,start_muted:true,parked:false,audio,audio_revision:revision};}}};
 vm.runInNewContext(fs.readFileSync(__dirname+'/content.js','utf8'),{browser:api,document,window:{},Date,setInterval:f=>{tick=f;}});
 const settle=async()=>{await new Promise(setImmediate);};await settle();
 return {get video(){return video;},messages,settle,tick,replace:()=>{video=makeVideo();},gesture:(trusted=true)=>events.pointerdown({isTrusted:trusted,target:video}),command:m=>listener({sk:true,...m},{},()=>{})};
}
test('saved manual audio overrides startup mute and script resets; replacement video inherits mute and volume',async()=>{
 const h=await sticky();assert.equal(h.video.muted,false);assert.equal(h.video.volume,.3);
 h.video.muted=true;h.video.volume=1;h.video.fire('volumechange');assert.equal(h.video.muted,false);assert.equal(h.video.volume,.3);
 h.replace();await h.tick();assert.equal(h.video.muted,false);assert.equal(h.video.volume,.3);
 assert.equal(h.messages.filter(m=>m.op==='audio_intent').length,0);
});
test('trusted player audio gesture updates intent; synthetic events and unowned frames cannot',async()=>{
 const h=await sticky();h.gesture();h.video.muted=true;h.video.volume=.6;h.video.fire('volumechange');await h.settle();
 assert.equal(h.messages.filter(m=>m.op==='audio_intent').length,1);assert.equal(h.video.muted,true);assert.equal(h.video.volume,.6);
 h.gesture(false);h.video.muted=false;h.video.fire('volumechange');assert.equal(h.video.muted,true);
 const personal=await sticky(false);personal.gesture();personal.video.fire('volumechange');await personal.settle();assert.equal(personal.messages.filter(m=>m.op==='audio_intent').length,0);assert.equal(personal.video.volume,1);
});
test('native audio command supersedes older heartbeat preferences',async()=>{
 const h=await sticky();h.command({op:'audio',muted:true,volume:.7,audio_revision:3});await h.tick();
 assert.equal(h.video.muted,true);assert.equal(h.video.volume,.7);
});
test('a manually muted player stays muted through script unmute and replacement',async()=>{
 const h=await sticky(true,{muted:true,volume:.45});
 h.video.muted=false;h.video.fire('volumechange');assert.equal(h.video.muted,true);
 h.replace();await h.tick();assert.equal(h.video.muted,true);assert.equal(h.video.volume,.45);
});
