from __future__ import annotations

import os
import subprocess
import sys


def main() -> int:
    port = os.environ.get("PORT", "8501")
    command = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        "streamlit_app.py",
        "--server.address=0.0.0.0",
        f"--server.port={port}",
        "--server.headless=true",
        "--browser.gatherUsageStats=false",
    ]
    return subprocess.call(command)


if __name__ == "__main__":
    raise SystemExit(main())
