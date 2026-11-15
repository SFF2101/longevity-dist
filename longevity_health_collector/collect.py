#!/usr/bin/env python3
"""Longevity Health Collector: read-only HA metrics snapshot, daily minimum weight selection, derived weight
analytics and health heartbeat.
Reads HA via GET /core/api/states and /core/api/history (never changes source entities).
Writes ONLY: /share/Longevity/03_Current_Data/{snapshot_,daily_weight_min_}YYYY-MM-DD.json, the app-private
/data/collector_state.json and, when publish_entities is enabled, the four sensor.longevity_* states via
POST /core/api/states. Reads the Drive Bridge status file /share/Longevity/.runtime/bridge_status.json.
All account-specific entity mappings come from the app options (private runtime configuration).
"""
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from weight_min import query_day, reconcile_min
import mount_guard
import summary
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

VERSION = '0.3.0'
ROOT = mount_guard.ROOT
DEST = ROOT / '03_Current_Data'
RUNTIME = ROOT / '.runtime'
BRIDGE_STATUS = RUNTIME / 'bridge_status.json'
OPTIONS = Path('/data/options.json')
STATE = Path('/data/collector_state.json')
CORE = 'http://supervisor/core/api/'
API = CORE + 'states/'
METRIC_SPEC = re.compile(r'^([a-z0-9_]{1,40})\|(sensor\.[a-z0-9_]{1,120})\|([a-z0-9_]{1,60})\|([a-z0-9_]{1,40})$')
ENTITY_ID = re.compile(r'^sensor\.[a-z0-9_]{1,120}$')
PUBLISHED = ('sensor.longevity_weight_daily_min', 'sensor.longevity_weight_7d_mean',
             'sensor.longevity_collector_health', 'sensor.longevity_drive_bridge_health')

class CollectError(Exception):
    pass


def load_config(raw):
    """Validate the private runtime configuration; fail closed on anything unexpected."""
    cfg = {
        'dry_run': bool(raw.get('dry_run', True)), 'collection_approved': bool(raw.get('collection_approved', False)),
        'run_once': bool(raw.get('run_once', True)),
        'collect_hour': int(raw.get('collect_hour', 23)), 'collect_minute': int(raw.get('collect_minute', 45)),
        'timezone': str(raw.get('timezone', 'UTC')), 'weight_entity': str(raw.get('weight_entity', '')).strip(),
        'publish_entities': bool(raw.get('publish_entities', False)),
        'heartbeat_minutes': int(raw.get('heartbeat_minutes', 15)),
        'bridge_interval_hours': int(raw.get('bridge_interval_hours', 24)),
    }
    if not (0 <= cfg['collect_hour'] <= 23 and 0 <= cfg['collect_minute'] <= 59): raise CollectError('Invalid collection time')
    if not 5 <= cfg['heartbeat_minutes'] <= 60: raise CollectError('Invalid heartbeat interval')
    if not 1 <= cfg['bridge_interval_hours'] <= 168: raise CollectError('Invalid bridge interval')
    if cfg['weight_entity'] and not ENTITY_ID.match(cfg['weight_entity']): raise CollectError('Invalid weight entity option')
    metrics = {}
    for spec in raw.get('metrics', []) or []:
        m = METRIC_SPEC.match(str(spec).strip())
        if not m or m.group(1) in metrics: raise CollectError('Invalid or duplicate metrics option entry')
        metrics[m.group(1)] = (m.group(2), m.group(3), m.group(4))
    cfg['metrics'] = metrics
    try: cfg['zone'] = ZoneInfo(cfg['timezone'])
    except (ZoneInfoNotFoundError, ValueError): raise CollectError('Invalid timezone option') from None
    return cfg


def read_state(entity_id, token):
    url = API + urllib.parse.quote(entity_id, safe='.')
    req = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/json'}, method='GET')
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            state = json.load(response)
            if not isinstance(state, dict) or state.get('entity_id') != entity_id:
                raise CollectError('Unexpected HA state response')
            return state
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise CollectError(f'HA state query failed with HTTP {exc.code}') from None
    except (urllib.error.URLError, TimeoutError):
        raise CollectError('Home Assistant API unavailable') from None


def as_number(raw):
    if raw in ('unknown', 'unavailable', '', None):
        return None
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    return val if math.isfinite(val) else None


def record(name, entity, source, category, state, local_day):
    if state is None:
        return {'entity_id': entity, 'source': source, 'category': category, 'value': None,
                'unit': None, 'availability': 'entity_not_found', 'source_data_date': None,
                'last_updated': None, 'last_reported': None, 'freshness': 'unavailable'}
    attrs = state.get('attributes') or {}
    value = as_number(state.get('state'))
    data_date = attrs.get('data_date') if isinstance(attrs.get('data_date'), str) else None
    # Oura explicitly tags a daily observation with its source date.
    freshness = 'source_date_matches' if data_date == local_day else ('source_date_differs' if data_date else 'source_date_unverified')
    return {'entity_id':entity, 'source':source, 'category':category,
            'value':value, 'unit':attrs.get('unit_of_measurement'),
            'availability':'available' if value is not None else ('unavailable' if state.get('state')=='unavailable' else 'unknown_or_non_numeric'),
            'source_data_date': data_date,
            'last_updated':state.get('last_updated'), 'last_reported':state.get('last_reported'),
            'freshness':freshness}


def collect(local_now, token, metrics, reader=read_state):
    day=local_now.date().isoformat()
    observations={}
    for metric, (entity, source, category) in metrics.items():
        observations[metric] = record(metric,entity,source,category,reader(entity,token),day)
    return {'schema':'longevity.ha.daily_snapshot.v1', 'date_local': day,
            'captured_at': local_now.isoformat(), 'timezone':str(local_now.tzinfo),
            'capture_semantics':'single late-evening state snapshot; activity totals may be incomplete and values may reflect earlier measurements',
            'source_policy':'non-weight snapshots; daily official weight is a separate selected-minimum file; no cross-source summing',
            'measurements':observations}


def write_once(document, dest=DEST):
    if not dest.is_dir() or dest.is_symlink():
        raise CollectError('Longevity current-data directory missing or inaccessible')
    name='snapshot_'+document['date_local']+'.json'
    if not re.fullmatch(r'snapshot_\d{4}-\d{2}-\d{2}\.json',name):
        raise CollectError('Invalid snapshot filename')
    target=dest/name
    if target.exists():
        if not target.is_file() or target.is_symlink():
            raise CollectError('Unexpected existing snapshot path')
        # Immutable: never overwrite or modify a previously captured day.
        return 'exists'
    data=(json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2)+'\n').encode('utf-8')
    if len(data) > 256000:
        raise CollectError('Unexpected snapshot size')
    temp_name=None
    try:
        fd,temp_name=tempfile.mkstemp(prefix='.longevity_',suffix='.tmp',dir=str(dest))
        os.fchmod(fd,0o600)
        with os.fdopen(fd,'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        # Atomically claim the immutable destination; link fails if it already exists.
        try:
            os.link(temp_name, target)
        except FileExistsError:
            return 'exists'
        except OSError:
            raise CollectError('Filesystem cannot guarantee atomic no-overwrite snapshot placement') from None
        return 'created'
    finally:
        if temp_name and os.path.exists(temp_name): os.unlink(temp_name)


def atomic_json(path, doc):
    """Replace a small private status document atomically (status files only; never health records)."""
    if path.parent.is_symlink(): raise CollectError('Invalid status directory')
    path.parent.mkdir(mode=0o700, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix='.status_', suffix='.tmp', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf8') as f:
            json.dump(doc, f, sort_keys=True); f.write('\n'); f.flush(); os.fsync(f.fileno())
        os.replace(temp, path); temp = None
    finally:
        if temp and os.path.exists(temp): os.unlink(temp)


def load_state():
    try: return json.loads(STATE.read_text(encoding='utf8'))
    except (OSError, ValueError): return {}


def save_state(state):
    try: atomic_json(STATE, state)
    except (OSError, CollectError): pass


def bridge_health(now_utc, interval_hours, path=BRIDGE_STATUS):
    """Classify Drive Bridge health from its private status file (written by Bridge >= 0.4.0)."""
    try:
        st = json.loads(path.read_text(encoding='utf8'))
        last_attempt = dt.datetime.fromisoformat(st['last_attempt']); last_success = st.get('last_success')
        last_success = dt.datetime.fromisoformat(last_success) if last_success else None
        result = str(st.get('last_result', 'unknown'))[:40]
    except (OSError, ValueError, KeyError, TypeError):
        return 'unknown', {'reason': 'status_unavailable'}
    attrs = {'last_attempt': last_attempt.isoformat(), 'last_success': last_success.isoformat() if last_success else None,
             'last_result': result, 'interval_hours': interval_hours, 'mode': str(st.get('mode', ''))[:20],
             'version': str(st.get('version', ''))[:20]}
    grace = dt.timedelta(hours=interval_hours + 2)
    if result in ('running', 'restore_pass', 'restore_dry_run_pass', 'dry_run_pass'):
        state = 'ok' if now_utc - last_attempt <= grace else 'stale'
    elif result != 'archive_pass':
        state = 'failed'
    elif last_success is None or now_utc - last_success > grace:
        state = 'stale'
    else:
        state = 'ok'
    return state, attrs


def publish(token, entity_id, state, attrs, opener=urllib.request.urlopen):
    if entity_id not in PUBLISHED: raise CollectError('Refusing to write a non-Longevity entity')
    body = json.dumps({'state': state, 'attributes': attrs}).encode()
    req = urllib.request.Request(API + entity_id, data=body, method='POST',
                                 headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
    with opener(req, timeout=20) as resp:
        if resp.status not in (200, 201): raise CollectError('HA state publish failed')


def heartbeat(cfg, token, health, now, opener=urllib.request.urlopen):
    """Publish derived weight entities and health; values stay inside Home Assistant (no external export)."""
    now_utc = now.astimezone(dt.timezone.utc)
    common = {'date_basis': 'home_assistant_recorder_last_changed_proxy (sync time, not weigh-in time)',
              'source_files': 'canonical daily_weight_min files only; revision audits excluded'}
    if health.get('mount') == 'pass' and cfg['weight_entity']:
        days, rejected = summary.load_canonical(DEST, cfg['weight_entity'])
        s = summary.summarise(days, now.date())
        publish(token, PUBLISHED[0], s['latest_value'] if s['latest_value'] is not None else 'unknown',
                dict(common, unit_of_measurement='kg', device_class='weight', icon='mdi:scale-bathroom',
                     friendly_name='Longevity weight · daily minimum (derived)', observation_day=s['latest_day'],
                     age_days=s['latest_age_days'], recent_days=s['recent_days'], rejected_files=rejected), opener)
        publish(token, PUBLISHED[1], s['mean'] if s['mean'] is not None else 'unknown',
                dict(common, unit_of_measurement='kg', device_class='weight', icon='mdi:chart-line',
                     friendly_name='Longevity weight · 7-day mean of daily minima (derived)',
                     window_start=s['window_start'], window_end=s['window_end'], days_with_data=s['days_with_data'],
                     window_days=s['window_days']), opener)
    publish(token, PUBLISHED[2], health['state'], dict(health['attrs'], friendly_name='Longevity Health Collector health',
            icon='mdi:heart-pulse', last_heartbeat=now_utc.isoformat(), version=VERSION), opener)
    if health.get('mount') == 'pass':
        bstate, battrs = bridge_health(now_utc, cfg['bridge_interval_hours'])
    else:
        bstate, battrs = 'unknown', {'reason': 'storage_unavailable'}
    publish(token, PUBLISHED[3], bstate, dict(battrs, friendly_name='Longevity Drive Bridge health',
            icon='mdi:cloud-check', checked_at=now_utc.isoformat()), opener)


def daily_cycle(cfg, token, now):
    for lag in range(7, 0, -1):
        day = now.date() - dt.timedelta(days=lag)
        path = DEST / ('daily_weight_min_' + day.isoformat() + '.json')
        if not cfg['weight_entity']: break
        try:
            chosen = query_day(day, cfg['zone'], token, cfg['weight_entity'])
        except (ValueError, urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            raise CollectError('Weight history retrieval failed; ' + type(exc).__name__) from None
        if chosen:
            result = reconcile_min(chosen, path)
            if result in ('created', 'revised'): print('WEIGHT MIN PASS — ' + day.isoformat() + '; ' + result, flush=True)
    filename = DEST / ('snapshot_' + now.date().isoformat() + '.json')
    if not cfg['metrics']:
        print('SNAPSHOT SKIPPED — no metrics configured', flush=True)
    elif not filename.exists():
        result = write_once(collect(now, token, cfg['metrics']))
        print(('SNAPSHOT PASS — local date ' + now.date().isoformat() + ', metrics ' + str(len(cfg['metrics'])))
              if result == 'created' else 'SNAPSHOT EXISTS — no overwrite', flush=True)
    else:
        print('SNAPSHOT EXISTS — no overwrite', flush=True)


def main(clock=lambda zone: dt.datetime.now(zone), sleep=time.sleep):
    cfg = load_config(json.loads(OPTIONS.read_text(encoding='utf8')))
    zone = cfg['zone']; hh, mm = cfg['collect_hour'], cfg['collect_minute']
    print(f"Longevity Health Collector v{VERSION}; dry_run={cfg['dry_run']}; run_once={cfg['run_once']}; "
          f"collection_approved={cfg['collection_approved']}; metrics={len(cfg['metrics'])}; "
          f"weight={'configured' if cfg['weight_entity'] else 'disabled'}; publish={cfg['publish_entities']}", flush=True)
    try:
        g = mount_guard.check()
        print('MOUNT GUARD PASS — external Longevity volume (' + g['fstype'] + '), ' + str(g['directories']) + ' directories', flush=True)
    except mount_guard.MountGuardError as exc:
        if cfg['dry_run'] or cfg['run_once'] or not cfg['publish_entities']:
            raise CollectError(str(exc) + '; no measurements captured') from None
        print('MOUNT GUARD FAIL — ' + str(exc) + '; no measurements captured; will retry', flush=True)
    token = os.environ.get('SUPERVISOR_TOKEN', '')
    if not token: raise CollectError('Home Assistant API token unavailable in app environment')
    if cfg['dry_run']:
        print('DRY RUN PASS — destination and token environment found; no health values read or files written', flush=True)
        return
    if not cfg['collection_approved']: raise CollectError('Live collection is not enabled')
    state = load_state(); last_beat = None
    while True:
        now = clock(zone)
        try:
            mount_guard.check(); mount = 'pass'; error = None
        except mount_guard.MountGuardError:
            mount = 'fail'; error = 'storage_unavailable'
        planned = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        due = cfg['run_once'] or (now >= planned and state.get('last_cycle_day') != now.date().isoformat())
        if mount == 'pass' and due:
            try:
                daily_cycle(cfg, token, now)
                state['last_cycle_day'] = now.date().isoformat()
                state['last_cycle_success'] = now.astimezone(dt.timezone.utc).isoformat(); state.pop('last_error', None)
                save_state(state)
                print('DAILY COLLECTION PASS — ' + now.date().isoformat(), flush=True)
            except (CollectError, OSError, ValueError) as exc:
                state['last_error'] = 'daily_cycle_failed'; save_state(state)
                print('DAILY COLLECTION FAIL — ' + str(exc), flush=True)
                if cfg['run_once']: raise
        if cfg['publish_entities'] and (last_beat is None or now - last_beat >= dt.timedelta(minutes=cfg['heartbeat_minutes'])):
            last_ok = state.get('last_cycle_success')
            overdue = False
            if last_ok:
                overdue = now.astimezone(dt.timezone.utc) - dt.datetime.fromisoformat(last_ok) > dt.timedelta(hours=26)
            error = error or state.get('last_error') or ('daily_cycle_overdue' if overdue else None)
            health = {'mount': mount, 'state': 'ok' if not error else 'degraded',
                      'attrs': {'mount_guard': mount, 'last_cycle_success': last_ok, 'problem': error,
                                'schedule_local': f'{hh:02d}:{mm:02d}'}}
            try:
                heartbeat(cfg, token, health, now); last_beat = now
            except (CollectError, OSError, ValueError, urllib.error.URLError, TimeoutError) as exc:
                print('HEARTBEAT FAIL — ' + type(exc).__name__, flush=True)
        if cfg['run_once']: return
        sleep(60)


if __name__ == '__main__':
    try: main()
    except (CollectError, OSError, KeyError, ValueError, TypeError, ZoneInfoNotFoundError, urllib.error.URLError) as exc:
        print('COLLECTOR STOP: ' + str(exc), file=sys.stderr)
        sys.exit(1)
