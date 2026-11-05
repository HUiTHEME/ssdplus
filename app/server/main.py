#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SSD助手 —— 后端服务

标准库 HTTP 服务 + 静态托管 + JSON API。
数据来源：sysfs / /proc/diskstats（基础与实时）+ smartctl（健康信息）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import socketserver
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import alerts
import backup
import bench
import collector
import sampler as sampler_mod
import store

APP_NAME = "ssdplus"


def read_version() -> str:
    """版本号优先取飞牛注入的 TRIM_APPVER，其次读 manifest，避免写死。"""
    env = os.environ.get("TRIM_APPVER")
    if env:
        return env.strip()
    base = os.path.dirname(os.path.abspath(__file__))
    for rel in (os.path.join("..", "..", "manifest"), os.path.join("..", "manifest")):
        try:
            with open(os.path.join(base, rel), "r", encoding="utf-8") as fp:
                for line in fp:
                    key, _, value = line.partition("=")
                    if key.strip() == "version":
                        return value.strip()
        except OSError:
            continue
    return "1.0.0"


VERSION = read_version()

MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}


def log(message: str, exc: bool = False) -> None:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    sys.stdout.write(f"{stamp} [{APP_NAME}] {message}\n")
    if exc:
        traceback.print_exc(file=sys.stdout)
    sys.stdout.flush()


# ------------------------------------------------------------------ 配置

class Config:
    def __init__(self, port: int, www_dir: str, data_dir: str, etc_dir: str,
                 socket_path: str = "", prefix: str = "") -> None:
        self.port = port
        # 网关口（Unix Socket）模式：飞牛统一网关把 /app/{appname}/... 转发进来，不占用任何端口
        self.socket_path = socket_path
        self.prefix = (prefix or "").rstrip("/")
        self.www_dir = os.path.abspath(www_dir)
        self.data_dir = os.path.abspath(data_dir)
        self.etc_dir = os.path.abspath(etc_dir)
        self.app_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        self.db_path = os.path.join(self.data_dir, "ssdplus.db")
        self.file = os.path.join(self.etc_dir, "config.json")
        # 先把默认值放进 self.raw，再读取文件合并；否则首次运行写配置时会拿到未初始化的 raw
        self.raw = self._defaults()
        self._load()

    def _defaults(self) -> dict:
        return {
            "sample_interval": 60,     # 采样间隔（秒）
            "retention_days": 0,       # 采样明细保留天数，0 表示永久保留
            "bench_size_mb": 256,      # 测速文件大小
            "notify_health_below": 60, # 健康度告警阈值
            "show_virtual_disks": False,  # 是否显示虚拟机/云主机虚拟磁盘
            "only_ssd": True,             # 只显示固态硬盘
            # 告警（只写站内事件，暂不做 Webhook 外发）
            "alert_enabled": True,
            "notify_health_below": 90,
            "alert_temp_high": 60,
            "alert_interval_seconds": 600,   # 同一问题的通知频率（秒）
            # 备份
            "backup_enabled": True,
            "backup_dir": "",
            "backup_interval_days": 1,
            "backup_keep": 14,
            "backup_last_at": 0,
            "backup_last_path": "",
        }

    MIGRATION_KEY = "keep_all_history"

    ALERT_MIGRATION_KEY = "alert_v2"

    def _load(self) -> dict:
        self._load_file()
        # 早期版本默认 365 天自动清理，与"永久保留"的定位冲突，这里一次性改成 0
        if not self.raw.get(self.MIGRATION_KEY):
            self.raw["retention_days"] = 0
            self.raw[self.MIGRATION_KEY] = True
            self.save()
        # 告警设置换代：冷却小时 → 通知频率（秒）；阈值默认值上调；去掉重复/废弃键
        if not self.raw.get(self.ALERT_MIGRATION_KEY):
            old_hours = self.raw.pop("alert_cooldown_hours", None)
            try:
                old_hours = int(old_hours) if old_hours is not None else None
            except (TypeError, ValueError):
                old_hours = None
            if "alert_interval_seconds" not in self.raw:
                # 旧值就是默认 6 小时的话，直接落到新默认 10 分钟
                self.raw["alert_interval_seconds"] = 600 if old_hours in (None, 6) else old_hours * 3600
            self.raw.pop("alert_webhook", None)
            self.raw.pop("alert_health_below", None)
            # 只有还停留在旧默认值时才跟着改，用户自己调过的不动
            if int(self.raw.get("notify_health_below") or 0) == 60:
                self.raw["notify_health_below"] = 90
            if int(self.raw.get("alert_temp_high") or 0) == 70:
                self.raw["alert_temp_high"] = 60
            self.raw[self.ALERT_MIGRATION_KEY] = True
            self.save()
        return self.raw

    def _load_file(self) -> None:
        try:
            with open(self.file, "r", encoding="utf-8") as fp:
                stored = json.load(fp)
            if isinstance(stored, dict):
                self.raw.update(stored)
        except FileNotFoundError:
            # 首次运行：把默认配置落盘，方便用户直接编辑
            self.save()
        except Exception:
            log("读取配置失败，使用默认配置", exc=True)

    def get(self, key: str):
        return self.raw.get(key)

    def set(self, key: str, value) -> None:
        self.raw[key] = value

    def save(self, data: dict | None = None) -> None:
        if isinstance(data, dict):
            self.raw.update(data)
        try:
            os.makedirs(self.etc_dir, exist_ok=True)
            with open(self.file, "w", encoding="utf-8") as fp:
                json.dump(self.raw, fp, ensure_ascii=False, indent=2)
        except OSError:
            # 配置目录不可写时不影响服务启动，本次运行使用内存中的配置
            log(f"配置写入失败（{self.file}），本次运行使用内存配置", exc=True)


class App:
    """运行期状态容器。"""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.sampler: sampler_mod.Sampler | None = None
        self.bench_state: dict = {"running": False, "started_at": None, "result": None, "error": None}
        self.bench_lock = threading.Lock()
        self.alerts = alerts.AlertEngine()


# ------------------------------------------------------------------ 工具函数

def human_status(disk: dict, threshold: int) -> str:
    if not disk.get("smart_ok"):
        return "unknown"
    if disk.get("smart_passed") is False:
        return "bad"
    if disk.get("health") is None and disk.get("smart_passed") is None:
        # 拿不到健康度（如虚拟磁盘），不要误判为正常
        return "unknown"
    health = disk.get("health")
    if isinstance(health, int):
        if health < 40:
            return "bad"
        if health < threshold:
            return "warn"
    if (disk.get("media_errors") or 0) > 0:
        return "warn"
    return "ok"


def public_disk(info: dict, threshold: int) -> dict:
    return {
        "device": info.get("device"),
        "key": info.get("key") or ("dv:" + str(info.get("device") or "")),
        "path": info.get("path"),
        "model": info.get("model") or info.get("device"),
        "serial": info.get("serial") or "",
        "firmware": info.get("firmware") or "",
        "capacity": info.get("capacity") or 0,
        "protocol": info.get("protocol") or "",
        "bus": info.get("bus") or "",
        "is_ssd": bool(info.get("is_ssd")),
        "temp": info.get("temp"),
        "health": info.get("health"),
        "status": human_status(info, threshold),
        "smart_ok": bool(info.get("smart_ok")),
        "smart_error": info.get("smart_error") or "",
        "tbw": info.get("tbw"),
        "tbr": info.get("tbr"),
        "power_on_hours": info.get("power_on_hours"),
        "power_cycles": info.get("power_cycles"),
        "unsafe_shutdowns": info.get("unsafe_shutdowns"),
        "media_errors": info.get("media_errors"),
        "percent_used": info.get("percent_used"),
        "available_spare": info.get("available_spare"),
        "available_spare_threshold": info.get("available_spare_threshold"),
        "critical_warning": info.get("critical_warning"),
        "controller_busy_time": info.get("controller_busy_time"),
        "num_err_log_entries": info.get("num_err_log_entries"),
        "host_reads": info.get("host_reads"),
        "host_writes": info.get("host_writes"),
        "rotation_rate": info.get("rotation_rate"),
        "reallocated_sectors": info.get("reallocated_sectors"),
        "pending_sectors": info.get("pending_sectors"),
        "uncorrectable_sectors": info.get("uncorrectable_sectors"),
        "crc_errors": info.get("crc_errors"),
        "read_rate": info.get("read_rate"),
        "write_rate": info.get("write_rate"),
        "acc_read": info.get("acc_read") or 0,
        "acc_write": info.get("acc_write") or 0,
    }


def resolve_disk(app: App, device: str) -> tuple[str, dict] | tuple[None, None]:
    """设备名 → (disk_key, 实时信息)。历史数据一律按 disk_key 走。"""
    disks, _, _ = app.sampler.snapshot()
    info = disks.get(device)
    if not info:
        return None, None
    return info.get("key") or ("dv:" + device), info


def life_estimate(conn, disk_key: str) -> dict:
    """按已用寿命（NVMe Percentage Used，回退到健康度）的消耗速度估算剩余可用时间。"""
    data = store.life_inputs(conn, disk_key)
    out = {
        "status": "insufficient",
        "note": "需要累积更多采样数据",
        "used_pct": None,
        "per_year_pct": None,
        "remaining_days": None,
        "write_per_day": data.get("write_per_day"),
        "span_days": data.get("span_days") or 0,
    }
    first, last = data.get("first"), data.get("last")
    if not first or not last:
        return out

    out["used_pct"] = last["pct"]
    span = out["span_days"]
    if span < 1:
        out["note"] = "采样跨度不足 1 天"
        return out

    delta = last["pct"] - first["pct"]
    if delta <= 0:
        out.update(status="flat", note="按当前强度，暂未观测到寿命消耗")
        return out

    per_day = delta / span
    out["per_year_pct"] = round(per_day * 365, 2)
    out["remaining_days"] = int(max(0.0, 100 - last["pct"]) / per_day)
    out["status"] = "ok"
    out["note"] = ""
    return out


def fmt_days(days: int | None) -> str:
    if days is None:
        return "—"
    if days >= 365 * 30:
        return "30 年以上"
    if days >= 365:
        return f"{days // 365} 年 {int(days % 365 / 30)} 个月"
    if days >= 30:
        return f"{days // 30} 个月"
    return f"{days} 天"


def build_history(app: App, query: dict) -> dict:
    """按硬盘返回全部历史（按天聚合），供热力图与年份切换使用。"""
    disks, _, _ = app.sampler.snapshot()
    all_devices = sorted(disks.keys())
    raw_devices = (query.get("devices") or [""])[0]
    selected = [d for d in raw_devices.split(",") if d in all_devices]
    keys = [disks[d].get("key") or ("dv:" + d) for d in selected]

    conn = store.connect(app.cfg.db_path)
    try:
        daily = store.daily_history(conn, keys, days=None) if keys else []
    finally:
        conn.close()

    years = sorted({int(row["date"][:4]) for row in daily if row.get("date")}, reverse=True)
    device = selected[0] if selected else ""
    info = disks.get(device, {})

    return {
        "device": device,
        "model": info.get("model") or device,
        "serial": info.get("serial") or "",
        "years": years,
        "daily": daily,
        "has_data": bool(daily),
    }


def build_overview(app: App) -> dict:
    threshold = int(app.cfg.get("notify_health_below") or 60)
    disks, last_run, error = app.sampler.snapshot()
    items = [public_disk(info, threshold) for info in disks.values()]
    items.sort(key=lambda d: d["device"])

    conn = store.connect(app.cfg.db_path)
    try:
        keys = [d["key"] for d in items]
        sparks = store.daily_writes_map(conn, keys, days=30) if keys else {}
        for item in items:
            item["spark"] = sparks.get(item["key"]) or []
            item["life"] = life_estimate(conn, item["key"])
    finally:
        conn.close()

    abnormal = [d for d in items if d["status"] in ("warn", "bad")]
    total_capacity = sum(d["capacity"] or 0 for d in items)
    written = sum(d["tbw"] or 0 for d in items)
    read = sum(d["tbr"] or 0 for d in items)

    if not items:
        state, title, desc = "empty", "尚未识别到磁盘", "请确认设备已连接，或稍后刷新。"
    elif abnormal:
        state = "warn" if all(d["status"] == "warn" for d in abnormal) else "bad"
        title = f"发现 {len(abnormal)} 块硬盘异常"
        desc = "建议尽快备份重要数据，并检查硬盘健康状态。"
    else:
        state, title, desc = "ok", "运行顺利", "硬盘状态良好，未发现异常。"

    return {
        "generated_at": int(time.time()),
        "sample_interval": int(app.cfg.get("sample_interval") or 60),
        "last_sample_at": last_run,
        "sampler_error": error,
        "summary": {
            "state": state,
            "title": title,
            "desc": desc,
            "disk_count": len(items),
            "total_capacity": total_capacity,
            "total_written": written,
            "total_read": read,
            "abnormal": len(abnormal),
            "smart_supported": any(d["smart_ok"] for d in items),
        },
        "disks": items,
    }


# ------------------------------------------------------------------ 路由

def api_health(app: App, query: dict) -> dict:
    return {"status": "ok", "app": APP_NAME, "version": VERSION, "server_time": int(time.time())}


def api_overview(app: App, query: dict) -> dict:
    return build_overview(app)


def api_disks(app: App, query: dict) -> dict:
    data = build_overview(app)
    return {"items": data["disks"], "total": len(data["disks"])}


def api_history(app: App, query: dict) -> dict:
    return build_history(app, query)


def api_day(app: App, query: dict) -> dict:
    """某块盘某一天的采样明细，供热力图悬停展示日内曲线。"""
    device = str((query.get("device") or [""])[0])
    date = str((query.get("date") or [""])[0])
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        return {"error": "bad date", "total": 0, "points": []}
    disks, _, _ = app.sampler.snapshot()
    info = disks.get(device) or {}
    key = info.get("key") or ("dv:" + device)
    conn = store.connect(app.cfg.db_path)
    try:
        data = store.day_series(conn, key, date)
    finally:
        conn.close()
    data["date"] = date
    data["device"] = device
    data["model"] = info.get("model") or device
    return data


def api_settings(app: App, query: dict) -> dict:
    cfg = app.cfg
    return {
        "sample_interval": cfg.get("sample_interval"),
        "bench_size_mb": cfg.get("bench_size_mb"),
        "notify_health_below": cfg.get("notify_health_below"),
        "show_virtual_disks": bool(cfg.get("show_virtual_disks")),
        "only_ssd": bool(cfg.get("only_ssd")),
        "alert_enabled": bool(cfg.get("alert_enabled")),
        "alert_temp_high": cfg.get("alert_temp_high"),
        "alert_interval_seconds": cfg.get("alert_interval_seconds"),
        "backup_enabled": bool(cfg.get("backup_enabled")),
        "backup_dir": cfg.get("backup_dir") or os.path.join(cfg.data_dir, "backup"),
        "backup_interval_days": cfg.get("backup_interval_days"),
        "backup_keep": cfg.get("backup_keep"),
        "backup_last_at": cfg.get("backup_last_at") or 0,
        "backup_last_path": cfg.get("backup_last_path") or "",
        "data_dir": cfg.data_dir,
        "etc_dir": cfg.etc_dir,
        "smartctl": collector.find_smartctl(cfg.app_dir) or "",
    }


def api_about(app: App, query: dict) -> dict:
    return {
        "app": APP_NAME,
        "name": "SSD助手",
        "version": VERSION,
        "python": sys.version.split()[0],
        "data_dir": app.cfg.data_dir,
        "etc_dir": app.cfg.etc_dir,
        "db_path": app.cfg.db_path,
        "smartctl": collector.find_smartctl(app.cfg.app_dir) or "未找到",
        "disks": len(app.sampler.snapshot()[0]),
        "db_size": _db_size(app.cfg.db_path),
        "sample_count": _sample_count(app.cfg.db_path),
        "skipped": _skipped_disks(app),
    }


def _skipped_disks(app: App) -> list[dict]:
    """列出被"只显示固态"筛掉的盘。

    用户反馈过「系统里能看到、应用里没有」，靠这份清单能直接看出
    是判定条件把它当机械盘了，还是根本不在这台机器的 /sys/block 里。
    """
    out: list[dict] = []
    try:
        smartctl = collector.find_smartctl(app.cfg.app_dir)
        for name in collector.list_devices():
            info = collector.base_info(name)
            if info.get("protocol") == "NVMe" or info.get("is_ssd"):
                continue
            sm, _err = collector.smart_read(info["path"], smartctl)
            if sm:
                for key in ("model", "rotation_rate", "serial"):
                    if sm.get(key) is not None:
                        info[key] = sm[key]
                if sm.get("attributes"):
                    info["attributes"] = sm["attributes"]
            if collector.looks_like_ssd(info):
                continue
            out.append({
                "device": name,
                "model": info.get("model") or "",
                "rotation_rate": info.get("rotation_rate"),
                "rotational": info.get("rotational"),
            })
    except Exception:
        return []
    return out


def _db_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _sample_count(path: str) -> int:
    try:
        conn = store.connect(path)
    except Exception:
        return 0
    try:
        return store.count_samples(conn)
    except Exception:
        return 0
    finally:
        conn.close()


def api_bench_list(app: App, query: dict) -> dict:
    conn = store.connect(app.cfg.db_path)
    try:
        rows = store.list_bench(conn, 30)
    finally:
        conn.close()
    device = str((query.get("device") or [""])[0])
    plan = None
    if device and re.fullmatch(r"[a-zA-Z0-9]{1,16}", device):
        plan = bench_targets(device)
    return {"items": rows, "state": app.bench_state, "plan": plan}


def bench_targets(device: str) -> dict:
    """确定这块盘的测速目标：原始设备用于读，挂载卷用于写。"""
    dev_node = f"/dev/{device}"
    read_device = dev_node if os.path.exists(dev_node) and os.access(dev_node, os.R_OK) else None
    write_dir = collector.best_write_dir(device)
    return {"read_device": read_device, "write_dir": write_dir}


def _run_bench(app: App, device: str, size_mb: int, targets: dict) -> None:
    rounds: dict[str, list] = {"read": [], "write": []}

    def progress(stage: str, percent: int, extra: dict | None = None) -> None:
        app.bench_state.update(stage=stage, percent=percent)
        if not extra:
            return
        app.bench_state["live"] = extra
        value = extra.get("mbps")
        phase = extra.get("phase")
        if phase in rounds and isinstance(value, (int, float)):
            rounds[phase].append(value)
            # 前端据此按顺序播放指针，避免轮次太快被轮询漏掉
            app.bench_state["rounds"] = {k: list(v) for k, v in rounds.items()}

    try:
        result = bench.run(
            write_dir=targets.get("write_dir"),
            size_mb=size_mb,
            read_device=targets.get("read_device"),
            progress=progress,
        )
        disks, _, _ = app.sampler.snapshot()
        model = (disks.get(device) or {}).get("model") or device
        note = "读取:%s · 写入:%s" % (
            "设备直读" if result["read_source"] == "raw" else "测试文件",
            targets.get("write_dir") or "未测",
        )
        info = disks.get(device) or {}
        disk_key = info.get("key") or ("dv:" + device)

        # 入库单独兜底：写库失败也不能把已经测出来的数字丢掉，同时要把原因告诉前端
        save_error = ""
        try:
            conn = store.connect(app.cfg.db_path)
            try:
                store.add_bench(conn, device, model, result["read_mbps"],
                                result["write_mbps"], size_mb, note,
                                result.get("read_rounds"), result.get("write_rounds"),
                                disk_key=disk_key, serial=info.get("serial") or "")
            finally:
                conn.close()
        except Exception as exc:
            save_error = "测速结果写入数据库失败：" + str(exc)
            log(save_error, exc=True)

        app.bench_state.update(running=False, result=result, error=save_error or None,
                               stage=None, percent=100)
        log(f"测速完成 {device}: 读 {result['read_mbps']} MB/s, "
            f"写 {result['write_mbps']} MB/s（{note}）" + ("，但未存入历史" if save_error else ""))
    except Exception as exc:
        app.bench_state.update(running=False, result=None, error=str(exc), stage=None)
        log(f"测速失败: {exc}", exc=True)


def api_bench_start(app: App, body: dict, query: dict) -> dict:
    if app.bench_state.get("running"):
        return {"ok": False, "error": "已有测速任务在执行"}
    device = str((body or {}).get("device") or "")
    # 设备名来自请求，先做白名单校验（服务以较高权限运行，不能容忍拼接）
    if not re.fullmatch(r"[a-zA-Z0-9]{1,16}", device):
        return {"ok": False, "error": "设备名非法"}
    try:
        size_mb = int((body or {}).get("size_mb") or app.cfg.get("bench_size_mb") or 256)
    except (TypeError, ValueError):
        size_mb = int(app.cfg.get("bench_size_mb") or 256)
    disks, _, _ = app.sampler.snapshot()
    if device not in disks:
        return {"ok": False, "error": f"未找到磁盘 {device}"}

    targets = bench_targets(device)
    if not targets["read_device"] and not targets["write_dir"]:
        return {"ok": False, "error": "该磁盘既无法直接读取，也没有可写的挂载卷，无法测速"}

    app.bench_state = {
        "running": True, "started_at": int(time.time()), "result": None,
        "error": None, "stage": "准备中", "percent": 0,
        "targets": targets, "device": device,
    }
    threading.Thread(target=_run_bench, args=(app, device, size_mb, targets), daemon=True).start()
    return {"ok": True, "targets": targets}


def api_bench_clear(app: App, body: dict, query: dict) -> dict:
    conn = store.connect(app.cfg.db_path)
    try:
        store.clear_bench(conn)
    finally:
        conn.close()
    return {"ok": True}


def api_events_clear(app: App, body: dict, query: dict) -> dict:
    conn = store.connect(app.cfg.db_path)
    try:
        removed = store.clear_events(conn)
    finally:
        conn.close()
    log(f"已清空 {removed} 条事件记录")
    return {"ok": True, "removed": removed}


INT_KEYS = {
    "sample_interval": (10, 3600), "bench_size_mb": (64, 4096),
    "notify_health_below": (1, 100), "alert_temp_high": (30, 100),
    "alert_interval_seconds": (60, 604800), "backup_interval_days": (1, 30), "backup_keep": (1, 365),
}
BOOL_KEYS = ("alert_enabled", "backup_enabled", "show_virtual_disks", "only_ssd")
STR_KEYS = ("backup_dir",)


def api_settings_save(app: App, body: dict, query: dict) -> dict:
    body = body or {}
    patch: dict = {}
    for key, (low, high) in INT_KEYS.items():
        if key in body:
            try:
                patch[key] = max(low, min(high, int(body[key])))
            except (TypeError, ValueError):
                continue
    for key in BOOL_KEYS:
        if key in body:
            patch[key] = bool(body[key])
    for key in STR_KEYS:
        if key in body:
            patch[key] = str(body[key] or "").strip()[:500]
    if not patch:
        return {"ok": False, "error": "没有可更新的配置项"}
    app.cfg.save(patch)
    if "sample_interval" in patch and app.sampler is not None:
        app.sampler.interval = max(10, int(patch["sample_interval"]))
    if "alert_enabled" in patch:
        app.alerts.prev.clear()      # 重新建立基线，避免开关一开就刷旧告警
    return {"ok": True, "settings": api_settings(app, {})}


def api_export_json(app: App, query: dict) -> tuple[str, dict] | None:
    """导出单块硬盘的采样记录（按序列号归档，可直接回灌）。"""
    device = str((query.get("device") or [""])[0])
    if not re.fullmatch(r"[a-zA-Z0-9]{1,16}", device):
        return None
    key, info = resolve_disk(app, device)
    if not key:
        return None

    payload = backup.build_payload(app, key, device, info.get("model") or device, info)
    serial = payload["disk"].get("serial") or ""
    safe_serial = backup.safe_name(serial) or device
    filename = f"ssdplus_{safe_serial}_{device}_{time.strftime('%Y%m%d-%H%M')}.json"
    return filename, payload


def api_smart(app: App, query: dict) -> dict:
    """展开硬盘卡片时才拉取的原始 SMART 明细。"""
    device = str((query.get("device") or [""])[0])
    key, info = resolve_disk(app, device)
    if not key:
        return {"ok": False, "error": "未找到该磁盘"}
    return {
        "ok": True,
        "device": device,
        "key": key,
        "protocol": info.get("protocol") or "",
        "attributes": info.get("attributes") or [],
        "nvme_log": info.get("nvme_log") or {},
    }


def api_events(app: App, query: dict) -> dict:
    try:
        limit = min(200, max(1, int((query.get("limit") or ["50"])[0])))
    except ValueError:
        limit = 50
    conn = store.connect(app.cfg.db_path)
    try:
        rows = store.list_events(conn, limit)
        # 事件里只有设备名，补上型号，前端才能显示"硬盘名称"而不是 sda
        for row in rows:
            key = row.get("disk_key")
            if key and not row.get("model"):
                disk = conn.execute(
                    "SELECT model FROM disks WHERE disk_key = ? AND model IS NOT NULL"
                    " ORDER BY last_seen DESC LIMIT 1", (key,)).fetchone()
                row["model"] = (disk["model"] if disk else "") or ""
    finally:
        conn.close()
    return {"items": rows}


def api_import(app: App, body: dict, query: dict) -> dict:
    try:
        result = backup.import_payload(app, body)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    log(f"导入完成：新增 {result['added']} 条采样，涉及 {len(result['disks'])} 块盘")
    return {"ok": True, **result}


def api_backup_now(app: App, body: dict, query: dict) -> dict:
    try:
        info = backup.write_backup(app)
    except OSError as exc:
        return {"ok": False, "error": f"写入失败：{exc}"}
    log(f"手动备份：{info['path']}")
    return {"ok": True, **info}


def build_report_html(app: App, device: str) -> str | None:
    """单块硬盘的诊断报告（HTML，可直接另存或打印成 PDF）。"""
    key, info = resolve_disk(app, device)
    if not key:
        return None
    conn = store.connect(app.cfg.db_path)
    try:
        daily = store.daily_history(conn, [key], days=365)
        life = life_estimate(conn, key)
        benches = [row for row in store.list_bench(conn, 200) if row.get("disk_key") == key][:10]
    finally:
        conn.close()

    def size(value) -> str:
        if not value:
            return "—"
        units = ("B", "KB", "MB", "GB", "TB", "PB")
        idx = 0
        val = float(value)
        while val >= 1024 and idx < len(units) - 1:
            val /= 1024
            idx += 1
        return f"{val:.1f} {units[idx]}"

    rows = [
        ("设备名", device), ("型号", info.get("model") or "—"),
        ("序列号", info.get("serial") or "—"), ("固件", info.get("firmware") or "—"),
        ("接口", info.get("protocol") or "—"), ("容量", size(info.get("capacity"))),
        ("健康度", f"{info.get('health')}%" if info.get("health") is not None else "—"),
        ("当前温度", f"{info.get('temp')}℃" if info.get("temp") is not None else "—"),
        ("已用寿命", f"{info.get('percent_used')}%" if info.get("percent_used") is not None else "—"),
        ("累计写入", size(info.get("tbw"))), ("累计读取", size(info.get("tbr"))),
        ("通电时长", f"{info.get('power_on_hours')} 小时" if info.get("power_on_hours") else "—"),
        ("通电次数", info.get("power_cycles")), ("异常断电", info.get("unsafe_shutdowns")),
        ("介质错误", info.get("media_errors")),
    ]
    kv = "".join(f"<tr><th>{esc_html(k)}</th><td>{esc_html(str(v if v is not None else '—'))}</td></tr>"
                 for k, v in rows)

    total_write = sum(d["write"] for d in daily)
    total_read = sum(d["read"] for d in daily)
    peak = max([d["write"] for d in daily] + [1])
    bars = "".join(
        f'<div class="bar" title="{d["date"]} {size(d["write"])}">'
        f'<i style="height:{max(2, int(d["write"] / peak * 70))}px"></i></div>'
        for d in daily[-120:]
    ) or '<p class="muted">暂无采样数据</p>'

    bench_rows = "".join(
        f"<tr><td>{time.strftime('%Y-%m-%d %H:%M', time.localtime(b['ts']))}</td>"
        f"<td>{b.get('read_mbps') or '—'} MB/s</td><td>{b.get('write_mbps') or '—'} MB/s</td></tr>"
        for b in benches
    ) or '<tr><td colspan="3" class="muted">暂无测速记录</td></tr>'

    life_line = (
        f"按当前写入强度预计还可使用 <b>{esc_html(fmt_days(life['remaining_days']))}</b>"
        f"（每年消耗约 {life['per_year_pct']}% 寿命）"
        if life.get("status") == "ok"
        else esc_html(life.get("note") or "数据不足，暂无法预估")
    )

    return f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>SSD助手 诊断报告 - {esc_html(device)}</title>
<style>
body{{margin:0;padding:40px;background:#f3f4f6;color:#1f2329;
 font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;font-size:13px}}
.wrap{{max-width:900px;margin:0 auto;background:#fff;border:1px solid #e6e9ee;border-radius:12px;padding:28px 32px}}
h1{{font-size:18px;margin:0 0 4px}} .sub{{color:#8a9099;font-size:12px;margin-bottom:20px}}
h2{{font-size:13px;margin:26px 0 10px;color:#4b5563}}
table{{width:100%;border-collapse:collapse}}
th,td{{text-align:left;padding:7px 10px;border-bottom:1px solid #eef1f5;vertical-align:top}}
th{{width:110px;color:#8a9099;font-weight:400}}
.note{{background:#f7f9fc;border-radius:8px;padding:12px 14px;color:#3d4652;margin-top:6px}}
.bars{{display:flex;align-items:flex-end;gap:2px;height:76px;margin-top:6px}}
.bar{{flex:1;display:flex;align-items:flex-end}}
.bar i{{display:block;width:100%;background:#7fd8a8;border-radius:2px 2px 0 0}}
.muted{{color:#a5adb8}}
</style></head><body><div class="wrap">
<h1>SSD助手 · 硬盘诊断报告</h1>
<div class="sub">生成时间 {time.strftime('%Y-%m-%d %H:%M:%S')} · 应用版本 {VERSION}</div>
<h2>基本信息</h2><table>{kv}</table>
<h2>寿命预估</h2><div class="note">{life_line}</div>
<h2>近一年写入量</h2><div class="bars">{bars}</div>
<p class="muted">累计写入 {esc_html(size(total_write))} · 累计读取 {esc_html(size(total_read))} · 共 {len(daily)} 天有记录</p>
<h2>最近测速</h2><table><tr><th style="width:150px">时间</th><th>读取</th><th>写入</th></tr>{bench_rows}</table>
<p class="muted" style="margin-top:24px">由 SSD助手 自动生成，数据来源为该盘 SMART 与本地采样记录。</p>
</div></body></html>"""


def esc_html(text: str) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


GET_ROUTES = {
    "/api/health": api_health,
    "/api/overview": api_overview,
    "/api/disks": api_disks,
    "/api/history": api_history,
    "/api/settings": api_settings,
    "/api/about": api_about,
    "/api/bench": api_bench_list,
    "/api/smart": api_smart,
    "/api/events": api_events,
    "/api/day": api_day,
}

POST_ROUTES = {
    "/api/bench/start": api_bench_start,
    "/api/bench/clear": api_bench_clear,
    "/api/events/clear": api_events_clear,
    "/api/settings": api_settings_save,
    "/api/import": api_import,
    "/api/backup/now": api_backup_now,
}


# ------------------------------------------------------------------ HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = f"{APP_NAME}/{VERSION}"
    app: App

    def address_string(self):
        # Unix Socket 模式下 client_address 为空字符串，父类会索引越界
        try:
            return self.client_address[0]
        except (TypeError, IndexError):
            return "unix"

    def log_message(self, fmt, *args):  # noqa: A003
        log("%s - %s" % (self.address_string(), fmt % args))

    def _gateway_path(self, parsed):
        """剥掉网关前缀，返回 (路径, 需要重定向到的地址)。"""
        prefix = self.app.cfg.prefix
        path = parsed.path
        if not prefix:
            return path, None
        if path == prefix:
            target = prefix + "/"
            if parsed.query:
                target += "?" + parsed.query
            return path, target
        if path.startswith(prefix + "/"):
            return path[len(prefix):] or "/", None
        return path, None

    def _base_href(self) -> str:
        prefix = self.app.cfg.prefix
        return (prefix + "/") if prefix else "/"

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: str) -> None:
        root = self.app.cfg.www_dir
        target = os.path.abspath(os.path.join(root, path.lstrip("/")))
        if not target.startswith(root + os.sep) and target != root:
            self._json({"error": "forbidden"}, 403)
            return
        if os.path.isdir(target):
            target = os.path.join(target, "index.html")
        if not os.path.isfile(target):
            # 浏览器/WebView 里 JSON 常常直接显示成空白，这里改成可读的 HTML
            self._html_error(target, path)
            return

        ext = os.path.splitext(target)[1].lower()
        with open(target, "rb") as fp:
            body = fp.read()
        if ext in (".html", ".htm"):
            body = self._inject_base(body)
        self.send_response(200)
        self.send_header("Content-Type", MIME_TYPES.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _inject_base(self, body: bytes) -> bytes:
        """给 HTML 注入 <base>，前端全部用相对地址，直连与网关两种模式都能跑。"""
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            return body
        if "<base " in text.lower():
            return body
        tag = '<base href="%s">' % self._base_href()
        m = re.search(r"<head[^>]*>", text, re.IGNORECASE)
        if m:
            text = text[:m.end()] + tag + text[m.end():]
        else:
            text = tag + text
        return text.encode("utf-8")

    def _html_error(self, target: str, path: str) -> None:
        body = (
            "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
            "<title>SSD助手</title><style>"
            "body{margin:0;padding:48px;font-family:-apple-system,'PingFang SC',sans-serif;"
            "background:#f3f4f6;color:#1f2329}"
            "h1{font-size:16px;margin:0 0 8px}p{color:#6b7684;font-size:13px;line-height:1.8;margin:4px 0}"
            "code{background:#fff;border:1px solid #e5e8ec;border-radius:4px;padding:1px 6px;font-size:12px}"
            "</style></head><body>"
            "<h1>界面文件缺失</h1>"
            "<p>请求的页面不存在：<code>{path}</code></p>"
            "<p>找不到文件：<code>{target}</code></p>"
            "<p>请卸载后重新安装应用；如果仍然出现，请查看日志 "
            "<code>{log}</code>。</p>"
            "</body></html>"
        ).format(path=path, target=target, log=self.app.cfg.data_dir + "/info.log")
        data = body.encode("utf-8")
        self.send_response(404)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _download_json(self, filename: str, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _download_html(self, filename: str, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        path, redirect = self._gateway_path(parsed)
        if redirect:
            self.send_response(301)
            self.send_header("Location", redirect)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if path == "/api/export":
            try:
                result = api_export_json(self.app, query)
            except Exception:
                log("导出失败", exc=True)
                self._json({"error": "export failed"}, 500)
                return
            if not result:
                self._json({"error": "未找到该磁盘的采样数据"}, 404)
                return
            self._download_json(*result)
            return

        if path == "/api/report":
            device = str((query.get("device") or [""])[0])
            if not re.fullmatch(r"[a-zA-Z0-9]{1,16}", device):
                self._json({"error": "设备名非法"}, 400)
                return
            html = build_report_html(self.app, device)
            if not html:
                self._json({"error": "未找到该磁盘"}, 404)
                return
            self._download_html(
                f"ssdplus-report-{device}-{time.strftime('%Y%m%d-%H%M')}.html", html)
            return

        handler = GET_ROUTES.get(path)
        if handler:
            try:
                self._json(handler(self.app, query))
            except Exception:
                log(f"接口异常: {path}", exc=True)
                self._json({"error": "internal error"}, 500)
            return
        if path.startswith("/api/"):
            self._json({"error": "not found", "path": path}, 404)
            return
        self._file("/index.html" if path == "/" else path)

    def do_POST(self):  # noqa: N802
        parsed = urlparse(self.path)
        path, redirect = self._gateway_path(parsed)
        if redirect:
            self._json({"error": "use trailing slash", "path": redirect}, 404)
            return
        handler = POST_ROUTES.get(path)
        if not handler:
            self._json({"error": "not found", "path": path}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length > 64 * 1024 * 1024:
                self._json({"ok": False, "error": "文件过大（上限 64MB）"}, 413)
                return
            body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except Exception:
            body = {}
        try:
            self._json(handler(self.app, body, parse_qs(parsed.query)))
        except Exception:
            log(f"接口异常: {path}", exc=True)
            self._json({"error": "internal error"}, 500)


# ------------------------------------------------------------------ 启动

def _env_port(default: int = 8901) -> int:
    """未声明 service_port 时飞牛会注入空字符串，这里兜底。"""
    raw = (os.environ.get("TRIM_SERVICE_PORT") or "").strip()
    try:
        return int(raw)
    except ValueError:
        return default


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="SSD助手 后端服务")
    base = os.path.dirname(os.path.abspath(__file__))
    parser.add_argument("--port", type=int, default=_env_port())
    parser.add_argument("--socket", default=os.environ.get("SSDPLUS_SOCKET", ""),
                        help="Unix Socket 路径；设置后走飞牛网关模式，不再监听 TCP 端口")
    parser.add_argument("--prefix", default=os.environ.get("SSDPLUS_PREFIX", ""),
                        help="网关前缀，例如 /app/ssdplus")
    parser.add_argument("--www", default=os.path.join(base, "..", "www"))
    parser.add_argument("--data", default=os.path.join(base, "..", "..", "var"))
    parser.add_argument("--etc", default=os.path.join(base, "..", "..", "etc"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    cfg = Config(args.port, args.www, args.data, args.etc, args.socket, args.prefix)
    for d in (cfg.data_dir, cfg.etc_dir):
        os.makedirs(d, exist_ok=True)
    store.init_db(cfg.db_path)

    app = App(cfg)
    def on_tick(disks: dict, ts: int) -> None:
        try:
            app.alerts.evaluate(app, disks, ts, logger=log)
        except Exception:
            log("告警检查失败", exc=True)
        try:
            done = backup.maybe_backup(app)
            if done:
                log(f"自动备份完成：{done['path']}（{done['disk_count']} 块盘，"
                    f"{done['bytes'] // 1024} KB，清理旧备份 {done['removed']} 份）")
        except Exception:
            log("自动备份失败", exc=True)

    sampler = sampler_mod.Sampler(
        cfg.app_dir, cfg.db_path,
        interval=int(cfg.get("sample_interval") or 60),
        retention_days=0,          # 数据永久保留，不做任何清理
        include_virtual=bool(cfg.get("show_virtual_disks")),
        only_ssd=bool(cfg.get("only_ssd")),
        on_tick=on_tick,
    )
    app.sampler = sampler
    sampler.start()

    index_file = os.path.join(cfg.www_dir, "index.html")
    if not os.path.isfile(index_file):
        log(f"警告：前端文件缺失 {index_file}")
        if os.environ.get("TRIM_TEMP_LOGFILE"):
            try:
                with open(os.environ["TRIM_TEMP_LOGFILE"], "a", encoding="utf-8") as fp:
                    fp.write(f"前端文件缺失：{index_file}\n请重新安装应用。\n")
            except OSError:
                pass
    log(f"采样线程已启动，间隔 {sampler.interval}s")

    Handler.app = app
    socket_path = ""

    if cfg.socket_path:
        # 飞牛统一网关模式：只监听 Unix Socket，不占任何 TCP 端口
        os.makedirs(os.path.dirname(cfg.socket_path) or ".", exist_ok=True)
        try:
            os.unlink(cfg.socket_path)
        except FileNotFoundError:
            pass

        class _UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
            daemon_threads = True

        server = _UnixServer(cfg.socket_path, Handler)
        try:
            os.chmod(cfg.socket_path, 0o770)
        except OSError:
            log("设置 socket 权限失败", exc=True)
        socket_path = cfg.socket_path
        addr = f"unix:{cfg.socket_path}  prefix={cfg.prefix or '-'}"
    else:
        server = ThreadingHTTPServer(("0.0.0.0", cfg.port), Handler)
        server.daemon_threads = True
        addr = f"http://0.0.0.0:{cfg.port}"

    def shutdown(*_args):
        log("收到退出信号，正在停止")
        sampler.stop()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    log(f"服务已启动: {addr}  www={cfg.www_dir}  data={cfg.data_dir}")
    try:
        server.serve_forever()
    finally:
        sampler.stop()
        server.server_close()
        if socket_path:
            try:
                os.unlink(socket_path)
            except OSError:
                pass
        log("服务已退出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
