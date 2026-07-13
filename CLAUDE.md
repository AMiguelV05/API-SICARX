## Overview

API SICARX is a FastAPI middleware between an e-commerce frontend (Ferretería Charly) and the SICAR X POS/ERP platform. It exposes a small REST API, secured with a static API key, that orchestrates calls to SICAR X's REST and GraphQL endpoints while maintaining a local PostgreSQL cache of the product catalog for fast reads.

## Commands

- Run dev server: `uvicorn app.main:app --reload`
- Run the catalog sync worker (separate long-running process, not started by the FastAPI app): `python -m app.worker.sync_task`
- Migrations: `alembic revision --autogenerate -m "description"`, `alembic upgrade head`, `alembic downgrade -1`
- Install deps: `pip install -r requirements.txt`
- No test suite exists in this repo currently.

### Docker Compose

`docker-compose.yml` runs the whole stack: `db` (Postgres 16), a one-off `migrate` service (`alembic upgrade head`, runs once and exits), `api` (uvicorn), and `worker` (`sync_task`) — `api`/`worker` both wait on `migrate` completing successfully before starting, so migrations never race.

- `docker compose up --build` — build images and start everything
- `docker compose logs -f api` / `worker` — tail a service's stdout (app-level logs still also land in `app.log`/`sync.log` on the host via bind mounts)
- `docker compose run --rm migrate alembic revision --autogenerate -m "description"` — generate a new migration without starting the rest of the stack
- `docker compose down` — stop everything; add `-v` to also drop the `db_data` volume (destructive — wipes the containerized Postgres)

**Important**: the `db` service creates a **new, empty** Postgres instance (its own `db_data` volume) — it is not connected to whatever Postgres you already use for local development or production. `DATABASE_URL` for `api`/`worker`/`migrate` is overridden in `docker-compose.yml` to point at this containerized `db` (not the `DATABASE_URL` in your `.env`, which is only used for the other vars). To point this stack at your existing Postgres instead, remove the `db` service and its `depends_on` entries, and set `DATABASE_URL` in `.env` to that instance's address (use `host.docker.internal` in place of `localhost` if it runs on the host machine, since `localhost` inside a container refers to the container itself).

### Deploying to Railway

Live at project **api-sicarx** (workspace: Angel Villalvazo's Projects), three services: `api` (public domain `api-production-cf7a.up.railway.app`), `worker` (no public networking), and a Postgres plugin named **`Postgres-O4xA`** (mind the suffix — a second, unused `Postgres` plugin service also exists in the project from a setup mistake and should be deleted once confirmed unneeded, since nothing references it).

Two Railway services run from this same repo/`Dockerfile`: `railway.api.json` (uvicorn, plus `alembic upgrade head` as a `preDeployCommand`) and `railway.worker.json` (`sync_task`, no pre-deploy command — see below on why only one service runs migrations). Railway's managed Postgres plugin replaces the `db`/`migrate` services from Docker Compose — there is no dedicated one-off migration service on Railway, so `preDeployCommand` on the `api` service is what runs migrations instead.

**How these were actually deployed**: no GitHub integration is wired up yet (the Docker/Railway files weren't committed/pushed at deploy time), so both services were pushed via `railway up --service <name>` from the local directory — which uploads whatever `railway.json` currently sits at the repo root as that deploy's config. Since there's no per-service config-as-code path set in the dashboard, deploying either service means temporarily copying its file over the root one first: `cp railway.api.json railway.json && railway up --service api`, then `cp railway.worker.json railway.json && railway up --service worker`, removing the root `railway.json` afterward so the repo doesn't carry a stale third config file. The one-time fix that removes this manual step: in each service's Settings → Config-as-code file path, point `api` at `railway.api.json` and `worker` at `railway.worker.json`, then connect both to the GitHub repo for auto-deploy-on-push.

**Gotchas specific to Railway** (neither applies to the Docker Compose setup):
- Railway does **not** run `startCommand` through a shell — `--port $PORT` gets passed to uvicorn as the literal 5-character string `$PORT`, not the actual port number, and it crash-loops with `Error: Invalid value for '--port': '$PORT' is not a valid integer.` (hit and fixed live during initial deploy). `railway.api.json`'s `startCommand` must wrap the whole thing in a shell explicitly: `sh -c \"uvicorn app.main:app --host 0.0.0.0 --port $PORT\"`. The worker's command has no env var references so it doesn't need this.
- Railway's Postgres plugin exposes `PGHOST`/`PGPORT`/`PGUSER`/`PGPASSWORD`/`PGDATABASE` (and a `DATABASE_URL` using the plain `postgresql://` scheme) — but `app/core/database.py` needs the `asyncpg` driver. Don't use the plugin's own `DATABASE_URL` as-is; set `DATABASE_URL` on both `api` and `worker` manually as a variable reference: `postgresql+asyncpg://${{Postgres-O4xA.PGUSER}}:${{Postgres-O4xA.PGPASSWORD}}@${{Postgres-O4xA.PGHOST}}:${{Postgres-O4xA.PGPORT}}/${{Postgres-O4xA.PGDATABASE}}`.
- Both services import `app.core.config.settings`, and pydantic-settings fails at import time if any required var is missing — so all 7 vars (`X_API_KEY`, `SICAR_ADMIN_EMAIL`, `SICAR_ADMIN_PASSWORD`, `SICAR_TOKEN`, `SICAR_PRICE_LIST_ID`, `CASH_REGISTER_UUID`, plus the `DATABASE_URL` override above) must be set on **both** `api` and `worker`, even the ones only one of them actually uses.
- Only `api` runs `alembic upgrade head` (as its `preDeployCommand`) — Railway has no `depends_on`/completion-order primitive across services like Compose's `service_completed_successfully`, so avoid a migration race by not running it from both. On the very first deploy, deploy `api` first and let it finish before deploying `worker`, so the schema exists before the worker's first sync tries to write to it (this was the actual order used). On later deploys that include a new migration, redeploy `api` before `worker` for the same reason.
- The worker logs to `sync.log` via `RotatingFileHandler` inside the container's ephemeral filesystem, same as locally — Railway has no bind-mount equivalent, so `railway logs --service worker` only ever shows `Starting Container` and nothing else. To confirm the worker is actually syncing, check `POST /catalog`'s `total` count over time instead (both services share the same Postgres).

## Configuration

- Settings load via pydantic-settings from a project-root `.env` (`app/core/config.py`, `Settings`). Required vars: `DATABASE_URL`, `X_API_KEY`, `SICAR_ADMIN_EMAIL`, `SICAR_ADMIN_PASSWORD`, `SICAR_TOKEN`, `SICAR_PRICE_LIST_ID`, `CASH_REGISTER_UUID`.
- `DATABASE_URL` must use an async driver (`postgresql+asyncpg://...`) — `app/core/database.py` uses `create_async_engine`.
- `secret.py` is a standalone one-off script (`secrets.token_urlsafe(60)`) for generating a new `X_API_KEY`; it isn't imported by the app.

## Architecture

Two separate processes share the same Postgres database and `Product` model:

1. **FastAPI app** (`app/main.py`) — serves the HTTP API consumed by the e-commerce frontend.
2. **Background worker** (`app/worker/sync_task.py`) — a standalone script with its own logging setup and its own `AsyncIOScheduler` that polls SICAR X every 5 minutes and upserts the full catalog into `products`. Must be run as a second process alongside uvicorn.

### Two distinct SICAR X auth schemes — do not conflate them

- **Admin/B2B token** (`app/services/sicar_auth.py`, the `sicar_auth` singleton): one shared token for server-to-server calls (catalog sync, product-detail scraping, payment, cancellation). Seeded from `SICAR_TOKEN`, held in memory, refreshed via `SicarAuthManager.refresh_token()` (hits two AWS Lambda endpoints with `SICAR_ADMIN_EMAIL`/`SICAR_ADMIN_PASSWORD`) whenever SICAR X returns 401. Every caller follows the same pattern: try with the current token, on 401 call `refresh_token()`, retry once (see `order_service.pay_order_in_sicar`, `cancel_service.process_order_cancellation`, `product_service.fetch_full_details_from_sicar`).
- **Customer/session token** (`app/services/session_service.py`): a per-shopper JWT. A new session is bootstrapped by scraping the `tmpStore` cookie off the SICAR X storefront HTML — SICAR double-encodes it (URL-encode, then Base64 over that, and the Base64 payload itself is URL-encoded JSON), so decoding requires URL-decode → Base64-decode → URL-decode, in that order, before `json.loads`; getting this order wrong throws `binascii.Error: Incorrect padding`. An existing session is refreshed by calling SICAR X's `/api/ecommerce/config` with the current token. This token travels in the `Authorization` header between the frontend and this API and is what's used for cart validation and order creation — never the admin token.

### Request flow: placing an order (`app/api/routes/orders.py`)

The `/orders` request body is intentionally minimal — the frontend sends only `products:
[{uuid, quantity}]` and `deliveryInfo`; everything else (pricing, tax fields, sku,
description, unit, totals) is computed server-side. This contract and the exact field
semantics below were reverse-engineered by capturing one real accepted order from the
actual storefront checkout — see `app/services/order_service.py:build_order_payload`.

1. Client sends its session JWT via `Authorization` header → `get_or_refresh_customer_session` validates/refreshes it (and now also decodes the JWT's `jti` claim as a fallback `contentId`).
2. `validate_cart_items` (`order_service.py`) checks stock/availability in SICAR X via GraphQL using the customer token, and returns the raw per-product `priceList`/`type` data plus `content.units` (sales-unit uuid → short name) for the next step — it's no longer validate-and-discard.
3. `build_order_payload` (pure function, no network calls) assembles the full SICAR order document: `sku`/`description` come from the local `Product` cache, `priceBaseTax`/`priceTax`/`amountTax` all use the **same** value — `priceList.netPrice1` (the tax-inclusive retail price) — despite the field names; SICAR X does not want the tax broken out here. `serie` is hardcoded `"TL"` and there's no shipping line item for `deliveryType: "PICKUP"` — both confirmed from the captured real request.
4. `create_order_in_sicar` posts the order to SICAR X's REST endpoint (customer token), then decrements local `Product.stock` in Postgres.
5. `pay_order_in_sicar` applies payment via SICAR X REST using the admin/B2B token (with 401-retry). Note: the real storefront leaves pickup orders in `TO_PAY` status (paid in-store); this backend pays immediately via the admin token instead — an intentional difference, not a bug.

Cancellation (`app/services/cancel_service.py`) mirrors this: resolve the real document `uuid`, cancel in SICAR X with the admin token, then restore local stock. SICAR X identifies each document with **two distinct values**: the `id` this API returns from order creation (Mongo-style, e.g. `6a4fd308da77fe7cd25d1dd9` — what `OrderResponse.id`/`OrderCancel.uuid` actually carries) and a separate RFC4122 `uuid` that `POST https://api.sicarx.com/documents/v1/sale/cancel` requires — passing the `id` there fails with `"is not a valid UUID"`. `_resolve_document_uuid` bridges the two with a GraphQL lookup (`document-graph/v1/graph-v2`, query `generatedV2(objectId: "<id>") { uuid }`, admin token), confirmed against a real cancellation captured live from `app.sicarx.com`. Note separately: SICAR X's own admin panel requires a step-up "Autorización" (a second login from a higher-privilege user) before it will cancel *any* order through its UI — that's a UI-level safeguard on the restricted account used for browser testing, not a constraint on this API's admin B2B token, which already has cancel privileges.

### Local catalog vs. live SICAR X data

- `Product` (`app/models/product.py`) is the single local table. The sync worker upserts it with `on_conflict_do_update` on `sicar_uuid`. Rows are never deleted — a completed sync pass marks stale rows (whose `last_sync_id` doesn't match the current pass) `is_deleted=True` instead.
- `GET /products/{uuid}` (`routes/products.py`) serves from Postgres but lazily refreshes `description_details`, `tags`, `additional_images`, etc. from SICAR X's GraphQL API when `details_updated_at` is null or older than 24h (`fetch_full_details_from_sicar`).
- `POST /catalog` reads only from Postgres (`catalog_service.get_local_catalog`) — no live SICAR X calls — filtered by `department_uuid`/`category_uuid` with pagination.
- `GET /taxonomy` (`routes/taxonomy.py`) returns departments with their nested categories (for building filter UIs) from local `Department`/`Category`/`department_category` tables (`app/models/taxonomy.py`). Categories are many-to-many with departments in SICAR X, not a strict hierarchy. Fetched from SICAR X's `/store/` GraphQL endpoint using an anonymous customer session (`taxonomy_service.fetch_taxonomy_from_sicar`), cached with the same lazy 24h-staleness refresh pattern as `GET /products/{uuid}`.

### Endpoints (all verified live end-to-end)

| Endpoint | Token used | Notes |
|---|---|---|
| `POST /catalog` | none (just `x-api-key`) | Postgres only, paginated, filterable by `department_uuid`/`category_uuid`. |
| `GET /products/{uuid}` | admin/B2B (internal, only if stale) | Serves from Postgres; lazily refreshes detail fields from SICAR GraphQL if `details_updated_at` is null/>24h old. |
| `GET /taxonomy` | admin/B2B (internal, only if stale) | Departments + nested categories from Postgres; same 24h lazy-refresh pattern. |
| `POST /session/init` | none (just `x-api-key`) | Bootstraps a new customer session (no `Authorization` header) or refreshes an existing one (with it). Returns the customer JWT the frontend must then send back as `Authorization` on `/orders`. |
| `POST /orders` | customer (required `Authorization` header) + admin/B2B (internal, for payment) | See flow below. |
| `POST /cancel` | admin/B2B (internal only — no customer token involved) | See flow below. |

### Auth on this API's own endpoints

Every route depends on `validate_api_key` (`app/core/security.py`), which checks a static `x-api-key` header against `settings.X_API_KEY`. This is separate from both SICAR X token schemes above — it authenticates the frontend to this middleware, not this middleware to SICAR X.

## Logging

- The FastAPI app logs to `app.log` (configured in `main.py`).
- The worker logs separately to `sync.log` via a `RotatingFileHandler` (10MB × 5 backups) configured in `app/worker/sync_task.py` — the two processes do not share a logger config.
- Log messages and docstrings throughout are in Spanish; keep new code consistent with that convention.
