# -*- coding: utf-8 -*-
"""异常告警：规则判定 + 事件落库（仅站内事件，暂不做 Webhook 外发）。

设计要点：
  * 只在"变化"时提醒，同一块盘的同一规则在通知频率内不重复提醒；
  * 首次启动以当前状态作为基准，不会把历史遗留问题一股脑推给用户。
"""

from __future__ import annotations

import store

DEFAULT_INTERVAL_SECONDS = 600

RULES = {
    "health_low": "健康度偏低",
    "temp_high": "温度过高",
    "media_errors": "介质错误增加",
    "unsafe_shutdown": "异常断电增加",
    "spare_low": "备用块不足",
    "critical_warning": "关键告警位",
    "smart_lost": "SMART 不可读",
}


class AlertEngine:
    def __init__(self):
        self.prev: dict[str, dict] = {}

    def evaluate(self, app, disks: dict[str, dict], ts: int, logger=None) -> list[dict]:
        cfg = app.cfg
        if not bool(cfg.get("alert_enabled")):
            self.prev = {info.get("key"): _snapshot(info) for info in disks.values()}
            return []

        # 与状态着色共用同一个阈值，避免用户在两处配置不同数值
        health_below = int(cfg.get("notify_health_below") or 90)
        temp_high = int(cfg.get("alert_temp_high") or 60)
        interval = max(60, int(cfg.get("alert_interval_seconds") or DEFAULT_INTERVAL_SECONDS))

        fired: list[dict] = []
        conn = store.connect(cfg.db_path)
        try:
            for device, info in disks.items():
                key = info.get("key") or ("dv:" + device)
                prev = self.prev.get(key)
                current = _snapshot(info)
                if prev is None:
                    self.prev[key] = current
                    continue

                for rule, level, message in self._checks(info, prev, current, health_below, temp_high):
                    if _cooling_down(conn, key, rule, ts, interval):
                        continue
                    store.add_event(conn, key, device, level, rule, message)
                    fired.append({
                        "disk_key": key, "device": device, "level": level,
                        "rule": rule, "message": message,
                        "model": info.get("model") or device,
                        "serial": info.get("serial") or "",
                    })
                self.prev[key] = current
        finally:
            conn.close()

        for item in fired:
            if logger:
                logger(f"告警 [{item['level']}] {item['device']}: {item['message']}")
        return fired

    # ---------------------------------------------------------- 规则
    def _checks(self, info: dict, prev: dict, cur: dict,
                health_below: int, temp_high: int) -> list[tuple[str, str, str]]:
        out: list[tuple[str, str, str]] = []
        health = cur.get("health")
        if isinstance(health, int) and health < health_below:
            level = "bad" if health < max(20, health_below // 2) else "warn"
            out.append(("health_low", level, f"健康度 {health}%，已低于阈值 {health_below}%"))

        temp = cur.get("temp")
        if isinstance(temp, int) and temp >= temp_high:
            level = "bad" if temp >= temp_high + 10 else "warn"
            out.append(("temp_high", level, f"温度 {temp}℃，超过阈值 {temp_high}℃"))

        media = cur.get("media_errors")
        prev_media = prev.get("media_errors")
        if isinstance(media, int) and isinstance(prev_media, int) and media > prev_media:
            out.append(("media_errors", "warn", f"介质错误从 {prev_media} 增加到 {media}"))

        shutdowns = cur.get("unsafe_shutdowns")
        prev_shutdowns = prev.get("unsafe_shutdowns")
        if isinstance(shutdowns, int) and isinstance(prev_shutdowns, int) and shutdowns > prev_shutdowns:
            out.append(("unsafe_shutdown", "info",
                        f"异常断电次数从 {prev_shutdowns} 增加到 {shutdowns}"))

        spare, spare_thresh = cur.get("available_spare"), cur.get("available_spare_threshold")
        if isinstance(spare, int) and isinstance(spare_thresh, int) and spare < spare_thresh:
            out.append(("spare_low", "bad", f"可用备用块 {spare}%，低于阈值 {spare_thresh}%"))

        warn = cur.get("critical_warning")
        if isinstance(warn, int) and warn != 0:
            out.append(("critical_warning", "bad", f"SMART 关键告警位 0x{warn:02x}"))

        if prev.get("smart_ok") and not cur.get("smart_ok"):
            out.append(("smart_lost", "warn", "SMART 信息读取失败，可能是接触不良或硬盘异常"))
        return out


def _snapshot(info: dict) -> dict:
    return {
        "health": info.get("health"),
        "temp": info.get("temp"),
        "media_errors": info.get("media_errors"),
        "unsafe_shutdowns": info.get("unsafe_shutdowns"),
        "available_spare": info.get("available_spare"),
        "available_spare_threshold": info.get("available_spare_threshold"),
        "critical_warning": info.get("critical_warning"),
        "smart_ok": bool(info.get("smart_ok")),
    }


def _cooling_down(conn, disk_key: str, rule: str, ts: int, interval: int) -> bool:
    last = store.last_event_ts(conn, disk_key, rule)
    return last is not None and ts - last < interval
