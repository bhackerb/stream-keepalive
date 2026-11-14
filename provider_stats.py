"""Provider and embed-host performance report built from StreamKeeper's local evidence.

Joins playback-reliability.json (confirmed play/buffer time), buffer-watch.jsonl (buffering episodes and
auto-switches), autoplay-outcomes.jsonl (did the embed start?) and provider-health.json (catalog polls).
Read-only: it never opens or closes a player. Run daily by streamkeeper-provider-report.timer, and served
live at GET /provider-stats so source choice can consult it.
"""
import json
import time
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
WINDOW_DAYS = 14


def _jsonl(path, since):
    try:
        lines = Path(path).read_text().splitlines()[-20000:]
    except OSError:
        return []
    rows = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and isinstance(row.get('at'), (int, float)) and row['at'] >= since:
            rows.append(row)
    return rows


def _json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}


def build_report(directory=HERE, now=None):
    d, now = Path(directory), now or time.time()
    since = now - WINDOW_DAYS * 86400
    play = defaultdict(lambda: dict(play=0.0, buf=0.0, longest=0.0, starts=0, start_s=0.0))
    for row in (_json(d / 'playback-reliability.json').get('rows') or {}).values():
        if not isinstance(row, dict) or row.get('at', 0) < since:
            continue
        p = play[(row.get('provider'), row.get('sport'))]
        p['play'] += row.get('playing', 0)
        p['buf'] += row.get('buffering', 0)
        p['longest'] = max(p['longest'], row.get('longest_run_seconds', 0))
        p['starts'] += row.get('startup_successes', 0)
        p['start_s'] += row.get('startup_seconds', 0)
    watch = defaultdict(Counter)
    for row in _jsonl(d / 'buffer-watch.jsonl', since):
        watch[row.get('provider')][row.get('event')] += 1
    hosts = defaultdict(Counter)
    for row in _jsonl(d / 'autoplay-outcomes.jsonl', since):
        if isinstance(row.get('site'), str):
            hosts[row['site'].removeprefix('www.')][row.get('outcome')] += 1

    providers = []
    for (name, sport), p in play.items():
        total = p['play'] + p['buf']
        providers.append(dict(
            provider=name, sport=sport, play_min=round(p['play'] / 60, 1), buffer_min=round(p['buf'] / 60, 1),
            buffer_pct=round(100 * p['buf'] / total, 2) if total else None, longest_run_min=round(p['longest'] / 60),
            startups=p['starts'], mean_startup_s=round(p['start_s'] / p['starts'], 1) if p['starts'] else None,
            buffering_episodes=watch[name]['buffering'], auto_switches=watch[name]['switched'],
            confidence='high' if total >= 3600 else 'low' if total >= 900 else 'insufficient'))
    providers.sort(key=lambda r: -(r['play_min'] + r['buffer_min']))

    host_rows = []
    for host, c in hosts.items():
        n = sum(c.values())
        host_rows.append(dict(host=host, steps=n, advanced_pct=round(100 * c['advanced'] / n), stalled_pct=round(100 * c['stalled'] / n),
                              verdict='failing' if n >= 10 and c['advanced'] / n < .1 else 'ok' if n >= 10 else 'too few steps'))
    host_rows.sort(key=lambda r: -r['steps'])

    health = _json(d / 'provider-health.json').get('polls') or {}
    catalog = {}
    for name, rows in health.items():
        ok = [r for r in rows if r.get('ok')]
        catalog[name] = dict(polls=len(rows), ok_rate=round(len(ok) / len(rows), 3) if rows else None,
                             avg_coverage=round(sum(r.get('coverage') or 0 for r in ok) / len(ok), 3) if ok else None)
    return dict(generated_at=int(now), window_days=WINDOW_DAYS, providers=providers, hosts=host_rows, catalog=catalog,
                demoted_hosts=[h['host'] for h in host_rows if h['verdict'] == 'failing'],
                notes='Buffering counts only confirmed network buffering; low-confidence rows have under 15 minutes of data.')


def markdown(report):
    out = [f"# StreamKeeper provider report ({time.strftime('%Y-%m-%d %H:%M', time.localtime(report['generated_at']))})", '',
           f"Window: {report['window_days']} days. {report['notes']}", '', '## Playback by provider and sport', '',
           '| Provider | Sport | Play min | Buffer % | Longest run | Startup s | Buffer episodes | Switches | Confidence |', '|---|---|---|---|---|---|---|---|---|']
    for r in report['providers']:
        out.append(f"| {r['provider']} | {r['sport']} | {r['play_min']} | {r['buffer_pct']} | {r['longest_run_min']} | {r['mean_startup_s']} | {r['buffering_episodes']} | {r['auto_switches']} | {r['confidence']} |")
    out += ['', '## Embed hosts (autoplay)', '', '| Host | Steps | Advanced % | Stalled % | Verdict |', '|---|---|---|---|---|']
    out += [f"| {h['host']} | {h['steps']} | {h['advanced_pct']} | {h['stalled_pct']} | {h['verdict']} |" for h in report['hosts']]
    out += ['', 'Demoted in ranking: ' + (', '.join(report['demoted_hosts']) or 'none'), '', '## Catalog coverage', '',
            '| Provider | Polls | OK rate | Avg coverage |', '|---|---|---|---|']
    out += [f"| {k} | {v['polls']} | {v['ok_rate']} | {v['avg_coverage']} |" for k, v in sorted(report['catalog'].items())]
    return '\n'.join(out) + '\n'


def main():
    report = build_report()
    (HERE / 'provider-stats.json').write_text(json.dumps(report, indent=1))
    (HERE / 'PROVIDER_REPORT.md').write_text(markdown(report))
    print(markdown(report))


if __name__ == '__main__':
    main()
