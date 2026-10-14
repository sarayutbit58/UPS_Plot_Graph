# UPS load allocation

## API

Start the local recommendation service as described in `recommendation.md`. The allocator is available at `POST /api/allocations` and uses the same read-only SQLite catalog.

```json
{
  "selectedUps": {
    "mode": "auto",
    "models": [
      { "productId": 1019, "quantity": 1 },
      { "productId": 1020 }
    ]
  },
  "runtimeRequirementMinutes": 10,
  "devices": [
    { "name": "PoE switch", "quantity": 2, "watt": 400, "va": 500, "priority": 5 }
  ]
}
```

Use `mode: "fixed"` to allocate only the specified UPS counts; each model must have a positive `quantity`. Use `mode: "auto"` to treat any supplied quantities as starting units and allow the allocator to add units from the selected model pool until devices fit or no selected model can pass the constraints. In auto mode, an omitted quantity starts at zero. It never invents a different UPS model. Duplicate product IDs are rejected.

Each physical device instance is placed on one UPS and receives one generic outlet number. Quantities can split across units, but a single device is not divided across UPSs. Higher integer priorities (1–100) are placed first; equal priorities keep input order. Candidate placements use deterministic best-fit packing among already selected UPS units; auto mode adds a new selected model only when no current unit can accept that device. The auto mode is a greedy heuristic, not an optimization solver.

The response includes:

- `deviceAllocations`: each assigned device instance, its UPS unit, outlet number, and post-placement runtime estimate.
- `unassignedDevices`: remaining quantity and the constraints that blocked placement.
- `upsUtilization`: W/VA ratings and usage, outlet usage, runtime, source model identity, and whether the unit was auto-added.
- `outletAllocations`: generic sequential outlet assignments. `outletType` is null because the catalog does not record receptacle types.
- `capacityReport`: required load, rated fleet capacity, the 20% engineering planning limits, assigned load, and overload/margin status.
- `validation`: `isValid` is true only when every device instance has an assignment and W, VA, outlet count, and runtime all pass.
- `engineeringRecommendations`: concise actions derived from the failed constraints.

Malformed input returns HTTP 400. A well-formed request that cannot be fully allocated returns HTTP 200 with `validation.isValid: false` and the partial assignments/unassigned reasons.

## Validation rules

- Every request requires `runtimeRequirementMinutes`, at least one selected UPS model, and at least one device.
- Each device requires name, positive integer quantity, finite positive W and VA, and integer priority from 1 through 100.
- The allocator uses the existing 20% minimum engineering margin for both W and VA: allocated load on each unit cannot exceed its rating divided by 1.20.
- Each UPS unit needs source-backed W, VA, outlet count, and a runtime curve that covers the resulting W load. Unknown outlet count, unknown runtime, and runtime below the requested requirement block placement. Runtime is interpolated only inside published curve points; it is never extrapolated.
- One outlet is consumed by each physical device. Outlet assignments are count-only; plug type and electrical compatibility cannot be confirmed from the current schema/data.
- A device that individually exceeds every selected UPS model's rated W or VA is marked overloaded. The fleet capacity report separately compares aggregate demand with selected fleet capacity in fixed mode.
- The API caps the request at 20 UPS models, 500 UPS units, 500 device rows, and 10,000 physical device instances.

## Evidence limits

The current local catalog has outlet counts for 40 of 806 recommendation identities and runtime curves for 68. A selected UPS with missing outlet or runtime evidence cannot receive a fully validated assignment. The catalog contains no normalized outlet/receptacle type field, so numbered outlets are abstract count-based slots only.

## Tests

Run allocation and API acceptance cases with the rest of the database suite:

```powershell
python -X utf8 -m unittest database.tests.test_allocation_engine database.tests.test_recommendation_api -v
```
