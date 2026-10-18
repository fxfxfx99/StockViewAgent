"""Dependency-free data-root resolution shared by Python and local launchers.

STOCKVIEW_DATA_DIR is resolved once at process startup. Relative paths are
relative to backend/, never the caller's working directory. dotenv values are
literal paths (quotes and ~ supported); no shell evaluation is performed.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import shlex


def _dotenv_data_dir(path: Path) -> str | None:
    if not path.exists():
        return None
    value = None
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^\s*(?:export\s+)?STOCKVIEW_DATA_DIR\s*=\s*(.*)$", line)
        if not match:
            continue
        try:
            parts = shlex.split(match.group(1), comments=True, posix=True)
        except ValueError:
            raise ValueError("STOCKVIEW_DATA_DIR 引号不完整") from None
        value = " ".join(parts)
    return value


def resolve_data_dir(backend_dir: Path, *, environ=None) -> Path:
    backend = Path(backend_dir).expanduser().resolve()
    env = os.environ if environ is None else environ
    raw = env.get("STOCKVIEW_DATA_DIR")
    if raw is None:
        raw = _dotenv_data_dir(backend / ".env")
    raw = str(raw or "").strip()
    path = Path(raw).expanduser() if raw else Path("data")
    if not path.is_absolute():
        path = backend / path
    return path.resolve()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Resolve the fixed local data root")
    parser.add_argument("--backend-dir", type=Path, default=Path(__file__).resolve().parent.parent)
    print(resolve_data_dir(parser.parse_args().backend_dir))
