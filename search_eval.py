"""Compara la busqueda de Postgres (catalog_service.search_products, la de antes) contra la de
Typesense (search_service.search) sobre consultas reales - las del sitio en vivo y las que
reportaron usuarios. Script standalone, no importado por la app (misma convencion que
secret.py / create_admin_user.py). Ver CLAUDE.md, "Busqueda con Typesense".

Uso (Typesense debe estar configurado e indexado):
    TYPESENSE_URL=http://localhost:8108 TYPESENSE_API_KEY=dev-key python search_eval.py
    railway run --service api python search_eval.py      # contra produccion, antes del cambio

Por cada consulta imprime en que posicion aparece el primer producto "correcto" en cada motor y
cuantos de los primeros 5 lo son. Un producto es correcto si su nombre (normalizado igual que
la busqueda: sin acentos, singular, unidades pegadas) contiene TODAS las palabras de `must`,
al menos una de `any_of` (si se da) y ninguna de `forbid`, o si su SKU es exactamente `sku`.

Criterios:
- "top5": Typesense debe tener un resultado correcto en los primeros 5.
- "no_regress": ya funcionaba antes; Typesense no puede quedar peor que Postgres (salvo que
  siga dentro de los primeros 5).
Termina con codigo 1 si algun caso falla, para poder usarse como compuerta antes del cambio."""
import asyncio
import re
import sys
from dataclasses import dataclass, field

from app.core.config import settings
from app.core.database import AsyncSessionLocal, engine
from app.schemas.search import SearchFilter
from app.services import catalog_service, search_service, typesense_client
from app.services.search_index import normalize_search_text

TOP_N = 5
LIMIT = 20


@dataclass
class Case:
    q: str
    must: list[str] = field(default_factory=list)
    forbid: list[str] = field(default_factory=list)
    any_of: list[str] = field(default_factory=list)  # al menos una de estas (p. ej. sinonimos)
    sku: str | None = None
    expectation: str = "top5"  # "top5" | "no_regress"
    note: str = ""


CASES = [
    # Reportados por el usuario (2026-09-26). Se evalua el TIPO de producto: las consultas no
    # dicen medida, asi que cualquier producto del tipo correcto cuenta.
    Case("dado de impacto", ["dado", "impacto"], ["adaptador"], note="usuario"),
    Case("dado de impacto de entrada 1/2", ["dado", "impacto", "1/2"], ["adaptador"], note="usuario, sinonimo entrada=cuadro"),
    Case("adaptador pvc macho", ["adaptador", "macho", "pvc"], ["cpvc"], note="usuario"),
    # F1 - coincidencias dentro de otra palabra
    Case("martillo", ["martillo"], ["rotomartillo", "engrapadora"], note="F1"),
    Case("matillo", ["martillo"], ["casco", "amarillo"], note="F1/F7 typo"),
    Case("codo pvc 90", ["codo", "pvc"], ["cpvc"], note="F1"),
    # F2 - SKU exacto
    Case("12410", sku="12410", note="F2"),
    Case("51-0025-001", sku="51-0025-001", note="F2 SKU con guiones"),
    Case("UBSD1-1/4", sku="UBSD1-1/4", note="F2 SKU con fraccion"),
    # F3 - tipo de producto
    Case("broca 1/4", ["broca", "1/4"], note="F3"),
    Case("llave 10mm", ["llave", "10mm"], ["candado"], note="F3/F5"),
    # F4 - plurales
    Case("martillos", ["martillo"], ["rotomartillo"], note="F4"),
    Case("focos led", ["foco", "led"], note="F4"),
    Case("dados de impacto", ["dado", "impacto"], ["adaptador"], note="F4"),
    Case("pijas", ["pija"], note="F4"),
    # F5 - unidades
    Case("dado 10mm", ["dado", "10mm"], note="F5"),
    Case("dado 10 mm", ["dado", "10mm"], note="F5"),
    Case("foco 10w", ["foco", "10w"], note="F5"),
    # F7 - typos
    Case("pinsas", ["pinza"], note="F7"),
    Case("desarmdor", ["desarmador"], note="F7"),
    Case("martilo", ["martillo"], note="F7"),
    # F8 - orden de palabras
    Case("truper martillo", ["martillo"], ["mango", "azadon"], note="F8"),
    # F9 - fracciones mixtas
    Case("adaptador pvc 1-1/2", ["adaptador", "pvc", "1_1/2"], ["cpvc"], note="F9"),
    Case("adaptador pvc 1 1/2", ["adaptador", "pvc", "1_1/2"], ["cpvc"], note="F9"),
    # F10 - vocabulario (sinonimos)
    Case("llave allen", ["llave", "hexagonal"], ["artilleria"], note="F10 sinonimo"),
    Case("llaves allen", ["llave"], ["artilleria", "brocasierra"], note="F10 sinonimo + plural"),
    Case("destornillador", any_of=["desarmador", "destornillador"], note="F10 sinonimo"),
    Case("taladro inalambrico truper", ["taladro"], note="F10 sinonimo de una direccion"),
    # Ya funcionaban - no deben empeorar
    Case("llave española", ["llave", "espanola"], expectation="no_regress"),
    Case("llave stilson", ["llave", "stilson"], expectation="no_regress"),
    Case("cinta de aislar", ["cinta", "aislar"], expectation="no_regress"),
    Case("pinza de presion", ["pinza", "presion"], expectation="no_regress"),
    Case("silicón", ["silicon"], expectation="no_regress"),
    Case("wd40", ["wd"], expectation="no_regress"),
    Case("wd-40", ["wd"], expectation="no_regress"),
    Case("cople pvc", ["cople", "pvc"], ["cpvc"], expectation="no_regress"),
    Case("tee pvc", ["tee", "pvc"], ["cpvc"], expectation="no_regress"),
    Case("AM3165", sku="AM3165", expectation="no_regress"),
    Case("u1222v", sku="U1222V", expectation="no_regress"),
]

_TOKEN_RE = re.compile(r"[a-z0-9_/]+")


def is_relevant(case: Case, name: str, sku: str | None) -> bool:
    if case.sku is not None:
        return (sku or "").strip().lower() == case.sku.lower()
    tokens = set(_TOKEN_RE.findall(normalize_search_text(name)))
    return (all(w in tokens for w in case.must)
            and (not case.any_of or any(w in tokens for w in case.any_of))
            and not any(w in tokens for w in case.forbid))


def score(case: Case, docs) -> tuple[int | None, int]:
    """(posicion del primer relevante, relevantes en los primeros TOP_N)."""
    flags = [is_relevant(case, p.name, p.sku) for p in docs]
    rank = next((i + 1 for i, ok in enumerate(flags) if ok), None)
    return rank, sum(flags[:TOP_N])


async def run_postgres(case: Case):
    async with AsyncSessionLocal() as session:
        return (await catalog_service.search_products(session, case.q, LIMIT, 0))["docs"]


async def run_typesense(case: Case):
    async with AsyncSessionLocal() as session:
        return (await search_service.search(session, SearchFilter(q=case.q, limit=LIMIT)))["docs"]


def fmt(rank, hits) -> str:
    return f"{('#' + str(rank)) if rank else '—':>4} {hits}/{TOP_N}"


async def main() -> int:
    if not typesense_client.is_enabled():
        print("TYPESENSE_URL/TYPESENSE_API_KEY no configurados - no hay nada que comparar.")
        return 2
    if await typesense_client.get_alias("products") is None:
        print("Typesense no tiene indice todavia (alias 'products'). Arranca el worker primero.")
        return 2

    failures = []
    print(f"{'consulta':34} {'postgres':>10} {'typesense':>10}  resultado  nota")
    for case in CASES:
        pg_rank, pg_hits = score(case, await run_postgres(case))
        typesense_client.reset_breaker()
        ts_rank, ts_hits = score(case, await run_typesense(case))
        if not typesense_client.is_available():
            print("Typesense dejo de responder a mitad de la evaluacion; los resultados no son confiables.")
            return 2

        ts_ok_top = ts_rank is not None and ts_rank <= TOP_N
        if case.expectation == "top5":
            ok = ts_ok_top
        else:  # no_regress
            ok = ts_ok_top or (pg_rank is None) or (ts_rank is not None and ts_rank <= pg_rank)
        verdict = "OK" if ok else "FALLA"
        if not ok:
            failures.append(case.q)
        print(f"{case.q[:34]:34} {fmt(pg_rank, pg_hits):>10} {fmt(ts_rank, ts_hits):>10}  {verdict:9}  {case.note}")

    total = len(CASES)
    print(f"\n{total - len(failures)}/{total} casos OK en Typesense.")
    if failures:
        print("Fallan: " + ", ".join(repr(f) for f in failures))
    await engine.dispose()
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
