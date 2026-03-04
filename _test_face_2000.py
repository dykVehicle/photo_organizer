"""人脸识别测试: 从有人脸的图片中挑选2000张，聚类并按人物创建硬链接文件夹。"""
import hashlib
import json
import logging
import os
import random
import shutil
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
logger = logging.getLogger("test_face_2k")

SOURCE_DIR = r"H:\All_相册_20260305"
CACHE_DIR = r"H:\.photo_organizer"
TEST_DIR = r"H:\__test_face_2000"
ALBUM_DIR = os.path.join(TEST_DIR, "All_F_人物相册")
TEST_CACHE_DIR = os.path.join(TEST_DIR, ".cache")
NSFW_DIR_NAME = "All_7_NSFW"
HASH_SAMPLE_SIZE = 16 * 1024
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif", ".heic", ".heif"}
MAX_PHOTOS = 2000


def _fast_hash(filepath, file_size):
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


class FakeRecord:
    def __init__(self, status, file_hash, destination):
        self.status = status
        self.file_hash = file_hash
        self.destination = destination


def main():
    logger.info("=" * 60)
    logger.info("人脸识别测试: 2000 张有人脸的照片")
    logger.info("=" * 60)

    # 1. 加载嵌入缓存，获取有人脸的哈希列表
    emb_cache_path = os.path.join(CACHE_DIR, "face_embedding_cache.json")
    logger.info("加载嵌入缓存: %s", emb_cache_path)
    with open(emb_cache_path, "r", encoding="utf-8") as f:
        cache_data = json.load(f)
    entries = cache_data.get("entries", {})
    face_hashes = {h for h, v in entries.items() if not v.get("no_face", True)}
    logger.info("缓存中有人脸的图片: %d 张", len(face_hashes))

    # 2. 扫描源目录，找到匹配的文件
    nsfw_prefix = os.path.normcase(os.path.join(SOURCE_DIR, NSFW_DIR_NAME) + os.sep)
    logger.info("扫描源目录，匹配有人脸的文件...")
    t0 = time.time()
    matched = []
    for root, dirs, files in os.walk(SOURCE_DIR):
        if os.path.normcase(root + os.sep).startswith(nsfw_prefix):
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
                continue
            fhash = _fast_hash(fpath, fsize)
            if fhash in face_hashes:
                matched.append((fhash, fpath))
    scan_time = time.time() - t0
    logger.info("匹配到 %d 张有人脸的文件，耗时 %.1f 秒", len(matched), scan_time)

    # 3. 随机挑选 MAX_PHOTOS 张
    if len(matched) > MAX_PHOTOS:
        selected = random.sample(matched, MAX_PHOTOS)
    else:
        selected = matched
    logger.info("选中 %d 张照片用于测试", len(selected))

    # 4. 清理旧测试目录，复制文件
    if os.path.isdir(TEST_DIR):
        logger.info("清理旧测试目录...")
        shutil.rmtree(TEST_DIR, ignore_errors=True)

    photos_dir = os.path.join(TEST_DIR, "photos")
    os.makedirs(photos_dir, exist_ok=True)
    os.makedirs(TEST_CACHE_DIR, exist_ok=True)

    logger.info("复制文件到临时目录...")
    records = []
    seen_names = {}
    for fhash, src in selected:
        fname = os.path.basename(src)
        if fname in seen_names:
            seen_names[fname] += 1
            base, ext = os.path.splitext(fname)
            fname = f"{base}_{seen_names[fname]}{ext}"
        else:
            seen_names[fname] = 0
        dst = os.path.join(photos_dir, fname)
        shutil.copy2(src, dst)
        records.append(FakeRecord("ok", fhash, dst))

    logger.info("已复制 %d 张照片到 %s", len(records), photos_dir)

    # 5. 复制嵌入缓存到测试缓存目录（复用已有缓存）
    test_emb_cache = os.path.join(TEST_CACHE_DIR, "face_embedding_cache.json")
    shutil.copy2(emb_cache_path, test_emb_cache)
    logger.info("已复制嵌入缓存到测试目录")

    # 6. 运行人脸聚类 + 创建相册
    logger.info("=" * 60)
    logger.info("运行人脸聚类...")
    from face_detector import FaceAnalyzer
    analyzer = FaceAnalyzer()

    t1 = time.time()
    result = analyzer.run_pipeline(
        records=records,
        dest_dir=ALBUM_DIR,
        cache_dir=TEST_CACHE_DIR,
        max_workers=4,
        skip_nsfw_dir="",
        min_cluster_size=3,
        dry_run=False,
        cache_only=False,
    )
    elapsed = time.time() - t1

    logger.info("=" * 60)
    logger.info("测试结果:")
    logger.info("  输入图片:     %d", len(records))
    logger.info("  检测人脸:     %d", result.total_faces)
    logger.info("  识别人物:     %d", result.total_persons)
    logger.info("  硬链接:       %d", result.total_links)
    logger.info("  离群人脸:     %d", result.outlier_faces)
    logger.info("  总耗时:       %.1f 秒", elapsed)

    # 7. 统计每个人物文件夹的照片数
    if os.path.isdir(ALBUM_DIR):
        person_dirs = sorted([
            d for d in os.listdir(ALBUM_DIR)
            if os.path.isdir(os.path.join(ALBUM_DIR, d))
        ])
        logger.info("\n人物分组统计 (共 %d 个人物):", len(person_dirs))
        for pd in person_dirs[:30]:
            pd_path = os.path.join(ALBUM_DIR, pd)
            count = len([f for f in os.listdir(pd_path) if os.path.isfile(os.path.join(pd_path, f))])
            logger.info("  %s: %d 张照片", pd, count)
        if len(person_dirs) > 30:
            logger.info("  ... 还有 %d 个人物未列出", len(person_dirs) - 30)

    # 8. 验证硬链接
    link_count = 0
    valid_links = 0
    if os.path.isdir(ALBUM_DIR):
        for pd in os.listdir(ALBUM_DIR):
            pd_path = os.path.join(ALBUM_DIR, pd)
            if not os.path.isdir(pd_path):
                continue
            for fname in os.listdir(pd_path):
                fpath = os.path.join(pd_path, fname)
                if not os.path.isfile(fpath):
                    continue
                link_count += 1
                try:
                    if os.stat(fpath).st_nlink >= 2:
                        valid_links += 1
                except OSError:
                    pass
    logger.info("\n硬链接验证: %d 个文件, %d 个有效 (nlink>=2)", link_count, valid_links)

    # 9. 磁盘空间验证
    photos_size = sum(
        os.path.getsize(os.path.join(photos_dir, f))
        for f in os.listdir(photos_dir)
        if os.path.isfile(os.path.join(photos_dir, f))
    )
    album_size = 0
    if os.path.isdir(ALBUM_DIR):
        for root, dirs, files in os.walk(ALBUM_DIR):
            for f in files:
                fp = os.path.join(root, f)
                if os.path.isfile(fp):
                    album_size += os.path.getsize(fp)
    logger.info("磁盘空间: 照片 %.1f MB, 相册 %.1f MB (硬链接不占额外空间)",
                photos_size / 1024 / 1024, album_size / 1024 / 1024)

    logger.info("=" * 60)
    logger.info("测试完成! 测试目录: %s", TEST_DIR)
    logger.info("可在文件管理器中打开 %s 查看人物分组", ALBUM_DIR)


if __name__ == "__main__":
    main()
