# Daily minimum weight rule (v1.1)

- Policy: the lowest valid body-weight observation per local calendar day (`timezone` option).
  Invalid, unavailable, non-numeric, non-finite, out-of-range (40–250 kg), wrong-unit and
  timezone-naive observations are excluded. Missing is never zero and never carried forward.
- Date basis: Home Assistant recorder `last_changed` is used as an observation-time **proxy**. Many
  integrations expose no true measurement timestamp; a late synchronisation can move a reading to
  the next day and identical consecutive values may not create a state change. Every file records
  this limitation.
- Reconciliation: each daily cycle re-checks the previous seven local days within recorder
  retention. A later lower value revises the canonical `daily_weight_min_<date>.json` and keeps the
  prior selection in an append-only `..._revision_<sha256>.json` audit file; equal or higher values
  never overwrite. Identity mismatches or corrupt files fail closed.
- Derived analytics read **only** canonical `daily_weight_min_<date>.json` files whose content
  matches the filename date, unit, schema and configured entity. The 7-day mean covers the seven
  completed calendar days ending yesterday, averages only days with data, and reports the count and
  latest-data age. Raw recorder charts remain raw and should be labelled as such.
