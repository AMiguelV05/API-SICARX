"""webhook_outbox + webhook_outbox_worker: firma, X-Webhook-Id, backoff y FAILED. El worker
abre sus propias sesiones; aqui se le inyecta la sesion transaccional del test."""
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.core.config import settings
from app.core.webhook_signing import verify_hmac_sha256
from app.models.webhook_outbox import WebhookOutbox
from app.services.webhook_outbox_service import enqueue_webhook
from app.worker import webhook_outbox_worker


class _SessionCtx:
    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        return self.db

    async def __aexit__(self, *exc):
        return False


@pytest.fixture
def worker_db(db, monkeypatch):
    monkeypatch.setattr(webhook_outbox_worker, "AsyncSessionLocal", lambda: _SessionCtx(db))
    return db


def _mock_frontend(monkeypatch, status_code: int, seen: list):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status_code)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(webhook_outbox_worker.httpx, "AsyncClient",
                        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))


async def test_sends_signed_with_webhook_id(worker_db, monkeypatch):
    seen = []
    _mock_frontend(monkeypatch, 200, seen)
    row = enqueue_webhook(worker_db, "product-back-in-stock", "/api/webhooks/product-back-in-stock", {"email": "a@b.com"})
    await worker_db.commit()

    assert await webhook_outbox_worker.drain_webhook_outbox() == 1
    await worker_db.refresh(row)
    assert row.status == "SUCCEEDED"

    request = seen[0]
    assert str(request.url) == f"{settings.FRONTEND_BASE_URL.rstrip('/')}/api/webhooks/product-back-in-stock"
    assert request.headers["X-Webhook-Id"] == row.uuid
    ts = request.headers["X-Webhook-Timestamp"]
    assert verify_hmac_sha256(settings.FRONTEND_WEBHOOK_SECRET, f"{ts}.".encode() + request.content, request.headers["X-Webhook-Signature"])


async def test_backoff_then_failed(worker_db, monkeypatch):
    seen = []
    _mock_frontend(monkeypatch, 404, seen)  # cualquier no-2xx reintenta, no solo 5xx
    row = enqueue_webhook(worker_db, "cart-abandoned", "/api/webhooks/cart-abandoned", {"x": 1})
    await worker_db.commit()

    expected = [timedelta(minutes=1), timedelta(minutes=10), timedelta(hours=1)]
    for attempt, delay in enumerate(expected, start=1):
        before = datetime.now(timezone.utc)
        await webhook_outbox_worker.drain_webhook_outbox()
        await worker_db.refresh(row)
        assert row.status == "PENDING" and row.attempts == attempt
        assert row.next_attempt_at >= before + delay - timedelta(seconds=5)
        # Adelanta el reloj: hace la fila reclamable de inmediato.
        row.next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        await worker_db.commit()

    await webhook_outbox_worker.drain_webhook_outbox()
    await worker_db.refresh(row)
    assert row.status == "FAILED" and row.attempts == 4 and "HTTP 404" in row.last_error
    assert len({r.headers["X-Webhook-Id"] for r in seen}) == 1  # mismo id en cada reintento


async def test_not_yet_due_and_stale_in_progress(worker_db, monkeypatch):
    seen = []
    _mock_frontend(monkeypatch, 200, seen)
    future = WebhookOutbox(event="e", path="/x", payload={}, status="PENDING", attempts=0,
                           next_attempt_at=datetime.now(timezone.utc) + timedelta(minutes=5))
    stale = WebhookOutbox(event="e", path="/y", payload={}, status="IN_PROGRESS", attempts=0,
                          next_attempt_at=datetime.now(timezone.utc))
    worker_db.add_all([future, stale])
    await worker_db.flush()
    stale.updated_at = datetime.now(timezone.utc) - timedelta(minutes=10)
    await worker_db.commit()

    assert await webhook_outbox_worker.drain_webhook_outbox() == 1
    await worker_db.refresh(future)
    await worker_db.refresh(stale)
    assert future.status == "PENDING" and stale.status == "SUCCEEDED"
