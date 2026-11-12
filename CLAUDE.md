## Overview

API SICARX is a FastAPI middleware between the Ferretería Charly e-commerce frontend (Next.js, separate repo) and the SICAR X POS/ERP. Postgres is the source of truth for catalog, orders, accounts and PIM data; SICAR X is only contacted for catalog sync, lazy product-detail refresh, and inventory deltas when an admin accepts/cancels an order. Frontend contract: `FRONTEND_INTEGRATION.md`. Admin contract: `ADMIN_INTEGRATION.md`. All routes live under `/v1` (`app/api/v1_router.py`).

## Commands

- Dev server: `uvicorn app.main:app --reload`
- Worker (separate long-running process): `python -m app.worker.sync_task`
- Migrations: `alembic revision --autogenerate -m "..."`, `alembic upgrade head`, `alembic downgrade -1`
- Deps: `pip install -r requirements.txt`
- Tests: `pytest` (needs local Postgres; uses `TEST_DATABASE_URL`, or `DATABASE_URL` with the db renamed to `sicarx_test` — never the dev/prod db). Typesense integration tests need `TYPESENSE_URL=http://localhost:8108 TYPESENSE_API_KEY=dev-key` and `docker compose up -d typesense`; skipped otherwise.
- Search relevance check (Postgres vs Typesense, exit 1 on failure): `python search_eval.py` with the Typesense vars set.
- Standalone one-off scripts at repo root (not imported by the app): `secret.py`, `create_admin_user.py`, `create_backup_role.py`, `prune_r2_backups.py`, `import_gonher_vehicles.py`, `import_package_dimensions.py`, `search_eval.py`, `urrea_probe.py` (read-only probe of the URREA webservice; `OUT_DIR` must be outside the repo, responses include our cost price).

### Docker Compose

Services: `db` (Postgres 16, its own empty `db_data` volume — not your dev/prod db), `migrate` (one-off `alembic upgrade head`), `typesense` (`typesense/typesense:28.0`, disposable volume; the worker rebuilds the index), `api`, `worker`. `api`/`worker` wait for `migrate` to complete and `typesense` to be healthy. `DATABASE_URL` is overridden in `docker-compose.yml` to point at `db`; to use an existing Postgres instead, remove `db` and set `DATABASE_URL` in `.env` (`host.docker.internal` instead of `localhost`). `docker compose down -v` wipes the containerized db.

**Windows/Git Bash gotcha**: `docker run ... --data-dir /data` gets rewritten to `C:/Program Files/Git/data`; prefix with `MSYS_NO_PATHCONV=1`.

### Railway

Project **api-sicarx**. Services: `api` (public `api-production-cf7a.up.railway.app`), `worker` (private), `typesense` (private), `frontend` (Next.js, own repo), Postgres plugin **`Postgres-O4xA`** (mind the suffix).

- `api`/`worker` build from this repo's `Dockerfile`, connected to GitHub `AMiguelV05/API-SICARX` `main`. Each service's *Config-as-code file path* must stay set (`api` → `railway.api.json`, `worker` → `railway.worker.json`); without it Railway falls back to the Dockerfile `CMD` and both services break. **Never commit a root `railway.json`.** Manual deploy: `cp railway.api.json railway.json && railway up --service api`, then same for worker, deleting the root file after each.
- Auto-deploy on push has been unreliable. Verify with `railway deployment list --service <name> --json` (recent `"reason": "deploy"` with matching `commitHash`). Stuck deploy: `railway redeploy --service <name> --from-source --yes`. Persistent trigger failure: disconnect/reconnect the source (`railway service source disconnect|connect --repo AMiguelV05/API-SICARX --branch main --service <name>`), then check the GitHub app installation for a pending permission banner.
- Only `api` runs migrations (`preDeployCommand`). When a deploy includes a migration, deploy `api` before `worker`.
- `startCommand` isn't run through a shell: `railway.api.json` must use `sh -c "uvicorn app.main:app --host 0.0.0.0 --port $PORT"`.
- `DATABASE_URL` on `api`/`worker` must be the asyncpg reference: `postgresql+asyncpg://${{Postgres-O4xA.PGUSER}}:${{Postgres-O4xA.PGPASSWORD}}@${{Postgres-O4xA.PGHOST}}:${{Postgres-O4xA.PGPORT}}/${{Postgres-O4xA.PGDATABASE}}` (the plugin's own `DATABASE_URL` uses the wrong scheme).
- Every required setting must be set on **both** `api` and `worker` (pydantic-settings fails at import otherwise), even if only one uses it.
- The worker logs only to `sync.log` inside the container; `railway logs --service worker` shows nothing useful. Check `GET /admin/sync/catalog-status` instead.
- **Typesense service**: image `typesense/typesense:28.0`, volume at `/data`, configured only by variables: `TYPESENSE_DATA_DIR=/data`, `TYPESENSE_API_KEY`, `TYPESENSE_API_ADDRESS=::`, `TYPESENSE_THREAD_POOL_SIZE=4` (lowered from 8 on 2026-10-29; without it, it sizes to 384 host CPUs and crash-loops), `TYPESENSE_NUM_COLLECTIONS_PARALLEL_LOAD=2`, `TYPESENSE_NUM_DOCUMENTS_PARALLEL_LOAD=1000`. On `api`/`worker` set references, never pasted values: `TYPESENSE_URL=http://${{typesense.RAILWAY_PRIVATE_DOMAIN}}:8108`, `TYPESENSE_API_KEY=${{typesense.TYPESENSE_API_KEY}}`. Unsetting them on `api` rolls search back to Postgres.
- Setting secrets: piping from PowerShell into `railway variables --set-from-stdin` prepends a BOM — pipe from Bash. `railway add --variables KEY=VALUE` echoes the value; rotate any secret set that way.
- `frontend` has `RAILPACK_NODE_VERSION=24.22.1` pinned; bump it on each Node 24 security release, or delete it once Railpack resolves `24.x` to the latest (check the `mise node@` line in the build log).
- `frontend` start command (service setting, not in its repo): `node --max-old-space-size=256 node_modules/next/dist/bin/next start` (was 384 until 2026-10-29; its RAM plateaus at that heap cap, mostly from crawler renders). If it restarts with heap OOM, raise it back. Its `app/robots.ts` blocks MJ12bot, DotBot, PetalBot, Amazonbot, AhrefsBot and SemrushBot.
- One-off scripts in a service (`railway ssh --service worker -- python -u search_eval.py`) need a key registered with `railway ssh keys add`, `ssh.railway.com` in `~/.ssh/known_hosts` (`ssh-keyscan -t ed25519 ssh.railway.com >> ~/.ssh/known_hosts`), and `python -u`.
- `MALLOC_ARENA_MAX=2` on `api`/`worker`; worker uses `DB_POOL_SIZE=2`/`DB_MAX_OVERFLOW=6`.

**Cost rules** (bill is ~90% RAM; workspace limit soft $20 / **hard $25**, services stop at hard). Watch with `railway metrics --all --since 7d` and `railway usage`.
- No scheduled Typesense full rebuild: each rebuild permanently steps up the service's RAM until a `typesense` restart. `drain_dirty` keeps the index current. To repair suspected drift: `UPDATE products SET search_dirty_at = now();` (via `railway connect Postgres-O4xA`). Never run `full_rebuild` from a second process — its lock is per-process.
- The catalog sync must keep writing only changed rows (WAL/RAM). The full SICAR `hidden` check runs at most hourly (egress).

### Backups and disaster recovery

Details: `docs/superpowers/specs/2026-09-09-disaster-recovery-design.md` (may have been deleted). Railway PITR is disabled (Hobby plan); offsite `pg_dump` is the only mechanism.

- `.github/workflows/backup.yml`, run in a `postgres:18-alpine` container (prod is Postgres 18):
  - `frequent` (every 5 min): excludes `products`, `vehicles`, `product_categories` → R2 `frequent/`, 7-day retention.
  - `daily` (09:00 UTC): full dump → R2 `daily/`, 35-day retention.
  - `workflow_dispatch` takes a `tier` input.
- Restore: latest `daily` into a fresh instance, then latest `frequent` with `pg_restore --data-only --disable-triggers`. Rare FK violations on PIM/social tables for products created after the last daily are an accepted manual fix.
- Uses the read-only `backup_reader` role (`create_backup_role.py`, prints the URL once). GitHub secrets on this repo: `BACKUP_DATABASE_URL`, `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET_NAME`.
- `prune_r2_backups.py` (last step of both jobs) keeps the bucket under 9 GB, deleting oldest `frequent/` first, `daily/` only as a last resort; exits non-zero if it can't.
- R2 setup (manual): lifecycle rules per prefix (7 d / 35 d) and bucket-lock rules **shorter** than those (e.g. 1 d / 2 d) so pruning still works.
- Deferred: narrowing the app role's `DROP`/`TRUNCATE` privileges.

## Configuration

Settings via pydantic-settings from `.env` (`app/core/config.py`). Remember: on Railway, every required var goes on both `api` and `worker`.

- **Required**: `DATABASE_URL` (must be `postgresql+asyncpg://`), `X_API_KEY`, `SICAR_ADMIN_EMAIL`, `SICAR_ADMIN_PASSWORD`, `SICAR_TOKEN`, `SICAR_PRICE_LIST_ID`, `CASH_REGISTER_UUID`, `CLIENT_JWT_SECRET`, `ADMIN_JWT_SECRET`, `GOOGLE_CLIENT_ID` (not a secret; no client secret needed), `MP_ACCESS_TOKEN` (private token, never the public key), `MP_WEBHOOK_SECRET`, `FRONTEND_BASE_URL`, `FRONTEND_WEBHOOK_SECRET` (shared out-of-band with the frontend), `ENVIA_API_TOKEN`, `ENVIA_ORIGIN_NAME/COMPANY/PHONE/EMAIL/STREET/NUMBER/DISTRICT/CITY/STATE/ZIP_CODE`.
- **Optional**: `SHIPPING_CHECKOUT_ENABLED` (false), `SHIPPING_MARGIN_PERCENT`/`SHIPPING_MARGIN_FIXED_MXN` (0), `SHIPPING_QUOTE_CARRIERS` (""); `CLIENT_JWT_EXPIRE_MINUTES` (10080); `CLIENT_JWT_SECRET_PREVIOUS` (accepted only by `decode_unsubscribe_token`, after a rotation — runbook in `ops/2026-10-railway-incident/`); `ADMIN_DASHBOARD_BASE_URL`/`ADMIN_WEBHOOK_SECRET` (admin webhooks no-op if unset; set in prod, the admin lives in the storefront app); `SENTRY_DSN` (no project yet); `TYPESENSE_URL`/`TYPESENSE_API_KEY`; `URREA_USER`/`URREA_PASSWORD` (integration off if unset), `URREA_WS_URL`, `URREA_SKU_PREFIX` ("U"), `URREA_BACKORDER_ENABLED` (false), `URREA_BACKORDER_LEAD_DAYS` (5); `ENVIA_ORIGIN_REFERENCE`; `ENVIA_COUNTRY` (`"MX"`, alpha-2 — unlike SICAR's `"MEX"`), `ENVIA_PHONE_CODE` (`"+52"`); `DB_POOL_SIZE`/`DB_MAX_OVERFLOW` (5/5); `ENVIRONMENT` (default `production`).
- Local `.env` should set `ENVIRONMENT=development`: enables cookies over plain HTTP and serves `/docs`/`/redoc`/`/openapi.json` (404 in production).
- `API_BASE_URL` and `ADMIN_API_KEY` no longer exist; delete them if found.
- Generate secrets with `secrets.token_urlsafe(48)` (`secret.py` for `X_API_KEY`). Keep a distinct secret per purpose.
- Engine uses `pool_pre_ping=True`.

## Architecture

Two processes share one Postgres:
1. **FastAPI app** (`app/main.py`).
2. **Worker** (`app/worker/sync_task.py`), own `AsyncIOScheduler` and logging:
   - catalog sync, every 5 min;
   - `sicar_sync_worker` (drains `sicar_sync_outbox`), every 1 min;
   - `search_index_worker`: `drain_dirty` every 30 s, `reconcile_synonyms` every 5 min, `ensure_index_on_startup`;
   - `webhook_outbox_worker` every 1 min, `stock_alert_worker` every 5 min, `wishlist_price_drop_worker` hourly, `abandoned_cart_worker` every 30 min, `shipping_quote_worker` daily (prunes `shipping_quotes` > 30 days).

Conventions: log messages and docstrings are in Spanish. Money is `Decimal` (`Decimal(str(x))`, never from float), quantized with `ROUND_HALF_UP`. Status vocabularies are plain strings constrained in the app layer, not DB enums. Externally referenced rows use a `uuid`, not `id`; a resource not owned by the caller is `404`, not `403`. External HTTP calls use plain `httpx` (no SDKs); blocking work (bcrypt, `PyJWKClient`) goes through `asyncio.to_thread`. Never use `Depends(XxxDep)` on an `Annotated` dep alias (causes `422 args/kwargs`).

### SICAR X auth

One admin/B2B token (`app/services/sicar_auth.py`, `sicar_auth` singleton per process), seeded from `SICAR_TOKEN`. Every caller: try → on 401 `refresh_token()` → retry once. There is no per-shopper SICAR token or `/session/init` anymore.

### Placing an order (`app/api/routes/orders.py`)

No live SICAR X call anywhere in checkout.
1. Body is only `products: [{uuid, quantity}]`, `deliveryInfo`, optional `couponCode`. `X-Client-Token` optional (absent → guest; invalid → 401).
2. `validate_cart_items` checks local `Product` (`is_deleted=false`, `is_active=true`) and rejects with `409` if missing, not enough `available_stock`, or `price <= 0` (the sync can save `0.00` when a price-list key is missing).
3. `build_order_payload` (pure): `priceTax` = `Product.price` (IVA included); `amountTax` = **line total** (`price * quantity`), not unit price; `unit` = `Product.unit_short_name` or `"PZA"`; `serie` = `"TL"`. A coupon discount is subtracted once from the total, never per line.
4. `apply_reserved_deltas` reserves stock; `create_local_order` persists `status="TO_PAY"` (adds `imageUrl` per line). Response includes `id`, `orderUuid`, `amount`.
- `deliveryType` is `PICKUP` | `DELIVERYMAN`. `DELIVERYMAN` needs exactly one of `addressUuid` (account) or inline `address` (guest). Required address fields: `street`, `city`, `county`, `state`, `zipCode`, `extNumber`, `neighborhood` → `400` naming missing ones. Snapshot uses `ClientAddressPublic` shape; `country` is `"MEX"`. Address resolution is shared with `/shipping/quotes` (`delivery_service.resolve_delivery_address`). Shipping: see "Shipping at checkout" below.
- `Idempotency-Key` header: `OrderIdempotencyKey` claim-then-fill (unique on `(client_account_id, key)`, partial unique on `(guest_email, key) WHERE client_account_id IS NULL`). Existing filled claim → replay; unfilled < 2 min → `409`; older → discard and proceed.

### Shipping at checkout (`shipping_quote_service.py`)

- Gated by `SHIPPING_CHECKOUT_ENABLED` (default false = old behavior: no `shipping` required, no cost). Optional settings: `SHIPPING_MARGIN_PERCENT`/`SHIPPING_MARGIN_FIXED_MXN` (price = envia `totalPrice` × (1+%) + fixed; `totalPrice` already includes IVA and surcharges like extended zone), `SHIPPING_QUOTE_CARRIERS` (comma list; empty = the ~23 carriers in envia's catalog, ~6 s per quote).
- `POST /shipping/quotes` (20/min): reads products **without** `FOR UPDATE` (never holds row locks across envia calls) and commits before calling envia. Any product without package dims (or `is_bulk`, or non-integer qty) → `NOT_QUOTABLE/MISSING_DIMENSIONS` without calling envia. Otherwise `shipping_packing.build_packages` (first-fit decreasing, 30 kg / 60³ cm³ per box, 1.15 volume padding for multi-piece boxes, oversize pieces alone) → `shipping_service.fetch_rates` → cheapest per carrier, top 5. No rates → `NO_RATES`; envia error → `UPSTREAM_ERROR`. Stored in `shipping_quotes` (30 min TTL; a `QUOTED` row < 10 min old with the same items fingerprint + `destination_key` + client is reused; worker prunes rows > 30 days daily).
- `POST /orders` (`resolve_for_order`, DB reads only, inside the order transaction): items without dims → `MANUAL`, no cost. Else `shipping.quoteUuid` is required and must match client, unexpired, same fingerprint (`uuid:qty` sha256) and `destination_key` (zip|state|city normalized) → `409` otherwise. `NOT_QUOTABLE` quote → `MANUAL`, no cost. `QUOTED` → `optionId` required; `Order.delivery_cost` = option price, added to `total` **after** the coupon discount (`subtotal` stays products-only; coupons never discount shipping); snapshot in `Order.shipping_quote` (JSONB). MP charges `order.total`, so it includes shipping; refunds cap at `order.total`; dashboard revenue (`SUM(total)`) includes shipping.
- Admin `shipping/quote|generate`: dims optional (all four or none) → default to `shipping_quote.packages`; `generate` defaults `carrier/service` to the paid option and logs the difference between the label price and `carrierPrice`.
- Tests: `tests/test_checkout_shipping.py` (envia mocked at `shipping_service.fetch_rates`), `tests/test_shipping_packing.py`.

### Payments (Mercado Pago Orders API, `app/services/payment_service.py`)

- Frontend renders the Payment Brick (card, debit, OXXO; no Wallet) with `amount`, then calls `POST /orders/{id}/pay` with the Brick's formData. Amount always comes from `order.total`. `X-Idempotency-Key` = hash of `order.uuid` + payload.
- Only the **Orders API** (`/v1/orders`, ids `ORD...`) is used; test credentials are rejected by the Payments API. `_order_as_payment` maps responses to the old Payments shape (`processed`→`approved`, `action_required`→`pending`, `failed`→`rejected`, `canceled`→`cancelled`, `charged_back`, `in_process`→`in_mediation`). Non-`ORD` ids get `409`; webhooks for them are ignored.
- Sandbox gotchas: declined card = HTTP 402 with the order in `data` (treated as `rejected`); refunded amount comes from `refunded_amount` or the sum of `processed` refunds (`refunds[]` includes failed ones); a refund right after another can get `409 post_processing_operation_pending` (retried with same key); partial refund needs the `PAY...` transaction id; `/cancel` only works on `created`/`action_required`; `payment_method.type` required (frontend sends `paymentTypeId`, else inferred from `/v1/payment_methods`, cached 1 h); no `notification_url` — webhooks are configured on the MP app (topics `order`, `topic_chargebacks_wh`).
- `order_history_service.finalize_order_payment` is the **only** place mapping MP status to order state: `approved` → `PAID` (only if not `CANCELLED`) + confirm coupon + sales count + `order-confirmed` webhook (on the real transition only); `pending`/`in_process` → stays `TO_PAY`, stores `mp_ticket_url`; `rejected`/`cancelled` → `prepare_local_cancellation`; `charged_back` / `in_mediation` → set `disputed_at` once, alert admin on the real status transition, status stays `PAID`; `refunded` (out-of-band) → insert a `Refund` row + admin alert.
- `POST /payments/webhook` is the only non-admin route without `x-api-key`. Signature: HMAC-SHA256 of `id:{data.id};request-id:{x-request-id};ts:{ts};` with `MP_WEBHOOK_SECRET` (the app's **test-mode** key); accepts `data.id` as-is or lowercased. It re-fetches the order from MP, never trusts the body, and always returns 200. Chargebacks are let through even on a `CANCELLED` order.
- Chargeback detail: cases listed in `transactions.chargebacks[]` are fetched via `GET /v1/chargebacks/{case_id}` and upserted by `chargeback_service.process_chargeback` (`IN_PROCESS`/`WON`/`LOST` from `coverage_applied`). MP's field is misspelled `coverage_elegible`. A lost chargeback changes nothing automatically.
- Outbound frontend webhooks (`order-confirmed`, `order-cancelled`, `order-dispatched`, `verification-requested`, `password-reset-requested`) are HMAC-signed (`X-Webhook-Timestamp`, `X-Webhook-Signature` = hex HMAC-SHA256 of `f"{ts}." + body` with `FRONTEND_WEBHOOK_SECRET`), non-fatal, no retry. `clientEmail` prefers `deliveryInfo.contactInfo.email`, falls back to the account email.
- Tests: `tests/test_mp_orders_api.py`.

### Cancellation (local-first)

- `order_history_service.prepare_local_cancellation` is shared by `POST /orders/{id}/cancel`, `DELETE /orders/{id}` and `finalize_order_payment`. It takes `SELECT ... FOR UPDATE`, rechecks status inside the lock (409 if already cancelled), sets `CANCELLED`, releases the coupon redemption if `PENDING`, and: if not accepted → releases `reserved`; if accepted → enqueues a `SicarSyncOutbox(action="CANCEL")` (stock restored when it succeeds). No commit of its own.
- MP refund/cancel runs synchronously **before** it; `mp_resolved_here` makes sure a real MP action is committed even if the cancellation loses the race.
- `/cancel` returns `409` when `dispatch_status == "DISPATCHED"` (checked before any money moves; `COMPLETE` is still cancellable, since it's also the terminal state for `PICKUP`).
- `DELETE /orders/{id}` only for `TO_PAY`; soft-deletes via `Order.deleted_at`. Every customer/webhook lookup filters `deleted_at IS NULL`; the outbox worker deliberately doesn't.
- `sicar_sync_worker.py`: claims one row with an atomic `UPDATE ... WHERE id = (SELECT ... FOR UPDATE SKIP LOCKED) RETURNING`, committed before the HTTP call (never hold a lock across it). `IN_PROGRESS` rows older than 5 min are reclaimable. `ACCEPT`/`CANCEL` call `sicar_stock_service.apply_order_stock_delta(items, branch_id, sign=∓1)` — SICAR only ever learns inventory deltas, never orders/documents/dispatch. Backoff 1/2/4/8/16 min, `MAX_ATTEMPTS=5`, then `FAILED` + `notify_admin_sicar_sync_failed` + `capture_message`.
- `OrderCancelResponse.sicarTimestamp` is the local cancellation time (epoch ms).

### Stock reservation (`Product.reserved`, `available_stock`)

- `Product.stock` = SICAR truth, written by the sync and by the outbox success handlers. `Product.reserved` = held by open, not-yet-accepted orders; excluded from the sync. `available_stock` (hybrid, `GREATEST(stock - reserved, 0)`) is what every customer-facing read and `in_stock` filter uses (partial expression index `ix_products_available_stock` must match that exact expression).
- Lifecycle: checkout → `reserved += q`; cancel before accept → `reserved -= q`; `ACCEPT` success → `stock -= q`, `reserved -= q`; `CANCEL` success → `stock += q`.
- After each sync, `reserved > stock` rows trigger `notify_admin_stock_drift`.
- Customer `ProductBasic.stock` is sourced from `available_stock`; admin lists use `ProductAdminBasic` (`stock` + `availableStock`).
- Batched `UPDATE ... WHERE id = bindparam(...)` statements need `.execution_options(dml_strategy="core_only")`, otherwise SQLAlchemy raises `bulk synchronize of persistent objects not supported` with 2+ rows.

### Catalog

- **Sync** (`sync_task.py`): upserts on `sicar_uuid`, only rows that differ (`_changed_product_values`, compared in Python — not a `WHERE` on `ON CONFLICT`, which still writes WAL). Products not seen in a completed pass get `is_deleted=True` (skipped if SICAR returned nothing). `last_sync_id` = last pass that changed the row. Writes `SyncStatus`. Commits after each page read.
- `Product.price` comes from `/product/list` `prices[f"N{price_list_id_suffix}"]`, IVA included.
- `is_active` (SICAR `hidden`, via GraphQL per page): full check at most hourly (`HIDDEN_REFRESH_INTERVAL`, `_last_full_hidden_refresh`); other passes only check new products. A failed page keeps stored values. Hiding/unhiding takes up to 1 h to show. Tests: `tests/test_catalog_sync.py`.
- **PIM-owned columns, never in the sync whitelist**: `brand`, `bullet_points`, `technical_specs`, `contents`, `description`, `attributes`, `variant_group_uuid`, `sales_count`, `reserved`, `average_rating`, `reviews_count`, `compare_at_price`, `sale_ends_at`, `package_weight_kg`/`package_length_cm`/`package_width_cm`/`package_height_cm`. Any new locally-owned column must stay out of `product_values` too.
- `GET /products/{uuid}` filters deleted/inactive, and lazily refreshes `additional_skus`, `tags`, `additional_images`, `sales_unit_uuid`, `unit_short_name`, etc. from SICAR GraphQL if `details_updated_at` is null/>24h. `description` is admin-authored, not refreshed.
- Product info edits: `PATCH /admin/products/{uuid}/info` (`exclude_unset`) or the `InfoProducto` sheet of the bulk import (blank cell = keep, literal `NULL` = clear). `brand` is normalized by `brand_service.normalize_brand` (trim, `""`→null, casing untouched), so grouping by `lower(brand)` is enough. `hasBrand` filter on `/products`/`/search`.
- **Package dimensions** (`package_dimensions_service.py`, for shipping quotes): `package_weight_kg` + `package_length_cm/width_cm/height_cm` of one sale unit's package. All four or none (`ck_products_package_dims_all_or_none`), all `> 0`; caps 1500 kg / 600 cm; rounded to 3/2 decimals, a positive value below the step rounds up to it. Not in the search trigger. Written by `PUT/DELETE /admin/products/{uuid}/package-dimensions`, the `Medidas` bulk-import sheet (replace; only changed rows written) and `import_package_dimensions.py` (Urrea distributor template: SICAR `sku` = `"U"` + its `Código`, never the raw code, which collides with other brands; EAN fallback; dry-run by default, `--apply` writes to `DATABASE_URL`, `--out` builds a `Medidas` xlsx to upload in prod). Exposed as `packageDimensions` on `GET /products/{uuid}`. Used by the checkout shipping quote (see "Shipping at checkout").
- Catalog listing (`catalog_service`): `tag` filter uses JSONB `@>` with a GIN index; `Product.price` is `Numeric(10,2)`.
- Postgres search (now the Typesense fallback): `search_normalize(sku/name)` (unaccent, strip `-`) with trigram GIN indexes `ix_products_sku_trgm_search`/`ix_products_name_trgm_search`; prefix matches rank first. Needs `pg_trgm` (extension privileges). "0 scans" on Railway's index dashboard doesn't mean unused; check the code.
- **Taxonomy**: self-referencing `categories` (`parent_uuid`), admin-managed, not synced. `get_descendant_uuids` is a recursive CTE. `product_categories` (N:M) is the PIM tagging used by the `taxonomyUuid` filter; `Product.department_uuid`/`category_uuid` are SICAR's raw classification.
- **Vehicles**: flat `vehicles` table (`vehicle_type` `AUTOMOTIVE`/`MOTORCYCLE`, `year_end` null = current), `product_vehicles` N:M, `vehicleUuid` filter. Public cascade `GET /vehicles/makes|years|models|engines` (order: type → make → year → model → engine) and `GET /vehicles`. Seeded once from Gonher's public reference (`import_gonher_vehicles.py`, heuristic parsing). **asyncpg gotcha**: `(:p IS NULL OR col = :p)` with `None` raises `AmbiguousParameterError`; use `CAST(:p AS VARCHAR)`.
- **Bulk import** (`POST /admin/bulk-import/products`, `bulk_import_service.py`): sheets `Categorias` (comma/semicolon-separated slugs), `Vehiculos`, `Atributos`, `Variantes`, `InfoProducto`, `Medidas`. Products resolved by `sku` (case-insensitive) with `additional_skus` fallback. Additive (`ON CONFLICT DO NOTHING`), partial success with per-row errors; batch-resolves in a constant number of queries. `GET /admin/bulk-import/template` shares the same column constants.

### Search with Typesense (`/search`, `/search/suggest`)

Spec: `docs/superpowers/specs/2026-09-26-typesense-search-design.md` (may have been deleted).
- Typesense returns only ids/order/total; `search_service.hydrate` loads products from Postgres (price/stock always live). Falls back to `catalog_service.search_products` when unconfigured/down/erroring. `typesense_client.py`: 1 s connect / 2 s read, circuit breaker skips Typesense for 30 s after a network error or 5xx.
- **Indexing** (worker only): `products.search_dirty_at` is set **only by Postgres triggers** (on indexed-column changes, availability crossing zero, photo gained/lost, sale columns; statement-level triggers on `product_categories`/`product_vehicles`). `drain_dirty` clears with compare-and-clear. `full_rebuild` (new collection → stopwords/synonyms → alias swap → drop old) runs only at startup when the `products` alias is missing or on another `SCHEMA_VERSION`. **Bump `SCHEMA_VERSION` whenever `build_schema`/`to_document` change.** `drain_dirty`/`full_rebuild` share an `asyncio.Lock`.
- `normalize_search_text` is applied identically to names, queries and synonyms: lowercase, no accents, mixed fractions as one token (`1 1/2` → `1_1/2`), number+unit joined (`10 mm` → `10mm`, standalone numbers only), naive Spanish singular. Typesense's `stem` is deliberately not used (broken for `es`).
- **Ranking**: `sku_compact`/`name_compact`/`additional_skus` without typos; `name_first` low weight; `text_match_type=max_weight`; prefix only when the last word has ≥3 chars. Sort: `_text_match`, then `_tiebreak_eval` (name starts with a query word > in stock > has photo), then `sales_count`. `_eval` doesn't sum clause weights — every combination is listed as its own clause. Exact SKU is pinned first on page 1 via a second search. `multi_search` order: `[main, price stats, SKU pin]`.
- `GET /search/suggest`: products + up to 4 brands (facet); `brands: []` on fallback.
- Synonyms: `search_synonyms` table is the source of truth, stored normalized; admin CRUD at `/admin/search/synonyms`; push to Typesense is best-effort (`syncedToSearch`), reconciled every 5 min.
- Known leftovers: `te pvc` (add a one-way synonym); confirm which `sortBy` the storefront sends by default (only `relevance` uses this ranking).

### Sorting, price filter, priceRange

- `sortBy: "relevance"` on `/products`: in stock first, then has photo, then `sales_count desc, name`. Every Postgres sort ends in `Product.id` (stable pagination); no `sortBy` = `ORDER BY id`.
- `minPrice`/`maxPrice` (`PriceFilterMixin`, inclusive, `422` if min > max) filter `price`. `priceRange` = min/max of the set without the price filter, over `price > 0`; null if empty. The Postgres typo fallback only runs when the unpriced word match is empty.

### Sales, related products, notifications, marketing

- **Sale price**: `compare_at_price`/`sale_ends_at` (admin-entered previous price). Always read via `Product.is_on_sale` / `effective_compare_at_price` / `effective_sale_ends_at` (a sale disappears when it expires or SICAR's `price` rises to ≥ previous). Typesense field `sale_until` (`0` = none, `SALE_NO_END`). Admin `PUT/DELETE /admin/products/{uuid}/sale` and bulk `/admin/products/sale` (audited).
- **Related** (`GET /products/{uuid}/related`, `related_service.py`): bought together (PAID, 180 d) → taxonomy node, then parent → brand → vehicles; in-stock first; uuid list cached in-process 1 h (only valid with a single `api` instance).
- Order line enrichment (`productUuid`/`unitPrice`/`lineTotal`) happens at serialization (`schemas/orders.enrich_order_items`); stored JSON untouched.
- **`webhook_outbox`**: used by `product-back-in-stock`, `wishlist-price-drop`, `cart-abandoned`. `enqueue_webhook` only `db.add`s in the caller's transaction. Worker retries after 1 min, 10 min, 1 h, then `FAILED`. Adds `X-Webhook-Id`/`X-Webhook-Event`. Older webhooks still go direct.
- **Stock alerts**: subscribe only while `available_stock = 0`; one active per product+email (partial unique); rate-limited; the worker detects availability every 5 min (no sync hook); expire after 90 days.
- **Marketing**: `ClientAccount.marketing_emails` defaults `false`. `marketing_consent_service.record_marketing_consent` is the **only** writer (sets bool, timestamp and an append-only `marketing_consent_events` row, no commit). Events are immutable (trigger blocks UPDATE/DELETE/TRUNCATE). IP/UA from `ConsentContextDep` (`X-Client-IP`/`X-Client-User-Agent` only with a valid `X-Frontend-Auth`).
- Unsubscribe tokens: no-`exp` JWT on `CLIENT_JWT_SECRET`, `purpose: "unsubscribe"`, scopes `marketing` (sub = client uuid) / `stock-alert` (sub = alert uuid); `POST /notifications/unsubscribe` is idempotent.
- `wishlist_price_drop_worker`: vs `WishlistItem.baseline_price`, drop ≥5%, ≤1 webhook per client per 24 h. `abandoned_cart_worker`: account carts idle 2 h with no newer order, ≤1 per 7 days. **Gotcha**: `Cart.updated_at` has `onupdate`; mark `abandoned_notified_at` with a core `UPDATE` that sets `updated_at=Cart.updated_at`.

### Client accounts

- `ClientAccount`/`ClientAddress` are local accounts, unrelated to SICAR. Session JWT: HS256 on `CLIENT_JWT_SECRET`, `sub` = client uuid, explicit `iat`. `get_current_client` reads `Authorization` (Bearer optional); `/orders`, `/cancel`, `/pay`, cart use `X-Client-Token` (`OptionalClientHeaderDep`). Both go through `_resolve_client_from_token`, which rejects tokens with any `purpose` claim and tokens whose `iat` predates `password_changed_at`.
- Emails are lowercased on every read/write. `authenticate_client` always runs bcrypt (dummy hash for unknown emails). Rate limits are in-memory per process (`slowapi`).
- `ClientAccount.addresses` is not eager-loaded; use `await client.awaitable_attrs.addresses` before serializing (avoids `MissingGreenlet`).
- Register/login/google/reset-password return `{token, client, cart}` (`_build_auth_response`) and accept an optional `cartToken` merged tolerantly.
- **Google** (`POST /auth/google`): verifies the ID token with `PyJWKClient` (needs `cryptography`). Matches by `google_sub`; an existing email account → `409` (no auto-linking). `is_verified` from Google's `email_verified`.
- **Email verification**: stateless JWT with `purpose: "email_verify"`, 24 h. Soft: blocks nothing. Verifying links guest orders by email (`link_guest_orders_by_email`, same transaction) — never on unverified registration.
- **Password reset**: DB-backed single-use `PasswordResetToken` (only `sha256` stored, 30 min TTL, previous ones invalidated). `forgot-password` always returns the same 200; Google-only accounts get the webhook with `hasPassword: false` and no token. Password changes (reset or `PATCH /auth/me`, which needs `current_password`) set `password_changed_at`.
- Addresses: `/auth/me/addresses`; one default per client via partial unique index (clear old default first). `zip_code` `^\d{5}$`; `latitude`/`longitude` come from the frontend (it calls envia.com Geocodes directly; this backend never geocodes).

### Orders history and guest checkout

- `Order` is the only record of an order anywhere. `client_account_id` nullable FK (no cascade) with `guest_email`; `CHECK ck_orders_has_identity`. `items`/`delivery_info` are JSON snapshots.
- `GET /auth/me/orders[/{uuid}]` are Postgres-only.
- Guests: require `deliveryInfo.contactInfo.email`, can't use `addressUuid` or coupons. `get_order_for_action(db, client, order_id)` resolves ownership for `/pay`, `/cancel`, `DELETE`, `GET /orders/guest/{id}`: with a client, by ownership; without, `order_id` matches `sicar_order_id` or `Order.uuid` with `client_account_id IS NULL`. Once linked to an account, the guest path 404s by design.
- `order_display_service.resolve_client_name` handles guest names (separate module to avoid circular imports).
- `dispatch_status` is purely local: `accept_order` sets `PENDING` and enqueues `ACCEPT`; `advance_order_dispatch_status` follows `_LEGAL_DISPATCH_TRANSITIONS` (`PENDING`→`PREPARING`→`COMPLETE`→`DISPATCHED`).

### Cart

- `Cart.items` = `[{uuid, quantity}]` only; every read re-joins live `Product` (`cart_service._enrich`), unavailable lines get `available: false`.
- Identity: `X-Client-Token` account first, else the `charly_cart_token` httpOnly cookie (`app/core/cookies.py`, path `/v1/cart`, `SameSite=None; Secure` in production, `Lax` in development). `client_account_id` nullable + unique.
- `GET` never creates; `PUT`/`PATCH /items` silently mint an anonymous cart; `POST /cart/merge` (Authorization) 404s on a bad token. `Cart.coupon_code` is a preview only.

### Wishlist

Account-only. `WishlistCollection` (one `is_default` "Favoritos" per client, partial unique index, created lazily on first `PUT /wishlist/favorites/{uuid}`) and `WishlistItem` (unique per collection+product). Add/remove are idempotent; deleted/inactive products stay with `available: false`.

### Coupons (`coupon_service.py`)

- `discount_type` `PERCENTAGE` (optional cap) / `FIXED_AMOUNT`; `scope_type` `ORDER` / `CATEGORY` (descendant-inclusive) / `PRODUCT`; limits `max_total_uses`, `max_uses_per_client`, `first_purchase_only`, `coupon_assigned_clients`; window and `min_order_amount`.
- Authoritative only in `POST /orders`: `lock_and_validate_coupon` takes `SELECT ... FOR UPDATE` on the coupon, counts `PENDING`+`CONFIRMED` redemptions, then `create_redemption`.
- `CouponRedemption`: `PENDING` → `CONFIRMED` (on `TO_PAY`→`PAID`) or `RELEASED` (on cancel while pending). A `CONFIRMED` redemption is never released, even on refund (anti-abuse).

### Reviews

`product_reviews` (one per client per product, admin hide/reply) and `product_review_helpful_votes`. `Product.average_rating`/`reviews_count` are recomputed fully (`review_service.recompute_product_rating`) after any change to the visible set. `isVerifiedPurchase` is computed once at creation.

### Admin (`/v1/admin/*`)

- Auth: `AdminUser` JWT (`ADMIN_JWT_SECRET`, 12 h) from `POST /admin/auth/login`, via `get_current_admin` on every admin router (no `x-api-key`). Roles `super_admin` / `staff`; `require_super_admin` (403) guards admin management, audit log, refunds, and `DELETE` of categories/vehicles/coupons/synonyms. Admins are deactivated, never deleted. First admin: `railway run python create_admin_user.py --email ... --name ... --password ...`.
- Audit: `audit_service.log_action` inline in the mutation's transaction, for the high-value mutations only (accept/cancel/refund, admins, coupons, deletes, reviews, brands, sales, synonyms).
- Refunds: `POST /admin/orders/{uuid}/refund` (partial or full, `PAID` with an MP id, amount ≤ total − prior refunds). `Refund` rows are the single source of truth for refunded amounts (customer/admin full refunds insert one too). A refund never changes `Order.status` or stock.
- System events (chargebacks, sync failures, stock drift) use `logger.critical` + admin webhooks, not the audit log.
- **envia.com shipping** (`shipping_service.py`; only `DELIVERYMAN` orders in `COMPLETE`):
  - `state` must be 2-3 chars: `_normalize_mx_state` converts names to ISO 3166-2:MX codes.
  - `/ship/rate/` returning the same `meta:"error"` for every carrier → `502` with that message instead of empty options.
  - `/ship/generate/` needs `shipment.{carrier,service,type}` nested plus `settings` (`printFormat: "PDF"`, `printSize: "STOCK_4X6"`); `meta:"error"` with HTTP 200 → `502`.
  - Options with `dropOff != 0` (branch pickup/dropoff) are filtered out; branch codes are unsupported.
  - Generate persists `Order.shipping_label` (incl. `shipmentId`, exposed to customers) and sets `DISPATCHED` immediately; no outbox. Cancel clears it and reverts to `COMPLETE`.
- Dashboard stats aggregate only `PAID`, non-deleted orders; top products/categories unpack `Order.items` with `jsonb_array_elements`.

### Auth summary

Every non-admin route needs `x-api-key` (`validate_api_key`), except `POST /payments/webhook`. Admin routes use the admin JWT only. `X-Client-Token` must stay in CORS `allow_headers`.

## Error tracking, tests, logging

- `app/core/error_tracking.py`: `capture_exception`/`capture_message` always log; also report to Sentry if `init_error_tracking(service)` found `SENTRY_DSN`. Catch-all exception handler in `main.py`.
- Tests: `tests/` (pytest + pytest-asyncio). `conftest.py` runs `alembic upgrade head` once per session and wraps each test in a rolled-back transaction. External services are monkeypatched at the service-function boundary. CI: `.github/workflows/tests.yml` with Postgres 16 and a Typesense service container. If the DB connection fails with `connection was closed in the middle of operation`, check the Postgres server log (usually wrong credentials).
- Logs: API → `app.log`; worker → `sync.log` (`RotatingFileHandler`, 10 MB × 5). Separate logger configs.
