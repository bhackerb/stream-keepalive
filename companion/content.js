/* Health and user-authoritative audio in proven owned frames. Never remove iframe sandbox. */
(()=>{
if(globalThis.__streamKeeperContentLoaded)return;
globalThis.__streamKeeperContentLoaded=true;
const skAPI=globalThis.browser || chrome;
let skOwned=false, skFill=false, skEscaped=false, skLastTime=null, skLastFrames=null, skLastProgress=Date.now(), skEverProgress=false, skStarted=false, skParked=false;
// Persist intent in the extension, never infer it from arbitrary volumechange events.
let skAudio=null, skAudioRevision=0, skAudioGestureUntil=0, skAudioPending=false;
const skWatched=new WeakSet();
let skTickPending=false, skWatchPending=false;
// Each owned frame hides its idle pointer, including iframe player surfaces.
let skPointerMoved=Date.now();
function skShowPointer(){document.getElementById('streamkeeper-cursor-style')?.remove();}
function skPointerActivity(){skPointerMoved=Date.now();skShowPointer();}
for(const type of ['pointermove','pointerdown','keydown'])document.addEventListener(type,skPointerActivity,true);
function skUpdatePointer(reply,v){
  if(!skOwned||!skFill||skEscaped||reply?.parked||v?.paused||v?.ended||Date.now()-skPointerMoved<3000){skShowPointer();return;}
  if(document.getElementById('streamkeeper-cursor-style'))return;
  const parent=document.head||document.documentElement;if(!parent)return;
  const style=document.createElement('style');style.id='streamkeeper-cursor-style';
  style.textContent='html,body,body *{cursor:none!important}';parent.append(style);
}
function skPrimary(){return [...document.querySelectorAll('video')].sort((a,b)=>b.videoWidth*b.videoHeight-a.videoWidth*a.videoHeight)[0];}
function skApplyAudio(){
  if(!skOwned || !skAudio || skAudioPending || Date.now()<skAudioGestureUntil)return;
  const v=skPrimary();if(!v)return;
  try{if(v.muted!==skAudio.muted)v.muted=skAudio.muted;if('volume'in skAudio&&v.volume!==skAudio.volume)v.volume=skAudio.volume;}catch{}
}
function skAudioGesture(e){
  if(!skOwned || !e.isTrusted)return;
  const path=e.composedPath?.()||[e.target];
  const control=path.find(el=>el?.getAttribute && (/mute|volume|sound|audio/i.test([el.getAttribute('aria-label'),el.getAttribute('title'),el.getAttribute('data-a-target')].join(' ')) || (el.tagName==='VIDEO'&&el.controls)));
  if(!control)return;
  if(e.type==='keydown'&&!['m','M','ArrowUp','ArrowDown','ArrowLeft','ArrowRight',' ','Enter'].includes(e.key))return;
  skAudioGestureUntil=Date.now()+1200;
}
for(const type of ['pointerdown','keydown','input','change'])document.addEventListener(type,skAudioGesture,true);
async function skVolumeChanged(v){
  if(!skOwned || v!==skPrimary() || skAudioPending)return;
  if(Date.now()<skAudioGestureUntil){
    skAudioGestureUntil=0;skAudioPending=true;
    try{
      const reply=await skAPI.runtime.sendMessage({sk:true,op:'audio_intent',revision:skAudioRevision,audio:{muted:v.muted,volume:v.volume}});
      if(reply?.owned&&reply.audio){skAudio=reply.audio;skAudioRevision=reply.audio_revision;}
      else skOwned=false;
    }catch{}finally{skAudioPending=false;skApplyAudio();}
  }else skApplyAudio();
}
function skWatchAudio(){
  for(const v of document.querySelectorAll('video'))if(!skWatched.has(v)){
    skWatched.add(v);v.addEventListener?.('volumechange',()=>{void skVolumeChanged(v);});
    v.addEventListener?.('loadedmetadata',skApplyAudio);v.addEventListener?.('play',skApplyAudio);
  }
  skApplyAudio();
}
// Busy player pages mutate constantly. Coalesce scans instead of scanning the
// entire document for every mutation delivery, including unowned frames.
if(typeof MutationObserver!=='undefined')new MutationObserver(()=>{
  if(!skOwned||skWatchPending)return;
  skWatchPending=true;setTimeout(()=>{skWatchPending=false;skWatchAudio();},200);
}).observe(document,{childList:true,subtree:true});
function skRestore(){document.getElementById('streamkeeper-tab-style')?.remove();document.querySelectorAll('[data-sk-tab]').forEach(e=>e.removeAttribute('data-sk-tab'));}
function skExpand(target){
  if(!target || skEscaped)return;
  if(target.dataset.skTab==='player')return;
  skRestore();
  const style=document.createElement('style');style.id='streamkeeper-tab-style';
  style.textContent=`html,body{overflow:hidden!important}body{visibility:hidden!important}[data-sk-tab="ancestor"]{transform:none!important;filter:none!important;perspective:none!important;contain:none!important;overflow:visible!important;position:static!important;opacity:1!important}[data-sk-tab="player"]{position:fixed!important;inset:0!important;width:100vw!important;height:100vh!important;max-width:none!important;max-height:none!important;margin:0!important;padding:0!important;border:0!important;z-index:2147483646!important;visibility:visible!important;background:#000!important;object-fit:contain!important}[data-sk-tab="player"] *{visibility:visible!important}[data-sk-tab="player"] video{width:100%!important;height:100%!important;object-fit:contain!important}`;
  (document.head||document.documentElement).append(style);
  for(let e=target.parentElement;e;e=e.parentElement)e.dataset.skTab='ancestor';target.dataset.skTab='player';

}
document.addEventListener('keydown',e=>{if(e.key==='Escape'){skEscaped=true;skShowPointer();skRestore();if(skOwned)void skAPI.runtime.sendMessage({sk:true,op:'escape'}).catch(()=>{});}},true);
skAPI.runtime.onMessage.addListener((m,_s,respond)=>{
  if(!m?.sk)return false;
  if(m.op==='audio'){
    skAudioGestureUntil=0;
    if(!skOwned || typeof m.muted!=='boolean' || ('volume'in m && (typeof m.volume!=='number' || !Number.isFinite(m.volume) || m.volume<0 || m.volume>1))){respond({confirmed:false});return false;}
    // Volume targets only the fresh primary frame selected by the controller.
    // Validate a decoded video before changing even mute state.
    const primary=m.primary_only===true ? [...document.querySelectorAll('video')].filter(v=>v.videoWidth>0&&v.videoHeight>0).sort((a,b)=>b.videoWidth*b.videoHeight-a.videoWidth*a.videoHeight)[0] : null;
    if(m.primary_only===true&&!primary){respond({confirmed:false});return false;}
    const media=m.primary_only===true?[primary]:[...document.querySelectorAll('video,audio')];
    try{
      skAudioRevision=m.audio_revision??skAudioRevision;
      skAudio={...skAudio,muted:m.muted,...('volume'in m?{volume:m.volume}:{})};
      for(const v of media){v.muted=m.muted;if('volume'in m)v.volume=m.volume;}
      const confirmed=media.every(v=>v.muted===m.muted && (!('volume'in m)||v.volume===m.volume));
      respond({confirmed,muted:m.muted,...('volume'in m?{volume:m.volume}:{}),media_count:media.length,video_count:m.primary_only===true?1:document.querySelectorAll('video').length});
    }catch{respond({confirmed:false});}
    return false;
  }
  if(m.op==='seek'){
    // Seek only the decoded primary video in this frame; live streams have no finite duration.
    const v=[...document.querySelectorAll('video')].filter(v=>v.videoWidth>0&&Number.isFinite(v.duration)&&v.duration>0).sort((a,b)=>b.videoWidth*b.videoHeight-a.videoWidth*a.videoHeight)[0];
    if(!skOwned||!v||(typeof m.to!=='number'&&typeof m.delta!=='number')){respond({confirmed:false});return false;}
    const target=typeof m.to==='number'?m.to:v.currentTime+m.delta;
    try{v.currentTime=Math.max(0,Math.min(v.duration-1,target));respond({confirmed:true,current_time:v.currentTime,duration:v.duration,width:v.videoWidth,height:v.videoHeight});}
    catch{respond({confirmed:false});}
    return false;
  }
  if(m.op==='fill_child' && skOwned && skFill && !skEscaped){const f=[...document.querySelectorAll('iframe')].find(f=>f.src===m.src || f.src.split('#')[0]===m.src.split('#')[0]);if(f)skExpand(f);}
  if(m.op==='marker' && window===window.top)document.title=m.value;
  if(m.op==='fill'){skFill=m.enabled;skEscaped=false;if(!skFill)skRestore();}
  if(m.op==='park'){skParked=m.enabled;for(const v of document.querySelectorAll('video')){if(m.enabled){v.dataset.skResume=String(!v.paused);v.pause();}else if(v.dataset.skResume==='true'){delete v.dataset.skResume;void v.play().catch(()=>{});}}}
  if(m.op==='play')for(const v of document.querySelectorAll('video'))void v.play().catch(()=>{});
  if(m.op==='unmute'){skAudioRevision=m.audio_revision??skAudioRevision;skAudio={muted:false,volume:1};skAudioGestureUntil=0;for(const v of document.querySelectorAll('video')){v.muted=false;v.volume=1;}}
  respond(true);return false;
});
async function skTick(){
  if(skTickPending)return;
  skTickPending=true;
  try {
    const videos=[...document.querySelectorAll('video')];const v=videos.sort((a,b)=>b.videoWidth*b.videoHeight-a.videoWidth*a.videoHeight)[0];
    const quality=v?.getVideoPlaybackQuality?.();
    const frames=quality?.totalVideoFrames ?? v?.webkitDecodedFrameCount ?? null;
    let bufferAhead=null,liveDistance=null;
    if(v){
      for(let i=0;i<v.buffered.length;i++)if(v.buffered.start(i)<=v.currentTime&&v.currentTime<=v.buffered.end(i)){bufferAhead=Math.max(0,v.buffered.end(i)-v.currentTime);break;}
      // This is distance to the player's seekable edge, not broadcast latency.
      if(!Number.isFinite(v.duration)&&v.seekable.length)liveDistance=Math.max(0,v.seekable.end(v.seekable.length-1)-v.currentTime);
    }
    if(v && skLastTime!==null && v.currentTime>skLastTime && (frames===null || skLastFrames===null || frames>skLastFrames)){skLastProgress=Date.now();skEverProgress=true;}
    const health=v?{state:v.error?'ERROR':v.paused?'PAUSED':skEverProgress&&v.readyState>=2&&Date.now()-skLastProgress<15000?'PLAYING':'LOADING',current_time:v.currentTime,decoded_frames:frames,ready_state:v.readyState,width:v.videoWidth,height:v.videoHeight,duration:Number.isFinite(v.duration)&&v.duration>0?v.duration:null,ended:v.ended&&skEverProgress&&(frames??0)>0,paused:v.paused,muted:v.muted,volume:v.volume,stall_seconds:(Date.now()-skLastProgress)/1000}: {state:'NO_VIDEO'};
    if(v)Object.assign(health,{dropped_frames:quality?.droppedVideoFrames??v.webkitDroppedFrameCount??null,buffer_ahead_seconds:bufferAhead,live_edge_distance_seconds:liveDistance,playback_rate:v.playbackRate});
    skLastTime=v?.currentTime??null;skLastFrames=frames;
    const reply=await skAPI.runtime.sendMessage({sk:true,op:'health',health});
    skOwned=!!reply?.owned;skFill=!!reply?.fill;
    skUpdatePointer(reply,v);
    if(reply?.audio&&!skAudioPending&&reply.audio_revision>=skAudioRevision){skAudio=reply.audio;skAudioRevision=reply.audio_revision;}
    skWatchAudio();
    if(skOwned&&reply?.autoplay&&v&&!skStarted&&!reply.parked){skStarted=true;if(!skAudio&&reply.start_muted)v.muted=true;void v.play().catch(()=>{});}
    if(skOwned&&reply?.parked&&v&&!v.paused){if(!skParked)v.dataset.skResume='true';v.pause();skParked=true;}
    if(skOwned&&skFill&&!skEscaped&&v)skExpand(v.closest('.jwplayer,.video-js,.plyr,[data-player]')||v);
    if(!skOwned)skRestore();
  }catch{skShowPointer();}finally{skTickPending=false;}
}
setInterval(skTick,3000);void skTick();
})();
