#!/usr/bin/env python3
import json
import subprocess
import sys


def main() -> int:
    result = subprocess.run(
        [
            "supervisorctl",
            "-c",
            "/etc/supervisor/conf.d/tg-media-suite.conf",
            "status",
        ],
        capture_output=True,
        text=True,
        timeout=8,
    )
    if result.returncode:
        print(result.stderr or result.stdout)
        return 1
    states = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            states[parts[0]] = parts[1]
    expected = {"forwarder", "watch115"}
    if any(states.get(name) != "RUNNING" for name in expected):
        print(json.dumps(states, ensure_ascii=False))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
