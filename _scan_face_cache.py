"""全量人脸嵌入缓存预热: 扫描 H:\All_相册_20260305\ 全目录图片，检测人脸并存缓存。

不创建相册、不移动文件，仅生成 face_embedding_cache.json。
"""
import logging
import os
import sys
import time

try:
    import xxhash
    _USE_XXHASH = True
except ImportError:
    _USE_XXHASH = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("scan_face")

SOURCE_DIR = r"H:\All_相册_20260305"
CACHE_DIR = r"H:\.photo_organizer"
NSFW_DIR_NAME = "All_7_NSFW"
HASH_SAMPLE_SIZE = 16 * 1024

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif", ".heic", ".heif"}


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


class FakeRecord:
    def __init__(self, status, file_hash, destination):
        self.status = status
        self.file_hash = file_hash
        self.destination = destination


def main():
    logger.info("=" * 60)
    logger.info("全量人脸嵌入缓存预热")
    logger.info("源目录: %s", SOURCE_DIR)
    logger.info("缓存目录: %s", CACHE_DIR)
    logger.info("=" * 60)

    os.makedirs(CACHE_DIR, exist_ok=True)

    nsfw_prefix = os.path.normcase(os.path.join(SOURCE_DIR, NSFW_DIR_NAME) + os.sep)

    logger.info("扫描图片文件...")
    t_scan = time.time()
    records = []
    skipped_nsfw = 0
    skipped_small = 0
    for root, dirs, files in os.walk(SOURCE_DIR):
        if os.path.normcase(root + os.sep).startswith(nsfw_prefix):
            skipped_nsfw += len([f for f in files if os.path.splitext(f)[1].lower() in IMAGE_EXTS])
            continue
        for f in files:
            ext = os.path.splitext(f)[1].lower()
            if ext not in IMAGE_EXTS:
                continue
            fpath = os.path.join(root, f)
            try:
                fsize = os.path.getsize(fpath)
            except OSError:
                continue
            if fsize < 10_000:
                skipped_small += 1
                continue
            fhash = _fast_hash(fpath, fsize)
            records.append(FakeRecord("ok", fhash, fpath))

    scan_elapsed = time.time() - t_scan
    logger.info("找到 %d 张图片（跳过 NSFW %d, 小图 %d），哈希耗时 %.1f 秒",
                len(records), skipped_nsfw, skipped_small, scan_elapsed)

    if not records:
        logger.info("无图片可处理")
        return

    from face_detector import FaceAnalyzer
    analyzer = FaceAnalyzer()

    t0 = time.time()
    result = analyzer.run_pipeline(
        records=records,
        dest_dir="",
        cache_dir=CACHE_DIR,
        max_workers=4,
        skip_nsfw_dir="",
        min_cluster_size=2,
        dry_run=True,
        cache_only=True,
    )
    elapsed = time.time() - t0

    logger.info("=" * 60)
    logger.info("扫描完成!")
    logger.info("  图片总数: %d", len(records))
    logger.info("  检测人脸: %d", result.total_faces)
    logger.info("  总耗时:   %.1f 秒 (%.1f 分钟)", elapsed, elapsed / 60)
    logger.info("  缓存位置: %s", os.path.join(CACHE_DIR, "face_embedding_cache.json"))
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
