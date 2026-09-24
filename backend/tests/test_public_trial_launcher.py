"""Launcher helpers are verified without building or opening a public tunnel."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts/public_trial.py"
_SPEC = importlib.util.spec_from_file_location("public_trial_launcher", _SCRIPT)
launcher = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(launcher)


def test_macos_proxy_settings_respect_enabled_flags_and_ports():
    result = launcher.parse_macos_proxy("""<dictionary> {
        HTTPEnable : 1
        HTTPProxy : 127.0.0.1
        HTTPPort : 8123
        HTTPSEnable : 1
        HTTPSProxy : ::1
        HTTPSPort : 8124
    }""")
    assert result == {"HTTP_PROXY": "http://127.0.0.1:8123", "HTTPS_PROXY": "http://[::1]:8124"}
    assert launcher.parse_macos_proxy("HTTPEnable : 0\nHTTPProxy : localhost\nHTTPPort : 1234") == {}
    assert launcher.parse_macos_proxy("HTTPEnable : 1\nHTTPProxy : localhost\nHTTPPort : 99999") == {}


def test_tunnel_proxy_does_not_leak_into_backend_environment(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "private-test-key")
    monkeypatch.setenv("AUTH_REQUIRED", "false")
    monkeypatch.setenv("HTTPS_PROXY", "http://wrong.example:9000")
    monkeypatch.setattr(launcher.sys, "platform", "darwin")
    monkeypatch.setattr(launcher.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(
        stdout="HTTPSEnable : 1\nHTTPSProxy : localhost\nHTTPSPort : 8888"
    ))
    tunnel_env = launcher.tunnel_environment()
    backend_env = launcher.clean_environment()
    assert tunnel_env["HTTPS_PROXY"] == "http://localhost:8888"
    assert tunnel_env["NO_PROXY"] == "localhost,127.0.0.1,::1"
    for env in (tunnel_env, backend_env):
        assert "OPENAI_API_KEY" not in env
        assert "AUTH_REQUIRED" not in env
    assert "HTTPS_PROXY" not in backend_env


def test_process_identity_protects_against_reused_pids(monkeypatch):
    monkeypatch.setattr(launcher, "process_record", lambda pid: {"pid": pid, "identity": "new process"})
    assert not launcher.is_alive({"pid": 123, "identity": "old process"})
    assert not launcher.is_alive({"pid": 123, "identity": ""})
    assert launcher.is_alive({"pid": 123, "identity": "new process"})


@pytest.fixture
def isolated_launcher(tmp_path, monkeypatch):
    trial = tmp_path / ".public-trial"
    trial.mkdir()
    monkeypatch.setattr(launcher, "ROOT", tmp_path)
    monkeypatch.setattr(launcher, "TRIAL", trial)
    monkeypatch.setattr(launcher, "RUNTIME", trial / "runtime")
    monkeypatch.setattr(launcher, "STATE", trial / "state.json")
    return tmp_path, trial


def test_stop_never_signals_a_reused_recorded_pid(isolated_launcher, monkeypatch):
    _, trial = isolated_launcher
    launcher.private_json(launcher.STATE, {"supervisor": {"pid": 123, "identity": "stale"}, "processes": {}})
    monkeypatch.setattr(launcher, "process_record", lambda pid: {"pid": pid, "identity": "different process"})
    calls = []
    monkeypatch.setattr(launcher.os, "kill", lambda *args: calls.append(args))
    launcher.stop()
    assert calls == []
    assert launcher.read_state()["status"] == "stopped"


@pytest.mark.parametrize("has_npm", [True, False])
def test_prepare_copies_only_trial_code_and_preserves_trial_data(isolated_launcher, monkeypatch, has_npm):
    root, trial = isolated_launcher
    for name, content in {
        "backend/app/main.py": "pass",
        "backend/.env": "OPENAI_API_KEY=private-source-key",
        "backend/data/private.json": "private-data",
        "backend/.venv/bin/python": "python",
        "frontend/dist/index.html": "public-app",
        "frontend/node_modules/vite/bin/vite.js": "vite",
        "deploy/trial_app.py": "pass",
        "issuer_uploads/private.txt": "private-upload",
    }.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    monkeypatch.setattr(launcher, "PYTHON", root / "backend/.venv/bin/python")
    monkeypatch.setattr(launcher, "find_node", lambda: Path("/mock/bin/node"))
    monkeypatch.setattr(launcher.shutil, "which", lambda *args, **kwargs: "/mock/bin/npm" if has_npm else None)
    builds = []

    def fake_build(command, **kwargs):
        builds.append((command, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(launcher.subprocess, "run", fake_build)
    launcher.prepare_runtime()
    runtime = launcher.RUNTIME
    assert (runtime / "backend/app/main.py").is_file()
    assert (runtime / "backend/trial_app.py").is_file()
    assert (runtime / "frontend/dist/index.html").read_text() == "public-app"
    assert not (runtime / "backend/data/private.json").exists()
    assert not (runtime / "issuer_uploads").exists()
    assert "private-source-key" not in (runtime / "backend/.env").read_text()
    assert "AUTH_REQUIRED=true" in (runtime / "backend/.env").read_text()
    assert builds[0][1]["env"]["VITE_API_BASE_URL"] == ""
    assert builds[0][0] == (["/mock/bin/npm", "run", "build"] if has_npm else ["/mock/bin/node", str(root / "frontend/node_modules/vite/bin/vite.js"), "build"])
    credentials_path = trial / "admin-credentials.json"
    credentials = credentials_path.read_bytes()
    assert credentials_path.stat().st_mode & 0o777 == 0o600
    assert (runtime / "backend/.env").stat().st_mode & 0o777 == 0o600
    assert json.loads((runtime / "backend/data/integrations.json").read_text())["market_data_provider"] == "public"
    (runtime / "backend/data/retained.json").write_text("keep")
    launcher.prepare_runtime()
    assert credentials_path.read_bytes() == credentials
    assert (runtime / "backend/data/retained.json").read_text() == "keep"


def test_tunnel_url_extraction_only_uses_quick_tunnel_host():
    assert launcher.URL_RE.search("Visit https://bright-test.trycloudflare.com now").group() == "https://bright-test.trycloudflare.com"
    assert launcher.URL_RE.search("https://api.trycloudflare.com/tunnel") is None
