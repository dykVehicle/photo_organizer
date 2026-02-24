"""磁盘扫描模块：二级展开 + 多线程并行扫描（照片+视频）"""

import os
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed, wait, FIRST_COMPLETED
from typing import Dict, List, Optional, Set, Tuple

from tqdm import tqdm

from interrupt import is_interrupted
from config import (
    IMAGE_EXTENSIONS,
    VIDEO_EXTENSIONS,
    MEDIA_EXTENSIONS,
    EXCLUDED_DIRS,
    DEFAULT_WORKERS,
    get_all_drives,
    get_excluded_drive_letters,
)

logger = logging.getLogger(__name__)


def _interruptible_as_completed(futures, timeout_per_poll=1.0):
    pending = set(futures)
    while pending:
        if is_interrupted():
            for f in pending:
                f.cancel()
            return
        done, pending = wait(pending, timeout=timeout_per_poll, return_when=FIRST_COMPLETED)
        yield from done


class _ScanStats:
    """线程安全的扫描统计"""

    def __init__(self):
        self._lock = threading.Lock()
        self.dirs_scanned = 0
        self.dirs_skipped_rule = 0
        self.dirs_skipped_perm = 0
        self.photo_count = 0
        self.video_count = 0

    def add_dir(self):
        with self._lock:
            self.dirs_scanned += 1

    def add_skipped_rule(self):
        with self._lock:
            self.dirs_skipped_rule += 1

    def add_skipped_perm(self):
        with self._lock:
            self.dirs_skipped_perm += 1

    def add_file(self, ext: str):
        with self._lock:
            if ext in VIDEO_EXTENSIONS:
                self.video_count += 1
            else:
                self.photo_count += 1

    def summary(self) -> str:
        total = self.photo_count + self.video_count
        return (
            f"扫描统计: {self.dirs_scanned} 个目录, "
            f"跳过 {self.dirs_skipped_rule}(排除规则) + {self.dirs_skipped_perm}(权限), "
            f"发现 {total} 个媒体文件 (照片 {self.photo_count}, 视频 {self.video_count})"
        )


def _should_skip_dir(
    dirname: str,
    no_exclude: bool = False,
    include_hidden: bool = False,
) -> bool:
    if no_exclude:
        return False
    lower = dirname.lower()
    if lower in EXCLUDED_DIRS:
        return True
    if not include_hidden and lower[0:1] in (".", "$"):
        return True
    return False


def _scan_root_files(
    root: str,
    no_exclude: bool = False,
    include_hidden: bool = False,
) -> Tuple[List[str], List[str]]:
    """扫描根目录直属文件（不递归），同时返回一级子目录列表。"""
    root_media: List[str] = []
    subdirs: List[str] = []
    try:
        with os.scandir(root) as entries:
            for entry in entries:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if not _should_skip_dir(entry.name, no_exclude, include_hidden):
                            subdirs.append(entry.path)
                    elif entry.is_file(follow_symlinks=False):
                        ext = os.path.splitext(entry.name)[1].lower()
                        if ext in MEDIA_EXTENSIONS:
                            root_media.append(entry.path)
                except OSError:
                    pass
    except (PermissionError, OSError):
        pass
    return root_media, subdirs


def scan_directory(
    root: str,
    pbar: Optional[tqdm] = None,
    lock: Optional[threading.Lock] = None,
    stats: Optional[_ScanStats] = None,
    no_exclude: bool = False,
    include_hidden: bool = False,
) -> List[str]:
    """递归扫描单个目录，返回所有媒体文件的绝对路径列表。线程安全。"""
    media_files: List[str] = []
    try:
        for dirpath, dirnames, filenames in os.walk(root, topdown=True, onerror=lambda e: None):
            if is_interrupted():
                break
            if stats:
                stats.add_dir()

            orig_count = len(dirnames)
            dirnames[:] = [
                d for d in dirnames
                if not _should_skip_dir(d, no_exclude, include_hidden)
            ]
            if stats:
                skipped = orig_count - len(dirnames)
                for _ in range(skipped):
                    stats.add_skipped_rule()

            for fname in filenames:
                ext = os.path.splitext(fname)[1].lower()
                if ext in MEDIA_EXTENSIONS:
                    media_files.append(os.path.join(dirpath, fname))
                    if stats:
                        stats.add_file(ext)
                    if pbar is not None:
                        display = dirpath[-60:] if len(dirpath) > 60 else dirpath
                        if lock:
                            with lock:
                                pbar.update(1)
                                pbar.set_postfix_str(display, refresh=False)
                        else:
                            pbar.update(1)
                            pbar.set_postfix_str(display, refresh=False)
    except PermissionError:
        if stats:
            stats.add_skipped_perm()
    return media_files


def scan_all_drives(
    custom_dirs: Optional[List[str]] = None,
    exclude_drives: Optional[Set[str]] = None,
    max_workers: int = DEFAULT_WORKERS,
    no_exclude: bool = False,
    include_hidden: bool = False,
) -> List[str]:
    """
    二级展开 + 多线程并行扫描（照片+视频）。

    对每个根目录先列出一级子目录，然后把所有子目录作为独立任务并行扫描。
    """
    excluded_letters = get_excluded_drive_letters()
    if exclude_drives:
        excluded_letters |= exclude_drives

    if custom_dirs:
        roots = custom_dirs
    else:
        all_drives = get_all_drives()
        roots = [d for d in all_drives if d[0].upper() not in excluded_letters]

    valid_roots = []
    for root in roots:
        if os.path.exists(root):
            valid_roots.append(root)
        else:
            logger.warning("目录不存在，跳过: %s", root)

    logger.info("将扫描以下根目录: %s", valid_roots)
    logger.info("排除的盘符: %s", excluded_letters)
    if no_exclude:
        logger.info("已禁用目录排除规则（--no-exclude）")
    if include_hidden:
        logger.info("包含隐藏目录（--include-hidden）")

    if not valid_roots:
        return []

    # ── 二级展开：收集根目录直属文件 + 一级子目录 ──
    all_media: List[str] = []
    scan_tasks: List[str] = []

    for root in valid_roots:
        root_media, subdirs = _scan_root_files(root, no_exclude, include_hidden)
        all_media.extend(root_media)
        if subdirs:
            scan_tasks.extend(subdirs)
        else:
            scan_tasks.append(root)

    logger.info("二级展开后共 %d 个扫描任务（根目录直属文件 %d 个）",
                len(scan_tasks), len(all_media))

    if not scan_tasks:
        return all_media

    # ── 多线程并行扫描所有子目录 ──
    workers = min(len(scan_tasks), max_workers)
    logger.info("使用 %d 线程并行扫描...", workers)

    stats = _ScanStats()
    lock = threading.Lock()
    with tqdm(desc="扫描文件", unit="个", initial=len(all_media), dynamic_ncols=True) as pbar:
        pool = ThreadPoolExecutor(max_workers=workers)
        try:
            futures = {
                pool.submit(
                    scan_directory, task, pbar, lock, stats,
                    no_exclude, include_hidden,
                ): task
                for task in scan_tasks
            }
            for future in _interruptible_as_completed(futures):
                task = futures[future]
                try:
                    found = future.result()
                    all_media.extend(found)
                except Exception as e:
                    logger.error("扫描出错 %s: %s", task, e)
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

    logger.info(stats.summary())
    logger.info("扫描完成，共发现 %d 个媒体文件", len(all_media))
    return all_media
