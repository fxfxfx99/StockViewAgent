"""Control-plane contracts, paid-call fencing, and frozen research evidence."""
import asyncio
from datetime import datetime
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import settings
from app.deps.auth import get_current_user
from app.research_worker import run_one, schedule_due
from app.routers.research import router
from app.schemas.research import StrategySpecV1
from app.security.user_context import current_user_id
from app.services import research_generation as generation, research_service as service
from app.services.market_time import SHANGHAI_TZ
from app.storage import research_store as store


@pytest.fixture
def anyio_backend():
    return "asyncio"


def spec(**changes):
    return StrategySpecV1.model_validate({
        'name': 'Test rule', 'hypothesis': 'Test momentum', 'applicability': 'daily',
        'failure_conditions': ['sideways'], 'entry': {'op': 'gt', 'args': [
            {'op': 'field', 'field': 'c'}, {'op': 'const', 'value': 10}]}, **changes,
    }).model_dump(mode='json')


def llm_settings(monkeypatch):
    for name, value in [('kline_llm_key', 'test-only'), ('kline_llm_base', 'https://model.example/v1'), ('kline_llm_model', 'test-model')]:
        monkeypatch.setattr(type(settings), name, property(lambda self, v=value: v))


def test_model_prompt_never_contains_holdout_or_forward_evidence():
    messages = generation._messages(1, {'goal': 'research', 'holdout': 'SECRET_HOLDOUT',
        'forward_metrics': 'SECRET_FORWARD', 'validation_feedback': [{'status': 'failed'}]})
    assert 'SECRET' not in str(messages)
    assert 'failed' in str(messages)


@pytest.mark.anyio
async def test_paid_dispatch_is_single_and_response_is_replayable(monkeypatch):
    llm_settings(monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    requests = []
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, **kwargs):
            requests.append(kwargs)
            entered.set()
            await release.wait()
            return httpx.Response(200, request=httpx.Request('POST', url), json={
                'choices': [{'message': {'content': store.canonical_json(spec())}}],
                'usage': {'total_tokens': 1000}})
    monkeypatch.setattr(generation.httpx, 'AsyncClient', Client)
    first = asyncio.create_task(generation.generate_candidate(1, {}, attempt_key='one'))
    await entered.wait()
    second = await generation.generate_candidate(1, {}, attempt_key='one')
    assert second['status'] == 'in_progress'
    release.set()
    result = await first
    assert result['status'] == 'generated'
    assert await generation.generate_candidate(1, {}, attempt_key='one') == result
    assert len(requests) == 1
    assert store.budget_status(1)['used'] == {'calls': 1, 'tokens': 1000, 'candidates': 1}
    assert store.get_entity(1, 'proposal_request', 'one')['payload']['prompt_version']


@pytest.mark.anyio
async def test_budget_exhaustion_releases_dispatch_without_network(monkeypatch):
    llm_settings(monkeypatch)
    store.update_config(1, {'daily_calls': 0})
    with pytest.raises(store.BudgetExceeded):
        await generation.generate_candidate(1, {}, attempt_key='budget')
    assert store.get_state(1, 'dispatch:budget') is None
    assert store.get_entity(1, 'proposal_request', 'budget') is None
    assert store.budget_status(1)['used']['calls'] == 0


@pytest.mark.anyio
async def test_interrupted_paid_request_is_not_resent(monkeypatch):
    llm_settings(monkeypatch)
    store.put_entity(1, 'proposal_request', {'model': 'test'}, 'interrupted')
    result = await generation.generate_candidate(1, {}, attempt_key='interrupted')
    assert result['status'] == 'interrupted'
    assert store.budget_status(1)['used']['calls'] == 0


def test_campaign_freezes_data_financials_and_protocol(monkeypatch):
    pool = store.put_entity(1, 'pool_version', {'symbols': ['600000.SS']})
    old = {'id': 'snapshot-old', 'payload': {'pool_version_id': pool['id'], 'symbols': ['600000.SS']}}
    newer = {**old, 'id': 'snapshot-new'}
    monkeypatch.setattr(service, 'snapshot_bars', lambda *a: {})
    monkeypatch.setattr(service.evaluation, 'build_research_splits', lambda *a, **kw: {'eligible': False, 'issues': ['short']})
    first = service._campaign(1, old, 'original goal')
    second = service._campaign(1, newer, 'changed goal')
    assert first == second
    assert first['payload']['snapshot_id'] == 'snapshot-old'
    assert first['payload']['financial_config']['initial_cash'] == 100000
    store.update_config(1, {'initial_cash': 200000})
    assert service._campaign(1, newer, 'new capital')['id'] != first['id']


def test_failed_experiment_is_immutable_and_cash_is_frozen(monkeypatch):
    record = service.register_strategy(1, spec())
    snapshot = {'id': 'snap', 'payload': {'quality': {'eligible': True}, 'strict': True}}
    monkeypatch.setattr(service, 'snapshot_bars', lambda *args: {})
    def fail(*args, **kwargs):
        assert kwargs['initial_cash'] == 70000
        raise RuntimeError('sensitive provider detail')
    monkeypatch.setattr(service.evaluation, 'evaluate_candidate', fail)
    result = service._experiment(1, record, snapshot, initial_cash=70000)
    assert result['payload']['status'] == 'failed'
    assert 'sensitive' not in str(result)
    store.update_config(1, {'initial_cash': 90000})
    assert service._experiment(1, record, snapshot, initial_cash=70000) == result
    assert store.get_entity(1, 'experiment_request', result['id'])
    assert store.get_entity(2, 'experiment', result['id']) is None


def test_holdout_runs_only_after_freeze_and_only_once(monkeypatch):
    record = service.register_strategy(1, spec())
    calls = []
    def holdout(*args, **kwargs):
        calls.append(kwargs)
        assert store.get_state(1, 'holdout_access')['strategy_id'] == record['id']
        return {'eligible': False, 'issues': ['negative return']}
    monkeypatch.setattr(service.evaluation, 'evaluate_holdout', holdout)
    monkeypatch.setattr(service, 'snapshot_bars', lambda *args: {})
    experiment = store.put_entity(1, 'experiment', {'strategy_id': record['id'], 'code_hash': service._code_digest(),
        'initial_cash': 100000, 'report': {'eligible': True, 'rank_key': [1, -2, -3]}})
    campaign = {'id': 'batch', 'payload': {'snapshot_id': 'snap', 'symbols': ['600000.SS'],
        'needs_holdout': True, 'splits': {'holdout': {'start': 1, 'end': 2}}}}
    first = service.freeze_best_candidate(1, campaign, [experiment['id']])
    assert first['status'] == 'holdout_rejected'
    assert service.freeze_best_candidate(1, campaign, [experiment['id']]) == first
    assert len(calls) == 1
    assert not store.get_state(1, 'paper_active')


@pytest.mark.anyio
async def test_worker_scope_retry_and_duplicate_claim(monkeypatch):
    seen = []
    async def dispatch(job):
        seen.append(current_user_id.get())
        return {'status': 'finished'}
    monkeypatch.setattr('app.research_worker.dispatch', dispatch)
    queued = store.queue_job(9, 'paper', {}, 'once')
    before = current_user_id.get()
    assert await run_one('worker')
    assert current_user_id.get() == before
    assert seen == [9]
    assert not await run_one('worker')
    assert store.get_job(9, queued['id'])['status'] == 'completed'


def test_paused_research_keeps_collection_and_deterministic_evaluation():
    store.queue_job(1, 'research', {}, 'generation')
    store.queue_job(1, 'collect', {}, 'collect')
    store.update_config(1, {'paused': True})
    job = store.claim_job('worker')
    assert job['kind'] == 'collect'
    store.complete_job(1, job['id'], job['lease_token'], {})
    assert store.claim_job('worker') is None


def test_schedule_resumes_without_duplicate_jobs(monkeypatch):
    monkeypatch.setattr(settings, "enable_research_worker", True)
    monkeypatch.setattr('app.research_worker.users_store.list_users', lambda: [SimpleNamespace(id=1), SimpleNamespace(id=2)])
    (settings.data_dir / 'watchlists').mkdir()
    (settings.data_dir / 'watchlists' / '1.json').write_text('[]')
    now = datetime(2026, 10, 19, 17, tzinfo=SHANGHAI_TZ)
    first = schedule_due(now)
    second = schedule_due(now)
    assert {j['id'] for j in first} == {j['id'] for j in second}
    assert {j['kind'] for j in first} == {'collect', 'source_maintenance', 'github', 'watch_review'}
    assert {j['kind'] for j in store.list_jobs(2)} == {'collect', 'source_maintenance', 'github', 'watch_review'}
    assert not any(j['kind'] == 'collect' for j in store.list_jobs(1))  # explicit empty list


def test_api_validation_ownership_and_no_model_in_request():
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=1)
    with TestClient(app) as client:
        created = client.post('/api/research/strategies', json={'spec': spec()})
        assert created.status_code == 201
        strategy_id = created.json()['id']
        queued = client.post('/api/research/runs', json={'kind': 'evaluate', 'payload': {'strategy_id': strategy_id}, 'idempotency_key': 'queue'})
        assert queued.status_code == 202
        assert queued.json()['status'] == 'queued'
        assert store.budget_status(1)['used']['calls'] == 0
        assert client.post('/api/research/runs', json={'payload': {'candidate_count': -1}}).status_code == 400
        assert client.patch('/api/research/config', json={'min_sharpe': -99}).status_code == 400
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=2)
        assert client.get('/api/research/strategies/' + strategy_id).status_code == 404
        assert client.get('/api/research/jobs/' + queued.json()['id']).status_code == 404
        assert client.get('/api/research/overview').json()['strategies'] == []


def test_morning_restart_catchup_does_not_consume_evening_tick(monkeypatch):
    monkeypatch.setattr(settings, "enable_research_worker", True)
    monkeypatch.setattr('app.research_worker.users_store.list_users', lambda: [SimpleNamespace(id=1)])
    (settings.data_dir / 'watchlists').mkdir()
    (settings.data_dir / 'watchlists' / '1.json').write_text('{"symbols": ["600519.SS"]}')
    morning = schedule_due(datetime(2026, 10, 19, 9, tzinfo=SHANGHAI_TZ), startup=True)
    assert {j['kind'] for j in morning} == {'collect', 'source_maintenance', 'github', 'watch_review'}
    collected = store.claim_job('test-worker', user_id=1, kinds=['collect'])
    store.complete_job(1, collected['id'], collected['lease_token'], {'status': 'collected'})
    evening = schedule_due(datetime(2026, 10, 19, 17, tzinfo=SHANGHAI_TZ))
    assert next(j['id'] for j in morning if j['kind'] == 'collect') != next(j['id'] for j in evening if j['kind'] == 'collect')


@pytest.mark.anyio
async def test_lease_loss_drains_thread_before_releasing_writer(monkeypatch):
    import threading
    import app.research_worker as worker
    entered, release, wrote = threading.Event(), threading.Event(), threading.Event()
    def blocking_write():
        entered.set()
        release.wait(3)
        wrote.set()
    async def dispatch(job):
        await asyncio.to_thread(blocking_write)
        return {'done': True}
    def lose(*args, **kwargs):
        raise store.LeaseLost('lost')
    monkeypatch.setattr(worker, 'dispatch', dispatch)
    monkeypatch.setattr(worker, 'LEASE_RENEW_INTERVAL', .01)
    monkeypatch.setattr(worker.store, 'renew_job', lose)
    store.queue_job(1, 'paper', {}, 'lease-drain')
    task = asyncio.create_task(worker.run_one('worker'))
    assert await asyncio.to_thread(entered.wait, 1)
    await asyncio.sleep(.04)
    assert not task.done(), 'The writer lock must outlive an active thread'
    release.set()
    with pytest.raises(store.LeaseLost):
        await task
    assert wrote.is_set()
    assert store.list_jobs(1)[0]['status'] == 'running'


def test_import_rejects_null_or_nonobject_candles_without_500():
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=1)
    with TestClient(app) as client:
        for invalid in (None, 'data', [None], [1]):
            response = client.post('/api/research/import', json={'bundles': {'600000.SS': {'candles': invalid}}, 'metadata': {}})
            assert response.status_code == 400


def test_published_strategy_requires_research_rationale_and_tracks_origin():
    with pytest.raises(ValueError, match='研究假设'):
        service.register_strategy(1, spec(hypothesis=''))
    record = service.register_strategy(1, spec())
    assert record['payload']['spec']['sources'] == ['local:manual']
    assert record['payload']['spec']['parent_version_id'] is None
