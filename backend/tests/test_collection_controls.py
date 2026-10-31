"""Collection/display separation and memory admission; all I/O is local or mocked."""
import asyncio
import tracemalloc

import pytest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import settings
from app.routers import data_center, news, settings_integrations
from app.services import collector_resources, data_collection, startup_data_check
from app.storage import data_asset_manager, data_collection_store, news_store, users_store


def local_client(monkeypatch):
    monkeypatch.setattr(settings, 'auth_required', False)
    users_store.init_db()
    app = FastAPI()
    for router in (data_center.router, news.router, settings_integrations.router):
        app.include_router(router)
    return TestClient(app)


def test_polling_collection_status_never_scans_files_or_enqueues(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('display read must not scan the directory or schedule collection')
    monkeypatch.setattr(data_asset_manager, 'scan_data_assets', forbidden)
    monkeypatch.setattr(data_collection, 'enqueue', forbidden)
    with local_client(monkeypatch) as client:
        result = client.get('/api/data-center/collection/status')
    assert result.status_code == 200
    assert {'sources', 'jobs', 'worker', 'usage', 'config'} <= result.json().keys()


def test_inventory_summary_omits_per_file_payload(monkeypatch, tmp_path):
    (tmp_path / 'one.txt').write_text('one')
    full = data_asset_manager.scan_data_assets()
    summary = data_asset_manager.scan_data_assets(include_items=False)
    assert 'items' not in summary
    for field in ('totals', 'directory_totals', 'categories'):
        assert summary[field] == full[field]
    with local_client(monkeypatch) as client:
        response = client.get('/api/data-center/inventory/summary')
    assert response.status_code == 200
    assert 'items' not in response.json()


def test_news_manual_queue_validates_targets_and_deduplicates(monkeypatch):
    with local_client(monkeypatch) as client:
        payload = {'symbols': ['600519.SS'], 'lookback_days': 7}
        first = client.post('/api/news/sync-archive-feeds', json=payload)
        assert first.status_code == 202
        job = first.json()['jobs'][0]
        repeated = client.post('/api/news/sync-archive-feeds', json=payload)
        assert repeated.status_code == 202
        assert repeated.json()['jobs'][0]['id'] == job['id']
        conflict = client.post('/api/news/sync-archive-feeds', json={**payload, 'lookback_days': 14})
        assert conflict.status_code == 409
        assert client.post('/api/news/sync-archive-feeds', json={'symbols': ['https://bad']}).status_code == 422
        assert data_collection_store.list_jobs()[0]['status'] == 'queued'


def test_refresh_endpoint_schedules_inside_the_event_loop(monkeypatch):
    completed = []
    async def run():
        completed.append(True)
        return {'status': 'ok'}
    monkeypatch.setattr(startup_data_check, 'run_startup_data_check', run)
    monkeypatch.setattr(startup_data_check, '_BACKGROUND_TASK', None)
    with local_client(monkeypatch) as client:
        response = client.post('/api/settings/data-center/refresh')
    assert response.status_code == 200
    assert response.json()['status'] == 'scheduled'
    assert completed


def test_startup_skips_network_step_under_memory_pressure(monkeypatch):
    monkeypatch.setattr(collector_resources, 'snapshot', lambda *args: {'pressure': True, 'rss_mb': 900})
    async def forbidden():
        raise AssertionError('must wait for memory to fall')
    result = asyncio.run(startup_data_check._capture_step('market', forbidden))
    assert result['status'] == 'skipped'
    assert result['reason'] == 'high_memory'


@pytest.mark.parametrize('offset', [5, 900, 1000000])
def test_ranked_archive_page_streams_bodies_and_keeps_correct_order(monkeypatch, offset):
    def items(*args, **kwargs):
        for index in range(1000):
            yield {'id': str(index), 'summary': 'x' * 65536, 'published_ts': index,
                   'saved_at': index, 'attention': {'rank': index % 7},
                   'analysis_status': 'pending', 'interpretation_qualified': False}
    news_store.init_db()
    with news_store._connect() as conn:
        conn.executemany("INSERT INTO articles(id,title,link,summary,published_ts,saved_at) VALUES(?,?,?,?,?,?)",
                         [(str(i), f"Headline {i}", f"https://example.com/{i}", "fixture", i, i) for i in range(1000)])
    monkeypatch.setattr(news_store, '_iter_stock_items', items)
    tracemalloc.start()
    try:
        page, total = news_store.list_archive(symbol='600519.SS', sort='importance', limit=10, offset=offset)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    expected = sorted(range(1000), key=lambda i: (i % 7, i, i, str(i)), reverse=True)[offset:offset+10]
    assert [item['id'] for item in page] == [str(i) for i in expected]
    assert total == 1000
    assert peak < 4 * 1024 ** 2
