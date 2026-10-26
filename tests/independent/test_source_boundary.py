"""独立HTTP源边界：真实本机HTTP，非MockTransport。"""

from contextlib import contextmanager
from datetime import timedelta
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Thread
from time import monotonic

import pytest
from pydantic import ValidationError

from data_service.source import SourceClient, SourceConfig, SourceError


@contextmanager
def local_source(responder):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            requests.append((monotonic(), self.path, payload))
            status, headers, content = responder(payload, len(requests))
            self.send_response(status)
            for header, value in headers.items():
                self.send_header(header, value)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/source", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def source_config(url, **changes):
    return SourceConfig(dataset_slug="ind_http", url=url, page_size=2, requests_per_second=20,
                        max_retries=0, **changes)


def test_http_short_page_does_not_end_pagination(independent_settings, day):
    def response(payload, _count):
        page = payload["pageIndex"]
        records = [{"id": str(page), "nested": {"value": "0.100001"}}] if page < 2 else []
        return 200, {}, json.dumps({"result": {"rows": records}}).encode()

    settings = independent_settings.model_copy(update={"allowed_source_hosts": ["127.0.0.1"]})
    with local_source(response) as (url, requests):
        configuration = source_config(url, rows_path="result.rows", field_mapping={"code": "id", "amount": "nested.value"})
        with SourceClient(configuration, settings) as client:
            pages = list(client.iter_pages(day))
    assert pages == [[{"code": "0", "amount": "0.100001"}], [{"code": "1", "amount": "0.100001"}], []]
    assert [request[2]["pageIndex"] for request in requests] == [0, 1, 2]
    assert all(request[2]["dt"] == day.isoformat() and request[2]["pageSize"] == 2 for request in requests)


def test_http_numeric_json_preserves_decimal(independent_settings, day):
    settings = independent_settings.model_copy(update={"allowed_source_hosts": ["127.0.0.1"]})
    with local_source(lambda *_: (200, {}, b'{"data":[{"amount":12345678901234567890.123456}]}')) as (url, _):
        with SourceClient(source_config(url), settings) as client:
            rows = client.query_page(day, 0)
    assert rows[0]["amount"] == Decimal("12345678901234567890.123456")


@pytest.mark.parametrize("content,expected", [
    (b'{"data":[],"data":[{}]}', "INVALID_RESPONSE"),
    (b'{"data":[{"amount":NaN}]}', "INVALID_RESPONSE"),
    (b'{"different":[]}', "RESPONSE_SCHEMA_CHANGED"),
    (b'{"data":null}', "RESPONSE_SCHEMA_CHANGED"),
    (b'{"data":[{}, {}, {}]}', "RESPONSE_SCHEMA_CHANGED"),
])
def test_http_invalid_json_or_shape_rejected(independent_settings, day, content, expected):
    settings = independent_settings.model_copy(update={"allowed_source_hosts": ["127.0.0.1"]})
    with local_source(lambda *_: (200, {}, content)) as (url, _):
        with SourceClient(source_config(url), settings) as client, pytest.raises(SourceError) as failure:
            client.query_page(day, 0)
    assert failure.value.code == expected


def test_http_redirect_is_not_followed(independent_settings, day):
    settings = independent_settings.model_copy(update={"allowed_source_hosts": ["127.0.0.1"]})
    with local_source(lambda *_: (302, {"Location": "/forbidden"}, b"")) as (url, requests):
        with SourceClient(source_config(url), settings) as client, pytest.raises(SourceError) as failure:
            client.query_page(day, 0)
    assert failure.value.code == "UPSTREAM_HTTP_ERROR"
    assert len(requests) == 1


def test_http_limit_spans_dates(independent_settings, day):
    settings = independent_settings.model_copy(update={"allowed_source_hosts": ["127.0.0.1"]})
    with local_source(lambda *_: (200, {}, b'{"data":[]}')) as (url, requests):
        configuration = SourceConfig(dataset_slug="ind_http", url=url, requests_per_second=4)
        with SourceClient(configuration, settings) as client:
            for offset in range(4):
                assert list(client.iter_pages(day - timedelta(days=offset))) == [[]]
    # 服务端到达时刻受操作系统调度影响；真实HTTP验证总窗口，发起间隔另用可控单调时钟验证。
    assert requests[-1][0] - requests[0][0] >= 0.70


def test_client_dispatch_spacing_uses_shared_clock(independent_settings, day):
    import httpx

    settings = independent_settings.model_copy(update={"allowed_source_hosts": ["127.0.0.1"]})
    clock = [0.0]
    starts = []

    def transport(_request):
        starts.append(clock[0])
        return httpx.Response(200, json={"data": []})

    def advance(duration):
        clock[0] += duration

    configuration = SourceConfig(dataset_slug="ind_http", url="http://127.0.0.1/source", requests_per_second=4)
    with SourceClient(configuration, settings, transport=httpx.MockTransport(transport),
                      monotonic=lambda: clock[0], sleep=advance) as client:
        for offset in range(4):
            list(client.iter_pages(day - timedelta(days=offset)))
    assert starts == [0.0, 0.25, 0.5, 0.75]


@pytest.mark.parametrize("url", ["http://user:password@127.0.0.1/source", "http://127.0.0.1/source?token=secret", "http://127.0.0.1/source#fragment"])
def test_source_credentials_and_fragments_rejected(url):
    with pytest.raises(ValidationError):
        source_config(url)


def test_unlisted_hostname_is_rejected_before_network(independent_settings):
    with pytest.raises(SourceError) as failure:
        SourceClient(source_config("http://not-allowed.invalid/source"), independent_settings)
    assert failure.value.code == "SOURCE_HOST_NOT_ALLOWED"


@pytest.mark.parametrize("source_value,expected", [
    ("2026-10-20 00:00:00", "2026-10-20"),
    ("2026-10-20T18:00:00Z", "2026-10-21"),
    ("2026-10-20", "2026-10-20"),
])
def test_source_datetime_to_date_is_explicit_and_timezone_aware(independent_settings, day, source_value, expected):
    settings = independent_settings.model_copy(update={"allowed_source_hosts": ["127.0.0.1"]})
    with local_source(lambda *_: (200, {}, json.dumps({"data": [{"source_dt": source_value}]}).encode())) as (url, _):
        configuration = source_config(url, field_mapping={"dt": "source_dt"}, date_fields_from_datetime=["dt"], source_timezone="Asia/Shanghai")
        with SourceClient(configuration, settings) as client:
            assert client.query_page(day, 0) == [{"dt": expected}]


def test_datetime_not_transformed_without_configuration(independent_settings, day):
    settings = independent_settings.model_copy(update={"allowed_source_hosts": ["127.0.0.1"]})
    original = "2026-10-20 00:00:00"
    with local_source(lambda *_: (200, {}, json.dumps({"data": [{"dt": original}]}).encode())) as (url, _):
        with SourceClient(source_config(url), settings) as client:
            assert client.query_page(day, 0) == [{"dt": original}]


def test_upstream_retry_is_finite(independent_settings, day):
    settings = independent_settings.model_copy(update={"allowed_source_hosts": ["127.0.0.1"]})
    with local_source(lambda *_: (503, {}, b"unavailable")) as (url, requests):
        configuration = SourceConfig(dataset_slug="ind_http", url=url, max_retries=1)
        with SourceClient(configuration, settings) as client, pytest.raises(SourceError) as failure:
            client.query_page(day, 0)
    assert len(requests) == 2 and failure.value.code == "UPSTREAM_UNAVAILABLE"


def test_no_empty_page_before_page_limit_rejected(independent_settings, day):
    settings = independent_settings.model_copy(update={"allowed_source_hosts": ["127.0.0.1"]})
    with local_source(lambda *_: (200, {}, b'{"data":[{"id":"a"}]}')) as (url, requests):
        with SourceClient(source_config(url, max_pages=2), settings) as client, pytest.raises(SourceError) as failure:
            list(client.iter_pages(day))
    assert len(requests) == 2 and failure.value.code == "PAGE_LIMIT_REACHED"


def test_response_byte_limit_checked(independent_settings, day):
    settings = independent_settings.model_copy(update={"allowed_source_hosts": ["127.0.0.1"]})
    with local_source(lambda *_: (200, {}, json.dumps({"data": [{"text": "x" * 2048}]}).encode())) as (url, _):
        with SourceClient(source_config(url, max_response_bytes=1024), settings) as client, pytest.raises(SourceError) as failure:
            client.query_page(day, 0)
    assert failure.value.code == "RESPONSE_TOO_LARGE"
