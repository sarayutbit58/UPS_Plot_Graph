# UPS load allocation

## API

Start the local recommendation service as described in `recommendation.md`. The allocator is available at `POST /api/allocations` and uses the same read-only SQLite catalog.

```json
{
  "selectedUps": {
    "mode": "auto",
    "models": [
      { "productId": 1019, "batteryConfigId": "internal", "quantity": 1 },
      { "productId": 1020, "batteryConfigId": "internal" }
    ]
  },
  "runtimeRequirementMinutes": 10,
  "devices": [
    { "name": "PoE switch", "quantity": 2, "watt": 400, "va": 500, "priority": 5,
      "plugs": [{ "connector": "IEC_C14", "count": 1 }] }
  ]
}
```

Use `mode: "fixed"` to allocate only the specified UPS counts; each selection needs a positive `quantity`. Use `mode: "auto"` to treat supplied quantities as starting units and allow the allocator to add units from the selected pool. The same product can be selected with different `batteryConfigId` values; an identical product/configuration pair cannot be repeated. Omitting the configuration is accepted only when exactly one verified configuration exists.

Each physical device stays on one UPS even when it has multiple plugs. `plugs` is optional and defaults to one connector of unknown type; explicit supported connectors are `IEC_C14`, `IEC_C20` and `NEMA_5_15P`. Connector compatibility is checked only where the manufacturer-backed outlet type is known. A null connector is counted but explicitly marked `not_checked`. Higher integer priorities are placed first. Fixed mode uses best-fit and a bounded backtracking fallback when necessary; `searchComplete: false` means the search did not prove that no feasible arrangement exists. Auto mode remains a greedy heuristic.

The response includes:

- `deviceAllocations`: each assigned device instance, its UPS unit, outlet number, and runtime estimate for the final UPS load.
- `unassignedDevices`: remaining quantity and the constraints that blocked placement.
- `upsUtilization`: W/VA ratings and usage, outlet usage, final runtime, source model identity, whether the unit was auto-added, capacity/outlet source locators, and source conflict warnings. Device-level runtime estimates match this final per-unit runtime.
- `outletAllocations`: one entry per plug with its verified outlet type where known and a `compatibilityStatus`. Outlet numbers are abstract assignments, not manufacturer port labels.
- `capacityReport`: required load, rated fleet capacity, the 20% engineering planning limits, assigned load, and overload/margin status.
- `validation`: `isValid` is true only when every device instance has an assignment and W, VA, outlet count, and runtime all pass.
- `engineeringRecommendations`: concise actions derived from the failed constraints.

Malformed input returns HTTP 400. A well-formed request that cannot be fully allocated returns HTTP 200 with `validation.isValid: false` and the partial assignments/unassigned reasons.

## Validation rules

- Every request requires `runtimeRequirementMinutes`, at least one selected UPS model, and at least one device.
- Each device requires name, positive integer quantity, finite non-negative W and VA, and integer priority from 1 through 100. Both W and VA may be zero for a socket-only row, but the request must contain at least one positive-load device.
- The allocator uses the existing 20% minimum engineering margin for both W and VA: allocated load on each unit cannot exceed its rating divided by 1.20.
- Each UPS unit needs a verified product identity, manufacturer-sourced W and VA, explicitly battery-backed sockets, and a manufacturer runtime curve for the selected battery configuration. Unknown facts or incompatible connectors block placement. Runtime is interpolated only inside published points and never extrapolated; provisional partial loads are rechecked at the final load.
- Hardwired terminals and surge-only sockets are not counted as backup plug sockets. When no connector type is specified, the result must not be interpreted as verified plug compatibility.
- A device that individually exceeds every selected UPS model's rated W or VA is marked overloaded. The fleet capacity report separately compares aggregate demand with selected fleet capacity in fixed mode.
- The API caps the request at 20 UPS models, 500 UPS units, 500 device rows, and 10,000 physical device instances.

## Evidence limits

Catalog completeness depends on the current private snapshot. A displayed total outlet count is not evidence that all outlets have battery backup. Products with unresolved model/bundle identity remain searchable as unknown but cannot be allocated automatically.

## Tests

Run allocation and API acceptance cases with the rest of the database suite:

```powershell
python -X utf8 -m unittest database.tests.test_allocation_engine database.tests.test_recommendation_api -v
```
