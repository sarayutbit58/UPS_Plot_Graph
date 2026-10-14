# UPS design optimization

## Local API and UI

Start the local service from the repository root with the catalog that contains the local price observations:

```powershell
python -X utf8 database/recommendation_api.py --db database/local/ups-products.sqlite3 --port 8000
```

Open `http://127.0.0.1:8000` to use the calculator and the Enterprise scenario-comparison step. The server binds to loopback and serves only the app's HTML, JavaScript and compiled CSS from a fixed allowlist. This keeps browser calls same-origin and keeps catalog prices off public GitHub Pages. A static GitHub Pages page cannot call the local optimizer.

`POST /api/optimizations` accepts the runtime target and device rows:

```json
{
  "runtimeRequirementMinutes": 10,
  "devices": [
    { "name": "PoE switch", "quantity": 2, "watt": 200, "va": 250, "priority": 50 }
  ]
}
```

Input follows the allocation engine's limits and validation: finite positive W and VA for loaded devices, integer quantities and priorities, at least one positive-load device, and a positive runtime target. A row with 0 W and 0 VA still consumes its outlets; a UPS that carries only those zero-load outlets has runtime marked not applicable. The Enterprise screen converts each line's total W/VA and outlet count to per-outlet instances; when a line uses multiple outlets, its load is split evenly across those outlets. Forecast load is added proportionally to the entered device loads without increasing outlet count.

## Constraint solver

The engine uses a deterministic constraint satisfaction search (CSP). Each design chooses integer counts of eligible UPS models and then places device instances onto units. A model is eligible only when its source-backed W, VA, outlet count and runtime curve are all present. The engine reuses the allocation placement checks, including a minimum 20% W/VA engineering margin and runtime interpolation only within published curve points.

The search is deliberately bounded for a local service: at most 12 UPS units per design and never more units than physical outlet instances, up to three distinct model types per design, a 2,000 fleet-candidate cap, a 250,000 total assignment-node cap, and a 1,000-node cap per fleet. Device placement uses backtracking with symmetric-unit pruning for up to 200 physical device instances per fleet. Larger requests use deterministic best-fit placement and are marked incomplete. If an assignment-node or fleet-candidate limit is reached, `search.isComplete` is false and the response identifies results as best among the explored designs.

The response contains:

- `bestSolution`: the Best Overall valid design, or `null` when none is found within the search bounds.
- `topAlternatives`: up to five distinct valid fleet designs, with per-unit load, outlet, runtime, headroom, evidence and engineering justification.
- `scenarios`: a result or an explicit unavailable reason for each scenario.
- `search`: candidate counts, bounded-search limits, pruning and work-budget status.
- `priceComparison`: whether catalog amounts have a comparable basis.

## Scenario ranking

All available scenario results first satisfy W, VA, runtime and outlet constraints.

| Scenario | Ranking |
|---|---|
| A · Lowest Cost | Lowest total catalog amount on an exclusive-VAT THB basis, then fewer units and greater runtime reserve. Unavailable if any eligible model has no price, currency conflicts, VAT is unknown, or an inclusive offer cannot be normalized from a verified VAT period. Missing source dates on exclusive offers are shown as a warning. |
| B · Best Runtime | Highest minimum estimated runtime across units carrying a positive load, then fewer UPS units. Zero-load-only UPS units have runtime marked not applicable. |
| C · Maximum Expansion | Highest total explicitly supported external-battery pack count; unknown capacity is not counted. |
| D · Minimum UPS Count | Fewest UPS units, then greater runtime reserve. |
| E · Best Overall | Fewest UPS units, then lowest comparable catalog cost when available, then greater runtime reserve. |

The local catalog snapshot on 2026-10-14 has 16 eligible models and 76 eligible price offers: 52 are marked exclusive, 24 have unknown VAT, and none are marked inclusive. No eligible offer records currency, source date or an effective period. The user confirmed THB, so the optimizer reports that as a user-confirmed assumption rather than source data. Scenario A remains unavailable because 24 offers have unknown VAT.

## Exclusive-VAT comparison basis

- Explicit `exclusive` offers are compared at their recorded amount.
- An `inclusive` offer is normalized to the pre-VAT amount with `gross × 100 / (100 + rate)` only when its source record provides both `price_effective_from` and `price_effective_to`, and that full effective period maps to a verified Thai VAT-rate period. These are optional ISO date fields preserved in `product_sources.raw_json`; `source_date` alone does not establish when a price was effective. The original catalog amount is never changed.
- The currently supported period is 2025-10-01 through 2027-09-30 at 7%, checked on 2026-10-14 against the Revenue Department's [2025 extension notice](https://www.rd.go.th/59/page-2.html) and [2026 extension notice](https://www.rd.go.th/59/page-1.html). The Revenue Department gives the included-VAT calculation as `gross × 7 / 107`; equivalently, the net amount is `gross × 100 / 107` ([calculation guide](https://rd.go.th/fileadmin/user_upload/SMEs/infographic/info-e-business-63.pdf)).
- An `unknown` VAT treatment, missing effective-period fields, or a price effective period outside a verified period blocks cost comparison. Update the verified-period map when the official rate or dates change.
- `priceComparison.vatBasis` reports `exclusive`; `inclusiveOffersNormalized`, `vatRatePercent`, `vatRatePeriods`, and `priceEffectivePeriods` disclose any normalization applied.

## Tests

Run the optimizer and local API acceptance cases with:

```powershell
python -X utf8 -m unittest database.tests.test_optimization_engine database.tests.test_recommendation_api -v
```

The browser request builder is covered by the project Node test suite:

```powershell
npm test
npm run build:css
```
