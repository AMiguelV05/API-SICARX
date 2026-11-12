import logging

from fastapi import APIRouter, Depends, HTTPException, status

from app.core.database import DbDep
from app.core.security import SuperAdminDep, get_current_admin
from app.schemas.suppliers import PendingPurchasesResponse, ProductSupplierInfo, SupplierSyncRequestResponse, SupplierSyncStatusResponse
from app.services import supplier_service, urrea_client

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["Admin - Suppliers"], dependencies=[Depends(get_current_admin)])

# Integracion con URREA (ver urrea_sync_worker.py). El sync corre en el worker; aqui solo se
# consulta su estado y se pide una corrida.


@router.get("/suppliers/urrea/status", response_model=SupplierSyncStatusResponse, summary="Estado del sync con URREA")
async def admin_urrea_status(db: DbDep):
    """Ultima corrida del sync de la masiva de URREA, cuantos codigos se emparejaron y cuantos
    productos estan marcados como disponibles en URREA."""
    return await supplier_service.get_urrea_status(db)


@router.post(
    "/suppliers/urrea/sync", response_model=SupplierSyncRequestResponse, status_code=status.HTTP_202_ACCEPTED,
    summary="Pedir un sync de URREA ahora",
)
async def admin_request_urrea_sync(db: DbDep, current: SuperAdminDep):
    """Solo super_admin. El worker lo atiende en su siguiente minuto (la descarga tarda ~1.5 min);
    el resultado se ve en GET /admin/suppliers/urrea/status."""
    if not urrea_client.is_enabled():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="La integracion con URREA no esta configurada.")
    requested_at = await supplier_service.request_urrea_sync(db)
    await db.commit()
    logger.info(f"Sync de URREA solicitado por el admin {current.email}.")
    return SupplierSyncRequestResponse(requested_at=requested_at, detail="Sync solicitado; el worker lo correra en el siguiente minuto.")


@router.get("/suppliers/urrea/pending-purchases", response_model=PendingPurchasesResponse, summary="Por pedir a URREA")
async def admin_urrea_pending_purchases(db: DbDep):
    """Productos con piezas sobre pedido en ordenes pagadas y no aceptadas, con el codigo de
    URREA y la cantidad sugerida redondeada al multiplo de venta. La orden mas antigua primero.
    Una orden sale de aqui al aceptarla (despues de dar de entrada la mercancia en Sicar X)."""
    return PendingPurchasesResponse(docs=await supplier_service.list_pending_purchases(db))


@router.get("/products/{product_uuid}/supplier", response_model=ProductSupplierInfo, summary="Ficha de proveedor de un producto")
async def admin_product_supplier(product_uuid: str, db: DbDep):
    """Codigo de URREA, precio de lista (no es nuestro costo), disponibilidad segun la masiva, medios y la ficha completa.
    404 si el producto no esta ligado a URREA."""
    return await supplier_service.get_product_supplier_info(db, product_uuid)
