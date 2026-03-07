"""全量文档检测扫描: 扫描已整理目录，缓存检测结果，并将文档图片复制到 All_7_文档图片"""
import json
import logging
import os
import shutil
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

try:
    import xxhash
    _USE_XXHASH = True
except ImportError:
    _USE_XXHASH = False

import numpy as np
from concurrent.futures import ThreadPoolExecutor
from tqdm import tqdm

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("scan_doc")

SOURCE_DIR = r"H:\All_相册_20260305"
CACHE_DIR = r"H:\.photo_organizer"
DOC_OUTPUT_DIR = os.path.join(SOURCE_DIR, "All_7_文档图片")
SCREENSHOT_OUTPUT_DIR = os.path.join(DOC_OUTPUT_DIR, "截图")
NSFW_DIR_NAME = "All_999_NSFW"
FACE_DIR_NAME = "All_F_人物相册"
DOC_DIR_NAME = "All_7_文档图片"
HASH_SAMPLE_SIZE = 16 * 1024
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif", ".heic", ".heif"}
RAW_EXTS = {".cr2", ".cr3", ".nef", ".arw", ".orf", ".rw2", ".dng", ".raf", ".pef", ".srw"}
SCREENSHOT_KEYWORDS = {"screenshot", "screen_shot", "screen-shot", "screen shot", "截图", "截屏"}
DOC_THRESHOLD = 0.5
BATCH_SIZE = 256
PREPROCESS_WORKERS = 8


def _fast_hash(filepath, file_size):
    if _USE_XXHASH:
        h = xxhash.xxh3_64()
    else:
        import hashlib
        h = hashlib.md5()
    h.update(file_size.to_bytes(8, "little"))
    with open(filepath, "rb") as f:
        head = f.read(HASH_SAMPLE_SIZE)
        h.update(head)
        if file_size > HASH_SAMPLE_SIZE * 2:
            f.seek(-HASH_SAMPLE_SIZE, 2)
            h.update(f.read())
    return h.hexdigest()


def _is_screenshot(filepath):
    filename = os.path.basename(filepath).lower()
    return any(kw in filename for kw in SCREENSHOT_KEYWORDS)


def _should_skip_dir(dirpath):
    parts = os.path.normpath(dirpath).split(os.sep)
    for p in parts:
        if p == NSFW_DIR_NAME or p == FACE_DIR_NAME or p == DOC_DIR_NAME:
            return True
    return False


def main():
    logger.info("=" * 60)
    logger.info("全量文档检测扫描")
    logger.info("源目录: %s", SOURCE_DIR)
    logger.info("缓存目录: %s", CACHE_DIR)
    logger.info("输出目录: %s", DOC_OUTPUT_DIR)
    logger.info("阈值: %.2f", DOC_THRESHOLD)
    logger.info("=" * 60)

    # 1. 扫描所有图片（跳过 NSFW、人物相册、文档图片目录）
    logger.info("扫描图片文件...")
    t0 = time.time()
    all_images = []
    screenshot_images = []
    for root, dirs, files in os.walk(SOURCE_DIR):
        if _should_skip_dir(root):
            dirs.clear()
            continue
        for f in files:
            ext = os.path.splitext(f)[1].lower()
            if ext in RAW_EXTS:
                continue
            if ext in IMAGE_EXTS:
                fp = os.path.join(root, f)
                if _is_screenshot(fp):
                    screenshot_images.append(fp)
                else:
                    all_images.append(fp)

    scan_time = time.time() - t0
    logger.info("扫描完成: %d 张图片 + %d 张截图，耗时 %.1f 秒",
                len(all_images), len(screenshot_images), scan_time)

    # 2. 计算哈希
    logger.info("计算文件哈希...")
    t1 = time.time()
    file_hashes = {}
    for fp in tqdm(all_images, desc="计算哈希", unit="个"):
        try:
            fsize = os.path.getsize(fp)
            fhash = _fast_hash(fp, fsize)
            file_hashes[fp] = fhash
        except OSError:
            pass
    hash_time = time.time() - t1
    logger.info("哈希完成: %d 个文件，耗时 %.1f 秒", len(file_hashes), hash_time)

    # 3. 加载文档检测缓存
    os.makedirs(CACHE_DIR, exist_ok=True)
    cache_path = os.path.join(CACHE_DIR, "document_score_cache.json")
    doc_cache = {}
    if os.path.isfile(cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                doc_cache = json.load(f)
            logger.info("文档缓存已加载: %d 条", len(doc_cache))
        except Exception as e:
            logger.warning("文档缓存加载失败: %s", e)

    # 分离缓存命中和需检测
    cached_scores = {}
    need_detect = []
    for fp, fhash in file_hashes.items():
        if fhash in doc_cache:
            cached_scores[fp] = doc_cache[fhash]
        else:
            need_detect.append(fp)

    logger.info("缓存命中: %d, 需检测: %d", len(cached_scores), len(need_detect))

    # 4. 运行文档检测
    new_scores = {}
    if need_detect:
        from doc_detector import DocumentDetector, preprocess_single
        detector = DocumentDetector(threshold=DOC_THRESHOLD)
        detector._ensure_model()

        logger.info("开始文档检测: %d 张图片 (batch=%d, %d 预处理线程)",
                    len(need_detect), BATCH_SIZE, PREPROCESS_WORKERS)

        batches = [need_detect[i:i + BATCH_SIZE] for i in range(0, len(need_detect), BATCH_SIZE)]
        processed = 0
        save_interval = 2000

        with tqdm(total=len(need_detect), desc="文档检测", unit="个") as pbar:
            pool = ThreadPoolExecutor(max_workers=PREPROCESS_WORKERS)

            for bi, batch_files in enumerate(batches):
                futs = [(fp, pool.submit(preprocess_single, fp)) for fp in batch_files]

                arrays, valid_fps = [], []
                for fp, fut in futs:
                    arr = fut.result()
                    if arr is not None:
                        arrays.append(arr)
                        valid_fps.append(fp)

                if arrays:
                    try:
                        scores = detector.run_batch_inference(np.stack(arrays))
                    except Exception as e:
                        logger.debug("batch 推理异常: %s", e)
                        scores = [0.0] * len(arrays)
                    for fp, score in zip(valid_fps, scores):
                        new_scores[fp] = score
                        fhash = file_hashes.get(fp)
                        if fhash:
                            doc_cache[fhash] = score

                pbar.update(len(batch_files))
                processed += len(batch_files)

                if processed >= save_interval:
                    with open(cache_path, "w", encoding="utf-8") as f:
                        json.dump(doc_cache, f)
                    processed = 0

            pool.shutdown(wait=False)

        # 最终保存缓存
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(doc_cache, f)
        logger.info("文档缓存已保存: %d 条 → %s", len(doc_cache), cache_path)

    # 5. 汇总结果
    all_scores = {**cached_scores, **new_scores}
    doc_files = [(fp, s) for fp, s in all_scores.items() if s >= DOC_THRESHOLD]
    doc_files.sort(key=lambda x: -x[1])

    logger.info("=" * 60)
    logger.info("检测结果统计:")
    logger.info("  总扫描图片: %d", len(all_images))
    logger.info("  截图（直接归类）: %d", len(screenshot_images))
    logger.info("  文档图片（模型检出）: %d (阈值 >= %.2f)", len(doc_files), DOC_THRESHOLD)
    logger.info("  普通照片: %d", len(all_images) - len(doc_files))

    if doc_files:
        logger.info("\nTop 20 文档图片（最高分）:")
        for score, fp in [(s, f) for f, s in doc_files[:20]]:
            rel = os.path.relpath(fp, SOURCE_DIR)
            logger.info("  %.3f %s", score, rel)

    # 6. 复制文档图片到 All_7_文档图片
    logger.info("\n" + "=" * 60)
    logger.info("复制文档图片到 %s", DOC_OUTPUT_DIR)

    copied = 0
    skipped = 0

    # 6a. 截图 → All_7_文档图片/截图/
    for fp in screenshot_images:
        rel = os.path.relpath(fp, SOURCE_DIR)
        dest = os.path.join(SCREENSHOT_OUTPUT_DIR, rel)
        if os.path.exists(dest):
            skipped += 1
            continue
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        try:
            os.link(fp, dest)
            copied += 1
        except OSError:
            try:
                shutil.copy2(fp, dest)
                copied += 1
            except Exception as e:
                logger.debug("复制截图失败 %s: %s", fp, e)

    logger.info("截图硬链接/复制: %d 个, 已存在跳过: %d 个", copied, skipped)

    # 6b. 模型检出的文档 → All_7_文档图片/{原始相对路径}
    doc_copied = 0
    doc_skipped = 0
    for fp, score in doc_files:
        rel = os.path.relpath(fp, SOURCE_DIR)
        dest = os.path.join(DOC_OUTPUT_DIR, rel)
        if os.path.exists(dest):
            doc_skipped += 1
            continue
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        try:
            os.link(fp, dest)
            doc_copied += 1
        except OSError:
            try:
                shutil.copy2(fp, dest)
                doc_copied += 1
            except Exception as e:
                logger.debug("复制文档失败 %s: %s", fp, e)

    logger.info("文档图片硬链接/复制: %d 个, 已存在跳过: %d 个", doc_copied, doc_skipped)

    total_in_doc = copied + doc_copied
    logger.info("\n" + "=" * 60)
    logger.info("完成!")
    logger.info("  All_7_文档图片 总计: %d 个文件 (%d 截图 + %d 模型检出)",
                total_in_doc, copied, doc_copied)
    logger.info("  文档缓存: %d 条 → %s", len(doc_cache), cache_path)
    logger.info("  硬链接不占额外磁盘空间")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
