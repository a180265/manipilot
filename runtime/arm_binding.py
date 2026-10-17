"""Stable identity of a registered arm route, independent of camera identity.

This names an installation route, not a hardware serial-number attestation.
Daemon sessions and calibrated motion gates continue to detect reconnects.
"""
from hashlib import sha256
import json


PREFIX = "daemon-binding.v1:"


def normalize_daemon_binding(config):
    """Resolve old endpoint registrations and recompute identity on route changes."""
    result = dict(config)
    endpoint = str(result.get("daemon_addr") or "").strip()
    if not endpoint:
        # An absent daemon cannot retain a generated daemon installation identity.
        if str(result.get("daemon_binding_id") or "").startswith(PREFIX):
            result.pop("daemon_binding_id", None)
            result.pop("daemon_resource_id", None)
        return result
    resource = result.get("daemon_resource_id")
    if not resource:
        old = result.get("daemon_binding_id")
        resource = old if isinstance(old,str) and old.startswith("daemon:") else f"daemon:{result['device_id']}"
    if not isinstance(resource,str) or not resource.startswith("daemon:") or not resource[7:]:
        raise ValueError("已登记机械臂资源编号无效")
    facts = {"version":1,"device_id":str(result["device_id"]),
             "resource_id":resource,"daemon_addr":endpoint,
             "serial_port":str(result.get("serial_port") or "").strip()}
    digest = sha256(json.dumps(facts,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()).hexdigest()
    result["daemon_resource_id"] = resource
    result["daemon_binding_id"] = PREFIX + digest
    return result


def require_arm_binding(value):
    if not isinstance(value,str) or not value.strip():
        raise ValueError("缺少已登记的机械臂绑定身份，请先核对设备绑定")
    return value
