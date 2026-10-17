"""Read connected, enabled desktop outputs. Configuration never proves connectivity.

KScreen reports logical Wayland coordinates; xrandr reports X11 root coordinates.
Never use XWayland's synthetic screen as a substitute for a failed Wayland probe.
Logical display names bind by explicit output connector, internal-panel identity,
or an unambiguous exact geometry match for existing configurations.
"""
import asyncio
import json
import math
import stat
from pathlib import Path
import os
import re
import shutil

KEYS=('x','y','width','height')

def kscreen_outputs(value):
    rows=value.get('outputs') if isinstance(value,dict) else None
    if not isinstance(rows,list):raise ValueError('Invalid KScreen outputs')
    result=[]
    for row in rows:
        if not isinstance(row,dict) or not isinstance(row.get('name'),str):continue
        connected=row.get('connected') is True;enabled=row.get('enabled') is True
        output={'name':row['name'],'connected':connected,'enabled':enabled}
        if connected and enabled:
            pos=row.get('pos',{});size=row.get('size',{})
            # KScreen size is logical desktop size (mode size divided by scale).
            if size:
                scale=row.get('scale',1)
                if type(scale)not in(int,float)or not math.isfinite(scale)or scale<=0:raise ValueError('Invalid KScreen scale')
                size={k:math.ceil(size.get(k,0)/scale)for k in ('width','height')}
            else:
                mode=next((m for m in row.get('modes',[]) if m.get('id')==row.get('currentModeId')),None)
                if not mode:raise ValueError('KScreen active output has no mode')
                scale=row.get('scale',1)
                if type(scale)not in(int,float)or scale<=0:raise ValueError('Invalid KScreen scale')
                size={k:math.ceil(mode.get('size',{}).get(k,0)/scale) for k in ('width','height')}
                if row.get('rotation') in (2,8):size={'width':size['height'],'height':size['width']}
            geometry={'x':pos.get('x'),'y':pos.get('y'),'width':size.get('width'),'height':size.get('height')}
            if any(type(geometry[k])is not int for k in KEYS)or geometry['width']<=0 or geometry['height']<=0:raise ValueError('Invalid KScreen geometry')
            output.update(geometry)
        result.append(output)
    return result

def xrandr_outputs(value):
    result=[]
    for line in value.splitlines():
        match=re.match(r'^(\S+) (connected|disconnected)\b(.*)$',line)
        if not match:continue
        name,state,tail=match.groups();geometry=re.search(r'\b(\d+)x(\d+)([+-]\d+)([+-]\d+)',tail)
        output={'name':name,'connected':state=='connected','enabled':bool(geometry)and state=='connected'}
        if output['enabled']:
            width,height,x,y=map(int,geometry.groups());output.update(x=x,y=y,width=width,height=height)
        result.append(output)
    if not result:raise ValueError('No XRandR outputs reported')
    return result

def bind_displays(configured,outputs):
    displays={};availability={};used=set()
    for name,region in configured.items():
        connector=region.get('output')
        candidates=[o for o in outputs if o['name']==connector] if connector else []
        if not connector and name=='laptop':candidates=[o for o in outputs if re.match(r'^(eDP|LVDS|DSI)(-|\d|$)',o['name'],re.I)]
        if not candidates and not connector:
            candidates=[o for o in outputs if o.get('enabled') and all(o.get(k)==region.get(k)for k in KEYS)]
        if len(candidates)!=1:
            availability[name]='unknown';continue
        output=candidates[0]
        if not output.get('connected')or not output.get('enabled'):
            availability[name]='disconnected';continue
        if output['name'] in used:
            availability[name]='unknown';continue
        used.add(output['name']);availability[name]='connected';displays[name]={**region,**{k:output[k]for k in KEYS},'output':output['name']}
    return displays,availability

async def probe_displays(configured,preferred_backend=None):
    backend='kscreen' if preferred_backend=='kwin'or os.environ.get('WAYLAND_DISPLAY')or os.environ.get('XDG_SESSION_TYPE')=='wayland' else 'xrandr'
    command=shutil.which('kscreen-doctor' if backend=='kscreen' else 'xrandr')
    try:
        if not command:raise ValueError('Display probe command unavailable')
        env=dict(os.environ)
        if backend=='kscreen':
            runtime=Path(env.get('XDG_RUNTIME_DIR',f'/run/user/{os.getuid()}'))
            if runtime.is_dir()and runtime.stat().st_uid==os.getuid():
                sockets=[p for p in runtime.glob('wayland-*')if p.stat().st_uid==os.getuid()and stat.S_ISSOCK(p.stat().st_mode)]
                current=runtime/env.get('WAYLAND_DISPLAY','wayland-0')
                if current not in sockets:
                    if len(sockets)!=1:raise ValueError('Active Wayland socket unavailable or ambiguous')
                    env['WAYLAND_DISPLAY']=sockets[0].name
                env['XDG_RUNTIME_DIR']=str(runtime);env['QT_QPA_PLATFORM']='wayland'
        proc=await asyncio.create_subprocess_exec(command,'-j' if backend=='kscreen' else '--query',stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,env=env)
        try:stdout,stderr=await asyncio.wait_for(proc.communicate(),4)
        except BaseException:
            proc.kill();await proc.wait();raise
        if proc.returncode:raise ValueError('Display probe failed')
        outputs=kscreen_outputs(json.loads(stdout))if backend=='kscreen' else xrandr_outputs(stdout.decode())
        displays,availability=bind_displays(configured,outputs)
        return {'state':'available','backend':backend,'message':'Connected desktop outputs detected','outputs':outputs,'displays':displays,'availability':availability}
    except (OSError,ValueError,TypeError,asyncio.TimeoutError):
        return {'state':'unknown','backend':backend,'message':'Connected displays could not be verified; window placement paused','outputs':[],'displays':{},'availability':{name:'unknown'for name in configured}}
