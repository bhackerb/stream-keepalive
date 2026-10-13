"""Validated, fixed display slots for manual game placement and future reservations."""
import re
from display_layout import CAPACITIES,plan_layout
PRESETS={'tv':(1,2,4),'widescreen':(1,2,4,6),'vertical':(1,2,3,4),'laptop':(1,2)}
def validate_manual_layout(body,displays):
    if not isinstance(body,dict):raise ValueError('Expected layout object')
    presets=body.get('presets',{})
    assignments=body.get('assignments',[])
    if not isinstance(presets,dict)or not isinstance(assignments,list)or len(assignments)>14:raise ValueError('Invalid manual layout')
    for name,count in presets.items():
        if name not in displays or name not in PRESETS or type(count)is not int or count not in PRESETS[name]or count>displays[name].get('max_streams',CAPACITIES[name]):raise ValueError('Unknown display or invalid grid size')
        plan_layout(displays,name,'grid',[str(i)for i in range(count)])
    result=[];games=set();slots=set()
    for a in assignments:
        if not isinstance(a,dict):raise ValueError('Invalid game placement')
        gid,name,slot=a.get('game_id'),a.get('display'),a.get('slot')
        if not isinstance(gid,str)or not re.fullmatch(r'[A-Za-z0-9_-]{1,180}',gid):raise ValueError('Invalid game ID')
        if name not in presets or type(slot)is not int or not 0<=slot<presets[name]:raise ValueError('Invalid display slot')
        if gid in games or(name,slot)in slots:raise ValueError('Duplicate game or occupied slot')
        games.add(gid);slots.add((name,slot));result.append(dict(game_id=gid,display=name,slot=slot))
    return dict(presets=presets,assignments=result)

def manual_placements(layout,displays,active_ids):
    grids={name:plan_layout(displays,name,'grid',[str(i)for i in range(count)])for name,count in layout['presets'].items()}
    return [{**grids[a['display']][a['slot']],'stream_id':a['game_id']}for a in layout['assignments']if a['game_id']in active_ids]
