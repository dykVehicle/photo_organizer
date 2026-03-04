"""人脸识别端到端测试: 从真实相册中挑选照片，在临时目录中测试完整流程。

不修改 H:\All_相册_20260305\ 的任何文件。
"""
import hashlib
import json
import logging
import os
import shutil
import sys
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("test_face")

TEST_SOURCE = r"H:\All_相册_20260305"
TEST_DIR = r"H:\__test_face_recognition"
CACHE_DIR = os.path.join(TEST_DIR, ".cache")
ALBUM_DIR = os.path.join(TEST_DIR, "All_F_人物相册")

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif", ".heic", ".heif"}
MAX_PHOTOS_PER_DIR = 15
MAX_TOTAL = 50


def _file_hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def collect_test_photos():
    """从 All_1 和 All_2 子目录中各挑照片"""
    source_dirs = [
        os.path.join(TEST_SOURCE, "All_1_目标设备_手机照片"),
        os.path.join(TEST_SOURCE, "All_2_目标设备_相机照片"),
    ]
    collected = []
    for sdir in source_dirs:
        if not os.path.isdir(sdir):
            logger.warning("源目录不存在: %s", sdir)
            continue
        count = 0
        for root, dirs, files in os.walk(sdir):
            for f in files:
                ext = os.path.splitext(f)[1].lower()
                if ext not in IMAGE_EXTS:
                    continue
                fpath = os.path.join(root, f)
                if os.path.getsize(fpath) < 50_000:
                    continue
                collected.append(fpath)
                count += 1
                if count >= MAX_PHOTOS_PER_DIR:
                    break
            if count >= MAX_PHOTOS_PER_DIR:
                break
    return collected[:MAX_TOTAL]


class FakeRecord:
    def __init__(self, status, file_hash, destination):
        self.status = status
        self.file_hash = file_hash
        self.destination = destination


def main():
    logger.info("=" * 60)
    logger.info("人脸识别端到端测试")
    logger.info("=" * 60)

    # 清理旧测试数据
    if os.path.isdir(TEST_DIR):
        logger.info("清理旧测试目录: %s", TEST_DIR)
        shutil.rmtree(TEST_DIR, ignore_errors=True)

    photos_dir = os.path.join(TEST_DIR, "photos")
    os.makedirs(photos_dir, exist_ok=True)
    os.makedirs(CACHE_DIR, exist_ok=True)

    # 1. 收集并复制照片
    logger.info("步骤 1: 从源目录收集照片...")
    source_photos = collect_test_photos()
    if not source_photos:
        logger.error("未找到测试照片")
        return

    logger.info("找到 %d 张候选照片，复制到临时目录...", len(source_photos))
    records = []
    for src in source_photos:
        dst = os.path.join(photos_dir, os.path.basename(src))
        if os.path.exists(dst):
            base, ext = os.path.splitext(dst)
            dst = f"{base}_{len(records)}{ext}"
        shutil.copy2(src, dst)
        fhash = _file_hash(dst)
        records.append(FakeRecord("ok", fhash, dst))

    logger.info("已复制 %d 张照片到 %s", len(records), photos_dir)

    # 2. 运行人脸检测 + 嵌入提取
    logger.info("步骤 2: 人脸检测 + 嵌入提取...")
    from face_detector import FaceAnalyzer
    analyzer = FaceAnalyzer()

    t0 = time.time()
    result = analyzer.run_pipeline(
        records=records,
        dest_dir=ALBUM_DIR,
        cache_dir=CACHE_DIR,
        max_workers=4,
        skip_nsfw_dir="",
        min_cluster_size=2,
        dry_run=False,
    )
    elapsed = time.time() - t0

    logger.info("=" * 60)
    logger.info("测试结果:")
    logger.info("  图片数:       %d", len(records))
    logger.info("  检测人脸:     %d", result.total_faces)
    logger.info("  识别人物:     %d", result.total_persons)
    logger.info("  硬链接:       %d", result.total_links)
    logger.info("  离群人脸:     %d", result.outlier_faces)
    logger.info("  总耗时:       %.1f 秒", elapsed)

    # 3. 验证缓存
    logger.info("\n步骤 3: 验证缓存命中...")
    t1 = time.time()
    result2 = analyzer.run_pipeline(
        records=records,
        dest_dir=ALBUM_DIR + "_v2",
        cache_dir=CACHE_DIR,
        max_workers=4,
        min_cluster_size=2,
        dry_run=True,
    )
    cache_elapsed = time.time() - t1
    logger.info("  缓存命中运行耗时: %.1f 秒 (首次 %.1f 秒)", cache_elapsed, elapsed)

    # 4. 验证硬链接
    logger.info("\n步骤 4: 验证硬链接...")
    if os.path.isdir(ALBUM_DIR):
        link_count = 0
        valid_links = 0
        for person_dir in os.listdir(ALBUM_DIR):
            pd = os.path.join(ALBUM_DIR, person_dir)
            if not os.path.isdir(pd):
                continue
            for fname in os.listdir(pd):
                fpath = os.path.join(pd, fname)
                if not os.path.isfile(fpath):
                    continue
                link_count += 1
                try:
                    stat = os.stat(fpath)
                    if stat.st_nlink >= 2:
                        valid_links += 1
                except OSError:
                    pass
        logger.info("  硬链接文件: %d, 有效(nlink>=2): %d", link_count, valid_links)
    else:
        logger.info("  未创建相册目录")

    # 5. 检查 face_index.json
    index_path = os.path.join(ALBUM_DIR, "face_index.json")
    if os.path.isfile(index_path):
        with open(index_path, "r", encoding="utf-8") as f:
            idx = json.load(f)
        logger.info("  face_index.json: %d 个人物", len(idx.get("persons", [])))
    else:
        logger.info("  face_index.json 未生成")

    # 6. 检查缓存文件
    emb_cache = os.path.join(CACHE_DIR, "face_embedding_cache.json")
    cluster_cache = os.path.join(CACHE_DIR, "face_clustering_cache.json")
    for name, path in [("嵌入缓存", emb_cache), ("聚类缓存", cluster_cache)]:
        if os.path.isfile(path):
            size = os.path.getsize(path)
            logger.info("  %s: %.1f KB", name, size / 1024)
        else:
            logger.info("  %s: 未生成", name)

    logger.info("=" * 60)
    logger.info("测试完成! 测试目录: %s", TEST_DIR)
    logger.info("(可手动检查后删除测试目录)")


if __name__ == "__main__":
    main()
