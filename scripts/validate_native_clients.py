"""Run installed native CLIs against an isolated fixture, checking real output.

Client launchers must already configure the gateway/key and trust the workspace.
This script never enables blanket tool auto-approval.
"""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
from uuid import uuid4


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bin-dir", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--model", default="gemini-3.8-flash-high")
    parser.add_argument("--timeout", type=int, default=150)
    args = parser.parse_args()
    args.workspace.mkdir(parents=True, exist_ok=True)
    marker = "NATIVE_TOOL_" + uuid4().hex
    fixture = args.workspace / ("probe-" + uuid4().hex[:8] + ".txt")
    fixture.write_text(marker + "\n")
    failures = []
    secrets = [os.environ.get(name, "") for name in ("GEMINI_API_KEY", "AGY_GATEWAY_API_KEY")]
    key_file = args.bin_dir.parent / "test-key.json"
    if key_file.exists():
        secrets.append(json.loads(key_file.read_text())["key"])
    try:
        for client in ("gemini", "agy"):
            prompt = f"Read ./{fixture.name} using your file-reading tool and return its exact contents. Do not use plan mode."
            command = ([str(args.bin_dir / client), "-m", args.model, "-p", prompt, "-o", "json"]
                       if client == "gemini" else
                       [str(args.bin_dir / client), "--model", args.model, "--print", prompt,
                        "--output-format", "json", "--print-timeout", str(args.timeout - 10) + "s"])
            process = subprocess.Popen(command, cwd=args.workspace, text=True,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
            try:
                stdout, stderr = process.communicate(timeout=args.timeout)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                stdout, stderr = process.communicate()
                stderr += "\nHARNESS TIMEOUT: process group stopped"
            try:
                response = json.loads(stdout).get("response", "")
            except ValueError:
                response = ""
            passed = process.returncode == 0 and marker in response and "print timeout" not in stderr
            log = stdout + "\n" + stderr
            for secret in filter(None, secrets):
                log = log.replace(secret, "[REDACTED]")
            path = args.workspace / (client + "-validation.log")
            path.write_text(log)
            path.chmod(0o600)
            print(f"{client}: {'PASS' if passed else 'FAIL'} (exit={process.returncode}, log={path})", flush=True)
            if not passed:
                failures.append(client)
    finally:
        fixture.unlink(missing_ok=True)
    if failures:
        raise SystemExit("Client validation failed: " + ", ".join(failures))


if __name__ == "__main__":
    main()
