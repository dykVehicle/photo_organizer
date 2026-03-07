"""
清理源盘重复文件脚本

两种模式：
  模式 1（哈希比对，推荐）：指定目标目录 + 源目录，直接哈希比对找重复
  模式 2（溯源记录）：指定目标目录，从 filelist.txt 读取源路径匹配

安全机制：
  1. 默认 dry-run 模式，不实际删除
  2. 删除前必须哈希验证（目标文件 == 源文件）
  3. H 盘文件永远不会被删除
  4. 每个源目录生成 _cleanup_log_{timestamp}.txt 记录

用法：
  # 模式 1：哈希比对（推荐）— 扫描源目录，与目标目录哈希比对
  python cleanup_source.py --target-dir "H:\\All_相册_20260307" --source-dirs "E:\\相册_E" "G:\\相册_G"

  # 模式 1：真正删除
  python cleanup_source.py --execute --target-dir "H:\\All_相册_20260307" --source-dirs "E:\\相册_E"

  # 模式 2：溯源记录（旧模式）
  python cleanup_source.py "H:\\All_相册_20260307\\All_1_目标设备_手机照片\\Apple iPhone 8"
"""

import argparse
import os
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import json
import time

try:
    import xxhash
    _USE_XXHASH = True
except ImportError:
    import hashlib
    _USE_XXHASH = False

try:
    from tqdm import tqdm
    _HAS_TQDM = True
except ImportError:
    _HAS_TQDM = False

HASH_SAMPLE_SIZE = 16 * 1024
PROTECTED_DRIVE = "H:"
CACHE_DIR = ".photo_organizer"
HASH_CACHE_FILENAME = "hash_cache.json"
SRC_HASH_CACHE_FILENAME = "src_hash_cache.json"
_HASH_CACHE_VERSION = 2


def _file_fast_hash(filepath: str, file_size: int) -> str:
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


def _human_size(size) -> str:
    size = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if abs(size) < 1024:
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}TB"


def _is_protected(filepath: str) -> bool:
    return filepath.upper().startswith(PROTECTED_DRIVE.upper())


def _time_match(entry: dict, mtime: float, ctime: float = 0.0) -> bool:
    tol = 0.01
    cached_mtime = entry.get("mtime", 0.0)
    cached_ctime = entry.get("ctime", 0.0)
    candidates = [t for t in (cached_mtime, cached_ctime) if t > 0]
    for cur in (mtime, ctime):
        if cur <= 0:
            continue
        for ref in candidates:
            if abs(cur - ref) < tol:
                return True
    return False


def _load_hash_cache(cache_path: str) -> dict:
    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("version") != _HASH_CACHE_VERSION:
            return {}
        return data.get("entries", {})
    except (FileNotFoundError, OSError, json.JSONDecodeError, ValueError):
        return {}


def _save_hash_cache(cache_path: str, entries: dict) -> None:
    data = {"version": _HASH_CACHE_VERSION, "entries": entries}
    tmp = cache_path + ".tmp"
    try:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, cache_path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _get_cache_for_drive(drive_letter: str) -> str:
    cache_dir = os.path.join(drive_letter + os.sep, CACHE_DIR)
    try:
        os.makedirs(cache_dir, exist_ok=True)
    except OSError:
        return ""
    return os.path.join(cache_dir, SRC_HASH_CACHE_FILENAME)


def _hash_with_cache(filepath: str, cache_entries: dict) -> tuple:
    """返回 (hash, mtime, ctime, cache_hit)。优先从缓存命中。"""
    try:
        st = os.stat(filepath)
        fsize = st.st_size
        mtime = st.st_mtime
        ctime = st.st_ctime
    except OSError:
        return None, 0, 0, False

    cached = cache_entries.get(filepath)
    if cached and cached.get("size") == fsize and _time_match(cached, mtime, ctime):
        return cached["hash"], mtime, ctime, True

    try:
        h = _file_fast_hash(filepath, fsize)
        return h, mtime, ctime, False
    except OSError:
        return None, 0, 0, False


# ═══════════════════════════════════════════════════════════════
# 模式 1：哈希直接比对（推荐）
# ═══════════════════════════════════════════════════════════════

def _scan_media_files(directory: str):
    """扫描目录下所有媒体文件，返回文件路径列表"""
    from config import MEDIA_EXTENSIONS
    files = []
    for dirpath, _, filenames in os.walk(directory):
        for f in filenames:
            if os.path.splitext(f)[1].lower() in MEDIA_EXTENSIONS:
                files.append(os.path.join(dirpath, f))
    return files


def _build_target_hash_index(target_dir: str, workers: int = 8):
    """扫描目标目录，建立 hash → filepath 索引（支持缓存加速）"""
    print(f"扫描目标目录: {target_dir}")
    files = _scan_media_files(target_dir)
    print(f"  目标文件数: {len(files)}")

    drv = os.path.splitdrive(target_dir)[0].upper()
    cache_path = _get_cache_for_drive(drv) if drv else ""
    cache_entries = _load_hash_cache(cache_path) if cache_path else {}
    if cache_entries:
        print(f"  已加载哈希缓存: {len(cache_entries)} 条")

    hash_index = {}
    new_entries = dict(cache_entries)
    errors = 0
    cache_hits = 0

    if _HAS_TQDM:
        pbar = tqdm(total=len(files), desc="目标哈希", unit="个")
    else:
        pbar = None

    def _hash_one(fp):
        return fp, *_hash_with_cache(fp, cache_entries)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_hash_one, fp): fp for fp in files}
        for fut in as_completed(futures):
            try:
                fp, h, mtime, ctime, hit = fut.result()
                if h:
                    if h not in hash_index:
                        hash_index[h] = fp
                    if hit:
                        cache_hits += 1
                    else:
                        new_entries[fp] = {"hash": h, "mtime": mtime, "size": os.path.getsize(fp)}
                        if ctime:
                            new_entries[fp]["ctime"] = ctime
                else:
                    errors += 1
            except Exception:
                errors += 1
            if pbar:
                pbar.update(1)

    if pbar:
        pbar.close()

    if cache_path and len(new_entries) > len(cache_entries):
        _save_hash_cache(cache_path, new_entries)
        added = len(new_entries) - len(cache_entries)
        print(f"  哈希缓存已更新: +{added} 条, 总计 {len(new_entries)} 条")

    print(f"  唯一哈希数: {len(hash_index)}, 缓存命中: {cache_hits}, 新计算: {len(files)-cache_hits-errors}, 错误: {errors}")
    return hash_index


def cleanup_by_hash(target_dir: str, source_dirs: list, execute: bool = False, workers: int = 8):
    """模式 1：哈希直接比对清理"""
    mode = "执行删除" if execute else "试运行(dry-run)"
    print(f"=== 源文件清理 - 哈希比对模式 [{mode}] ===")
    print(f"目标目录: {target_dir}")
    print(f"源目录: {len(source_dirs)} 个")
    for d in source_dirs:
        print(f"  {d}")
    print()

    for d in source_dirs:
        if _is_protected(d):
            print(f"[ERROR] 源目录 {d} 在受保护的 {PROTECTED_DRIVE} 盘上，跳过")
            source_dirs = [s for s in source_dirs if not _is_protected(s)]
    if not source_dirs:
        print("没有可处理的源目录，退出")
        return

    hash_index = _build_target_hash_index(target_dir, workers)
    if not hash_index:
        print("目标目录无文件，退出")
        return

    print(f"\n扫描源目录...")
    source_files_by_dir = {}
    total_source = 0
    for src_dir in source_dirs:
        if not os.path.isdir(src_dir):
            print(f"[WARN] 源目录不存在: {src_dir}")
            continue
        files = _scan_media_files(src_dir)
        source_files_by_dir[src_dir] = files
        total_source += len(files)
        print(f"  {src_dir}: {len(files)} 个文件")

    print(f"  源文件总计: {total_source}")
    print()

    stats = {
        "total": total_source,
        "matched": 0,
        "deleted": 0,
        "unique": 0,
        "error": 0,
        "freed_bytes": 0,
    }

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_files_written = []

    # 加载各源盘的哈希缓存
    src_caches = {}
    src_cache_paths = {}
    src_new_entries = {}
    for src_dir in source_dirs:
        drv = os.path.splitdrive(src_dir)[0].upper()
        if drv and drv not in src_caches:
            cp = _get_cache_for_drive(drv)
            if cp:
                src_cache_paths[drv] = cp
                entries = _load_hash_cache(cp)
                src_caches[drv] = entries
                src_new_entries[drv] = dict(entries)
                if entries:
                    print(f"  {drv} 盘哈希缓存: {len(entries)} 条")

    if _HAS_TQDM:
        pbar = tqdm(total=total_source, desc="比对源文件", unit="个")
    else:
        pbar = None

    total_cache_hits = 0

    for src_dir, files in source_files_by_dir.items():
        drv = os.path.splitdrive(src_dir)[0].upper()
        cache_entries = src_caches.get(drv, {})
        dir_groups = defaultdict(list)

        for fp in files:
            try:
                h, mtime, ctime, hit = _hash_with_cache(fp, cache_entries)
                if not h:
                    stats["error"] += 1
                    if pbar:
                        pbar.update(1)
                    continue

                if hit:
                    total_cache_hits += 1
                elif drv in src_new_entries:
                    sz = os.path.getsize(fp)
                    entry = {"hash": h, "mtime": mtime, "size": sz}
                    if ctime:
                        entry["ctime"] = ctime
                    src_new_entries[drv][fp] = entry

                target_fp = hash_index.get(h)
                if target_fp:
                    stats["matched"] += 1
                    sz = os.path.getsize(fp)
                    dir_groups[os.path.dirname(fp)].append((fp, sz, target_fp))
                else:
                    stats["unique"] += 1
            except Exception:
                stats["error"] += 1
            if pbar:
                pbar.update(1)

        for sub_dir, matches in sorted(dir_groups.items()):
            log_lines = [
                f"# 源文件清理日志 (哈希比对) - {mode}",
                f"# 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                f"# 源目录: {sub_dir}",
                f"# 匹配数: {len(matches)}",
                "#",
                "# 格式: 状态 | 源文件 | 大小 | 目标文件(哈希匹配)",
                "#" + "-" * 80,
            ]

            dir_deleted = 0
            dir_freed = 0

            for src_fp, src_size, target_fp in sorted(matches, key=lambda x: x[0]):
                if execute:
                    try:
                        os.remove(src_fp)
                        stats["deleted"] += 1
                        stats["freed_bytes"] += src_size
                        dir_deleted += 1
                        dir_freed += src_size
                        log_lines.append(f"已删除 | {src_fp} | {_human_size(src_size)} | {target_fp}")
                    except Exception as e:
                        stats["error"] += 1
                        log_lines.append(f"删除失败({e}) | {src_fp} | {_human_size(src_size)} | {target_fp}")
                else:
                    stats["deleted"] += 1
                    stats["freed_bytes"] += src_size
                    dir_deleted += 1
                    dir_freed += src_size
                    log_lines.append(f"可删除(dry-run) | {src_fp} | {_human_size(src_size)} | {target_fp}")

            log_lines.append("#" + "-" * 80)
            status_word = "已删除" if execute else "可删除"
            log_lines.append(f"# 小计: {status_word} {dir_deleted} 个文件, 释放 {_human_size(dir_freed)}")

            try:
                if os.path.isdir(sub_dir):
                    log_path = os.path.join(sub_dir, f"_cleanup_log_{timestamp}.txt")
                else:
                    log_path = os.path.join(src_dir, f"_cleanup_log_{timestamp}.txt")
                with open(log_path, "w", encoding="utf-8") as f:
                    f.write("\n".join(log_lines) + "\n")
                if log_path not in log_files_written:
                    log_files_written.append(log_path)
            except OSError:
                pass

    if pbar:
        pbar.close()

    # 保存源盘哈希缓存
    for drv, cp in src_cache_paths.items():
        new_ent = src_new_entries.get(drv, {})
        old_ent = src_caches.get(drv, {})
        if len(new_ent) > len(old_ent):
            _save_hash_cache(cp, new_ent)
            added = len(new_ent) - len(old_ent)
            print(f"  {drv} 盘缓存已更新: +{added} 条, 总计 {len(new_ent)} 条")

    stats["cache_hits"] = total_cache_hits

    _print_hash_summary(stats, execute, log_files_written)
    return stats


def _print_hash_summary(stats, execute, log_files_written):
    print()
    print("=" * 60)
    print(f"{'执行' if execute else '试运行'}结果汇总 (哈希比对模式):")
    print(f"  源文件总数:     {stats['total']}")
    print(f"  缓存命中:       {stats.get('cache_hits', 0)}")
    print(f"  哈希匹配(重复): {stats['matched']}")
    print(f"  无匹配(独有):   {stats['unique']}")
    status_word = "已删除" if execute else "可删除"
    print(f"  {status_word}文件数:   {stats['deleted']}")
    print(f"  可释放空间:     {_human_size(stats['freed_bytes'])}")
    print(f"  错误:           {stats['error']}")
    print()
    if log_files_written:
        print(f"日志文件 ({len(log_files_written)} 个):")
        for lp in log_files_written[:20]:
            print(f"  {lp}")
        if len(log_files_written) > 20:
            print(f"  ... 共 {len(log_files_written)} 个")
    print("=" * 60)

    if not execute and stats["deleted"] > 0:
        print()
        print("提示: 以上为试运行结果。确认无误后，加 --execute 参数真正执行删除。")


# ═══════════════════════════════════════════════════════════════
# 模式 2：溯源记录（旧模式，保留兼容）
# ═══════════════════════════════════════════════════════════════

def _parse_filelist(filelist_path: str):
    """解析 filelist.txt，返回 [(dest_filename, source_path)] 列表"""
    pairs = []
    dest_dir = os.path.dirname(filelist_path)
    try:
        with open(filelist_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or line.startswith("-") or line.startswith("Filename"):
                    continue
                parts = [p.strip() for p in line.split(" | ")]
                if len(parts) < 8:
                    continue
                dest_filename = parts[0]
                source_path = parts[-1]
                if not source_path or _is_protected(source_path):
                    continue
                dest_path = os.path.join(dest_dir, dest_filename)
                pairs.append((dest_path, source_path))
    except (OSError, UnicodeDecodeError) as e:
        print(f"  [WARN] 无法读取 {filelist_path}: {e}")
    return pairs


def _collect_pairs(target_dirs: list):
    """从目标目录收集所有 (dest_path, source_path) 对"""
    all_pairs = []
    for target_dir in target_dirs:
        if not os.path.isdir(target_dir):
            print(f"[ERROR] 目标目录不存在: {target_dir}")
            continue
        for dirpath, _, filenames in os.walk(target_dir):
            if "filelist.txt" in filenames:
                pairs = _parse_filelist(os.path.join(dirpath, "filelist.txt"))
                all_pairs.extend(pairs)
    return all_pairs


def cleanup_by_filelist(target_dirs: list, execute: bool = False, workers: int = 8):
    """模式 2：基于 filelist.txt 溯源记录清理（H 盘源文件受保护）"""
    mode = "执行删除" if execute else "试运行(dry-run)"
    print(f"=== 源文件清理 - 溯源记录模式 [{mode}] ===")
    print(f"目标目录: {len(target_dirs)} 个")
    for d in target_dirs:
        print(f"  {d}")
    print()

    pairs = _collect_pairs(target_dirs)
    if not pairs:
        print("未找到任何溯源记录（注: H 盘源路径已自动跳过），退出")
        return

    print(f"共找到 {len(pairs)} 条溯源记录")

    source_dir_groups = defaultdict(list)
    for dest_path, source_path in pairs:
        src_dir = os.path.dirname(source_path)
        source_dir_groups[src_dir].append((dest_path, source_path))

    stats = {
        "total": len(pairs),
        "verified": 0,
        "deleted": 0,
        "src_missing": 0,
        "dest_missing": 0,
        "hash_mismatch": 0,
        "error": 0,
        "freed_bytes": 0,
    }

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_files_written = []

    for src_dir, dir_pairs in sorted(source_dir_groups.items()):
        log_lines = [
            f"# 源文件清理日志 (溯源记录) - {mode}",
            f"# 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"# 源目录: {src_dir}",
            f"# 记录数: {len(dir_pairs)}",
            "#",
            "# 格式: 状态 | 源文件 | 大小 | 目标文件",
            "#" + "-" * 80,
        ]

        dir_deleted = 0
        dir_freed = 0

        for dest_path, source_path in sorted(dir_pairs, key=lambda x: x[1]):
            if not os.path.isfile(source_path):
                stats["src_missing"] += 1
                log_lines.append(f"源文件不存在 | {source_path} | - | {dest_path}")
                continue

            if not os.path.isfile(dest_path):
                stats["dest_missing"] += 1
                log_lines.append(f"目标文件不存在 | {source_path} | - | {dest_path}")
                continue

            try:
                src_size = os.path.getsize(source_path)
                dst_size = os.path.getsize(dest_path)

                if src_size != dst_size:
                    stats["hash_mismatch"] += 1
                    log_lines.append(
                        f"大小不匹配({_human_size(src_size)} vs {_human_size(dst_size)}) | "
                        f"{source_path} | {_human_size(src_size)} | {dest_path}"
                    )
                    continue

                src_hash = _file_fast_hash(source_path, src_size)
                dst_hash = _file_fast_hash(dest_path, dst_size)

                if src_hash != dst_hash:
                    stats["hash_mismatch"] += 1
                    log_lines.append(f"哈希不匹配 | {source_path} | {_human_size(src_size)} | {dest_path}")
                    continue

                stats["verified"] += 1

                if execute:
                    os.remove(source_path)
                    stats["deleted"] += 1
                    stats["freed_bytes"] += src_size
                    dir_deleted += 1
                    dir_freed += src_size
                    log_lines.append(f"已删除 | {source_path} | {_human_size(src_size)} | {dest_path}")
                else:
                    stats["deleted"] += 1
                    stats["freed_bytes"] += src_size
                    dir_deleted += 1
                    dir_freed += src_size
                    log_lines.append(f"可删除(dry-run) | {source_path} | {_human_size(src_size)} | {dest_path}")

            except Exception as e:
                stats["error"] += 1
                log_lines.append(f"错误({e}) | {source_path} | - | {dest_path}")

        log_lines.append("#" + "-" * 80)
        status_word = "已删除" if execute else "可删除"
        log_lines.append(f"# 小计: {status_word} {dir_deleted} 个文件, 释放 {_human_size(dir_freed)}")

        if dir_pairs:
            try:
                if os.path.isdir(src_dir):
                    log_path = os.path.join(src_dir, f"_cleanup_log_{timestamp}.txt")
                else:
                    log_path = os.path.join(os.path.dirname(src_dir), f"_cleanup_log_{timestamp}.txt")
                with open(log_path, "w", encoding="utf-8") as f:
                    f.write("\n".join(log_lines) + "\n")
                log_files_written.append(log_path)
            except OSError as e:
                print(f"  [WARN] 无法写入日志到 {src_dir}: {e}")

    print()
    print("=" * 60)
    print(f"{'执行' if execute else '试运行'}结果汇总 (溯源记录模式):")
    print(f"  溯源记录总数:   {stats['total']}")
    print(f"  哈希验证通过:   {stats['verified']}")
    status_word = "已删除" if execute else "可删除"
    print(f"  {status_word}文件数:   {stats['deleted']}")
    print(f"  可释放空间:     {_human_size(stats['freed_bytes'])}")
    print(f"  源文件不存在:   {stats['src_missing']}")
    print(f"  目标文件不存在: {stats['dest_missing']}")
    print(f"  哈希/大小不匹配: {stats['hash_mismatch']}")
    print(f"  错误:           {stats['error']}")
    print()
    if log_files_written:
        print(f"日志文件 ({len(log_files_written)} 个):")
        for lp in log_files_written:
            print(f"  {lp}")
    print("=" * 60)

    if not execute and stats["deleted"] > 0:
        print()
        print("提示: 以上为试运行结果。确认无误后，加 --execute 参数真正执行删除。")

    return stats


# ═══════════════════════════════════════════════════════════════

def main():
    p = argparse.ArgumentParser(
        description="清理源盘中已整理到目标目录的重复文件",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
模式 1 - 哈希比对（推荐，不依赖 filelist.txt）：
  python cleanup_source.py --target-dir "H:\\All_相册_20260307" --source-dirs "E:\\相册_E" "G:\\相册_G"
  python cleanup_source.py --execute --target-dir "H:\\All_相册_20260307" --source-dirs "E:\\相册_E"

模式 2 - 溯源记录（旧模式，依赖 filelist.txt，H 盘源文件不可删除）：
  python cleanup_source.py "H:\\All_相册_20260307\\All_1_目标设备_手机照片\\Apple iPhone 8"
  python cleanup_source.py --execute "H:\\...\\Apple iPhone 8"

注意: H 盘上的文件永远不会被删除。
        """,
    )
    p.add_argument(
        "target_dirs", nargs="*", default=[],
        help="[模式 2] 目标目录路径（已整理好的文件夹，包含 filelist.txt）",
    )
    p.add_argument(
        "--target-dir", default=None,
        help="[模式 1] 目标目录（已整理好的 H 盘文件夹）",
    )
    p.add_argument(
        "--source-dirs", nargs="+", default=None,
        help="[模式 1] 源目录列表（要清理的非 H 盘目录）",
    )
    p.add_argument(
        "--execute", action="store_true",
        help="真正执行删除（默认为 dry-run 模式）",
    )
    p.add_argument(
        "--workers", type=int, default=8,
        help="并行线程数（默认 8）",
    )

    args = p.parse_args()

    if args.target_dir and args.source_dirs:
        cleanup_by_hash(args.target_dir, args.source_dirs,
                        execute=args.execute, workers=args.workers)
    elif args.target_dirs:
        cleanup_by_filelist(args.target_dirs, execute=args.execute, workers=args.workers)
    else:
        p.print_help()
        print("\n[ERROR] 请指定目标目录。使用 --target-dir + --source-dirs（哈希比对）或直接传入目标目录（溯源记录）。")
        sys.exit(1)


if __name__ == "__main__":
    main()
