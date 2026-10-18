"""Execute setup/start scripts in isolated fixture repositories without installs."""
import hashlib
import importlib.util
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]


def executable(path, contents):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents)
    path.chmod(0o755)


def fixture_project(tmp_path):
    root = tmp_path / "project with spaces"
    for folder in ("scripts", "backend/data", "backend/.venv/bin", "frontend/node_modules", "fake-bin"):
        (root / folder).mkdir(parents=True, exist_ok=True)
    for relative in ("scripts/setup.sh", "scripts/dev.sh", "scripts/local_supervisor.py", "backend/app/runtime_paths.py",
                     "backend/app/storage/data_portability.py", "start.sh"):
        (root / relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, root / relative)
    quoted_root, python = shlex.quote(str(root)), shlex.quote(sys.executable)
    (root / "scripts/_common.sh").write_text(f'''set -euo pipefail
sva_root() {{ printf '%s\\n' {quoted_root}; }}
sva_resolve_python() {{ printf '%s\\n' {python}; }}
sva_python_ok() {{ return 0; }}
sva_require_node() {{ return 0; }}
sva_frontend_ok() {{ test -f "$1/frontend/dependencies.ready"; }}
sva_ensure_env_files() {{ touch "$1/backend/.env"; }}
''')
    requirements = "fixture-dependency==1.0\n"
    (root / "backend/requirements.txt").write_text(requirements)
    (root / "backend/.venv/.requirements-hash").write_text(hashlib.sha256(requirements.encode()).hexdigest() + "\n")
    lock = '{"name":"fixture","lockfileVersion":3}\n'
    (root / "frontend/package-lock.json").write_text(lock)
    (root / "frontend/node_modules/.package-hash").write_text(hashlib.sha256(lock.encode()).hexdigest() + "\n")
    (root / "backend/data/user-data.txt").write_text("preserve user data")
    (root / "frontend/node_modules/original.txt").write_text("preserve broken dependency copy")
    executable(root / "backend/.venv/bin/python", f'''#!/usr/bin/env bash
set -eu
root={quoted_root}
if [[ "$1" == */check_dependencies.py ]]; then
  test -f "$root/backend/dependencies.ready"
  exit $?
fi
if [[ "$1" == '-m' && "$2" == 'pip' ]]; then
  if [[ "$3" == '--version' ]]; then echo 'pip fixture'; exit 0; fi
  printf '%s\\n' "$*" >> "$root/pip.calls"
  sleep .15
  touch "$root/backend/dependencies.ready"
  exit 0
fi
exec {python} "$@"
''')
    executable(root / "fake-bin/node", "#!/usr/bin/env bash\necho 'v22.0.0'\n")
    executable(root / "fake-bin/npm", f'''#!/usr/bin/env bash
set -eu
root={quoted_root}
if [[ "$1" == '-v' ]]; then echo '10.0.0'; exit 0; fi
[[ "$1" == 'ci' ]]
printf '%s\\n' "$*" >> "$root/npm.calls"
mkdir -p "$root/frontend/node_modules"
touch "$root/frontend/dependencies.ready"
''')
    env = {**os.environ, "PATH": str(root / "fake-bin") + os.pathsep + os.environ["PATH"]}
    env.pop("SVA_SETUP_LOCKED", None)
    env.pop("STOCKVIEW_DATA_DIR", None)
    return root, env


def test_setup_repairs_missing_declared_dependencies_without_updating_code_or_data(tmp_path):
    root, env = fixture_project(tmp_path)
    command = ["bash", str(root / "scripts/setup.sh")]
    first = subprocess.run(command, env=env, capture_output=True, text=True, timeout=15)
    assert first.returncode == 0, first.stdout + first.stderr
    calls = (root / "pip.calls").read_text().splitlines()
    assert len(calls) == 1
    assert "install --force-reinstall -r " in calls[0]
    assert calls[0].endswith(str(root / "backend/requirements.txt"))
    assert "-U pip" not in calls[0]
    assert (root / "npm.calls").read_text() == "ci\n"
    assert (root / "backend/data/user-data.txt").read_text() == "preserve user data"
    backups = list((root / "frontend").glob("node_modules.invalid.*/original.txt"))
    assert len(backups) == 1 and backups[0].read_text() == "preserve broken dependency copy"
    assert "只需填写大模型 API" in first.stdout
    second = subprocess.run(command, env=env, capture_output=True, text=True, timeout=15)
    assert second.returncode == 0, second.stdout + second.stderr
    assert (root / "pip.calls").read_text().splitlines() == calls
    assert (root / "npm.calls").read_text() == "ci\n"


@pytest.mark.parametrize("separate_data_roots", [False, True])
def test_concurrent_setup_is_serialized_before_dependency_install(tmp_path, separate_data_roots):
    root, env = fixture_project(tmp_path)
    environments = [{**env, "STOCKVIEW_DATA_DIR": str(root / f"data-{index}")} if separate_data_roots else env
                    for index in range(2)]
    processes = [subprocess.Popen(["bash", str(root / "scripts/setup.sh")], env=environment,
                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True) for environment in environments]
    outputs = [process.communicate(timeout=15)[0] for process in processes]
    assert all(process.returncode == 0 for process in processes), outputs
    assert len((root / "pip.calls").read_text().splitlines()) == 1
    assert (root / "npm.calls").read_text() == "ci\n"


def test_start_status_forwards_arguments_without_dependency_install(tmp_path):
    root, env = fixture_project(tmp_path)
    result = subprocess.run(["bash", str(root / "start.sh"), "--status", "--no-browser"],
                            env=env, capture_output=True, text=True, timeout=5)
    assert result.returncode == 1
    assert "当前未运行" in result.stdout
    assert not (root / "pip.calls").exists()
    assert not (root / "npm.calls").exists()
    assert not (root / "backend/.env").exists()


def test_setup_uses_configured_root_for_runtime_lock_and_preserves_old_data(tmp_path):
    root, env = fixture_project(tmp_path)
    (root / "backend/.env").write_text('STOCKVIEW_DATA_DIR="../portable data"\n')
    result = subprocess.run(["bash", str(root / "scripts/setup.sh")], cwd=tmp_path, env=env,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (root / "portable data/runtime/pids/setup.lock").exists()
    assert not (root / "pids").exists()
    assert (root / "backend/data/user-data.txt").read_text() == "preserve user data"


def test_dependency_probe_checks_declared_versions_and_missing_modules(tmp_path):
    spec = importlib.util.spec_from_file_location("sva_dependency_probe", ROOT / "scripts/check_dependencies.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("fastapi==0.0.0\nsva_missing_fixture_package==1.0\n")
    failures = module.check(requirements)
    assert any("fastapi 版本不符合" in item for item in failures)
    assert any("sva_missing_fixture_package" in item for item in failures)


def test_dependency_probe_detects_missing_transitive_requirement(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("sva_dependency_probe_transitive", ROOT / "scripts/check_dependencies.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("fastapi>=0.100\n")
    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1, "fixture requires missing-child", ""))
    assert any("传递依赖缺失" in item for item in module.check(requirements))
