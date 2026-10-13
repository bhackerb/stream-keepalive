"""Pure match ranking and schedule planning. No browser, network, or wall-clock reads."""
import math
from datetime import datetime
from zoneinfo import ZoneInfo
from display_layout import CAPACITIES, plan_layout

SPORTS = {'hockey', 'baseball', 'basketball', 'football', 'american-football', 'rugby', 'tennis', 'cricket', 'motor-sports', 'fight', 'other'}


def validate_spec(spec):
    if not isinstance(spec, dict):
        raise ValueError('Session must be an object')
    sports = spec.get('sports', ['hockey'])
    if not isinstance(sports, list) or not sports or any(s not in SPORTS for s in sports):
        raise ValueError('sports must be a nonempty list of supported Streamed categories')
    browser = spec.get('browser', 'firefox')
    if browser not in ('firefox', 'chromium'):
        raise ValueError('browser must be firefox or chromium')
    display = spec.get('display', 'dock')
    if display not in ('dock', 'tv', 'widescreen', 'vertical'):
        raise ValueError('Unknown display')
    hours = spec.get('hours', 12)
    if type(hours) is not int or not 1 <= hours <= 24:
        raise ValueError('hours must be an integer from 1 to 24')
    preload=spec.get('preload_minutes',10)
    if type(preload)is not int or not 0<=preload<=60:raise ValueError('preload_minutes must be an integer from 0 to 60')
    favorites = spec.get('favorites', [])
    if not isinstance(favorites, list) or any(not isinstance(f, str) or not f.strip() or len(f) > 120 for f in favorites):
        raise ValueError('favorites must be team names')
    return dict(sports=list(dict.fromkeys(sports)), browser=browser, display=display,
                hours=hours, preload_minutes=preload, favorites=favorites, timezone='America/Denver')


def favorite_match(match, favorites):
    # Match complete team names, including punctuation-insensitive St. Louis.
    def words(value):
        return ' '.join(''.join(c if c.isalnum() else ' ' for c in value.lower()).split())
    names = [t.get('name', '') for t in (match.get('teams') or {}).values() if isinstance(t, dict)]
    title = ' ' + words(match.get('title', '')) + ' '
    return any(words(f) in [words(n) for n in names] or (' ' + words(f) + ' ') in title for f in favorites)


def reserve_sports(selected, ranked, sports, capacity):
    """Keep at least one live game of each requested sport when slots permit."""
    selected=list(selected)
    for sport in sports[:capacity]:
        if any(r.get('sport')==sport for r in selected):continue
        candidate=next((r for r in ranked if r.get('sport')==sport),None)
        if not candidate:continue
        if len(selected)<capacity:selected.append(candidate);continue
        counts={r.get('sport'):sum(s.get('sport')==r.get('sport')for s in selected)for r in selected}
        removable=[r for r in reversed(selected)if counts[r.get('sport')]>1]
        if not removable:continue
        victim=next((r for r in removable if not r['favorite']),removable[0])
        selected.remove(victim);selected.append(candidate)
    return sorted(selected,key=lambda r:(not r['favorite'],-(r['viewers']if r['viewers']is not None else -1),r['start_ms'],r['id']))


def build_plan(matches, live_ids, viewers, spec, displays, now_ms, until_ms=None):
    spec = validate_spec(spec)
    end = until_ms if until_ms is not None else now_ms + spec['hours'] * 3600000
    names = [n for n in ('widescreen', 'laptop', 'vertical') if n!='laptop' or n in displays] if spec['display'] == 'dock' else [spec['display']]
    capacity = sum(min(CAPACITIES[n],displays.get(n,{}).get("max_streams",CAPACITIES[n])) for n in names)
    window_start = end - spec['hours'] * 3600000 if until_ms is not None else now_ms
    rows = []
    seen = set()
    for match in matches:
        mid, start = match.get('id'), match.get('date')
        if not isinstance(mid, str) or mid in seen or match.get('category') not in spec['sports']:
            continue
        # date=0 is a channel, not a game. Old missing games are not inferred live.
        if type(start) not in (int, float) or not math.isfinite(start) or not 0 < start < 253402300799000:
            continue
        live = mid in live_ids
        if not live and not window_start <= start <= end:
            continue
        seen.add(mid)
        count = viewers.get(mid)
        count = count if type(count) in (int, float) and math.isfinite(count) and count >= 0 else None
        rows.append(dict(id=mid, title=match.get('title', mid), sport=match['category'],
                         start_ms=start, start_local=datetime.fromtimestamp(start / 1000, ZoneInfo(spec['timezone'])).isoformat(),
                         favorite=favorite_match(match, spec['favorites']), viewers=count,
                         live=live, sources=match.get('sources') or []))
    live = sorted((r for r in rows if r['live']), key=lambda r: (not r['favorite'], -(r['viewers'] if r['viewers'] is not None else -1), r['start_ms'], r['id']))
    pending = sorted((r for r in rows if not r['live']), key=lambda r: (r['start_ms'], not r['favorite'], r['id']))
    selected = reserve_sports(live[:capacity],live,spec['sports'],capacity)
    placements = plan_layout(displays, spec['display'], 'grid', [r['id'] for r in selected])
    return dict(spec=spec, capacity=capacity, live=live, selected=selected, pending=pending,
                overflow=[r for r in live if r['id'] not in {s['id']for s in selected}], placements=placements, until_ms=end,
                viewer_metric='Maximum reported source viewers per game; not unique viewers',
                generated_ms=now_ms)
