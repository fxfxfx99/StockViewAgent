"""雪球会话错误、缓存期限和自动更新时的请求上限回归测试。"""
import httpx
import pytest

from app.services import xueqiu_http as xh


@pytest.fixture
def session(monkeypatch):
    state = {"now": 1000.0, "raw": "", "calls": []}
    for name, value in {
        "_warmed_cookies": {},
        "_anonymous_expires_at": 0.0,
        "_anonymous_retry_after": 0.0,
        "_rejected_cookie_key": None,
        "_rejected_until": 0.0,
    }.items():
        monkeypatch.setattr(xh, name, value)
    monkeypatch.setattr(xh.time, "monotonic", lambda: state["now"])
    monkeypatch.setattr(xh.time, "sleep", lambda _: None)
    monkeypatch.setattr(xh, "_throttle", lambda: None)
    monkeypatch.setattr(xh, "effective_xueqiu_cookies", lambda: state["raw"])
    monkeypatch.setattr(xh.settings, "xueqiu_max_retries", 0)
    client_type = httpx.Client

    def install(handler):
        def record(request):
            state["calls"].append(request)
            return handler(request)

        transport = httpx.MockTransport(record)
        monkeypatch.setattr(xh.httpx, "Client", lambda **kwargs: client_type(transport=transport, **kwargs))

    state["install"] = install
    return state


def test_anonymous_rejection_clears_token_and_cools_down(session):
    count = {"warmup": 0, "api": 0}

    def handler(request):
        if request.url.path == "/":
            count["warmup"] += 1
            return httpx.Response(200, headers={"set-cookie": f"xq_a_token=anon{count['warmup']}; Path=/"})
        count["api"] += 1
        if count["api"] == 1:
            return httpx.Response(400, json={"error_code": 400016})
        assert "xq_a_token=anon2" in request.headers["cookie"]
        return httpx.Response(200, json={"data": {"current": 10}})

    session["install"](handler)
    data, error = xh.request_json("GET", "https://xueqiu.com/quote")
    assert data is None
    assert xh.auth_error_status(error) == "missing"
    assert isinstance(error, str)
    assert xh._warmed_cookies == {}
    for _ in range(5):
        assert xh.auth_error_status(xh.request_json("GET", "https://xueqiu.com/quote")[1]) == "missing"
    assert count == {"warmup": 1, "api": 1}
    session["now"] += xh._SESSION_RETRY_SEC + 1
    assert xh.request_json("GET", "https://xueqiu.com/quote")[1] is None
    assert count == {"warmup": 2, "api": 2}


def test_updated_configured_cookie_bypasses_rejected_session_cooldown(session):
    session["raw"] = "xq_a_token=old-synthetic-token"

    def handler(request):
        assert request.url.path == "/quote"
        if "old-synthetic-token" in request.headers["cookie"]:
            return httpx.Response(400, json={"error_code": "400016"})
        return httpx.Response(200, json={"data": {"current": 10}})

    session["install"](handler)
    _, error = xh.request_json("GET", "https://xueqiu.com/quote")
    assert xh.auth_error_status(error) == "expired"
    assert "old-synthetic-token" not in error
    assert "匿名" not in error
    xh.request_json("GET", "https://xueqiu.com/quote")
    assert len(session["calls"]) == 1
    session["raw"] = "xq_a_token=new-synthetic-token"
    assert xh.request_json("GET", "https://xueqiu.com/quote")[1] is None
    assert len(session["calls"]) == 2


def test_anonymous_cookie_is_refreshed_after_ttl(session):
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        return httpx.Response(200, headers={"set-cookie": f"xq_a_token=anon{count}; Path=/"})

    session["install"](handler)
    assert xh.effective_cookie_dict()["xq_a_token"] == "anon1"
    assert xh.effective_cookie_dict()["xq_a_token"] == "anon1"
    session["now"] += xh._ANONYMOUS_TTL_SEC + 1
    assert xh.effective_cookie_dict()["xq_a_token"] == "anon2"
    assert count == 2


def test_failed_warmup_is_cooled_down(session):
    def handler(request):
        raise httpx.ConnectError("offline", request=request)

    session["install"](handler)
    assert xh.effective_cookie_dict() == {}
    assert xh.effective_cookie_dict() == {}
    assert len(session["calls"]) == 1
    session["now"] += xh._SESSION_RETRY_SEC + 1
    assert xh.effective_cookie_dict() == {}
    assert len(session["calls"]) == 2


def test_partial_configured_cookie_does_not_attempt_anonymous_login(session):
    session["raw"] = "u=synthetic-id"
    assert xh.effective_cookie_dict() == {"u": "synthetic-id"}
    assert not session["calls"]


@pytest.mark.parametrize("retries", [0, 2])
def test_connection_retry_setting_including_zero(session, monkeypatch, retries):
    session["raw"] = "xq_a_token=synthetic-token"
    monkeypatch.setattr(xh.settings, "xueqiu_max_retries", retries)

    def handler(request):
        raise httpx.ConnectError("offline", request=request)

    session["install"](handler)
    assert xh.request_json("GET", "https://xueqiu.com/quote") == (None, "雪球网络连接失败，请稍后重试。")
    assert len(session["calls"]) == retries + 1


@pytest.mark.parametrize("configured", [False, True])
def test_http_401_is_a_session_error(session, configured):
    session["raw"] = "xq_a_token=synthetic-token" if configured else ""

    def handler(request):
        if request.url.path == "/":
            return httpx.Response(200, headers={"set-cookie": "xq_a_token=anonymous; Path=/"})
        return httpx.Response(401, text="Login required")

    session["install"](handler)
    _, error = xh.request_json("GET", "https://xueqiu.com/quote")
    assert xh.auth_error_status(error) == ("expired" if configured else "missing")


def test_waf_response_does_not_claim_cookie_is_expired(session):
    session["raw"] = "xq_a_token=synthetic-token"
    session["install"](lambda _: httpx.Response(403, text="<html>Forbidden</html>"))
    _, error = xh.request_json("GET", "https://xueqiu.com/quote")
    assert "403" in error
    assert xh.auth_error_status(error) is None
    assert "Cookie" not in error


def test_success_code_string_zero_and_other_business_errors():
    success = {"error_code": "0", "data": {"current": 10}}
    assert xh._parse_xueqiu_body(success) == (success, None)
    _, error = xh._parse_xueqiu_body({"error_code": 500001, "error_description": "稍后重试"})
    assert "500001" in error
    assert xh.auth_error_status(error) is None


def test_per_request_budget_overrides_retries_and_bounds_anonymous_warmup(session, monkeypatch):
    monkeypatch.setattr(xh.settings, "xueqiu_max_retries", 2)
    monkeypatch.setattr(xh.settings, "xueqiu_timeout_sec", 25)

    def handler(request):
        assert all(value == 10 for value in request.extensions["timeout"].values())
        raise httpx.ReadTimeout("upstream timeout", request=request)

    session["install"](handler)
    assert xh.request_json("GET", "https://xueqiu.com/quote", timeout_sec=10, max_retries=0) == (None, "雪球请求超时，请稍后重试。")
    assert [request.url.path for request in session["calls"]] == ["/", "/quote"]
    # 首页失败也进入冷却，面板再次尝试时不消耗第二次预热预算。
    xh.request_json("GET", "https://xueqiu.com/quote", timeout_sec=10, max_retries=0)
    assert [request.url.path for request in session["calls"]] == ["/", "/quote", "/quote"]


def test_default_request_timeout_is_preserved(session, monkeypatch):
    session["raw"] = "xq_a_token=synthetic-token"
    monkeypatch.setattr(xh.settings, "xueqiu_timeout_sec", 25)
    session["install"](lambda _: httpx.Response(200, json={"data": {}}))
    xh.request_json("GET", "https://xueqiu.com/quote")
    assert all(value == 25 for value in session["calls"][0].extensions["timeout"].values())


def test_company_stages_use_the_bounded_request_budget(monkeypatch):
    from app.services import xueqiu_pipeline as pipeline

    calls = []

    def request(method, url, **kwargs):
        calls.append(kwargs)
        return {"data": {}, "list": []}, None

    monkeypatch.setattr(xh, "request_json", request)
    pipeline.stage_fetch_company_f10("SH600519")
    pipeline.stage_fetch_major_events("SH600519")
    pipeline.stage_fetch_stock_timeline("SH600519", source="自选股新闻")
    assert len(calls) == 3
    assert all(call["timeout_sec"] == 10 and call["max_retries"] == 0 for call in calls)


def test_protocol_error_does_not_expose_cookie_header_in_bundle(session):
    from app.services import xueqiu_pipeline as pipeline

    session["raw"] = "xq_a_token=synthetic-private-cookie\ninvalid"

    def handler(request):
        # httpx/h11 实际会把非法请求头放在 LocalProtocolError 的错误消息中。
        raise httpx.LocalProtocolError(
            f"Illegal header value {request.headers['cookie']!r}", request=request,
        )

    session["install"](handler)
    result = pipeline.run_bundle("600519.SS")
    assert result["errors"] == ["雪球请求格式无效，请检查配置台的 Cookie 格式。"]
    rendered = str(result)
    assert "synthetic-private-cookie" not in rendered
    assert "xq_a_token" not in rendered
    assert "Illegal header value" not in rendered


@pytest.mark.parametrize("error_type, expected", [
    (httpx.ConnectError, "雪球网络连接失败，请稍后重试。"),
    (httpx.ReadTimeout, "雪球请求超时，请稍后重试。"),
    (httpx.RemoteProtocolError, "雪球响应协议异常，请稍后重试。"),
])
def test_network_error_messages_do_not_echo_request_details(session, error_type, expected):
    session["raw"] = "xq_a_token=synthetic-token"

    def handler(request):
        raise error_type("synthetic-sensitive-header-detail", request=request)

    session["install"](handler)
    assert xh.request_json("GET", "https://xueqiu.com/quote") == (None, expected)
