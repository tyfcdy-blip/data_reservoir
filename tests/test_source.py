"""仅模拟HTTP外部边界，实际执行协议、资源边界和限速逻辑。"""

from datetime import date
from decimal import Decimal
import json

import httpx
from pydantic import ValidationError
import pytest

from data_service.config import Settings
from data_service.source import SourceClient, SourceConfig, SourceError, generate_sign, load_sources

DAY = date(2026, 10, 1)


def settings() -> Settings:
    return Settings(database_url="postgresql://unused/unused_test", allowed_source_hosts=["source.test"])


def config(**changes) -> SourceConfig:
    return SourceConfig(url="https://source.test/data", dataset_slug="products", **changes)


class Clock:
    def __init__(self):
        self.now = 0.0
        self.waits = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.waits.append(seconds)
        self.now += seconds


def test_generic_get_parameters_mapping_decimal_and_short_page():
    requests = []

    def respond(request):
        requests.append(request)
        page = int(request.url.params["page"])
        rows = [{"raw": {"id": "001", "amount": Decimal("12.12")}, "unused": "ignored"}] if page < 3 else []
        body = json.dumps({"result": {"rows": rows}, "ok": True}, default=float)
        return httpx.Response(200, content=body)

    clock = Clock()
    with SourceClient(config(method="GET", parameter_location="query", page_start=1, page_param="page",
                             page_size=200, rows_path="result.rows", success_field="ok", success_value=True,
                             static_params={"tenant": "x"}, field_mapping={"id": "raw.id", "amount": "raw.amount"}),
                      settings(), transport=httpx.MockTransport(respond), monotonic=clock.monotonic, sleep=clock.sleep) as client:
        pages = list(client.iter_pages(DAY))
    assert len(pages) == 3 and pages[-1] == []
    assert pages[0] == [{"id": "001", "amount": Decimal("12.12")}]
    assert requests[0].url.params["dt"] == "2026-10-01"
    assert requests[0].url.params["tenant"] == "x"
    assert requests[0].headers["Accept-Encoding"] == "identity"


@pytest.mark.parametrize("page_start", [0, 1])
def test_vendor_post_signature(monkeypatch, page_start):
    monkeypatch.setenv("SRC_KEY", "key-test")
    monkeypatch.setenv("SRC_SECRET", "secret-test")
    observed = []

    def respond(request):
        observed.append(request)
        return httpx.Response(200, json={"success": True, "code": "200", "data": [], "extraInfo": None})

    with SourceClient(config(protocol="vendor_batch", vendor_api_id="api123", vendor_app_key_env="SRC_KEY",
                             vendor_app_secret_env="SRC_SECRET", page_start=page_start), settings(), transport=httpx.MockTransport(respond)) as client:
        assert list(client.iter_pages(DAY)) == [[]]
    request = observed[0]
    assert json.loads(request.content) == {"apiId": "api123", "pageIndex": page_start, "pageSize": 200,
                                           "batchQueryParams": [{"openFieldName": "dt", "openFieldValue": "2026-10-01"}]}
    assert request.headers["sign"] == generate_sign("secret-test", request.headers["timestamp"])


def test_vendor_one_based_pagination_does_not_request_zero_again(monkeypatch):
    monkeypatch.setenv("SRC_KEY", "key-test")
    monkeypatch.setenv("SRC_SECRET", "secret-test")
    pages_requested = []

    def respond(request):
        number = json.loads(request.content)["pageIndex"]
        pages_requested.append(number)
        # 实际供应商将0和1都视为首页；配置1后必须从第二页继续，不能重复首页。
        rows = [{"product_day_id": "2026-10-01_001"}] if number <= 1 else (
            [{"product_day_id": "2026-10-01_002"}] if number == 2 else [])
        return httpx.Response(200, json={"success": True, "code": "200", "data": rows})

    with SourceClient(config(protocol="vendor_batch", vendor_api_id="api123", vendor_app_key_env="SRC_KEY",
                             vendor_app_secret_env="SRC_SECRET", page_start=1), settings(),
                      transport=httpx.MockTransport(respond), sleep=lambda _: None) as client:
        rows = [row for page in client.iter_pages(DAY) for row in page]
    assert pages_requested == [1, 2, 3]
    assert [row["product_day_id"] for row in rows] == ["2026-10-01_001", "2026-10-01_002"]


@pytest.mark.parametrize("changes", [
    {"url": "https://user:pass@source.test/a"}, {"url": "https://source.test/a?secret=1"},
    {"url": "https://source.test/a#x"}, {"url": "file:///tmp/a"}, {"url": "https://source.test:0/a"},
    {"url": "https://source.test/\n"}, {"method": "GET"}, {"protocol": "unknown"},
    {"page_size": 1001}, {"page_start": 2}, {"page_param": "dt"}, {"static_params": {"pageSize": 1}},
    {"static_params": {"api_token": "oops"}}, {"header_env": {"Host": "SRC"}},
    {"header_env": {"Authorization": "BAD-VAR"}}, {"rows_path": "a[*]"},
    {"success_field": "ok"}, {"protocol": "vendor_batch"},
    {"vendor_api_id": "bad"}, {"requests_per_second": 21},
])
def test_invalid_source_config(changes):
    payload = {"url": "https://source.test/data", "dataset_slug": "products", **changes}
    with pytest.raises(ValidationError):
        SourceConfig(**payload)


def test_host_and_secret_fail_closed(monkeypatch):
    with pytest.raises(SourceError, match="白名单"):
        SourceClient(config(), Settings(database_url="postgresql://unused/unused_test"))
    monkeypatch.delenv("SOURCE_MISSING", raising=False)
    with SourceClient(config(header_env={"Authorization": "SOURCE_MISSING"}), settings()) as client:
        with pytest.raises(SourceError) as caught:
            client.query_page(DAY, 0)
    assert caught.value.code == "SOURCE_SECRET_MISSING"


@pytest.mark.parametrize("content,code", [
    (b"nojson", "INVALID_RESPONSE"), (b'{"data":[],"data":[]}', "INVALID_RESPONSE"),
    (b'{"data":[{"x":NaN}]}', "INVALID_RESPONSE"), (b'{"other":[]}', "RESPONSE_SCHEMA_CHANGED"),
    (b'{"data":{}}', "RESPONSE_SCHEMA_CHANGED"), (b'{"data":[1]}', "RESPONSE_SCHEMA_CHANGED"),
])
def test_invalid_responses(content, code):
    with SourceClient(config(), settings(), transport=httpx.MockTransport(lambda _: httpx.Response(200, content=content))) as client:
        with pytest.raises(SourceError) as caught:
            client.query_page(DAY, 0)
    assert caught.value.code == code


def test_wrong_success_marker_type_and_missing_mapping():
    with SourceClient(config(success_field="ok", success_value=True), settings(),
                      transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"ok": 1, "data": []}))) as client:
        with pytest.raises(SourceError) as caught:
            client.query_page(DAY, 0)
    assert caught.value.code == "UPSTREAM_REJECTED"
    with SourceClient(config(field_mapping={"id": "nested.id"}), settings(),
                      transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"data": [{}]}))) as client:
        with pytest.raises(SourceError) as caught:
            client.query_page(DAY, 0)
    assert caught.value.code == "RESPONSE_SCHEMA_CHANGED"


@pytest.mark.parametrize("status", [301, 302, 307, 400, 401, 403, 404])
def test_no_redirect_or_client_error_retry(status):
    seen = []

    def response(request):
        seen.append(request)
        return httpx.Response(status, headers={"Location": "http://another.invalid/secret"})

    with SourceClient(config(), settings(), transport=httpx.MockTransport(response)) as client:
        with pytest.raises(SourceError) as caught:
            client.query_page(DAY, 0)
    assert caught.value.code == "UPSTREAM_HTTP_ERROR" and len(seen) == 1


@pytest.mark.parametrize("failure", [429, 500, 502, 503, 504, "timeout", "network"])
def test_retry_is_bounded(failure):
    seen = []
    clock = Clock()

    def response(request):
        seen.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("contains-secret-url", request=request)
        if failure == "network":
            raise httpx.ConnectError("contains-secret-url", request=request)
        return httpx.Response(failure, headers={"Retry-After": "2"})

    with SourceClient(config(max_retries=2), settings(), transport=httpx.MockTransport(response),
                      monotonic=clock.monotonic, sleep=clock.sleep) as client:
        with pytest.raises(SourceError) as caught:
            client.query_page(DAY, 0)
    assert len(seen) == 3 and clock.now >= 3
    assert "secret" not in str(caught.value)


def test_rate_limit_reused_across_days_and_retry():
    clock = Clock()
    times = []

    def response(request):
        times.append(clock.now)
        return httpx.Response(503 if len(times) == 2 else 200, json={"data": []})

    with SourceClient(config(requests_per_second=2), settings(), transport=httpx.MockTransport(response),
                      monotonic=clock.monotonic, sleep=clock.sleep) as client:
        for day in [DAY, date(2026, 10, 2), date(2026, 10, 3)]:
            assert list(client.iter_pages(day)) == [[]]
    assert len(times) == 4
    assert all(right - left >= .5 for left, right in zip(times, times[1:], strict=False))


def test_size_encoding_page_limit_and_page_count():
    for response, expected in [(httpx.Response(200, content=b"x" * 1025), "RESPONSE_TOO_LARGE"),
                               (httpx.Response(200, headers={"Content-Encoding": "gzip"}, stream=httpx.ByteStream(b"{}")), "RESPONSE_ENCODING_UNSUPPORTED")]:
        with SourceClient(config(max_response_bytes=1024), settings(), transport=httpx.MockTransport(lambda _, r=response: r)) as client:
            with pytest.raises(SourceError) as caught:
                client.query_page(DAY, 0)
        assert caught.value.code == expected
    clock = Clock()
    with SourceClient(config(max_pages=2), settings(), transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"data": [{"id": 1}]})),
                      monotonic=clock.monotonic, sleep=clock.sleep) as client:
        with pytest.raises(SourceError) as caught:
            list(client.iter_pages(DAY))
    assert caught.value.code == "PAGE_LIMIT_REACHED"
    with SourceClient(config(page_size=1), settings(), transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"data": [{}, {}]}))) as client:
        with pytest.raises(SourceError) as caught:
            client.query_page(DAY, 0)
    assert caught.value.code == "RESPONSE_SCHEMA_CHANGED"


def test_load_sources_strict_file(tmp_path):
    path = tmp_path / "sources.json"
    local = settings().model_copy(update={"source_config_path": str(path)})
    with pytest.raises(SourceError) as caught:
        load_sources(local)
    assert caught.value.code == "SOURCE_CONFIG_MISSING"
    path.write_text('{"sources":[]}', encoding="utf-8")
    assert load_sources(local) == {}
    path.write_text(json.dumps({"sources": [config().model_dump(), config().model_dump()]}), encoding="utf-8")
    with pytest.raises(SourceError) as caught:
        load_sources(local)
    assert caught.value.code == "SOURCE_CONFIG_INVALID"
    path.write_text('{"sources":[],"unexpected":1}', encoding="utf-8")
    with pytest.raises(SourceError):
        load_sources(local)


def test_vendor_unknown_protocol_and_field_type(monkeypatch):
    monkeypatch.setenv("KEY", "test-key")
    monkeypatch.setenv("SECRET", "test-secret")
    vendor = config(protocol="vendor_batch", vendor_api_id="api", vendor_app_key_env="KEY", vendor_app_secret_env="SECRET")
    for body, code in [({"success": True, "code": 200, "data": []}, "UPSTREAM_REJECTED"),
                       ({"success": True, "code": "200", "data": [], "newProtocol": 1}, "RESPONSE_SCHEMA_CHANGED")]:
        with SourceClient(vendor, settings(), transport=httpx.MockTransport(lambda _, b=body: httpx.Response(200, json=b))) as client:
            with pytest.raises(SourceError) as caught:
                client.query_page(DAY, 0)
        assert caught.value.code == code


@pytest.mark.parametrize("raw,expected", [
    ("2026-10-01 00:00:00", "2026-10-01"), ("2026-09-30T17:00:00Z", "2026-10-01"),
    ("2026-10-01T00:00:00+08:00", "2026-10-01"), ("2026-10-01", "2026-10-01"), (None, None),
])
def test_explicit_datetime_to_date(raw, expected):
    with SourceClient(config(date_fields_from_datetime=["dt"]), settings(),
                      transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"data": [{"dt": raw}]}))) as client:
        assert client.query_page(DAY, 0) == [{"dt": expected}]


@pytest.mark.parametrize("raw", ["2026-02-31", "2026/10/01", "yesterday", 1790776800, "2026-10-01T99:00:00"])
def test_invalid_explicit_date_rejected(raw):
    with SourceClient(config(date_fields_from_datetime=["dt"]), settings(),
                      transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"data": [{"dt": raw}]}))) as client:
        with pytest.raises(SourceError) as caught:
            client.query_page(DAY, 0)
    assert caught.value.code == "INVALID_SOURCE_DATE"


def test_datetime_not_transformed_without_explicit_configuration():
    raw = "2026-10-01 00:00:00"
    with SourceClient(config(), settings(),
                      transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"data": [{"dt": raw}]}))) as client:
        assert client.query_page(DAY, 0) == [{"dt": raw}]


def test_explicit_timezone_and_bad_timezone():
    with SourceClient(config(date_fields_from_datetime=["dt"], source_timezone="UTC"), settings(),
                      transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"data": [{"dt": "2026-10-01T00:00:00+08:00"}]}))) as client:
        assert client.query_page(DAY, 0) == [{"dt": "2026-09-30"}]
    with pytest.raises(ValidationError):
        config(source_timezone="not/a-zone")
