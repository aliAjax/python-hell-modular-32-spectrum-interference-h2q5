import json
import sqlite3
from datetime import datetime, timezone

from .audit import audit_hash, canonical_json
from .domain import ConflictError, NotFoundError, DomainError


def now_iso():
    return datetime.now(timezone.utc).isoformat()


class Repository:
    def __init__(self, path):
        self.path = path

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def initialize(self):
        conn = self.connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_type TEXT NOT NULL,
                    stable_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_role TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(entity_type, stable_key)
                );
                CREATE TABLE IF NOT EXISTS sources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL,
                    source_type TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, source_type, external_id),
                    FOREIGN KEY(item_id) REFERENCES items(id)
                );
                CREATE TABLE IF NOT EXISTS actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    role TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(item_id) REFERENCES items(id)
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER,
                    event_type TEXT NOT NULL,
                    actor TEXT,
                    role TEXT,
                    payload TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    event_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS offline_batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_uuid TEXT,
                    vehicle_id TEXT,
                    region TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    role TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS offline_measurements (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id INTEGER NOT NULL,
                    item_id INTEGER,
                    stable_key TEXT NOT NULL,
                    match_key TEXT NOT NULL,
                    region TEXT NOT NULL,
                    station_id TEXT NOT NULL,
                    frequency_mhz REAL NOT NULL,
                    bandwidth_mhz REAL NOT NULL,
                    strength_dbm REAL NOT NULL,
                    detected_at TEXT NOT NULL,
                    offline_assessment TEXT,
                    content_hash TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    disposition TEXT NOT NULL,
                    conflict_reason TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(batch_id) REFERENCES offline_batches(id),
                    FOREIGN KEY(item_id) REFERENCES items(id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_offline_batches_uuid
                    ON offline_batches(batch_uuid);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_offline_measurements_hash
                    ON offline_measurements(content_hash);
                CREATE INDEX IF NOT EXISTS idx_offline_measurements_match
                    ON offline_measurements(match_key);
                """
            )
        finally:
            conn.close()

    def _row_to_item(self, row):
        if row is None:
            return None
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        return result

    def _last_hash(self, conn, item_id):
        row = conn.execute(
            "SELECT event_hash FROM audit_events WHERE item_id IS ? ORDER BY id DESC LIMIT 1",
            (item_id,),
        ).fetchone()
        return row["event_hash"] if row else "GENESIS"

    def append_audit(self, conn, item_id, event_type, actor, role, payload):
        previous = self._last_hash(conn, item_id)
        event = {
            "item_id": item_id,
            "event_type": event_type,
            "actor": actor,
            "role": role,
            "payload": payload,
            "created_at": now_iso(),
        }
        event_hash = audit_hash(previous, event)
        conn.execute(
            "INSERT INTO audit_events(item_id,event_type,actor,role,payload,previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (item_id, event_type, actor, role, canonical_json(payload), previous, event_hash, event["created_at"]),
        )

    def create_item(self, entity_type, stable_key, initial_status, payload, actor, role):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT INTO items(entity_type,stable_key,status,version,payload,created_by,created_role,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        entity_type,
                        stable_key,
                        initial_status,
                        1,
                        canonical_json(payload),
                        actor,
                        role,
                        now_iso(),
                        now_iso(),
                    ),
                )
            except sqlite3.IntegrityError:
                raise ConflictError("duplicate_item", "同一业务实体已经存在")
            item_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            self.append_audit(conn, item_id, "created", actor, role, {"stable_key": stable_key})
            conn.execute("COMMIT")
            return self.get_item(item_id)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def get_item(self, item_id):
        conn = self.connect()
        try:
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            return self._row_to_item(row)
        finally:
            conn.close()

    def list_items(self, status=None):
        conn = self.connect()
        try:
            if status:
                rows = conn.execute("SELECT * FROM items WHERE status=? ORDER BY id DESC", (status,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM items ORDER BY id DESC").fetchall()
            return [self._row_to_item(row) for row in rows]
        finally:
            conn.close()

    def add_source(self, item_id, source_type, external_id, payload, observed_at, actor, role):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            item = conn.execute("SELECT id FROM items WHERE id=?", (item_id,)).fetchone()
            if item is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            try:
                conn.execute(
                    "INSERT INTO sources(item_id,source_type,external_id,payload,observed_at,created_at) VALUES(?,?,?,?,?,?)",
                    (item_id, source_type, external_id, canonical_json(payload), observed_at, now_iso()),
                )
            except sqlite3.IntegrityError:
                raise ConflictError("duplicate_source", "同一来源记录已经提交")
            source_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            self.append_audit(
                conn,
                item_id,
                "source_recorded",
                actor,
                role,
                {"source_id": source_id, "source_type": source_type, "external_id": external_id},
            )
            conn.execute("COMMIT")
            return {"id": source_id, "item_id": item_id, "source_type": source_type, "external_id": external_id, "payload": payload, "observed_at": observed_at}
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def list_sources(self, item_id):
        conn = self.connect()
        try:
            rows = conn.execute("SELECT * FROM sources WHERE item_id=? ORDER BY id DESC", (item_id,)).fetchall()
            result = []
            for row in rows:
                value = dict(row)
                value["payload"] = json.loads(value["payload"])
                result.append(value)
            return result
        finally:
            conn.close()

    def apply_action(self, item_id, action, actor, role, new_status, new_payload, event_payload, expected_version=None):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            if expected_version is not None and int(expected_version) != int(row["version"]):
                raise ConflictError("version_conflict", "记录已被其他操作更新，请重新读取")
            version = int(row["version"]) + 1
            conn.execute(
                "UPDATE items SET status=?,version=?,payload=?,updated_at=? WHERE id=?",
                (new_status, version, canonical_json(new_payload), now_iso(), item_id),
            )
            conn.execute(
                "INSERT INTO actions(item_id,action,actor,role,payload,created_at) VALUES(?,?,?,?,?,?)",
                (item_id, action, actor, role, canonical_json(event_payload), now_iso()),
            )
            self.append_audit(conn, item_id, action, actor, role, event_payload)
            conn.execute("COMMIT")
            return self.get_item(item_id)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def audit_trail(self, item_id):
        conn = self.connect()
        try:
            rows = conn.execute("SELECT * FROM audit_events WHERE item_id=? ORDER BY id", (item_id,)).fetchall()
            result = []
            for row in rows:
                value = dict(row)
                value["payload"] = json.loads(value["payload"])
                result.append(value)
            return result
        finally:
            conn.close()

    def state_summary(self):
        conn = self.connect()
        try:
            counts = {}
            for row in conn.execute("SELECT status, COUNT(*) AS total FROM items GROUP BY status").fetchall():
                counts[row["status"]] = row["total"]
            return {"counts": counts, "items": self.list_items()}
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # 离线回收 (offline recovery)
    # ------------------------------------------------------------------

    def batch_exists(self, batch_uuid):
        """判断批次号是否已入库（无批次号时返回 False）。"""
        if not batch_uuid:
            return False
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT id FROM offline_batches WHERE batch_uuid=?", (batch_uuid,)
            ).fetchone()
            return row is not None
        finally:
            conn.close()

    def create_offline_batch(self, batch_uuid, vehicle_id, region, payload, actor, role):
        """创建离线批次。批次号重复时抛 ConflictError。"""
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT INTO offline_batches(batch_uuid,vehicle_id,region,actor,role,payload,created_at) VALUES(?,?,?,?,?,?,?)",
                    (batch_uuid, vehicle_id, region, actor, role, canonical_json(payload), now_iso()),
                )
            except sqlite3.IntegrityError:
                raise ConflictError("duplicate_batch", "同一批次已经上传，请勿重复提交")
            batch_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            conn.execute("COMMIT")
            return batch_id
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def measurement_hash_exists(self, content_hash):
        """判断测量内容哈希是否已入库（去重）。"""
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT id FROM offline_measurements WHERE content_hash=?", (content_hash,)
            ).fetchone()
            return row is not None
        finally:
            conn.close()

    def store_offline_measurement(self, batch_id, measurement, content_hash, disposition, conflict_reason):
        """存储一条离线测量。内容哈希已存在时返回 None（去重，不重复入库）。"""
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if self.measurement_hash_exists(content_hash):
                conn.execute("ROLLBACK")
                return None
            item_id = measurement.get("item_id")
            offline_assessment = measurement.get("offline_assessment")
            conn.execute(
                "INSERT INTO offline_measurements(batch_id,item_id,stable_key,match_key,region,station_id,frequency_mhz,bandwidth_mhz,strength_dbm,detected_at,offline_assessment,content_hash,payload,disposition,conflict_reason,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                    canonical_json(offline_assessment) if offline_assessment is not None else None,
                    content_hash,
                    canonical_json(measurement["payload"]),
                    disposition,
                    conflict_reason,
                    now_iso(),
                ),
            )
            measurement_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            conn.execute("COMMIT")
            return measurement_id
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def find_latest_item_by_match_key(self, match_key):
        """按 match_key（station_id|region|frequency）查找最近的业务实体。"""
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT * FROM items WHERE stable_key LIKE ? ORDER BY id DESC LIMIT 1",
                (match_key + "%",),
            ).fetchone()
            return self._row_to_item(row)
        finally:
            conn.close()

    def list_offline_batches(self):
        conn = self.connect()
        try:
            rows = conn.execute("SELECT * FROM offline_batches ORDER BY id DESC").fetchall()
            result = []
            for row in rows:
                value = dict(row)
                value["payload"] = json.loads(value["payload"])
                result.append(value)
            return result
        finally:
            conn.close()

    def list_offline_measurements(self, batch_id=None, disposition=None):
        conn = self.connect()
        try:
            sql = "SELECT * FROM offline_measurements WHERE 1=1"
            params = []
            if batch_id is not None:
                sql += " AND batch_id=?"
                params.append(batch_id)
            if disposition is not None:
                sql += " AND disposition=?"
                params.append(disposition)
            sql += " ORDER BY id DESC"
            rows = conn.execute(sql, params).fetchall()
            result = []
            for row in rows:
                value = dict(row)
                value["payload"] = json.loads(value["payload"])
                if value.get("offline_assessment"):
                    value["offline_assessment"] = json.loads(value["offline_assessment"])
                result.append(value)
            return result
        finally:
            conn.close()

    def update_item_measurement(self, item_id, new_payload, actor, role):
        """合并离线测量到业务实体：更新 payload、自增版本、记录审计，状态不变。"""
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            version = int(row["version"]) + 1
            conn.execute(
                "UPDATE items SET payload=?,version=?,updated_at=? WHERE id=?",
                (canonical_json(new_payload), version, now_iso(), item_id),
            )
            self.append_audit(
                conn,
                item_id,
                "measurement_merged",
                actor,
                role,
                {"strength_dbm": new_payload.get("strength_dbm"), "detected_at": new_payload.get("detected_at")},
            )
            conn.execute("COMMIT")
            return self.get_item(item_id)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()
