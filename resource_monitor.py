"""Bounded Linux resource snapshots; never collect page URLs or command lines."""
import os
import time
from pathlib import Path

GIB = 1024 ** 3


def fields(path):
    result = {}
    for line in path.read_text().splitlines():
        key, separator, value = line.partition(':')
        parts = value.split()
        # /proc/status also contains strings and empty architecture fields.
        if separator and len(parts) == 2 and parts[1] == 'kB' and parts[0].isdigit():
            result[key] = int(parts[0]) * 1024
    return result


class ResourceMonitor:
    def __init__(self, proc=Path('/proc'), clock=time.monotonic):
        self.proc = Path(proc)
        self.clock = clock
        self._previous = {}
        self._last = None
        self.snapshot = {'state': 'unknown', 'message': 'Resource sample pending'}

    def sample(self):
        now = self.clock()
        try:
            mem = fields(self.proc / 'meminfo')
            total, available = mem['MemTotal'], mem['MemAvailable']
            if total <= 0 or not 0 <= available <= total:
                raise ValueError('Invalid memory sample')
        except (OSError, ValueError, KeyError, IndexError):
            self.snapshot = {'state': 'unavailable', 'message': 'Linux memory diagnostics unavailable'}
            return self.snapshot
        pressure = {}
        try:
            for line in (self.proc / 'pressure/memory').read_text().splitlines():
                parts = line.split()
                pressure[parts[0]] = float(dict(p.split('=') for p in parts[1:])['avg10'])
        except (OSError, ValueError, KeyError):
            pass  # PSI is optional on older kernels; memory checks remain available.
        browsers = {name: {'rss_bytes': 0, 'swap_bytes': 0, 'cpu_percent': 0,
                           'processes': 0, 'largest_processes': []} for name in ('firefox', 'chromium')}
        previous = {}
        ticks_per_second = os.sysconf('SC_CLK_TCK')
        elapsed = now - self._last if self._last is not None else 0
        try:
            entries = list(self.proc.iterdir())
        except OSError:
            entries = []
        for path in entries:
            if not path.name.isdigit():
                continue
            try:
                if path.stat().st_uid != os.getuid():
                    continue
                executable = (path / 'exe').readlink().name.removesuffix(' (deleted)')
                name = 'firefox' if executable in ('firefox', 'firefox-bin') else 'chromium' if executable in ('chrome', 'chromium', 'chromium-browser') else None
                if name is None:
                    continue
                status = fields(path / 'status')
                stat = (path / 'stat').read_text().rsplit(')', 1)[1].split()
                ticks = int(stat[11]) + int(stat[12])
                # Start time disambiguates recycled PIDs. CPU can exceed 100% across cores.
                key = (int(path.name), stat[19])
                previous[key] = ticks
                cpu = max(0, (ticks - self._previous[key]) / ticks_per_second / elapsed * 100) if elapsed > 0 and key in self._previous else 0
                row = {'pid': int(path.name), 'rss_bytes': status.get('VmRSS', 0),
                       'swap_bytes': status.get('VmSwap', 0), 'cpu_percent': round(cpu, 1)}
                group = browsers[name]
                for field in ('rss_bytes', 'swap_bytes', 'cpu_percent'):
                    group[field] += row[field]
                group['processes'] += 1
                group['largest_processes'].append(row)
            except (OSError, ValueError, IndexError):
                continue  # Processes can exit between /proc reads.
        self._previous, self._last = previous, now
        for group in browsers.values():
            group['cpu_percent'] = round(group['cpu_percent'], 1)
            group['largest_processes'] = sorted(group['largest_processes'], key=lambda r: r['rss_bytes'] + r['swap_bytes'], reverse=True)[:5]
        reasons = []
        if available < max(2 * GIB, total * .10):
            reasons.append('Less than 10% RAM or 2 GiB available')
        if pressure.get('full', 0) >= 10 or pressure.get('some', 0) >= 25:
            reasons.append('Sustained memory pressure')
        for name, group in browsers.items():
            if group['rss_bytes'] + group['swap_bytes'] > max(4 * GIB, total * .50):
                reasons.append(name + ' resident memory plus swap exceeds half of RAM')
        self.snapshot = {'state': 'pressure' if reasons else 'ok', 'sampled_at': time.time(),
                         'message': '; '.join(reasons) if reasons else 'Memory headroom available',
                         'memory': {'total_bytes': total, 'available_bytes': available,
                                    'swap_used_bytes': max(0, mem.get('SwapTotal', 0) - mem.get('SwapFree', 0))},
                         'memory_pressure_avg10': pressure, 'browsers': browsers,
                         'note': 'Process RSS includes shared pages; CPU is percent of one core. Swap may be compressed zram.'}
        return self.snapshot
