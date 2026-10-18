"""Data-root resolution is independent of cwd and fixed for a running process."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from app.config import settings
from app.runtime_paths import resolve_data_dir
from app.storage import llm_runtime

ROOT = Path(__file__).resolve().parents[2]


def test_default_and_relative_paths_are_backend_relative_from_any_cwd(tmp_path, monkeypatch):
    backend = tmp_path / "project with spaces/backend"
    backend.mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert resolve_data_dir(backend, environ={}) == backend / "data"
    assert resolve_data_dir(backend, environ={"STOCKVIEW_DATA_DIR": "../portable data"}) == backend.parent / "portable data"
    assert not (backend / "data").exists()  # import/bootstrap parsing has no write side effects


def test_env_overrides_dotenv_with_literal_quoted_space_paths(tmp_path):
    backend = tmp_path / "backend"
    backend.mkdir()
    (backend / ".env").write_text('OTHER_KEY=do-not-execute\nexport STOCKVIEW_DATA_DIR="../data with spaces" # comment\n')
    assert resolve_data_dir(backend, environ={}) == tmp_path / "data with spaces"
    assert resolve_data_dir(backend, environ={"STOCKVIEW_DATA_DIR": "explicit"}) == backend / "explicit"
    assert resolve_data_dir(backend, environ={"STOCKVIEW_DATA_DIR": ""}) == backend / "data"
    (backend / ".env").write_text('STOCKVIEW_DATA_DIR="$(touch should-not-exist)"\n')
    assert resolve_data_dir(backend, environ={}).name == "$(touch should-not-exist)"
    assert not (backend / "should-not-exist").exists()


def test_data_root_does_not_switch_after_environment_or_cwd_changes(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    script = """
import json, os
from pathlib import Path
from app.config import settings
first = settings.data_dir
os.environ['STOCKVIEW_DATA_DIR'] = os.environ['TEST_SECOND_ROOT']
os.chdir(os.environ['TEST_SECOND_ROOT'])
print(json.dumps({'before': str(first), 'after': str(settings.data_dir),
                  'uploads': str(settings.issuer_uploads_dir), 'learning': str(settings.kline_learning_dir)}))
"""
    second.mkdir()
    result = subprocess.run([sys.executable, "-c", script], cwd=tmp_path, capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": str(ROOT / "backend"), "STOCKVIEW_DATA_DIR": str(first),
             "TEST_SECOND_ROOT": str(second)}, check=True, timeout=10)
    data = json.loads(result.stdout)
    assert data == {"before": str(first), "after": str(first), "uploads": str(first / "issuer_uploads"),
                    "learning": str(first / "kline_learning")}
    assert not (second / "issuer_uploads").exists()


def test_llm_config_uses_data_root_preserves_existing_keys_and_is_private(monkeypatch):
    monkeypatch.setattr(llm_runtime, "_FILE", None)
    path = settings.data_dir / "llm_runtime.json"
    path.write_text(json.dumps({"api_key": "existing-key", "fallback_api_key": "existing-fallback"}))
    llm_runtime.save_runtime_merge(model="new-model")
    assert llm_runtime.load_runtime() == {"api_key": "existing-key", "fallback_api_key": "existing-fallback", "model": "new-model"}
    assert path.stat().st_mode & 0o777 == 0o600


def test_interrupted_llm_replace_keeps_original_bytes(monkeypatch):
    monkeypatch.setattr(llm_runtime, "_FILE", None)
    path = settings.data_dir / "llm_runtime.json"
    original = b'{"api_key":"preserved-secret", "model":"old"}'
    path.write_bytes(original)
    def fail(*args):
        raise OSError("fixture disk failure")
    monkeypatch.setattr(llm_runtime.os, "replace", fail)
    with pytest.raises(OSError):
        llm_runtime.save_runtime_merge(model="new")
    assert path.read_bytes() == original
    assert not list(path.parent.glob(".llm-runtime-*.tmp"))


def test_invalid_existing_llm_file_is_not_overwritten(monkeypatch):
    monkeypatch.setattr(llm_runtime, "_FILE", None)
    path = settings.data_dir / "llm_runtime.json"
    path.write_bytes(b'{"api_key":"interrupted')
    with pytest.raises(ValueError, match="保留原文件"):
        llm_runtime.save_runtime_merge(model="new")
    assert path.read_bytes() == b'{"api_key":"interrupted'
