import copy
import math
from datetime import datetime, timezone

from .domain import DomainError

ENTITY_TYPE = "spectrum_interference"
INITIAL_STATUS = "pending"
CREATE_ROLES = {"analyst", "monitor"}
SOURCE_ROLES = {"analyst", "monitor", "field_operator"}
OFFLINE_BATCH_ROLES = {"monitor", "analyst"}
OFFLINE_ACTIVE_STATUSES = {"pending", "assessed", "located"}
CONFLICT_TOLERANCE_DB = 6.0
ACTION_ROLES = {
    "assess": {"analyst", "monitor"},
    "locate": {"field_operator", "analyst"},
    "suspend": {"coordinator", "regulator"},
    "coordinate": {"coordinator"},
    "resolve": {"coordinator", "regulator"},
    "correct_measurement": {"analyst", "monitor"},
    "cancel": {"coordinator"},
    "review_conflicts": {"analyst", "coordinator"},
}
ENFORCE_REGION = True
REGION_SENSITIVE_ACTIONS = {"suspend", "coordinate", "resolve", "cancel"}
ACTION_REQUIRES_VERSION = {"suspend", "coordinate", "resolve", "cancel"}


def assess(payload):
    strength = float(payload.get("strength_dbm", -120))
    bandwidth = max(float(payload.get("bandwidth_mhz", 0.1)), 0.001)
    impact = strength + 10.0 * math.log10(bandwidth * 1000.0)
    if impact >= -37:
        level = "critical"
    elif impact >= -50:
        level = "high"
    elif impact >= -65:
        level = "medium"
    else:
        level = "low"
    score = round(max(0.0, min(100.0, 100.0 + impact)), 2)
    return {"score": score, "level": level, "impact_value": round(impact, 2)}


def _need_status(item, allowed):
    if item["status"] not in allowed:
        raise DomainError("invalid_state", "当前状态 %s 不允许执行该操作" % item["status"])


def _need_no_pending_conflict(item):
    conflicts = item["payload"].get("conflicts") or []
    if any(c.get("status") == "pending" for c in conflicts):
        raise DomainError("pending_conflict", "存在待核对冲突，定位/停用前请先核对", 409)


def _parse_observed(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _measurement_key(observed_at, vehicle_id, measurement_id):
    return (_parse_observed(observed_at), str(vehicle_id or ""), str(measurement_id or ""))


def merge_offline_measurement(item, entry, archived, actor):
    """合并一条离线测量，返回 (item是否变更, 新payload, 结果信息)。

    判定与回传先后无关：规范值取 observed_at 最新的测量（并列按车辆/测量号
    字典序）；冲突按全集（已归档测量 + 事件原始强度）强度极差判定，涉及引用
    按全集重算，置位后保持到人工核对；结案/停用等状态只归档留档，不改规范
    值、不回退状态。
    """
    payload = copy.deepcopy(item["payload"])
    status = item["status"]
    normalized = entry["normalized"]
    prior_strength = payload.get("strength_dbm")
    late = status not in OFFLINE_ACTIVE_STATUSES
    changed = False
    applied = False
    if not late:
        baseline = payload.get("last_measurement") or {
            "observed_at": payload.get("detected_at"),
            "vehicle_id": "",
            "measurement_id": "",
        }
        new_key = _measurement_key(
            normalized["observed_at"], entry["vehicle_id"], normalized.get("measurement_id")
        )
        old_key = _measurement_key(
            baseline.get("observed_at"), baseline.get("vehicle_id"), baseline.get("measurement_id")
        )
        if new_key > old_key:
            applied = True
    if applied:
        # 服务端重算评估，车里上报的 score/level/assessment 一律不采用
        revision = {
            "old_strength_dbm": payload.get("strength_dbm"),
            "new_strength_dbm": normalized["strength_dbm"],
            "reason": "offline measurement %s" % entry["dedup_key"],
            "actor": actor,
            "source": "offline",
            "vehicle_id": entry["vehicle_id"],
            "batch_id": entry["batch_id"],
            "measurement_id": normalized.get("measurement_id"),
        }
        payload.setdefault("measurement_revisions", []).append(revision)
        payload["strength_dbm"] = normalized["strength_dbm"]
        payload["assessment"] = assess(payload)
        payload["last_measurement"] = {
            "observed_at": normalized["observed_at"],
            "vehicle_id": entry["vehicle_id"],
            "measurement_id": normalized.get("measurement_id"),
        }
        changed = True
    # 比对基准必须不可变：全部已归档测量 + 事件原始强度。
    # 当前规范值随合并移动，纳入会导致判定随回传先后改变。
    baseline = payload.get("original_strength_dbm")
    if baseline is None:
        baseline = prior_strength
    values = [a["strength_dbm"] for a in archived if a.get("strength_dbm") is not None]
    values.append(baseline)
    disagreement = bool(values) and (max(values) - min(values)) > CONFLICT_TOLERANCE_DB
    if late or disagreement:
        conflicts = payload.setdefault("conflicts", [])
        pending = next((c for c in conflicts if c.get("status") == "pending"), None)
        if pending is None:
            pending = {
                "id": len(conflicts) + 1,
                "status": "pending",
                "reasons": [],
                "measurement_refs": [],
                "values": [],
                "opened_by": actor,
            }
            conflicts.append(pending)
        reasons = set(pending.get("reasons", []))
        if late:
            reasons.add("late_measurement")
        if disagreement:
            reasons.add("measurement_disagreement")
        pending["reasons"] = sorted(reasons)
        pending["measurement_refs"] = sorted(a["dedup_key"] for a in archived)
        pending["values"] = sorted({round(v, 3) for v in values})
        changed = True
    conflict_pending = any(c.get("status") == "pending" for c in payload.get("conflicts", []))
    info = {"applied": applied, "late": late, "conflict_pending": conflict_pending}
    return changed, payload, info


def _text(payload, name):
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise DomainError("field_required", "%s 不能为空" % name)
    return value.strip()


def apply_action(item, action, payload, actor, role):
    status = item["status"]
    current = dict(item["payload"])

    if action == "assess":
        _need_status(item, {"pending", "assessed"})
        current["assessment"] = assess(current)
        return "assessed", current, {"assessment": current["assessment"]}

    if action == "correct_measurement":
        _need_status(item, {"pending", "assessed", "located"})
        try:
            strength = float(payload["strength_dbm"])
        except (KeyError, TypeError, ValueError):
            raise DomainError("field_required", "strength_dbm 不能为空")
        revision = {
            "old_strength_dbm": current.get("strength_dbm"),
            "new_strength_dbm": strength,
            "reason": _text(payload, "reason"),
            "actor": actor,
        }
        current.setdefault("measurement_revisions", []).append(revision)
        current["strength_dbm"] = strength
        current["assessment"] = assess(current)
        return status, current, {"revision": revision}

    if action == "locate":
        _need_no_pending_conflict(item)
        _need_status(item, {"assessed", "located"})
        location = _text(payload, "location")
        confidence = float(payload.get("confidence", 0))
        if confidence < 0.6:
            raise DomainError("low_location_confidence", "定位置信度低于0.6，不能进入处置", 409)
        current["location"] = {"label": location, "confidence": confidence}
        return "located", current, {"location": current["location"]}

    if action == "suspend":
        _need_no_pending_conflict(item)
        _need_status(item, {"located", "suspended"})
        authorization = _text(payload, "authorization_code")
        if not authorization.startswith("REG-"):
            raise DomainError("invalid_authorization", "停用授权编号无效", 403)
        current["suspend_authorization"] = authorization
        return "suspended", current, {"authorization_code": authorization}

    if action == "coordinate":
        _need_status(item, {"suspended"})
        agreement = _text(payload, "coordination_agreement")
        current["coordination_agreement"] = agreement
        current["coordination_note"] = payload.get("note", "")
        return "coordinating", current, {"coordination_agreement": agreement}

    if action == "resolve":
        _need_status(item, {"coordinating"})
        if not payload.get("measurement_cleared"):
            raise DomainError("interference_present", "干扰尚未消除，不能结案", 409)
        current["resolution"] = {"evidence": _text(payload, "evidence"), "cleared": True}
        return "resolved", current, {"evidence": current["resolution"]["evidence"]}

    if action == "cancel":
        _need_status(item, {"pending", "assessed"})
        reason = _text(payload, "reason")
        current["cancellation"] = {"reason": reason, "actor": actor}
        return "cancelled", current, {"reason": reason}

    if action == "review_conflicts":
        conflicts = current.get("conflicts") or []
        pending = [c for c in conflicts if c.get("status") == "pending"]
        if not pending:
            raise DomainError("no_pending_conflict", "没有待核对冲突", 409)
        note = payload.get("note", "")
        if not isinstance(note, str):
            raise DomainError("invalid_note", "note 必须是字符串")
        reviewed = []
        for entry in pending:
            entry["status"] = "reviewed"
            entry["reviewed_by"] = actor
            entry["review_note"] = note.strip()
            reviewed.append(entry["id"])
        return status, current, {"reviewed_conflict_ids": reviewed, "note": note.strip()}

    raise DomainError("unknown_action", "不支持的操作")
