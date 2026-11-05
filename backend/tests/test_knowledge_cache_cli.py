"""CLI persists a usable registry across separate offline processes."""
import json
import os
from pathlib import Path
import subprocess
import sys


def test_cli_rebuild_inspect_and_status_share_the_selected_local_root(tmp_path):
    backend = Path(__file__).resolve().parents[1]
    env = {**os.environ, "STOCKVIEW_DATA_DIR": str(tmp_path)}
    def invoke(*args):
        return subprocess.run([sys.executable, str(backend / "scripts/knowledge_cache.py"), *args],
                              cwd=backend, env=env, capture_output=True, text=True, timeout=20)
    result = invoke("rebuild", "--scope", "prompts")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["job"]["status"] == "completed"
    inspected = invoke("inspect", "prompts", "news_relevance")
    assert inspected.returncode == 0, inspected.stderr
    body = json.loads(inspected.stdout)
    assert body["content"]["text"] and body["metadata"]["revision_id"]
    status = invoke("status")
    assert status.returncode == 0, status.stderr
    state = json.loads(status.stdout)
    assert state["root_path"] == str(tmp_path / "knowledge_cache")
    assert state["counts"]["documents"] == 7
    assert state["maintenance"]["active_job"] is None
    assert invoke("rebuild", "--scope", "prompts", "--symbol", "600519.SS").returncode == 2
