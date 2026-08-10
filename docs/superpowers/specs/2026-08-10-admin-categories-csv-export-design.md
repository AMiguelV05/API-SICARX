# Admin CSV export: categories + products

## Problem

The admin dashboard has no way to see, in one file, which products are assigned to which
categories across the whole taxonomy. Reviewing this today means walking the tree in the
UI one category at a time (`GET /admin/categories/{uuid}/products`). This adds a one-shot
CSV export an admin can download and open in a spreadsheet.

## Scope

One new endpoint, one new service function. No new tables, no migrations, no new schema
file. Reuses `Category`, `product_categories`, `Product`, and the existing
`get_descendant_uuids` recursive CTE.

## Endpoint

`GET /admin/categories/export`

- Same `validate_admin_key` gate as every other route in `admin_categories.py` (router-level
  dependency, nothing new needed there).
- Optional query param `categoryUuid`: scopes the export to that category's subtree
  (itself + all descendants, via `get_descendant_uuids`). Omitted exports the entire tree.
- `404` (`"Categoria no encontrada."`) if `categoryUuid` is given but doesn't resolve to a
  real `Category` row — same lookup-then-404 pattern already used by
  `list_category_products` and every other `{category_uuid}`-scoped admin route.
- Returns `Response(media_type="text/csv", headers={"Content-Disposition": "attachment;
  filename=categorias_productos.csv"})` — no `response_model`, mirroring
  `admin_bulk_import_template`'s existing `Response`-with-`Content-Disposition` pattern in
  `admin_bulk_import.py`.

## Row shape

One row per (category, product) pair. Columns, in order:

```
category_uuid, category_path, category_slug, product_sku, product_name, product_price, product_stock
```

- `category_path` is the full ancestor chain joined with `" > "` (e.g. `Herramientas >
  Electricas > Taladros`), not just the category's own name — categories can nest to
  arbitrary depth and two categories can share a name under different parents, so the bare
  name alone doesn't disambiguate.
- `product_stock` sources from `Product.available_stock` (the hybrid property: `GREATEST(stock
  - reserved, 0)`), not raw `Product.stock` — matches the existing customer-facing
  `ProductBasic.stock` aliasing convention, so the export reflects what's actually sellable
  right now, not the raw SICAR count net of open local reservations.
- A category with zero assigned products still gets exactly one row, with the four
  `product_*` columns blank — achieved via an outer join, not a separate pass.
- A category whose only assigned products are all `is_deleted == True` also gets exactly
  one blank-product row (indistinguishable from a genuinely empty category in this export)
  — the `is_deleted == False` filter lives on the join's `ON` clause, not a `WHERE`, so it
  narrows which products attach without dropping the category row itself.
- Sort order: by `category_path` (so nested categories group together and read
  top-to-bottom the way the tree does), then by `product_name` within a category.

## Data flow

1. If `categoryUuid` given: `db.get(Category, category_uuid)` → 404 if missing; then
   `get_descendant_uuids(db, category_uuid)` for the allowed set (includes the node itself).
2. Fetch **all** categories flat (`uuid, name, parent_uuid`) in one query — the table is
   small (human-administered PIM data, not synced from SICAR X) — and build a `uuid ->
   full path string` map by walking each node to its root. This is independent of step 1's
   scoping, so a scoped export still shows correct ancestor names even when an ancestor
   itself falls outside the exported subtree.
3. Main query: `Category` outer-joined to `product_categories` outer-joined to `Product`
   (the `Product` join's `ON` clause carries `Product.is_deleted == False`), optionally
   filtered to `Category.uuid IN (<descendant set>)` from step 1.
4. Zip each result row's `category_uuid` against the path map from step 2 for
   `category_path`.
5. Sort rows as described above, write via Python's `csv` module into an `io.StringIO`,
   encode as `utf-8-sig` (BOM) so accented characters render correctly when the file is
   opened directly in Excel — the CSV content will be Spanish-language product/category
   names, same convention as the rest of this codebase's logs/docstrings.

## Implementation

- `app/services/taxonomy_service.py`: new `export_categories_csv(db, category_uuid: str |
  None) -> bytes` doing steps 1-5 above.
- `app/api/routes/admin_categories.py`: new `admin_export_categories` route calling it and
  wrapping the result in the `Response` described above.

## Error handling

- `categoryUuid` given but not found → `404`, same message/style as existing category
  lookups (`"Categoria no encontrada."`).
- No categories / no products at all → still `200` with a header-only (or category-rows
  with blank products) CSV — not an error, an empty tree is a valid state.

## Testing

No automated test suite exists in this repo (per `CLAUDE.md`). Verified manually against
the dev DB:

- Full export (no `categoryUuid`) — spot-check row count and a couple of known
  category/product pairs.
- Scoped export on a category that has children — confirm only that subtree's categories
  appear, and that `category_path` still shows the correct ancestors above the scoped root.
- A category with zero assigned products — confirm it appears once with blank product
  columns.
- `categoryUuid` for a nonexistent uuid — confirm `404`.
- Open the resulting CSV in a spreadsheet app — confirm accented characters (é, í, ñ, etc.)
  render correctly, not as mojibake.
