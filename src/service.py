import hashlib

from . import domain, rules
from .audit import canonical_json
from .domain import DomainError


class Service:
    def __init__(self, repository):
        self.repository = repository

    def create_item(self, payload, actor, role, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.CREATE_ROLES:
            raise DomainError("forbidden", "当前角色不能创建此类业务记录", 403)
        normalized = domain.normalize_create(payload)
        stable_key = normalized.pop("_stable_key")
        return self.repository.create_item(
            rules.ENTITY_TYPE, stable_key, rules.INITIAL_STATUS, normalized, actor, role
        )

    def add_source(self, item_id, payload, actor, role, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.SOURCE_ROLES:
            raise DomainError("forbidden", "当前角色不能提交来源记录", 403)
        item = self.repository.get_item(item_id)
        normalized = domain.normalize_source(payload)
        if region and rules.ENFORCE_REGION and role != "regulator" and normalized.get("region") and normalized["region"] != region:
            raise DomainError("region_mismatch", "来源记录不属于当前管辖区域", 403)
        result = self.repository.add_source(
            item_id,
            normalized.pop("source_type"),
            normalized.pop("external_id"),
            normalized,
            normalized.pop("observed_at"),
            actor,
            role,
        )
        return result

    def act(self, item_id, action, payload, actor, role, expected_version=None, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        item = self.repository.get_item(item_id)
        allowed = rules.ACTION_ROLES.get(action, set())
        if role not in allowed:
            raise DomainError("forbidden", "当前角色不能执行该操作", 403)
        if rules.ENFORCE_REGION and action in rules.REGION_SENSITIVE_ACTIONS and region and role != "regulator":
            if item["payload"].get("region") != region:
                raise DomainError("region_mismatch", "不能处理其他区域的记录", 403)
        if action in rules.ACTION_REQUIRES_VERSION and expected_version is None:
            raise DomainError("expected_version_required", "该操作需要 expected_version", 400)
        new_status, new_payload, event_payload = rules.apply_action(item, action, payload, actor, role)
        self.repository.apply_action(
            item_id, action, actor, role, new_status, new_payload, event_payload, expected_version
        )
        return self.get_item(item_id)

    def get_item(self, item_id):
        item = self.repository.get_item(item_id)
        item["sources"] = self.repository.list_sources(item_id)
        item["measurements"] = self.repository.list_measurements(item_id)
        item["audit"] = self.repository.audit_trail(item_id)
        item["assessment"] = rules.assess(item["payload"])
        return item

    def _offline_dedup_key(self, vehicle_id, batch_id, normalized):
        measurement_id = normalized.get("measurement_id")
        if measurement_id and batch_id:
            return "batch|%s|%s|%s" % (vehicle_id, batch_id, measurement_id)
        if measurement_id:
            return "legacy|%s|%s" % (vehicle_id, measurement_id)
        # 旧数据无批次号也无测量号：按内容哈希兼容并入
        digest = hashlib.sha256(canonical_json({
            "item_id": normalized["item_id"],
            "observed_at": normalized["observed_at"],
            "strength_dbm": normalized["strength_dbm"],
            "station_id": normalized.get("station_id"),
            "frequency_mhz": normalized.get("frequency_mhz"),
        }).encode("utf-8")).hexdigest()
        return "legacy-content|%s|%s" % (vehicle_id, digest)

    def ingest_offline_batch(self, payload, actor, role, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.OFFLINE_BATCH_ROLES:
            raise DomainError("forbidden", "当前角色不能回传离线测量", 403)
        batch_id, pairs = domain.normalize_offline_batch(payload)
        entries = []
        items = {}
        for normalized, raw in pairs:
            item_id = normalized["item_id"]
            if item_id not in items:
                items[item_id] = self.repository.get_item(item_id)
            item = items[item_id]
            if region and rules.ENFORCE_REGION and role != "regulator":
                if normalized.get("region") and normalized["region"] != region:
                    raise DomainError("region_mismatch", "测量记录不属于当前管辖区域", 403)
                if item["payload"].get("region") != region:
                    raise DomainError("region_mismatch", "不能回传其他区域事件的测量", 403)
            entries.append({
                "normalized": normalized,
                "raw": raw,
                "vehicle_id": actor,
                "batch_id": batch_id,
                "dedup_key": self._offline_dedup_key(actor, batch_id, normalized),
            })
        results = self.repository.ingest_offline_batch(
            actor, batch_id, entries, actor, role, rules.merge_offline_measurement
        )
        stored = sum(1 for r in results if r["status"] == "stored")
        return {
            "batch_id": batch_id,
            "vehicle_id": actor,
            "stored": stored,
            "duplicates": len(results) - stored,
            "results": results,
        }

    def list_items(self, status=None):
        return self.repository.list_items(status)

    def state(self):
        return self.repository.state_summary()
