"""通过本地HTTP服务器验证兼容协议；没有真实模型费用或真实KEY。"""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import threading
import time

import pytest

from data_service import llm_client
from data_service.errors import ServiceError
from data_service.llm_config import LlmConnection

KEY = 'test-http-key-not-real'


def envelope(content, **changes):
    return {'choices': [{'message': {'content': json.dumps(content)}, 'finish_reason': 'stop', **changes}]}


@contextmanager
def server(response=None, status=200, headers=None, delay=0, tls_context=None):
    requests = []
    response = envelope({'connection': 'ok'}) if response is None else response
    body = response if isinstance(response, bytes) else json.dumps(response).encode()
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            size = int(self.headers['Content-Length'])
            requests.append({'path': self.path, 'headers': dict(self.headers), 'body': json.loads(self.rfile.read(size))})
            time.sleep(delay)
            self.send_response(status)
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass  # 总超时用例主动断开连接是预期结果。

        def log_message(self, *_args):
            pass
    httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    if tls_context is not None:
        httpd.socket = tls_context.wrap_socket(httpd.socket, server_side=True)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{httpd.server_port}', requests
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def config(url, **changes):
    return LlmConnection(base_url=url, model='local-test-model', api_key=KEY, enabled=True,
                         timeout_seconds=5, token_parameter='max_tokens').model_copy(update=changes)


def test_real_http_protocol_paths_parameters_no_proxy(monkeypatch):
    monkeypatch.setenv('HTTP_PROXY', 'http://127.0.0.1:1')
    monkeypatch.setenv('HTTPS_PROXY', 'http://127.0.0.1:1')
    monkeypatch.setenv('NO_PROXY', '')
    with server() as (url, requests):
        assert llm_client.test_connection(config(url + '/v1'))['ok']
        assert llm_client.test_connection(config(url + '/v1/chat/completions',
                                                token_parameter='max_completion_tokens', api_key=None))['ok']
    assert requests[0]['path'] == '/v1/chat/completions' == requests[1]['path']
    first = requests[0]['body']
    assert set(first) == {'model', 'messages', 'stream', 'max_tokens'}
    assert first['model'] == 'local-test-model' and first['stream'] is False and first['max_tokens'] == 100
    assert requests[0]['headers']['Authorization'] == 'Bearer ' + KEY
    assert 'Authorization' not in requests[1]['headers']
    assert requests[1]['body']['max_completion_tokens'] == 100
    assert 'dataset' not in json.dumps(first['messages'])


@pytest.mark.parametrize('status,code', [(401, 'LLM_AUTH_FAILED'), (403, 'LLM_AUTH_FAILED'),
    (429, 'LLM_RATE_LIMITED'), (500, 'LLM_UPSTREAM_FAILED'), (302, 'LLM_UPSTREAM_FAILED')])
def test_upstream_error_no_retry_redirect_or_body_leak(status, code):
    with server({'error': KEY}, status=status, headers={'Location': 'http://127.0.0.1:1'}) as (url, requests):
        with pytest.raises(ServiceError) as failure:
            llm_client.test_connection(config(url))
        assert failure.value.code == code and KEY not in str(failure.value)
        assert len(requests) == 1


@pytest.mark.parametrize('body', [b'not json', b'{"choices":[],"choices":[]}', b'{"choices":NaN}',
    [], {'choices': []}, {'choices': [{'message': {'content': '```json\n{}\n```'}}]},
    envelope({'connection': 'no'}), envelope({'connection': 'ok'}, finish_reason='length'),
    {'choices': [{'message': {'content': '[]'}}]},
    {'choices': [{'message': {'content': '{"a":1,"a":2}'}}]},
    {'choices': [{'message': {'content': '{"a":NaN}'}}]},
    {'choices': [{'message': {'content': json.dumps({'key': KEY})}}]},
    {'choices': [{'message': {'content': '{}', 'tool_calls': [{'name': 'something'}]}}]},
    {'choices': [{'message': {'content': '{}', 'refusal': 'no'}}]},
    {'choices': [{'message': {'content': None}}]},
])
def test_bad_or_unsafe_model_response_is_redacted(body):
    with server(body) as (url, _requests):
        with pytest.raises(ServiceError) as failure:
            llm_client.test_connection(config(url))
        assert failure.value.code == 'LLM_INVALID_RESPONSE' and KEY not in str(failure.value)


@pytest.mark.parametrize('body,headers', [
    (b'x' * (llm_client.MAX_RESPONSE_BYTES + 1), {}),
    (b'{}', {'Content-Length': str(llm_client.MAX_RESPONSE_BYTES + 1)}),
    (b'{}', {'Content-Encoding': 'gzip'}),
])
def test_response_limits(body, headers):
    with server(body, headers=headers) as (url, _requests):
        with pytest.raises(ServiceError) as failure:
            llm_client.test_connection(config(url))
        assert failure.value.code == 'LLM_INVALID_RESPONSE'


def test_total_timeout_and_connection_failure():
    with server(delay=0.3) as (url, requests):
        started = time.monotonic()
        with pytest.raises(ServiceError) as failure:
            llm_client.test_connection(config(url, timeout_seconds=0.05))
        assert failure.value.code == 'LLM_TIMEOUT' and time.monotonic() - started < 1
        assert len(requests) <= 1
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        unused_port = listener.getsockname()[1]
    with pytest.raises(ServiceError) as failure:
        llm_client.test_connection(config(f'http://127.0.0.1:{unused_port}'))
    assert failure.value.code == 'LLM_CONNECTION_FAILED'


def test_request_limit_no_network():
    with pytest.raises(ServiceError) as failure:
        llm_client.complete(config('http://127.0.0.1:1'), [{'role': 'user', 'content': 'x' * 300000}])
    assert failure.value.code == 'LLM_INPUT_TOO_LARGE'


def test_resolved_metadata_address_blocked(monkeypatch):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *_args, **_kwargs:
                       [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('169.254.169.254', 80))])
    with pytest.raises(ServiceError) as failure:
        llm_client.test_connection(config('http://safe-name.test'))
    assert failure.value.code == 'LLM_ADDRESS_FORBIDDEN'


def test_pinned_dns_preserves_host_sni_and_certificate_verification(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone
    import ssl
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    import httpx

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'model.example.test')])
    now = datetime.now(timezone.utc)
    certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
        .public_key(private_key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName('model.example.test')]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(private_key, hashes.SHA256()))
    cert_path = tmp_path / 'test-ca.pem'
    key_path = tmp_path / 'test-key.pem'
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(private_key.private_bytes(serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(cert_path, key_path)
    seen_sni = []
    server_context.set_servername_callback(lambda _sock, servername, _ctx: seen_sni.append(servername))
    trusted_context = ssl.create_default_context(cafile=str(cert_path))
    original_client = httpx.AsyncClient
    original_lookup = socket.getaddrinfo
    dns_requests = []
    def lookup(host, port, *args, **kwargs):
        if host in ('model.example.test', 'wrong.example.test'):
            dns_requests.append(host)
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', port))]
        return original_lookup(host, port, *args, **kwargs)
    def client(**kwargs):
        assert kwargs['trust_env'] is False and kwargs['follow_redirects'] is False
        return original_client(verify=trusted_context, **kwargs)
    monkeypatch.setattr(socket, 'getaddrinfo', lookup)
    monkeypatch.setattr(httpx, 'AsyncClient', client)
    with server(tls_context=server_context) as (url, requests):
        port = url.rsplit(':', 1)[1]
        assert llm_client.test_connection(config('https://model.example.test:' + port + '/v1'))['ok']
        assert requests[0]['headers']['Host'] == 'model.example.test:' + port
        assert seen_sni == ['model.example.test']
        assert dns_requests == ['model.example.test']
        with pytest.raises(ServiceError) as failure:
            llm_client.test_connection(config('https://wrong.example.test:' + port + '/v1'))
        assert failure.value.code == 'LLM_CONNECTION_FAILED'
        assert len(requests) == 1, '错误证书必须在发送含KEY的HTTP请求前拒绝'
