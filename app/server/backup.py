# -*- coding: utf-8 -*-
"""历史数据的导出、自动备份与导入。

导出格式（format_version 2）：
    {
      "format": "ssdplus.history",
      "format_version": 2,
      "exported_at": "2026-11-04T17:00:00+0800",
      "disk": {"disk_key","serial","device","model","firmware","protocol","capacity_bytes"},
      "sample_interval_seconds": 60,
      "sample_count": 1234,
      "samples": [{"ts","time","read_bytes_total","write_bytes_total",
                   "read_rate_bps","write_rate_bps","temp_c","health_pct","percent_used"}]
    }

整机备份把多个这样的 payload 放进 "disks" 数组，导入时两种都能吃。
"""

from __future__ import annotations

import json
import os
import re
import time

import store

FORMAT = "ssdplus.history"
FORMAT_VERSION = 2
BACKUP_FORMAT = "ssdplus.backup"
BACKUP_PREFIX = "ssdplus-backup-"


def _sample_rows(conn, disk_key: str) -> list[dict]:
    rows = store.samples_for(conn, disk_key)
    out = []
    for row in rows:
        out.append({
            "ts": row["ts"],
            "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(row["ts"])),
            "read_bytes_total": row["acc_read"],
            "write_bytes_total": row["acc_write"],
            "read_rate_bps": row["read_rate"],
            "write_rate_bps": row["write_rate"],
            "temp_c": row["temp"],
            "health_pct": row["health"],
            "percent_used": row["percent_used"],
        })
    return out


def build_payload(app, disk_key: str, device: str, model: str, info: dict | None = None) -> dict:
    conn = store.connect(app.cfg.db_path)
    try:
        samples = _sample_rows(conn, disk_key)
        meta = store.disk_meta(conn, disk_key) or {}
    finally:
        conn.close()

    info = info or {}
    return {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "disk": {
            "disk_key": disk_key,
            "serial": (info.get("serial") or meta.get("serial") or "").strip(),
            "device": device,
            "model": info.get("model") or meta.get("model") or device,
            "firmware": info.get("firmware") or meta.get("firmware") or "",
            "protocol": info.get("protocol") or meta.get("protocol") or "",
            "capacity_bytes": info.get("capacity") or meta.get("capacity") or 0,
        },
        "sample_interval_seconds": int(app.cfg.get("sample_interval") or 60),
        "sample_count": len(samples),
        "samples": samples,
    }


def build_all(app) -> dict:
    """把当前所有硬盘（含已离线的）都导出成一份备份结构。"""
    disks, _, _ = app.sampler.snapshot()
    conn = store.connect(app.cfg.db_path)
    try:
        known = {row["disk_key"]: row for row in store.all_disk_meta(conn)}
    finally:
        conn.close()

    payloads = []
    seen = set()
    for device, info in disks.items():
        key = info.get("key") or ("dv:" + device)
        payloads.append(build_payload(app, key, device, info.get("model") or device, info))
        seen.add(key)
    for key, meta in known.items():
        if key in seen:
            continue
        payloads.append(build_payload(app, key, meta.get("device") or "", meta.get("model") or ""))

    return {
        "format": BACKUP_FORMAT,
        "format_version": FORMAT_VERSION,
        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "disk_count": len(payloads),
        "disks": payloads,
    }


def safe_name(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "", text) or "disk"


def write_backup(app) -> dict:
    """写一份整机备份到备份目录，并按保留份数轮转。"""
    cfg = app.cfg
    folder = str(cfg.get("backup_dir") or os.path.join(cfg.data_dir, "backup"))
    os.makedirs(folder, exist_ok=True)
    payload = build_all(app)
    name = BACKUP_PREFIX + time.strftime("%Y%m%d-%H%M%S") + ".json"
    path = os.path.join(folder, name)
    with open(path, "w", encoding="utf-8") as fp:
        json.dump(payload, fp, ensure_ascii=False)
    removed = _rotate(folder, int(cfg.get("backup_keep") or 14))
    cfg.set("backup_last_at", int(time.time()))
    cfg.set("backup_last_path", path)
    cfg.save()
    return {"path": path, "disk_count": payload["disk_count"],
            "bytes": os.path.getsize(path), "removed": removed}


def _rotate(folder: str, keep: int) -> int:
    keep = max(1, keep)
    try:
        files = sorted(
            (f for f in os.listdir(folder) if f.startswith(BACKUP_PREFIX) and f.endswith(".json")),
            reverse=True,
        )
    except OSError:
        return 0
    removed = 0
    for name in files[keep:]:
        try:
            os.unlink(os.path.join(folder, name))
            removed += 1
        except OSError:
            pass
    return removed


def maybe_backup(app) -> dict | None:
    """按配置的间隔决定是否该备份；每天最多检查一次。"""
    cfg = app.cfg
    if not bool(cfg.get("backup_enabled")):
        return None
    interval_days = max(1, int(cfg.get("backup_interval_days") or 1))
    last = int(cfg.get("backup_last_at") or 0)
    if time.time() - last < interval_days * 86400:
        return None
    return write_backup(app)


def import_payload(app, payload: dict) -> dict:
    """导入导出的 JSON；只按序列号匹配，按 (disk_key, ts) 去重。

    支持两种输入：单个 disk payload，或整机备份（含 disks 数组）。
    """
    if not isinstance(payload, dict):
        raise ValueError("文件内容不是 JSON 对象")

    blocks: list[dict] = []
    if isinstance(payload.get("disks"), list):
        blocks = [b for b in payload["disks"] if isinstance(b, dict)]
    elif payload.get("format") == FORMAT or "samples" in payload:
        blocks = [payload]
    if not blocks:
        raise ValueError("未找到可导入的数据（需要 ssdplus 导出的 JSON）")

    conn = store.connect(app.cfg.db_path)
    results = []
    try:
        for block in blocks:
            disk = block.get("disk") or {}
            serial = str(disk.get("serial") or "").strip()
            key = str(disk.get("disk_key") or "").strip()
            if not key:
                key = ("sn:" + serial) if serial else ("dv:" + str(disk.get("device") or "unknown"))
            samples = block.get("samples") or []
            if not isinstance(samples, list):
                continue
            device = str(disk.get("device") or "")
            added, skipped = store.merge_samples(conn, key, serial, device, samples)
            results.append({
                "disk_key": key, "serial": serial,
                "model": disk.get("model") or "", "device": device,
                "added": added, "skipped": skipped,
            })
    finally:
        conn.close()

    total_added = sum(item["added"] for item in results)
    return {"disks": results, "added": total_added}
