const test=require('node:test');const assert=require('node:assert/strict');const vm=require('node:vm');const fs=require('node:fs');
async function content(owned){
 let listener;
 const video={muted:true,volume:1,paused:false,currentTime:1,videoWidth:1280,videoHeight:720,readyState:4,getVideoPlaybackQuality:()=>({totalVideoFrames:10})};
 const api={runtime:{onMessage:{addListener:f=>{listener=f;}},sendMessage:async()=>({owned,fill:false})}};
 const document={addEventListener:()=>{},querySelectorAll:s=>s==='video'||s==='video,audio'?[video]:[],getElementById:()=>null};
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
