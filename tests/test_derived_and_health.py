"""Synthetic tests: derived weight analytics, private runtime config, HA publishing and health/monitoring signals."""
import datetime as dt
import io
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock
APPS = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APPS / "longevity_health_collector"))
import collect
import summary

ENT = "sensor.synthetic_weight"
UTC = dt.timezone.utc


def rec(day, value, entity=ENT, **over):
    d = {"schema": summary.SCHEMA, "date_local": day, "unit": "kg", "entity_id": entity, "value": value,
         "selected_observed_at": day + "T07:00:00+01:00", "date_basis": "home_assistant_recorder_last_changed_proxy"}
    d.update(over); return d


class Derived(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory(); self.addCleanup(t.cleanup); self.d = pathlib.Path(t.name)

    def put(self, name, doc):
        (self.d / name).write_text(json.dumps(doc) if not isinstance(doc, str) else doc)

    def test_canonical_only_and_rejections(self):
        self.put("daily_weight_min_2026-11-10.json", rec("2026-11-10", 100.0))
        self.put("daily_weight_min_2026-11-10_revision_" + "a" * 64 + ".json", rec("2026-11-10", 101.0))
        self.put(".weight_x.tmp", "{}")
        self.put("daily_weight_min_2026-11-11.json", "{broken")
        self.put("daily_weight_min_2026-11-12.json", rec("2026-11-12", 99.0, entity="sensor.other"))
        self.put("daily_weight_min_2026-11-13.json", rec("2026-11-14", 99.0))     # date mismatch
        self.put("daily_weight_min_2026-11-15.json", rec("2026-11-15", 99.0, unit="lb"))
        self.put("daily_weight_min_2026-11-16.json", rec("2026-11-16", float("nan")))
        (self.d / "daily_weight_min_2026-11-17.json").symlink_to(self.d / "daily_weight_min_2026-11-10.json")
        days, rejected = summary.load_canonical(self.d, ENT)
        self.assertEqual(list(days), [dt.date(2026, 11, 10)]); self.assertEqual(days[dt.date(2026, 11, 10)]["value"], 100.0)
        self.assertEqual(rejected, 5)

    def test_window_mean_count_and_no_zero_or_carry_forward(self):
        days = {dt.date(2026, 11, d): {"value": v} for d, v in ((8, 104.0), (10, 100.0), (12, 98.0), (15, 90.0))}
        s = summary.summarise(days, dt.date(2026, 11, 15))      # today=15 -> window 08..14; day 15 not completed
        self.assertEqual((s["window_start"], s["window_end"]), ("2026-11-08", "2026-11-14"))
        self.assertEqual(s["days_with_data"], 3); self.assertEqual(s["mean"], round((104 + 100 + 98) / 3, 2))
        self.assertEqual((s["latest_day"], s["latest_value"], s["latest_age_days"]), ("2026-11-12", 98.0, 2))
        self.assertEqual([r["date"] for r in s["recent_days"]], ["2026-11-12", "2026-11-10", "2026-11-08"])

    def test_empty_is_unknown_not_zero(self):
        s = summary.summarise({}, dt.date(2026, 11, 15))
        self.assertIsNone(s["mean"]); self.assertIsNone(s["latest_value"]); self.assertEqual(s["days_with_data"], 0)

    def test_window_excludes_days_older_than_seven(self):
        s = summary.summarise({dt.date(2026, 11, 1): {"value": 100.0}}, dt.date(2026, 11, 15))
        self.assertIsNone(s["mean"]); self.assertEqual(s["latest_age_days"], 13)


class Config(unittest.TestCase):
    def test_generic_defaults_are_safe(self):
        c = collect.load_config({})
        self.assertTrue(c["dry_run"]); self.assertFalse(c["collection_approved"]); self.assertTrue(c["run_once"])
        self.assertEqual(c["timezone"], "UTC"); self.assertEqual(c["weight_entity"], ""); self.assertEqual(c["metrics"], {})
        self.assertFalse(c["publish_entities"])

    def test_metric_mapping_parsed(self):
        c = collect.load_config({"metrics": ["resting_hr|sensor.example_resting_hr|vendor_a|cardiovascular"]})
        self.assertEqual(c["metrics"]["resting_hr"], ("sensor.example_resting_hr", "vendor_a", "cardiovascular"))

    def test_invalid_options_fail_closed(self):
        for bad in ({"metrics": ["bad"]}, {"metrics": ["a|light.x|s|c"]},
                    {"metrics": ["a|sensor.x|s|c", "a|sensor.y|s|c"]}, {"weight_entity": "light.kitchen"},
                    {"weight_entity": "sensor.x; drop"}, {"timezone": "Mars/Base"}, {"heartbeat_minutes": 1},
                    {"collect_hour": 24}, {"bridge_interval_hours": 0}):
            with self.assertRaises(collect.CollectError, msg=str(bad)): collect.load_config(bad)


class Recorder:
    def __init__(self): self.calls = []
    def __call__(self, req, timeout):
        self.calls.append((req.get_method(), req.full_url, json.loads(req.data) if req.data else None))
        r = io.BytesIO(b"{}"); r.status = 200; r.__enter__ = lambda s=r: s; r.__exit__ = lambda *a: False
        return type("R", (), {"status": 200, "__enter__": lambda s: s, "__exit__": lambda s, *a: False})()


class Publish(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory(); self.addCleanup(t.cleanup); base = pathlib.Path(t.name)
        self.dest = base / "03_Current_Data"; self.dest.mkdir(); self.status = base / ".runtime" / "bridge_status.json"
        for p in (mock.patch.object(collect, "DEST", self.dest), mock.patch.object(collect, "BRIDGE_STATUS", self.status)):
            p.start(); self.addCleanup(p.stop)
        (self.dest / "daily_weight_min_2026-11-14.json").write_text(json.dumps(rec("2026-11-14", 99.5)))
        self.cfg = collect.load_config({"weight_entity": ENT, "publish_entities": True, "timezone": "Europe/Berlin"})
        self.now = dt.datetime(2026, 11, 15, 20, 0, tzinfo=self.cfg["zone"])

    def test_refuses_any_other_entity(self):
        with self.assertRaises(collect.CollectError): collect.publish("t", ENT, "1", {}, opener=Recorder())
        with self.assertRaises(collect.CollectError): collect.publish("t", "sensor.source_weight", "1", {}, opener=Recorder())

    def test_heartbeat_publishes_exactly_four_longevity_entities_locally(self):
        r = Recorder()
        collect.heartbeat(self.cfg, "tok", {"mount": "pass", "state": "ok", "attrs": {}}, self.now, opener=r)
        self.assertEqual([c[1].rsplit("/", 1)[1] for c in r.calls], list(collect.PUBLISHED))
        self.assertTrue(all(c[0] == "POST" and c[1].startswith("http://supervisor/core/api/states/sensor.longevity_") for c in r.calls))
        daily = r.calls[0][2]
        self.assertEqual(daily["state"], 99.5); self.assertEqual(daily["attributes"]["observation_day"], "2026-11-14")
        self.assertIn("proxy", daily["attributes"]["date_basis"]); self.assertNotIn("state_class", daily["attributes"])
        self.assertEqual(r.calls[1][2]["attributes"]["days_with_data"], 1)
        self.assertEqual(r.calls[3][2]["state"], "unknown")       # no bridge status yet: unknown, never healthy

    def test_mount_failure_publishes_health_only_and_never_reads_data(self):
        r = Recorder()
        with mock.patch.object(collect.summary, "load_canonical", side_effect=AssertionError("must not read")):
            collect.heartbeat(self.cfg, "tok", {"mount": "fail", "state": "degraded", "attrs": {"problem": "storage_unavailable"}}, self.now, opener=r)
        self.assertEqual([c[1].rsplit("/", 1)[1] for c in r.calls], list(collect.PUBLISHED[2:]))
        self.assertEqual(r.calls[0][2]["state"], "degraded"); self.assertEqual(r.calls[1][2]["state"], "unknown")

    def test_bridge_health_classification(self):
        now = dt.datetime(2026, 11, 15, 20, 0, tzinfo=UTC)
        def put(**st):
            self.status.parent.mkdir(exist_ok=True); self.status.write_text(json.dumps(st))
            return collect.bridge_health(now, 24, self.status)[0]
        self.assertEqual(collect.bridge_health(now, 24, self.status)[0], "unknown")
        self.assertEqual(put(last_attempt="2026-11-15T05:00:00+00:00", last_success="2026-11-15T05:10:00+00:00", last_result="archive_pass"), "ok")
        self.assertEqual(put(last_attempt="2026-11-14T10:00:00+00:00", last_success="2026-11-14T10:10:00+00:00", last_result="archive_pass"), "stale")
        self.assertEqual(put(last_attempt="2026-11-15T19:00:00+00:00", last_success="2026-11-15T05:10:00+00:00", last_result="archive_stop"), "failed")
        self.assertEqual(put(last_attempt="2026-11-15T19:50:00+00:00", last_success="2026-11-14T05:10:00+00:00", last_result="running"), "ok")
        self.assertEqual(put(last_attempt="2026-11-15T19:00:00+00:00", last_result="restore_pass"), "ok")
        self.assertEqual(put(last_attempt="garbage"), "unknown")


class Loop(unittest.TestCase):
    """Main loop with a synthetic clock: one daily cycle per day at the configured time; heartbeat cadence;
    storage loss reported as degraded without collection; recovery returns to ok."""
    def test_schedule_heartbeat_and_storage_loss(self):
        with tempfile.TemporaryDirectory() as t:
            base = pathlib.Path(t); opts = base / "options.json"
            opts.write_text(json.dumps({"dry_run": False, "collection_approved": True, "run_once": False, "publish_entities": True,
                                        "timezone": "UTC", "weight_entity": ENT, "collect_hour": 23, "collect_minute": 45}))
            times = [dt.datetime(2026, 11, 15, 23, 30, tzinfo=dt.timezone.utc) + dt.timedelta(minutes=m) for m in range(0, 50, 1)]
            mount = {"ok": True}
            def check():
                if not mount["ok"]: raise collect.mount_guard.MountGuardError("not a mount point")
                return {"fstype": "ntfs3", "directories": 5}
            cycles, beats = [], []
            it = iter(times)
            def clock(zone):
                try: return next(it)
                except StopIteration: raise KeyboardInterrupt
            def sleep(_):
                if times.index(current[0]) == 5: mount["ok"] = False
                if times.index(current[0]) == 25: mount["ok"] = True
            current = [None]
            def clock2(zone):
                current[0] = clock(zone); return current[0]
            with mock.patch.object(collect, "OPTIONS", opts), mock.patch.object(collect, "STATE", base / "state.json"), \
                 mock.patch.object(collect.mount_guard, "check", side_effect=check), \
                 mock.patch.object(collect, "daily_cycle", side_effect=lambda c, tok, now: cycles.append(now)), \
                 mock.patch.object(collect, "heartbeat", side_effect=lambda c, tok, h, now: beats.append((now, h["state"], h["mount"]))), \
                 mock.patch.dict("os.environ", {"SUPERVISOR_TOKEN": "synthetic"}):
                with self.assertRaises(KeyboardInterrupt): collect.main(clock=clock2, sleep=sleep)
            self.assertEqual(len(cycles), 1); self.assertEqual(cycles[0].strftime("%H:%M"), "23:56")   # 23:45 due but storage lost until 23:55; first cycle after recovery
            self.assertEqual([b[0].strftime("%H:%M") for b in beats], ["23:30", "23:45", "00:00", "00:15"])
            self.assertEqual([b[1] for b in beats], ["ok", "degraded", "ok", "ok"])
            self.assertEqual(json.loads((base / "state.json").read_text())["last_cycle_day"], "2026-11-15")


if __name__ == "__main__": unittest.main()
