"""Regular-browser control through an authenticated companion, without launching profiles.

Only sustained, fresh NO_VIDEO or previously progressing BUFFERING evidence permits
bounded session recovery. Explicit local keepalive protects one selected owned
tab and needs no catalog or central-server connection; fresh playback, exact
ownership, network checks, and bounded backoff guard same-source refreshes.
Connected outputs supply all placement geometry; unavailable probes pause placement.
Playing, paused, initial loading, stale, closed and
ownership-lost streams never enter session recovery. Explicit resume and a single
unmute-pause repair use only play, bounded by fresh primary-video progress.
The session reconciler opens only new match IDs, and only retires explicitly ended
media with decoded-frame evidence. Catalog disappearance alone never ends a game.
New-tab layout readiness retries only saved title/marker operations for up to 40
seconds after open, audited against its exact tab/window/token receipt. A pending
layout never authorizes another open, reload, adoption, or an ownership bypass.
"""
import asyncio
import json
import logging
import os
import math
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse, quote
import aiohttp
from aiohttp import web
from display_layout import plan_layout
from display_probe import probe_displays
from manual_layout import validate_manual_layout,manual_placements
from session_planner import build_plan, validate_spec, reserve_sports

log=logging.getLogger('stream-keeper.regular')
ALLOWED_HOSTS=('streamed.pk','streamed.su','embed.st','rockylive.st','strmd.top')
PLAYBACK_CONFIRM_TIMEOUT=6
UNMUTE_OBSERVE_TIMEOUT=4
PLACEMENT_READY_TIMEOUT=40
PLACEMENT_RETRY_INTERVAL=1
PLACEMENT_COMMAND_TIMEOUT=3
PLAYBACK_POLL_INTERVAL=.1
def stream_rank(item):
    # Most-watched source first; English and then HD only break ties. Unknown counts rank as zero.
    viewers=item.get('viewers')if type(item.get('viewers'))in(int,float)and math.isfinite(item['viewers'])else 0
    language=str(item.get('language')or'').strip().lower()
    english=language=='english'or language.startswith('english')and not language[7:8].isalpha()
    return (viewers,english,bool(item.get('hd')))
def validate_url(url):
    try:u=urlparse(url)
    except Exception:raise ValueError('Invalid stream URL')
    if u.scheme!='https' or not u.hostname or u.username or u.password or not any(u.hostname==h or u.hostname.endswith('.'+h) for h in ALLOWED_HOSTS):
        raise ValueError('Regular mode supports HTTPS Streamed/player hosts only')
    return url

def recovery_record(value):
    if not isinstance(value,dict)or type(value.get('attempts'))is not int or not 0<=value['attempts']<=3:return None
    if not isinstance(value.get('tried'),list)or len(value['tried'])>10:return None
    record={'attempts':value['attempts'],'tried':value['tried']}
    try:
        for url in record['tried']:validate_url(url)
        if 'url'in value:record['url']=validate_url(value['url'])
    except (ValueError,TypeError):return None
    for key in ('created_at','last_action'):
        if key in value:
            if type(value[key])not in(int,float)or not math.isfinite(value[key])or value[key]<0:return None
            record[key]=value[key]
    if 'pending_release'in value:
        if type(value['pending_release'])is not bool:return None
        record['pending_release']=value['pending_release']
    if 'message'in value:
        if not isinstance(value['message'],str)or len(value['message'])>500:return None
        record['message']=value['message']
    record.setdefault('created_at',time.time())
    return record

class PlacementPending(ValueError):
    """An exact newly opened owned tab has not installed its receiver yet."""
    def __init__(self,sid):
        self.stream_id=sid
        super().__init__('Waiting for the newly opened owned stream tab to finish loading')

class CompanionPage:
    """Only exact layout title operations; no transport of arbitrary JavaScript."""
    def __init__(self,keeper,sid):self.keeper,self.sid=keeper,sid
    async def title(self):return await self.keeper._layout_command(self.sid,'title')
    async def evaluate(self,source,value):
        if source not in ('marker => document.title = marker','title => document.title = title'):
            raise ValueError('Unsupported companion page operation')
        return await self.keeper._layout_command(self.sid,'marker',value=value)

class RegularBrowserKeeper:
    def __init__(self,config):
        self.config=config;self.browser_engine=config.get('browser',{}).get('engine','firefox')
        self.max_streams=config.get('session',{}).get('max_open_streams',max(20,config.get('streams',{}).get('max_streams',10)))
        self.default_site='streamed';self.active_streams={};self.connections={};self.pending={}
        self.layout_status={'success':False,'message':'No companion connected'}
        self.ad_handler=SimpleNamespace(stats={'requests_blocked':0,'overlays_dismissed':0,'network_enabled':False,'network_verified':False,'extension_verified':False,'note':'Regular browser uses its own blockers. Installation and live blocking must be verified in that browser.'})
        self.state_path=Path(config.get('session',{}).get('state_file','regular-session.json'))
        self.session=None;self.plan=None;self.suppressed=set();self.errors=[];self._task=None;self._lock=asyncio.Lock();self._frames={};self._layout_ids=();self._client=None;self._reload_times={};self._browser_ready={};self._inventory_errors={};self._end_seen={};self._ownership_lost=set();self._slot_rows={};self._visible_ids=();self.manual_layout=None;self.saved_layout=None;self._manual_signature=();self._group_epochs={};self._releasing=set();self._recovery={};self._companion_info={};self._progress={}
        self._placement_startup={};self._placement_retry_task=None;self._placement_generation=0
        self.viewing_mode='all';self.local_keepalive={'enabled':False,'profile':'standard'}
        self.display_probe={'state':'unknown','message':'Display detection pending','outputs':[],'displays':{},'availability':{n:'unknown'for n in config.get('layout',{}).get('displays',{})}}
        self._local_care={};self._display_checked=0
        self._load()
    @property
    def is_watching(self):return bool(self.active_streams)
    def _load(self):
        if self.state_path.exists():
            try:
                state=json.loads(self.state_path.read_text());self._recovery={k:r for k,v in (state.get('recovery',{})if isinstance(state.get('recovery'),dict)else {}).items()if isinstance(k,str)and (r:=recovery_record(v))is not None};self._group_epochs=state.get('group_epochs',{});self._releasing=set(state.get('releasing',[]));self.manual_layout=state.get('manual_layout');self.saved_layout=state.get('saved_layout');self.session=state.get('session');self.suppressed=set(state.get('suppressed',[]));self._ownership_lost=set(state.get('ownership_lost',[]));self._slot_rows=state.get('slot_rows',{});self._layout_ids=tuple(state.get('layout_ids',[]));self._visible_ids=tuple(state.get('visible_ids',[]))
                if state.get('viewing_mode')in ('all','laptop-only'):self.viewing_mode=state['viewing_mode']
                care=state.get('local_keepalive',{})
                if isinstance(care,dict)and type(care.get('enabled'))is bool and care.get('profile')in ('standard','travel')and (not care['enabled']or isinstance(care.get('stream_id'),str)):
                    self.local_keepalive={k:care[k]for k in ('enabled','profile','stream_id')if k in care}
                saved_care=state.get('local_care',{})
                if isinstance(saved_care,dict):
                    self._local_care={k:v for k,v in saved_care.items()if k in ('attempts','last_action','next_retry')and type(v)in(int,float)and math.isfinite(v)and v>=0}
                # Migrate legacy TV splits without discarding group ownership or windows.
                for layout in (self.manual_layout,self.saved_layout):
                    if isinstance(layout,dict)and isinstance(layout.get('presets'),dict)and layout['presets'].get('tv',1)>1:
                        layout['presets']['tv']=1
                        layout['assignments']=[a for a in layout.get('assignments',[])if a.get('display')!='tv'or a.get('slot')==0]
                for sid,r in state.get('streams',{}).items():self._add(sid,r)
                for record in self._recovery.values():
                    record.pop('since',None);record.pop('last_health',None)
                self._releasing.intersection_update(self.active_streams)
                if self.manual_layout:
                    try:self.manual_layout=validate_manual_layout(self.manual_layout,self.config.get('layout',{}).get('displays',{}))
                    except ValueError:self.manual_layout=None;self.session=None;self.errors=['Saved display layout no longer matches configuration; streams preserved. Reapply a new layout.']
            except (ValueError,TypeError,KeyError):log.warning('Invalid regular browser state; ignoring')
    def _save(self):
        self._placement_startup={sid:r for sid,r in self._placement_startup.items()if self.active_streams.get(sid)is r[0]}
        self._recovery={sid:r for sid,r in self._recovery.items()if sid in self.active_streams or time.time()-r.get('last_action',r.get('created_at',time.time()))<86400}
        for sid,stream in self.active_streams.items():stream.health_monitor.stats['recovery_count']=self._recovery.get(sid,{}).get('attempts',0)
        value={'local_care':{k:v for k,v in self._local_care.items()if k in ('attempts','last_action','next_retry')},'viewing_mode':self.viewing_mode,'local_keepalive':self.local_keepalive,'recovery':self._recovery,'releasing':sorted(self._releasing),'group_epochs':self._group_epochs,'manual_layout':self.manual_layout,'saved_layout':self.saved_layout,'session':self.session,'suppressed':sorted(self.suppressed),'ownership_lost':sorted(self._ownership_lost),'slot_rows':self._slot_rows,'layout_ids':list(self._layout_ids),'visible_ids':list(self._visible_ids),'streams':{sid:{'url':s.url,'label':s.team,'browser':s.browser,'started_at':s.started_at,'session_id':s.session_id,'parked':s.parked}for sid,s in self.active_streams.items()}}
        self.state_path.parent.mkdir(parents=True,exist_ok=True);temp=self.state_path.with_suffix('.tmp')
        fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'w')as f:
            os.fchmod(f.fileno(),0o600);f.write(json.dumps(value,allow_nan=False))
        temp.replace(self.state_path)
    def _add(self,sid,r):
        for key in list(self._frames):
            if key[0]==sid:del self._frames[key];self._progress.pop(key,None)
        health={'state':'DISCONNECTED','resolution':'unknown','current_time':'0','buffered_to':'unknown','audio':'unknown','muted':True,'volume':0,'stall_duration':'0','recovery_count':0}
        self.active_streams[sid]=SimpleNamespace(id=sid,team=r.get('label',sid),site='streamed',url=r['url'],browser=r['browser'],started_at=r.get('started_at',time.time()),session_id=r.get('session_id'),parked=r.get('parked',False),health_monitor=SimpleNamespace(stats=health),page=CompanionPage(self,sid))
    async def refresh_displays(self):
        self.display_probe=await probe_displays(self.config.get('layout',{}).get('displays',{}),self.config.get('layout',{}).get('backend'))
        self._display_checked=time.time()
        return self.display_probe
    def _displays(self):
        if self.display_probe.get('state')!='available':raise ValueError('Connected display detection unavailable; existing windows preserved')
        displays=self.display_probe.get('displays',{})
        return {n:r for n,r in displays.items()if self.viewing_mode!='laptop-only'or n=='laptop'}
    def _display_target(self,display):
        return 'laptop'if self.viewing_mode=='laptop-only'else display
    def _protected(self,sid):
        return self.local_keepalive.get('enabled')and self.local_keepalive.get('stream_id')==sid
    async def settings_action(self,body):
        if not isinstance(body,dict)or set(body)-{'viewing_mode','keepalive'}:raise ValueError('Invalid local settings')
        mode=body.get('viewing_mode',self.viewing_mode)
        if mode not in ('all','laptop-only'):raise ValueError('viewing_mode must be all or laptop-only')
        care=body.get('keepalive',self.local_keepalive)
        if not isinstance(care,dict)or set(care)-{'enabled','stream_id','profile'}or type(care.get('enabled'))is not bool or care.get('profile','standard')not in ('standard','travel'):raise ValueError('Invalid keepalive settings')
        async with self._lock:
            if 'keepalive'in body and care['enabled']:
                sid=care.get('stream_id');stream=self.active_streams.get(sid)if isinstance(sid,str)else None
                if not stream or stream.parked or sid in self.suppressed|self._ownership_lost|self._releasing:raise ValueError('Select an existing visible owned stream for local keepalive')
            self.viewing_mode=mode;self.local_keepalive={k:care[k]for k in ('enabled','stream_id','profile')if k in care};self.local_keepalive.setdefault('profile','standard')
            self._local_care={};self._layout_ids=();self._manual_signature=();self._save()
            await self.refresh_displays()
            return {'success':True,'message':'Local viewing and keepalive settings saved',**self.extra_status()}
    async def start_browser(self):
        self._client=aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12))
        await self.refresh_displays()
        self._task=asyncio.create_task(self._loop())
    async def shutdown(self):
        if self._placement_retry_task:self._placement_retry_task.cancel();await asyncio.gather(self._placement_retry_task,return_exceptions=True)
        if self._task:self._task.cancel();await asyncio.gather(self._task,return_exceptions=True)
        self._save()
        for ws in list(self.connections.values()):await ws.close()
        if self._client:await self._client.close()
        # Browser and playing tabs survive daemon shutdown.
    async def bridge(self,request):
        ws=web.WebSocketResponse(max_msg_size=256*1024,heartbeat=25);await ws.prepare(request)
        browser=None
        try:
            async for msg in ws:
                if msg.type!=aiohttp.WSMsgType.TEXT:continue
                try:
                    m=json.loads(msg.data)
                    if not isinstance(m,dict):continue
                    if browser is None:
                        if m.get('event')!='hello' or m.get('browser') not in ('firefox','chromium'):break
                        browser=m['browser'];old=self.connections.get(browser)
                        if old:await old.close()
                        self.connections[browser]=ws;self._browser_ready[browser]=False;self._manual_signature=()
                        for key in list(self._frames):
                            if self.active_streams.get(key[0],SimpleNamespace(browser=None)).browser==browser:del self._frames[key];self._progress.pop(key,None)
                        self._companion_info[browser]={'version':str(m.get('version','unknown'))[:40],'capabilities':m.get('capabilities',[])if isinstance(m.get('capabilities'),list)else []}
                    elif m.get('id') in self.pending:
                        future=self.pending[m['id']]
                        if not future.done():
                            if m.get('error'):future.set_exception(ValueError(m['error']))
                            else:future.set_result(m.get('result'))
                    else:await self._event(browser,m)
                except (ValueError,TypeError,KeyError):log.warning('Invalid companion event')
        finally:
            if self.connections.get(browser) is ws:
                del self.connections[browser];self._browser_ready[browser]=False
                for s in self.active_streams.values():
                    if s.browser==browser:s.health_monitor.stats['state']='DISCONNECTED'
            await ws.close()
        return ws
    async def _event(self,browser,m):
        if m.get('event')=='inventory':
            self._inventory_errors[browser]=m.get('errors') or []
            records=m.get('records')
            if not isinstance(records,dict):return
            for sid,r in records.items():
                if not isinstance(sid,str) or not isinstance(r,dict):continue
                try:validate_url(r['url'])
                except (ValueError,KeyError):continue
                self._ownership_lost.discard(sid)
                if sid not in self.active_streams:self._add(sid,dict(r,browser=browser))
            for sid,s in list(self.active_streams.items()):
                if s.browser==browser and sid not in records:
                    self.active_streams.pop(sid,None)
                    if sid in self._releasing:self._releasing.discard(sid)
                    else:self._ownership_lost.add(sid)
            for sid in m.get('lost',[]):
                if isinstance(sid,str):self._ownership_lost.add(sid)
            self._browser_ready[browser]=True;self._save()
        elif m.get('event')=='ownership_lost':
            sid=m.get('stream_id');s=self.active_streams.get(sid)
            if isinstance(sid,str):
                if s and s.browser!=browser:return
                self._ownership_lost.add(sid)
                if s and s.browser==browser:self.active_streams.pop(sid,None);self._releasing.discard(sid)
                self._save()
        elif m.get('event')=='closed':
            sid=m.get('stream_id');
            if sid in self._releasing and self.active_streams.get(sid) and self.active_streams[sid].browser==browser:
                self.active_streams.pop(sid,None);self._releasing.discard(sid);self._save();return
            s=self.active_streams.get(sid)
            if s and s.browser==browser:self.active_streams.pop(sid,None);self._releasing.discard(sid);self.suppressed.add(sid);self._save()
        elif m.get('event')=='health':
            sid=m.get('stream_id');s=self.active_streams.get(sid);h=m.get('health')
            if not s or s.browser!=browser or not isinstance(h,dict):return
            if any(type(h.get(k,0))not in(int,float)or not math.isfinite(h.get(k,0))for k in('width','height','current_time','volume','stall_seconds')):return
            now=time.time();key=(sid,m.get('frame_id'));h=dict(h)
            previous=self._frames.get(key);progress=self._progress.get(key)
            if previous and now-previous[0]<12 and h.get('current_time',0)>previous[1].get('current_time',0):
                before,after=previous[1].get('decoded_frames'),h.get('decoded_frames')
                if before is None or after is None or (type(before)in(int,float)and type(after)in(int,float)and after>before):
                    progress=self._progress[key]=now
            # 0.1.0 reports LOADING after a stall, but only after excluding pause.
            # Startup loading alone never proves that a stream was playing.
            if h.get('state')=='LOADING' and progress is not None and 15<=now-progress and (now-progress<=300 or self._protected(sid)) and h.get('stall_seconds',0)>=15 and h.get('paused')is not True and not h.get('ended') and h.get('width',0)>0:
                h['state']='BUFFERING'
            record=self._recovery.get(sid)
            if record:
                if h.get('state')not in ('NO_VIDEO','BUFFERING') or now-record.get('last_health',now)>=12:record.pop('since',None)
                record['last_health']=now
            self._frames[key]=(now,h)
            rows=[v for (i,_),v in self._frames.items() if i==sid and now-v[0]<12]
            best=max(rows,key=lambda v:(v[1].get('state')=='PLAYING',v[1].get('width',0)*v[1].get('height',0)))[1]
            s.health_monitor.stats.update(state=best.get('state','UNKNOWN'),resolution=f"{best.get('width',0)}x{best.get('height',0)}",current_time=str(round(best.get('current_time',0),2)),stall_duration=str(round(best.get('stall_seconds',0),1)))
            # Playback aggregation can favor a small PLAYING ad. Audio controls
            # must describe the same fresh primary video that volume targets.
            primary=self._primary_video(sid)
            if primary:
                audio=primary[1][1]
                s.health_monitor.stats.update(muted=bool(audio.get('muted',True)),volume=audio.get('volume',0))
    async def command(self,browser,op,**args):
        ws=self.connections.get(browser)
        if not ws or ws.closed or not self._browser_ready.get(browser):raise ValueError(f'Install/connect the StreamKeeper companion in regular {browser} first')
        cid=uuid.uuid4().hex;future=asyncio.get_running_loop().create_future();self.pending[cid]=future
        try:
            await ws.send_json(dict(id=cid,op=op,**args));return await asyncio.wait_for(future,15)
        finally:self.pending.pop(cid,None)
    async def command_for(self,sid,op,**args):
        s=self.active_streams.get(sid)
        if not s:raise ValueError('Stream not found')
        return await self.command(s.browser,op,stream_id=sid,**args)
    def _find_stream(self,sid):return self.active_streams.get(sid)
    async def _open(self,sid,url,label,browser,session_id=None,allow_replacement=False):
        if sid in self.active_streams:return
        validate_url(url)
        record=await self.command(browser,'open',stream_id=sid,url=url,label=label,session_id=session_id,start_muted=self.config.get('session',{}).get('start_muted',True),allow_replacement=allow_replacement)
        self._add(sid,dict(url=url,label=label,browser=browser,session_id=session_id));self._save()
        if isinstance(record,dict)and all(k in record for k in ('tabId','windowId','token')):
            self._placement_startup[sid]=(self.active_streams[sid],dict(record),asyncio.get_running_loop().time()+PLACEMENT_READY_TIMEOUT)
    async def watch_url(self,url,label=None):
        try:
            if len(self.active_streams)>=self.max_streams:raise ValueError('Stream capacity reached')
            await self._open('manual-'+uuid.uuid4().hex[:12],url,label or 'Stream',self.browser_engine)
            return 'Stream window opened in your regular browser; playback may need a click'
        except ValueError as e:return '❌ '+str(e)
    async def watch(self,team,site=None):return '❌ Choose a listed live game or a Streamed watch URL'
    async def keepalive(self):return 'Companion health is report-only; personal tabs are not automatically adopted'
    async def stop(self,sid=None):
        failed=[]
        async with self._lock:
            if sid is None:self.session=None
            self._layout_ids=();self._visible_ids=();self._slot_rows={}
            self._save()
            targets=[sid] if sid else list(self.active_streams)
            for target in targets:
                if target in self.active_streams:
                    self.suppressed.add(target)
                    try:await self.command_for(target,'close')
                    except Exception as e:
                        log.warning('Stream close unavailable: %s',type(e).__name__);failed.append(target);continue
                    self.active_streams.pop(target,None);self._releasing.discard(target)
            self._save()
        return ('Tracking stopped; could not close '+', '.join(failed))if failed else 'Stopped selected StreamKeeper-owned windows'
    async def force_reload(self,sid=None):
        targets=[sid] if sid else list(self.active_streams);skipped=[]
        for target in targets:
            if time.time()-self._reload_times.get(target,0)<600:
                if sid:return '❌ Reload budget: at most once per stream per 10 minutes'
                skipped.append(target);continue
            await self.command_for(target,'reload');self._reload_times[target]=time.time()
            for key in list(self._frames):
                if key[0]==target:del self._frames[key];self._progress.pop(key,None)
        if skipped and len(skipped)==len(targets):return '❌ Reload budget: at most once per stream per 10 minutes'
        return 'Explicit reload requested'+('; skipped (reloaded within 10 minutes): '+', '.join(skipped)if skipped else '')
    async def force_unmute(self,sid=None):
        for target in ([sid] if sid else list(self.active_streams)):await self.command_for(target,'unmute')
        return 'Unmute requested'
    def audio_capabilities(self):
        result={}
        for browser in ('firefox','chromium'):
            info=self._companion_info.get(browser,{})
            available=bool(browser in self.connections and self._browser_ready.get(browser)and 'audio-v1'in info.get('capabilities',[]))
            volume=available and 'primary-volume-v1'in info.get('capabilities',[])
            result[browser]={'available':available,'muted':available,'volume':volume,'version':info.get('version'),'reason':None if available else 'Install/connect signed companion 0.2.0 or later for mute control','volume_reason':None if volume else 'Install/connect signed companion 0.2.1 or later for primary-video volume control'}
        return result

    def _playback_target(self,sid,expected=None):
        stream=self.active_streams.get(sid)
        if not stream or (expected is not None and stream is not expected) or stream.parked or sid in self._releasing or sid in self._ownership_lost or sid in self.suppressed:
            raise ValueError('Choose a visible owned stream with no pending transfer')
        connection=self.connections.get(stream.browser)
        if not connection or connection.closed or not self._browser_ready.get(stream.browser):
            raise ValueError('Browser companion disconnected; reconnect it before resuming')
        return stream

    def _primary_video(self,sid):
        # Aggregate status favors PLAYING children; a small ad must never prove
        # that the actual player resumed. The companion reports each frame's
        # largest video, so select the largest fresh video across those frames.
        now=time.time()
        rows=[(key,row)for key,row in self._frames.items()if key[0]==sid and 0<=now-row[0]<6 and row[1].get('width',0)>0 and row[1].get('height',0)>0]
        return max(rows,key=lambda item:(item[1][1]['width']*item[1][1]['height'],item[1][0]))if rows else None

    @staticmethod
    def _video_advanced(before,after):
        if after.get('state')!='PLAYING' or after.get('paused')is True or after.get('ended'):return False
        times=[before.get('current_time'),after.get('current_time')]
        if any(type(v)not in(int,float)or not math.isfinite(v)for v in times)or times[1]<=times[0]:return False
        frames=[before.get('decoded_frames'),after.get('decoded_frames')]
        if any(v is not None and (type(v)not in(int,float)or not math.isfinite(v))for v in frames):return False
        return None in frames or frames[1]>frames[0]

    async def _resume_playback_locked(self,sid,stream):
        # Health events do not take _lock and can supply evidence while this
        # operation serializes against layout, release, and audio mutations.
        self._playback_target(sid,stream)
        initial=self._primary_video(sid)
        previous={initial[0]:initial[1]}if initial else {}
        minimum_area=initial[1][1]['width']*initial[1][1]['height']if initial else 0
        requested_at=time.time();deadline=asyncio.get_running_loop().time()+PLAYBACK_CONFIRM_TIMEOUT
        command=asyncio.create_task(self.command_for(sid,'play'))
        try:
            while True:
                self._playback_target(sid,stream)
                sample=self._primary_video(sid)
                if sample:
                    key,(observed,health)=sample
                    prior=previous.get(key)
                    if observed>requested_at and health['width']*health['height']>=minimum_area:
                        if prior and observed>prior[0] and self._video_advanced(prior[1],health):
                            return {'success':True,'stream_id':sid,'playback_confirmed':True,'message':'Playback resumed; fresh video progress confirmed'}
                        previous[key]=(observed,health)
                remaining=deadline-asyncio.get_running_loop().time()
                if remaining<=0:break
                await asyncio.sleep(min(PLAYBACK_POLL_INTERVAL,remaining))
            return {'success':False,'stream_id':sid,'playback_confirmed':False,'message':'Playback was not confirmed. The browser or player may block remote play; click Play in this stream window, then refresh status.'}
        finally:
            if not command.done():command.cancel()
            await asyncio.gather(command,return_exceptions=True)

    async def resume_playback(self,sid):
        if not isinstance(sid,str)or not sid:raise ValueError('stream_id is required')
        async with self._lock:
            try:return await self._resume_playback_locked(sid,self._playback_target(sid))
            except ValueError as exc:return {'success':False,'stream_id':sid,'playback_confirmed':False,'message':str(exc)}

    async def _protect_unmute_locked(self,sid,stream,started_at,confirmed_at,primary):
        deadline=asyncio.get_running_loop().time()+UNMUTE_OBSERVE_TIMEOUT
        while True:
            try:self._playback_target(sid,stream)
            except ValueError as exc:return {'playback_confirmed':False,'playback_warning':str(exc)}
            sample=self._primary_video(sid)
            if sample and sample[0]==primary[0] and sample[1][0]>started_at:
                health=sample[1][1]
                if health.get('state')=='PAUSED' or health.get('paused')is True:
                    try:result=await self._resume_playback_locked(sid,stream)
                    except ValueError as exc:result={'success':False,'message':str(exc)}
                    if result['success']:return {'playback_confirmed':True}
                    return {'playback_confirmed':False,'playback_warning':result['message']}
                if sample[1][0]>confirmed_at and self._video_advanced(primary[1][1],health):
                    return {'playback_confirmed':True}
            remaining=deadline-asyncio.get_running_loop().time()
            if remaining<=0:return {'playback_confirmed':False,'playback_warning':'Audio settings applied; fresh playback status was not confirmed. Refresh status and use Resume or click Play in this stream window if paused.'}
            await asyncio.sleep(min(PLAYBACK_POLL_INTERVAL,remaining))

    async def set_audio(self,sid,muted,volume=None):
        if not isinstance(sid,str)or not sid or sid not in self.active_streams:raise ValueError('Choose an active owned stream')
        if type(muted)is not bool:raise ValueError('muted must be boolean')
        if volume is not None and (type(volume)not in(int,float)or not math.isfinite(volume)or not 0<=volume<=1):raise ValueError('volume must be a finite number from 0 to 1')
        async with self._lock:
            stream=self.active_streams.get(sid)
            if not stream or sid in self._releasing or sid in self._ownership_lost:raise ValueError('Owned stream is unavailable')
            if not self.audio_capabilities()[stream.browser]['available']:raise NotImplementedError('Mute and volume require signed companion 0.2.0 or later; existing Unmute remains available')
            primary=self._primary_video(sid)
            protect=not muted and primary and primary[1][1].get('state')=='PLAYING' and primary[1][1].get('paused')is not True and not primary[1][1].get('ended')
            audio_started=time.time()
            args={'muted':muted}
            if volume is not None:
                if not self.audio_capabilities()[stream.browser]['volume']:raise NotImplementedError('Install/connect signed companion 0.2.1 or later for primary-video volume control; mute/unmute remains available')
                if not primary or type(primary[0][1])is not int or primary[0][1]<0:raise ValueError('Fresh primary video unavailable; wait for the player and refresh status before changing volume')
                args.update(volume=volume,primary_frame_id=primary[0][1])
            try:result=await self.command_for(sid,'audio',**args)
            except (ValueError,TimeoutError)as exc:raise ValueError(str(exc)+'; some audio settings may have changed. Refresh status before retrying')from exc
            if not isinstance(result,dict)or result.get('confirmed')is not True or result.get('muted')is not muted or type(result.get('media_count'))is not int or result['media_count']<1 or type(result.get('video_count'))is not int or not 1<=result['video_count']<=result['media_count'] or (volume is not None and (type(result.get('volume'))not in(int,float)or result.get('volume')!=volume or type(result.get('primary_frame_id'))is not int or result['primary_frame_id']!=args['primary_frame_id'])):
                raise ValueError('Browser did not confirm all audio settings; some may have changed. Refresh status before retrying')
            stream.health_monitor.stats['muted']=muted
            if volume is not None:
                stream.health_monitor.stats['volume']=volume
                # Retain confirmed audio until the primary's next heartbeat.
                # Do not extend video freshness or alter progress evidence.
                cached=self._frames.get(primary[0])
                if cached:
                    self._frames[primary[0]]=(cached[0],{**cached[1],'muted':muted,'volume':volume})
            playback=await self._protect_unmute_locked(sid,stream,audio_started,time.time(),primary)if protect else {}
            message='Browser confirmed audio settings'
            if playback.get('playback_warning'):message+='; '+playback['playback_warning']
            return {'success':True,'stream_id':sid,'muted':muted,'volume':result.get('volume'),'confirmed':True,'message':message,**playback}

    async def player_sources(self,sid):
        if not isinstance(sid,str)or sid not in self.active_streams:raise ValueError('Choose an active owned game')
        matches=await asyncio.wait_for(self._json('matches/all'),10)
        row=next((r for r in matches if isinstance(r,dict)and r.get('id')==sid),None)
        if not row:raise ValueError('This game is no longer in the source catalog')
        choices=[];seen=set()
        for source in (row.get('sources')or[])[:3]:
            if not isinstance(source,dict)or not isinstance(source.get('source'),str)or not isinstance(source.get('id'),str):continue
            try:streams=await asyncio.wait_for(self._json('stream/'+quote(source['source'],safe='')+'/'+quote(source['id'],safe='')),8)
            except (aiohttp.ClientError,asyncio.TimeoutError,ValueError):continue
            for item in streams[:30]:
                if not isinstance(item,dict)or type(item.get('streamNo'))is not int or item['streamNo']<1:continue
                url='https://streamed.pk/watch/'+quote(sid,safe='')+'/'+quote(source['source'],safe='')+'/'+str(item['streamNo'])
                if url in seen:continue
                seen.add(url);choices.append(dict(url=url,source=source['source'],number=item['streamNo'],language=str(item.get('language')or'Unknown')[:50],hd=item.get('hd')is True,viewers=item.get('viewers')if type(item.get('viewers'))in(int,float)and math.isfinite(item['viewers'])else None,current=url==self.active_streams.get(sid,SimpleNamespace(url=None)).url))
        return dict(stream_id=sid,choices=choices)

    async def switch_source(self,sid=None,url=None):
        try:options=await asyncio.wait_for(self.player_sources(sid),20)
        except (ValueError,TimeoutError,aiohttp.ClientError)as e:return '❌ '+(str(e)or'Source lookup timed out; try again')
        async with self._lock:
            released=False
            try:
                stream=self.active_streams.get(sid)
                if not stream or stream.parked or sid in self._releasing or sid in self._ownership_lost:raise ValueError('Choose a visible, owned game with no pending transfer')
                if not isinstance(url,str)or url not in {c['url']for c in options['choices']}:raise ValueError('Choose an available source from this game')
                if url==stream.url:return 'Already using this source'
                audit=await self.command(stream.browser,'inventory')
                if not isinstance(audit,dict)or sid not in audit or self._inventory_errors.get(stream.browser):raise ValueError('Ownership audit failed; source unchanged')
                if self.active_streams.get(sid)is not stream:raise ValueError('Game changed while loading sources')
                placements=[p for p in self.layout_status.get('placements',[])if p['stream_id']in self.active_streams]
                record=self._recovery.setdefault(sid,dict(attempts=0,tried=[],created_at=time.time()))
                record.update(pending_release=True,url=url,last_action=time.time(),message='Manual source switch requested');record.pop('since',None)
                self._releasing.add(sid);self._save()
                if await self.command_for(sid,'close')is not True:raise ValueError('Source close unconfirmed; no replacement opened')
                audit=await self.command(stream.browser,'inventory')
                if not isinstance(audit,dict)or sid in audit or self._inventory_errors.get(stream.browser):raise ValueError('Source release unconfirmed; no replacement opened')
                self.active_streams.pop(sid,None);self._releasing.discard(sid);record['pending_release']=False;self._save()
                released=True;self._manual_signature=();self._layout_ids=()
                if sid in self.suppressed or sid in self._ownership_lost:raise ValueError('Game was closed; replacement blocked')
                for key in list(self._frames):
                    if key[0]==sid:del self._frames[key];self._progress.pop(key,None)
                await self._open(sid,url,stream.team,stream.browser,stream.session_id)
                record['message']='Manual source opened'
                self._manual_signature=();self._layout_ids=();self._save()
                if placements:
                    result=await self._place(placements)
                    if not result['success']:return 'Source opened; layout retry pending: '+result['message']
                return 'Source switched; fullscreen and display slot preserved'
            except Exception as e:
                if released:
                    if sid in self.active_streams:
                        record['message']='Source opened; layout retry pending';self._save()
                        return '❌ Source opened; layout retry pending: '+(str(e)or'Controller timed out')
                    record['message']='Manual source retry pending: '+str(e);self._save()
                    return '❌ Source released; selected source saved for session retry: '+str(e)
                return '❌ '+(str(e)or'Controller timed out; ownership confirmation required')
    async def take_screenshot(self,sid=None):return None
    async def get_status(self,sid=None):return json.dumps(self.extra_status())
    async def set_tab_view(self,sid,enabled):
        for target in ([sid] if sid else list(self.active_streams)):await self.command_for(target,'fill',enabled=enabled)
        return {'success':True,'message':'Tab fill setting updated'}
    async def restore_live_layout(self,display):
        """Reapply current intent only: never open, close, transfer, or rank games."""
        async with self._lock:
            await self.refresh_displays()
            if display not in self._displays():raise ValueError('Display is disconnected, unverified, or excluded by viewing mode')
            if not self.session:raise ValueError('No active viewing session')
            placements=[p for p in (self.plan or {}).get('placements',[]) if p.get('display')==display]
            if not placements:raise ValueError('No live placement on this display')
            if any(p['stream_id'] not in self.active_streams or self.active_streams[p['stream_id']].parked or p['stream_id'] in self._releasing or p['stream_id'] in self._ownership_lost for p in placements):raise ValueError('Live display is changing; refresh status before restoring')
            for p in placements:
                if await self.command_for(p['stream_id'],'fill',enabled=True) is not True:raise ValueError('Player fill request was not accepted; layout unchanged')
            previous=(self.plan or {}).get('placements',[])
            result=await self._place(placements)
            if result['success']:
                self.layout_status={**result,'placements':[p for p in previous if p.get('display')!=display]+placements}
                return {**self.layout_status,'message':'Live display restored; current games and draft preserved'}
            self.layout_status={**result,'placements':previous}
            return result

    async def _maintain_tv_focus(self):
        # Only the active full-size TV slot is maintained. Escape disables fill
        # in the companion; respect that explicit exit until Restore is clicked.
        if not self.session or self.config.get('layout',{}).get('backend')=='kwin':return
        placements=[p for p in (self.plan or {}).get('placements',[])if p.get('display')=='tv']
        tv=self._displays().get('tv',{})
        if len(placements)!=1:return
        p=placements[0];sid=p['stream_id'];s=self.active_streams.get(sid)
        if not all(p.get(k)==tv.get(k)for k in ('x','y','width','height')) or not s or s.session_id!=self.session['id'] or s.parked or sid in self._releasing or sid in self._ownership_lost or not self._browser_ready.get(s.browser):return
        records=await self.command(s.browser,'inventory')
        if not isinstance(records,dict) or self._inventory_errors.get(s.browser) or records.get(sid,{}).get('fill') is not True:return
        if self.active_streams.get(sid) is not s or sid in self._releasing or sid in self._ownership_lost:return
        from x11_windows import window_for_page,window_matches,wait_window_matches,move_window
        wid=await window_for_page(s.page)
        if not await asyncio.to_thread(window_matches,wid,p,fullscreen=True):
            await asyncio.to_thread(move_window,wid,p,fullscreen=True)
            if not await asyncio.to_thread(wait_window_matches,wid,p,fullscreen=True):raise ValueError('TV focus geometry not confirmed')
    async def arrange_streams(self,display,mode,ids=None):
        try:
            await self.refresh_displays()
            placements=plan_layout(self._displays(),self._display_target(display),mode,ids or list(self.active_streams))
            return await self._place(placements)
        except (ValueError,TimeoutError) as e:
            self.layout_status={'success':False,'message':str(e)};return self.layout_status
    async def _layout_command(self,sid,op,**args):
        """Retry only title/marker readiness, retaining the companion's ownership fence.

        Inventory can retain a just-created about:blank tab while mutation proof
        still refuses it. Compare the exact open receipt before treating that
        refusal (or a missing content receiver) as temporary. Never reopen it.
        """
        startup=self._placement_startup.get(sid)
        remaining=startup[2]-asyncio.get_running_loop().time()if startup else 0
        if startup and remaining<=0:
            self._placement_startup.pop(sid,None)
            raise ValueError('New owned stream tab did not become ready for placement within 40 seconds')
        try:
            if remaining>0:
                return await asyncio.wait_for(self.command_for(sid,op,**args),min(PLACEMENT_COMMAND_TIMEOUT,remaining))
            return await self.command_for(sid,op,**args)
        except (ValueError,TimeoutError)as exc:
            transient=isinstance(exc,TimeoutError)or str(exc)in (
                'Owned stream is unavailable','Owned stream is still loading',
                'Could not establish connection. Receiving end does not exist.',
                'The message port closed before a response was received.')
            if not transient or not startup or remaining<=0:raise
            stream,receipt,deadline=startup
            if self.active_streams.get(sid)is not stream or sid in self.suppressed|self._ownership_lost|self._releasing:raise
            remaining=deadline-asyncio.get_running_loop().time()
            if remaining<=0:raise
            try:audit=await asyncio.wait_for(self.command(stream.browser,'inventory'),min(PLACEMENT_COMMAND_TIMEOUT,remaining))
            except TimeoutError as audit_error:raise ValueError('Placement readiness audit timed out; ownership was not confirmed')from audit_error
            current=audit.get(sid)if isinstance(audit,dict)else None
            if self._inventory_errors.get(stream.browser)or not isinstance(current,dict)or any(current.get(k)!=receipt[k]for k in ('tabId','windowId','token')):
                raise ValueError('Placement ownership audit failed; exact opened tab is unavailable')from exc
            if self.active_streams.get(sid)is not stream or sid in self.suppressed|self._ownership_lost|self._releasing:raise
            raise PlacementPending(sid)from exc

    async def _place(self,placements):
        self._placement_generation+=1;generation=self._placement_generation
        if self._placement_retry_task:
            self._placement_retry_task.cancel();await asyncio.gather(self._placement_retry_task,return_exceptions=True)
            self._placement_retry_task=None
        result=await self._place_once(placements)
        if result.get('pending'):
            self._placement_retry_task=asyncio.create_task(self._retry_placement(placements,generation,self.session,self.manual_layout))
        return result

    async def _retry_placement(self,placements,generation,session,manual):
        targets={p['stream_id']:self.active_streams.get(p['stream_id'])for p in placements}
        while True:
            await asyncio.sleep(PLACEMENT_RETRY_INTERVAL)
            async with self._lock:
                if generation!=self._placement_generation:return
                if self.session is not session or self.manual_layout is not manual or any(self.active_streams.get(sid)is not stream or sid in self.suppressed|self._ownership_lost|self._releasing for sid,stream in targets.items()):
                    self.layout_status={'success':False,'pending':False,'message':'Placement canceled because session or stream ownership changed'};return
                try:
                    # Revalidate the original open receipt even when the next
                    # marker command succeeds; a restored/replaced tab is not
                    # the tab accepted for this bounded retry.
                    for sid in targets:
                        startup=self._placement_startup.get(sid)
                        if not startup:continue
                        remaining=startup[2]-asyncio.get_running_loop().time()
                        if remaining<=0:raise ValueError('New owned stream tab did not become ready for placement within 40 seconds')
                        audit=await asyncio.wait_for(self.command(startup[0].browser,'inventory'),min(PLACEMENT_COMMAND_TIMEOUT,remaining))
                        current=audit.get(sid)if isinstance(audit,dict)else None
                        if self._inventory_errors.get(startup[0].browser)or not isinstance(current,dict)or any(current.get(k)!=startup[1][k]for k in ('tabId','windowId','token')):
                            raise ValueError('Placement ownership audit failed; exact opened tab is unavailable')
                    if any(self.active_streams.get(sid)is not stream or sid in self.suppressed|self._ownership_lost|self._releasing for sid,stream in targets.items()):raise ValueError('Placement ownership changed')
                    result=await self._place_once(placements)
                except Exception as exc:
                    log.exception('New owned tab placement retry failed')
                    result=self.layout_status={'success':False,'message':'Placement retry failed: '+(str(exc)or type(exc).__name__)}
                if result.get('pending'):continue
                if result['success']:
                    if manual is not None:self._manual_signature=tuple((p['stream_id'],p['x'],p['y'],p['width'],p['height'])for p in placements)
                    elif session is not None:self._layout_ids=tuple(p['stream_id']for p in placements)
                elif not result.get('pending'):self.errors.append(result['message'])
                self._save();return

    async def _place_once(self,placements):
        try:
            await self.refresh_displays()
            displays=self._displays()
            for p in placements:
                region=displays.get(p.get('display'))
                if not region or p['x']<region['x']or p['y']<region['y']or p['x']+p['width']>region['x']+region['width']or p['y']+p['height']>region['y']+region['height']:raise ValueError('Placement targets an unavailable display or stale desktop geometry')
            borderless=self.config.get('layout',{}).get('borderless',True) is not False
            if any(p['stream_id'] not in self.active_streams for p in placements):raise ValueError('Unknown stream')
            if self.config.get('layout',{}).get('backend')=='kwin':
                from kwin_layout import arrange_windows
                result=await arrange_windows(self.active_streams,placements,borderless=borderless)
            else:
                from x11_windows import window_for_page,move_window,wait_window_matches
                # Identify every window before moving any; never move a shared personal window.
                windows=[await window_for_page(self.active_streams[p['stream_id']].page)for p in placements]
                if len(set(windows))!=len(windows):raise ValueError('Streams must occupy separate windows')
                tv=displays.get('tv',{})
                for wid,p in zip(windows,placements):
                    # A reserved half-slot stays tiled even if its other game is pending.
                    focus=sum(q.get('display')=='tv'for q in placements)==1 and p.get('display')=='tv' and all(p.get(k)==tv.get(k)for k in ('x','y','width','height'))
                    await asyncio.to_thread(move_window,wid,p,fullscreen=focus,borderless=borderless)
                    if focus and not await asyncio.to_thread(wait_window_matches,wid,p,fullscreen=True):raise ValueError('TV focus geometry not confirmed')
                result={'success':True,'message':'X11 placement requested; full-TV focus geometry confirmed when applicable'}
            if result['success']:
                for p in placements:self._placement_startup.pop(p['stream_id'],None)
            self.layout_status={**result,'placements':placements};return self.layout_status
        except PlacementPending as e:
            confirmed=self.layout_status.get('placements',[])if self.layout_status.get('success')else self.layout_status.get('confirmed_placements',[])
            pending=[p['stream_id']for p in placements if p['stream_id']in self._placement_startup]
            self.layout_status={'success':False,'pending':True,'pending_stream_ids':pending or [e.stream_id],'confirmed_placements':confirmed,'message':str(e),'placements':placements};return self.layout_status
        except (ValueError,TimeoutError) as e:
            self.layout_status={'success':False,'message':str(e)};return self.layout_status
    def extra_status(self):
        return {'displays':self.display_probe.get('displays',{}),'connected_displays':list(self.display_probe.get('displays',{})),'display_probe':self.display_probe,'viewing':{'mode':self.viewing_mode},'keepalive':{**self.local_keepalive,**{k:v for k,v in self._local_care.items()if k in ('state','message','attempts','next_retry')}},'control_mode':'regular','audio_capabilities':self.audio_capabilities(),'browser_connections':{b:bool(b in self.connections and self._browser_ready.get(b))for b in ('firefox','chromium')},'session':{'active':bool(self.session),'group_id':(self.session or{}).get('group_id'),'group_epoch':(self.session or{}).get('group_epoch'),'ownership_lost':sorted(self._ownership_lost),'recovery':self._recovery,'plan':self.plan,'errors':self.errors,'suppressed':sorted(self.suppressed),'manual_layout':self.manual_layout,'saved_layout':self.saved_layout}}
    async def _json(self,path):
        async with self._client.get('https://streamed.pk/api/'+path) as res:
            res.raise_for_status();value=await res.json()
            if not isinstance(value,list):raise ValueError('Invalid Streamed catalog response')
            return value
    async def discover(self,spec,until_ms=None):
        await self.refresh_displays()
        displays=self._displays();spec={**spec,'display':self._display_target(spec.get('display','dock'))}
        all_matches,live=await asyncio.gather(self._json('matches/all'),self._json('matches/live'))
        merged={m['id']:m for m in all_matches+live if isinstance(m,dict) and isinstance(m.get('id'),str)}
        selected=[m for m in merged.values()if m.get('category') in spec['sports'] and type(m.get('date'))in(int,float)and m['date']>0]
        viewers={};playback={};semaphore=asyncio.Semaphore(4)
        async def measure(match):
            counts=[];candidates=[]
            for source in (match.get('sources')or[])[:3]:
                if not isinstance(source,dict):continue
                try:
                    async with semaphore:streams=await self._json('stream/'+quote(source['source'],safe='')+'/'+quote(source['id'],safe=''))
                    candidates.extend((source['source'],s)for s in streams if isinstance(s,dict) and type(s.get('streamNo')) is int and s['streamNo']>0)
                    counts.extend(s['viewers'] for s in streams if isinstance(s,dict) and type(s.get('viewers')) in (int,float) and s['viewers']>=0)
                except (aiohttp.ClientError,asyncio.TimeoutError,ValueError,KeyError):pass
            if counts:viewers[match['id']]=max(counts)
            if candidates:
                source,s=max(candidates,key=lambda v:stream_rank(v[1]))
                playback[match['id']]='https://streamed.pk/watch/'+quote(match['id'],safe='')+'/'+quote(source,safe='')+'/'+str(s['streamNo'])
        now_ms=int(time.time()*1000);lead=spec.get('preload_minutes',10)*60000
        live_ids={m['id']for m in live if isinstance(m,dict)and isinstance(m.get('id'),str)}
        measured=[m for m in selected if m['id'] in live_ids or now_ms-1800000<=m['date']<=now_ms+lead]
        try:await asyncio.wait_for(asyncio.gather(*(measure(m)for m in measured)),20)
        except asyncio.TimeoutError:pass # Partial viewer evidence; still schedule later starts.
        missing=sum(m['id']not in playback for m in measured)
        if missing:log.warning('Source measurement incomplete: %d of %d matches lack a ranked playback URL',missing,len(measured))
        ready_ids=live_ids|{m['id']for m in selected if m['id']in playback and now_ms-1800000<=m['date']<=now_ms+lead}
        plan=build_plan(list(merged.values()),ready_ids,viewers,spec,displays,int(time.time()*1000),until_ms)
        for row in plan['live']:
            row['playback_url']=playback.get(row['id']);row['source_ready']=bool(playback.get(row['id']));row['prestart']=row['id']not in live_ids and row['start_ms']>now_ms;row['live']=row['id']in live_ids
        return plan
    async def session_action(self,body):
        action=body.get('action','plan')
        if action=='status':return {'success':True,**self.extra_status()['session']}
        if action=='stop':
            failed=[]
            async with self._lock:
                current=self.session['id'] if self.session else None
                self.session=None;self.manual_layout=None;self._layout_ids=();self._visible_ids=();self._slot_rows={};self._save()
                for sid,s in list(self.active_streams.items()):
                    if s.session_id and (not current or s.session_id==current):
                        self.suppressed.add(sid)
                        try:await self.command_for(sid,'close')
                        except Exception as e:
                            log.warning('Stream close unavailable: %s',type(e).__name__);failed.append(sid);continue
                        self.active_streams.pop(sid,None);self._releasing.discard(sid)
                self._save()
            return {'success':True,'message':'Session tracking stopped; other owned streams preserved','errors':['Could not close '+sid for sid in failed]}
        if action not in ('start','plan'):raise ValueError('action must be plan, start, status, or stop')
        spec=validate_spec(body);plan=await self.discover(spec)
        if action=='plan':return {'success':True,'plan':plan}
        async with self._lock:
            if self.session:raise ValueError('A session is already active; stop it before changing sports')
            self.session={'id':uuid.uuid4().hex,'spec':spec,'until_ms':plan['until_ms']};self.plan=plan;self.manual_layout=None;self.suppressed.clear();self._ownership_lost.clear();self._layout_ids=();self._visible_ids=();self._slot_rows={};self._save()
            await self._reconcile(plan,allow_replacements=True)
        return {'success':True,'message':'Session scheduled; inspect browser connections and playback status','plan':plan,'errors':self.errors}
    async def _retire_ended(self,plan):
        # Retire only a session-owned stream with explicit video end and prior progress.
        for sid,s in list(self.active_streams.items()):
            rows=[v for (i,_),v in self._frames.items()if i==sid and time.time()-v[0]<12 and v[1].get('width',0)>0]
            if not self._protected(sid) and self.session and s.session_id==self.session['id'] and sid not in {r['id']for r in plan['live']} and rows and all(v[1].get('ended') is True for v in rows):
                self._end_seen.setdefault(sid,time.time())
                if time.time()-self._end_seen[sid]<60:continue
                try:await self.command_for(sid,'close')
                except (ValueError,TimeoutError):continue
                self.active_streams.pop(sid,None);self._releasing.discard(sid);self.suppressed.add(sid)
            else:self._end_seen.pop(sid,None)
    async def _reconcile(self,plan,allow_replacements=False):
        if not self.session:return
        await self.refresh_displays()
        self._displays() # Block new opens/parking when live display evidence is unknown.
        if self.local_keepalive.get('enabled'):return # Explicit local selection owns ongoing care until disabled.
        if self.manual_layout:return await self._reconcile_manual(plan,allow_replacements)
        browser=self.session['spec']['browser'];self.errors=[]
        rows={r['id']:r for r in plan['live']}
        for sid,row in rows.items():
            prior=self._slot_rows.get(sid,{})
            if row['viewers']is None and prior.get('viewers')is not None:
                row['viewers']=prior['viewers'];row['viewer_stale']=True
        if any(r.get('viewer_stale')for r in rows.values()):plan['viewer_metric']+='; unavailable source counts retain last known values'
        self._slot_rows.update(rows)
        # A catalog gap must not hide a progressing game. Keep its last known rank.
        for sid,s in list(self.active_streams.items()):
            if sid not in rows and s.session_id==self.session['id'] and not s.parked:
                rows[sid]=self._slot_rows.get(sid,dict(id=sid,title=s.team,favorite=False,viewers=None,start_ms=int(s.started_at*1000),sources=[]))
        ranked=sorted(rows.values(),key=lambda r:(not r['favorite'],-(r['viewers']if r['viewers']is not None else -1),r['start_ms'],r['id']))
        eligible=[r for r in ranked if r['id'] not in self.suppressed and r['id'] not in self._ownership_lost and (r['id']in self.active_streams or r.get('sources'))]
        capacity=min(plan['capacity'],self.max_streams)
        if len(eligible)<=capacity:selected=eligible
        else:
            previous=set(self._visible_ids)
            selected=[r for r in eligible if r['id']in previous][:capacity]
            for candidate in eligible:
                if candidate in selected:continue
                if len(selected)<capacity:selected.append(candidate);continue
                worst=max(selected,key=lambda r:(not r['favorite'],-(r['viewers']if r['viewers']is not None else -1),r['start_ms'],r['id']))
                new,old=candidate['viewers'],worst['viewers']
                better=(candidate['favorite'] and not worst['favorite'])or(candidate['favorite']==worst['favorite']and new is not None and (old is None and new>=5 or old is not None and new>max(old*1.2,old+5)))
                if better:selected.remove(worst);selected.append(candidate)
            selected.sort(key=lambda r:(not r['favorite'],-(r['viewers']if r['viewers']is not None else -1),r['start_ms'],r['id']))

        selected=reserve_sports(selected,eligible,self.session['spec']['sports'],capacity)
        await self._retire_ended(plan)
        selected=[r for r in selected if r['id']not in self.suppressed]
        # No catalog-based close. Running games survive source/API outages.
        selected_ids={r['id']for r in selected}
        for sid,s in list(self.active_streams.items()):
            if s.session_id==self.session['id']:
                park=sid not in selected_ids
                if park!=s.parked:
                    try:await self.command_for(sid,'park',enabled=park);s.parked=park
                    except (ValueError,TimeoutError)as e:self.errors.append(str(e));return
        available=max(0,self.max_streams-len(self.active_streams));lookup_deadline=asyncio.get_running_loop().time()+10
        if self._ownership_lost:self.errors.append('Ownership lost; existing windows left alone. Restart the session to open replacements.')
        for row in selected:
            if row['id'] in self.active_streams:continue
            if available<=0:self.errors.append('All available stream slots are occupied');break
            sources=row['sources']
            if not sources:self.errors.append(row['title']+': source not yet available');continue
            try:
                url=await self._opening_url(row,lookup_deadline)
                if not self._openable(row['id']):continue # Closed or released during lookup.
                await self._open(row['id'],url,row['title'],browser,self.session['id'],allow_replacement=allow_replacements);available-=1
            except (ValueError,KeyError,TimeoutError) as e:self.errors.append(row['title']+': '+str(e))
        # Stable order between joins: viewers cannot cause continuous reshuffling.
        desired=[r['id']for r in selected if r['id'] in self.active_streams]
        prior=[sid for sid in self._layout_ids if sid in desired]
        if set(prior)==set(desired):desired=prior
        self._visible_ids=tuple(desired)
        plan['selected']=selected
        plan['overflow']=[r for r in plan['live']if r['id']not in {s['id']for s in selected}]
        display=self.session['spec']['display']
        if desired and tuple(desired)!=self._layout_ids:
            result=await self.arrange_streams(display,'grid',desired)
            if result['success']:self._layout_ids=tuple(desired)
            elif not result.get('pending'):self.errors.append(result['message'])
        self._save()
    async def set_session_layout(self,body):
        if not isinstance(body,dict)or body.get('mode')not in('manual','auto'):raise ValueError('mode must be manual or auto')
        async with self._lock:
            if body['mode']=='auto':
                self.manual_layout=None;self._layout_ids=();self._visible_ids=();self._manual_signature=();self._save()
                if self.session:
                    self.plan=await self.discover(self.session['spec'],self.session['until_ms']);await self._reconcile(self.plan)
                return {'success':True,'message':'Automatic priority layout selected','errors':self.errors}
            await self.refresh_displays()
            layout=validate_manual_layout(body,self._displays())
            if type(body.get('start',False))is not bool:raise ValueError('start must be boolean')
            spec=validate_spec(body) if body.get('start')or body.get('sports') else None
            group=body.get('group_id')
            if group is not None and(not isinstance(group,str)or len(group)!=32 or any(c not in '0123456789abcdef'for c in group)):raise ValueError('Invalid group ID')
            epoch=body.get('group_epoch',0)
            if group and(type(epoch)is not int or epoch<0 or epoch<=self._group_epochs.get(group,-1)):raise ValueError('Stale or invalid shared-session epoch')
            if not body.get('start'):
                self.saved_layout={**layout,'spec':spec};self._save()
                return {'success':True,'message':'Layout draft saved; no windows changed','layout':self.saved_layout}
            recoveries=body.get('recover_ids',[])
            reopen=body.get('reopen_ids',[])
            if not isinstance(reopen,list)or len(reopen)>14 or any(not isinstance(i,str)for i in reopen)or(reopen and not group):raise ValueError('Reopen requires explicit game IDs in a shared session')
            if not isinstance(recoveries,list)or len(recoveries)>14 or any(not isinstance(i,str)or i not in {a['game_id']for a in layout['assignments']}for i in recoveries)or(recoveries and not group):raise ValueError('Recovery requires explicit assigned game IDs in a shared session')
            if self.session and spec['browser']!=self.session['spec']['browser']:raise ValueError('Stop the active session before changing browsers')
            if recoveries or reopen:
                audit=await self.command(spec['browser'],'inventory')
                if audit is False or self._inventory_errors.get(spec['browser']):raise ValueError('Ownership inventory failed; replacement blocked')
            plan=await self.discover(spec)
            catalog_ids={r['id']for r in plan['live']+plan['pending']}
            if any(i not in catalog_ids for i in reopen):raise ValueError('Reopen game is outside the current catalog')
            known=catalog_ids|set(self.active_streams)
            if any(a['game_id']in self._releasing for a in layout['assignments']):raise ValueError('Previous source release still unconfirmed; wait for close or stop that owned stream')
            if any(a['game_id']not in known for a in layout['assignments']):raise ValueError('One or more games are outside the current live/later-start catalog; refresh the games list')
            releases=body.get('release_ids',[])
            if not isinstance(releases,list)or len(releases)>20 or any(not isinstance(i,str)for i in releases)or (releases and not group):raise ValueError('Invalid shared-session releases')
            if set(releases)&{a['game_id']for a in layout['assignments']}:raise ValueError('Cannot release an assigned game')
            protected=self.local_keepalive.get('stream_id')if self.local_keepalive.get('enabled')else None
            if protected and protected not in releases and protected not in {a['game_id']for a in layout['assignments']}:raise ValueError('Selected local keepalive stream must retain its assignment or receive an explicit release')
            if type(body.get('reset_suppression',False))is not bool or(body.get('reset_suppression')and not group):raise ValueError('Invalid suppression reset')
            if body.get('reset_suppression')and group not in self._group_epochs:self.suppressed.difference_update(r['id']for r in plan['live']+plan['pending'])
            # Commit the new desired layout before a potentially delayed close, so
            # the old local loop cannot reopen departing games after an API timeout.
            if group:self._group_epochs[group]=epoch
            self.saved_layout={**layout,'spec':spec};self.manual_layout=layout;self._manual_signature=()
            self.session={'id':self.session['id']if self.session else uuid.uuid4().hex,'spec':spec,'until_ms':plan['until_ms'],'group_id':group,'group_epoch':epoch}
            for a in layout['assignments']:
                if a['game_id']in self.active_streams:self.active_streams[a['game_id']].session_id=self.session['id']
            self.suppressed.difference_update(reopen)
            self._ownership_lost.difference_update(recoveries)
            if protected in releases:self.local_keepalive={'enabled':False,'profile':self.local_keepalive.get('profile','standard')};self._local_care={}
            self.plan=plan;self._releasing.update(sid for sid in releases if sid in self.active_streams);self._save()
            for sid in releases:
                if sid not in self.active_streams:continue
                result=await self.command_for(sid,'close')
                if result is not True:raise ValueError('Companion did not confirm source window close')
                self.active_streams.pop(sid,None);self._releasing.discard(sid);self._save()
            try:await self._reconcile_manual(plan,replacement_ids=set(recoveries))
            except Exception as e:
                log.exception('Manual layout partially applied; desired layout retained for retry')
                self.errors.append('Layout partially applied; retained for next poll retry: '+type(e).__name__)
            return {'success':True,'message':'Manual layout scheduled; future games keep their assigned slots','plan':plan,'errors':self.errors,'placement_pending':bool(self.layout_status.get('pending')),'layout':layout}

    async def _reconcile_manual(self,plan,allow_replacements=False,replacement_ids=None):
        await self.refresh_displays()
        self._displays()
        validate_manual_layout(self.manual_layout,self._displays()) # Validate before any browser mutations.
        self.errors=[];await self._retire_ended(plan);assignments=self.manual_layout['assignments'];wanted={a['game_id']for a in assignments}
        live={r['id']:r for r in plan['live']};visible={sid for sid in wanted if sid in live or sid in self.active_streams}
        visible-=self.suppressed|self._ownership_lost|self._releasing
        for sid,s in list(self.active_streams.items()):
            if s.session_id:
                park=sid not in visible
                if park!=s.parked:
                    try:await self.command_for(sid,'park',enabled=park);s.parked=park
                    except (ValueError,TimeoutError)as e:self.errors.append('Could not park an unassigned game; placement deferred to avoid overlap: '+str(e));self._save();return
        browser=self.session['spec']['browser'];lookup_deadline=asyncio.get_running_loop().time()+10
        for sid in visible:
            if sid in self.active_streams:continue
            row=live[sid]
            if len(self.active_streams)>=self.max_streams:self.errors.append('Open-window budget reached');continue
            sources=row.get('sources')or[]
            if not sources:self.errors.append(row['title']+': waiting for a source');continue
            try:
                url=await self._opening_url(row,lookup_deadline)
                if not self._openable(sid):continue # Closed or released during lookup.
                await self._open(sid,url,row['title'],browser,self.session['id'],allow_replacement=allow_replacements or sid in (replacement_ids or set()))
            except (ValueError,TimeoutError,KeyError)as e:self.errors.append(row['title']+': '+str(e))
        placements=manual_placements(self.manual_layout,self._displays(),visible&set(self.active_streams))
        signature=tuple((p['stream_id'],p['x'],p['y'],p['width'],p['height'])for p in placements)
        if placements and signature!=self._manual_signature:
            result=await self._place(placements)
            if result['success']:self._manual_signature=signature
            elif not result.get('pending'):self.errors.append(result['message'])
        for row in plan['pending']:row['scheduled_in_session']=row['id']in wanted
        plan.update(mode='manual',selected=[r for r in plan['live']if r['id']in wanted],overflow=[r for r in plan['live']if r['id']not in wanted],placements=placements)
        self._visible_ids=tuple(p['stream_id']for p in placements);self._save()

    def _recoverable_failure(self,sid,now):
        stream=self.active_streams.get(sid)
        if not self.session or not stream or stream.session_id!=self.session['id'] or stream.parked:return False
        if sid in self.suppressed or sid in self._ownership_lost or sid in self._releasing or sid not in self._visible_ids:return False
        if not self._browser_ready.get(stream.browser):return False
        rows=[v for (i,_),v in self._frames.items()if i==sid]
        if not rows or any(now-v[0]>=12 for v in rows):return False
        if all(v[1].get('state')=='NO_VIDEO'for v in rows):return True
        buffering=[(key,v)for key,v in self._frames.items()if key[0]==sid and v[1].get('state')=='BUFFERING']
        return bool(buffering)and all(v[1].get('state')in ('NO_VIDEO','BUFFERING')for v in rows)and all(key in self._progress and 15<=now-self._progress[key]<=300 and v[1].get('paused')is not True and not v[1].get('ended')for key,v in buffering)

    async def _opening_url(self,row,deadline):
        # Saved recovery URL, then the discovery choice, then a bounded viewer-ranked
        # lookup; only an unavailable lookup falls back to the first source's stream 1.
        url=self._recovery.get(row['id'],{}).get('url')or row.get('playback_url')
        if url:return url
        remaining=deadline-asyncio.get_running_loop().time()
        if self._client and remaining>0:
            try:url=await asyncio.wait_for(self._recovery_sources(row,None,[]),min(8,remaining))
            except Exception:log.warning('Ranked source lookup failed for %s; using first source',row['id'])
        return url or 'https://streamed.pk/watch/'+quote(row['id'],safe='')+'/'+quote(row['sources'][0]['source'],safe='')+'/1'
    def _openable(self,sid):
        return sid not in self.active_streams and sid not in self.suppressed|self._ownership_lost|self._releasing
    async def _recovery_sources(self,row,current,tried):
        choices=[]
        for source in (row.get('sources')or[])[:3]:
            try:
                streams=await self._json('stream/'+quote(source['source'],safe='')+'/'+quote(source['id'],safe=''))
                for item in streams:
                    if not isinstance(item,dict)or type(item.get('streamNo'))is not int or item['streamNo']<1:continue
                    url='https://streamed.pk/watch/'+quote(row['id'],safe='')+'/'+quote(source['source'],safe='')+'/'+str(item['streamNo'])
                    if url!=current and url not in tried:
                        choices.append((stream_rank(item),url))
            except (aiohttp.ClientError,asyncio.TimeoutError,ValueError,KeyError,TypeError):continue
        return max(choices,key=lambda c:c[0])[1]if choices else None

    async def _recover_no_video(self,plan):
        # Called under the same lock as layout/stop. One action per minute tick,
        # one refresh and at most two alternate players per match, across restarts.
        live={r['id']:r for r in plan['live']}
        if self.local_keepalive.get('enabled'):return
        for sid,record in list(self._recovery.items()):
            if record.get('pending_release')and sid in self._releasing and sid in self.active_streams:
                stream=self.active_streams[sid]
                if not self._browser_ready.get(stream.browser):continue
                try:
                    audit=await self.command(stream.browser,'inventory')
                    if not isinstance(audit,dict)or self._inventory_errors.get(stream.browser):continue
                    # Ordered audit runs after any timed-out close. An owned tab
                    # means close failed; absence proves release. Neither opens a copy.
                    self._releasing.discard(sid);record['pending_release']=False;record.pop('since',None)
                    if sid not in audit:
                        self.active_streams.pop(sid,None)
                        self._manual_signature=();self._layout_ids=()
                        await self._reconcile(plan)
                        record['message']='Previous close audited; saved source restored';self._save();return
                    record['message']='Previous close audited; waiting for fresh video evidence';self._save()
                except Exception:continue
        for sid in list(self.active_streams):
            now=time.time();record=self._recovery.get(sid)
            if not self._recoverable_failure(sid,now)or sid not in live:
                if record:
                    record.pop('since',None)
                    if self.active_streams[sid].health_monitor.stats.get('state')=='PLAYING':record['message']='Playback restored; remaining budget retained'
                continue
            if record is None:record=self._recovery[sid]={'attempts':0,'tried':[],'created_at':now,'message':'Waiting for video'}
            record.setdefault('since',now)
            if record['attempts']>=3:
                record['message']='Automatic recovery exhausted; choose a source manually';continue
            if now-record['since']<90 or now-record.get('last_action',0)<90:continue
            stream=self.active_streams[sid];stage='audit'
            try:
                # Audit proves the exact owned tab still exists. Exceptions or
                # inventory errors block any destructive command.
                audit=await self.command(stream.browser,'inventory')
                if not isinstance(audit,dict)or sid not in audit or self._inventory_errors.get(stream.browser):raise ValueError('Ownership audit failed')
                if not self._recoverable_failure(sid,time.time()):continue
                if record['attempts']==0:
                    if time.time()-self._reload_times.get(sid,0)<600:continue
                    record.update(attempts=1,last_action=time.time(),since=time.time(),message='One automatic refresh requested')
                    self._save();await self.force_reload(sid)
                else:
                    url=await asyncio.wait_for(self._recovery_sources(live[sid],stream.url,record['tried']),20)
                    if not self._recoverable_failure(sid,time.time()):continue
                    if not url:
                        record.update(last_action=time.time(),message='No alternate source available; waiting');continue
                    # Close and drain inventory before reopening the same canonical
                    # match ID, using the already signed companion's finite commands.
                    record.update(attempts=record['attempts']+1,last_action=time.time(),since=time.time(),pending_release=True,url=url,message='Trying alternate source')
                    record['tried']=list(dict.fromkeys([*record['tried'],stream.url,url]))
                    self._releasing.add(sid);self._save()
                    stage='close';closed=await self.command_for(sid,'close');stage='drain'
                    if closed is not True:raise ValueError('Source close unconfirmed')
                    audit=await self.command(stream.browser,'inventory')
                    if not isinstance(audit,dict)or self._inventory_errors.get(stream.browser)or sid in audit:raise ValueError('Source release unconfirmed')
                    self.active_streams.pop(sid,None);self._releasing.discard(sid);record.update(pending_release=False,url=url,last_action=time.time(),since=time.time());self._save()
                    if sid in self.suppressed or sid in self._ownership_lost:continue
                    for key in list(self._frames):
                        if key[0]==sid:del self._frames[key];self._progress.pop(key,None)
                    await self._open(sid,url,stream.team,stream.browser,stream.session_id)
                    self._manual_signature=();self._layout_ids=()
                    await self._reconcile(plan)
                self._save();return
            except Exception as e:
                if stage=='close'and isinstance(e,ValueError):self._ownership_lost.add(sid) # Native ownership refusal must not reopen a user-closed tab.
                record['message']='Recovery blocked: '+str(e);record['last_action']=time.time();self._save();return
        self._save()

    def _care_video(self,sid):
        primary=self._primary_video(sid)
        if primary:return primary
        rows=[(key,row)for key,row in self._frames.items()if key[0]==sid]
        if rows and all(v[1].get('state')=='NO_VIDEO'for _,v in rows):return max(rows,key=lambda item:item[1][0])
        return None
    def _care_failure(self,sid,now):
        primary=self._care_video(sid)
        stream=self.active_streams.get(sid)
        if not stream or stream.parked or not self._browser_ready.get(stream.browser)or not primary:return False
        rows=[v for (i,_),v in self._frames.items()if i==sid]
        if not rows or not all(0<=now-v[0]<12 and v[1].get('state')in ('NO_VIDEO','BUFFERING')and v[1].get('paused')is not True and not v[1].get('ended')for v in rows):return False
        key,(_,health)=primary;progress=self._progress.get(key)
        return health.get('state')!='BUFFERING'or progress is not None and now-progress>=15
    async def _local_keepalive_tick(self):
        """Care for one exact owned tab, without catalog/server discovery or source changes.

        Fresh progressing primary media resets the burst budget only after five
        continuous minutes. Failures back off through a fifteen minute ceiling;
        exhausted bursts get one probe/reload per thirty minutes until playback
        restores. Offline probes never consume reload attempts or change sources.
        """
        if not self.local_keepalive.get('enabled'):return
        sid=self.local_keepalive.get('stream_id');stream=self.active_streams.get(sid);care=self._local_care;now=time.time()
        if not stream or sid in self.suppressed|self._ownership_lost|self._releasing:
            care.update(state='inactive',message='Selected stream closed or ownership unavailable; no reopen permitted');return
        if not self._browser_ready.get(stream.browser):
            care.pop('healthy_since',None);care.pop('failure_since',None);care.update(state='disconnected',message='Waiting for the local browser companion');return
        primary=self._care_video(sid)
        if not primary or now-primary[1][0]>=12:
            care.pop('healthy_since',None);care.pop('failure_since',None);care.update(state='waiting',message='Waiting for fresh primary video evidence');return
        key,(seen,health)=primary
        progress=self._progress.get(key)
        if health.get('state')=='PLAYING'and progress is not None and now-progress<12:
            care.pop('failure_since',None)
            if now-care.get('last_healthy',now)>12:care.pop('healthy_since',None)
            care.setdefault('healthy_since',now);care['last_healthy']=now
            if now-care['healthy_since']>=300:
                care.update(attempts=0,next_retry=0);self._recovery.pop(sid,None)
            care.update(state='healthy',message='Selected stream is progressing; local keepalive active');self._save();return
        care.pop('healthy_since',None)
        if not self._care_failure(sid,now):
            care.pop('failure_since',None);care.update(state='waiting',message='Playback paused, loading, or not proven failed; no automatic action');return
        care.setdefault('failure_since',now)
        threshold=120 if self.local_keepalive.get('profile')=='travel'else 90
        if now-care['failure_since']<threshold or now<care.get('next_retry',0):
            care.update(state='cooldown',message='Sustained failure observation or recovery cooldown');return
        if now-self._reload_times.get(sid,0)<600:return
        care.update(state='checking',message='Checking network before recovery')
        try:
            if not self._client:raise ValueError('Local network client unavailable')
            async with self._client.get(stream.url,timeout=aiohttp.ClientTimeout(total=8))as response:
                if not 200<=response.status<400:raise ValueError('Selected stream site unavailable')
        except (aiohttp.ClientError,asyncio.TimeoutError,ValueError):
            delay=min(900,max(60,care.get('network_delay',30)*2));care.update(network_delay=delay,next_retry=now+delay,state='network-unavailable',message='Network or stream site unavailable; tab and exact source preserved');self._save();return
        # A user close or pause during the network request still wins.
        if self.active_streams.get(sid)is not stream or sid in self.suppressed|self._ownership_lost|self._releasing:return
        if not self._care_failure(sid,time.time()):return
        try:
            audit=await self.command(stream.browser,'inventory')
            if not isinstance(audit,dict)or sid not in audit or self._inventory_errors.get(stream.browser):raise ValueError('Exact ownership audit unavailable')
            if self.active_streams.get(sid)is not stream or sid in self.suppressed|self._ownership_lost|self._releasing:return
            if not self._care_failure(sid,time.time()):return
            attempts=min(3,care.get('attempts',0)+1);delay=1800 if attempts>=3 else (600 if attempts==1 else 900)
            care.update(attempts=attempts,last_action=time.time(),next_retry=time.time()+delay,state='recovering',message='Refreshing the selected source; audio and user pause remain authoritative',network_delay=30)
            self._save();await self.force_reload(sid)
        except (ValueError,TimeoutError):
            care.update(next_retry=time.time()+120,state='blocked',message='Exact owned tab could not be confirmed; no replacement opened');self._save()

    async def _loop(self):
        while True:
            await asyncio.sleep(10)
            async with self._lock:
                # Drop evidence only for departed streams; active stale frames still block recovery.
                for key in [k for k in self._frames if k[0]not in self.active_streams]:del self._frames[key]
                for key in [k for k in self._progress if k[0]not in self.active_streams]:del self._progress[key]
                for sid,s in self.active_streams.items():
                    rows=[v for (i,_),v in self._frames.items() if i==sid]
                    if rows and time.time()-max(v[0] for v in rows)>15:s.health_monitor.stats['state']='STALE'
                try:await self._local_keepalive_tick()
                except Exception:
                    log.exception('Local keepalive deferred; selected tab preserved')
                if not self.session or self.local_keepalive.get('enabled'):continue
                if time.time()-getattr(self,'_last_catalog_poll',0)<60:continue
                self._last_catalog_poll=time.time()
                if time.time()*1000>self.session['until_ms']:
                    self.session=None;self._save();continue # leave playing games alone
                try:
                    plan=await self.discover(self.session['spec'],self.session['until_ms'])
                    self.plan=plan;await self._reconcile(plan);await self._recover_no_video(plan)
                except Exception as e:
                    log.exception('Session poll failed; preserving playing windows')
                    self.errors=['Catalog unavailable; existing streams preserved: '+type(e).__name__]
                self.errors=[e for e in self.errors if not e.startswith('TV focus repair deferred:')]
                try:await self._maintain_tv_focus()
                except Exception as e:
                    log.exception('TV focus repair deferred; preserving games')
                    self.errors.append('TV focus repair deferred: '+str(e))
