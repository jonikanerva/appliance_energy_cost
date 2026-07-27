# Product Vision

## Vision

Home Assistant already knows how much energy each tracked appliance consumes and what electricity costs each hour — but it never answers the owner's actual question: *"what did this appliance cost me, in money?"* Appliance Energy Cost turns existing per-appliance cumulative energy sensors and one all-inclusive dynamic price sensor into trustworthy euro figures: a live cost sensor that accrues at the price in force at the moment of consumption, and a one-time, explicitly confirmed backfill that reconstructs past cost from the hourly history Home Assistant already stores. Everything is local, Home-Assistant-native, and deliberately conservative with the statistics database — the user should never fear that a restart, a price outage, or an import silently corrupted their numbers.

## Goal

Give any existing cumulative energy sensor a cumulative money-cost sensor priced at consumption time — live from now on, and backwards in time via a safe, preview-first import into long-term statistics.

## Core Principles

- **Price at the moment of consumption.**
  Energy consumed during one price period is settled at that period's price — never retro-priced at a later price, live or in backfill.

- **The price sensor is the only price authority.**
  The user points the integration at one all-inclusive price sensor (e.g. Nord Pool spot + transfer + taxes + margin). The integration converts units (Wh/kWh/MWh, ¤/kWh vs ¤/MWh) but never adds fees, VAT, or margins of its own.

- **Never corrupt history.**
  Statistics are written only through supported Recorder APIs — preview first, explicit confirm required, overlap protection on by default, overwrite an explicit named exception, no direct SQL ever. The integration touches only its own statistic IDs.

- **Local and HA-native.**
  No cloud, no accounts, no telemetry. Lean on Home Assistant primitives — `utility_meter` for daily/monthly cycles, `RestoreEntity` for restart safety, long-term statistics for history — instead of re-implementing them.

- **Fail visibly, degrade explicitly.**
  An unavailable price or energy source leads to a defined, documented behaviour and a visible state — never silent zeros, silently dropped energy, or fabricated values.

## Product Shape

1. User installs the integration via HACS.
2. In a UI config flow, the user selects the price sensor and adds appliances (name + existing cumulative energy sensor). Each appliance gets a cost sensor (`device_class: monetary`, `state_class: total`).
3. Cost sensors accrue money live, survive restarts, and feed long-term statistics — usable in dashboard cards and statistics graphs.
4. Optionally, the user runs the preview-backfill service, inspects the returned summary (points, totals, gaps), then runs the import service with an explicit confirm — historical cost appears in long-term statistics.
5. Daily and monthly cost figures are built with Home Assistant's own `utility_meter` on top of the cost sensors (documented recipe, not reimplemented).

## Non-Goals & Drift Guardrails

The product must not become:

- A power-estimation engine (that is [powercalc](https://github.com/bramstroker/homeassistant-powercalc)'s job) — we only cost energy that an existing sensor already measured.
- A tariff or billing engine — no standing charges, VAT fields, tariff blending, contract modelling, or invoice reconciliation (the same exclusion [dynamic_energy_cost](https://github.com/martinarva/dynamic_energy_cost) makes; the all-inclusive price sensor owns that complexity).
- A statistics administration tool — no general-purpose editing, deleting, or repairing of Recorder data beyond importing the integration's own cost series.
- An Energy Dashboard replacement or a custom frontend — entities render through standard HA cards.

Drift signals to flag when proposing UX, copy, or features — do not:

- add price-component fields (fee, tax, margin, multiplier stacking) beyond simple unit conversion.
- write to the Recorder database outside the supported statistics import APIs, or touch statistic IDs the integration does not own.
- auto-calibrate or auto-import anything; every history-affecting action stays explicit, previewed, and confirmed.
- reimplement `utility_meter` cycles, forecasting, or optimisation inside the integration.

If a feature makes the product feel more like powercalc, an energy billing system, or a database admin tool, it is the wrong direction.

## Decision Filter

A proposed change should only be accepted if it clearly supports the core experience.

Ask:

1. Does it make the per-appliance cost figures more accurate, more trustworthy, or easier to verify?
2. Does it work fully locally through supported Home Assistant APIs, with no direct database writes and no cloud dependency?
3. Does it preserve the integrity of existing recorded history — no double counting, no silent overwrites, no touching statistics the integration does not own?
4. Does it stay within costing existing energy sensors — rather than estimating power, modelling tariffs, or building dashboards?

If not, it should not be added.

## Success Definition

The product succeeds when the user feels:

- "I can see what my heat pump cost me today, this month, and last winter — in euros, not just kWh."
- "I trust the numbers: a restart, a price outage, or a meter reset never silently corrupts them."
- "The backfill was boring, in the best way: preview, confirm, done — and nothing else in my database changed."
- "It feels like a built-in Home Assistant feature, not a bolted-on app."

## Persistence and Privacy Posture

- **Persisted on-device:** config entry data/options (price sensor entity ID, per-appliance name + energy sensor entity ID, currency); each cost sensor's cumulative value and last-priced energy baseline via `RestoreEntity`; imported hourly cost rows in Recorder long-term statistics under the integration's own statistic IDs.
- **Transmitted off-device:** nothing.
- **Never persisted:** raw source payloads or copies of other sensors' history; modifications to statistics the integration does not own; credentials or tokens; any PII.
- **Telemetry / analytics:** none.

## Audience & Voice

- **Primary audience:** Home Assistant power users who already have per-device energy sensors and a dynamic electricity price sensor (Nord Pool and similar markets), and who care that the money figures are correct enough to act on.
- **Tone:** technical and terse — copy states units, timezones, and consequences plainly ("imports hourly rows in UTC; existing rows block the import unless you explicitly allow overwrite").

## Open Questions

- Currency handling beyond a single configured currency per entry (v1: one currency from config, default EUR; no multi-currency).
- Price-gap policy: v1 prices energy accumulated during a price outage at the price in force when the price returns; is a stricter policy (hold and discard, or mark degraded) wanted later?
- Backfill for daily/monthly `utility_meter` sensors (v1: out of scope — long-term-statistics charts cover historical days/months).
