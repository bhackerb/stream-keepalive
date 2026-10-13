const test=require('node:test');const assert=require('node:assert/strict');const fs=require('node:fs');const vm=require('node:vm');
function event(){const callbacks=[];return {addListener:f=>callbacks.push(f),fire:(...a)=>callbacks.forEach(f=>f(...a))};}
async function harness(local={},session={}){
 const sent=[],tabs=new Map([[1,{id:1,url:'https://personal.example/',title:'Personal',windowId:1}]]),removed=[];let next=10;
 const port={postMessage:m=>sent.push(m),onMessage:event(),onDisconnect:event()};
 const store=data=>({get:async k=>({[k]:data[k]}),set:async v=>Object.assign(data,v)});
 const tabValues={};
 const api={sessions:{getTabValue:async id=>tabValues[id],setTabValue:async(id,_key,value)=>{tabValues[id]=value;}},storage:{local:store(local),session:store(session)},runtime:{connectNative:()=>port,getManifest:()=>({version:'fixture',browser_specific_settings:{gecko:{id:'fixture'}}}),onMessage:event()},
  tabs:{get:async id=>{if(!tabs.has(id))throw Error('missing');return tabs.get(id);},query:async q=>{if(!q?.url)throw Error('Personal tab scan forbidden');return [...tabs.values()].filter(t=>t.url.startsWith(q.url.slice(0,-1)));},remove:async id=>{removed.push(id);tabs.delete(id);},sendMessage:async()=>true,reload:async()=>{throw Error('Unexpected reload');},onRemoved:event(),onUpdated:event()},
  windows:{update:async()=>{},create:async v=>{const id=next++;const tab={id,url:v.url,windowId:id};tabs.set(id,tab);return {id,tabs:[tab]};}},alarms:{create:()=>{},onAlarm:event()}};
 vm.runInNewContext(fs.readFileSync(__dirname+'/background.js','utf8'),{browser:api,console,setTimeout:()=>{},URL,Date,Promise,crypto:require('node:crypto').webcrypto});
 const settle=async()=>{for(let i=0;i<12;i++)await new Promise(r=>setImmediate(r));};await settle();
 async function command(op,args={}){const id='request-'+sent.length;port.onMessage.fire({id,op,...args});await settle();return sent.find(m=>m.id===id);}
 return {sent,tabs,removed,command,api,local,port,settle};
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
 const h=await harness();h.api.runtime.getManifest=()=>({version:'fixture'});
 // Chrome detection is based on manifest metadata, not namespace presence.
 const source=fs.readFileSync(__dirname+'/background.js','utf8');assert.ok(source.includes('!!api.runtime.getManifest().browser_specific_settings?.gecko'));
});
