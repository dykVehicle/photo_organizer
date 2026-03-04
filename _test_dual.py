# -*- coding: utf-8 -*-
"""测试两阶段方案（Falconsai 粗筛 + NudeNet 精检）对 Falconsai 之前判为 NSFW 的前 500 个文件"""
import sys, os, time
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

LIST_FILE = r"H:\All_相册_20260225_V2\nsfw_file_list.txt"
OUT_FILE = r"H:\All_相册_20260225_V2\nsfw_dual_top500.txt"

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp", ".tiff", ".tif", ".heic"}
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".wmv", ".flv", ".3gp", ".mpg", ".mpeg", ".m4v", ".mov"}

files = []
with open(LIST_FILE, "r", encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(" | ", 1)
        if len(parts) == 2:
            fp = parts[1]
            if os.path.isfile(fp):
                files.append((float(parts[0]), fp))
        if len(files) >= 500:
            break

print("取到 %d 个文件" % len(files))

from nsfw_detector import NsfwDetector
detector = NsfwDetector(threshold=0.5)

results = []
t0 = time.time()

for i, (old_falcon_score, fp) in enumerate(files):
    ext = os.path.splitext(fp)[1].lower()
    if ext in VIDEO_EXTS:
        score = detector.predict_video(fp, max_frames=3)
        mtype = "video"
    else:
        score = detector.predict_image(fp)
        mtype = "image"

    results.append((old_falcon_score, score, mtype, fp))
    if (i + 1) % 50 == 0 or i == 0:
        elapsed = time.time() - t0
        print("  检测: %d/%d (%.1f秒)" % (i + 1, len(files), elapsed))

elapsed = time.time() - t0
print("\n完成: %.1f 秒" % elapsed)

nsfw = [(ofs, s, mt, fp) for ofs, s, mt, fp in results if s >= 0.5]
clean = [(ofs, s, mt, fp) for ofs, s, mt, fp in results if s < 0.5]

print("\n=== 两阶段方案结果 ===")
print("NSFW:  %d" % len(nsfw))
print("CLEAN: %d" % len(clean))

# 按 NudeNet 分数降序
nsfw.sort(key=lambda x: -x[1])

with open(OUT_FILE, "w", encoding="utf-8") as f:
    f.write("# 两阶段检测结果（Falconsai 粗筛 + NudeNet 精检）\n")
    f.write("# NSFW: %d, CLEAN: %d (共 %d)\n" % (len(nsfw), len(clean), len(results)))
    f.write("# 格式: NudeNet分数 | 类型 | 文件路径\n\n")
    for ofs, s, mt, fp in nsfw:
        f.write("%.4f (%s) %s\n" % (s, mt, fp))

print("\n结果已保存: %s" % OUT_FILE)

print("\n--- NSFW 列表（按置信度降序）---")
for ofs, s, mt, fp in nsfw:
    print("  NudeNet=%.2f [%s] %s" % (s, mt, os.path.basename(fp)))
