"""
相机照片/视频自动整理工具 — 主入口

功能：
  1. 扫描所有磁盘中的照片和视频文件
  2. 读取 EXIF / 视频元数据，分类为相机/手机/未识别
  3. 按设备过滤，仅复制目标设备的文件
  4. 按 年-季度 复制到目标文件夹
  5. 生成 HTML + CSV 溯源报告
"""

import argparse
import logging
import os
import signal
import sys
import time
from datetime import datetime

from tqdm import tqdm

from interrupt import set_interrupted

import config
from scanner import scan_all_drives
from exif_reader import read_photo_infos_parallel
from organizer import copy_photos_parallel, OrganizeResult
from reporter import generate_report


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    logging.basicConfig(level=level, format=fmt, datefmt="%H:%M:%S")
    logging.getLogger("exifread").setLevel(logging.ERROR)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="照片/视频自动整理工具 — 扫描全盘，按设备类型和年份-季度整理",
    )
    p.add_argument(
        "--scan-dirs", nargs="+", default=None,
        help="自定义扫描目录（默认扫描所有磁盘）",
    )
    p.add_argument(
        "--dest-drive", default=None,
        help=f"目标盘符根路径（默认 {config.DEST_DRIVE}）",
    )
    p.add_argument(
        "--output-dir", default=None,
        help="指定完整输出目录路径（如 H:\\All_相册_20260224_030934），覆盖自动生成",
    )
    p.add_argument("--camera-dest", default=None, help="自定义相机照片目标路径")
    p.add_argument("--phone-dest", default=None, help="自定义手机照片目标路径")
    p.add_argument("--unknown-dest", default=None, help="自定义未识别设备目标路径")
    p.add_argument("--report-dir", default=None, help="报告输出目录")

    p.add_argument(
        "--workers", type=int, default=config.DEFAULT_WORKERS,
        help=f"并行线程数（默认 {config.DEFAULT_WORKERS}），影响扫描/EXIF/哈希/复制各阶段",
    )

    p.add_argument(
        "--devices", default=None,
        help="逗号分隔的目标设备品牌关键词（默认: %s）" % ",".join(sorted(config.DEFAULT_TARGET_DEVICES)),
    )
    p.add_argument(
        "--copy-all", action="store_true",
        help="忽略设备过滤，复制所有识别到的文件（兼容旧行为）",
    )
    p.add_argument(
        "--copy-unknown", action="store_true", default=config.COPY_UNKNOWN,
        help="也复制无 EXIF / 未识别设备的全部文件（照片+视频）",
    )
    p.add_argument(
        "--copy-unknown-photo", action="store_true",
        help="仅复制无 EXIF / 未识别设备的照片",
    )
    p.add_argument(
        "--copy-unknown-video", action="store_true",
        help="仅复制无 EXIF / 未识别设备的视频",
    )

    p.add_argument("--no-exclude", action="store_true", help="禁用目录排除规则，扫描一切")
    p.add_argument("--include-hidden", action="store_true", help="也扫描 . 开头的隐藏目录")
    p.add_argument("--dry-run", action="store_true", help="试运行模式，不实际复制文件")
    p.add_argument("--verbose", "-v", action="store_true", help="显示详细日志")
    p.add_argument("--fix-po", action="store_true",
                   help="修复之前错误产生的 _po 后缀文件（重命名回原始名称）")

    p.add_argument("--nsfw", action="store_true", default=True,
                   help="启用 NSFW 两阶段检测（默认开启）")
    p.add_argument("--no-nsfw", action="store_false", dest="nsfw",
                   help="禁用 NSFW 检测")
    p.add_argument("--nsfw-threshold", type=float, default=0.5,
                   help="NSFW 检测阈值 0.0~1.0（默认 0.5），概率超过此值判定为 NSFW")

    return p.parse_args()


def _init_dest_paths(args) -> str:
    if args.output_dir:
        root = args.output_dir
    else:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        root = os.path.join(
            args.dest_drive or config.DEST_DRIVE,
            f"All_相册_{timestamp}",
        )

    if args.copy_all:
        config.DEST_CAMERA = args.camera_dest or os.path.join(root, config.DEST_TARGET_CAMERA_NAME)
        config.DEST_PHONE = args.phone_dest or os.path.join(root, config.DEST_TARGET_PHONE_NAME)
        config.DEST_UNKNOWN = args.unknown_dest or os.path.join(root, config.DEST_OTHER_UNKNOWN_NAME)
        config.DEST_CAMERA_OTHER = os.path.join(root, config.DEST_OTHER_CAMERA_NAME)
        config.DEST_PHONE_OTHER = os.path.join(root, config.DEST_OTHER_PHONE_NAME)
    else:
        config.DEST_CAMERA = args.camera_dest or os.path.join(root, config.DEST_CAMERA_NAME)
        config.DEST_PHONE = args.phone_dest or os.path.join(root, config.DEST_PHONE_NAME)
        config.DEST_UNKNOWN = args.unknown_dest or os.path.join(root, config.DEST_UNKNOWN_NAME)
        config.DEST_CAMERA_OTHER = ""
        config.DEST_PHONE_OTHER = ""

    config.DEST_DJI = os.path.join(root, config.DEST_DJI_NAME)
    config.DEST_NSFW = os.path.join(root, config.DEST_NSFW_NAME)
    config.DEST_SCREENSHOT = os.path.join(root, config.DEST_SCREENSHOT_NAME)
    config.REPORT_DIR = args.report_dir or root
    return root


def _infer_video_device(photo_infos, logger):
    """对无 make/model 或仅有 make 缺 model 的视频，从同目录照片推断设备（多数投票）。"""
    from collections import defaultdict, Counter

    dir_infos = defaultdict(list)
    for info in photo_infos:
        dir_infos[os.path.dirname(info.filepath)].append(info)

    inferred_full = 0
    inferred_model = 0
    for directory, infos in dir_infos.items():
        votes = Counter()
        for info in infos:
            if info.media_type == "photo" and info.make:
                votes[(info.make, info.model)] += 1
        if not votes:
            continue

        best_make, best_model = votes.most_common(1)[0][0]
        for info in infos:
            if info.media_type != "video":
                continue
            if not info.make:
                info.make = best_make
                info.model = best_model
                info.extra["device_source"] = f"由同目录照片推断({best_make} {best_model})"
                inferred_full += 1
            elif not info.model and info.make.lower() == best_make.lower() and best_model:
                info.model = best_model
                info.extra["device_source"] = f"型号由同目录照片补全({best_make} {best_model})"
                inferred_model += 1

    total = inferred_full + inferred_model
    if total:
        parts = []
        if inferred_full:
            parts.append(f"完整推断 {inferred_full} 个")
        if inferred_model:
            parts.append(f"补全型号 {inferred_model} 个")
        logger.info("从同目录照片推断视频设备: %s", ", ".join(parts))


def _generate_report_safe(result, report_dir, target_devices, scan_dirs, logger):
    """安全地生成报告（即使中断也尽力输出）。"""
    try:
        os.makedirs(report_dir, exist_ok=True)
        html_path, csv_path = generate_report(
            result, report_dir, target_devices, scan_dirs=scan_dirs,
        )
        logger.info("HTML 报告: %s", html_path)
        logger.info("CSV 报告:  %s", csv_path)
        return html_path, csv_path
    except Exception as e:
        logger.error("生成报告失败: %s", e)
        return None, None


_ctrl_c_count = 0


def _force_exit_after(seconds: int):
    """守护线程：等待指定秒数后强制退出"""
    import threading as _th
    def _bomb():
        import time as _t
        _t.sleep(seconds)
        print(f"\n{seconds}秒内未正常退出，强制终止。")
        os._exit(130)
    t = _th.Thread(target=_bomb, daemon=True)
    t.start()


def _install_ctrl_c_handler():
    """
    Ctrl+C handler（Windows 兼容）：
      第一次：设置中断标志 + 启动 5 秒强制退出定时器
      第二次：立即 os._exit
    """
    def _handler(signum, frame):
        global _ctrl_c_count
        _ctrl_c_count += 1
        set_interrupted()
        if _ctrl_c_count >= 2:
            print("\n再次 Ctrl+C，强制退出。")
            os._exit(130)
        print("\n收到 Ctrl+C，正在停止...（再按一次强制退出）")
        _force_exit_after(5)
        raise KeyboardInterrupt
    signal.signal(signal.SIGINT, _handler)


def main() -> None:
    _install_ctrl_c_handler()
    args = parse_args()
    setup_logging(args.verbose)
    logger = logging.getLogger("main")

    root_dir = _init_dest_paths(args)
    report_dir = config.REPORT_DIR
    workers = args.workers

    if args.fix_po:
        from organizer import restore_po_files
        restore_po_files(root_dir)
        return

    # 解析目标设备列表
    if args.devices:
        target_devices = {d.strip().lower() for d in args.devices.split(",") if d.strip()}
    else:
        target_devices = config.DEFAULT_TARGET_DEVICES

    logger.info("输出根目录: %s", root_dir)
    logger.info("  手机 → %s", config.DEST_PHONE)
    logger.info("  相机 → %s", config.DEST_CAMERA)
    logger.info("  未识别 → %s", config.DEST_UNKNOWN)
    if config.DEST_CAMERA_OTHER:
        logger.info("  其他手机 → %s", config.DEST_PHONE_OTHER)
        logger.info("  其他相机 → %s", config.DEST_CAMERA_OTHER)
    logger.info("  报告 → %s", report_dir)
    logger.info("并行线程: %d", workers)

    if args.copy_all:
        logger.info("设备过滤: 已禁用（--copy-all），复制所有已识别设备")
        logger.info("  目标品牌 → %s", ", ".join(sorted(target_devices)))
        if config.DEFAULT_TARGET_MODELS:
            logger.info("  目标型号 → %s", ", ".join(sorted(config.DEFAULT_TARGET_MODELS)))
    else:
        logger.info("目标品牌: %s", ", ".join(sorted(target_devices)))
        if config.DEFAULT_TARGET_MODELS:
            logger.info("目标型号: %s", ", ".join(sorted(config.DEFAULT_TARGET_MODELS)))

    if args.copy_unknown:
        logger.info("未识别文件: 全部复制（照片+视频）")
    elif args.copy_unknown_photo and args.copy_unknown_video:
        logger.info("未识别文件: 全部复制（照片+视频）")
    elif args.copy_unknown_photo:
        logger.info("未识别文件: 仅复制照片")
    elif args.copy_unknown_video:
        logger.info("未识别文件: 仅复制视频")
    else:
        logger.info("未识别文件: 仅记录在报告中，不复制")

    # ── NSFW 检测初始化 ──
    nsfw_detector = None
    if args.nsfw:
        try:
            from nsfw_detector import check_dependencies, NsfwDetector
            check_dependencies()
            nsfw_detector = NsfwDetector(threshold=args.nsfw_threshold)
            logger.info("NSFW 检测: 已启用（阈值 %.2f）→ %s", args.nsfw_threshold, config.DEST_NSFW)
        except ImportError as e:
            logger.warning("NSFW 检测依赖未安装，已跳过: %s", e)
    else:
        logger.info("NSFW 检测: 已禁用（--no-nsfw）")

    if args.dry_run:
        logger.info("=== 试运行模式：不会实际复制文件 ===")

    result = None
    photo_infos = []
    interrupted = False
    t0 = time.time()

    try:
        # ── 阶段 1：扫描 ──
        logger.info("=" * 60)
        logger.info("阶段 1/4: 扫描磁盘中的照片和视频文件...")
        logger.info("=" * 60)

        media_files = scan_all_drives(
            custom_dirs=args.scan_dirs,
            max_workers=workers,
            no_exclude=args.no_exclude,
            include_hidden=args.include_hidden,
        )
        scan_time = time.time() - t0

        if not media_files:
            logger.warning("未发现任何媒体文件，程序退出。")
            sys.exit(0)

        logger.info("扫描完成: 发现 %d 个文件，耗时 %.1f 秒", len(media_files), scan_time)

        # ── 阶段 2：读取 EXIF / 视频元数据 ──
        logger.info("=" * 60)
        logger.info("阶段 2/4: 读取元数据信息（%d 线程）...", workers)
        logger.info("=" * 60)

        t1 = time.time()
        from exif_reader import _get_ffprobe_path
        _get_ffprobe_path()
        with tqdm(total=len(media_files), desc="读取元数据", unit="个") as pbar:
            photo_infos = read_photo_infos_parallel(
                media_files,
                max_workers=workers,
                progress_callback=lambda: pbar.update(1),
            )

        exif_time = time.time() - t1
        has_exif = sum(1 for p in photo_infos if p.has_exif_date)
        photo_cnt = sum(1 for p in photo_infos if p.media_type == "photo")
        video_cnt = sum(1 for p in photo_infos if p.media_type == "video")
        logger.info("元数据读取完成: %d 有日期, %d 无日期 (照片 %d, 视频 %d), 耗时 %.1f 秒",
                    has_exif, len(photo_infos) - has_exif, photo_cnt, video_cnt, exif_time)

        # ── 2b：从同目录照片推断视频设备 ──
        _infer_video_device(photo_infos, logger)

        # ── 阶段 3：分类 + 过滤 + 复制（三阶段并行） ──
        logger.info("=" * 60)
        copy_w = min(workers, 8)
        logger.info("阶段 3/4: 并行分类并%s文件...",
                    "模拟复制" if args.dry_run else "复制")
        logger.info("  3a) 并行哈希+分类+过滤（%d 线程）", workers)
        logger.info("  3b) 串行去重 + 路径冲突解决")
        logger.info("  3c) 并行复制文件（%d 线程，源路径排序）", copy_w)
        logger.info("=" * 60)

        t2 = time.time()
        result = copy_photos_parallel(
            photo_infos,
            max_workers=workers,
            dry_run=args.dry_run,
            target_devices=target_devices,
            copy_all=args.copy_all,
            copy_unknown=args.copy_unknown,
            copy_unknown_photo=args.copy_unknown_photo,
            copy_unknown_video=args.copy_unknown_video,
            nsfw_detector=nsfw_detector,
        )

        for record in result.records:
            if record.status == "error" and record.error_msg:
                logger.warning("处理失败: %s → %s", record.source, record.error_msg)

        copy_time = time.time() - t2
        nsfw_msg = f", NSFW {result.nsfw_count}" if result.nsfw_count else ""
        logger.info(
            "整理完成: 复制 %d%s, 重复跳过 %d, 非目标 %d, 无设备 %d, 小图过滤 %d, 错误 %d, 耗时 %.1f 秒",
            result.copied, nsfw_msg, result.skipped_dup, result.skipped_not_target,
            result.skipped_no_device, result.skipped_filtered, result.errors, copy_time,
        )

    except KeyboardInterrupt:
        interrupted = True
        logger.warning("")
        logger.warning("用户中断 (Ctrl+C)，正在生成已完成部分的报告...")

    # ── 阶段 4：生成报告（正常完成 或 中断后均执行） ──
    if result is not None or photo_infos:
        logger.info("=" * 60)
        logger.info("阶段 4/4: 生成整理报告%s", "（部分数据）" if interrupted else "")
        logger.info("=" * 60)

        if result is None:
            from organizer import OrganizeResult
            result = OrganizeResult(total_found=len(photo_infos))

        html_path, csv_path = _generate_report_safe(
            result, report_dir, target_devices, args.scan_dirs, logger,
        )

        total_time = time.time() - t0
        logger.info("=" * 60)
        logger.info("%s", "中断后报告已生成!" if interrupted else "全部完成!")
        logger.info("  发现文件:   %d 个", result.total_found)
        logger.info("  成功复制:   %d 个", result.copied)
        if result.nsfw_count:
            logger.info("  NSFW 检出:  %d 个", result.nsfw_count)
        logger.info("  重复跳过:   %d 个", result.skipped_dup)
        logger.info("  小图过滤:   %d 个", result.skipped_filtered)
        logger.info("  非目标设备: %d 个", result.skipped_not_target)
        logger.info("  无设备信息: %d 个", result.skipped_no_device)
        logger.info("  错误:       %d 个", result.errors)
        logger.info("  总耗时:     %.1f 秒", total_time)
        logger.info("=" * 60)

    if interrupted:
        sys.exit(130)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n用户中断，退出。")
        sys.exit(130)
