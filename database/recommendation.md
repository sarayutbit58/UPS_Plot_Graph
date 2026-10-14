# UPS recommendation engine

## Local API

Start the standard-library HTTP service from the repository root:

```powershell
python -X utf8 database/recommendation_api.py --db database/local/ups-products.sqlite3 --port 8000
```

The service binds only to `127.0.0.1` because the SQLite catalog contains reseller price observations. It exposes `GET /health` and `POST /api/recommendations`.

Example request:

```json
{
  "devices": [
    { "name": "PoE Switch", "quantity": 2, "watt": 200, "va": 250 }
  ],
  "powerFactor": 0.8,
  "runtimeRequirementMinutes": 10
}
```

Each device needs a name, positive integer quantity, and at least one of `watt` or `va`. If one load unit is missing, the API converts using the supplied power factor. When both are present it retains both explicit values and reports a warning if they conflict with the PF assumption. One output receptacle is assumed per device. `limit` is currently fixed to the five best candidates by default; the engine accepts limits from 1 to 20 for API tests/internal callers.

The JSON response contains `recommendedUps`, four `alternativeUps` when enough products qualify, and the full `recommendations` list (up to five). Each row includes capacity margins, runtime status/estimate, estimated reseller cost with source, per-factor scores, evidence coverage, explanation strings, and source references.

## Ranking formula

Capacity eligibility requires both UPS output ratings to cover the load with at least 20% engineering margin:

```text
W margin  = UPS rated W  / required W  - 1
VA margin = UPS rated VA / required VA - 1
eligible  = W margin >= 0.20 and VA margin >= 0.20
```

Capacity fitness rewards a 30% target margin and penalizes larger over-sizing:

```text
capacity = clamp(100 - 100 * abs(min(W margin, VA margin) - 0.30), 0, 100)
```

The 20% minimum follows APC guidance that UPS output watt capacity is generally 20–25% above connected load; the 30% target preserves the existing calculator helper's 1.3 headroom default. [APC UPS sizing guide](https://www.apc.com/us/en/support/product-support/ups-buying-guide-for-selecting-a-battery-backup-system.jsp)

The factors and weights are:

| Factor | Weight | Score |
|---|---:|---|
| Capacity fitness | 35% | Formula above |
| Runtime fitness | 30% | `min(100, estimated runtime / requested runtime * 100)` |
| Outlet fitness | 15% | `min(100, source outlet count / required device outlets * 100)` |
| Price efficiency | 10% | Lowest comparable-currency reseller amount per rated watt receives 100; remaining candidates scale relative to it |
| Expansion capability | 10% | 100 when external batteries are explicitly supported; 0 when explicitly unsupported |

The weighted score is the weighted mean of known factor scores. Unknown factors are omitted and their weight is reflected in `evidenceCoveragePercent`. Candidates sort by runtime status first (`meets_runtime`, `unknown`, then `below_runtime_requirement`), then weighted score and evidence coverage. Capacity candidates whose known runtime is below target remain in the top five with an explicit below-target status.

On the local catalog snapshot inspected on 2026-10-14, the API forms 806 recommendation identities from 817 UPS product rows by consolidating 11 exact model-to-manufacturer-SKU aliases. Runtime curves are available for 68 identities (including 12 exact APC matches from the official overlay); outlet counts for 40; expansion capability for 87; and explicit NDP/Dealer amounts for 758. None of the reseller price observations has a recorded currency, so every live price-efficiency score is currently unknown.

## Evidence and limits

- Ratings, price offers, technical source rows, and source locations are read from SQLite. If source ratings conflict, the engine uses the lowest recorded VA/W ratings and warns.
- Runtime uses exact manufacturer-key/model or SKU matching. The engine uses its APC runtime overlay in [manufacturer_runtime_curves.json](manufacturer_runtime_curves.json) plus eligible Full load/Half load points stored in `product_sources.raw_text`. It interpolates only between known points and never extrapolates. Each runtime result includes its source URL or local source path and row locator. The APC overlay is from Schneider Electric's [Smart-UPS SMC/SMT technical specification](https://ckm-content.se.com/ckmContent/sfc/servlet.shepherd/document/download/069Kj00000UXGLEIA5), published in 2018; table test conditions are not specified there.
- Outlet counts and external-battery support are scored only when a source row states them. The engine compares outlet counts only; it does not allocate equipment to receptacle types.
- Estimated reseller cost uses explicit NDP or Dealer Price rows and excludes SRP, end-user, warranty-upgrade, and pack-tier rows. It preserves the source amount and VAT treatment. The current database has no explicit currency or offer date, so `currency` and `sourceDate` remain null and price efficiency is not scored. The amount is a historical source observation, not a current quotation.
- The local catalog has 817 UPS rows; exact SKU aliases are grouped for ranking where a model-only row matches one explicit manufacturer SKU. This creates one recommendation per exact product identity without fuzzy-name joining.

## Test cases

Run the dedicated engine/API acceptance cases:

```powershell
python -X utf8 -m unittest database.tests.test_recommendation_engine database.tests.test_recommendation_api -v
```
