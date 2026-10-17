"""Real TLS proxy + uvicorn/IME: exact routes, long polling, and disconnects."""
import asyncio
import socket
import ssl
import subprocess
import threading
import time
from http.server import ThreadingHTTPServer

import httpx
import pytest
import uvicorn
from fastapi import FastAPI

from adapters.screen_ime.gesture_routes import create_gesture_router
from adapters.screen_ime.routes import create_screen_ime_router, screen_ime_error_handler
from adapters.screen_ime.service import ScreenImeApiError, ScreenImeService
from scripts import ime_lan_tls_proxy as proxy
from tests.test_gesture_coordinate_frame import LEGACY_SESSION, _hello, _page


@pytest.fixture
def tls_stack(tmp_path, monkeypatch):
    service = ScreenImeService(tmp_path / 'ime.db', tmp_path / 'mappings.json')
    app = FastAPI()
    captured_headers = []
    async def capture_headers(scope, receive, send):
        if scope["type"] == "http":
            captured_headers.append({key.decode(): value.decode() for key, value in scope["headers"]})
        await app(scope, receive, send)
    token = 'isolated-phone-token'
    def identity(value):
        return {'ime_device_id': 'ime-01', 'state': 'active'} if value == token else None
    app.include_router(create_screen_ime_router(service, phone_identity_provider=identity))
    app.include_router(create_gesture_router(service, phone_identity_provider=identity))
    app.add_exception_handler(ScreenImeApiError, screen_ime_error_handler)
    entered, disconnected = threading.Event(), threading.Event()
    original = service.tasks.next_task
    async def observed(*args, **kwargs):
        check = kwargs['disconnect_check']
        async def tracked():
            value = await check()
            if value:
                disconnected.set()
            return value
        kwargs['disconnect_check'] = tracked
        entered.set()
        try:
            return await original(*args, **kwargs)
        except asyncio.CancelledError:
            # Starlette middleware can cancel the app on ASGI disconnect before
            # Request.is_disconnected gets its next chance to observe the event.
            disconnected.set()
            raise
    monkeypatch.setattr(service.tasks, 'next_task', observed)
    upstream_socket = socket.socket()
    upstream_socket.bind(('127.0.0.1', 0))
    upstream = uvicorn.Server(uvicorn.Config(capture_headers, lifespan='off', log_level='error'))
    upstream_thread = threading.Thread(target=lambda: upstream.run(sockets=[upstream_socket]), daemon=True)
    upstream_thread.start()
    deadline = time.monotonic() + 5
    while not upstream.started and time.monotonic() < deadline:
        time.sleep(.01)
    assert upstream.started
    monkeypatch.setattr(proxy, 'UPSTREAM_HOST', '127.0.0.1')
    monkeypatch.setattr(proxy, 'UPSTREAM_PORT', upstream_socket.getsockname()[1])
    cert, key = tmp_path / 'test.crt', tmp_path / 'test.key'
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                    '-keyout', str(key), '-out', str(cert), '-days', '1',
                    '-subj', '/CN=localhost', '-addext', 'subjectAltName=IP:127.0.0.1'],
                   check=True, capture_output=True)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    frontend = ThreadingHTTPServer(('127.0.0.1', 0), proxy.Handler)
    frontend.socket = context.wrap_socket(frontend.socket, server_side=True)
    frontend_thread = threading.Thread(target=frontend.serve_forever, daemon=True)
    frontend_thread.start()
    client_context = ssl.create_default_context(cafile=str(cert))
    try:
        with httpx.Client(base_url=f'https://127.0.0.1:{frontend.server_port}',
                          verify=client_context, timeout=26,
                          headers={'x-ime-phone-token': token}) as client, \
                httpx.Client(base_url=f'http://127.0.0.1:{upstream_socket.getsockname()[1]}',
                             timeout=3, headers={'x-ime-phone-token': token}) as local:
            assert local.post('/api/v1/devices/ime-01/heartbeat',
                              json={'keyboardActive': True}).status_code == 200
            service.proxy_test_headers = captured_headers
            yield client, local, frontend, client_context, entered, disconnected, service
    finally:
        frontend.shutdown()
        frontend.server_close()
        frontend_thread.join(3)
        upstream.should_exit = True
        upstream_thread.join(5)
        upstream_socket.close()
        service.close()


def test_phone_metadata_passes_exact_routes_but_management_is_not_forwarded(tls_stack):
    client, local, _, _, _, _, service = tls_stack
    assert client.get('/api/v1/gesture-protocol', headers={
        'x-layout-forwarder-token': 'must-strip', 'x-debug-custom': 'must-strip',
        'x-forwarded-for': 'must-strip',
    }).status_code == 200
    forwarded = service.proxy_test_headers[-1]
    assert forwarded['x-ime-phone-token'] == 'isolated-phone-token'
    assert not {'x-layout-forwarder-token', 'x-debug-custom', 'x-forwarded-for'} & forwarded.keys()
    hello = _hello(_page('legacy-calibration', legacy=LEGACY_SESSION))
    assert client.post('/api/v1/devices/ime-01/gesture-client', json=hello).status_code == 200
    metadata = local.get('/api/v1/gesture-protocol/legacy-metadata',
                         params={'deviceId': 'ime-01', 'sessionId': LEGACY_SESSION})
    assert metadata.status_code == 200
    assert metadata.json()['page']['rootOriginDisplayPx'] == [0, 80]
    for method, path in [
        ('GET', '/api/v1/gesture-protocol/legacy-metadata'),
        ('POST', '/api/v1/gesture-protocol'),
        ('GET', '/api/v1/tasks'), ('POST', '/api/v1/tasks'),
        ('GET', '/api/v1/tasks/private-task'),
        ('GET', '/api/v1/tasks/private-task/wait'),
        ('POST', '/api/v1/tasks/private-task/cancel'),
        ('GET', '/api/v1/devices'), ('POST', '/api/v1/calibrations'),
        ('GET', '/api/v1/calibrations/private-session'),
        ('POST', '/api/v1/calibrations/private-session/arm'),
        ('POST', '/api/v1/calibrations/private-session/complete'),
        ('GET', '/v1/devices'), ('POST', '/v1/ime-pairing/invitations'),
        ('GET', '/api/v1/gesture-protocol;alias'),
        ('GET', '/api/v1/devices/ime-01/tasks/next/extra'),
    ]:
        response = client.request(method, path, json={})
        assert response.status_code == 403, (method, path, response.text)
    missing = client.post('/api/v1/devices/ime-01/gesture-client', json=hello,
                          headers={'x-ime-phone-token': ''})
    assert missing.status_code == 401


def test_twenty_second_poll_survives_proxy_timeout(tls_stack):
    client, _, _, _, _, _, _ = tls_stack
    started = time.monotonic()
    response = client.get('/api/v1/devices/ime-01/tasks/next?waitSeconds=20')
    assert response.status_code == 204, response.text
    assert time.monotonic() - started >= 19


def test_abandoned_tls_poll_cancels_upstream_before_later_task(tls_stack):
    client, local, frontend, context, entered, disconnected, service = tls_stack
    raw = socket.create_connection(('127.0.0.1', frontend.server_port))
    abandoned = context.wrap_socket(raw, server_hostname='127.0.0.1')
    abandoned.sendall(
        b'GET /api/v1/devices/ime-01/tasks/next?waitSeconds=20 HTTP/1.1\r\n'
        b'Host: localhost\r\nx-ime-phone-token: isolated-phone-token\r\n\r\n')
    assert entered.wait(3)
    abandoned.close()
    assert disconnected.wait(3), 'TLS client disconnect must cancel the upstream request'
    created = local.post('/api/v1/tasks', json={
        'taskId': 'after-proxy-disconnect', 'deviceId': 'ime-01',
        'targetUser': 'search', 'content': '文件传输助手'})
    assert created.status_code == 201, created.text
    assert service.tasks._query_one('SELECT * FROM ime_deliveries WHERE task_id=?',
                                   ('after-proxy-disconnect',)) is None
    delivered = client.get('/api/v1/devices/ime-01/tasks/next?waitSeconds=0')
    assert delivered.status_code == 200
    assert delivered.json()['taskId'] == 'after-proxy-disconnect'
    assert client.get('/api/v1/devices/ime-01/tasks/next?waitSeconds=0').status_code == 204
