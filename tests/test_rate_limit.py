"""Clave del rate limit (core/rate_limit.py): por visitante cuando el frontend se identifica con
FRONTEND_PROXY_SECRET, sin limite por defecto para sus renders sin visitante, e igual que antes
para todo lo demas. App FastAPI minima con su propio Limiter - no toca la BD."""
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.core import rate_limit
from app.core.rate_limit import FRONTEND_SSR_KEY, FrontendAwareSlowAPIMiddleware, get_client_ip

SECRET = "test-frontend-secret"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(rate_limit.settings, "FRONTEND_PROXY_SECRET", SECRET)
    limiter = Limiter(key_func=get_client_ip, default_limits=["2/minute"])
    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.add_middleware(FrontendAwareSlowAPIMiddleware)

    @app.get("/key")
    async def key(request: Request):
        return {"key": get_client_ip(request)}

    @app.post("/login")
    @limiter.limit("1/minute")
    async def login(request: Request):
        return {"ok": True}

    return TestClient(app)


def _key(client, headers):
    return client.get("/key", headers=headers).json()["key"]


def test_trusted_frontend_with_visitor_ip_keys_on_visitor(client):
    assert _key(client, {"X-Frontend-Auth": SECRET, "X-Client-IP": "203.0.113.7"}) == "203.0.113.7"


def test_trusted_frontend_without_visitor_ip_is_ssr(client):
    assert _key(client, {"X-Frontend-Auth": SECRET}) == FRONTEND_SSR_KEY


def test_client_ip_header_ignored_without_valid_secret(client):
    headers = {"X-Frontend-Auth": "wrong", "X-Client-IP": "203.0.113.7", "X-Forwarded-For": "1.1.1.1, 198.51.100.2"}
    assert _key(client, headers) == "198.51.100.2"


def test_client_ip_header_ignored_when_secret_not_configured(client, monkeypatch):
    monkeypatch.setattr(rate_limit.settings, "FRONTEND_PROXY_SECRET", None)
    headers = {"X-Frontend-Auth": SECRET, "X-Client-IP": "203.0.113.7", "X-Forwarded-For": "198.51.100.2"}
    assert _key(client, headers) == "198.51.100.2"


def test_ssr_requests_skip_default_limits(client):
    for _ in range(5):
        assert client.get("/key", headers={"X-Frontend-Auth": SECRET}).status_code == 200


def test_default_limits_still_apply_per_visitor(client):
    visitor = {"X-Frontend-Auth": SECRET, "X-Client-IP": "203.0.113.7"}
    assert [client.get("/key", headers=visitor).status_code for _ in range(3)] == [200, 200, 429]
    other = {"X-Frontend-Auth": SECRET, "X-Client-IP": "203.0.113.8"}
    assert client.get("/key", headers=other).status_code == 200


def test_route_specific_limits_still_apply_to_ssr_key(client):
    ssr = {"X-Frontend-Auth": SECRET}
    assert [client.post("/login", headers=ssr).status_code for _ in range(2)] == [200, 429]
