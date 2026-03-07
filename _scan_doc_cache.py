"""全量文档检测扫描: 扫描已整理目录，缓存检测结果，并将文档图片硬链接到 All_7_文档图片

使用双模型集成 (docornot + doctype) 条件融合检测。
"""
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

SOURCE_DIR = r"H:\All_相册_20260307"
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
BATCH_SIZE = 48
CHUNK_SIZE = 12
PREPROCESS_WORKERS = 6
PREFETCH = 4


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
    from doc_detector import CACHE_MODEL_VERSION

    logger.info("=" * 60)
    logger.info("全量文档检测扫描 [%s]", CACHE_MODEL_VERSION)
    logger.info("源目录: %s", SOURCE_DIR)
    logger.info("缓存目录: %s", CACHE_DIR)
    logger.info("输出目录: %s", DOC_OUTPUT_DIR)
    logger.info("阈值: %.2f", DOC_THRESHOLD)
    logger.info("=" * 60)

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

    os.makedirs(CACHE_DIR, exist_ok=True)
    cache_path = os.path.join(CACHE_DIR, "document_score_cache.json")
    doc_cache = {}
    if os.path.isfile(cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and data.get("_model") == CACHE_MODEL_VERSION:
                doc_cache = {k: v for k, v in data.items() if not k.startswith("_")}
                logger.info("文档缓存已加载 [%s]: %d 条", CACHE_MODEL_VERSION, len(doc_cache))
            else:
                old_ver = data.get("_model", "单模型") if isinstance(data, dict) else "单模型"
                logger.info("文档缓存版本不匹配 (%s → %s)，将重建", old_ver, CACHE_MODEL_VERSION)
        except Exception as e:
            logger.warning("文档缓存加载失败: %s", e)

    from doc_detector import fuse_score as _fuse

    cached_scores = {}
    need_detect = []
    for fp, fhash in file_hashes.items():
        if fhash in doc_cache:
            cached = doc_cache[fhash]
            if isinstance(cached, (list, tuple)) and len(cached) == 2:
                cached_scores[fp] = _fuse(cached[0], cached[1])
            else:
                cached_scores[fp] = float(cached)
        else:
            need_detect.append(fp)

    logger.info("缓存命中: %d, 需检测: %d", len(cached_scores), len(need_detect))

    new_scores = {}
    if need_detect:
        from doc_detector import DocumentDetector, fuse_score
        detector = DocumentDetector(threshold=DOC_THRESHOLD)
        detector._ensure_model()

        logger.info("开始文档检测 [%s]: %d 张图片 (batch=%d, %d 预处理线程)",
                    CACHE_MODEL_VERSION, len(need_detect), BATCH_SIZE, PREPROCESS_WORKERS)

        batches = [need_detect[i:i + BATCH_SIZE] for i in range(0, len(need_detect), BATCH_SIZE)]
        processed = 0
        save_interval = 2000

        def _save():
            save_data = dict(doc_cache)
            save_data["_model"] = CACHE_MODEL_VERSION
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(save_data, f)

        with tqdm(total=len(need_detect), desc="文档检测", unit="个") as pbar:
            from doc_detector import preprocess_chunk
            from concurrent.futures import ProcessPoolExecutor as _PPE
            try:
                pool = _PPE(max_workers=PREPROCESS_WORKERS)
            except Exception:
                logger.info("ProcessPoolExecutor 不可用，回退到线程池")
                pool = ThreadPoolExecutor(max_workers=PREPROCESS_WORKERS)

            def _submit_batch(batch_fps):
                chunks = []
                for ci in range(0, len(batch_fps), CHUNK_SIZE):
                    chunk_fps = batch_fps[ci:ci + CHUNK_SIZE]
                    chunks.append((chunk_fps, pool.submit(
                        preprocess_chunk, chunk_fps)))
                return chunks

            from collections import deque
            pq = deque()
            pf_end = min(PREFETCH, len(batches))
            for pi in range(pf_end):
                pq.append(_submit_batch(batches[pi]))
            next_sub = pf_end

            for bi in range(len(batches)):
                current = pq.popleft()
                if next_sub < len(batches):
                    pq.append(_submit_batch(batches[next_sub]))
                    next_sub += 1

                arrays_a, arrays_b, valid_fps = [], [], []
                for chunk_fps, fut in current:
                    results = fut.result()
                    for fp, res in zip(chunk_fps, results):
                        if res is not None:
                            arr_a, arr_b = res
                            arrays_a.append(arr_a)
                            arrays_b.append(arr_b)
                            valid_fps.append(fp)

                if arrays_a:
                    try:
                        raw_pairs = detector.run_batch_inference(
                            np.stack(arrays_a), np.stack(arrays_b),
                        )
                    except Exception as e:
                        logger.debug("batch 推理异常: %s", e)
                        raw_pairs = [(0.0, 0.0)] * len(arrays_a)
                    for fp, (sa, sb) in zip(valid_fps, raw_pairs):
                        new_scores[fp] = fuse_score(sa, sb)
                        fhash = file_hashes.get(fp)
                        if fhash:
                            doc_cache[fhash] = [sa, sb]

                pbar.update(len(batches[bi]))
                processed += len(batches[bi])

                if processed >= save_interval:
                    _save()
                    processed = 0

            pool.shutdown(wait=True)

        _save()
        logger.info("文档缓存已保存 [%s]: %d 条 → %s",
                    CACHE_MODEL_VERSION, len(doc_cache), cache_path)

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

    logger.info("\n" + "=" * 60)
    logger.info("复制文档图片到 %s", DOC_OUTPUT_DIR)

    copied = 0
    skipped = 0

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
    logger.info("  文档缓存 [%s]: %d 条 → %s", CACHE_MODEL_VERSION, len(doc_cache), cache_path)
    logger.info("  硬链接不占额外磁盘空间")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
