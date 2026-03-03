"""
临时脚本：恢复 H:\相册备份_20260301 中被删除的文件

原理：
  1. 从 hash_cache.json 中提取 H:\相册备份_20260301 下的原始文件记录（路径+哈希+大小）
  2. 在 H:\All_相册_20260302 中扫描实际存在的文件，通过大小+哈希匹配
  3. 将匹配的文件复制（不是移动）回 H:\相册备份_20260301 的原始路径

用法：
  python restore_backup.py              # dry-run
  python restore_backup.py --execute    # 真正复制
"""

import json
import os
import shutil
import sys
from collections import defaultdict
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import xxhash
    _USE_XXHASH = True
except ImportError:
    import hashlib
    _USE_XXHASH = False

HASH_SAMPLE_SIZE = 16 * 1024

DRY_RUN = "--execute" not in sys.argv

BACKUP_DIR = r"H:\相册备份_20260301"
ORGANIZED_DIR = r"H:\All_相册_20260302"
CACHE_PATH = r"H:\.photo_organizer\hash_cache.json"

IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".tiff", ".tif", ".bmp",
    ".cr2", ".cr3", ".nef", ".arw", ".orf", ".rw2",
    ".dng", ".raf", ".pef", ".heic", ".heif", ".srw",
}
VIDEO_EXTENSIONS = {
    ".mp4", ".mov", ".avi", ".mkv", ".mts", ".m2ts",
    ".m4v", ".wmv", ".flv", ".webm", ".3gp", ".mpg", ".mpeg",
}
MEDIA_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS


def _file_fast_hash(filepath, file_size):
    if _USE_XXHASH:
        h = xxhash.xxh3_64()
    else:
        h = hashlib.md5()
    h.update(file_size.to_bytes(8, "little"))
    with open(filepath, "rb") as f:
        head = f.read(HASH_SAMPLE_SIZE)
        h.update(head)
        if file_size > HASH_SAMPLE_SIZE * 2:
            f.seek(-HASH_SAMPLE_SIZE, 2)
            h.update(f.read())
    return h.hexdigest()


def human_size(size):
    for unit in ("B", "KB", "MB", "GB"):
        if abs(size) < 1024:
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}TB"


def main():
    mode = "执行恢复" if not DRY_RUN else "试运行(dry-run)"
    print(f"=== 恢复备份目录 [{mode}] ===")
    print(f"备份目录: {BACKUP_DIR}")
    print(f"来源目录: {ORGANIZED_DIR}")
    print()

    # 步骤 1: 从 hash_cache 提取备份目录原始文件列表
    print("步骤 1: 加载哈希缓存，提取备份目录记录...")
    with open(CACHE_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    entries = data.get("entries", {})

    backup_prefix = os.path.normcase(BACKUP_DIR + os.sep)
    backup_files = {}  # backup_path → {hash, size}
    for fp, entry in entries.items():
        if os.path.normcase(fp).startswith(backup_prefix):
            backup_files[fp] = {
                "hash": entry.get("hash"),
                "size": entry.get("size", 0),
            }
    print(f"  备份目录原始记录: {len(backup_files)} 个文件")

    # 需要恢复的文件（排除已存在的）
    to_restore = {}
    already_exists = 0
    for fp, info in backup_files.items():
        if os.path.isfile(fp):
            already_exists += 1
        else:
            to_restore[fp] = info
    print(f"  已存在(无需恢复): {already_exists}")
    print(f"  需要恢复: {len(to_restore)}")

    if not to_restore:
        print("\n所有文件已存在，无需恢复。")
        return

    # 按大小分组待恢复文件，加速匹配
    need_by_size = defaultdict(list)  # size → [(backup_path, hash)]
    for fp, info in to_restore.items():
        need_by_size[info["size"]].append((fp, info["hash"]))

    # 步骤 2: 扫描整理目录，按大小快速筛选 + 哈希精确匹配
    print(f"\n步骤 2: 扫描 {ORGANIZED_DIR} 查找匹配文件...")
    hash_to_source = {}  # hash → source_path (在整理目录中的实际路径)
    scanned = 0
    matched_hashes = set()
    target_hashes = {info["hash"] for info in to_restore.values()}

    for dirpath, dirs, filenames in os.walk(ORGANIZED_DIR):
        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower()
            if ext not in MEDIA_EXTENSIONS:
                continue
            fp = os.path.join(dirpath, fn)
            scanned += 1
            try:
                sz = os.path.getsize(fp)
            except OSError:
                continue

            if sz not in need_by_size:
                continue

            h = _file_fast_hash(fp, sz)
            if h in target_hashes and h not in hash_to_source:
                hash_to_source[h] = fp
                matched_hashes.add(h)

            if scanned % 10000 == 0:
                print(f"  已扫描 {scanned} 个文件, 匹配 {len(matched_hashes)} 个哈希...")

            if len(matched_hashes) == len(target_hashes):
                break
        if len(matched_hashes) == len(target_hashes):
            break

    print(f"  扫描完成: {scanned} 个文件, 匹配到 {len(matched_hashes)}/{len(target_hashes)} 个哈希")

    # 步骤 3: 复制恢复
    print(f"\n步骤 3: {'复制' if not DRY_RUN else '检查'}恢复文件...")
    stats = {"restored": 0, "no_source": 0, "error": 0, "total_bytes": 0}
    log_lines = [
        f"# 恢复日志 - {mode}",
        f"# 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"# 备份目录: {BACKUP_DIR}",
        f"# 来源目录: {ORGANIZED_DIR}",
        f"# 格式: 状态 | 恢复到 | 大小 | 复制自",
        "#" + "-" * 80,
    ]

    for backup_path, info in sorted(to_restore.items()):
        h = info["hash"]
        source = hash_to_source.get(h)

        if not source:
            stats["no_source"] += 1
            log_lines.append(f"无来源(hash={h[:12]}) | {backup_path} | {human_size(info['size'])} | -")
            continue

        if DRY_RUN:
            stats["restored"] += 1
            stats["total_bytes"] += info["size"]
            log_lines.append(f"可恢复(dry-run) | {backup_path} | {human_size(info['size'])} | {source}")
        else:
            try:
                os.makedirs(os.path.dirname(backup_path), exist_ok=True)
                shutil.copy2(source, backup_path)
                stats["restored"] += 1
                stats["total_bytes"] += info["size"]
                log_lines.append(f"已恢复 | {backup_path} | {human_size(info['size'])} | {source}")
            except Exception as e:
                stats["error"] += 1
                log_lines.append(f"错误({e}) | {backup_path} | - | {source}")

    log_lines.append("#" + "-" * 80)
    status_word = "已恢复" if not DRY_RUN else "可恢复"
    log_lines.append(f"# 小计: {status_word} {stats['restored']}, 已存在 {already_exists}, "
                     f"无来源 {stats['no_source']}, 错误 {stats['error']}")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = os.path.join(BACKUP_DIR, f"_restore_log_{timestamp}.txt")
    try:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        with open(log_path, "w", encoding="utf-8") as f:
            f.write("\n".join(log_lines) + "\n")
    except OSError as e:
        print(f"[WARN] 无法写入日志: {e}")
        log_path = None

    print()
    print("=" * 60)
    print(f"{mode}结果:")
    print(f"  备份目录记录总数: {len(backup_files)}")
    print(f"  已存在(跳过):     {already_exists}")
    print(f"  {status_word}:         {stats['restored']}")
    print(f"  需复制数据量:     {human_size(stats['total_bytes'])}")
    print(f"  无来源文件:       {stats['no_source']}")
    print(f"  错误:             {stats['error']}")
    if log_path:
        print(f"  日志: {log_path}")
    print("=" * 60)

    if DRY_RUN and stats["restored"] > 0:
        print()
        print("提示: 以上为试运行结果。确认无误后，加 --execute 参数真正执行恢复。")


if __name__ == "__main__":
    main()
