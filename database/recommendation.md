# UPS recommendation engine

## Local API

Start the standard-library HTTP service from the repository root:

```powershell
python -X utf8 database/recommendation_api.py --db database/local/ups-products.sqlite3 --port 8000
```

The service binds only to `127.0.0.1` because the SQLite catalog contains reseller price observations. It validates `Host` and any supplied `Origin` against loopback names and the bound port to reject DNS-rebinding requests. It exposes `GET /health` and the local `POST /api/recommendations`, `/api/allocations` and `/api/optimizations` endpoints.

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
| Outlet fitness | 15% | `min(100, verified battery-backed socket count / required device outlets * 100)` |
| Price efficiency | 10% | Lowest comparable-currency reseller amount per rated watt receives 100; remaining candidates scale relative to it |
| Expansion capability | 10% | 100 when external batteries are explicitly supported; 0 when explicitly unsupported |

The weighted score is the weighted mean of known factor scores. Unknown factors are omitted and their weight is reflected in `evidenceCoveragePercent`. Candidates sort by runtime status first (`meets_runtime`, `unknown`, then `below_runtime_requirement`), then weighted score and evidence coverage. Capacity candidates whose known runtime is below target remain in the top five with an explicit below-target status.

Catalog coverage depends on the current private snapshot. Legacy counts of total outlets or dealer-source runtime curves are not counts of manufacturer-verified backup sockets or battery configurations.

## Evidence and limits

- Ratings, price offers and technical source rows are read from SQLite. Automatic recommendations require a verified manufacturer identity and manufacturer-sourced W/VA; unresolved manufacturer conflicts are not silently resolved by picking a value. Legacy source observations and prices remain available for audit.
- UPS-typed product identities whose product description clearly identifies a battery pack/module or replacement battery are excluded before source rows are combined. This prevents an accessory SKU and its price from being paired with runtime/capacity evidence for the UPS it supports; complete UPS systems sold with an external battery pack remain eligible. The count appears under `excludedProductCount.nonUpsAccessory`.
- Runtime uses manufacturer-sourced points bound to an explicit product and battery configuration. When more than one configuration is available the caller must select one for a definite runtime; interpolation is inside known points only, with no extrapolation.
- Outlet scoring uses only groups explicitly verified as battery-backed. Total sockets, surge-only sockets and hardwired terminals are not interchangeable with battery-backed plug sockets.
- Estimated reseller cost uses explicit NDP or Dealer Price rows and excludes SRP, end-user, warranty-upgrade, and pack-tier rows. It preserves the source amount and VAT treatment. This single-model ranking scores only prices explicitly marked VAT-exclusive; inclusive or unknown treatments receive no price score. Fleet cost comparison has a separate policy that can normalize an inclusive offer when its effective dates and VAT rate are verified. When extra battery packs have no verified price, UPS-only prices are not ranked as complete configuration costs. Amounts are historical observations, not current quotations.
- Historic model-only/SKU merges are quarantined from automatic recommendations until the bundle identity is verified; the original offers and source references are retained.
- Load inputs that cannot be represented as finite JSON numbers are rejected with HTTP 400. Runtime estimates, scores, and the API serializer reject non-finite values rather than returning invalid JSON.

## Test cases

Run the dedicated engine/API acceptance cases:

```powershell
python -X utf8 -m unittest database.tests.test_recommendation_engine database.tests.test_recommendation_api -v
```
