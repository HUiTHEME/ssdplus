# -*- coding: utf-8 -*-
"""顺序读写测速。

要点（都是踩过坑换来的）：

1. 写入缓冲区必须填**随机数据**。用全零缓冲区时，ext4/btrfs 会把整块识别成
   空洞（sparse），文件不占真实空间，读回来直接由内存补零，能测出 15 GB/s
   这种不可能的速度。
2. 读要读**选中的那块盘**。优先直接顺序读 /dev/sdX（O_DIRECT、只读、安全），
   这样即使磁盘没挂载、或者只是阵列成员，也能拿到这块盘的原始顺序读速度；
   设备不可读时退回读文件。
3. 写只能在文件系统上做，所以写入测试落在该磁盘承载的挂载卷上；没有挂载卷
   时明确返回“未测”，而不是拿别的盘凑数。
4. 用 /proc/self/io 的 read_bytes 校验每轮到底有没有真的从磁盘读数据，
   发现命中缓存就清一次页缓存重测。
5. 读、写各跑 3 轮，每轮之间间隔 2 秒，各取最高值作为成绩（更接近盘的峰值能力，
   也不被偶发的一次慢速拖低），并返回每轮明细给前端展示。
"""

from __future__ import annotations

import mmap
import os
import random
import shutil
import threading
import time

BLOCK = 8 * 1024 * 1024          # 单次 IO 大小
ROUNDS = 3                        # 读写各测 3 轮
ROUND_SECONDS = 10.0              # 每轮实测 10 秒：数据量足够，结果才稳
ROUND_GAP = 2.0                   # 每轮之间停 2 秒（前端此时把指针归零）
TMP_NAME = ".ssdplus_bench.tmp"
_lock = threading.Lock()


# ------------------------------------------------------------------ 工具函数

def _aligned_buffer(size: int) -> mmap.mmap:
    """mmap 匿名映射按页对齐，满足 O_DIRECT 的对齐要求。"""
    return mmap.mmap(-1, size)


def _fill_random(buf: mmap.mmap) -> None:
    """填入不可压缩的随机数据，避免被当成空洞或被文件系统压缩。"""
    view = memoryview(buf)
    step = 1024 * 1024
    for off in range(0, len(view), step):
        chunk = os.urandom(min(step, len(view) - off))
        view[off:off + len(chunk)] = chunk
    view.release()


def _io_bytes(key: str) -> int | None:
    """读取 /proc/self/io 中的 read_bytes / write_bytes（真实落到块层的字节数）。"""
    try:
        with open("/proc/self/io", "r", encoding="ascii") as fp:
            for line in fp:
                name, _, value = line.partition(":")
                if name.strip() == key:
                    return int(value.strip())
    except (OSError, ValueError):
        pass
    return None


def _drop_page_cache() -> bool:
    """清空页缓存（只清 page cache）。需要 root，失败不影响主流程。"""
    try:
        with open("/proc/sys/vm/drop_caches", "w", encoding="ascii") as fp:
            fp.write("1")
        return True
    except OSError:
        return False


def _fadvise_dontneed(path: str) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
        finally:
            os.close(fd)
    except (OSError, AttributeError):
        pass


def _unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _open_direct(path: str, flags: int) -> tuple[int, bool]:
    """优先 O_DIRECT，失败则退化为普通打开。返回 (fd, 是否绕过缓存)。"""
    try:
        return os.open(path, flags | os.O_DIRECT, 0o600), True
    except OSError:
        return os.open(path, flags, 0o600), False


def _device_size(path: str) -> int:
    """块设备字节数，失败返回 0。"""
    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            return os.lseek(fd, 0, os.SEEK_END)
        finally:
            os.close(fd)
    except OSError:
        return 0


# ------------------------------------------------------------------ 单轮测试

def _write_round(path: str, block: mmap.mmap, total: int,
                 seconds: float = ROUND_SECONDS, tick=None) -> dict:
    """在 seconds 秒内持续写入（写满就从头覆盖），统计平均速度。"""
    _unlink(path)
    fd, direct = _open_direct(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
    written = 0
    before = _io_bytes("write_bytes")
    start = time.monotonic()
    deadline = start + max(0.2, seconds)
    last_tick, last_bytes = start, 0
    try:
        view = memoryview(block)
        length = len(view)
        while True:
            os.lseek(fd, 0, os.SEEK_SET)
            sent = 0
            while sent < total:
                chunk = min(length, total - sent)
                n = os.write(fd, view[:chunk])
                if n <= 0:
                    break
                sent += n
                written += n
                if tick:
                    now = time.monotonic()
                    if now - last_tick >= 1.0:
                        tick((written - last_bytes) / 1048576 / (now - last_tick))
                        last_tick, last_bytes = now, written
            if not direct:
                os.fsync(fd)          # 每圈落盘，否则测的是页缓存
            if time.monotonic() >= deadline or sent <= 0:
                break
        elapsed = time.monotonic() - start
    finally:
        os.close(fd)
    after = _io_bytes("write_bytes")
    real = (after - before) if (before is not None and after is not None) else None
    return {
        "seconds": elapsed,
        "mb": written / (1024 * 1024),
        "mbps": (written / (1024 * 1024)) / elapsed if elapsed > 0 else 0.0,
        "real_io": real,
        "direct": direct,
    }


def _read_round(path: str, block: mmap.mmap, total: int, offset: int = 0,
                span: int = 0, seconds: float = ROUND_SECONDS, tick=None) -> dict:
    """在 seconds 秒内持续读取（每圈换个位置，避免反复命中缓存）。"""
    _fadvise_dontneed(path)
    fd, direct = _open_direct(path, os.O_RDONLY)
    done = 0
    before = _io_bytes("read_bytes")
    start = time.monotonic()
    deadline = start + max(0.2, seconds)
    last_tick, last_bytes = start, 0
    try:
        view = memoryview(block)
        length = len(view)
        lap = 0
        while True:
            pos = offset + ((lap * total) % span if span > 0 else 0)
            sent = 0
            while sent < total:
                want = min(length, total - sent)
                got = os.preadv(fd, [view[:want]], pos)
                if got <= 0:
                    break
                sent += got
                pos += got
                done += got
                if tick:
                    now = time.monotonic()
                    if now - last_tick >= 1.0:
                        tick((done - last_bytes) / 1048576 / (now - last_tick))
                        last_tick, last_bytes = now, done
            lap += 1
            if sent <= 0 or time.monotonic() >= deadline:
                break
        elapsed = time.monotonic() - start
    finally:
        os.close(fd)
    after = _io_bytes("read_bytes")
    real = (after - before) if (before is not None and after is not None) else None
    return {
        "seconds": elapsed,
        "mb": done / (1024 * 1024),
        "mbps": (done / (1024 * 1024)) / elapsed if elapsed > 0 else 0.0,
        "real_io": real,
        "direct": direct,
    }


def _is_cached(round_result: dict) -> bool:
    """这一轮是否有明显比例的数据来自页缓存。"""
    real = round_result.get("real_io")
    if real is None:
        return False
    total = round_result["mb"] * 1024 * 1024
    return total > 0 and real < total * 0.8


def _peak(values: list[float]) -> float:
    """取最好的一轮：SSD 的标称速度本就指峰值，平均值会被一次抖动拖低。"""
    return round(max(values), 1) if values else 0.0


# ------------------------------------------------------------------ 对外入口

def run(write_dir: str | None, size_mb: int = 256,
        read_device: str | None = None, progress=None) -> dict:
    """执行一次完整测速：读 3 轮 + 写 3 轮（轮间 2 秒），各取最高值。

    write_dir:    写入测试目录（该磁盘承载的挂载点）；None 表示无法测写入
    read_device:  读取测试的块设备（如 /dev/sdc）；None 则读 write_dir 里的文件
    progress:     可选回调 fn(stage: str, percent: int)
    """
    def report(stage: str, percent: int, extra: dict | None = None) -> None:
        if progress:
            try:
                progress(stage, percent, extra)
            except TypeError:
                try:
                    progress(stage, percent)
                except Exception:
                    pass
            except Exception:
                pass

    if not _lock.acquire(blocking=False):
        raise RuntimeError("已有测速任务在执行")

    size_mb = max(64, min(int(size_mb), 4096))
    total = size_mb * 1024 * 1024
    path = os.path.join(write_dir, TMP_NAME) if write_dir else None

    try:
        if write_dir:
            os.makedirs(write_dir, exist_ok=True)
            free = shutil.disk_usage(write_dir).free
            if free < total * 1.5:
                write_dir, path = None, None   # 空间不够就只做读取测试
        if not write_dir and not read_device:
            raise RuntimeError("该磁盘既没有可写挂载点，也无法直接读取设备")

        # 实际读多少：设备太小时按设备容量的 80% 截断
        read_total = total
        read_offset = 0
        read_span = 0
        if read_device:
            dev_size = _device_size(read_device)
            if dev_size <= 0:
                read_device = None
            else:
                read_total = min(total, max(16 * 1024 * 1024, int(dev_size * 0.8)))
                read_total = read_total // 4096 * 4096
                # 从中段开始读，避开总是读最前面扇区（SSD 自带缓存会失真）
                read_span = max(0, dev_size - read_total)
                read_offset = (random.randint(0, read_span // 4096) * 4096) if read_span > 0 else 0

        block = _aligned_buffer(min(BLOCK, total))
        try:
            report("准备测试数据", 3)
            _fill_random(block)

            # 预热：第一轮总是偏慢（设备唤醒、USB 桥接、文件系统首次分配），
            # 这一轮不计入成绩，避免把冷启动混进平均值。
            read_path = read_device
            if not read_path:
                _write_round(path, block, total, 1.0)
                read_path = path
            report("预热中", 3)
            _read_round(read_path, block, read_total, read_offset,
                        span=read_span, seconds=1.0)

            # ---------------- 读：3 轮（先读后写，和常规测速工具一致） ----------------
            reads: list[dict] = []
            retried = 0
            for i in range(ROUNDS):
                pct = 8 + i * 14
                if i:
                    report(f"读取测试 {i + 1}/{ROUNDS}", pct,
                           {"phase": "read", "round": i + 1, "gap": True})
                    time.sleep(ROUND_GAP)      # 轮间停 2 秒，指针归零后再测
                report(f"读取测试 {i + 1}/{ROUNDS}", pct,
                       {"phase": "read", "round": i + 1})
                result = _read_round(
                    read_path, block, read_total, read_offset,
                    span=read_span, seconds=ROUND_SECONDS,
                    tick=lambda v, n=i: report(
                        f"读取测试 {n + 1}/{ROUNDS}", pct,
                        {"phase": "read", "round": n + 1, "rate": round(v, 1)}))
                # 命中缓存的结果不可信：清一次页缓存重测（仅 root 有效）
                if _is_cached(result) and retried < ROUNDS:
                    retried += 1
                    if _drop_page_cache():
                        time.sleep(0.2)
                        retry = _read_round(read_path, block, read_total, read_offset)
                        if not _is_cached(retry):
                            result = retry
                reads.append(result)
                report(f"读取测试 {i + 1}/{ROUNDS}", 8 + i * 14,
                       {"phase": "read", "round": i + 1, "mbps": round(result["mbps"], 1)})

            # ---------------- 写：3 轮 ----------------
            writes: list[dict] = []
            if write_dir:
                _write_round(path, block, total, 1.0)   # 写入预热，不计入成绩
                for i in range(ROUNDS):
                    pct = 52 + i * 15
                    if i:
                        report(f"写入测试 {i + 1}/{ROUNDS}", pct,
                               {"phase": "write", "round": i + 1, "gap": True})
                        time.sleep(ROUND_GAP)
                    report(f"写入测试 {i + 1}/{ROUNDS}", pct,
                           {"phase": "write", "round": i + 1})
                    done = _write_round(
                        path, block, total, ROUND_SECONDS,
                        tick=lambda v, n=i: report(
                            f"写入测试 {n + 1}/{ROUNDS}", pct,
                            {"phase": "write", "round": n + 1, "rate": round(v, 1)}))
                    writes.append(done)
                    report(f"写入测试 {i + 1}/{ROUNDS}", 52 + i * 15,
                           {"phase": "write", "round": i + 1, "mbps": round(done["mbps"], 1)})
            else:
                report("跳过写入测试（该磁盘未挂载）", 96)
        finally:
            block.close()

        report("整理结果", 97)

        cached = any(_is_cached(r) for r in reads)
        return {
            "read_mbps": _peak([r["mbps"] for r in reads]),
            "write_mbps": _peak([w["mbps"] for w in writes]) if writes else None,
            "size_mb": size_mb,
            "rounds": ROUNDS,
            "warmup": True,
            "read_rounds": [round(r["mbps"], 1) for r in reads],
            "write_rounds": [round(w["mbps"], 1) for w in writes],
            "read_seconds": [round(r["seconds"], 2) for r in reads],
            "write_seconds": [round(w["seconds"], 2) for w in writes],
            "read_source": "raw" if read_device else "file",
            "read_bytes_mb": round(read_total / (1024 * 1024)),
            "cached": cached,
            "cache_retries": retried,
            "direct": all(r["direct"] for r in reads) and all(w["direct"] for w in writes),
        }
    finally:
        if path:
            _unlink(path)
        _lock.release()
