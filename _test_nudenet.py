# -*- coding: utf-8 -*-
"""用 NudeNet 目标检测方法重新检测前500个文件，对比 Falconsai 分类模型"""
import sys, os, time
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

LIST_FILE = r"H:\All_相册_20260225_V2\nsfw_file_list.txt"
OUT_FILE = r"H:\All_相册_20260225_V2\nsfw_nudenet_top500.txt"

# 真正 explicit 的类别（只有这些才算 NSFW）
NSFW_CLASSES = {
    "FEMALE_GENITALIA_EXPOSED",
    "MALE_GENITALIA_EXPOSED",
    "FEMALE_BREAST_EXPOSED",
    "ANUS_EXPOSED",
}

# 可选的次要类别（可以降低阈值要求）
SECONDARY_CLASSES = {
    "BUTTOCKS_EXPOSED",
}

MIN_CONFIDENCE = 0.45  # 主要类别的最低置信度
MIN_CONFIDENCE_SECONDARY = 0.6  # 次要类别的最低置信度

# 解析文件列表（只取图片，NudeNet 不直接支持视频）
files = []
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp", ".tiff", ".tif", ".heic"}
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".wmv", ".flv", ".3gp", ".mpg", ".mpeg", ".m4v"}

with open(LIST_FILE, "r", encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(" | ", 1)
        if len(parts) == 2:
            fp = parts[1]
            ext = os.path.splitext(fp)[1].lower()
            if ext in IMAGE_EXTS and os.path.isfile(fp):
                files.append((float(parts[0]), fp))
        if len(files) >= 500:
            break

print("取到 %d 个图片文件" % len(files))

import cv2
import numpy as np

from nudenet import NudeDetector
detector = NudeDetector()

def read_image_unicode(path):
    """用 numpy 方式读取含中文路径的图片，绕过 OpenCV 编码问题"""
    with open(path, "rb") as f:
        data = np.frombuffer(f.read(), np.uint8)
    return cv2.imdecode(data, cv2.IMREAD_COLOR)

results = []
t0 = time.time()

for i, (old_score, fp) in enumerate(files):
    try:
        img = read_image_unicode(fp)
        if img is None:
            detections = []
        else:
            detections = detector.detect(img)
    except Exception as e:
        detections = []

    # 判断是否 NSFW
    nsfw_parts = []
    for d in detections:
        cls = d.get("class", "")
        score = d.get("score", 0)
        if cls in NSFW_CLASSES and score >= MIN_CONFIDENCE:
            nsfw_parts.append((cls, score))
        elif cls in SECONDARY_CLASSES and score >= MIN_CONFIDENCE_SECONDARY:
            nsfw_parts.append((cls, score))

    is_nsfw = len(nsfw_parts) > 0
    max_score = max((s for _, s in nsfw_parts), default=0.0)

    results.append({
        "path": fp,
        "old_score": old_score,
        "is_nsfw": is_nsfw,
        "max_score": max_score,
        "parts": nsfw_parts,
        "all_detections": detections,
    })

    if (i + 1) % 50 == 0 or i == 0:
        elapsed = time.time() - t0
        speed = (i + 1) / elapsed if elapsed > 0 else 0
        print("  检测: %d/%d (%.1f 个/秒)" % (i + 1, len(files), speed))

elapsed = time.time() - t0
print("\n检测完成: %.1f 秒, %.1f 个/秒" % (elapsed, len(files) / elapsed))

nsfw_count = sum(1 for r in results if r["is_nsfw"])
clean_count = len(results) - nsfw_count

print("\n=== NudeNet 结果 ===")
print("NSFW:  %d" % nsfw_count)
print("CLEAN: %d" % clean_count)
print("(旧 Falconsai 模型: 500/500 = 100%% NSFW)")
print("(NudeNet 目标检测: %d/500 = %.1f%% NSFW)" % (nsfw_count, nsfw_count / 5))

# 写入结果
with open(OUT_FILE, "w", encoding="utf-8") as f:
    f.write("# NudeNet 目标检测 vs Falconsai 分类 对比结果\n")
    f.write("# NSFW 类别: %s\n" % ", ".join(sorted(NSFW_CLASSES | SECONDARY_CLASSES)))
    f.write("# 主要类别置信度阈值: %.2f, 次要类别: %.2f\n" % (MIN_CONFIDENCE, MIN_CONFIDENCE_SECONDARY))
    f.write("# NudeNet NSFW: %d, CLEAN: %d (共 %d)\n\n" % (nsfw_count, clean_count, len(results)))

    for r in results:
        tag = "NSFW" if r["is_nsfw"] else "CLEAN"
        parts_str = ", ".join("%s:%.2f" % (c, s) for c, s in r["parts"]) if r["parts"] else "-"
        f.write("[%5s] falcon=%.4f nudenet_parts=[%s] %s\n" % (
            tag, r["old_score"], parts_str, r["path"]))

print("\n结果已保存: %s" % OUT_FILE)

# 统计检测到的部位分布
from collections import Counter
part_counter = Counter()
for r in results:
    for cls, _ in r["parts"]:
        part_counter[cls] += 1

print("\n检测到的 NSFW 部位分布:")
for cls, cnt in part_counter.most_common():
    print("  %s: %d" % (cls, cnt))

# 打印前 30 个 NSFW 和前 10 个 CLEAN
print("\n--- 前 20 个 NSFW ---")
nsfw_results = [r for r in results if r["is_nsfw"]]
for r in nsfw_results[:20]:
    parts_str = ", ".join("%s:%.2f" % (c, s) for c, s in r["parts"])
    print("  falcon=%.4f parts=[%s] %s" % (r["old_score"], parts_str, os.path.basename(r["path"])))

print("\n--- 前 20 个 CLEAN (Falconsai 误检) ---")
clean_results = [r for r in results if not r["is_nsfw"]]
for r in clean_results[:20]:
    print("  falcon=%.4f %s" % (r["old_score"], os.path.basename(r["path"])))
