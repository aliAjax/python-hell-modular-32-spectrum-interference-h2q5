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
        "detected_at": detected_at,
        "reporter": reporter,
        "measurement_revisions": [],
        "suspend_authorization": None,
        "_stable_key": stable_key,
    }


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


def normalize_offline_measurement(payload):
    """归一化单条离线测量。"""
    station_id = require_text(payload, "station_id")
    region = require_text(payload, "region")
    frequency = number(payload, "frequency_mhz", 0.001, 300000)
    bandwidth = number(payload, "bandwidth_mhz", 0.001)
    strength = number(payload, "strength_dbm")
    detected_at = parse_timestamp(payload, "detected_at")
    vehicle_id = payload.get("vehicle_id")
    if vehicle_id is not None:
        vehicle_id = str(vehicle_id).strip() or None
    offline_assessment = payload.get("offline_assessment")
    if offline_assessment is not None and not isinstance(offline_assessment, dict):
        raise DomainError("invalid_payload", "offline_assessment 必须是对象")
    stable_key = "%s|%s|%s|%s" % (station_id, region, frequency, detected_at)
    match_key = "%s|%s|%s|" % (station_id, region, frequency)
    return {
        "station_id": station_id,
        "region": region,
        "frequency_mhz": frequency,
        "bandwidth_mhz": bandwidth,
        "strength_dbm": strength,
        "detected_at": detected_at,
        "vehicle_id": vehicle_id,
        "offline_assessment": offline_assessment,
        "stable_key": stable_key,
        "match_key": match_key,
        "payload": payload,
    }


def normalize_offline_batch(payload):
    """归一化离线批次回传。批次号可选（旧数据无批次号兼容并入）。"""
    if not isinstance(payload, dict):
        raise DomainError("invalid_payload", "请求体必须是对象")
    measurements = payload.get("measurements")
    if not isinstance(measurements, list) or not measurements:
        raise DomainError("invalid_payload", "measurements 必须是非空数组")
    batch_uuid = payload.get("batch_id")
    if batch_uuid is not None:
        batch_uuid = str(batch_uuid).strip() or None
    vehicle_id = payload.get("vehicle_id")
    if vehicle_id is not None:
        vehicle_id = str(vehicle_id).strip() or None
    normalized = [normalize_offline_measurement(m) for m in measurements]
    return {
        "batch_id": batch_uuid,
        "vehicle_id": vehicle_id,
        "measurements": normalized,
    }
