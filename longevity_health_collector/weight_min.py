"""Lowest observed weight per configured local calendar day.
Read-only HA recorder GET; does not change Withings or other HA entities.
Recorder history is an observation proxy, NOT a complete Withings weigh-in event log.
"""
import datetime as dt
import json
import math
import os
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

API='http://supervisor/core/api/history/period/'


def select_daily_min(records, local_day, timezone, entity_id):
    """Exclude stale start-of-window states and non-numeric or wrong-unit values."""
    rows=[]
    for r in records:
        try:
            value=float(r.get('state'))
            if not math.isfinite(value) or not 40<=value<=250:continue
            attrs=r.get('attributes') or {}
            if attrs.get('unit_of_measurement') not in ('kg',):continue
            raw=r.get('last_changed')
            if not raw:continue
            changed=dt.datetime.fromisoformat(raw.replace('Z','+00:00'))
            if changed.tzinfo is None:continue
            if changed.astimezone(timezone).date()!=local_day:continue
            rows.append((value,changed,r))
        except (ValueError,TypeError,OverflowError,AttributeError):continue
    if not rows:return None
    chosen=min(rows,key=lambda x:(x[0],x[1]))
    return {'schema':'longevity.withings.daily_min.v1','date_local':local_day.isoformat(),
            'timezone':str(timezone),'metric':'weight_kg','value':chosen[0], 'unit':'kg',
            'source':'withings_via_home_assistant_recorder','entity_id':entity_id,
            'selected_observed_at':chosen[1].isoformat(),
            'number_of_qualifying_state_changes':len(rows),
            'selection':'minimum of qualifying HA Withings weight state changes on this local day',
            'quality':'observed_state_changes_only_not_complete_direct_withings_weigh_in_history',
            'date_basis':'home_assistant_recorder_last_changed_proxy',
            'true_measurement_timestamp':'unavailable_from_source',
            'limitations':'day assigned from HA state-change time (sync time), not the Withings weigh-in time; '
                          'a repeated identical value creates no state change; reconciliation horizon 7 local days '
                          'within recorder retention'}


def query_day(day,zone,token,entity_id,opener=urllib.request.urlopen):
    start=dt.datetime.combine(day,dt.time.min,tzinfo=zone)
    end=dt.datetime.combine(day+dt.timedelta(days=1),dt.time.min,tzinfo=zone)
    url=API+urllib.parse.quote(start.isoformat(),safe='')+'?'+urllib.parse.urlencode({
        'filter_entity_id':entity_id,'end_time':end.isoformat(),
        'skip_initial_state':'','significant_changes_only':'0'})
    req=urllib.request.Request(url,headers={'Authorization':'Bearer '+token,'Accept':'application/json'},method='GET')
    with opener(req,timeout=25) as resp: data=json.load(resp)
    if data == []: return None
    if not isinstance(data,list) or not isinstance(data[0],list):
        raise ValueError('Unexpected recorder-history response')
    return select_daily_min(data[0],day,zone,entity_id)

def reconcile_min(candidate, path):
    """Revision audit is append-only; canonical file is replaced atomically.
    Recorder observations remain a proxy, not actual weigh-in event timestamps.
    """
    if candidate is None:
        return 'missing'
    if path.name != 'daily_weight_min_'+candidate['date_local']+'.json':
        raise ValueError('Date/path mismatch')
    if path.is_symlink() or path.parent.is_symlink() or not path.parent.is_dir():
        raise ValueError('Invalid destination')
    previous = None
    if path.exists():
        previous = json.loads(path.read_text(encoding='utf8'))
        for key in ('schema','date_local','timezone','entity_id','unit','source'):
            if previous.get(key) != candidate.get(key):
                raise ValueError('Existing daily record identity mismatch')
        if not math.isfinite(float(previous['value'])):
            raise ValueError('Invalid existing value')
        if candidate['value'] >= previous['value']:
            return 'unchanged'
    def atomic_write(target, doc):
        fd, temp = tempfile.mkstemp(prefix='.weight_',suffix='.tmp',dir=str(path.parent))
        try:
            with os.fdopen(fd,'w',encoding='utf8') as f:
                json.dump(doc,f,sort_keys=True,indent=2)
                f.write('\n')
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp,target)
        finally:
            if os.path.exists(temp): os.unlink(temp)
    new = dict(candidate)
    new['processed_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
    if previous is not None:
        import hashlib
        digest = hashlib.sha256(json.dumps(previous,sort_keys=True).encode()).hexdigest()
        audit = path.with_name(path.stem+'_revision_'+digest+'.json')
        if audit.is_symlink(): raise ValueError('Invalid audit path')
        if audit.exists():
            if json.loads(audit.read_text(encoding='utf8')) != previous:
                raise ValueError('Existing audit mismatch')
        else:
            atomic_write(audit,previous)
    atomic_write(path,new)
    return 'revised' if previous is not None else 'created'
