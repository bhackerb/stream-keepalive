"""Regular-browser control through an authenticated companion, without launching profiles.

Health is report-only. Health polls cannot refresh, switch, or restart a game.
The session reconciler opens only new match IDs, and only retires explicitly ended
media with decoded-frame evidence. Catalog disappearance alone never ends a game.
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
from session_planner import build_plan, validate_spec, reserve_sports

log=logging.getLogger('stream-keeper.regular')
ALLOWED_HOSTS=('streamed.pk','streamed.su','embed.st','rockylive.st','strmd.top')
def validate_url(url):
    try:u=urlparse(url)
    except Exception:raise ValueError('Invalid stream URL')
    if u.scheme!='https' or not u.hostname or u.username or u.password or not any(u.hostname==h or u.hostname.endswith('.'+h) for h in ALLOWED_HOSTS):
        raise ValueError('Regular mode supports HTTPS Streamed/player hosts only')
    return url

class CompanionPage:
    """Only exact layout title operations; no transport of arbitrary JavaScript."""
    def __init__(self,keeper,sid):self.keeper,self.sid=keeper,sid
    async def title(self):return await self.keeper.command_for(self.sid,'title')
    async def evaluate(self,source,value):
        if source not in ('marker => document.title = marker','title => document.title = title'):
            raise ValueError('Unsupported companion page operation')
        return await self.keeper.command_for(self.sid,'marker',value=value)

class RegularBrowserKeeper:
    def __init__(self,config):
        self.config=config;self.browser_engine=config.get('browser',{}).get('engine','firefox')
        self.max_streams=config.get('session',{}).get('max_open_streams',max(20,config.get('streams',{}).get('max_streams',10)))
        self.default_site='streamed';self.active_streams={};self.connections={};self.pending={}
        self.layout_status={'success':False,'message':'No companion connected'}
        self.ad_handler=SimpleNamespace(stats={'requests_blocked':0,'overlays_dismissed':0,'network_enabled':False,'network_verified':False,'extension_verified':False,'note':'Regular browser uses its own blockers. Installation and live blocking must be verified in that browser.'})
        self.state_path=Path(config.get('session',{}).get('state_file','regular-session.json'))
        self.session=None;self.plan=None;self.suppressed=set();self.errors=[];self._task=None;self._lock=asyncio.Lock();self._frames={};self._layout_ids=();self._client=None;self._reload_times={};self._browser_ready={};self._end_seen={};self._ownership_lost=set();self._slot_rows={};self._visible_ids=()
        self._load()
    @property
    def is_watching(self):return bool(self.active_streams)
    def _load(self):
        if self.state_path.exists():
            try:
                state=json.loads(self.state_path.read_text());self.session=state.get('session');self.suppressed=set(state.get('suppressed',[]));self._ownership_lost=set(state.get('ownership_lost',[]));self._slot_rows=state.get('slot_rows',{});self._layout_ids=tuple(state.get('layout_ids',[]));self._visible_ids=tuple(state.get('visible_ids',[]))
                for sid,r in state.get('streams',{}).items():self._add(sid,r)
            except (ValueError,TypeError,KeyError):log.warning('Invalid regular browser state; ignoring')
    def _save(self):
        value={'session':self.session,'suppressed':sorted(self.suppressed),'ownership_lost':sorted(self._ownership_lost),'slot_rows':self._slot_rows,'layout_ids':list(self._layout_ids),'visible_ids':list(self._visible_ids),'streams':{sid:{'url':s.url,'label':s.team,'browser':s.browser,'started_at':s.started_at,'session_id':s.session_id,'parked':s.parked}for sid,s in self.active_streams.items()}}
        self.state_path.parent.mkdir(parents=True,exist_ok=True);temp=self.state_path.with_suffix('.tmp')
        fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'w')as f:
            os.fchmod(f.fileno(),0o600);f.write(json.dumps(value,allow_nan=False))
        temp.replace(self.state_path)
    def _add(self,sid,r):
        health={'state':'DISCONNECTED','resolution':'unknown','current_time':'0','buffered_to':'unknown','audio':'unknown','muted':True,'volume':0,'stall_duration':'0','recovery_count':0}
        self.active_streams[sid]=SimpleNamespace(id=sid,team=r.get('label',sid),site='streamed',url=r['url'],browser=r['browser'],started_at=r.get('started_at',time.time()),session_id=r.get('session_id'),parked=r.get('parked',False),health_monitor=SimpleNamespace(stats=health),page=CompanionPage(self,sid))
    async def start_browser(self):
        self._client=aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12))
        self._task=asyncio.create_task(self._loop())
    async def shutdown(self):
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
                        self.connections[browser]=ws;self._browser_ready[browser]=False
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
                    self.active_streams.pop(sid,None);self._ownership_lost.add(sid)
            for sid in m.get('lost',[]):
                if isinstance(sid,str):self._ownership_lost.add(sid)
            self._browser_ready[browser]=True;self._save()
        elif m.get('event')=='ownership_lost':
            sid=m.get('stream_id');s=self.active_streams.get(sid)
            if isinstance(sid,str):
                self._ownership_lost.add(sid)
                if s and s.browser==browser:self.active_streams.pop(sid,None)
                self._save()
        elif m.get('event')=='closed':
            sid=m.get('stream_id');s=self.active_streams.get(sid)
            if s and s.browser==browser:self.active_streams.pop(sid,None);self.suppressed.add(sid);self._save()
        elif m.get('event')=='health':
            sid=m.get('stream_id');s=self.active_streams.get(sid);h=m.get('health')
            if not s or s.browser!=browser or not isinstance(h,dict):return
            if any(type(h.get(k,0))not in(int,float)or not math.isfinite(h.get(k,0))for k in('width','height','current_time','volume','stall_seconds')):return
            key=(sid,m.get('frame_id'));self._frames[key]=(time.time(),h)
            rows=[v for (i,_),v in self._frames.items() if i==sid and time.time()-v[0]<12]
            best=max(rows,key=lambda v:(v[1].get('state')=='PLAYING',v[1].get('width',0)*v[1].get('height',0)))[1]
            s.health_monitor.stats.update(state=best.get('state','UNKNOWN'),resolution=f"{best.get('width',0)}x{best.get('height',0)}",current_time=str(round(best.get('current_time',0),2)),muted=bool(best.get('muted',True)),volume=best.get('volume',0),stall_duration=str(round(best.get('stall_seconds',0),1)))
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
        await self.command(browser,'open',stream_id=sid,url=url,label=label,session_id=session_id,start_muted=self.config.get('session',{}).get('start_muted',True),allow_replacement=allow_replacement)
        self._add(sid,dict(url=url,label=label,browser=browser,session_id=session_id));self._save()
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
                    self.active_streams.pop(target,None)
            self._save()
        return ('Tracking stopped; could not close '+', '.join(failed))if failed else 'Stopped selected StreamKeeper-owned windows'
    async def force_reload(self,sid=None):
        targets=[sid] if sid else list(self.active_streams)
        for target in targets:
            if time.time()-self._reload_times.get(target,0)<600:return '❌ Reload budget: at most once per stream per 10 minutes'
            await self.command_for(target,'reload');self._reload_times[target]=time.time()
        return 'Explicit reload requested'
    async def force_unmute(self,sid=None):
        for target in ([sid] if sid else list(self.active_streams)):await self.command_for(target,'unmute')
        return 'Unmute requested'
    async def switch_source(self,sid=None):return '❌ Select another source explicitly; automatic source switching is disabled'
    async def take_screenshot(self,sid=None):return None
    async def get_status(self,sid=None):return json.dumps(self.extra_status())
    async def set_tab_view(self,sid,enabled):
        for target in ([sid] if sid else list(self.active_streams)):await self.command_for(target,'fill',enabled=enabled)
        return {'success':True,'message':'Tab fill setting updated'}
    async def arrange_streams(self,display,mode,ids=None):
        try:
            placements=plan_layout(self.config.get('layout',{}).get('displays',{}),display,mode,ids or list(self.active_streams))
            if any(p['stream_id'] not in self.active_streams for p in placements):raise ValueError('Unknown stream')
            if self.config.get('layout',{}).get('backend')=='kwin':
                from kwin_layout import arrange_windows
                result=await arrange_windows(self.active_streams,placements)
            else:
                from x11_windows import window_for_page,move_window
                # Identify every window before moving any; never move a shared personal window.
                windows=[await window_for_page(self.active_streams[p['stream_id']].page)for p in placements]
                if len(set(windows))!=len(windows):raise ValueError('Streams must occupy separate windows')
                for wid,p in zip(windows,placements):await asyncio.to_thread(move_window,wid,p)
                result={'success':True,'message':'X11 placement requested (no geometry readback)'}
            self.layout_status={**result,'placements':placements};return self.layout_status
        except (ValueError,TimeoutError) as e:
            self.layout_status={'success':False,'message':str(e)};return self.layout_status
    def extra_status(self):
        return {'control_mode':'regular','browser_connections':{b:bool(b in self.connections and self._browser_ready.get(b))for b in ('firefox','chromium')},'session':{'active':bool(self.session),'plan':self.plan,'errors':self.errors,'suppressed':sorted(self.suppressed)}}
    async def _json(self,path):
        async with self._client.get('https://streamed.pk/api/'+path) as res:
            res.raise_for_status();value=await res.json()
            if not isinstance(value,list):raise ValueError('Invalid Streamed catalog response')
            return value
    async def discover(self,spec,until_ms=None):
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
                source,s=max(candidates,key=lambda v:(v[1].get('language')=='English',(v[1].get('viewers') if type(v[1].get('viewers')) in(int,float) else 0),bool(v[1].get('hd'))))
                playback[match['id']]='https://streamed.pk/watch/'+quote(match['id'],safe='')+'/'+quote(source,safe='')+'/'+str(s['streamNo'])
        live_ids={m['id']for m in live if isinstance(m,dict)and isinstance(m.get('id'),str)}
        try:await asyncio.wait_for(asyncio.gather(*(measure(m)for m in selected if m['id'] in live_ids)),20)
        except asyncio.TimeoutError:pass # Partial viewer evidence; still schedule later starts.
        plan=build_plan(list(merged.values()),live_ids,viewers,spec,self.config.get('layout',{}).get('displays',{}),int(time.time()*1000),until_ms)
        for row in plan['live']:row['playback_url']=playback.get(row['id'])
        return plan
    async def session_action(self,body):
        action=body.get('action','plan')
        if action=='status':return {'success':True,**self.extra_status()['session']}
        if action=='stop':
            failed=[]
            async with self._lock:
                current=self.session['id'] if self.session else None
                self.session=None;self._layout_ids=();self._visible_ids=();self._slot_rows={};self._save()
                for sid,s in list(self.active_streams.items()):
                    if s.session_id and (not current or s.session_id==current):
                        self.suppressed.add(sid)
                        try:await self.command_for(sid,'close')
                        except Exception as e:
                            log.warning('Stream close unavailable: %s',type(e).__name__);failed.append(sid);continue
                        self.active_streams.pop(sid,None)
                self._save()
            return {'success':True,'message':'Session tracking stopped; other owned streams preserved','errors':['Could not close '+sid for sid in failed]}
        if action not in ('start','plan'):raise ValueError('action must be plan, start, status, or stop')
        spec=validate_spec(body);plan=await self.discover(spec)
        if action=='plan':return {'success':True,'plan':plan}
        async with self._lock:
            if self.session:raise ValueError('A session is already active; stop it before changing sports')
            self.session={'id':uuid.uuid4().hex,'spec':spec,'until_ms':plan['until_ms']};self.plan=plan;self.suppressed.clear();self._ownership_lost.clear();self._layout_ids=();self._visible_ids=();self._slot_rows={};self._save()
            await self._reconcile(plan,allow_replacements=True)
        return {'success':True,'message':'Session scheduled; inspect browser connections and playback status','plan':plan,'errors':self.errors}
    async def _reconcile(self,plan,allow_replacements=False):
        if not self.session:return
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
        # Retire only a session-owned stream with explicit video end and prior progress.
        for sid,s in list(self.active_streams.items()):
            rows=[v for (i,_),v in self._frames.items()if i==sid and time.time()-v[0]<12 and v[1].get('width',0)>0]
            if s.session_id==self.session['id'] and sid not in {r['id']for r in plan['live']} and rows and all(v[1].get('ended') is True for v in rows):
                self._end_seen.setdefault(sid,time.time())
                if time.time()-self._end_seen[sid]<60:continue
                try:await self.command_for(sid,'close')
                except (ValueError,TimeoutError):continue
                self.active_streams.pop(sid,None);self.suppressed.add(sid)
            else:self._end_seen.pop(sid,None)
        selected=[r for r in selected if r['id']not in self.suppressed]
        # No catalog-based close. Running games survive source/API outages.
        selected_ids={r['id']for r in selected}
        for sid,s in list(self.active_streams.items()):
            if s.session_id==self.session['id']:
                park=sid not in selected_ids
                if park!=s.parked:
                    try:await self.command_for(sid,'park',enabled=park);s.parked=park
                    except (ValueError,TimeoutError)as e:self.errors.append(str(e));return
        available=max(0,self.max_streams-len(self.active_streams))
        if self._ownership_lost:self.errors.append('Ownership lost; existing windows left alone. Restart the session to open replacements.')
        for row in selected:
            if row['id'] in self.active_streams:continue
            if available<=0:self.errors.append('All available stream slots are occupied');break
            sources=row['sources']
            if not sources:self.errors.append(row['title']+': source not yet available');continue
            source=sources[0]
            try:
                url=row.get('playback_url') or 'https://streamed.pk/watch/'+quote(row['id'],safe='')+'/'+quote(source['source'],safe='')+'/1'
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
            else:self.errors.append(result['message'])
        self._save()
    async def _loop(self):
        while True:
            await asyncio.sleep(60)
            async with self._lock:
                for sid,s in self.active_streams.items():
                    rows=[v for (i,_),v in self._frames.items() if i==sid]
                    if rows and time.time()-max(v[0] for v in rows)>15:s.health_monitor.stats['state']='STALE'
                if not self.session:continue
                if time.time()*1000>self.session['until_ms']:
                    self.session=None;self._save();continue # leave playing games alone
                try:
                    plan=await self.discover(self.session['spec'],self.session['until_ms'])
                    self.plan=plan;await self._reconcile(plan)
                except Exception as e:
                    log.exception('Session poll failed; preserving playing windows')
                    self.errors=['Catalog unavailable; existing streams preserved: '+type(e).__name__]
