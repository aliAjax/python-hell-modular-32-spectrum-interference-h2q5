import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.repository import Repository
from src.service import Service
from src.domain import ConflictError, DomainError
from src.rules import assess


def make_service():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    repo = Repository(tmp.name)
    repo.initialize()
    return Service(repo), tmp.name


class OfflineIngestTest(unittest.TestCase):
    def setUp(self):
        self.service, self.db_path = make_service()
        self.item = self.service.create_item({
            "frequency_mhz": 2400.0,
            "bandwidth_mhz": 10.0,
            "station_id": "ST-10",
            "region": "west",
            "strength_dbm": -55,
            "detected_at": "2026-09-27T08:00:00+00:00",
            "reporter": "monitor-0",
        }, "analyst-1", "analyst")

    def tearDown(self):
        os.unlink(self.db_path)

    def measurement(self, mid, strength, observed_at, item_id=None, **extra):
        value = {
            "item_id": self.item["id"] if item_id is None else item_id,
            "measurement_id": mid,
            "strength_dbm": strength,
            "observed_at": observed_at,
        }
        value.update(extra)
        return value

    def test_batch_ingest_updates_and_server_recomputes(self):
        result = self.service.ingest_offline_batch({
            "batch_id": "B-1",
            "measurements": [
                self.measurement("M-1", -53, "2026-09-27T09:00:00+00:00",
                                 score=1, level="low", assessment={"level": "low"}),
                self.measurement("M-2", -52, "2026-09-27T09:30:00+00:00"),
            ],
        }, "veh-1", "monitor", "west")
        self.assertEqual(result["stored"], 2)
        item = self.service.get_item(self.item["id"])
        # 最新的 M-2 成为规范值
        self.assertEqual(item["payload"]["strength_dbm"], -52.0)
        # 服务端重算，不采用车里上报的 low
        self.assertEqual(item["payload"]["assessment"], assess(item["payload"]))
        self.assertNotEqual(item["payload"]["assessment"]["level"], "low")
        revisions = item["payload"]["measurement_revisions"]
        self.assertEqual(len(revisions), 2)
        self.assertEqual(revisions[0]["source"], "offline")
        self.assertEqual(revisions[0]["vehicle_id"], "veh-1")
        self.assertEqual(len(item["measurements"]), 2)
        self.assertFalse(any(c.get("status") == "pending" for c in item["payload"].get("conflicts", [])))

    def test_duplicate_batch_stored_once(self):
        batch = {
            "batch_id": "B-2",
            "measurements": [self.measurement("M-1", -53, "2026-09-27T09:00:00+00:00")],
        }
        first = self.service.ingest_offline_batch(batch, "veh-1", "monitor", "west")
        self.assertEqual(first["stored"], 1)
        item = self.service.get_item(self.item["id"])
        version, audits = item["version"], len(item["audit"])
        second = self.service.ingest_offline_batch(batch, "veh-1", "monitor", "west")
        self.assertEqual(second["stored"], 0)
        self.assertEqual(second["duplicates"], 1)
        item = self.service.get_item(self.item["id"])
        self.assertEqual(item["version"], version)
        self.assertEqual(len(item["audit"]), audits)
        self.assertEqual(len(item["measurements"]), 1)

    def test_legacy_without_batch_id_is_merged(self):
        legacy = {"measurements": [self.measurement("M-9", -53, "2026-09-27T09:00:00+00:00")]}
        first = self.service.ingest_offline_batch(legacy, "veh-1", "monitor", "west")
        self.assertIsNone(first["batch_id"])
        self.assertEqual(first["stored"], 1)
        again = self.service.ingest_offline_batch(legacy, "veh-1", "monitor", "west")
        self.assertEqual(again["duplicates"], 1)
        # 旧数据连测量号也没有：按内容哈希去重
        no_id = {"measurements": [self.measurement(None, -54, "2026-09-27T09:05:00+00:00")]}
        del no_id["measurements"][0]["measurement_id"]
        self.assertEqual(self.service.ingest_offline_batch(no_id, "veh-1", "monitor", "west")["stored"], 1)
        self.assertEqual(self.service.ingest_offline_batch(no_id, "veh-1", "monitor", "west")["duplicates"], 1)
        item = self.service.get_item(self.item["id"])
        self.assertEqual(len(item["measurements"]), 2)

    def test_late_measurement_keeps_status_and_flags_conflict(self):
        item = self.service.act(self.item["id"], "assess", {}, "a", "analyst", self.item["version"])
        item = self.service.act(item["id"], "locate", {"location": "cell-1", "confidence": 0.9}, "f", "field_operator", item["version"])
        item = self.service.act(item["id"], "suspend", {"authorization_code": "REG-W-1"}, "c", "coordinator", item["version"], "west")
        late = {
            "batch_id": "B-late",
            "measurements": [self.measurement("M-L1", -56, "2026-09-27T10:00:00+00:00")],
        }
        result = self.service.ingest_offline_batch(late, "veh-1", "monitor", "west")
        self.assertTrue(result["results"][0]["late"])
        self.assertTrue(result["results"][0]["conflict_pending"])
        item = self.service.get_item(self.item["id"])
        # 迟到测量照旧留档，状态不回退，规范值不被改写
        self.assertEqual(item["status"], "suspended")
        self.assertEqual(item["payload"]["strength_dbm"], -55.0)
        self.assertEqual(len(item["measurements"]), 1)
        conflicts = item["payload"]["conflicts"]
        self.assertEqual(conflicts[0]["status"], "pending")
        self.assertEqual(conflicts[0]["reasons"], ["late_measurement"])
        # 有冲突时停用先拒绝
        with self.assertRaises(DomainError) as blocked:
            self.service.act(item["id"], "suspend", {"authorization_code": "REG-W-2"}, "c", "coordinator", item["version"], "west")
        self.assertEqual(blocked.exception.code, "pending_conflict")
        # 核对后流程继续，结案后迟到测量依旧只留档
        item = self.service.act(item["id"], "review_conflicts", {"note": "已核对"}, "c", "coordinator")
        item = self.service.act(item["id"], "coordinate", {"coordination_agreement": "AGC-1"}, "c", "coordinator", item["version"], "west")
        item = self.service.act(item["id"], "resolve", {"measurement_cleared": True, "evidence": "scan-1"}, "c", "coordinator", item["version"], "west")
        self.service.ingest_offline_batch({
            "batch_id": "B-late2",
            "measurements": [self.measurement("M-L2", -57, "2026-09-27T11:00:00+00:00")],
        }, "veh-2", "monitor", "west")
        item = self.service.get_item(self.item["id"])
        self.assertEqual(item["status"], "resolved")
        self.assertEqual(len(item["measurements"]), 2)
        self.assertTrue(any(c.get("status") == "pending" for c in item["payload"]["conflicts"]))

    def test_pending_conflict_blocks_locate_until_reviewed(self):
        item = self.service.act(self.item["id"], "assess", {}, "a", "analyst", self.item["version"])
        result = self.service.ingest_offline_batch({
            "batch_id": "B-3",
            "measurements": [self.measurement("M-1", -80, "2026-09-27T09:00:00+00:00")],
        }, "veh-1", "monitor", "west")
        self.assertTrue(result["results"][0]["conflict_pending"])
        item = self.service.get_item(self.item["id"])
        self.assertEqual(item["payload"]["conflicts"][0]["reasons"], ["measurement_disagreement"])
        with self.assertRaises(DomainError) as blocked:
            self.service.act(item["id"], "locate", {"location": "cell-2", "confidence": 0.95}, "f", "field_operator", item["version"])
        self.assertEqual(blocked.exception.code, "pending_conflict")
        self.assertEqual(blocked.exception.status, 409)
        item = self.service.act(item["id"], "review_conflicts", {"note": "现场复测"}, "a", "analyst")
        self.assertEqual(item["payload"]["conflicts"][0]["status"], "reviewed")
        item = self.service.act(item["id"], "locate", {"location": "cell-2", "confidence": 0.95}, "f", "field_operator", item["version"])
        self.assertEqual(item["status"], "located")

    def test_cross_region_rejected(self):
        with self.assertRaises(DomainError) as mismatch:
            self.service.ingest_offline_batch({
                "batch_id": "B-4",
                "measurements": [self.measurement("M-1", -53, "2026-09-27T09:00:00+00:00")],
            }, "veh-9", "monitor", "north")
        self.assertEqual(mismatch.exception.code, "region_mismatch")
        with self.assertRaises(DomainError) as mismatch:
            self.service.ingest_offline_batch({
                "batch_id": "B-5",
                "measurements": [self.measurement("M-1", -53, "2026-09-27T09:00:00+00:00", region="east")],
            }, "veh-1", "monitor", "west")
        self.assertEqual(mismatch.exception.code, "region_mismatch")
        with self.assertRaises(DomainError) as forbidden:
            self.service.ingest_offline_batch({
                "batch_id": "B-6",
                "measurements": [self.measurement("M-1", -53, "2026-09-27T09:00:00+00:00")],
            }, "f-1", "field_operator", "west")
        self.assertEqual(forbidden.exception.status, 403)
        item = self.service.get_item(self.item["id"])
        self.assertEqual(len(item["measurements"]), 0)

    def test_outcome_does_not_depend_on_arrival_order(self):
        def run(order):
            service, db_path = make_service()
            try:
                item = service.create_item({
                    "frequency_mhz": 2400.0, "bandwidth_mhz": 10.0, "station_id": "ST-10",
                    "region": "west", "strength_dbm": -55,
                    "detected_at": "2026-09-27T08:00:00+00:00", "reporter": "monitor-0",
                }, "analyst-1", "analyst")
                item = service.act(item["id"], "assess", {}, "a", "analyst", item["version"])
                batches = {
                    "A": {"batch_id": "BA", "measurements": [{
                        "item_id": item["id"], "measurement_id": "M-1", "strength_dbm": -52,
                        "observed_at": "2026-09-27T09:00:00+00:00"}]},
                    "B": {"batch_id": "BB", "measurements": [{
                        "item_id": item["id"], "measurement_id": "M-1", "strength_dbm": -80,
                        "observed_at": "2026-09-27T09:30:00+00:00"}]},
                }
                for key in order:
                    vehicle = "veh-A" if key == "A" else "veh-B"
                    service.ingest_offline_batch(batches[key], vehicle, "monitor", "west")
                return service.get_item(item["id"])
            finally:
                os.unlink(db_path)

        first = run(["A", "B"])
        second = run(["B", "A"])
        self.assertEqual(first["payload"]["strength_dbm"], second["payload"]["strength_dbm"])
        self.assertEqual(first["payload"]["strength_dbm"], -80.0)
        self.assertEqual(first["status"], second["status"])
        self.assertEqual(len(first["measurements"]), len(second["measurements"]))
        self.assertEqual(len(first["measurements"]), 2)
        conflict_a = [c for c in first["payload"]["conflicts"] if c["status"] == "pending"]
        conflict_b = [c for c in second["payload"]["conflicts"] if c["status"] == "pending"]
        self.assertEqual(len(conflict_a), 1)
        self.assertEqual(len(conflict_b), 1)
        self.assertEqual(conflict_a[0]["measurement_refs"], conflict_b[0]["measurement_refs"])
        self.assertEqual(conflict_a[0]["values"], conflict_b[0]["values"])
        self.assertEqual(conflict_a[0]["reasons"], conflict_b[0]["reasons"])

    def test_concurrent_uploads_are_deduplicated(self):
        batch = {
            "batch_id": "B-race",
            "measurements": [
                self.measurement("M-%d" % i, -53 - i * 0.5, "2026-09-27T09:0%d:00+00:00" % i)
                for i in range(5)
            ],
        }
        errors = []

        def upload():
            try:
                self.service.ingest_offline_batch(batch, "veh-1", "monitor", "west")
            except Exception as exc:  # noqa: BLE001 - 收集线程内异常统一断言
                errors.append(exc)

        threads = [threading.Thread(target=upload) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        item = self.service.get_item(self.item["id"])
        self.assertEqual(len(item["measurements"]), 5)


if __name__ == "__main__":
    unittest.main()
