import json

import pytest

from app.services import a_share_stocks


class _FakeResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {
            "data": {
                "diff": [
                    {"f12": "000001", "f14": "平安银行", "f13": 0},
                    {"f12": "600519", "f14": "贵州茅台", "f13": 1},
                ]
            }
        }


class _FakeClient:
    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get(self, *args, **kwargs):
        return _FakeResponse()


def test_fetch_and_save_rejects_too_small_result(monkeypatch, tmp_path):
    target = tmp_path / "a_share_stocks.json"
    target.write_text(
        json.dumps({"updated_at": 1, "count": 1200, "stocks": [{"code": "000001"}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(a_share_stocks, "_path", lambda: target)
    monkeypatch.setattr(a_share_stocks.httpx, "Client", _FakeClient)

    with pytest.raises(RuntimeError, match="返回数量异常"):
        a_share_stocks.fetch_and_save()

    saved = json.loads(target.read_text(encoding="utf-8"))
    assert saved["count"] == 1200
