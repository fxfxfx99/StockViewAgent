#!/usr/bin/env python3
"""Check declared Python requirements without installing or contacting a source."""
from __future__ import annotations
import importlib
import importlib.metadata
import importlib.util
from pathlib import Path
import subprocess
import sys

MODULES = {"python-multipart": "multipart", "python-dotenv": "dotenv", "pydantic-settings": "pydantic_settings", "pyyaml": "yaml", "pyjwt": "jwt"}


def check(requirements: Path) -> list[str]:
    try:
        from pip._vendor.packaging.requirements import Requirement
    except ImportError:
        return ["pip 不可用"]
    problems = []
    for line in requirements.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            requirement = Requirement(line)
            if requirement.marker and not requirement.marker.evaluate():
                continue
            version = importlib.metadata.version(requirement.name)
            if version not in requirement.specifier:
                problems.append(f"{requirement.name} 版本不符合 {requirement.specifier}")
            module = MODULES.get(requirement.name.lower(), requirement.name.lower().replace("-", "_"))
            if importlib.util.find_spec(module) is None:
                problems.append(f"{requirement.name} 模块文件缺失")
        except (ImportError, ValueError) as exc:
            problems.append(f"依赖不可用：{line} ({type(exc).__name__})")
    # Import the execution/data libraries to catch broken native wheels as well
    # as metadata-only leftovers. No application modules or credentials load.
    for module in ("uvicorn", "fastapi", "duckdb", "pyarrow", "pandas", "numpy"):
        try:
            importlib.import_module(module)
        except Exception as exc:
            problems.append(f"{module} 无法导入 ({type(exc).__name__})")
    try:
        result = subprocess.run([sys.executable, "-m", "pip", "check"], capture_output=True, text=True, timeout=30)
        if result.returncode:
            problems.append("传递依赖缺失或版本冲突：" + (result.stdout + result.stderr).strip()[:1000])
    except (OSError, subprocess.TimeoutExpired):
        problems.append("无法完成传递依赖检查")
    return problems


if __name__ == "__main__":
    failures = check(Path(sys.argv[1]))
    if failures:
        print("\n".join(failures), file=sys.stderr)
    raise SystemExit(bool(failures))
