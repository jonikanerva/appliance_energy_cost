# STACK.md — Python 3.13 / Home Assistant custom integration (HACS) profile

> A strict-typed Home Assistant **custom integration** distributed through HACS, written in async Python. The same correctness-first principles as the TypeScript/Effect and Swift profiles — model impossible states as impossible, validate at the boundary, keep the critical path unblocked, add no dependency without justification — expressed through Home Assistant's own idioms (asyncio event loop, `DataUpdateCoordinator`, config-entry lifecycle, `manifest.json` requirements) rather than against the grain of the ecosystem.
>
> **Normative.** `MUST`, `MUST NOT`, `SHOULD`, and `MAY` are binding as written. When this document conflicts with product scope, `VISION.md` decides product intent and this file decides implementation mechanics. When it conflicts with Home Assistant's own developer rules or the [Integration Quality Scale](https://developers.home-assistant.io/docs/core/integration-quality-scale/), **Home Assistant wins** — surface the conflict before deviating.

---

## 0. Project shape

- **Shape:** Home Assistant custom integration (a `custom_components/<domain>/` package), installed via HACS, not a standalone app.
- **Critical execution path:** the Home Assistant **asyncio event loop**. It is single-threaded and shared with the entire instance; blocking it degrades every integration and the UI. This is the direct analog of "the main actor / one display frame" (Swift) and "the per-request hot path" (TS) — the event loop is sacred and MUST NOT block.
- **Applicable states:** every entity handles awaiting-first-data, success, empty, degraded, offline, error (plus product-specific). In HA these map to coordinator status (`last_update_success`), entity `available`, and `unknown` / `unavailable` states — not to ad-hoc flags scattered across platforms.
- **Recommended repository layout:**

```txt
repo/
  custom_components/
    <domain>/
      __init__.py          # async_setup_entry / async_unload_entry, wiring only
      manifest.json        # domain, name, version, requirements (pinned), iot_class
      const.py             # DOMAIN, defaults, config keys — no logic
      config_flow.py       # boundary: user input validated with voluptuous / helpers
      coordinator.py       # DataUpdateCoordinator[T] — the polling + typed-data seam
      api.py               # thin async client wrapping the upstream (own PyPI lib preferred)
      models.py            # frozen dataclasses / TypedDict — the decoded domain shape
      entity.py            # shared CoordinatorEntity base
      sensor.py            # (and other platforms) — thin, render coordinator data
      diagnostics.py       # async_redact_data-backed diagnostics
      strings.json
      translations/en.json
      quality_scale.yaml   # tracked quality-scale rule status
  tests/                   # pytest + pytest-homeassistant-custom-component
  hacs.json                # HACS repository metadata
  mise.toml                # pinned tool/runtime versions (Python, uv, ruff, mypy)
  pyproject.toml           # ruff, mypy, pytest, uv dev tooling config
  .github/workflows/       # hassfest + HACS validate + verify
  STACK.md
  VISION.md
  CLAUDE.md
```

- **Package boundaries (enforced structurally):**
  - `models.py` and any pure-logic module MUST NOT import `homeassistant.*` I/O, `aiohttp`, or perform I/O. Pure functions compute; the decoded domain shape is framework-free.
  - `api.py` is the only module that talks to the upstream service. Everything downstream consumes **decoded, narrowed** data, never raw payloads.
  - Platform files (`sensor.py`, …) render coordinator data into entities and contain no business rules and no I/O.

---

## 1. Language & Runtime

- **Primary language:** Python 3.13 (`from __future__ import annotations` in every module).
- **Runtime version is not freely chosen — it tracks the Home Assistant release you target.** Set `manifest.json` / CI to the Python version the targeted HA core requires (Home Assistant 2025.x requires **Python 3.13**; do not target a version HA no longer supports, and do not back-deploy to an older Python than HA's minimum). When you bump the supported HA version, re-verify the Python floor first.
- **Strictness mode:** `mypy --strict` with zero errors. Additionally enable `disallow_any_explicit`, `warn_unreachable`, `warn_redundant_casts`, and `no_implicit_optional`. Type checking is **the first reviewer** — prefer designs where a mistake is a type error rather than a runtime surprise. Add the integration to a strict-typing gate; new warnings are not allowed.
- **Typing discipline:**
  - Model impossible states as impossible: frozen `@dataclass(frozen=True, slots=True)` for domain values, `enum.StrEnum` / `typing.Literal` for closed sets, tagged unions resolved with `match`.
  - `ConfigEntry` MUST be typed via a `type MyConfigEntry = ConfigEntry[MyData]` alias and `runtime_data` used for per-entry state — never module-level globals or `hass.data[DOMAIN]` dictionaries of untyped values for new code.
  - Prefer `TypedDict` for structured dict boundaries; prefer explicit narrowing over `cast`.
- **Dev-environment provisioning:** [`mise`](https://mise.jdx.dev/) is the single bootstrap. `mise install` provisions **every pinned tool and runtime version** from `mise.toml` — the exact Python 3.13 interpreter, `uv`, `ruff`, and `mypy` — so a fresh checkout reaches a reproducible environment with one command. `mise.toml` is the source of truth for tool/runtime versions.
- **Python dependency manager:** [`uv`](https://docs.astral.sh/uv/) (itself provisioned by mise) resolves and locks the dev/test dependencies. Wire it as a mise task (e.g. `mise run setup` → `uv sync`) so `mise install` followed by that task fully bootstraps. The **integration's own runtime dependencies** are declared in `manifest.json → requirements` (HA's contract), never in `pyproject.toml`; `pyproject.toml` + `uv.lock` govern the **development / test** environment only.
- **Pinning surfaces (three layers, each owns one):** `mise.toml` pins tool/runtime versions; `uv.lock` pins dev dependencies; `manifest.json → requirements` pins exact runtime versions with `==`.

---

## 2. Frameworks

| Concern                | Framework / library                                                              | Notes                                                                                     |
| ---------------------- | -------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------- |
| Platform               | Home Assistant Core (integration APIs)                                           | Follow the Integration Quality Scale; do not fight core conventions                       |
| Concurrency            | `asyncio` — `async`/`await`, `async_add_executor_job` for unavoidable blocking   | Never block the event loop; no bare threads                                               |
| Polling / data seam    | `DataUpdateCoordinator[T]`                                                        | The single place that fetches + owns typed data; do not roll your own polling loop        |
| Entities               | `CoordinatorEntity` subclasses per platform                                      | Thin; render coordinator data; expose `available`                                         |
| Config / options       | Config flow (`ConfigFlow`, `SchemaConfigFlowHandler` where it fits)              | UI-based setup required; no YAML-only config for new integrations                         |
| Boundary validation    | `voluptuous` (HA's built-in) for config/service schemas                          | Validate every user input and service call payload before use                             |
| HTTP client            | `aiohttp` via `homeassistant.helpers.aiohttp_client.async_get_clientsession(hass)` | Use the **shared** session; never create your own; no blocking `requests`               |
| Upstream API wrapper   | A dedicated async PyPI library (yours or third-party), pinned in `requirements`   | HA prefers the protocol/device logic to live in a separate published package              |
| Persistence            | Config entry data/options; `homeassistant.helpers.storage.Store`; `RestoreEntity` | Minimal; see §5                                                                            |
| Logging                | stdlib `logging` — `_LOGGER = logging.getLogger(__name__)`                        | No `print()`; structured, level-appropriate                                               |
| Diagnostics            | `homeassistant.components.diagnostics` + `async_redact_data`                     | Redact secrets/PII from downloadable diagnostics                                          |
| Testing                | `pytest` + `pytest-homeassistant-custom-component` + `pytest-asyncio`             | Async tests, mock HA, snapshot with `syrupy`                                              |
| Time in tests          | `freezegun` / HA's `async_fire_time_changed`                                      | Deterministic — no wall-clock sleeps                                                       |
| Lint + format          | `ruff` (lint **and** format)                                                      | HA core's own choice; replaces black/isort/flake8/pylint                                  |
| Type checker           | `mypy --strict`                                                                   | The compiler-as-reviewer analog                                                           |
| Manifest / repo checks | `hassfest` + HACS validation (GitHub Actions)                                     | Must pass in CI                                                                            |

---

## 3. Build & verify commands

Bootstrap the environment with `mise install` (provisions the pinned tools/runtime) before running any command below. `pyproject.toml` (via `uv`) is the single source of truth for tool configuration. Never invoke `ruff`, `mypy`, or `pytest` with ad-hoc flags from commits, CI, or agent scripts — go through the declared commands below so local and CI behaviour cannot drift.

| Variable      | Command                                                                       |
| ------------- | ----------------------------------------------------------------------------- |
| `$FORMAT_CMD` | `uv run ruff format .`                                                         |
| `$LINT_CMD`   | `uv run ruff check . && uv run mypy custom_components`                         |
| `$BUILD_CMD`  | `uv run python -m compileall -q custom_components` (syntax gate — there is no compile step) |
| `$TEST_CMD`   | `uv run pytest`                                                                |
| `$VERIFY_CMD` | `uv run ruff format --check . && uv run ruff check . && uv run mypy custom_components && uv run pytest` (format-check → lint → type-check → tests) |

> Python has no build artifact, so `$BUILD_CMD` maps to a **bytecode-compile syntax gate** over the integration package. The ecosystem's structural gates — `hassfest` and HACS validation — cannot run locally in a custom-integration repo (`script.hassfest` lives in the Home Assistant core repository), so they run in CI as the `home-assistant/actions/hassfest` and `hacs/action` GitHub Actions. `$VERIFY_CMD` is what any agent must run and report on before claiming completion; the PR must additionally pass the hassfest and HACS-validate actions in CI.

---

## 4. Performance budgets

- **Event loop:** MUST NOT be blocked. Any call that does file/network/CPU-bound work synchronously runs via `hass.async_add_executor_job`. HA actively detects and warns on blocking calls inside the loop — treat such a warning as a build failure.
- **Setup latency:** `async_setup_entry` returns quickly; a slow or unreachable device raises `ConfigEntryNotReady` (HA retries with backoff) rather than blocking startup.
- **Polling interval:** `update_interval` MUST be justified against the upstream's cost and rate limits. Default no faster than the product genuinely needs; prefer push (webhooks/subscriptions) over aggressive polling where the device supports it.
- **Upstream calls** MUST have: a timeout, bounded concurrency (no unbounded fan-out), a typed failure, and a retry-or-explicit-no-retry decision. Coordinator failures surface as `UpdateFailed`, not silent empties.
- **Memory / entity count:** create only the entities the product needs; avoid per-poll object churn on the hot path.

---

## 5. Persistence shape

- **Storage primitives (in order of preference):**
  1. **Config entry** `data` (immutable connection details) and `options` (user-tunable) — the default home for configuration.
  2. **`Store`** (`homeassistant.helpers.storage.Store`, versioned JSON) for small integration-owned state that must survive restarts.
  3. **`RestoreEntity`** for restoring last known entity state across restarts.
- **Do not** write your own files, open databases, or persist to arbitrary paths. Do not stash mutable runtime state in module globals — use `entry.runtime_data`.
- **Persisted entities:** declared by `VISION.md → Persistence and Privacy Posture`. Default is "as little as possible."
- **Schema migration policy:** `Store` is versioned; provide an `async_migrate_func`. Config entries use `async_migrate_entry` with a bumped `entry.version`. A decode/migration failure degrades gracefully (re-setup / re-auth), it does not crash.
- **Forbidden persistence:** anything declared forbidden in `VISION.md → Persistence and Privacy Posture`. Never persist raw upstream payloads, secrets in plaintext beyond the config-entry store, or PII the product does not need.

---

## 6. Approved dependencies

Default answer to "should we add a library?" is **no**. Home Assistant enforces this structurally: every runtime dependency MUST be listed in `manifest.json → requirements`, **version-pinned exactly** (`==`), published on PyPI, and ideally pure-Python (wheels for HA's platforms). `hassfest` validates the manifest; unpinned or unlisted imports fail CI.

**Runtime (`manifest.json → requirements`):**

| Dependency                | Pinning | Why it earns its place                                            | Approver  | Date       |
| ------------------------- | ------- | ---------------------------------------------------------------- | --------- | ---------- |
| `<upstream-client-lib>`   | `==x.y.z` | The async client for the target device/service (prefer your own published lib) | (project) | (fill in)  |

> `aiohttp`, `voluptuous`, and the HA helper APIs ship **with Home Assistant** — do not add them to `requirements`; depend on the versions HA provides.

**Development only (`pyproject.toml` dev group, `uv`):**

| Dependency                             | Version                 | Why it earns its place                          |
| -------------------------------------- | ----------------------- | ----------------------------------------------- |
| `homeassistant`                        | matches targeted core   | Type stubs + test harness against real core     |
| `pytest`                               | stable, explicit semver | Test runner                                     |
| `pytest-homeassistant-custom-component`| matches targeted core   | The standard custom-component test fixtures     |
| `pytest-asyncio`                       | stable, explicit semver | Async test support                              |
| `ruff`                                 | stable, explicit semver | Lint + format (HA core's choice)                |
| `mypy`                                 | stable, explicit semver | Strict type checking                            |
| `syrupy`                               | stable, explicit semver | Snapshot tests for diagnostics/entity states    |
| `freezegun`                            | stable, explicit semver | Deterministic time in tests                     |

New runtime entries require a `STACK.md` PR (or ADR) with rationale, owner, approver, and date — and a `hassfest`-clean manifest.

---

## 7. Stack-specific reject-list additions

- **Blocking the event loop:** synchronous `requests`, `open()`/file I/O, `time.sleep`, blocking device SDKs, or any CPU-bound loop called directly from a coroutine. Route blocking work through `hass.async_add_executor_job`.
- **Creating your own `aiohttp.ClientSession`** — use `async_get_clientsession(hass)`.
- **`typing.Any`** (explicit or implicit) without an inline `# reason: ...` justification; `cast()` that bypasses narrowing instead of a runtime guard or `TypedDict`.
- **`# type: ignore`** without a specific code and inline reason naming the underlying constraint.
- **Bare `except:` / `except Exception:` that swallows silently**, and `throw`-and-forget. Catch the upstream's specific exceptions and re-raise as the correct HA exception (`ConfigEntryNotReady`, `ConfigEntryAuthFailed`, `ConfigEntryError`, `UpdateFailed`, `HomeAssistantError`) — see §8 for the taxonomy.
- **`print()`** anywhere in shipped code — use `_LOGGER`.
- **Logging secrets/tokens/PII** at any level; logging raw upstream payloads.
- **Module-level mutable global state** for per-entry data — use `entry.runtime_data` (typed).
- **Rolling your own polling loop / `asyncio.create_task` for polling** — use `DataUpdateCoordinator`.
- **Untyped `hass.data[DOMAIN]` dictionaries** for new code where `runtime_data` fits.
- **YAML-only configuration** for a new integration — a UI config flow is required.
- **`ObservableObject`-equivalent anti-patterns:** ad-hoc `is_loading` / `has_error` flags scattered across entities instead of deriving availability/state from the coordinator.
- **Wildcard imports** (`from x import *`) and dependencies not listed + pinned in `manifest.json`.
- **Naive `datetime` objects** anywhere in logic, storage, caches, or logs; `datetime.utcnow()` / `datetime.now()` without an explicit timezone (both produce naive or local-drifting values). See §10.
- **Local-time storage or computation, and manual UTC-offset arithmetic** — timezone conversion happens only at the request-parse / response-build edges via `dt_util`.

---

## 8. Logging & privacy

- **Logger:** one module-level `_LOGGER = logging.getLogger(__name__)` per module. Levels are meaningful: `debug` for developer detail, `info` sparingly (not per-poll), `warning`/`error` for actionable conditions. Never log inside a tight per-update path at `info`.
- **Typed error taxonomy → HA behaviour.** Every failure mode maps to exactly one HA outcome (this is the ecosystem's version of "errors are values with an explicit descriptor"):

  | Failure                          | Raise                        | HA behaviour                                  |
  | -------------------------------- | ---------------------------- | --------------------------------------------- |
  | Device/service offline at setup  | `ConfigEntryNotReady`        | Retry setup with backoff                      |
  | Bad/expired credentials          | `ConfigEntryAuthFailed`      | Start the reauth flow                         |
  | Unrecoverable config error       | `ConfigEntryError`           | Mark entry failed, surface to user            |
  | Coordinator fetch failed         | `UpdateFailed`               | Entities go `unavailable`, logged once        |
  | User-facing action failed        | `HomeAssistantError`         | Error shown in UI/service response            |

  Define the integration's own exceptions in one place and translate the upstream library's exceptions into this taxonomy at `api.py` — never let a raw upstream exception escape into HA.
- **PII redaction:** downloadable diagnostics MUST route through `async_redact_data` with an explicit `TO_REDACT` set (tokens, coordinates, emails, device identifiers). Default to redacting unknown-sensitive fields rather than exposing them.
- **Crash/telemetry reporting:** none. Home Assistant owns error reporting; do not add third-party analytics or crash reporters.

---

## 9. Background & lifecycle

- **Setup/teardown symmetry:** `async_setup_entry` wires the coordinator, client, and platforms; `async_unload_entry` MUST fully reverse it. Register every listener/unsub/cancel with `entry.async_on_unload(...)` so unload is leak-free. Return the platform-unload result honestly.
- **Allowed background work:** the coordinator's scheduled refresh; push subscriptions/websockets to the device **owned by the entry and cancelled on unload**; long-lived tasks created via `entry.async_create_background_task(hass, ...)` (tied to entry lifecycle), never orphaned `asyncio.create_task`.
- **Forbidden background work:** tasks that outlive the config entry, polling more aggressively than the product needs, background activity that retains data forbidden by `VISION.md`, and any loop that keeps the event loop busy without active need.
- **Reload:** support `async_reload_entry` / options-update reload so config changes apply without a HA restart.

---

## 10. Time & timezones

Time is treated exactly like any other external input: **UTC everywhere internally, converted only at the boundary.** This is the same "decode/narrow at the edge" discipline that §0 applies to data, applied to instants.

- **Internal representation:** all datetimes in logic, coordinator data, `Store`/persistence, caches, and logs are **timezone-aware UTC**. Naive datetimes are forbidden (see §7).
- **Conversion happens only at the two edges:** parsing an inbound request/payload → normalise to UTC immediately; building an outbound response / user-facing value → convert to the target timezone at the last moment. Nothing in between ever holds local time.
- **Python mechanics:** use `datetime.now(UTC)` and aware datetimes. Never `datetime.utcnow()` or `datetime.now()` (both banned — naive/local). Never hand-roll `timedelta` offset math for timezones.
- **Home Assistant mechanics:** use `homeassistant.util.dt` (`dt_util`) rather than raw `datetime` for anything time-of-day-aware:
  - `dt_util.utcnow()` for "now";
  - `dt_util.parse_datetime()` / `dt_util.as_utc()` to normalise inbound values to UTC **at the boundary**;
  - `dt_util.as_local()` **only** when producing a user-facing value.
  - HA stores and computes in UTC and renders in the user's configured timezone — do not fight this.
- **Timestamp entities:** `SensorDeviceClass.TIMESTAMP` (and similar) MUST return timezone-aware UTC datetimes; HA localises them for display.

> The language-neutral UTC-in-logic / convert-at-edges rule lives in `CLAUDE.md → Time`; this section pins the concrete Python/HA mechanics.

---

## 11. Design guidelines & UX thresholds

- **Design authority:** Home Assistant UX conventions. The integration owns no custom frontend — configuration renders through standard HA config-flow steps and selectors; entities follow HA naming and device-class conventions so HA's own UI thresholds apply.
- **Input paths:** whatever HA's frontend provides; nothing custom to verify beyond flow-step correctness.

---

## 12. Best practices source

`architect` and `ux-guardian` consult the current Home Assistant developer documentation before design and review verdicts, and cite the page. **Tool:** the `ctx7` CLI via Bash — `npx ctx7@latest library "<name>" "<question>"`, then `npx ctx7@latest docs <libraryId> "<question>"` (workflow in `~/.claude/rules/context7.md`) — with `developers.home-assistant.io` via WebFetch as fallback. Training-data memory is not an acceptable source for HA API details.

---

## 13. Intentional Divergences

| Date     | CLAUDE.md rule | Divergence | Reason |
| -------- | -------------- | ---------- | ------ |
| _(none)_ | —              | —          | —      |
