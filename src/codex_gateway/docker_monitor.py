"""Read-only Docker telemetry; never expose container environments or mounts."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Lock
from time import monotonic

_lock = Lock()
_cached = None
_cached_at = 0.0


def usage(stats):
    cpu = stats.get("cpu_stats", {})
    previous = stats.get("precpu_stats", {})
    delta = cpu.get("cpu_usage", {}).get("total_usage", 0) - previous.get("cpu_usage", {}).get("total_usage", 0)
    system = cpu.get("system_cpu_usage", 0) - previous.get("system_cpu_usage", 0)
    cores = cpu.get("online_cpus") or len(cpu.get("cpu_usage", {}).get("percpu_usage", []))
    percent = delta / system * cores * 100 if previous and system > 0 and delta >= 0 and cores else None
    memory = stats.get("memory_stats", {})
    cache = memory.get("stats", {}).get("inactive_file", memory.get("stats", {}).get("total_inactive_file", 0))
    used = memory.get("usage")
    return {"cpu_percent": percent, "memory_bytes": max(0, used - min(cache, used)) if used is not None else None,
            "memory_limit_bytes": memory.get("limit"), "pids": stats.get("pids_stats", {}).get("current")}


def snapshot(client):
    global _cached, _cached_at
    with _lock:
        # Ping even on cache hits so a failed daemon is never reported as healthy.
        client.ping()
        if _cached is not None and monotonic() - _cached_at < 20:
            return _cached
        info = client.info()
        containers = client.api.containers(all=True)

        def sample(container):
            result = {"name": (container.get("Names") or [container["Id"][:12]])[0].lstrip("/"),
                      "image": container.get("Image", ""), "state": container.get("State", "unknown"),
                      "managed": container.get("Labels", {}).get("io.codex-gateway.managed") == "true",
                      "cpu_percent": None, "memory_bytes": None, "memory_limit_bytes": None, "pids": None}
            if result["state"] == "running":
                try:
                    result.update(usage(client.api.stats(container["Id"], stream=False)))
                except Exception:
                    result["metrics_unavailable"] = True
            return result

        with ThreadPoolExecutor(max_workers=8) as executor:
            rows = list(executor.map(sample, containers))
        running = [row for row in rows if row["state"] == "running"]
        complete_cpu = all(row["cpu_percent"] is not None for row in running)
        complete_memory = all(row["memory_bytes"] is not None for row in running)
        _cached = {"sampled_at": datetime.now(timezone.utc).isoformat(), "hostname": info.get("Name"),
                   "docker_version": info.get("ServerVersion"), "cpu_cores": info.get("NCPU"),
                   "memory_total_bytes": info.get("MemTotal"), "running": len(running),
                   "stopped": len(rows) - len(running), "containers": rows,
                   "cpu_percent": sum(row["cpu_percent"] for row in running) / max(info.get("NCPU", 1), 1) if complete_cpu else None,
                   "memory_bytes": sum(row["memory_bytes"] for row in running) if complete_memory else None}
        _cached_at = monotonic()
        return _cached
