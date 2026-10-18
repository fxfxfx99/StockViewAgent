import json
from pathlib import Path
import sqlite3

import pytest

from app.storage import storage_bootstrap as boot


def make_project(tmp_path):
    project = tmp_path / 'project'
    data = project / 'backend' / 'data'
    data.mkdir(parents=True)
    return project, data


def test_default_consolidates_originals_once_without_overwrite(tmp_path):
    project, data = make_project(tmp_path)
    legacy = project / 'issuer_uploads'
    (legacy / 'files').mkdir(parents=True)
    (legacy / 'files' / 'evidence.txt').write_text('local evidence')
    (legacy / 'materials.json').write_text('{"items": [{"id": "a", "stored_name": "evidence.txt"}]}')
    key = project / 'backend' / 'app' / 'data' / 'llm_runtime.json'
    key.parent.mkdir(parents=True)
    key.write_text('{"api_key":"private-example-key"}')
    csv = project / '财务指标 2025.csv'
    csv.write_text('symbol,value\n600519,1')
    state = boot.initialize_local_storage(data, project_root=project)
    assert (data / 'issuer_uploads/files/evidence.txt').read_text() == 'local evidence'
    assert key.exists() and legacy.exists() and csv.exists()
    assert json.loads((data / 'llm_runtime.json').read_text())['api_key'] == 'private-example-key'
    assert (data / 'llm_runtime.json').stat().st_mode & 0o777 == 0o600
    assert (data / 'imports/company_fundamentals' / csv.name).read_bytes() == csv.read_bytes()
    assert 'private-example-key' not in (data / '_managed/layout.json').read_text()
    (data / 'issuer_uploads/files/evidence.txt').write_text('new learning')
    again = boot.initialize_local_storage(data, project_root=project)
    assert again['directory_id'] == state['directory_id']
    assert (data / 'issuer_uploads/files/evidence.txt').read_text() == 'new learning'
    assert (data / 'INDEX.md').is_file()


def test_custom_root_does_not_import_repository_keys_or_materials(tmp_path):
    project, _ = make_project(tmp_path)
    (project / 'issuer_uploads').mkdir()
    (project / 'issuer_uploads/materials.json').write_text('{"items":[1]}')
    root = tmp_path / 'isolated'
    state = boot.initialize_local_storage(root, project_root=project)
    assert state['legacy_import'] == {}
    assert not (root / 'issuer_uploads/materials.json').exists()
    assert not (root / 'llm_runtime.json').exists()


def test_existing_destination_kept_and_old_data_archived(tmp_path):
    project, data = make_project(tmp_path)
    old = project / 'kline_learning'
    old.mkdir()
    (old / 'prompt.json').write_text('{"system":"old"}')
    new = data / 'kline_learning'
    new.mkdir()
    (new / 'prompt.json').write_text('{"system":"current"}')
    state = boot.initialize_local_storage(data, project_root=project)
    assert json.loads((new / 'prompt.json').read_text())['system'] == 'current'
    assert json.loads((data / '_managed/legacy/kline_learning/prompt.json').read_text())['system'] == 'old'
    assert state['legacy_import']['kline_learning']['status'] == 'archived_conflict'


def test_bad_key_is_reported_without_copying_or_exposing_content(tmp_path):
    project, data = make_project(tmp_path)
    key = project / 'backend/app/data/llm_runtime.json'
    key.parent.mkdir(parents=True)
    key.write_text('secret-invalid-json')
    state = boot.initialize_local_storage(data, project_root=project)
    assert not (data / 'llm_runtime.json').exists()
    assert state['legacy_import']['llm_runtime.json']['status'] == 'needs_attention'
    assert 'secret-invalid-json' not in (data / '_managed/layout.json').read_text()


def test_interruption_preserves_source_and_retries(tmp_path, monkeypatch):
    project, data = make_project(tmp_path)
    source = project / 'issuer_uploads'
    source.mkdir()
    (source / 'materials.json').write_text('{"items":[]}')
    real = boot._copy_file
    monkeypatch.setattr(boot, '_copy_file', lambda *args: (_ for _ in ()).throw(OSError('disk full')))
    first = boot.initialize_local_storage(data, project_root=project)
    assert first['legacy_import']['issuer_uploads']['status'] == 'needs_attention'
    assert (source / 'materials.json').exists()
    assert not list(data.rglob('.legacy-stage-*'))
    monkeypatch.setattr(boot, '_copy_file', real)
    second = boot.initialize_local_storage(data, project_root=project)
    assert second['legacy_import']['issuer_uploads']['status'] == 'copied'


def test_racing_destination_is_not_overwritten(tmp_path, monkeypatch):
    source = tmp_path / 'source.json'
    source.write_text('{"key":"legacy"}')
    destination = tmp_path / 'current.json'
    real = boot._publish_no_replace
    def race(parent_fd, stage_name, destination_name):
        destination.write_text('{"key":"just-saved"}')
        return real(parent_fd, stage_name, destination_name)
    monkeypatch.setattr(boot, '_publish_no_replace', race)
    with pytest.raises(boot.DataPortabilityError):
        boot._copy_asset(source, destination)
    assert json.loads(destination.read_text())['key'] == 'just-saved'
    assert not list(tmp_path.glob('.legacy-stage-*'))


def test_legacy_symlink_is_not_followed(tmp_path):
    project, data = make_project(tmp_path)
    source = project / 'issuer_uploads'
    source.mkdir()
    external = tmp_path / 'external-secret'
    external.write_text('keep outside')
    (source / 'materials.json').symlink_to(external)
    state = boot.initialize_local_storage(data, project_root=project)
    assert state['legacy_import']['issuer_uploads']['status'] == 'needs_attention'
    assert not (data / 'issuer_uploads/materials.json').exists()
    assert external.read_text() == 'keep outside'


def test_sqlite_copy_merges_committed_wal(tmp_path):
    source = tmp_path / 'original'
    source.mkdir()
    conn = sqlite3.connect(source / 'chunks.db')
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('CREATE TABLE chunks(text TEXT)')
    conn.execute("INSERT INTO chunks VALUES ('knowledge')")
    conn.commit()
    try:
        boot._copy_asset(source, tmp_path / 'copied')
        with sqlite3.connect(tmp_path / 'copied/chunks.db') as copied:
            assert copied.execute('SELECT text FROM chunks').fetchone()[0] == 'knowledge'
    finally:
        conn.close()


def test_legacy_parent_link_cannot_import_another_instances_key(tmp_path):
    project, data = make_project(tmp_path)
    outside = tmp_path / 'another-instance'
    outside.mkdir()
    (outside / 'llm_runtime.json').write_text('{"api_key":"other-instance-secret"}')
    (project / 'backend/app').mkdir()
    (project / 'backend/app/data').symlink_to(outside, target_is_directory=True)
    state = boot.initialize_local_storage(data, project_root=project)
    assert state['legacy_import']['llm_runtime.json']['status'] == 'needs_attention'
    assert not (data / 'llm_runtime.json').exists()


def test_managed_link_cannot_write_outside_data_root(tmp_path):
    project, data = make_project(tmp_path)
    outside = tmp_path / 'outside'
    outside.mkdir()
    (data / '_managed').symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        boot.initialize_local_storage(data, project_root=project)
    assert list(outside.iterdir()) == []
