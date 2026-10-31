"""Small current-RSS probe for cooperative collector admission, not a RAM cap."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import threading
import time

_lock = threading.Lock()
_sample = (0.0, None)


def _rss_bytes():
    if sys.platform.startswith("linux"):
        fields = Path("/proc/self/statm").read_text().split()
        return int(fields[1]) * os.sysconf("SC_PAGE_SIZE")
    if sys.platform == "darwin":
        # getrusage is a lifetime high-water mark, so it cannot detect recovery.
        result = subprocess.run(["/bin/ps", "-o", "rss=", "-p", str(os.getpid())],
                                capture_output=True, text=True, timeout=1, check=True)
        return int(result.stdout.strip()) * 1024
    return None


def snapshot(max_memory_mb=768, *, fresh=False):
    global _sample
    with _lock:
        now = time.monotonic()
        if fresh or now - _sample[0] >= 5:
            try:
                rss = _rss_bytes()
            except (OSError, ValueError, IndexError, subprocess.SubprocessError):
                rss = None
            _sample = (now, rss if isinstance(rss, int) and rss > 0 else None)
        rss = _sample[1]
    pressure = rss is not None and rss >= max_memory_mb * 1024**2
    return {"rss_mb": round(rss / 1024**2, 1) if rss is not None else None,
            "limit_mb": max_memory_mb, "pressure": pressure, "supported": rss is not None,
            "reason": "high_memory" if pressure else None, "scope": "current_process"}
