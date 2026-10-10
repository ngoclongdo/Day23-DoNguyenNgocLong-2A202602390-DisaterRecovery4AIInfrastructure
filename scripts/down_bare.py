"""Python script to stop bare services."""
import os
import pathlib
import signal
import sys

root = pathlib.Path(__file__).resolve().parent.parent
run_dir = root / "run"

for f in run_dir.glob("*.pid"):
    try:
        pid = int(f.read_text(encoding="utf-8").strip())
        try:
            os.kill(pid, getattr(signal, "SIGTERM", 15))
        except OSError:
            pass
    except Exception:
        pass
    f.unlink(missing_ok=True)

print("all stopped")
