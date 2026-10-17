/* Finite native commands; personal tabs are never auto-adopted. */
const api = globalThis.browser || chrome;
const isFirefox = !!api.runtime.getManifest().browser_specific_settings?.gecko;
const HOST = 'com.bhackerb.streamkeeper';
const HOSTS = ['streamed.pk','streamed.su','embed.st','rockylive.st','strmd.top'];
function allowed(url) { try { const u = new URL(url); return u.protocol === 'https:' && !u.username && !u.password && (!u.port || u.port==='443') && HOSTS.some(h => u.hostname === h || u.hostname.endsWith('.'+h)); } catch { return false; } }
let port, connecting=false;
const ready = api.storage.local.get('owned').then(v => Object.assign(Object.create(null),v.owned || {}));
let records, lost;
let audioPreferences;
const audioReady=api.storage.local.get('audioPreferences').then(v=>v.audioPreferences||{});
async function preferences(){return audioPreferences ||= await audioReady;}
function validAudio(a){return a && typeof a.muted==='boolean' && (!('volume'in a)||(typeof a.volume==='number'&&Number.isFinite(a.volume)&&a.volume>=0&&a.volume<=1));}
async function rememberAudio(id,r,a){
  r.audio={...r.audio,...a};r.audioRevision=(r.audioRevision||0)+1;
  const prefs=await preferences();delete prefs[id];prefs[id]={session:r.session_id,audio:r.audio};
  for(const key of Object.keys(prefs).slice(0,-200))delete prefs[key];
  await api.storage.local.set({audioPreferences:prefs});await save();
}

const createdHere=new Set();
let sessionOwners;
const ownersReady=api.storage.session.get('ownerTokens').then(v=>v.ownerTokens||{});
async function ownerTokens(){return sessionOwners ||= await ownersReady;}
async function saveOwners(){await api.storage.session.set({ownerTokens:await ownerTokens()});}
async function provesTab(tab,r){
 if(hasMarker(tab,r))return true;
 if(!allowed(tab.url||tab.pendingUrl||''))return false;
 if((await ownerTokens())[tab.id]===r.token && tab.windowId===r.windowId)return true;
 if(isFirefox)return (await api.sessions.getTabValue(tab.id,'streamkeeperOwner').catch(()=>null))===r.token;
 return false;
}
const lostReady=api.storage.local.get('lost').then(v=>Object.assign(Object.create(null),v.lost||{}));
async function lostRecords(){return lost ||= await lostReady;}
async function owned() { return records ||= await ready; }
async function save() { await api.storage.local.set({owned:await owned(),lost:await lostRecords()}); }
async function send(value) { try { port?.postMessage(value); } catch {} }
function markedURL(r) { const u=new URL(r.url);u.hash=(u.hash ? u.hash.slice(1)+'&' : '')+'sk-owned='+r.token;return u.href; }
function hasMarker(tab,r) { return allowed(tab.url || tab.pendingUrl || '') && (tab.url || tab.pendingUrl || '').includes('sk-owned='+r.token); }
async function recover(r) {
  const tab=typeof r.tabId==='number' ? await api.tabs.get(r.tabId).catch(()=>null) : null;
  if(tab && await provesTab(tab,r))return tab;
  if(tab && createdHere.has(r.tabId) && (tab.url==='about:blank' || !tab.url) && Date.now()-r.createdAt<90000)return tab;
  // Search only this saved stream URL, and claim only its unpredictable ownership marker.
  const candidates=await api.tabs.query({url:r.url.split('#')[0]+'*'});
  const matches=[];for(const candidate of candidates)if(await provesTab(candidate,r))matches.push(candidate);
  return matches.length===1 ? matches[0] : null;
}
async function inventory() {
  const all=await owned(), missing=await lostRecords(), errors=[];
  for(const [id,r]of Object.entries(missing)) {
    try{const tab=await recover(r);if(tab){r.tabId=tab.id;r.windowId=tab.windowId;all[id]=r;delete missing[id];}}catch{errors.push(id+': ownership lookup failed');}
  }
  for(const [id,r]of Object.entries(all)) {
    try{
      const tab=await recover(r);
      if(tab){r.tabId=tab.id;r.windowId=tab.windowId;}
      else{delete all[id];missing[id]=r;await send({event:'ownership_lost',stream_id:id});}
    }catch{errors.push(id+': ownership lookup failed');}
  }
  await save();await send({event:'inventory',records:all,lost:Object.keys(missing),errors});return all;
}
function scheduleInventory(){queue=queue.catch(()=>{}).then(inventory).catch(()=>{});return queue;}
async function frameCommand(tabId, value) {
  // tabs.sendMessage without frameId sends to all permitted content-script frames.
  return api.tabs.sendMessage(tabId, {sk:true,...value});
}
async function audioCommand(r,c) {
  if(typeof c.muted!=='boolean' || ('volume'in c && (typeof c.volume!=='number' || !Number.isFinite(c.volume) || c.volume<0 || c.volume>1)))throw Error('Invalid audio settings');
  const frames=await api.webNavigation.getAllFrames({tabId:r.tabId});
  if(!frames?.length || frames.length>50)throw Error('Player frames unavailable');
  // Address each exact permitted frame. A broadcast can return only one arbitrary
  // frame response and cannot prove that the player accepted the settings.
  const volume='volume'in c;
  if(volume && (!Number.isInteger(c.primary_frame_id)||c.primary_frame_id<0))throw Error('Primary video frame required; update the StreamKeeper controller for volume control');
  const targets=volume?frames.filter(f=>f.frameId===c.primary_frame_id&&allowed(f.url)):frames.filter(f=>allowed(f.url));
  if(!targets.length)throw Error(volume?'Primary video frame unavailable or unsupported; refresh player status':'No supported player frame');
  if(!await provesTab(await api.tabs.get(r.tabId),r))throw Error('Stream ownership changed before audio update');
  const replies=await Promise.all(targets.map(f=>api.tabs.sendMessage(r.tabId,{sk:true,op:'audio',audio_revision:(r.audioRevision||0)+1,muted:c.muted,...(volume?{volume:c.volume,primary_only:true}:{})},{frameId:f.frameId})));
  let count=0,videos=0;
  for(const result of replies){
    if(!result || result.confirmed!==true || !Number.isInteger(result.media_count) || result.media_count<0 || result.muted!==c.muted || ('volume'in c && result.volume!==c.volume))throw Error('Player audio confirmation unavailable');
    if(!Number.isInteger(result.video_count) || result.video_count<0 || result.video_count>result.media_count)throw Error('Player video confirmation unavailable; audio may be partially changed');
    count+=result.media_count;videos+=result.video_count;
  }
  if(count<1 || videos<1)throw Error('No player video confirmed audio settings; audio may be partially changed');
  const current=await api.tabs.get(r.tabId);
  if(!await provesTab(current,r))throw Error('Stream ownership changed during audio update');
  // Browser tab muting also covers inaccessible or unsupported nested frames.
  await api.tabs.update(r.tabId,{muted:c.muted});
  const verified=await api.tabs.get(r.tabId);
  if(!await provesTab(verified,r) || verified.mutedInfo?.muted!==c.muted)throw Error('Tab audio confirmation unavailable');
  return {confirmed:true,muted:c.muted,...(volume?{volume:c.volume,primary_frame_id:c.primary_frame_id}:{}),media_count:count,video_count:videos};
}
async function command(c) {
  const all = await owned(); const r=all[c.stream_id];
  if (c.op==='inventory') return inventory();
  if (c.op==='open') {
    if (typeof c.stream_id!=='string' || !/^[a-zA-Z0-9_-]{1,180}$/.test(c.stream_id) || !allowed(c.url)) throw Error('Unsupported stream');
    if(r){const tab=await recover(r);if(tab){r.tabId=tab.id;r.windowId=tab.windowId;await save();return r;}}
    const missing=await lostRecords();
    if(missing[c.stream_id] && !c.allow_replacement)throw Error('Ownership lost; explicit session restart required');
    delete missing[c.stream_id];
    const record={url:c.url,token:crypto.randomUUID(),createdAt:Date.now(),created:true,session_id:c.session_id||null,startMuted:c.start_muted!==false,label:String(c.label||'Stream').slice(0,200),fill:true};
    const saved=(await preferences())[c.stream_id];
    record.audio=saved?.session===record.session_id&&validAudio(saved.audio)?saved.audio:{muted:record.startMuted};record.audioRevision=0;
    all[c.stream_id]=record;await save();
    const w=await api.windows.create({url:markedURL(record),type:'popup',focused:false});
    const tab=w.tabs?.[0] || (await api.tabs.query({windowId:w.id}))[0];
    if(!tab)throw Error('Browser did not return a tab');
    record.tabId=tab.id;record.windowId=w.id;createdHere.add(tab.id);
    (await ownerTokens())[tab.id]=record.token;await saveOwners();
    if(isFirefox)await api.sessions.setTabValue(tab.id,'streamkeeperOwner',record.token);
    await api.tabs.update(tab.id,{muted:record.audio.muted});
    await save(); return all[c.stream_id];
  }
  if (!r) throw Error('Stream is not owned');
  const tab=await recover(r);if(!tab || !(await provesTab(tab,r)))throw Error('Owned stream is unavailable');r.tabId=tab.id;r.windowId=tab.windowId;
  if(c.op==='close') {if(r.created)await api.tabs.remove(r.tabId);delete all[c.stream_id];delete (await ownerTokens())[r.tabId];await saveOwners();await save();return true;}
  if(c.op==='detach') {delete all[c.stream_id]; await save(); return true;}
  if(c.op==='park'){await api.windows.update(r.windowId,{state:c.enabled?'minimized':'normal'});r.parked=!!c.enabled;await save();await frameCommand(r.tabId,{op:'park',enabled:r.parked}).catch(()=>{});return true;}
  if(!(await provesTab(tab,r)))throw Error('Owned stream is still loading');
  if(c.op==='reload') {await api.tabs.reload(r.tabId); return true;}
  if(c.op==='title') return tab.title || '';
  if(c.op==='marker') {if(typeof c.value!=='string' || c.value.length>300) throw Error('Invalid title'); return api.tabs.sendMessage(r.tabId,{sk:true,op:'marker',value:c.value},{frameId:0});}
  if(c.op==='fill') {r.fill=!!c.enabled;await save(); await frameCommand(r.tabId,{op:'fill',enabled:r.fill}).catch(()=>{});return true;}
  if(c.op==='audio'){const result=await audioCommand(r,c);await rememberAudio(c.stream_id,r,{muted:c.muted,...('volume'in c?{volume:c.volume}:{})});return result;}
  if(c.op==='unmute'){
    // Preserve the legacy permissive request; it does not promise audio confirmation.
    const result=await frameCommand(r.tabId,{op:'unmute',audio_revision:(r.audioRevision||0)+1});
    if(!await provesTab(await api.tabs.get(r.tabId),r))throw Error('Owned stream is unavailable');
    await api.tabs.update(r.tabId,{muted:false});await rememberAudio(c.stream_id,r,{muted:false,volume:1});return result;
  }
  if(c.op==='play') return frameCommand(r.tabId,{op:c.op});
  throw Error('Unsupported native command');
}
let queue=Promise.resolve();
function connect() {
  if(port || connecting) return; connecting=true;
  try {
    port=api.runtime.connectNative(HOST);
    port.onMessage.addListener(c=> {
      if(!c || typeof c.id!=='string') return;
      queue=queue.catch(()=>{}).then(async()=>{try {await send({id:c.id,result:await command(c)});} catch(e){await send({id:c.id,error:String(e.message||e)});}});
    });
    port.onDisconnect.addListener(()=>{console.warn('StreamKeeper native bridge:',api.runtime.lastError?.message || 'disconnected');port=null;connecting=false;setTimeout(connect,5000);});
    port.postMessage({event:'hello',browser:isFirefox?'firefox':'chromium',version:api.runtime.getManifest().version,capabilities:['audio-v1','primary-volume-v1']});
    void scheduleInventory();
  } catch { port=null;setTimeout(connect,5000); }
  connecting=false;
}
api.runtime.onMessage.addListener((m,sender,respond)=>{
  if(!m?.sk || !sender.tab || !allowed(sender.url || '')) return false;
  queue=queue.catch(()=>{}).then(async()=>{
    const all=await owned();const entry=Object.entries(all).find(([,r])=>r.tabId===sender.tab.id);
    if(!entry) return {owned:false};
    const [id,r]=entry;
    if(!await provesTab(sender.tab,r))return {owned:false};
    const frames=await api.webNavigation.getAllFrames({tabId:r.tabId});
    if(!frames?.some(f=>f.frameId===sender.frameId && f.url===sender.url))return {owned:false};
    if(m.op==='audio_intent' && validAudio(m.audio) && m.revision===(r.audioRevision||0)){await rememberAudio(id,r,m.audio);await api.tabs.update(r.tabId,{muted:m.audio.muted});}
    if(m.op==='escape'){r.fill=false;await save();await frameCommand(r.tabId,{op:'fill',enabled:false}).catch(()=>{});}
    if(m.op==='health' && r.fill && m.health?.width>0){
      const frames=await api.webNavigation.getAllFrames({tabId:r.tabId});
      let child=frames.find(f=>f.frameId===sender.frameId);
      while(child && child.parentFrameId>=0){
        await api.tabs.sendMessage(r.tabId,{sk:true,op:'fill_child',src:child.url},{frameId:child.parentFrameId}).catch(()=>{});
        child=frames.find(f=>f.frameId===child.parentFrameId);
      }
    }
    if(m.op==='health') await send({event:'health',stream_id:id,frame_id:sender.frameId,health:m.health});
    return {owned:true,fill:r.fill,parked:!!r.parked,autoplay:r.created,start_muted:r.startMuted,audio:r.audio||{muted:r.startMuted!==false},audio_revision:r.audioRevision||0};
  });queue.then(respond).catch(()=>respond({owned:false}));return true;
});
api.tabs.onRemoved.addListener((tabId,info)=>{
  queue=queue.catch(()=>{}).then(async()=>{
    const all=await owned(),missing=await lostRecords();
    for(const [id,r]of Object.entries(all))if(r.tabId===tabId){
      delete all[id];
      if(info?.isWindowClosing){missing[id]=r;await send({event:'ownership_lost',stream_id:id});}
      else await send({event:'closed',stream_id:id});
    }
    delete (await ownerTokens())[tabId];await saveOwners();await save();
  });
});
api.tabs.onUpdated.addListener((tabId,change)=>{
  // Only browser UI mute actions change intent; our own tabs.update is ignored.
  if(change.mutedInfo?.reason==='user' || (change.mutedInfo?.muted===false&&!change.mutedInfo.reason))queue=queue.catch(()=>{}).then(async()=>{
    const entry=Object.entries(await owned()).find(([,r])=>r.tabId===tabId);
    if(entry && entry[1].audio?.muted!==change.mutedInfo.muted && await provesTab(await api.tabs.get(tabId),entry[1]))await rememberAudio(entry[0],entry[1],{muted:change.mutedInfo.muted});
  });
  if(change.status==='complete')void owned().then(all=>{if(Object.values(all).some(r=>r.tabId===tabId))return scheduleInventory();});
});
api.alarms.create('bridge-reconnect',{periodInMinutes:1});api.alarms.onAlarm.addListener(()=>{connect();void scheduleInventory();});
connect();
