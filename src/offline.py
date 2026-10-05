import sqlite3

from . import domain, rules
from .audit import canonical_json, measurement_content_hash
from .domain import ConflictError, DomainError
from .repository import now_iso


def _build_item_payload(measurement):
    """从离线测量构建业务实体 payload。

    服务端重算评估，不采用车里离线结果。
    """
    payload = {
        "frequency_mhz": measurement["frequency_mhz"],
        "bandwidth_mhz": measurement["bandwidth_mhz"],
        "station_id": measurement["station_id"],
        "region": measurement["region"],
        "strength_dbm": measurement["strength_dbm"],
        "detected_at": measurement["detected_at"],
        "reporter": measurement.get("vehicle_id") or "offline-vehicle",
        "measurement_revisions": [],
        "suspend_authorization": None,
    }
    payload["assessment"] = rules.assess(payload)
    return payload


def _append_audit(repo, conn, item_id, event_type, actor, role, payload):
    repo.append_audit(conn, item_id, event_type, actor, role, payload)


def _merge_into_item(repo, conn, item, measurement, actor, role):
    """把离线测量合并到已存在的业务实体。

    返回 (disposition, conflict_reason)。
    规则：
    - 已结案/停用：留档，状态不回退，改挂待核对冲突
    - 测量时间更新：服务端重算后合并
    - 测量时间相同且值不同：冲突，挂待核对
    - 测量时间更早：旧版本丢弃
    """
    item_id = item["id"]
    item_payload = dict(item["payload"])
    item_status = item["status"]

    if item_status in rules.TERMINAL_STATUSES:
        item_payload["pending_conflict"] = True
        conn.execute(
            "UPDATE items SET payload=?,version=version+1,updated_at=? WHERE id=?",
            (canonical_json(item_payload), now_iso(), item_id),
        )
        _append_audit(
            repo, conn, item_id, "measurement_archived", actor, role,
            {"reason": "案件已结案，迟到测量留档待核对"},
        )
        return "archived", "案件已结案，迟到测量留档待核对"

    item_detected_at = item_payload.get("detected_at")
    measurement_detected_at = measurement["detected_at"]

    if measurement_detected_at > item_detected_at:
        new_payload = _build_item_payload(measurement)
        new_payload["pending_conflict"] = item_payload.get("pending_conflict", False)
        conn.execute(
            "UPDATE items SET payload=?,version=version+1,updated_at=? WHERE id=?",
            (canonical_json(new_payload), now_iso(), item_id),
        )
        _append_audit(
            repo, conn, item_id, "measurement_merged", actor, role,
            {"strength_dbm": new_payload.get("strength_dbm"), "detected_at": new_payload.get("detected_at")},
        )
        return "merged", None

    if measurement_detected_at == item_detected_at:
        if measurement["strength_dbm"] != item_payload.get("strength_dbm"):
            item_payload["pending_conflict"] = True
            conn.execute(
                "UPDATE items SET payload=?,version=version+1,updated_at=? WHERE id=?",
                (canonical_json(item_payload), now_iso(), item_id),
            )
            _append_audit(
                repo, conn, item_id, "measurement_conflict", actor, role,
                {"reason": "同一时间测量值不一致"},
            )
            return "conflict", "同一时间测量值不一致，待核对"
        return "duplicate", None

    return "discarded", "测量时间早于当前记录，旧版本丢弃"


def _process_one(repo, conn, batch_id, measurement, actor, role):
    """在单一事务内处理一条离线测量。

    返回 (disposition, item_id, conflict_reason)。
    去重和冲突判定在 BEGIN IMMEDIATE 事务内完成，
    并发提交时结果确定，不随先后改变。
    """
    content_hash = measurement_content_hash(measurement)

    # 去重：内容指纹已存在则不重复入库
    row = conn.execute(
        "SELECT id FROM offline_measurements WHERE content_hash=?", (content_hash,)
    ).fetchone()
    if row:
        return "duplicate", None, None

    # 按 match_key 查找最近的业务实体
    item_row = conn.execute(
        "SELECT * FROM items WHERE stable_key LIKE ? ORDER BY id DESC LIMIT 1",
        (measurement["match_key"] + "%",),
    ).fetchone()

    if item_row is None:
        # 新建业务实体
        payload = _build_item_payload(measurement)
        try:
            conn.execute(
                "INSERT INTO items(entity_type,stable_key,status,version,payload,created_by,created_role,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    rules.ENTITY_TYPE,
                    measurement["stable_key"],
                    rules.INITIAL_STATUS,
                    1,
                    canonical_json(payload),
                    actor,
                    role,
                    now_iso(),
                    now_iso(),
                ),
            )
        except sqlite3.IntegrityError:
            # 并发创建：回退为合并
            item_row = conn.execute(
                "SELECT * FROM items WHERE stable_key LIKE ? ORDER BY id DESC LIMIT 1",
                (measurement["match_key"] + "%",),
            ).fetchone()
            if item_row is None:
                raise
            item = repo._row_to_item(item_row)
            item_id = item["id"]
            disposition, conflict_reason = _merge_into_item(repo, conn, item, measurement, actor, role)
        else:
            item_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            _append_audit(
                repo, conn, item_id, "created", actor, role,
                {"stable_key": measurement["stable_key"]},
            )
            disposition, conflict_reason = "merged", None
    else:
        item = repo._row_to_item(item_row)
        item_id = item["id"]
        disposition, conflict_reason = _merge_into_item(repo, conn, item, measurement, actor, role)

    # 存储离线测量记录
    conn.execute(
        "INSERT INTO offline_measurements(batch_id,item_id,stable_key,match_key,region,station_id,frequency_mhz,bandwidth_mhz,strength_dbm,detected_at,offline_assessment,content_hash,payload,disposition,conflict_reason,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            batch_id,
            item_id,
            measurement["stable_key"],
            measurement["match_key"],
            measurement["region"],
            measurement["station_id"],
            measurement["frequency_mhz"],
            measurement["bandwidth_mhz"],
            measurement["strength_dbm"],
            measurement["detected_at"],
            canonical_json(measurement["offline_assessment"]) if measurement["offline_assessment"] is not None else None,
            content_hash,
            canonical_json(measurement["payload"]),
            disposition,
            conflict_reason,
            now_iso(),
        ),
    )
    return disposition, item_id, conflict_reason


def process_batch(repo, batch_payload, actor, role, region_header=None):
    """处理一批离线测量回传。

    多车批量回传，重复上传只入库一次。
    测量更新后服务端重算，不采用车里结果。
    结案停用后迟到测量留档，状态不回退，改挂待核对冲突。
    监测车只交本区测量，跨区守角色边界。
    旧数据无批次号兼容并入。
    """
    if not actor or not role:
        raise DomainError("identity_required", "需要用户身份和角色", 401)
    if role not in rules.OFFLINE_ROLES and role != "regulator":
        raise DomainError("forbidden", "当前角色不能回传离线测量", 403)

    normalized = domain.normalize_offline_batch(batch_payload)
    measurements = normalized["measurements"]

    # 确定车辆管辖区域
    vehicle_region = region_header
    if not vehicle_region:
        vehicle_region = measurements[0]["region"]

    # 区域边界：监测车只交本区测量，跨区守角色边界
    if rules.ENFORCE_REGION and role != "regulator":
        for m in measurements:
            if m["region"] != vehicle_region:
                raise DomainError("region_mismatch", "离线测量不属于当前管辖区域", 403)

    conn = repo.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")

        # 创建批次（批次号重复时由 UNIQUE 约束拒绝）
        try:
            conn.execute(
                "INSERT INTO offline_batches(batch_uuid,vehicle_id,region,actor,role,payload,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    normalized["batch_id"],
                    normalized["vehicle_id"],
                    vehicle_region,
                    actor,
                    role,
                    canonical_json(batch_payload),
                    now_iso(),
                ),
            )
        except sqlite3.IntegrityError:
            conn.execute("ROLLBACK")
            raise ConflictError("duplicate_batch", "同一批次已经上传，请勿重复提交")

        batch_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]

        results = []
        summary = {"merged": 0, "duplicate": 0, "conflict": 0, "archived": 0, "discarded": 0}
        for measurement in measurements:
            disposition, item_id, conflict_reason = _process_one(
                repo, conn, batch_id, measurement, actor, role
            )
            results.append({
                "station_id": measurement["station_id"],
                "region": measurement["region"],
                "frequency_mhz": measurement["frequency_mhz"],
                "detected_at": measurement["detected_at"],
                "disposition": disposition,
                "item_id": item_id,
                "conflict_reason": conflict_reason,
            })
            summary[disposition] += 1

        conn.execute("COMMIT")
    except ConflictError:
        raise
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()

    return {
        "batch_id": batch_id,
        "batch_uuid": normalized["batch_id"],
        "vehicle_id": normalized["vehicle_id"],
        "region": vehicle_region,
        "total": len(measurements),
        "summary": summary,
        "results": results,
    }
