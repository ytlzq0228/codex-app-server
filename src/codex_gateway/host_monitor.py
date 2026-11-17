"""Host metrics from explicit read-only host mounts; never container /proc."""
import os
import time
from pathlib import Path
from threading import Lock

_lock = Lock()
_previous = None
_cached = None


def read_host(proc, root):
    cpu = [int(v) for v in (proc / 'stat').read_text().splitlines()[0].split()[1:9]]
    memory = {parts[0].rstrip(':'): int(parts[1]) * 1024
              for line in (proc / 'meminfo').read_text().splitlines() if (parts := line.split()) and len(parts) >= 2}
    # PID 1 belongs to the mounted host procfs, so its network namespace is the host.
    rx = tx = 0
    for line in (proc / '1/net/dev').read_text().splitlines()[2:]:
        name, values = line.split(':', 1)
        # Exclude loopback/virtual bridges to avoid counting Docker traffic twice.
        if name.strip().startswith(('lo', 'veth', 'docker', 'br-', 'virbr')):
            continue
        values = values.split()
        rx += int(values[0]); tx += int(values[8])
    disk = os.statvfs(root)
    cores = sum(line.startswith('cpu') and line[3:4].isdigit() for line in (proc / 'stat').read_text().splitlines())
    return {'cpu_total': sum(cpu), 'cpu_idle': cpu[3] + cpu[4], 'rx': rx, 'tx': tx,
            'cpu_cores': cores, 'memory_total_bytes': memory['MemTotal'],
            'memory_percent': 100 * (1 - memory['MemAvailable'] / memory['MemTotal']),
            'disk_percent': 100 * (1 - disk.f_bavail / disk.f_blocks),
            'load1': float((proc / 'loadavg').read_text().split()[0]), 'time': time.monotonic()}


def snapshot():
    global _previous, _cached
    with _lock:
        if _cached is not None and time.monotonic() - _previous['time'] < 10:
            return _cached
        proc = Path(os.environ.get('CODEX_HOST_PROC', '/host/proc'))
        root = os.environ.get('CODEX_HOST_ROOT', '/host/root')
        current = read_host(proc, root)
        if _previous is None:
            _previous = current
            time.sleep(.2)
            current = read_host(proc, root)
        elapsed = current['time'] - _previous['time']
        delta = current['cpu_total'] - _previous['cpu_total']
        result = {key: current[key] for key in ('cpu_cores', 'memory_total_bytes', 'memory_percent', 'disk_percent', 'load1')}
        result.update(cpu_percent=100 * (1 - (current['cpu_idle'] - _previous['cpu_idle']) / delta) if delta > 0 else None,
                      rx_bytes_per_second=max(0, current['rx'] - _previous['rx']) / elapsed,
                      tx_bytes_per_second=max(0, current['tx'] - _previous['tx']) / elapsed)
        _previous, _cached = current, result
        return result
