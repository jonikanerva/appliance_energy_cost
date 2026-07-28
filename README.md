# <img src="https://raw.githubusercontent.com/jonikanerva/appliance_energy_cost/main/custom_components/appliance_energy_cost/brand/icon.png" alt="" width="40"> Appliance Energy Cost

[![Validate](https://github.com/jonikanerva/appliance_energy_cost/actions/workflows/validate.yml/badge.svg)](https://github.com/jonikanerva/appliance_energy_cost/actions/workflows/validate.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Adds a cost sensor to each of your existing appliance energy sensors — money
counted the way the meter counts energy, every reading priced at the price in
force when the energy was actually used.

It does:

- Give every appliance you configure one cost sensor that grows as the
  appliance spends money — live from the moment you add it.
- Price energy at the moment it was used — a later price never re-prices
  earlier consumption.
- Reconstruct past cost from history Home Assistant already stores — preview
  first, nothing is written without an explicit confirm.
- Run entirely locally — no cloud, no accounts, nothing leaves your Home
  Assistant.

It does not:

- Calculate electricity prices. You point it at one price sensor whose value
  is the full price you pay; the integration adds nothing to it.
- Estimate consumption. Only energy an existing sensor has already measured
  is costed.

## Install

You need three things:

- A cumulative energy sensor for each appliance you want to cost — one that
  counts total energy upwards, in Wh, kWh or MWh.
- An all-inclusive price sensor in your currency per kWh or MWh.
- Recorder enabled — it is on by default in Home Assistant.

Then:

1. In HACS, add `https://github.com/jonikanerva/appliance_energy_cost` as a
   custom repository of type **Integration**, download **Appliance Energy
   Cost**, and restart Home Assistant.
2. Go to **Settings → Devices & services → Add integration** and search for
   **Appliance Energy Cost**.
3. The wizard first asks for the price sensor and currency — pick a price
   sensor that already includes transfer, taxes and margins; the integration
   adds nothing on top. (Backfilling history additionally needs a price
   sensor that records statistics — `state_class: measurement`; the wizard
   warns if it does not.)
4. Name your first appliance and pick its energy sensor. Add more appliances
   at any time with **Add appliance** on the integration's page.

## Use

Every appliance you added got one sensor. When the energy sensor belongs to a
device, the cost sensor sits on that device's page and is named **Cost**;
otherwise it is a standalone sensor named **&lt;Name&gt; cost**. All of them
are listed on the integration's page under **Settings → Devices & services**.

The sensor's value is the cumulative money spent since it started counting,
in your configured currency. It survives restarts and keeps growing — a
meter, for money.

To chart costs:

- **Daily or monthly bars:** add a **Statistics graph card** for the cost
  sensor, set the stat type to **Change**, and the period to **Day** (a bar
  per day) or **Month** (a bar per month).
- **Single numbers:** add a **Statistic card** for the cost sensor, choose
  **Change**, and pick a calendar period — today for cost so far today,
  month for month-to-date, or one period back for yesterday or last month.
- The figures refresh about every 5 minutes, and days and months follow your
  Home Assistant timezone.
- The Energy dashboard is not applicable: it accepts energy sensors, not
  money sensors.

## Backfill

A cost sensor starts counting when you add it. If your energy and price
sensors are older, the backfill reconstructs what the appliance cost before
that — the past shows up in the same charts. Nothing is written without a
preview and an explicit confirm.

1. Open **Developer tools → Actions** and pick
   **Appliance Energy Cost: Preview backfill**. Choose the integration entry,
   pick the start of the period at a full hour (minutes 00 — your local time
   is fine), and leave **End** empty to backfill up to now. Run it.
2. Read the summary it returns: `ok` should be `true`, each appliance's point
   count should be roughly the number of hours in your period, and the gap
   counters should be zero. Anything else — check the technical reference
   before importing.
3. Pick **Appliance Energy Cost: Import backfill**, fill in the same values,
   tick **Confirm**, and run it. The rows-written count in the response
   confirms the history landed.
4. If the cost sensor was already counting before you imported, join the two
   series — otherwise the chart shows a large drop where imported history
   meets the live sensor. Follow "Cutover" in the technical reference.

## Remove

- Remove a single appliance from the integration's page — its cost sensor is
  removed with it.
- Remove the whole integration from **Settings → Devices & services**, then
  uninstall the repository in HACS and restart Home Assistant.

Cost history already recorded or imported stays in your database — removing
the integration never deletes the old numbers.

---

Detailed technical reference — configuration options, action fields and
responses, cutover, known limitations, troubleshooting:
[docs/reference.md](https://github.com/jonikanerva/appliance_energy_cost/blob/main/docs/reference.md)

Licensed under the [MIT License](LICENSE).
