"""路径适配使用真实 ASGI 路由；不连接业务库或请求外部数据源。"""

from fastapi.testclient import TestClient
import httpx
from pydantic import ValidationError
import pytest
import socket
import threading
import time
import uvicorn

from data_service.api import create_app
from data_service.config import Settings


def settings(prefix: str) -> Settings:
    return Settings(database_url="postgresql://unused:unused@127.0.0.1:1/unused", root_path=prefix)


@pytest.mark.parametrize(("value", "expected"), [
    ("", ""), ("/", ""), ("/data-service/", "/data-service"),
    ("/apps/data_service", "/apps/data_service"),
])
def test_prefix_normalization(value, expected):
    assert settings(value).root_path == expected


@pytest.mark.parametrize("value", [
    "//evil.test", "https://evil.test", "data-service", "/a//b", "/a/../b",
    "/a%2fb", "/a?x=1", "/a#b", '/a"b', "/a\\b", "/a\nb", "/a b", "/" + "a" * 201,
])
def test_invalid_prefix_rejected(value):
    with pytest.raises(ValidationError):
        settings(value)


@pytest.mark.parametrize("prefix", ["", "/data-service", "/apps/data-service"])
def test_paths_assets_docs_and_anonymous_auth(prefix):
    incoming = prefix
    with TestClient(create_app(settings(prefix)), base_url="https://service.example", root_path=prefix) as client:
        for source in ("/", "/admin"):
            response = client.get(incoming + source, headers={"Accept": "text/html"}, follow_redirects=False)
            assert response.status_code == 307
            assert response.headers["location"] == prefix + "/admin/"
        response = client.get(incoming + "/")
        assert response.json()["data"]["openapi"] == prefix + "/openapi.json"
        response = client.get(incoming + "/admin/")
        assert response.status_code == 200
        assert "default-src 'self'" in response.headers["Content-Security-Policy"]
        assert response.headers["Cache-Control"] == "no-store"
        for resource in ("app.js", "core.js", "styles.css"):
            assert client.get(incoming + "/admin/" + resource).status_code == 200
        docs = client.get(incoming + "/docs")
        assert docs.status_code == 200
        assert f'href="{prefix}/admin/"' in docs.text
        assert f'href="{prefix}/openapi.json"' in docs.text
        assert prefix + "/api/v1/query/batch" in docs.text
        schema = client.get(incoming + "/openapi.json").json()
        assert schema.get("servers", [{"url": "/"}]) == [{"url": prefix or "/"}]
        assert "/api/v1/query/batch" in schema["paths"]
        assert client.get(incoming + "/health/live").json() == {"status": "ok"}
        for path in ("/api/v1/admin/keys", "/api/v1/datasets", "/api/v1/services"):
            assert client.get(incoming + path).status_code == 401
        assert client.post(incoming + "/api/v1/auth/login", json={}).status_code == 422
        assert client.get(incoming + "/not-found").status_code == 404


def test_untrusted_prefix_headers_do_not_change_generated_paths():
    with TestClient(create_app(settings("/data-service"))) as client:
        headers = {"X-Forwarded-Prefix": "//evil.example", "Accept": "text/html"}
        response = client.get("/", headers=headers, follow_redirects=False)
        assert response.headers["location"] == "/data-service/admin/"
        assert client.get("/openapi.json", headers=headers).json()["servers"] == [{"url": "/data-service"}]


@pytest.mark.parametrize("prefix", ["", "/data-service"])
def test_real_server_receives_stripped_proxy_path(prefix):
    """真实 Uvicorn 将剥离前缀的 HTTP 路径还原成完整 ASGI scope；不模拟静态挂载。"""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(create_app(settings(prefix)), root_path=prefix,
                                               log_level="error", access_log=False, lifespan="off"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert server.started, "隔离 HTTP 服务未启动"
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=5) as client:
                assert client.get("/admin/").status_code == 200
                assert client.get("/admin/core.js").status_code == 200
                assert client.get("/admin").headers["location"] == prefix + "/admin/"
                assert client.get("/", headers={"Accept": "text/html"}).headers["location"] == prefix + "/admin/"
                assert client.get("/docs").status_code == 200
                assert client.get("/api/v1/datasets").status_code == 401
                assert client.get("/openapi.json").json().get("servers", [{"url": "/"}]) == [{"url": prefix or "/"}]
        finally:
            server.should_exit = True
            thread.join(timeout=10)
        assert not thread.is_alive(), "隔离 HTTP 服务未停止"
