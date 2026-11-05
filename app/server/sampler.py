# -*- coding: utf-8 -*-
"""后台采样线程：定期扫描磁盘、写库，并维护内存中的最新快照。"""

from __future__ import annotations

import threading
import time
import traceback

import collector
import store


class Sampler(threading.Thread):
    def __init__(self, app_dir: str, db_path: str, interval: int = 60, retention_days: int = 0,
                 include_virtual: bool = False, only_ssd: bool = True, on_tick=None):
        super().__init__(daemon=True, name="ssdplus-sampler")
        self.app_dir = app_dir
        self.db_path = db_path
        self.interval = max(10, int(interval))
        self.retention_days = retention_days
        self.include_virtual = include_virtual
        self.only_ssd = only_ssd
        self.on_tick = on_tick          # 每轮采样完成后的回调（告警、备份等）
        self.latest: dict[str, dict] = {}
        self.last_run: int | None = None
        self.last_error: str | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._pruned_at = 0.0

    def snapshot(self) -> tuple[dict[str, dict], int | None, str | None]:
        with self._lock:
            return dict(self.latest), self.last_run, self.last_error

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                self.last_error = traceback.format_exc(limit=2)
            self._stop.wait(self.interval)

    def tick(self) -> None:
        started = int(time.time())
        try:
            disks = collector.snapshot(self.app_dir, self.include_virtual, self.only_ssd)
        except Exception:
            self.last_error = traceback.format_exc(limit=2)
            return

        conn = store.connect(self.db_path)
        try:
            for device, info in disks.items():
                key = info.get("key") or ("dv:" + device)
                store.upsert_disk(conn, info, started)
                acc_read, acc_write, read_rate, write_rate = store.update_counter(
                    conn, key, device,
                    info.get("session_read", 0), info.get("session_write", 0), started,
                )
                info["acc_read"] = acc_read
                info["acc_write"] = acc_write
                info["read_rate"] = read_rate
                info["write_rate"] = write_rate
                store.add_sample(conn, info, started, acc_read, acc_write, read_rate, write_rate)

            now = time.time()
            # retention_days <= 0 表示永久保留，不做任何清理
            if self.retention_days > 0 and now - self._pruned_at > 3600:
                store.prune(conn, self.retention_days)
                self._pruned_at = now
        finally:
            conn.close()

        with self._lock:
            self.latest = disks
            self.last_run = started
            self.last_error = None

        if self.on_tick:
            try:
                self.on_tick(disks, started)
            except Exception:
                # 告警/备份失败不能影响采样
                pass
