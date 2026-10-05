import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.repository import Repository
from src.service import Service
from src.domain import ConflictError, DomainError
from src.rules import assess


def make_measurement(station_id="ST-01", region="north", frequency=2400.0,
                     bandwidth=20.0, strength=-40, detected_at="2026-10-01T10:00:00+00:00",
                     vehicle_id="vehicle-1", offline_assessment=None):
    m = {
        "station_id": station_id,
        "region": region,
        "frequency_mhz": frequency,
        "bandwidth_mhz": bandwidth,
        "strength_dbm": strength,
        "detected_at": detected_at,
    }
    if vehicle_id is not None:
        m["vehicle_id"] = vehicle_id
    if offline_assessment is not None:
        m["offline_assessment"] = offline_assessment
    return m


class OfflineBatchUploadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_batch_upload_creates_items_with_server_assessment(self):
        """批量回传创建业务实体，评估由服务端重算。"""
        payload = {
            "batch_id": "batch-001",
            "vehicle_id": "vehicle-1",
            "measurements": [
                make_measurement(strength=-35, detected_at="2026-10-01T10:00:00+00:00"),
                make_measurement(station_id="ST-02", strength=-80, detected_at="2026-10-01T11:00:00+00:00"),
            ],
        }
        result = self.service.offline_batch_upload(payload, "m", "monitor", "north")
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["summary"]["merged"], 2)
        self.assertEqual(result["summary"]["duplicate"], 0)

        # 验证业务实体已创建
        items = self.service.list_items()
        self.assertEqual(len(items), 2)

        # 验证评估由服务端计算（不采用车里结果）
        item1 = self.service.get_item(result["results"][0]["item_id"])
        expected = assess({"strength_dbm": -35, "bandwidth_mhz": 20.0})
        self.assertEqual(item1["payload"]["assessment"]["level"], expected["level"])
        self.assertEqual(item1["payload"]["assessment"]["score"], expected["score"])

    def test_server_recalculates_ignoring_offline_result(self):
        """测量更新后服务端重算，不采用车里结果。"""
        # 车里离线评估故意报错（critical），服务端应重算为 low
        wrong_assessment = {"score": 99, "level": "critical", "impact_value": -30}
        payload = {
            "batch_id": "batch-002",
            "measurements": [
                make_measurement(strength=-90, bandwidth=0.1, offline_assessment=wrong_assessment),
            ],
        }
        result = self.service.offline_batch_upload(payload, "m", "monitor", "north")
        item = self.service.get_item(result["results"][0]["item_id"])
        # 服务端重算应为 low，而非车里的 critical
        self.assertEqual(item["payload"]["assessment"]["level"], "low")
        self.assertNotEqual(item["payload"]["assessment"]["level"], "critical")
        # 车里结果应留档但不被采用
        stored = self.repo.list_offline_measurements(batch_id=result["batch_id"])
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["offline_assessment"]["level"], "critical")

    def test_duplicate_batch_only_stored_once(self):
        """重复上传批次只入库一次。"""
        payload = {
            "batch_id": "batch-dup",
            "measurements": [make_measurement()],
        }
        self.service.offline_batch_upload(payload, "m", "monitor", "north")
        with self.assertRaises(ConflictError) as ctx:
            self.service.offline_batch_upload(payload, "m", "monitor", "north")
        self.assertEqual(ctx.exception.code, "duplicate_batch")

    def test_duplicate_measurement_content_only_stored_once(self):
        """相同测量内容只入库一次（跨批次去重）。"""
        m = make_measurement()
        payload1 = {"batch_id": "batch-a", "measurements": [m]}
        payload2 = {"batch_id": "batch-b", "measurements": [m]}
        result1 = self.service.offline_batch_upload(payload1, "m", "monitor", "north")
        result2 = self.service.offline_batch_upload(payload2, "m", "monitor", "north")
        self.assertEqual(result1["summary"]["merged"], 1)
        self.assertEqual(result2["summary"]["duplicate"], 1)
        self.assertEqual(result2["summary"]["merged"], 0)

    def test_legacy_data_without_batch_id_merged(self):
        """旧数据无批次号兼容并入。"""
        payload = {
            "measurements": [make_measurement()],
        }
        result = self.service.offline_batch_upload(payload, "m", "monitor", "north")
        self.assertIsNone(result["batch_uuid"])
        self.assertEqual(result["summary"]["merged"], 1)

    def test_older_measurement_discarded(self):
        """测量时间更早的记录当旧版本丢掉。"""
        # 先创建一个较新的测量
        payload1 = {
            "batch_id": "batch-new",
            "measurements": [make_measurement(strength=-40, detected_at="2026-10-02T10:00:00+00:00")],
        }
        result1 = self.service.offline_batch_upload(payload1, "m", "monitor", "north")
        item_id = result1["results"][0]["item_id"]

        # 再回传一个较旧的测量
        payload2 = {
            "batch_id": "batch-old",
            "measurements": [make_measurement(strength=-50, detected_at="2026-10-01T10:00:00+00:00")],
        }
        result2 = self.service.offline_batch_upload(payload2, "m", "monitor", "north")
        self.assertEqual(result2["summary"]["discarded"], 1)

        # 业务实体的测量值不应改变
        item = self.service.get_item(item_id)
        self.assertEqual(item["payload"]["strength_dbm"], -40)

    def test_newer_measurement_merged(self):
        """测量时间更新的记录合并到业务实体。"""
        payload1 = {
            "batch_id": "batch-old",
            "measurements": [make_measurement(strength=-40, detected_at="2026-10-01T10:00:00+00:00")],
        }
        result1 = self.service.offline_batch_upload(payload1, "m", "monitor", "north")
        item_id = result1["results"][0]["item_id"]

        payload2 = {
            "batch_id": "batch-new",
            "measurements": [make_measurement(strength=-65, detected_at="2026-10-02T10:00:00+00:00")],
        }
        result2 = self.service.offline_batch_upload(payload2, "m", "monitor", "north")
        self.assertEqual(result2["summary"]["merged"], 1)

        item = self.service.get_item(item_id)
        self.assertEqual(item["payload"]["strength_dbm"], -65)
        self.assertEqual(item["payload"]["detected_at"], "2026-10-02T10:00:00+00:00")

    def test_conflicting_measurement_sets_pending_conflict(self):
        """同一时间测量值不一致，挂待核对冲突。"""
        payload1 = {
            "batch_id": "batch-1",
            "measurements": [make_measurement(strength=-40, detected_at="2026-10-01T10:00:00+00:00")],
        }
        result1 = self.service.offline_batch_upload(payload1, "m", "monitor", "north")
        item_id = result1["results"][0]["item_id"]

        # 同一时间，不同强度
        payload2 = {
            "batch_id": "batch-2",
            "measurements": [make_measurement(strength=-50, detected_at="2026-10-01T10:00:00+00:00")],
        }
        result2 = self.service.offline_batch_upload(payload2, "m", "monitor", "north")
        self.assertEqual(result2["summary"]["conflict"], 1)

        item = self.service.get_item(item_id)
        self.assertTrue(item["payload"]["pending_conflict"])

    def test_pending_conflict_rejects_locate(self):
        """有冲突时定位先拒绝。"""
        payload1 = {
            "batch_id": "batch-1",
            "measurements": [make_measurement(strength=-40, detected_at="2026-10-01T10:00:00+00:00")],
        }
        result1 = self.service.offline_batch_upload(payload1, "m", "monitor", "north")
        item_id = result1["results"][0]["item_id"]

        payload2 = {
            "batch_id": "batch-2",
            "measurements": [make_measurement(strength=-50, detected_at="2026-10-01T10:00:00+00:00")],
        }
        self.service.offline_batch_upload(payload2, "m", "monitor", "north")

        # 评估后尝试定位，应被拒绝
        item = self.service.act(item_id, "assess", {}, "a", "analyst")
        with self.assertRaises(DomainError) as ctx:
            self.service.act(item_id, "locate", {"location": "x", "confidence": 0.8}, "f", "field_operator", item["version"])
        self.assertEqual(ctx.exception.code, "pending_conflict")

    def test_pending_conflict_rejects_suspend(self):
        """有冲突时停用先拒绝。"""
        payload1 = {
            "batch_id": "batch-1",
            "measurements": [make_measurement(strength=-40, detected_at="2026-10-01T10:00:00+00:00")],
        }
        result1 = self.service.offline_batch_upload(payload1, "m", "monitor", "north")
        item_id = result1["results"][0]["item_id"]

        # 先完成评估和定位（此时无冲突）
        item = self.service.act(item_id, "assess", {}, "a", "analyst")
        item = self.service.act(item_id, "locate", {"location": "x", "confidence": 0.8}, "f", "field_operator", item["version"])

        # 回传冲突测量，挂起待核对
        payload2 = {
            "batch_id": "batch-2",
            "measurements": [make_measurement(strength=-50, detected_at="2026-10-01T10:00:00+00:00")],
        }
        self.service.offline_batch_upload(payload2, "m", "monitor", "north")

        # 有冲突时停用先拒绝
        with self.assertRaises(DomainError) as ctx:
            self.service.act(item_id, "suspend", {"authorization_code": "REG-X"}, "c", "coordinator", item["version"], "north")
        self.assertEqual(ctx.exception.code, "pending_conflict")

    def test_clear_conflict_allows_locate(self):
        """核对清除冲突后可正常定位。"""
        payload1 = {
            "batch_id": "batch-1",
            "measurements": [make_measurement(strength=-40, detected_at="2026-10-01T10:00:00+00:00")],
        }
        result1 = self.service.offline_batch_upload(payload1, "m", "monitor", "north")
        item_id = result1["results"][0]["item_id"]

        payload2 = {
            "batch_id": "batch-2",
            "measurements": [make_measurement(strength=-50, detected_at="2026-10-01T10:00:00+00:00")],
        }
        self.service.offline_batch_upload(payload2, "m", "monitor", "north")

        # 清除冲突
        item = self.service.act(item_id, "clear_conflict", {}, "a", "analyst")
        self.assertFalse(item["payload"].get("pending_conflict"))

        # 评估后可正常定位
        item = self.service.act(item_id, "assess", {}, "a", "analyst")
        item = self.service.act(item_id, "locate", {"location": "x", "confidence": 0.8}, "f", "field_operator", item["version"])
        self.assertEqual(item["status"], "located")

    def test_late_measurement_on_resolved_item_archived(self):
        """结案停用后迟到测量照旧留档，状态不回退，改挂待核对冲突。"""
        # 创建并结案
        item = self.service.create_item({
            "frequency_mhz": 2400.0,
            "bandwidth_mhz": 20.0,
            "station_id": "ST-01",
            "region": "north",
            "strength_dbm": -35,
            "detected_at": "2026-10-01T10:00:00+00:00",
            "reporter": "monitor-1",
        }, "a", "analyst")
        item = self.service.act(item["id"], "assess", {}, "a", "analyst", item["version"])
        item = self.service.act(item["id"], "locate", {"location": "x", "confidence": 0.9}, "f", "field_operator", item["version"])
        item = self.service.act(item["id"], "suspend", {"authorization_code": "REG-X"}, "c", "coordinator", item["version"], "north")
        item = self.service.act(item["id"], "coordinate", {"coordination_agreement": "AGC-1"}, "c", "coordinator", item["version"], "north")
        item = self.service.act(item["id"], "resolve", {"measurement_cleared": True, "evidence": "scan-1"}, "c", "coordinator", item["version"], "north")
        self.assertEqual(item["status"], "resolved")

        # 迟到测量回传
        payload = {
            "batch_id": "batch-late",
            "measurements": [make_measurement(strength=-70, detected_at="2026-10-02T10:00:00+00:00")],
        }
        result = self.service.offline_batch_upload(payload, "m", "monitor", "north")
        self.assertEqual(result["summary"]["archived"], 1)

        # 状态不回退，仍为 resolved
        item = self.service.get_item(item["id"])
        self.assertEqual(item["status"], "resolved")
        # 改挂待核对冲突
        self.assertTrue(item["payload"]["pending_conflict"])
        # 测量值不被采用
        self.assertEqual(item["payload"]["strength_dbm"], -35)

    def test_region_boundary_monitor_rejected(self):
        """监测车只交本区测量，跨区拒绝。"""
        payload = {
            "batch_id": "batch-cross",
            "measurements": [make_measurement(region="south")],
        }
        with self.assertRaises(DomainError) as ctx:
            self.service.offline_batch_upload(payload, "m", "monitor", "north")
        self.assertEqual(ctx.exception.code, "region_mismatch")

    def test_regulator_cross_region_allowed(self):
        """监管角色可跨区。"""
        payload = {
            "batch_id": "batch-cross-reg",
            "measurements": [make_measurement(region="south")],
        }
        result = self.service.offline_batch_upload(payload, "r", "regulator", "north")
        self.assertEqual(result["summary"]["merged"], 1)

    def test_invalid_role_rejected(self):
        """非授权角色拒绝回传。"""
        payload = {
            "batch_id": "batch-role",
            "measurements": [make_measurement()],
        }
        with self.assertRaises(DomainError) as ctx:
            self.service.offline_batch_upload(payload, "f", "field_operator", "north")
        self.assertEqual(ctx.exception.code, "forbidden")

    def test_empty_measurements_rejected(self):
        """空测量数组拒绝。"""
        with self.assertRaises(DomainError) as ctx:
            self.service.offline_batch_upload({"batch_id": "batch-empty", "measurements": []}, "m", "monitor", "north")
        self.assertEqual(ctx.exception.code, "invalid_payload")

    def test_concurrent_duplicate_deterministic(self):
        """两车同时提交相同测量，去重结果不随先后改变。"""
        m = make_measurement()
        payload_a = {"batch_id": "batch-A", "measurements": [m]}
        payload_b = {"batch_id": "batch-B", "measurements": [m]}

        # 顺序1：A 先 B 后
        r1a = self.service.offline_batch_upload(payload_a, "m", "monitor", "north")
        r1b = self.service.offline_batch_upload(payload_b, "m", "monitor", "north")
        self.assertEqual(r1a["summary"]["merged"], 1)
        self.assertEqual(r1b["summary"]["duplicate"], 1)

        # 顺序2：B 先 A 后（新 setUp 重置数据库）
        self.tearDown()
        self.setUp()
        r2b = self.service.offline_batch_upload(payload_b, "m", "monitor", "north")
        r2a = self.service.offline_batch_upload(payload_a, "m", "monitor", "north")
        self.assertEqual(r2b["summary"]["merged"], 1)
        self.assertEqual(r2a["summary"]["duplicate"], 1)

        # 两种顺序结果一致
        self.assertEqual(r1a["summary"]["merged"], r2b["summary"]["merged"])
        self.assertEqual(r1b["summary"]["duplicate"], r2a["summary"]["duplicate"])

    def test_concurrent_conflict_deterministic(self):
        """两车同时提交冲突测量，冲突判定不随先后改变。"""
        m1 = make_measurement(strength=-40, detected_at="2026-10-01T10:00:00+00:00")
        m2 = make_measurement(strength=-50, detected_at="2026-10-01T10:00:00+00:00")
        payload_a = {"batch_id": "batch-A", "measurements": [m1]}
        payload_b = {"batch_id": "batch-B", "measurements": [m2]}

        # 顺序1：A 先 B 后
        r1a = self.service.offline_batch_upload(payload_a, "m", "monitor", "north")
        r1b = self.service.offline_batch_upload(payload_b, "m", "monitor", "north")
        self.assertEqual(r1a["summary"]["merged"], 1)
        self.assertEqual(r1b["summary"]["conflict"], 1)
        item1 = self.service.get_item(r1a["results"][0]["item_id"])
        self.assertTrue(item1["payload"]["pending_conflict"])

        # 顺序2：B 先 A 后（重置）
        self.tearDown()
        self.setUp()
        r2b = self.service.offline_batch_upload(payload_b, "m", "monitor", "north")
        r2a = self.service.offline_batch_upload(payload_a, "m", "monitor", "north")
        self.assertEqual(r2b["summary"]["merged"], 1)
        self.assertEqual(r2a["summary"]["conflict"], 1)
        item2 = self.service.get_item(r2b["results"][0]["item_id"])
        self.assertTrue(item2["payload"]["pending_conflict"])

        # 两种顺序冲突判定一致
        self.assertEqual(r1b["summary"]["conflict"], r2a["summary"]["conflict"])


if __name__ == "__main__":
    unittest.main()
