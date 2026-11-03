#!/usr/bin/env python3
"""Host launcher: only the configured host and mounted archive disk may run."""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    args, extra = parser.parse_known_args()
    config = json.loads(args.config.read_text())
    if socket.gethostname() != config["hostname"]:
        print("Archive skipped: not the designated host")
        return
    mounted = subprocess.run(["findmnt", "--mountpoint", config["mount"], "--noheadings", "--output", "UUID"],
                             capture_output=True, text=True)
    if mounted.returncode or mounted.stdout.strip() != config["uuid"]:
        print("Archive skipped: expected disk is not mounted")
        return
    try:
        container = json.loads(subprocess.check_output(["docker", "inspect", config["gateway"]], stderr=subprocess.DEVNULL))[0]
    except subprocess.CalledProcessError:
        print("Archive skipped: designated gateway is unavailable")
        return
    env = dict(item.split("=", 1) for item in container["Config"]["Env"])
    if not container["State"]["Running"] or env.get("CODEX_GATEWAY_NODE_ID") != config["node_id"]:
        print("Archive skipped: designated gateway is unavailable")
        return
    root = Path(config["mount"]).resolve() / "request-context-archives"
    root.mkdir(mode=0o700, exist_ok=True)
    if root.is_symlink():
        raise RuntimeError("Archive directory must not be a symlink")
    network = next(iter(container["NetworkSettings"]["Networks"]))
    script = Path(__file__).with_name("archive_request_context.py").resolve()
    command = ["docker", "run", "--rm", "--name", "request-context-archiver",
               "--user", "0:0", "--network", network, "--read-only", "--tmpfs", "/tmp",
               "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
               "--memory", "1g", "--cpus", "0.5",
               "--mount", f"type=bind,src={root},dst=/archives",
               "--mount", f"type=bind,src={script},dst=/archive.py,readonly",
               "--env", "CODEX_GATEWAY_DATABASE_URL", "--entrypoint", "python",
               container["Image"], "/archive.py", "--root", "/archives", *extra]
    result = subprocess.run(command, env={**os.environ,
                            "CODEX_GATEWAY_DATABASE_URL": env["CODEX_GATEWAY_DATABASE_URL"]})
    sys.exit(result.returncode)

if __name__ == "__main__":
    main()
