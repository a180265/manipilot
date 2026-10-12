"""可插拔硬件发现：优先使用稳定串口路径，pyserial 仅作增强探测。

发现只回答“硬件是否在场”，不会把未知硬件自动绑定到设备、启动运动或猜测归属。
对已经登记且绑定未变化的串口，设备管理器可在重新出现时受控调用 attach/self-check；
绑定变化、daemon 未连接或未知串口仍必须经过人工确认，不能把发现事件直接当成业务可用。
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path


def serial_ports() -> list[dict]:
    """返回当前可见串口及稳定描述；未安装 pyserial 时退化为空列表。"""
    try:
        from serial.tools import list_ports
    except Exception:
        return []
    result: list[dict] = []
    for item in list_ports.comports():
        path = str(item.device or "")
        if not path:
            continue
        # 指纹不含 path：同一适配器换 USB 口重插后身份不变，绑定可跨端口自愈
        # （2026-09-30 真机：21110→21130 换口后按路径匹配误判"已移除"）。
        identity = "|".join(str(value or "") for value in (
            item.vid, item.pid, item.serial_number, item.manufacturer, item.product,
        ))
        result.append({
            "path": path,
            "vid": item.vid,
            "pid": item.pid,
            "serial_number": item.serial_number,
            "manufacturer": item.manufacturer,
            "product": item.product,
            "fingerprint": hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24],
        })
    return result


def serial_probe_available() -> bool:
    try:
        from serial.tools import list_ports  # noqa: F401
    except Exception:
        return False
    return True


def port_present(path: str, *, ports: list[dict] | None = None) -> bool:
    """检查指定串口是否在场，支持测试注入和 macOS/Linux 路径。"""
    if not path:
        return False
    if ports is not None:
        return any(str(item.get("path")) == str(path) for item in ports)
    if os.path.exists(path):
        return True
    return any(str(item.get("path")) == str(path) for item in serial_ports())


def discover(known_configs: list[dict], *, ports: list[dict] | None = None) -> dict:
    """对已知绑定做在场判定，并返回未绑定串口列表供 WX/控制台提示。

    已知绑定的在场结果附硬件指纹（当该串口可见且能解析指纹时），
    供设备菜单展示“绑定 + 硬件指纹”事实。
    """
    explicit = ports is not None
    visible = ports if explicit else serial_ports()
    visible_by_path = {str(item.get("path")): item for item in visible}
    visible_paths = set(visible_by_path)
    known_paths = {str(cfg.get("serial_port")) for cfg in known_configs if cfg.get("serial_port")}
    bindings = []
    for cfg in known_configs:
        path = str(cfg.get("serial_port") or "")
        if not path:
            continue
        present = path in visible_paths if ports is not None else port_present(path, ports=visible)
        port = visible_by_path.get(path) if present else None
        bindings.append({
            "device_id": cfg.get("device_id"),
            "serial_port": path,
            "present": present,
            "hardware_fingerprint": (port or {}).get("fingerprint"),
            # 硬件身份三元组（跨端口稳定），供换口重枚举时按身份重绑
            "vid": (port or {}).get("vid"),
            "pid": (port or {}).get("pid"),
            "serial_number": (port or {}).get("serial_number"),
        })
    unknown = [item for item in visible if str(item.get("path")) not in known_paths]
    return {"bindings": bindings, "unknown_ports": unknown,
            "probe_available": bool(explicit or serial_probe_available())}
