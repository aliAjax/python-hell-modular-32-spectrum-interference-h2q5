from datetime import datetime


class DomainError(Exception):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code = code
        self.status = status


class ConflictError(DomainError):
    def __init__(self, code, message):
        super().__init__(code, message, 409)


class NotFoundError(DomainError):
    def __init__(self, code, message):
        super().__init__(code, message, 404)


def require_text(payload, name):
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise DomainError("field_required", "%s 不能为空" % name)
    return value.strip()


def number(payload, name, minimum=None, maximum=None):
    value = payload.get(name)
    if isinstance(value, bool):
        raise DomainError("invalid_number", "%s 必须是数字" % name)
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise DomainError("invalid_number", "%s 必须是数字" % name)
    if minimum is not None and value < minimum:
        raise DomainError("invalid_number", "%s 不能小于 %s" % (name, minimum))
    if maximum is not None and value > maximum:
        raise DomainError("invalid_number", "%s 不能大于 %s" % (name, maximum))
    return value


def parse_timestamp(payload, name):
    value = require_text(payload, name)
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise DomainError("invalid_timestamp", "%s 必须是 ISO 时间" % name)
    return value


def normalize_create(payload):
    frequency = number(payload, "frequency_mhz", 0.001, 300000)
    bandwidth = number(payload, "bandwidth_mhz", 0.001)
    station_id = require_text(payload, "station_id")
    region = require_text(payload, "region")
    strength = number(payload, "strength_dbm")
    detected_at = parse_timestamp(payload, "detected_at")
    reporter = require_text(payload, "reporter")
    stable_key = "%s|%s|%s|%s" % (station_id, region, frequency, detected_at)
    return {
        "frequency_mhz": frequency,
        "bandwidth_mhz": bandwidth,
        "station_id": station_id,
        "region": region,
        "strength_dbm": strength,
        "original_strength_dbm": strength,
        "detected_at": detected_at,
        "reporter": reporter,
        "measurement_revisions": [],
        "suspend_authorization": None,
        "_stable_key": stable_key,
    }


MAX_OFFLINE_BATCH = 200


def normalize_offline_measurement(payload):
    if not isinstance(payload, dict):
        raise DomainError("invalid_measurement", "测量记录必须是对象")
    item_id = payload.get("item_id")
    if isinstance(item_id, bool):
        raise DomainError("invalid_number", "item_id 必须是整数")
    try:
        item_id = int(item_id)
    except (TypeError, ValueError):
        raise DomainError("invalid_number", "item_id 必须是整数")
    measurement_id = payload.get("measurement_id")
    if measurement_id is not None:
        measurement_id = str(measurement_id).strip() or None
    region = payload.get("region")
    if region is not None:
        region = str(region).strip() or None
    return {
        "item_id": item_id,
        "strength_dbm": number(payload, "strength_dbm"),
        "observed_at": parse_timestamp(payload, "observed_at"),
        "measurement_id": measurement_id,
        "region": region,
        "station_id": payload.get("station_id"),
        "frequency_mhz": payload.get("frequency_mhz"),
    }


def normalize_offline_batch(payload):
    batch_id = payload.get("batch_id")
    if batch_id is not None:
        if not isinstance(batch_id, str) or not batch_id.strip():
            raise DomainError("invalid_batch", "batch_id 无效")
        batch_id = batch_id.strip()
    measurements = payload.get("measurements")
    if not isinstance(measurements, list) or not measurements:
        raise DomainError("field_required", "measurements 不能为空")
    if len(measurements) > MAX_OFFLINE_BATCH:
        raise DomainError("batch_too_large", "单批次最多 %d 条测量" % MAX_OFFLINE_BATCH)
    normalized = []
    for raw in measurements:
        normalized.append((normalize_offline_measurement(raw), raw))
    return batch_id, normalized


def normalize_source(payload):
    source_type = require_text(payload, "source_type")
    external_id = require_text(payload, "external_id")
    observed_at = parse_timestamp(payload, "observed_at")
    strength = number(payload, "strength_dbm")
    region = payload.get("region")
    if region is not None:
        region = str(region).strip() or None
    return {
        "source_type": source_type,
        "external_id": external_id,
        "observed_at": observed_at,
        "strength_dbm": strength,
        "region": region,
        "station_id": payload.get("station_id"),
        "frequency_mhz": payload.get("frequency_mhz"),
    }
