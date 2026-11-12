"""app/services/urrea_client.py contra un httpx.MockTransport - sin red real. Las formas de
respuesta vienen del probe real de 2026-11-12 (no del PDF de URREA)."""
import json
import logging
from decimal import Decimal

import httpx
import pytest

from app.core.config import settings
from app.services import urrea_client
from app.services.urrea_client import AVAILABLE, LIMITED, UNAVAILABLE, UrreaAuthError, UrreaError

PASSWORD = "secreto-super-123"


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setattr(settings, "URREA_USER", "distribuidor")
    monkeypatch.setattr(settings, "URREA_PASSWORD", PASSWORD)
    monkeypatch.setattr(settings, "URREA_WS_URL", "https://urrea.test/ws.php")
    urrea_client.reset_breaker()
    yield
    urrea_client.reset_breaker()
    monkeypatch.setattr(urrea_client, "_transport", None)


def _mock(monkeypatch, handler):
    calls = []

    def wrapped(request: httpx.Request):
        calls.append(json.loads(request.content))
        return handler(request)

    monkeypatch.setattr(urrea_client, "_transport", httpx.MockTransport(wrapped))
    return calls


def _availability(verdict: str, code: str = "10CAP", descripcion: str = "Jgo candados") -> dict:
    return {"status": "OK", "disponibilidad": [{
        "codigo": code, "descripcion": descripcion, "marca": "Lock", "cantidadSolicitada": "3",
        "disponibilidad": verdict, "fechaDisponibilidad": "2026-11-12",
    }]}


BULK_ROW = {
    "codigo": "100364", "nombreLargo": "Llave comb pulid esp 14mm", "DescripcionProducto": "Llave combinada 14mm Surtek",
    "Marca": "Surtek", "Submarca": "Surtek", "familia": "Herramientas manuales", "clase": "Llaves", "Subclase": "Combinadas",
    "Precio": "57.00", "Moneda": "MXP", "Multiplo": "6", "CodigoBarras": "0660731003642", "CodigoBarrasSinCero": "660731003642",
    "EstatusInventario": "Con reservas", "anexo20SAT": "27111713", "claveUnidadSAT": "EA",
    "bullets": "-Pulido espejo -Marca Surtek", "esJuego": "No", "piezasJuego": "0", "contenidoJuego": "",
    "accesorios": "Llave combinada.", "garantia": "Sin garantía", "empaque": "Granel", "keywords": "llave",
    "fotografia": ["https://blob.test/100364.jpg", "https://blob.test/100364_2.jpg"],
    "video": "https://www.youtube.com/embed/0", "fichaTecnica": "", "manual": "http://medios.urrea.com/m.pdf",
    "alto": "1", "fondo": "2.7", "ancho": "18", "peso": "0", "caracteristica": "",
}


def test_is_enabled_requires_user_and_password(monkeypatch):
    assert urrea_client.is_enabled()
    monkeypatch.setattr(settings, "URREA_PASSWORD", None)
    assert not urrea_client.is_enabled()


def test_parse_catalog_row_normalizes_values():
    row = urrea_client.parse_catalog_row(BULK_ROW)
    assert row.code == "100364"
    assert row.status == LIMITED
    assert row.price == Decimal("57.00")
    assert row.sale_multiple == 6
    assert row.set_pieces is None  # "0" es vacio
    assert row.weight_kg is None
    assert row.photo_urls == ["https://blob.test/100364.jpg", "https://blob.test/100364_2.jpg"]
    assert row.video_url is None  # placeholder embed/0
    assert row.datasheet_url is None
    assert row.manual_url == "https://medios.urrea.com/m.pdf"
    assert row.is_set is False


def test_parse_catalog_row_is_case_insensitive_and_maps_statuses():
    lowered = {k.lower(): v for k, v in BULK_ROW.items()}
    for raw, expected in (("Disponible", AVAILABLE), ("No disponible", UNAVAILABLE), ("raro", None)):
        assert urrea_client.parse_catalog_row({**lowered, "estatusinventario": raw}).status == expected
    assert urrea_client.parse_catalog_row({**BULK_ROW, "codigo": "  "}) is None


@pytest.mark.parametrize("verdict, found, sufficient", [
    ("Suficiente", True, True),
    ("Insuficiente", True, False),
    ("-", False, False),
])
async def test_check_availability(monkeypatch, verdict, found, sufficient):
    calls = _mock(monkeypatch, lambda r: httpx.Response(200, json=_availability(verdict)))
    result = await urrea_client.check_availability("10CAP", 3)
    assert (result.found, result.sufficient) == (found, sufficient)
    assert calls[0]["opcion"] == 2
    assert calls[0]["producto"] == {"codigoProducto": "10CAP", "cantidad": "3"}
    assert calls[0]["usuario"] == "distribuidor"


async def test_auth_error_comes_with_http_200_and_trips_breaker(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    calls = _mock(monkeypatch, lambda r: httpx.Response(200, json={"status": "ERROR", "mensaje": "Usuario o contraseña incorrectos"}))
    with pytest.raises(UrreaAuthError) as exc:
        await urrea_client.check_availability("10CAP", 1)
    assert not urrea_client.is_available()
    # Con el cortacircuitos abierto ni siquiera se llama.
    with pytest.raises(UrreaError):
        await urrea_client.check_availability("10CAP", 1)
    assert len(calls) == 1
    assert PASSWORD not in str(exc.value)
    assert PASSWORD not in caplog.text


async def test_network_error_trips_breaker(monkeypatch):
    def fail(request):
        raise httpx.ConnectTimeout("timeout", request=request)

    _mock(monkeypatch, fail)
    with pytest.raises(UrreaError):
        await urrea_client.check_availability("10CAP", 1)
    assert not urrea_client.is_available()


async def test_check_availability_requires_configuration(monkeypatch):
    monkeypatch.setattr(settings, "URREA_USER", None)
    with pytest.raises(UrreaError):
        await urrea_client.check_availability("10CAP", 1)


async def test_download_and_iterate_bulk(monkeypatch):
    body = {"status": "OK", "resultadoDispMasiva": [BULK_ROW, {**BULK_ROW, "codigo": "000AA", "EstatusInventario": "Disponible"}]}
    calls = _mock(monkeypatch, lambda r: httpx.Response(200, content=json.dumps(body).encode()))
    path = await urrea_client.download_bulk_catalog()
    try:
        rows = list(urrea_client.iter_bulk_rows(path))
    finally:
        import os
        os.unlink(path)
    assert calls[0]["opcion"] == 4
    assert [r.code for r in rows] == ["100364", "000AA"]
    assert rows[1].status == AVAILABLE


async def test_download_bulk_rejects_error_status(monkeypatch):
    _mock(monkeypatch, lambda r: httpx.Response(200, json={"status": "ERROR", "mensaje": "Usuario o contraseña incorrectos"}))
    with pytest.raises(UrreaAuthError):
        await urrea_client.download_bulk_catalog()


async def test_download_bulk_retries_once_on_5xx(monkeypatch):
    responses = iter([httpx.Response(503, text="mantenimiento"), httpx.Response(200, json={"status": "OK", "resultadoDispMasiva": []})])
    calls = _mock(monkeypatch, lambda r: next(responses))
    path = await urrea_client.download_bulk_catalog()
    import os
    os.unlink(path)
    assert len(calls) == 2
