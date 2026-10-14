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
| A · Lowest Cost | Lowest total catalog amount, then fewer units and greater runtime reserve. Unavailable if any eligible model has no price. A blank currency uses THB based on the user's confirmation; an explicit non-THB/conflicting currency or unknown/mixed VAT also makes the scenario unavailable. Missing source dates are shown as a warning. |
| B · Best Runtime | Highest minimum estimated runtime across units carrying a positive load, then fewer UPS units. Zero-load-only UPS units have runtime marked not applicable. |
| C · Maximum Expansion | Highest total explicitly supported external-battery pack count; unknown capacity is not counted. |
| D · Minimum UPS Count | Fewest UPS units, then greater runtime reserve. |
| E · Best Overall | Fewest UPS units, then lowest comparable catalog cost when available, then greater runtime reserve. |

The local catalog snapshot on 2026-10-14 has no currency recorded for eligible UPS prices. The user confirmed that these catalog amounts are THB, so the optimizer reports that as a user-confirmed assumption rather than source data. VAT is unknown for some records, so Scenario A remains unavailable until the VAT basis is completed and comparable.

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
