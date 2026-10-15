/* Passive health evidence in every permitted frame. Never remove iframe sandbox. */
const skAPI=globalThis.browser || chrome;
let skOwned=false, skFill=false, skEscaped=false, skLastTime=null, skLastFrames=null, skLastProgress=Date.now(), skEverProgress=false, skStarted=false, skParked=false;
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
document.addEventListener('keydown',e=>{if(e.key==='Escape'){skEscaped=true;skRestore();if(skOwned)void skAPI.runtime.sendMessage({sk:true,op:'escape'}).catch(()=>{});}},true);
skAPI.runtime.onMessage.addListener((m,_s,respond)=>{
  if(!m?.sk)return false;
  if(m.op==='audio'){
    if(!skOwned || typeof m.muted!=='boolean' || ('volume'in m && (typeof m.volume!=='number' || !Number.isFinite(m.volume) || m.volume<0 || m.volume>1))){respond({confirmed:false});return false;}
    const media=[...document.querySelectorAll('video,audio')];
    try{
      for(const v of media){v.muted=m.muted;if('volume'in m)v.volume=m.volume;}
      const confirmed=media.every(v=>v.muted===m.muted && (!('volume'in m)||v.volume===m.volume));
      respond({confirmed,muted:m.muted,...('volume'in m?{volume:m.volume}:{}),media_count:media.length,video_count:document.querySelectorAll('video').length});
    }catch{respond({confirmed:false});}
    return false;
  }
  if(m.op==='fill_child' && skOwned && skFill && !skEscaped){const f=[...document.querySelectorAll('iframe')].find(f=>f.src===m.src || f.src.split('#')[0]===m.src.split('#')[0]);if(f)skExpand(f);}
  if(m.op==='marker' && window===window.top)document.title=m.value;
  if(m.op==='fill'){skFill=m.enabled;skEscaped=false;if(!skFill)skRestore();}
  if(m.op==='park'){skParked=m.enabled;for(const v of document.querySelectorAll('video')){if(m.enabled){v.dataset.skResume=String(!v.paused);v.pause();}else if(v.dataset.skResume==='true'){delete v.dataset.skResume;void v.play().catch(()=>{});}}}
  if(m.op==='play')for(const v of document.querySelectorAll('video'))void v.play().catch(()=>{});
  if(m.op==='unmute')for(const v of document.querySelectorAll('video')){v.muted=false;v.volume=1;}
  respond(true);return false;
});
async function skTick(){
  try {
    const videos=[...document.querySelectorAll('video')];const v=videos.sort((a,b)=>b.videoWidth*b.videoHeight-a.videoWidth*a.videoHeight)[0];
    const frames=v?.getVideoPlaybackQuality?.().totalVideoFrames ?? v?.webkitDecodedFrameCount ?? null;
    if(v && skLastTime!==null && v.currentTime>skLastTime && (frames===null || skLastFrames===null || frames>skLastFrames)){skLastProgress=Date.now();skEverProgress=true;}
    const health=v?{state:v.error?'ERROR':v.paused?'PAUSED':skEverProgress&&v.readyState>=2&&Date.now()-skLastProgress<15000?'PLAYING':'LOADING',current_time:v.currentTime,decoded_frames:frames,ready_state:v.readyState,width:v.videoWidth,height:v.videoHeight,ended:v.ended&&skEverProgress&&(frames??0)>0,paused:v.paused,muted:v.muted,volume:v.volume,stall_seconds:(Date.now()-skLastProgress)/1000}: {state:'NO_VIDEO'};
    skLastTime=v?.currentTime??null;skLastFrames=frames;
    const reply=await skAPI.runtime.sendMessage({sk:true,op:'health',health});
    skOwned=!!reply?.owned;skFill=!!reply?.fill;
    if(skOwned&&reply?.autoplay&&v&&!skStarted&&!reply.parked){skStarted=true;if(reply.start_muted)v.muted=true;void v.play().catch(()=>{});}
    if(skOwned&&reply?.parked&&v&&!v.paused){if(!skParked)v.dataset.skResume='true';v.pause();skParked=true;}
    if(skOwned&&skFill&&!skEscaped&&v)skExpand(v.closest('.jwplayer,.video-js,.plyr,[data-player]')||v);
    if(!skOwned)skRestore();
  }catch{}
}
setInterval(skTick,3000);void skTick();
