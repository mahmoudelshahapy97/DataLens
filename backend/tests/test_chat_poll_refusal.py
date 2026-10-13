"""`chat_poll` turns a refused caller into 403/401, not a 500.

The caller is resolved inside the chat handler, so a forged `X-Tenant-Id` or a
missing credential raises `PermissionError` out of `handle_poll`. The route used
to log it as a crash and answer 500. The integration version of this check, with
a real directory behind it, is in `test_chat_multitenancy.py`.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from vanna.servers.fastapi.routes import register_chat_routes
from vanna_app.identity import AuthenticationRequired


class _Refusing:
    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    async def handle_poll(self, request):
        raise self.exc


def _post(exc: Exception):
    app = FastAPI()
    register_chat_routes(app, _Refusing(exc))
    return TestClient(app).post("/api/vanna/v2/chat_poll", json={"message": "hi"})


def test_a_workspace_refusal_is_403():
    response = _post(PermissionError("ada@acme.test is not a member of 'globex'."))
    assert response.status_code == 403
    assert "not a member" in response.json()["detail"]


def test_no_credential_is_401():
    response = _post(AuthenticationRequired("Sign in to continue."))
    assert response.status_code == 401


def test_a_real_crash_is_still_500_without_its_message():
    response = _post(RuntimeError("database password is hunter2"))
    assert response.status_code == 500
    assert "hunter2" not in response.text
