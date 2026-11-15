# Longevity apps for Home Assistant

Code-only Home Assistant app (add-on) repository with two independent, local-first apps:

| App | Purpose |
|---|---|
| `longevity_health_collector` | Reads configured Home Assistant sensors once a day and writes write-once JSON snapshots to an external disk; selects the lowest valid daily body-weight reading; publishes derived weight analytics and health/heartbeat entities. |
| `longevity_drive_bridge` | Non-destructive, content-addressed copy-and-verify of the external Longevity folders to Google Drive (`drive.file` scope), with a gated restore mode. |

This repository contains **source code, synthetic tests and documentation only**. It contains no
measurements, account-specific entity names, archive identifiers or credentials. All of those are
private runtime options set in the Home Assistant app configuration, or private files under the
app's own `addon_configs` folder.

## Install

1. Settings → Apps → App store → ⋮ → Repositories → add this repository URL.
2. Install the app(s). Both install with safe defaults: dry run, single run, no collection or
   upload approved, no entity publishing, empty mappings.
3. Turn **off** automatic updates for both apps. Updates are pulled by the Supervisor and should be
   installed deliberately after reviewing the release and `PROVENANCE.json`.

## Collector options

| Option | Default | Meaning |
|---|---|---|
| `dry_run` / `collection_approved` / `run_once` | `true` / `false` / `true` | Live collection needs `dry_run: false` **and** `collection_approved: true`. |
| `collect_hour`, `collect_minute` | 23:45 | Daily cycle time in `timezone`. |
| `timezone` | `UTC` | IANA zone defining the local calendar day. |
| `weight_entity` | empty | `sensor.*` entity holding body weight in kg. Empty disables weight work. |
| `metrics` | `[]` | Entries `key|sensor.entity_id|source|category` for daily snapshots. |
| `publish_entities` | `false` | Publish the four derived/health entities below. |
| `heartbeat_minutes` | 15 | Health entity refresh interval (5–60). |
| `bridge_interval_hours` | 24 | Expected Bridge cycle interval used for its freshness check. |

Invalid options stop the app before any read or write (fail closed).

### Published entities (local only, not registry entities)

- `sensor.longevity_weight_daily_min`: lowest valid reading of the latest **completed** local day with a canonical file; attributes give the day, age and the date-basis caveat.
- `sensor.longevity_weight_7d_mean`: mean of the daily minima in the 7 completed calendar days ending yesterday, with `days_with_data`; `unknown` when there are none. Missing days are never zero or carried forward.
- `sensor.longevity_collector_health`: `ok` or `degraded` (`problem`: storage unavailable, daily cycle failed or overdue), refreshed every heartbeat.
- `sensor.longevity_drive_bridge_health`: `ok`, `stale`, `failed` or `unknown`, from the Bridge's run-status file.

No measurement values appear in logs. A stale `last_heartbeat` attribute means the collector itself has stopped.

## Bridge options

`root_folder_id`, `mode` (`archive`/`restore`), `restore_approved`, `production_approved`, and
`pinned_hashes` (entries `folder/name=<sha256>` that a restore must reproduce exactly). The OAuth
`token.json` is created by the owner in the app's private config folder and is mounted read-only.

## Weight rule

See `docs/WEIGHT_RULE.md`.
