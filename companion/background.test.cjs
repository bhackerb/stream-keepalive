const test=require('node:test');const assert=require('node:assert/strict');const fs=require('node:fs');const vm=require('node:vm');
function event(){const callbacks=[];return {addListener:f=>callbacks.push(f),fire:(...a)=>callbacks.forEach(f=>f(...a))};}
async function harness(local={},session={},engine='firefox'){
 const sent=[],tabs=new Map([[1,{id:1,url:'https://personal.example/',title:'Personal',windowId:1}]]),removed=[];let next=10;
 const port={postMessage:m=>sent.push(m),onMessage:event(),onDisconnect:event()};
 const store=data=>({get:async k=>({[k]:data[k]}),set:async v=>Object.assign(data,v)});
 const tabValues={};
 const api={webNavigation:{onCompleted:event(),getAllFrames:async()=>[]},scripting:{executeScript:async()=>[]},sessions:{getTabValue:async id=>tabValues[id],setTabValue:async(id,_key,value)=>{tabValues[id]=value;}},storage:{local:store(local),session:store(session)},runtime:{connectNative:()=>port,getManifest:()=>({version:'fixture',...(engine==='firefox'?{browser_specific_settings:{gecko:{id:'fixture'}}}:{})}),onMessage:event()},
  tabs:{update:async(id,value)=>{tabs.get(id).mutedInfo={muted:value.muted,reason:'extension'};},get:async id=>{if(!tabs.has(id))throw Error('missing');return tabs.get(id);},query:async q=>{if(!q?.url)throw Error('Personal tab scan forbidden');return [...tabs.values()].filter(t=>t.url.startsWith(q.url.slice(0,-1)));},remove:async id=>{removed.push(id);tabs.delete(id);},sendMessage:async()=>true,reload:async()=>{throw Error('Unexpected reload');},onRemoved:event(),onUpdated:event()},
  windows:{update:async()=>{},create:async v=>{const id=next++;const tab={id,url:v.url,windowId:id};tabs.set(id,tab);return {id,tabs:[tab]};}},alarms:{create:()=>{},onAlarm:event()}};
 vm.runInNewContext(fs.readFileSync(__dirname+'/background.js','utf8'),{browser:api,console,setTimeout:()=>{},URL,Date,Promise,crypto:require('node:crypto').webcrypto});
 const settle=async()=>{for(let i=0;i<12;i++)await new Promise(r=>setImmediate(r));};await settle();
 async function command(op,args={}){const id='request-'+sent.length;port.onMessage.fire({id,op,...args});await settle();return sent.find(m=>m.id===id);}
 async function message(m,sender){let reply;api.runtime.onMessage.fire({sk:true,...m},sender,r=>{reply=r;});await settle();return reply;}
 return {sent,tabs,removed,command,api,local,port,settle,message};
}
test('open is idempotent, unknown eval denied, only owned tab closes',async()=>{
 const h=await harness();await h.command('open',{stream_id:'game',url:'https://streamed.pk/watch/game'});await h.command('open',{stream_id:'game',url:'https://streamed.pk/watch/game'});
 assert.equal(h.tabs.size,2);assert.match((await h.command('eval',{stream_id:'game',code:'document.cookie'})).error,/Unsupported/);
 await h.command('close',{stream_id:'game'});assert.equal(h.tabs.size,1);assert.equal(h.tabs.get(1).title,'Personal');assert.deepEqual(h.removed,[10]);
});
test('unsupported URLs and unowned tabs never controlled',async()=>{
 const h=await harness();for(const url of ['javascript:alert(1)','https://streamed.pk.evil.example/game','http://streamed.pk/watch/game'])assert.ok((await h.command('open',{stream_id:'game',url})).error);
 assert.ok((await h.command('close',{stream_id:'personal'})).error);assert.equal(h.tabs.size,1);assert.equal(h.removed.length,0);
});

test('supplied URL creates one origin-scoped owned player and injects top/nested frames after mute',async()=>{
 const h=await harness();const injections=[];
 const url='https://olympicweb.me/game/2/#/watch';
 h.api.webNavigation.getAllFrames=async({tabId})=>[{frameId:0,documentId:'root-doc',parentFrameId:-1,url:h.tabs.get(tabId).url},{frameId:2,documentId:'child-doc',parentFrameId:0,url:'https://dervlin.me/embed/game'},{frameId:3,parentFrameId:0,url:'http://unsupported.example/video'}];
 h.api.scripting.executeScript=async args=>{assert.equal(h.tabs.get(args.target.tabId).mutedInfo.muted,true);injections.push(args);return [];};
 assert.ok((await h.command('open',{stream_id:'game',url})).error);
 const opened=await h.command('open',{stream_id:'game',url,user_source:true});assert.ok(opened.result);
 assert.equal(h.tabs.get(10).url,url);assert.equal(h.local.owned.game.userSource,true);
 assert.deepEqual(injections.map(x=>x.target.documentIds[0]),['root-doc','child-doc']);
 assert.ok(h.sent.find(x=>x.event==='hello').capabilities.includes('user-source-v1'));
 const reply=await h.message({op:'health',health:{state:'PLAYING'}},{tab:h.tabs.get(10),frameId:2,url:'https://dervlin.me/embed/game'});
 assert.equal(reply.owned,true);
 await h.command('open',{stream_id:'game',url,user_source:true});assert.equal(h.tabs.size,2);
 assert.match((await h.command('open',{stream_id:'game',url:'https://different.example.com/game',user_source:true})).error,/different owned source/);assert.equal(h.tabs.size,2);
 await h.command('close',{stream_id:'game'});assert.deepEqual(h.removed,[10]);assert.equal(h.tabs.get(1).title,'Personal');
});
test('generic authorization never becomes a host-wide grant or adopts a copied marker',async()=>{
 const h=await harness();const url='https://falconstreams.st/game/123';
 await h.command('open',{stream_id:'game',url,user_source:true});const r=h.local.owned.game;
 h.tabs.get(1).url=url+'#sk-owned='+r.token;
 assert.equal((await h.message({op:'health',health:{}},{tab:h.tabs.get(1),frameId:0,url:h.tabs.get(1).url})).owned,false);
 h.tabs.delete(10);const inventory=await h.command('inventory');assert.equal(inventory.result.game,undefined);
 assert.ok((await h.command('close',{stream_id:'game'})).error);assert.equal(h.removed.length,0);assert.equal(h.tabs.get(1).title,'Personal');
 assert.ok((await h.command('open',{stream_id:'other',url:'https://falconstreams.st/another'})).error);
});
test('generic streams reject cross-origin redirects and recover Firefox tab tokens without exposing a marker',async()=>{
 const h=await harness();const url='https://video.example.com/#/game';
 await h.command('open',{stream_id:'game',url,user_source:true});const r=h.local.owned.game,old=h.tabs.get(10);
 h.tabs.delete(10);h.tabs.set(30,{...old,id:30,windowId:30});await h.api.sessions.setTabValue(30,'streamkeeperOwner',r.token);
 const inv=await h.command('inventory');assert.equal(inv.result.game.tabId,30);
 h.tabs.get(30).url='https://offsite.example.com/redirect';
 assert.ok((await h.command('reload',{stream_id:'game'})).error);assert.equal(h.removed.length,0);
});
test('navigation injects only frames in a proven owned URL window; personal tabs and redirects fail closed',async()=>{
 const h=await harness();await h.command('open',{stream_id:'game',url:'https://falconstreams.st/game',user_source:true});const calls=[];
 h.api.scripting.executeScript=async args=>{calls.push(args);};
 h.api.webNavigation.onCompleted.fire({tabId:10,frameId:0,documentId:'root-doc',url:'https://falconstreams.st/game'});await h.settle();
 assert.equal(calls.length,1);
 h.api.webNavigation.onCompleted.fire({tabId:1,frameId:0,url:'https://personal.example/'});
 h.tabs.get(10).url='https://offsite.example.com/';
 h.api.webNavigation.onCompleted.fire({tabId:10,frameId:2,url:'https://dervlin.me/embed/game'});await h.settle();assert.equal(calls.length,1);
});
test('supplied URL permission rejects unsafe addresses and malformed flags before creating tabs',async()=>{
 const h=await harness();
 for(const url of ['http://example.com/','https://a:b@example.com/','https://@example.com/','https://a:@example.com/','https://127.0.0.1/','https://0x7f.0.0.1/','https://[::1]/','https://example.local/','https://example.com:8890/','https://example.com\\@other.com/','https://exam\nple.com/'])assert.ok((await h.command('open',{stream_id:'game',url,user_source:true})).error);
 assert.ok((await h.command('open',{stream_id:'game',url:'https://example.com/',user_source:'true'})).error);
 assert.equal(h.tabs.size,1);assert.equal(h.removed.length,0);
});
test('browser restart invalidates old numeric ownership before IDs can be reused',async()=>{
 const h=await harness({owned:{stale:{tabId:1,url:'https://streamed.pk/watch/stale',created:true}}},{});
 assert.equal(Object.keys(h.local.owned).length,0);assert.ok((await h.command('close',{stream_id:'stale'})).error);assert.equal(h.tabs.get(1).title,'Personal');
});
test('daemon reconnection inventories existing owned tabs without creating anything',async()=>{
 const h=await harness();await h.command('open',{stream_id:'game',url:'https://embed.st/embed/admin/game/1'});const before=h.tabs.size;
 const result=await h.command('inventory');assert.ok(result.result.game);assert.equal(h.tabs.size,before);assert.equal(h.sent.filter(m=>m.event==='hello').length,1);
});

test('restored marker recovers owned tab after numeric IDs change',async()=>{
 const h=await harness();await h.command('open',{stream_id:'game',url:'https://streamed.pk/watch/game'});
 const record=h.local.owned.game;const tab=h.tabs.get(10);h.tabs.delete(10);h.tabs.set(30,{...tab,id:30,windowId:30});record.tabId=99;
 const result=await h.command('inventory');assert.equal(result.result.game.tabId,30);assert.equal(h.tabs.get(1).title,'Personal');
});
test('loading blank owned window is not mistaken for a user close',async()=>{
 const h=await harness();await h.command('open',{stream_id:'game',url:'https://streamed.pk/watch/game'});h.tabs.get(10).url='about:blank';
 const result=await h.command('inventory');assert.ok(result.result.game);assert.equal(h.sent.filter(m=>m.event==='closed').length,0);
});

test('owned tab completion cannot delete another pending open intent',async()=>{
 const h=await harness();await h.command('open',{stream_id:'first',url:'https://streamed.pk/watch/first'});
 let release;const gate=new Promise(r=>release=r);const create=h.api.windows.create;h.api.windows.create=async value=>{await gate;return create(value);};
 h.port.onMessage.fire({id:'pending',op:'open',stream_id:'second',url:'https://streamed.pk/watch/second'});await h.settle();
 assert.ok(h.local.owned.second);h.api.tabs.onUpdated.fire(10,{status:'complete'});await h.settle();assert.ok(h.local.owned.second);
 release();await h.settle();assert.equal(h.local.owned.second.tabId,11);assert.equal(h.sent.filter(m=>m.event==='ownership_lost').length,0);
});
test('offline ownership loss is durable and cannot reopen automatically',async()=>{
 const h=await harness();await h.command('open',{stream_id:'game',url:'https://streamed.pk/watch/game'});h.tabs.get(10).url='https://offsite.example/';
 const original=h.api.runtime.connectNative;h.api.runtime.connectNative=()=>{throw Error('offline');};h.port.onDisconnect.fire();h.api.alarms.onAlarm.fire();await h.settle();
 assert.ok(h.local.lost.game);h.api.runtime.connectNative=original;h.api.alarms.onAlarm.fire();await h.settle();
 assert.ok(h.sent.some(m=>m.event==='inventory'&&m.lost.includes('game')));
 assert.match((await h.command('open',{stream_id:'game',url:'https://streamed.pk/watch/game'})).error,/explicit session restart/);assert.equal(h.tabs.size,2);
});

test('stripping the URL marker does not lose proven tab-scoped ownership',async()=>{
 const h=await harness();await h.command('open',{stream_id:'game',url:'https://streamed.pk/watch/game'});h.tabs.get(10).url='https://streamed.pk/watch/game';
 const result=await h.command('inventory');assert.ok(result.result.game);await h.command('park',{stream_id:'game',enabled:true});await h.command('close',{stream_id:'game'});assert.deepEqual(h.removed,[10]);
});
test('window closing preserves durable proof for browser restoration',async()=>{
 const h=await harness();await h.command('open',{stream_id:'game',url:'https://streamed.pk/watch/game'});const t=h.tabs.get(10);h.tabs.delete(10);h.api.tabs.onRemoved.fire(10,{isWindowClosing:true});await h.settle();
 assert.ok(h.local.lost.game);h.tabs.set(30,{...t,id:30,windowId:30});const result=await h.command('inventory');assert.equal(result.result.game.tabId,30);
});

test('Chrome browser namespace alias cannot masquerade as Firefox',async()=>{
 const h=await harness({}, {}, 'chromium');assert.equal(h.sent.find(m=>m.event==='hello').browser,'chromium');
});
test('close drain reopen keeps one canonical copy and ignores a delayed old-tab close',async()=>{
 const h=await harness();await h.command('open',{stream_id:'game',url:'https://streamed.pk/watch/game/golf/1'});
 const first=h.local.owned.game.tabId;assert.equal((await h.command('close',{stream_id:'game'})).result,true);
 const inventory=await h.command('inventory');assert.equal(inventory.result.game,undefined);
 await h.command('open',{stream_id:'game',url:'https://streamed.pk/watch/game/golf/2'});
 h.api.tabs.onRemoved.fire(first,{isWindowClosing:false});await h.settle();
 assert.equal(h.tabs.size,2);assert.ok(h.local.owned.game);assert.equal(h.sent.filter(m=>m.event==='closed').length,0);
 assert.match(h.tabs.get(h.local.owned.game.tabId).url,/golf\/2/);assert.equal(h.tabs.get(1).title,'Personal');
});
test('user close before recovery close produces refusal rather than a new tab',async()=>{
 const h=await harness();await h.command('open',{stream_id:'game',url:'https://streamed.pk/watch/game/golf/1'});
 const first=h.local.owned.game.tabId;h.tabs.delete(first);h.api.tabs.onRemoved.fire(first,{isWindowClosing:false});await h.settle();
 assert.ok((await h.command('close',{stream_id:'game'})).error);assert.equal(h.tabs.size,1);assert.ok(h.sent.some(m=>m.event==='closed'&&m.stream_id==='game'));
});

test('audio confirms every exact player frame and tab mute, never personal tabs',async()=>{
 const h=await harness();await h.command('open',{stream_id:'game',url:'https://streamed.pk/watch/game'});
 const calls=[];h.api.webNavigation={getAllFrames:async()=>[{frameId:0,url:'https://streamed.pk/watch/game'},{frameId:2,url:'https://embed.st/embed/game'}]};
 h.api.tabs.sendMessage=async(id,m,options)=>{calls.push({id,m,options});return {confirmed:true,media_count:options.frameId===2?1:0,video_count:options.frameId===2?1:0,muted:m.muted,volume:m.volume};};
 h.api.tabs.update=async(id,value)=>{h.tabs.get(id).mutedInfo={muted:value.muted};};
 const r=await h.command('audio',{stream_id:'game',muted:false});assert.equal(r.result.confirmed,true);assert.equal(r.result.media_count,1);
 assert.deepEqual(calls.map(c=>[c.id,c.options.frameId]),[[10,0],[10,2]]);assert.equal(h.tabs.get(1).mutedInfo,undefined);
});
test('audio rejects legacy arbitrary replies, invalid inputs, no media and lost ownership',async()=>{
 const h=await harness();await h.command('open',{stream_id:'game',url:'https://streamed.pk/watch/game'});
 h.api.webNavigation={getAllFrames:async()=>[{frameId:0,url:'https://streamed.pk/watch/game'}]};
 for(const invalid of [{muted:1},{muted:false,volume:NaN},{muted:true,volume:2}])assert.match((await h.command('audio',{stream_id:'game',...invalid})).error,/Invalid/);
 for(const result of [true,{confirmed:true,media_count:0,muted:true}]){h.api.tabs.sendMessage=async()=>result;assert.ok((await h.command('audio',{stream_id:'game',muted:true})).error);}
 h.tabs.get(10).url='https://personal.example/';assert.ok((await h.command('audio',{stream_id:'game',muted:true})).error);
});
test('health from an allowlisted child of a navigated unowned tab is refused',async()=>{
 const h=await harness();await h.command('open',{stream_id:'game',url:'https://streamed.pk/watch/game'});
 h.tabs.get(10).url='https://personal.example/';
 let reply;h.api.runtime.onMessage.fire({sk:true,op:'health',health:{state:'PLAYING'}},{tab:h.tabs.get(10),url:'https://embed.st/embed/game',frameId:2},r=>{reply=r;});await h.settle();
 assert.equal(reply.owned,false);assert.equal(h.sent.filter(m=>m.event==='health').length,0);
});

test('legacy volume payload without primary target fails before mutating media',async()=>{
 const h=await harness();await h.command('open',{stream_id:'game',url:'https://streamed.pk/watch/game'});
 h.api.webNavigation={getAllFrames:async()=>[{frameId:0,url:'https://streamed.pk/watch/game'},{frameId:2,url:'https://unsupported.example/video'}]};
 let messages=0;h.api.tabs.sendMessage=async()=>{messages++;return true;};
 assert.match((await h.command('audio',{stream_id:'game',muted:false,volume:.5})).error,/Primary video frame required/);assert.equal(messages,0);
});
test('legacy unmute keeps permissive frame behavior and clears owned tab mute',async()=>{
 const h=await harness();await h.command('open',{stream_id:'game',url:'https://streamed.pk/watch/game'});
 h.api.webNavigation={getAllFrames:async()=>{throw Error('Legacy unmute must not enumerate unsupported frames');}};
 const messages=[];h.api.tabs.sendMessage=async(id,m,options)=>{messages.push({id,m,options});return true;};
 h.api.tabs.update=async(id,value)=>{h.tabs.get(id).mutedInfo={muted:value.muted};};
 const result=await h.command('unmute',{stream_id:'game'});assert.equal(result.result,true);
 assert.equal(messages[0].id,10);assert.equal(messages[0].m.op,'unmute');assert.equal(messages[0].options,undefined);
 assert.equal(h.tabs.get(10).mutedInfo.muted,false);assert.equal(h.tabs.get(1).mutedInfo,undefined);
});
test('audio-only frames never satisfy video confirmation and failed tab verification stays failure',async()=>{
 const h=await harness();await h.command('open',{stream_id:'game',url:'https://streamed.pk/watch/game'});
 h.api.webNavigation={getAllFrames:async()=>[{frameId:0,url:'https://streamed.pk/watch/game'}]};
 h.api.tabs.sendMessage=async()=>({confirmed:true,media_count:1,video_count:0,muted:false});
 assert.match((await h.command('audio',{stream_id:'game',muted:false})).error,/No player video/);
 h.api.tabs.sendMessage=async()=>({confirmed:true,media_count:1,video_count:1,muted:false});
 h.api.tabs.update=async()=>{throw Error('Tab changed during update; frame audio may have changed');};
 const response=await h.command('audio',{stream_id:'game',muted:false});assert.ok(response.error);assert.equal(response.result,undefined);
 // Explicit audio settings do not alter the existing startup mute policy.
 assert.equal(h.local.owned.game.startMuted,true);
});

test('primary volume ignores unrelated unsupported embeds but targets only supported exact frame',async()=>{
 const h=await harness();await h.command('open',{stream_id:'game',url:'https://streamed.pk/watch/game'});
 h.api.webNavigation={getAllFrames:async()=>[{frameId:0,url:'https://streamed.pk/watch/game'},{frameId:2,url:'https://embed.st/embed/game'},{frameId:3,url:'https://unsupported.example/ad'}]};
 const calls=[];h.api.tabs.sendMessage=async(id,m,options)=>{calls.push({id,m,options});return {confirmed:true,muted:m.muted,volume:m.volume,media_count:1,video_count:1};};
 h.api.tabs.update=async(id,value)=>{h.tabs.get(id).mutedInfo={muted:value.muted};};
 const r=await h.command('audio',{stream_id:'game',muted:false,volume:.25,primary_frame_id:2});
 assert.equal(r.result.confirmed,true);assert.equal(r.result.primary_frame_id,2);assert.equal(calls.length,1);
 assert.equal(calls[0].id,10);assert.equal(calls[0].options.frameId,2);assert.equal(calls[0].m.primary_only,true);assert.equal(h.tabs.get(1).mutedInfo,undefined);
 assert.ok(h.sent[0].capabilities.includes('primary-volume-v1'));
});
test('missing unsupported invalid primary frame and ownership loss fail before audio mutation',async()=>{
 const h=await harness();await h.command('open',{stream_id:'game',url:'https://streamed.pk/watch/game'});
 h.api.webNavigation={getAllFrames:async()=>[{frameId:0,url:'https://streamed.pk/watch/game'},{frameId:3,url:'http://unsupported.example/video'}]};
 let mutations=0;h.api.tabs.sendMessage=async()=>{mutations++;return true;};h.api.tabs.update=async()=>{mutations++;};
 for(const frame of [undefined,-1,true,99,3])assert.ok((await h.command('audio',{stream_id:'game',muted:false,volume:.25,primary_frame_id:frame})).error);
 h.tabs.get(10).url='https://personal.example/';assert.ok((await h.command('audio',{stream_id:'game',muted:false,volume:.25,primary_frame_id:0})).error);
 assert.equal(mutations,0);
});
test('primary frame without video never changes tab audio or confirms success',async()=>{
 const h=await harness();await h.command('open',{stream_id:'game',url:'https://streamed.pk/watch/game'});
 h.api.webNavigation={getAllFrames:async()=>[{frameId:2,url:'https://embed.st/embed/game'}]};
 h.api.tabs.sendMessage=async()=>({confirmed:false});let tabsChanged=0;h.api.tabs.update=async()=>{tabsChanged++;};
 assert.ok((await h.command('audio',{stream_id:'game',muted:false,volume:.25,primary_frame_id:2})).error);assert.equal(tabsChanged,0);
});

test('manual audio survives reload, recovery reopen and extension restart; a new session uses startup policy',async()=>{
 const h=await harness();await h.command('open',{stream_id:'game',url:'https://streamed.pk/watch/game',session_id:'session-a'});
 h.api.webNavigation={getAllFrames:async()=>[{frameId:0,url:'https://streamed.pk/watch/game'}]};
 h.api.tabs.sendMessage=async(_id,m)=>({confirmed:true,muted:m.muted,volume:m.volume,media_count:1,video_count:1});
 assert.equal((await h.command('audio',{stream_id:'game',muted:false,volume:.27,primary_frame_id:0})).result.confirmed,true);
 h.api.tabs.reload=async()=>{};await h.command('reload',{stream_id:'game'});
 assert.equal(h.local.owned.game.audio.muted,false);assert.equal(h.local.owned.game.audio.volume,.27);
 await h.command('close',{stream_id:'game'});await h.command('open',{stream_id:'game',url:'https://streamed.pk/watch/game/other/1',session_id:'session-a'});
 assert.equal(h.local.owned.game.audio.volume,.27);assert.equal(h.tabs.get(11).mutedInfo.muted,false);
 const restarted=await harness(h.local);await restarted.command('open',{stream_id:'game',url:'https://streamed.pk/watch/game',session_id:'session-a',allow_replacement:true});
 assert.equal(restarted.local.owned.game.audio.muted,false);assert.equal(restarted.local.owned.game.audio.volume,.27);
 await h.command('close',{stream_id:'game'});await h.command('open',{stream_id:'game',url:'https://streamed.pk/watch/game',session_id:'session-b'});
 assert.equal(h.local.owned.game.audio.muted,true);assert.equal(h.local.owned.game.audio.volume,undefined);
});
test('only proven current-frame manual intent changes durable preference; stale revisions lose',async()=>{
 const h=await harness();await h.command('open',{stream_id:'game',url:'https://streamed.pk/watch/game'});
 const url='https://embed.st/embed/game';h.api.webNavigation={getAllFrames:async()=>[{frameId:2,url}]};
 const sender={tab:h.tabs.get(10),frameId:2,url};
 let reply=await h.message({op:'audio_intent',revision:0,audio:{muted:false,volume:.4}},sender);
 assert.equal(reply.audio.muted,false);assert.equal(reply.audio_revision,1);assert.equal(h.tabs.get(10).mutedInfo.muted,false);
 reply=await h.message({op:'audio_intent',revision:0,audio:{muted:true,volume:1}},sender);
 assert.equal(reply.audio.volume,.4);assert.equal(reply.audio_revision,1);
 await h.message({op:'health',health:{muted:true,volume:1}},sender);assert.equal(h.local.owned.game.audio.volume,.4);
 await h.message({op:'audio_intent',revision:1,audio:{muted:true}}, {...sender,frameId:3});assert.equal(h.local.owned.game.audio.muted,false);
 h.tabs.get(10).url='https://personal.example/';assert.equal((await h.message({op:'audio_intent',revision:1,audio:{muted:true}},sender)).owned,false);
});
test('browser UI mute is saved while extension tab updates cannot replace manual intent',async()=>{
 const h=await harness();await h.command('open',{stream_id:'game',url:'https://streamed.pk/watch/game'});
 h.api.tabs.onUpdated.fire(10,{mutedInfo:{muted:false,reason:'user'}});await h.settle();assert.equal(h.local.owned.game.audio.muted,false);
 h.api.tabs.onUpdated.fire(10,{mutedInfo:{muted:true,reason:'extension'}});await h.settle();assert.equal(h.local.owned.game.audio.muted,false);
});
test('browser unmute with omitted reason is remembered; matching programmatic state adds no revision',async()=>{
 const h=await harness();await h.command('open',{stream_id:'game',url:'https://streamed.pk/watch/game'});
 h.api.tabs.onUpdated.fire(10,{mutedInfo:{muted:false}});await h.settle();assert.equal(h.local.owned.game.audio.muted,false);
 const revision=h.local.owned.game.audioRevision;
 h.api.tabs.onUpdated.fire(10,{mutedInfo:{muted:false}});await h.settle();assert.equal(h.local.owned.game.audioRevision,revision);
});

test('generic injection targets an exact document and never falls back to a navigated frame',async()=>{
 const h=await harness();await h.command('open',{stream_id:'game',url:'https://falconstreams.st/game',user_source:true});
 let attempted,executed=0;
 h.api.tabs.get=async id=>{const snapshot={...h.tabs.get(id)};h.tabs.get(id).url='https://offsite.example.com/';return snapshot;};
 h.api.scripting.executeScript=async args=>{attempted=args.target;if(args.target.documentIds?.[0]==='old-document')throw Error('Document no longer exists');executed++;};
 h.api.webNavigation.onCompleted.fire({tabId:10,frameId:0,documentId:'old-document',url:'https://falconstreams.st/game'});await h.settle();
 assert.deepEqual(Array.from(attempted.documentIds),['old-document']);assert.equal(attempted.frameIds,undefined);assert.equal(executed,0);
});
test('generic navigation without document proof is not injected',async()=>{
 const h=await harness();await h.command('open',{stream_id:'game',url:'https://falconstreams.st/game',user_source:true});let calls=0;
 h.api.scripting.executeScript=async()=>calls++;
 h.api.webNavigation.onCompleted.fire({tabId:10,frameId:0,url:'https://falconstreams.st/game'});await h.settle();assert.equal(calls,0);
});
test('known-host nested player injection supports exact documents and the existing frame fallback',async()=>{
 const h=await harness();await h.command('open',{stream_id:'game',url:'https://streamed.pk/watch/game'});const calls=[];
 h.api.scripting.executeScript=async args=>{calls.push(args.target);};
 h.api.webNavigation.onCompleted.fire({tabId:10,frameId:2,documentId:'legacy-player',url:'https://player.example.com/video'});await h.settle();
 h.api.webNavigation.onCompleted.fire({tabId:10,frameId:3,url:'https://player.example.com/other'});await h.settle();
 assert.deepEqual(Array.from(calls[0].documentIds),['legacy-player']);assert.equal(calls[0].frameIds,undefined);
 assert.deepEqual(Array.from(calls[1].frameIds),[3]);
});
test('generic restoration query is canonical and independent of URL path syntax',async()=>{
 const h=await harness();const url='https://video.example.com?watch=1#/player';
 await h.command('open',{stream_id:'game',url,user_source:true});const r=h.local.owned.game,old=h.tabs.get(10);
 h.tabs.delete(10);h.tabs.set(30,{...old,id:30,windowId:30,url:new URL(url).href});await h.api.sessions.setTabValue(30,'streamkeeperOwner',r.token);
 let pattern;const query=h.api.tabs.query;h.api.tabs.query=async q=>{pattern=q.url;return query(q);};
 const result=await h.command('inventory');assert.equal(result.result.game.tabId,30);assert.equal(pattern,'https://video.example.com/*');
});
test('Chromium uses document targeting and private session proof without Firefox APIs',async()=>{
 const h=await harness({}, {},'chromium');const url='https://falconstreams.st/game',calls=[];
 h.api.sessions.getTabValue=async()=>{throw Error('Firefox-only API called');};h.api.sessions.setTabValue=h.api.sessions.getTabValue;
 h.api.webNavigation.getAllFrames=async()=>[{frameId:0,documentId:'chrome-top',url},{frameId:2,documentId:'chrome-player',url:'https://player.example.com/embed'}];
 h.api.scripting.executeScript=async args=>{calls.push(args.target);return [];};
 const opened=await h.command('open',{stream_id:'game',url,user_source:true});assert.ok(opened.result);assert.equal(opened.error,undefined);
 assert.deepEqual(calls.map(c=>Array.from(c.documentIds)),[['chrome-top'],['chrome-player']]);
 assert.ok((await h.command('inventory')).result.game);await h.command('close',{stream_id:'game'});assert.deepEqual(h.removed,[10]);assert.equal(h.tabs.get(1).title,'Personal');
});
