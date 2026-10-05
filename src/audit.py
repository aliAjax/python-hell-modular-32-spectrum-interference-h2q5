import hashlib
import json


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def audit_hash(previous_hash, event):
    payload = canonical_json(event)
    return hashlib.sha256((previous_hash + payload).encode("utf-8")).hexdigest()


def measurement_content_hash(measurement):
    """离线测量内容指纹：相同业务字段+测量值视为同一内容，用于去重。"""
    key = {
        "station_id": measurement["station_id"],
        "region": measurement["region"],
        "frequency_mhz": measurement["frequency_mhz"],
        "bandwidth_mhz": measurement["bandwidth_mhz"],
        "detected_at": measurement["detected_at"],
        "strength_dbm": measurement["strength_dbm"],
    }
    return hashlib.sha256(canonical_json(key).encode("utf-8")).hexdigest()
