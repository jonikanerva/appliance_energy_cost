# Appliance Energy Cost — technical reference

Technical reference — the user guide is the [README](../README.md).

## Source requirements

- **Energy sensors.** One existing *cumulative* energy sensor per appliance:
  `state_class: total` or `total_increasing`, unit Wh, kWh, or MWh. The
  integration never estimates power — only energy an existing sensor has
  already measured is costed. The sensor should declare
  `device_class: energy` to appear in the config-flow picker; the
  authoritative unit and state-class checks run after selection anyway.
- **Price sensor.** One all-inclusive numeric price sensor — spot, transfer,
  taxes, and margins already included — with a `<currency>/<energy>` unit such
  as EUR/kWh or EUR/MWh. Live costing reads its current state. **The
  statistics backfill additionally requires `state_class: measurement`**:
  Home Assistant records hourly `mean` long-term statistics only for
  measurement sensors, and the backfill reads price history exclusively from
  those statistics — without a measurement-class price sensor the backfill has
  no price history to read.
- **Backfill depth.** History can be reconstructed only as far back as *both*
  the energy sensor's and the price sensor's hourly long-term statistics
  reach. The price sensor must have been installed and recording before any
  hour it should price.
- **Recorder** enabled (the Home Assistant default).

## Configuration

### Price sensor and currency

The first step pairs one all-inclusive dynamic price sensor with a currency.
Every appliance added to this entry is costed against this price at the moment
of consumption. One config entry exists per price sensor.

| Parameter | Description |
| --- | --- |
| Price sensor | All-inclusive price per energy unit, for example EUR/kWh or EUR/MWh. The integration converts units but never adds fees, VAT, or margins — this sensor is the only price authority. |
| Currency | Must match the price sensor's unit numerator exactly (defaults to the Home Assistant configured currency). The currency is immutable after creation: to change it, remove this entry and add it again (a mixed-currency cost series would corrupt statistics). |

If the selected price sensor does not declare `state_class: measurement`, the
flow shows an explicit warning step: Home Assistant records no hourly mean
statistics for such a sensor, so live costing works but the historical
backfill will not. Submitting the step continues anyway.

After the entry is created, the flow chains directly into adding the first
appliance.

### Appliances

Each appliance is a named pairing with an existing cumulative energy sensor:

| Parameter | Description |
| --- | --- |
| Name | Display name for the appliance and its cost sensor. |
| Energy sensor | An existing cumulative energy sensor in Wh, kWh or MWh with state_class total or total_increasing. Only energy this sensor has already measured is costed. |

Two appliances in the same entry cannot use the same energy sensor. Add more
appliances at any time with **Add appliance** on the entry page. When the
energy sensor belongs to a device, the cost sensor attaches to that device
and appears on the source's device page, named "Cost" (friendly name
"*Device name* Cost"); a device-less source gets a standalone cost sensor
named "*Name* cost". The integration never creates devices of its own.

One deliberate allowance: two *different entries* may cost the same energy
sensor under different price sensors (a legitimate price-comparison setup) —
but summing their cost sensors on a dashboard double-counts the money.

### Reconfiguring

- **Entry:** the reconfigure flow changes the price sensor only. The currency
  is immutable: to change it, remove this entry and add it again (a
  mixed-currency cost series would corrupt statistics).
- **Appliance:** rename the appliance or swap its energy sensor. Swapping
  keeps the accumulated cost and re-baselines from the new sensor's current
  reading — the baseline belongs to the old meter, so the next reading from
  the new meter re-initialises it without charging.

Renaming a source entity's *entity ID* (in **Settings → Devices & services →
Entities**) needs no reconfigure: the integration follows renames of the
price sensor and every appliance's energy sensor automatically while the
entry is loaded — the configuration updates, the entry reloads once, and the
accumulated cost and baseline survive. A removed source entity leaves its
cost sensor unavailable (a removed price source is a price gap) until the
entry or appliance is reconfigured.

### Configuration errors

| Step | Error | Meaning |
| --- | --- | --- |
| Price sensor | `price_sensor_unavailable` | The selected price sensor is unavailable. Its current state is needed to validate the unit; try again when it has a value. |
| Price sensor | `price_not_numeric` | The price sensor's state is not numeric. |
| Price sensor | `price_unit_unsupported` | The unit is not a per-energy price such as EUR/kWh, EUR/MWh or EUR/Wh. |
| Price sensor | `currency_mismatch` | The price unit's numerator does not match the configured currency. This integration never rescales prices (a subunit price like snt/kWh would be 100x off) — point it at a sensor priced in the configured currency per kWh or MWh. |
| Price sensor | `already_configured` (abort) | A config entry for this price sensor already exists. |
| Appliance | `energy_sensor_unavailable` | The selected energy sensor is unavailable. Its current state is needed to validate the unit; try again when it has a value. |
| Appliance | `energy_unit_unsupported` | The energy sensor's unit is not supported. Expected Wh, kWh or MWh. |
| Appliance | `energy_not_cumulative` | Only cumulative energy sensors (state_class total or total_increasing) can be costed; a power or per-period sensor would corrupt the figures. |
| Appliance | `energy_not_numeric` | The energy sensor's state is not numeric. |
| Appliance | `duplicate_energy_sensor` | Another appliance in this entry already uses this energy sensor. |

## The cost sensor

One cost sensor per appliance: `device_class: monetary`,
`state_class: total`, unit = the entry currency. The displayed value rounds to
two decimals for presentation only; the recorded state is the exact
full-precision value (all money arithmetic is `Decimal` end to end). Because
`state_class` is `total`, the sensor feeds hourly long-term statistics
automatically and renders in standard cards — for historical charts, use a
statistics-graph card over the cost sensor.

**Data updates are event-driven, not polled.** Every state change of the
appliance's energy sensor or the entry's price sensor drives the accrual; no
update interval exists. The backfill services read recorder statistics on
demand and are the only other data path.

Accrual semantics, stated plainly:

- **Price at the moment of consumption.** Each energy delta is charged at the
  price in force when it was metered. On a price change, energy accumulated
  during the outgoing price period is settled at the outgoing price first, so
  a slow-updating energy sensor never shifts consumption onto the next
  period's price.
- **First reading baselines.** The first reading from an energy sensor only
  initialises the baseline — pre-existing consumption is never priced at
  today's price.
- **Price gaps hold, never fabricate.** When the price sensor becomes
  unavailable, non-numeric, or reports an unsupported unit or mismatched
  currency, a price gap starts: the `price_gap_active` attribute turns `true`,
  accrual holds, and energy metered during the gap is priced at the price in
  force when the price returns. The cost sensor stays available during a gap —
  the settled cost is still true.
- **Availability follows the energy sensor.** If the energy sensor is
  unavailable, the cost sensor is unavailable; a price failure is a gap, not
  unavailability.
- **Meter resets are detected, dips are not charged.** A reading that drops
  below 90 % of the previous one is a reset: the new reading is charged as
  consumption since the reset. A smaller dip charges nothing and the priced
  baseline holds at its high-water mark, so the recovery leg is not
  double-charged.
- **Negative prices are legal, negative meters are not.** A negative price
  legally decreases the cumulative cost (which is why `last_reset` is never
  set). A negative cumulative energy reading is rejected visibly and never
  becomes a baseline.
- **Restarts are safe.** The cumulative cost and the last-priced-energy
  baseline are restored across restarts. The restore snapshot is written every
  15 minutes and on graceful shutdown, so after a crash the sensor resumes
  from a snapshot at most 15 minutes old — energy metered in that window is
  settled at the price in force after the restart rather than at its
  consumption-time price.

Attributes: `energy_sensor`, `price_sensor`, `price_gap_active`. Nothing
per-event is exposed as an attribute by design.

## Daily and monthly costs

Because `state_class` is `total`, the cost sensor compiles hourly long-term
statistics automatically, and daily, monthly and yearly figures come straight
from them — no helper entities required (verified against HA 2026.7 in
issue #35):

- **Statistics graph card** — stat type **Change**, period **Day** or
  **Month**: daily bars over a month, monthly bars over a year.
- **Statistic card** — **Change** with a calendar period and offset: cost
  today so far, month-to-date, year-to-date, and yesterday / last month.

Caveats:

- Period boundaries follow the Home Assistant instance timezone — days and
  months are local calendar days and months.
- Card figures are fresh to about 5 minutes (the short-term statistics
  merge); they are display values, not per-state-change entities.
- The Energy dashboard is not applicable: it accepts energy-class sensors,
  not monetary ones.
- Footnote: in fractional-offset timezones (UTC+05:30 and similar), period
  boundaries can be skewed by up to one hour.

### The utility_meter recipe

What `utility_meter` uniquely adds over the cards is a live per-period
*entity* that updates on every source change — for automations, templates,
and gauges. When that is needed, build it with Home Assistant's own
[`utility_meter`](https://www.home-assistant.io/integrations/utility_meter/)
on top of the cost sensor — documented, not reimplemented:

```yaml
utility_meter:
  heat_pump_cost_daily:
    source: sensor.heat_pump_cost
    cycle: daily
    net_consumption: true
    periodically_resetting: false
  heat_pump_cost_monthly:
    source: sensor.heat_pump_cost
    cycle: monthly
    net_consumption: true
    periodically_resetting: false
```

> Entity IDs are generated from the instance language (a Finnish instance
> yields `sensor.<nimi>_kustannus`) — check **Developer Tools → States** for
> your real IDs; the IDs in every example in this reference are illustrative.

Both keys matter:

- `net_consumption: true` — a cost sensor can legally decrease (negative
  prices, downward calibration). Without it, a negative-price dip becomes a
  silent overcount: the dip is discarded but the meter's baseline advances.
- `periodically_resetting: false` — the cost sensor never resets
  periodically. Without it, the delta accrued across an unavailable spell of
  the source is lost.

**Warning:** in the UI, "Net consumption" can only be set when *creating* the
helper (**Settings → Devices & services → Helpers → Utility meter**) — an
existing meter must be deleted and recreated to change it.

Named trade: a downward calibration (see
[calibrate_cost](#calibrate_cost)) lands in that day's meter as a negative
change.

A utility meter starts counting at its creation; it has no history for
earlier days or months. Past days and months come from statistics-graph cards
over the cost sensor's long-term statistics — which the
[backfill](#backfilling-history) can extend backwards in time.

## Backfilling history

### How it works

The backfill reconstructs the cost series for hours that predate the live
sensor, from history Home Assistant already stores:

- It reads the hourly long-term statistics of the entry's price sensor (the
  hourly `mean`, falling back to the hourly `state`) and of each appliance's
  energy sensor (the hourly `change`, converted to kWh).
- Each hour's cost is that hour's consumption times that hour's mean price,
  accumulated into a cumulative series, and written as hourly rows in UTC
  under the cost sensor's own statistic ID through the supported recorder
  import API. Only the integration's own statistic IDs are ever touched.
- Pricing an hour's consumption at the hour's mean price is an approximation
  the live sensor does not make: with sub-hour price periods, consumption-time
  precision within the hour is reduced to the hourly mean.
- Findings are reported per appliance, never silently dropped: hours with
  consumption but no price (`missing_price_hours`, skipped), hours with a
  negative energy change (`invalid_energy_hours`, skipped — the recorder
  already reset-compensates `change`, so a negative value is abnormal), and
  hours with no energy row at all (`energy_gap_hours`, nothing to cost). A
  zero-consumption hour without a price still produces a flat point — its
  cost delta is zero with certainty.
- Nothing is written without preview-and-confirm: `preview_backfill` is a dry
  run, `import_backfill` requires an explicit `confirm: true`, and every
  pre-write gate (strict findings, nothing to import, overlap, continuity,
  metadata mismatch) is evaluated across every selected appliance before
  anything is queued — any failure aborts the whole call with nothing written.
- The import's response is returned only after the rows are committed and
  re-read from the database.

Period boundaries follow one rule everywhere: `start` (inclusive) and `end`
(exclusive) must be top-of-the-hour instants. A value with a timezone is
converted to UTC; a value without one is interpreted in the Home Assistant
timezone; after conversion, minutes, seconds, and microseconds must be zero.
`end` defaults to the start of the current UTC hour, so a partial hour is
never included. Every example below carries an explicit offset for this
reason.

### preview_backfill

Dry run of the historical cost backfill: returns per-appliance point counts,
totals and gaps. Writes nothing — importing is a separate, explicitly
confirmed action.

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `config_entry` | yes | — | The Appliance Energy Cost entry whose appliances are previewed. |
| `start` | yes | — | Start of the period, inclusive. Top-of-the-hour rule above. |
| `end` | no | start of the current UTC hour | End of the period, exclusive. Must be after `start` and not in the future. |
| `appliances` | no | every appliance in the entry | **Energy-sensor entity IDs** to limit the preview to. Also the recovery path when one appliance's missing statistics fail the whole call. |
| `strict` | no | `true` | When on, `ok` is `false` if any appliance has missing-price or invalid-energy hours. When off, `ok` stays `true` and the findings are reported only in the per-appliance counts and ranges. |

The service *only* returns a response: called from a script or automation it
must be given a `response_variable`. In **Developer Tools → Actions** the
response is shown directly.

```yaml
action: appliance_energy_cost.preview_backfill
data:
  config_entry: 1234567890abcdef1234567890abcdef  # pick the entry in the UI
  start: "2025-01-01T00:00:00+00:00"
  end: "2025-06-01T00:00:00+00:00"
response_variable: preview
```

Response shape (illustrative values):

```yaml
start: "2025-01-01T00:00:00+00:00"
end: "2025-06-01T00:00:00+00:00"
expected_hours: 3624       # hours in [start, end)
strict: true
ok: true                   # with strict: false, always true
currency: EUR
price_sensor: sensor.electricity_price
appliances:
  - appliance: Heat pump
    energy_sensor: sensor.heat_pump_energy
    statistic_id: sensor.heat_pump_cost
    valid: true            # no missing-price and no invalid-energy hours
    hourly_points: 3624    # rows the import would write
    first_point: "2025-01-01T00:00:00+00:00"
    last_point: "2025-05-31T23:00:00+00:00"
    total_energy_kwh: 1234.5
    total_cost: 145.27
    end_energy_kwh: 4321.0 # meter reading at end; the cutover formula input
    missing_price_hours: 0
    missing_price_ranges: []
    invalid_energy_hours: 0
    invalid_energy_ranges: []
    energy_gap_hours: 0
```

Notes on the shape:

- `ok` means the source data passed validation at preview time; the import
  separately enforces overlap protection and re-reads history. `ok` exists
  only in the preview response, and is always `true` when `strict: false`.
- Energy gaps do not affect `valid`: absent hours are reported, not treated as
  source-data corruption.
- `total_energy_kwh` and `total_cost` are unrounded — the exact values the
  import would write.
- The `*_ranges` lists show flagged hours as contiguous UTC `[start, end)`
  ranges, capped at 10 ranges; the sibling count field is always exact, so a
  count larger than the ranges cover means the list was capped.
- `end_energy_kwh` is `null` when the period's last energy row carries no
  cumulative state.

### import_backfill

Writes the reconstructed historical cost series into long-term statistics.
Takes every `preview_backfill` field with identical semantics — a pasted
preview call plus `confirm: true` is a valid import call — and adds:

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `confirm` | no | `false` | Must be true for anything to be written. The requirement exists so a pasted preview call can never write history by accident; run preview_backfill first to inspect what would be written. |
| `overwrite_existing` | no | `false` | Proceed although the cost series already has rows in the period, updating the window's matching hourly rows for this integration's own cost series. Hours where the new series has no point keep their existing rows; rows outside the period are never touched; there is no deletion path. |
| `initial_cost` | no | *absent* | Cumulative cost immediately before `start`, for continuing an existing series; negative values are valid. Requires exactly one selected appliance — one import call per appliance when supplying it. A wrong value makes the series step at `start`. |

**Absent `initial_cost` is not the same as `0`.** When it is absent and the
cost series already has rows before `start`, the continuity gate refuses the
import — importing on top of them would step the series at `start`. An
explicit `initial_cost: 0` asserts that the series really starts from zero
and bypasses the gate. When absent and no pre-start rows exist, the series
starts from 0.

With `strict: true` (the default) the import aborts if any appliance has
missing-price or invalid-energy hours. With `strict: false`, flagged hours
are skipped and reported in the receipt — skipped hours' consumption is never
costed, a permanent under-count for the period.

The import never calibrates live cost sensors — joining the live series to
imported history is the separate [calibration](#calibrate_cost) service.
Removing the integration does not delete imported rows.

```yaml
action: appliance_energy_cost.import_backfill
data:
  config_entry: 1234567890abcdef1234567890abcdef
  start: "2025-01-01T00:00:00+00:00"
  end: "2025-06-01T00:00:00+00:00"
  confirm: true
response_variable: receipt
```

Limiting to one appliance and continuing an existing series (note that
`appliances` takes the **energy sensor's** entity ID, never the cost sensor's):

```yaml
action: appliance_energy_cost.import_backfill
data:
  config_entry: 1234567890abcdef1234567890abcdef
  start: "2025-06-01T00:00:00+00:00"
  end: "2025-07-01T00:00:00+00:00"
  appliances:
    - sensor.heat_pump_energy
  initial_cost: 145.27
  confirm: true
response_variable: receipt
```

The receipt (returned only after the rows are committed and re-read from the
database; request it with `response_variable`) is *not* the preview shape:

```yaml
start: "2025-01-01T00:00:00+00:00"
end: "2025-06-01T00:00:00+00:00"
strict: true
overwrite_existing: false
initial_cost: 0.0          # echoes the effective value; 0.0 when absent
currency: EUR
price_sensor: sensor.electricity_price
appliances:
  - appliance: Heat pump
    energy_sensor: sensor.heat_pump_energy
    statistic_id: sensor.heat_pump_cost
    rows_written: 3624     # confirmed by the post-commit read-back
    first_point: "2025-01-01T00:00:00+00:00"
    last_point: "2025-05-31T23:00:00+00:00"
    total_energy_kwh: 1234.5
    total_cost: 145.27     # the cutover formula input
    end_energy_kwh: 4321.0 # the cutover formula input
    missing_price_hours: 0
    invalid_energy_hours: 0
    energy_gap_hours: 0
```

Differences from the preview shape:

- Adds `rows_written` per appliance, and `existing_rows_kept` **only** when
  `overwrite_existing: true` (pre-existing in-window rows the new series had
  no point for, kept as-is).
- Drops `valid`, `hourly_points`, `expected_hours`, and the `*_ranges` lists.
- Has **no `ok` field**: an import that returns at all has passed every gate
  and verified its rows; anything else raises an error with nothing (or, on a
  partial multi-appliance outcome, exactly the reported rows) written.

### calibrate_cost

Sets the cost sensor's cumulative cost to `value` (in the entry currency) and
re-baselines to the energy sensor's current reading — subsequent accrual
continues from the new value. To reset, calibrate to 0 — this records the
whole accumulated cost as one negative change in long-term statistics. There
is no separate reset service.

Never touches recorded statistics; the next hourly statistic records the jump
as one change. Calibration changes the level from the next compiled hour
onward; it cannot move a mismatch already recorded at the imported-to-live
boundary.

| Field | Required | Description |
| --- | --- | --- |
| `value` | yes | New cumulative cost in the entry currency; negative values are valid. One value belongs to one cost sensor — call the service once per sensor. During a price gap the value you set supersedes energy tracked but not yet priced; it will not be charged again when the price returns. |

The target must be exactly one cost sensor; targeting several (an area, a
label, a list) is refused as a whole. Calibration also refuses when the
energy sensor has no usable numeric reading — the new baseline must come from
a real reading — and when the reading is negative.

```yaml
action: appliance_energy_cost.calibrate_cost
target:
  entity_id: sensor.heat_pump_cost
data:
  value: 145.44
```

The optional response maps the entity ID to a receipt with `old_cost`,
`new_cost`, `old_baseline_kwh`, `new_baseline_kwh`, `currency`, and
`price_gap_active`. The same old → new record is always written to the log at
INFO level.

## Cutover: joining live to imported history

Importing history never changes the live sensor: after an import, the live
cumulative value still starts wherever live accrual started (typically 0),
while imported history ends at the receipt's `total_cost`. The cutover is one
`calibrate_cost` call that makes the live series continue the imported one.

### Standard cutover

Best case: run the import soon after configuring the appliance, before the
live sensor has compiled its first hourly statistics row (otherwise use the
[retro-fix](#retro-fix-the-sensor-ran-live-before-the-import)).

1. Run `preview_backfill` over `[history start, current hour)` and inspect
   the points, totals, and gaps.
2. Run `import_backfill` over the same period with `confirm: true`, with a
   `response_variable` — the receipt's `total_cost` and `end_energy_kwh` are
   the formula inputs. **Use the receipt's values, not the preview's**: they
   differ whenever `initial_cost` is non-zero.
3. Calibrate each cost sensor:

   ```txt
   value = receipt total_cost
         + (current meter reading in kWh − receipt end_energy_kwh)
         × current price per kWh
   ```

   Worked example: receipt `total_cost: 145.27`, `end_energy_kwh: 4321.0`;
   the meter now reads 4322.4 kWh and the current price is 0.12 EUR/kWh →
   `value = 145.27 + (4322.4 − 4321.0) × 0.12 = 145.44` (rounded here for
   readability). Convert the meter reading to kWh and the price to per-kWh
   first if your sensors report Wh/MWh — `end_energy_kwh` is always kWh.

4. [Verify the boundary](#verifying-the-boundary).

Failure modes, each with its remedy:

- **The price changed since the import's end.** The formula is valid only
  within the same price hour as the import's end; if the price changed,
  re-run the preview and import up to the start of the current hour and
  calibrate from the new receipt.
- **The meter reset after the import's end.** The post-cutoff consumption is
  the full current reading (consumption since the reset):
  `value = total_cost + current reading × current price`.
- **`end_energy_kwh` is `null`.** The period's last energy row carried no
  cumulative state — re-run the preview over a period whose last hour does;
  never guess the reading.
- **The calibration value was wrong.** Correct it with another
  `calibrate_cost` within the same hour and long-term statistics never record
  the mistake; a later correction leaves a paired jump (a wrong change in one
  hour, its inverse in another).

One import call per appliance whenever `initial_cost` is supplied, and one
`calibrate_cost` call per cost sensor always.

Note the restore staleness window: after an unclean shutdown the live sensor
resumes from a snapshot at most 15 minutes old. A calibration is a state jump
like any other in that regard — recalibrate if a crash swallowed it.

### Retro-fix: the sensor ran live before the import

If the cost sensor has been live for days or weeks before the import, its
already-compiled statistics start from 0 at the hour it went live. Importing
only the pre-live window then leaves a recorded mismatch at the boundary
where imported history meets the live rows — and calibration cannot fix a
mismatch that is already recorded. The remedy is to move the boundary to now:

1. Run `preview_backfill` over the whole `[history start, current hour)`.
2. Run `import_backfill` over the same period with `overwrite_existing: true`
   and `confirm: true`. Matching hourly rows are updated; hours where the new
   series has no point keep their existing rows; rows outside the period are
   never touched.
3. Calibrate with the receipt exactly as in the standard cutover.
4. [Verify the boundary](#verifying-the-boundary).

The named trade: the weeks the sensor ran live become hourly-mean-priced
imported data — cost settled live at exact consumption-time prices is
replaced by each hour's consumption at that hour's mean price. Accepted
consciously, once.

### Verifying the boundary

Check the hour where imported history meets the live series — not just that
the live sensor's current level looks right. In a statistics-graph card (or
**Developer Tools → Statistics**), look at the boundary hour's *change*: it
must look like an ordinary hour of cost, not a spike. The boundary change is
`live sum − imported sum`, so a live series that started from 0 after an
import shows a large negative spike there. Calibration fixes the level from
the next compiled hour onward only; a mismatch already recorded at the
boundary is fixed by the
[retro-fix](#retro-fix-the-sensor-ran-live-before-the-import) — re-importing
over it with `overwrite_existing: true`.

## Known limitations

- **Only energy an existing sensor has measured is costed** — power
  estimation is out of scope; that is what
  [powercalc](https://github.com/bramstroker/homeassistant-powercalc) does.
- **Price-gap policy: the returning price wins.** Energy metered while no
  usable price is in force is priced at the price in force when the price
  returns — not at the price that was current when it was consumed. Two
  sub-cases: (1) if the energy sensor supplies no usable reading at the
  moment a price *changes*, the old-price share cannot be measured, and
  energy accumulated since the last event is priced at the new price at the
  next energy event; (2) a meter reset in the middle of a price gap drops the
  unsettled pre-reset gap energy — an undercharge; the integration never
  fabricates values.
- **Meter resets and dips.** A reading below 90 % of the previous one is a
  reset and the new reading is charged as consumption since the reset; a
  smaller dip charges nothing and the baseline holds at its high-water mark.
  A real consumption decrease that looks like a dip (within 10 %) is
  therefore never credited.
- **Price-unit-change blind spot.** Price history is interpreted in the price
  series' current unit; if the price sensor's unit changed mid-history, older
  previewed figures are wrong — sanity-check the totals.
- **`strict: false` is a permanent under-count.** Skipped hours' consumption
  is never costed.
- **Backfill depth.** History reaches only as far back as *both* source
  sensors' hourly long-term statistics.
- **Money precision.** The live sensor state is exact `Decimal`; service
  responses and long-term statistics rows carry floats.
- **Exact currency matching.** The price unit's numerator must equal the
  configured currency (case-insensitive) — no symbol or subunit equivalence,
  so `€/kWh` or `snt/kWh` never match `EUR` (tracked in issue #14). One
  currency per entry, fixed at creation.
- **No `utility_meter` backfill.** Imported history lands in the cost
  sensor's long-term statistics; utility meters start counting at creation.
  Historical days and months come from statistics-graph cards.
- **Entity IDs follow the instance language** — see the note in
  [Daily and monthly costs](#daily-and-monthly-costs).
- **Source renames are followed only while the entry is loaded.** Two gaps
  share one visible state: (1) a source renamed while the integration is not
  loaded cannot be reconciled afterwards — the configuration stores only the
  old entity ID, and no old→new map exists once the rename has happened; (2)
  a second rename landing during the reload window of the first is missed
  the same way. In both cases the cost sensor is unavailable and a setup
  warning names the source; reconfiguring the appliance (or entry, for the
  price sensor) repairs it.
- **Upgrade note: device-linked friendly names gain the device prefix.**
  After upgrading to the version with device links, an appliance whose
  energy sensor belongs to a device renders as "*Device name* Cost" instead
  of "*Name* cost". This is display-only — entity IDs, unique IDs and
  long-term statistics are unchanged, and name overrides made in the entity
  registry survive.

## Troubleshooting

Log messages (logger: `custom_components.appliance_energy_cost`):

| Log line (abridged) | Meaning |
| --- | --- |
| `price gap started (…) — accrual holds` (warning) | The price sensor has no usable state, a non-finite state, an unsupported unit, or a mismatched currency. Costing holds and resumes automatically; the gap energy is priced at the returning price. |
| `usable price in force — energy accumulated without a price is settled at the returning price` (info) | The price gap ended. |
| `has no usable price from … at setup` (warning) | The price source was already unusable when the sensor started; accrual holds until a usable price arrives. |
| `meter reset detected on … — the new reading is charged as consumption since the reset` (warning) | The energy sensor dropped below 90 % of its previous reading (device reboot, counter reset). Cost never decreases from meter behaviour. |
| `small meter dip on … — nothing charged, baseline held` (debug) | A decrease within 10 % — float noise or a small correction. |
| `energy source … became unavailable` / `recovered` (info) | The cost sensor's availability follows the energy sensor. |
| `skipping energy update from …` (warning) | The energy reading is unparseable (unknown state, unsupported unit); logged once per streak. |
| `negative cumulative reading from … rejected` (warning) | Out-of-contract source value; nothing charged, baseline held. |
| `restore data unusable; cumulative cost … restored from the last state` (warning) | After a restart the baseline was lost; the next reading re-baselines without charging. |
| `restore data unusable and the last state was not numeric; cumulative cost restarts at 0` (warning) | Long-term statistics will record a negative step. |
| `Price sensor … was renamed to …` (debug) / no log at all after renaming an energy source | A source entity ID rename is followed automatically while the entry is loaded: the configuration updates, the entry reloads once, cost and baseline survive. No action needed. |
| `Source entity … was removed from the entity registry` (warning) | The cost sensor is unavailable (a removed price source is a price gap). Reconfigure the appliance or entry to point at a replacement sensor. |
| `… now both track … and their summed cost figures double-count` (error) | A rename made two appliances point at the same energy sensor. Reconfigure the *other* named appliance to a different sensor first, then reconfigure this one. |
| `energy source … has no usable state at setup` (warning) | The source is slow to start (recovers by itself once it reports), or it was renamed or removed while the entry was not loaded — reconfigure the appliance to repair that case. |

Service errors, with their remedies:

| Error | Remedy |
| --- | --- |
| `Existing rows in the cost series block the import …` | Narrow the period to hours without existing rows, or set `overwrite_existing: true` to update the matching hours in place. |
| `The cost series already has rows before …` | Supply `initial_cost` with the pre-start cumulative sum to continue the series (one import call per appliance), or re-run the preview and import from the true start of the series. |
| `The price sensor … records no long-term statistics` | The price sensor needs `state_class: measurement`; without hourly mean prices no historical cost can be reconstructed. Fix the sensor, then wait — the backfill can only reach hours recorded after the fix. |
| `… rows expected, … confirmed …` (verification failed) | Re-running the same import is safe: if the rows landed later, overlap protection refuses and nothing is double-written; if they never landed, the re-run imports them. After a partial multi-appliance outcome, re-run with the `appliances` filter narrowed to the appliances missing rows. |
| `This service writes historical cost rows …` (confirm required) | Run `preview_backfill` first to inspect what would be written, then call again with `confirm: true`. |
| `No importable hourly points between …` | Run `preview_backfill` to see what the period contains — commonly the period predates one source's statistics. |

For a bug report, download diagnostics from the integration's entry page
(**Settings → Devices & services → Appliance Energy Cost**). Entity IDs and
names are redacted — the download is safe to attach. File issues at
<https://github.com/jonikanerva/appliance_energy_cost/issues>.
