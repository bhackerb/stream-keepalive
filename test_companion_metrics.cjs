/* Hermetic execution of the actual content script, including delayed replies. */
const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const source=fs.readFileSync(__dirname+'/companion/content.js','utf8');
function fixture({duration=Infinity,delayed=false}={}){
  const messages=[],timers=[],intervals=[];let observer,resolve;
  const video={currentTime:100,duration,videoWidth:1920,videoHeight:1080,readyState:4,paused:false,ended:false,muted:true,volume:1,playbackRate:1,
    buffered:{length:2,start:i=>i?95:0,end:i=>i?108:5},seekable:{length:1,end:()=>120},
    getVideoPlaybackQuality:()=>({totalVideoFrames:600,droppedVideoFrames:12}),addEventListener:()=>{}};
  let scans=0;
  const document={querySelectorAll:s=>{if(s==='video'){scans++;return [video];}return [];},getElementById:()=>null,addEventListener:()=>{}};
  const api={runtime:{onMessage:{addListener:()=>{}},sendMessage:async m=>{messages.push(m);if(delayed)await new Promise(r=>resolve=r);return {owned:true,fill:false};}}};
  const context={browser:api,document,window:{},Date,console,MutationObserver:class{constructor(cb){observer=cb;}observe(){}},setInterval:f=>intervals.push(f),setTimeout:f=>timers.push(f)};
  vm.createContext(context);vm.runInContext(source,context);
  return {context,video,messages,timers,intervals,mutate:()=>observer(),release:()=>resolve(),scans:()=>scans};
}
const flush=()=>new Promise(r=>setImmediate(r));
test('reports dropped frames, buffer in the containing range and seekable-edge distance',async()=>{
  const f=fixture();await flush();
  const h=f.messages[0].health;
  assert.equal(h.dropped_frames,12);assert.equal(h.decoded_frames,600);
  assert.equal(h.buffer_ahead_seconds,8);assert.equal(h.live_edge_distance_seconds,20);
  assert.equal(h.playback_rate,1);
});
test('VOD does not claim live latency and gaps do not claim buffered media',async()=>{
  const f=fixture({duration:500});await flush();
  assert.equal(f.messages[0].health.live_edge_distance_seconds,null);
  f.video.currentTime=50;await f.intervals[0]();
  assert.equal(f.messages[1].health.buffer_ahead_seconds,null);
});
test('slow native replies cannot accumulate overlapping health requests',async()=>{
  const f=fixture({delayed:true});
  for(let i=0;i<100;i++)await f.intervals[0]();
  assert.equal(f.messages.length,1);
  f.release();await flush();
  const tick=f.intervals[0]();assert.equal(f.messages.length,2);f.release();await tick;
});
test('mutation bursts coalesce scans and unowned documents schedule none',async()=>{
  const f=fixture({delayed:true});f.mutate();assert.equal(f.timers.length,0);
  f.release();await flush();
  for(let i=0;i<100;i++)f.mutate();
  assert.equal(f.timers.length,1);const before=f.scans();f.timers.shift()();
  assert.ok(f.scans()>before);
});
test('failed health requests release the pending flag for the next tick',async()=>{
  const f=fixture();await flush();
  f.context.browser.runtime.sendMessage=async()=>{throw Error('Disconnected');};
  await f.intervals[0]();
  let calls=0;f.context.browser.runtime.sendMessage=async()=>{calls++;return {owned:false};};
  await f.intervals[0]();assert.equal(calls,1);
});
