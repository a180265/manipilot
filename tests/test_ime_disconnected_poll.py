"""A real disconnected HTTP poll cannot consume a later one-time IME task."""
import socket
import threading
import time

import httpx
import uvicorn
from fastapi import FastAPI

from adapters.screen_ime.routes import create_screen_ime_router, screen_ime_error_handler
from adapters.screen_ime.service import ScreenImeApiError, ScreenImeService


def test_disconnected_http_poll_does_not_claim_later_task(tmp_path, monkeypatch):
    service = ScreenImeService(tmp_path / "ime.db", tmp_path / "mappings.json")
    entered, disconnected = threading.Event(), threading.Event()
    original = service.tasks.next_task

    async def observed(*args, **kwargs):
        check = kwargs["disconnect_check"]
        async def observed_check():
            result = await check()
            if result:
                disconnected.set()
            return result
        kwargs["disconnect_check"] = observed_check
        entered.set()
        return await original(*args, **kwargs)
    monkeypatch.setattr(service.tasks, "next_task", observed)
    app = FastAPI()
    app.include_router(create_screen_ime_router(service))
    app.add_exception_handler(ScreenImeApiError, screen_ime_error_handler)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="error"))
    thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started and time.monotonic() < deadline:
            time.sleep(.01)
        assert server.started
        with httpx.Client(base_url=origin, timeout=3) as client:
            assert client.post("/api/v1/devices/phone/heartbeat",
                               json={"keyboardActive": True}).status_code == 200
            abandoned = socket.create_connection(listener.getsockname())
            abandoned.sendall(
                b"GET /api/v1/devices/phone/tasks/next?waitSeconds=20 HTTP/1.1\r\n"
                b"Host: localhost\r\n\r\n")
            assert entered.wait(3)
            abandoned.close()
            assert disconnected.wait(3), "HTTP disconnect must end the original long poll"
            created = client.post("/api/v1/tasks", json={
                "taskId": "after-disconnect", "deviceId": "phone",
                "targetUser": "search", "content": "文件传输助手"})
            assert created.status_code == 201, created.text
            assert service.tasks._query_one(
                "SELECT * FROM ime_deliveries WHERE task_id=?", ("after-disconnect",)) is None
            delivered = client.get("/api/v1/devices/phone/tasks/next?waitSeconds=0")
            assert delivered.status_code == 200, delivered.text
            assert delivered.json()["taskId"] == "after-disconnect"
            assert client.get("/api/v1/devices/phone/tasks/next?waitSeconds=0").status_code == 204
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
        service.close()
