"""文档图片检测集成测试: 验证检测 + 路由 + 缓存"""
import os, sys, logging, shutil, random, time
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("test_doc")

SOURCE_DIR = r"H:\All_相册_20260305"
TEST_DIR = r"H:\__test_doc_detect"
NSFW_DIR_NAME = "All_7_NSFW"
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif", ".heic", ".heif"}
MAX_PHOTOS = 200

def main():
    logger.info("=" * 60)
    logger.info("文档图片检测集成测试")
    logger.info("=" * 60)

    # 1. 先测试 DocumentDetector 单独工作
    logger.info("--- 测试 1: DocumentDetector 单图推理 ---")
    from doc_detector import DocumentDetector, check_dependencies
    check_dependencies()
    detector = DocumentDetector(threshold=0.5)

    # 随机从源目录挑选几张图片测试
    test_images = []
    nsfw_prefix = os.path.normcase(os.path.join(SOURCE_DIR, NSFW_DIR_NAME) + os.sep)
    for root, dirs, files in os.walk(SOURCE_DIR):
        if os.path.normcase(root + os.sep).startswith(nsfw_prefix):
            continue
        for f in files:
            ext = os.path.splitext(f)[1].lower()
            if ext in IMAGE_EXTS:
                test_images.append(os.path.join(root, f))
        if len(test_images) > 5000:
            break

    if not test_images:
        logger.error("源目录中未找到图片")
        return

    sample = random.sample(test_images, min(10, len(test_images)))
    doc_count = 0
    for fp in sample:
        score = detector.predict_image(fp)
        label = "文档" if score >= 0.5 else "照片"
        doc_count += 1 if score >= 0.5 else 0
        logger.info("  %.3f [%s] %s", score, label, os.path.basename(fp))

    logger.info("单图测试完成: %d 张中 %d 张被判定为文档", len(sample), doc_count)

    # 2. 批量推理测试
    logger.info("\n--- 测试 2: batch 推理 (%d 张) ---", min(MAX_PHOTOS, len(test_images)))
    batch_sample = random.sample(test_images, min(MAX_PHOTOS, len(test_images)))

    from doc_detector import preprocess_single
    import numpy as np
    from concurrent.futures import ThreadPoolExecutor

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(preprocess_single, batch_sample))

    arrays, valid_paths = [], []
    for fp, arr in zip(batch_sample, results):
        if arr is not None:
            arrays.append(arr)
            valid_paths.append(fp)

    preprocess_time = time.time() - t0
    logger.info("预处理: %d/%d 成功, 耗时 %.1f 秒", len(arrays), len(batch_sample), preprocess_time)

    t1 = time.time()
    BATCH_SIZE = 64
    all_scores = []
    for i in range(0, len(arrays), BATCH_SIZE):
        batch = np.stack(arrays[i:i + BATCH_SIZE])
        scores = detector.run_batch_inference(batch)
        all_scores.extend(scores)
    infer_time = time.time() - t1
    logger.info("推理: %d 张, 耗时 %.1f 秒 (%.0f 张/秒)", len(all_scores), infer_time, len(all_scores) / infer_time if infer_time > 0 else 0)

    doc_found = sum(1 for s in all_scores if s >= 0.5)
    logger.info("检出文档图片: %d/%d (%.1f%%)", doc_found, len(all_scores), doc_found / len(all_scores) * 100 if all_scores else 0)

    # 展示 top 10 最高分
    scored = sorted(zip(all_scores, valid_paths), key=lambda x: -x[0])
    logger.info("\nTop 10 文档图片（最高分）:")
    for score, fp in scored[:10]:
        logger.info("  %.3f %s", score, os.path.basename(fp))

    logger.info("\nTop 10 照片（最低分）:")
    for score, fp in scored[-10:]:
        logger.info("  %.3f %s", score, os.path.basename(fp))

    # 3. dry-run 验证路由
    logger.info("\n--- 测试 3: dry-run 路由验证 ---")
    if os.path.isdir(TEST_DIR):
        shutil.rmtree(TEST_DIR, ignore_errors=True)
    os.makedirs(TEST_DIR, exist_ok=True)

    # 复制一些测试文件
    test_photos_dir = os.path.join(TEST_DIR, "source")
    os.makedirs(test_photos_dir, exist_ok=True)

    # 挑选 top 5 文档 + top 5 照片 + 一些截图
    doc_files = [(s, p) for s, p in scored[:5]]
    photo_files = [(s, p) for s, p in scored[-5:]]
    screenshot_files = [fp for fp in test_images if "screenshot" in os.path.basename(fp).lower()][:3]

    test_sources = []
    for score, fp in doc_files + photo_files:
        dst = os.path.join(test_photos_dir, os.path.basename(fp))
        if not os.path.exists(dst):
            shutil.copy2(fp, dst)
            test_sources.append(dst)
    for fp in screenshot_files:
        dst = os.path.join(test_photos_dir, os.path.basename(fp))
        if not os.path.exists(dst):
            shutil.copy2(fp, dst)
            test_sources.append(dst)

    logger.info("测试文件: %d 个（%d 文档 + %d 照片 + %d 截图）",
                len(test_sources), len(doc_files), len(photo_files), len(screenshot_files))

    # 运行 dry-run
    output_dir = os.path.join(TEST_DIR, "output")
    cmd = (f'python main.py --dry-run --copy-all --copy-unknown-photo '
           f'--document --document-threshold 0.5 '
           f'--no-nsfw --no-face '
           f'--output-dir "{output_dir}" '
           f'--scan-dirs "{test_photos_dir}"')
    logger.info("执行: %s", cmd)

    import subprocess
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", cwd=os.path.dirname(__file__))
    logger.info("stdout:\n%s", r.stdout[-2000:] if len(r.stdout) > 2000 else r.stdout)
    if r.returncode != 0:
        logger.error("stderr:\n%s", r.stderr[-2000:] if len(r.stderr) > 2000 else r.stderr)

    logger.info("=" * 60)
    logger.info("测试完成! 测试目录: %s", TEST_DIR)

if __name__ == "__main__":
    main()
