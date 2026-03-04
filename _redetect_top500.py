# -*- coding: utf-8 -*-
"""从 nsfw_file_list.txt 取前 500 个文件，用新模型重新检测并输出结果"""
import sys, os
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

LIST_FILE = r"H:\All_相册_20260225_V2\nsfw_file_list.txt"
OUT_FILE = r"H:\All_相册_20260225_V2\nsfw_redetect_top500.txt"

# 解析文件列表
files = []
with open(LIST_FILE, "r", encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(" | ", 1)
        if len(parts) == 2:
            files.append(parts[1])
        if len(files) >= 500:
            break

print("从列表读取 %d 个文件" % len(files))

# 过滤出实际存在的文件
existing = [(i, fp) for i, fp in enumerate(files) if os.path.isfile(fp)]
print("实际存在: %d 个" % len(existing))

# 加载检测器
from nsfw_detector import NsfwDetector
detector = NsfwDetector(threshold=0.5)

# 分批检测（图片 batch，视频单独）
from nsfw_detector import _preprocess_single
import numpy as np

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp", ".tiff", ".tif", ".heic"}
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".wmv", ".flv", ".3gp", ".mpg", ".mpeg", ".m4v"}

results = []  # (idx, filepath, score, media_type)

# 收集图片和视频
images = []
videos = []
for idx, fp in existing:
    ext = os.path.splitext(fp)[1].lower()
    if ext in VIDEO_EXTS:
        videos.append((idx, fp))
    else:
        images.append((idx, fp))

print("图片: %d, 视频: %d" % (len(images), len(videos)))

# Batch 检测图片
BATCH_SIZE = 64
for batch_start in range(0, len(images), BATCH_SIZE):
    batch = images[batch_start:batch_start + BATCH_SIZE]
    paths = [fp for _, fp in batch]
    scores = detector.predict_batch(paths)
    for (idx, fp), score in zip(batch, scores):
        results.append((idx, fp, score, "image"))
    done = min(batch_start + BATCH_SIZE, len(images))
    print("  图片检测: %d/%d" % (done, len(images)), end="\r")

print()

# 逐个检测视频
for i, (idx, fp) in enumerate(videos):
    score = detector.predict_video(fp, max_frames=2)
    results.append((idx, fp, score, "video"))
    print("  视频检测: %d/%d" % (i + 1, len(videos)), end="\r")

print()

# 按原始顺序排序
results.sort(key=lambda x: x[0])

# 输出结果
nsfw_count = sum(1 for _, _, s, _ in results if s >= 0.5)
clean_count = len(results) - nsfw_count

with open(OUT_FILE, "w", encoding="utf-8") as f:
    f.write("# 重新检测结果（Falconsai 二分类模型，前 500 个文件）\n")
    f.write("# NSFW (>= 0.5): %d, CLEAN (< 0.5): %d, 总计: %d\n" % (nsfw_count, clean_count, len(results)))
    f.write("# 格式: 分数 | 类型 | 文件路径\n\n")
    for idx, fp, score, mtype in results:
        tag = "NSFW" if score >= 0.5 else "CLEAN"
        f.write("%.4f [%5s] (%s) %s\n" % (score, tag, mtype, fp))

print("结果已保存: %s" % OUT_FILE)
print("NSFW: %d, CLEAN: %d" % (nsfw_count, clean_count))

# 打印前 50 条
print("\n--- 前 50 条 ---")
for idx, fp, score, mtype in results[:50]:
    tag = "NSFW" if score >= 0.5 else "CLEAN"
    print("%.4f [%5s] (%s) %s" % (score, tag, mtype, os.path.basename(fp)))
