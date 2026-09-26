"""app/services/typesense_client.py contra un httpx.MockTransport - sin red real."""
import json

import httpx
import pytest

from app.core.config import settings
from app.services import typesense_client


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setattr(settings, "TYPESENSE_URL", "http://typesense.test:8108")
    monkeypatch.setattr(settings, "TYPESENSE_API_KEY", "test-key")
    typesense_client.reset_breaker()
    yield
    typesense_client.reset_breaker()
    monkeypatch.setattr(typesense_client, "_transport", None)


def _mock(monkeypatch, handler):
    calls = []

    def wrapped(request: httpx.Request):
        calls.append(request)
        return handler(request)

    monkeypatch.setattr(typesense_client, "_transport", httpx.MockTransport(wrapped))
    return calls


def test_is_enabled_requires_both_settings(monkeypatch):
    assert typesense_client.is_enabled()
    monkeypatch.setattr(settings, "TYPESENSE_API_KEY", None)
    assert not typesense_client.is_enabled()


async def test_sends_api_key_and_returns_json(monkeypatch):
    calls = _mock(monkeypatch, lambda r: httpx.Response(200, json={"found": 1, "hits": []}))
    result = await typesense_client.search("products", {"q": "martillo"})
    assert result["found"] == 1
    assert calls[0].headers["X-TYPESENSE-API-KEY"] == "test-key"
    assert calls[0].url.path == "/collections/products/documents/search"
    assert calls[0].url.params["q"] == "martillo"


async def test_network_error_trips_breaker(monkeypatch):
    def boom(request):
        raise httpx.ConnectError("refused", request=request)

    _mock(monkeypatch, boom)
    with pytest.raises(typesense_client.TypesenseError):
        await typesense_client.search("products", {"q": "x"})
    assert not typesense_client.is_available()

    typesense_client.reset_breaker()
    assert typesense_client.is_available()


async def test_5xx_trips_breaker_but_4xx_does_not(monkeypatch):
    _mock(monkeypatch, lambda r: httpx.Response(400, json={"message": "bad param"}))
    with pytest.raises(typesense_client.TypesenseError) as exc:
        await typesense_client.search("products", {"q": "x"})
    assert exc.value.status_code == 400
    assert "bad param" in str(exc.value)
    assert typesense_client.is_available()

    _mock(monkeypatch, lambda r: httpx.Response(503, text="overloaded"))
    with pytest.raises(typesense_client.TypesenseError):
        await typesense_client.search("products", {"q": "x"})
    assert not typesense_client.is_available()


async def test_get_alias_returns_none_on_404(monkeypatch):
    _mock(monkeypatch, lambda r: httpx.Response(404, json={"message": "Not found"}))
    assert await typesense_client.get_alias("products") is None


async def test_get_alias_returns_collection(monkeypatch):
    _mock(monkeypatch, lambda r: httpx.Response(200, json={"name": "products", "collection_name": "products_v1_1"}))
    assert await typesense_client.get_alias("products") == "products_v1_1"


async def test_import_documents_sends_jsonl_and_parses_each_line(monkeypatch):
    def handler(request):
        lines = request.content.decode().splitlines()
        assert [json.loads(line)["id"] for line in lines] == ["a", "b"]
        assert request.url.params["action"] == "upsert"
        return httpx.Response(200, text='{"success":true}\n{"success":false,"error":"bad field"}')

    _mock(monkeypatch, handler)
    results = await typesense_client.import_documents("products", [{"id": "a"}, {"id": "b"}])
    assert results == [{"success": True}, {"success": False, "error": "bad field"}]


async def test_delete_documents_chunks_and_tolerates_404(monkeypatch):
    monkeypatch.setattr(typesense_client, "DELETE_CHUNK", 2)
    calls = _mock(monkeypatch, lambda r: httpx.Response(200, json={"num_deleted": 2}))
    deleted = await typesense_client.delete_documents("products", ["a", "b", "c`"])
    assert deleted == 4
    assert len(calls) == 2
    assert calls[1].url.params["filter_by"] == "id:[`c`]"  # backtick removido

    _mock(monkeypatch, lambda r: httpx.Response(404, json={"message": "Not found"}))
    assert await typesense_client.delete_documents("products", ["x"]) == 0


async def test_multi_search_raises_on_per_search_error(monkeypatch):
    _mock(monkeypatch, lambda r: httpx.Response(200, json={"results": [{"hits": []}, {"code": 404, "error": "no collection"}]}))
    with pytest.raises(typesense_client.TypesenseError):
        await typesense_client.multi_search([{"q": "a"}, {"q": "b"}])


async def test_disabled_raises_without_calling(monkeypatch):
    monkeypatch.setattr(settings, "TYPESENSE_URL", None)
    calls = _mock(monkeypatch, lambda r: httpx.Response(200, json={}))
    with pytest.raises(typesense_client.TypesenseError):
        await typesense_client.search("products", {"q": "x"})
    assert calls == []
