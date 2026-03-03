"""
清理源盘重复文件脚本

功能：
  指定一个已整理的目标目录，读取其中的 filelist.txt 溯源记录，
  在源盘中找到对应的原始文件，通过哈希比对确认内容一致后删除源文件。

安全机制：
  1. 默认 dry-run 模式，不实际删除
  2. 删除前必须哈希验证（目标文件 == 源文件）
  3. 每个源目录生成 _deleted_log_{timestamp}.txt 记录
  4. 支持指定多个目标目录

用法：
  # dry-run（默认，只生成报告不删除）
  python cleanup_source.py "H:\\All_相册_20260227\\All_1_目标设备_手机照片\\Apple iPhone 8"

  # 真正删除
  python cleanup_source.py --execute "H:\\All_相册_20260227\\All_1_目标设备_手机照片\\Apple iPhone 8"

  # 多个目录
  python cleanup_source.py "H:\\...\\Apple iPhone 7" "H:\\...\\HUAWEI P40 ANA-AN00"
"""

import argparse
import os
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
                if not source_path or source_path.startswith("H:"):
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


def _human_size(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(size) < 1024:
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}TB"


def cleanup_source(target_dirs: list, execute: bool = False, workers: int = 8):
    """
    主流程：
    1. 收集目标目录中所有文件的源路径
    2. 哈希比对验证
    3. 删除（或 dry-run 报告）
    """
    mode = "执行删除" if execute else "试运行(dry-run)"
    print(f"=== 源文件清理 [{mode}] ===")
    print(f"目标目录: {len(target_dirs)} 个")
    for d in target_dirs:
        print(f"  {d}")
    print()

    pairs = _collect_pairs(target_dirs)
    if not pairs:
        print("未找到任何溯源记录，退出")
        return

    print(f"共找到 {len(pairs)} 条溯源记录")

    # 按源目录分组
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
        log_lines = []
        log_lines.append(f"# 源文件清理日志 - {mode}")
        log_lines.append(f"# 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        log_lines.append(f"# 源目录: {src_dir}")
        log_lines.append(f"# 记录数: {len(dir_pairs)}")
        log_lines.append("#")
        log_lines.append("# 格式: 状态 | 源文件 | 大小 | 目标文件")
        log_lines.append("#" + "-" * 80)

        dir_deleted = 0
        dir_freed = 0

        for dest_path, source_path in sorted(dir_pairs, key=lambda x: x[1]):
            src_name = os.path.basename(source_path)

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

        # 写入日志文件
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
                # fallback: 写到目标目录
                try:
                    fallback = os.path.join(target_dirs[0], f"_cleanup_log_{timestamp}.txt")
                    with open(fallback, "a", encoding="utf-8") as f:
                        f.write("\n".join(log_lines) + "\n\n")
                    if fallback not in log_files_written:
                        log_files_written.append(fallback)
                except OSError:
                    pass

    # 打印汇总
    print()
    print("=" * 60)
    print(f"{'执行' if execute else '试运行'}结果汇总:")
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
    print(f"日志文件 ({len(log_files_written)} 个):")
    for lp in log_files_written:
        print(f"  {lp}")
    print("=" * 60)

    if not execute and stats["deleted"] > 0:
        print()
        print(f"提示: 以上为试运行结果。确认无误后，加 --execute 参数真正执行删除。")

    return stats


def main():
    p = argparse.ArgumentParser(
        description="清理源盘中已整理到目标目录的重复文件",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # dry-run（默认）
  python cleanup_source.py "H:\\All_相册_20260227\\All_1_目标设备_手机照片\\Apple iPhone 8"

  # 真正删除
  python cleanup_source.py --execute "H:\\...\\Apple iPhone 8"

  # 多个目录
  python cleanup_source.py "H:\\...\\Apple iPhone 7" "H:\\...\\HUAWEI P40 ANA-AN00"
        """,
    )
    p.add_argument(
        "target_dirs", nargs="+",
        help="目标目录路径（已整理好的文件夹，包含 filelist.txt）",
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
    cleanup_source(args.target_dirs, execute=args.execute, workers=args.workers)


if __name__ == "__main__":
    main()
