"""Derived weight analytics from canonical daily-minimum files only (pure functions; no I/O beyond reading the
Longevity current-data directory). Audit revisions, temp files and malformed files are excluded. Missing days are
never zero and are never carried forward."""
import datetime as dt
import json
import math
import re

CANONICAL = re.compile(r'^daily_weight_min_(\d{4}-\d{2}-\d{2})\.json$')
SCHEMA = 'longevity.withings.daily_min.v1'


def load_canonical(directory, entity_id):
    """Return {date: record} for valid canonical files of the configured entity; plus count of rejected files."""
    days, rejected = {}, 0
    for path in directory.iterdir():
        m = CANONICAL.match(path.name)
        if not m or path.is_symlink() or not path.is_file():
            continue                                   # revisions (_revision_), temp files and others are not canonical
        try:
            if path.stat().st_size > 10000:
                raise ValueError
            rec = json.loads(path.read_text(encoding='utf8'))
            day = dt.date.fromisoformat(m.group(1))
            value = float(rec['value'])
            if (rec.get('schema') != SCHEMA or rec.get('date_local') != m.group(1) or rec.get('unit') != 'kg'
                    or rec.get('entity_id') != entity_id or not math.isfinite(value) or not 40 <= value <= 250):
                raise ValueError
            days[day] = {'value': value, 'selected_observed_at': rec.get('selected_observed_at'),
                         'date_basis': rec.get('date_basis', 'home_assistant_recorder_last_changed_proxy')}
        except (OSError, ValueError, KeyError, TypeError):
            rejected += 1
    return days, rejected


def summarise(days, today, window=7, recent=14):
    """Latest completed observation day (strictly before today) and the mean over the `window` calendar days that end
    on yesterday, using only days that have a selected observation."""
    end = today - dt.timedelta(days=1)
    start = end - dt.timedelta(days=window - 1)
    done = {d: r for d, r in days.items() if d <= end}
    latest_day = max(done) if done else None
    in_window = [done[d]['value'] for d in sorted(done) if start <= d <= end]
    recent_days = [{'date': d.isoformat(), 'kg': round(done[d]['value'], 2)}
                   for d in sorted(done, reverse=True) if d >= end - dt.timedelta(days=recent - 1)]
    return {
        'latest_day': latest_day.isoformat() if latest_day else None,
        'latest_value': round(done[latest_day]['value'], 2) if latest_day else None,
        'latest_age_days': (end - latest_day).days if latest_day else None,
        'window_start': start.isoformat(), 'window_end': end.isoformat(), 'window_days': window,
        'days_with_data': len(in_window),
        'mean': round(sum(in_window) / len(in_window), 2) if in_window else None,
        'recent_days': recent_days,
    }
