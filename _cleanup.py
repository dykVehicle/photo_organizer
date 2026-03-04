# -*- coding: utf-8 -*-
"""清空 All_6_NSFW 目录 + 分析 NSFW 缓存分数分布"""
import os, sys, json, shutil
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 1. 清空 All_6_NSFW
nsfw_dir = r"H:\All_相册_20260225_V2\All_6_NSFW"
if os.path.exists(nsfw_dir):
    cnt = sum(len(fs) for _, _, fs in os.walk(nsfw_dir))
    print("All_6_NSFW 文件数: %d, 正在删除..." % cnt)
    shutil.rmtree(nsfw_dir)
    print("All_6_NSFW 已删除")
else:
    print("All_6_NSFW 不存在，无需清理")

# 2. 分析 NSFW 缓存分数分布
cache_path = r"H:\All_相册_20260225_V2\.photo_organizer\nsfw_score_cache.json"
if not os.path.isfile(cache_path):
    print("Cache not found:", cache_path)
    sys.exit(0)

with open(cache_path, "r", encoding="utf-8") as f:
    cache = json.load(f)

print("\n=== NSFW 缓存分数分布 (共 %d 条) ===" % len(cache))

# 细粒度区间
bins = [
    (0.0, 0.1, "<0.1 (干净)"),
    (0.1, 0.2, "0.1-0.2"),
    (0.2, 0.3, "0.2-0.3"),
    (0.3, 0.4, "0.3-0.4"),
    (0.4, 0.5, "0.4-0.5"),
    (0.5, 0.6, "0.5-0.6"),
    (0.6, 0.7, "0.6-0.7"),
    (0.7, 0.8, "0.7-0.8"),
    (0.8, 0.9, "0.8-0.9"),
    (0.9, 1.01, "0.9-1.0 (高置信)"),
]

for lo, hi, label in bins:
    count = sum(1 for v in cache.values() if lo <= v < hi)
    if count > 0:
        print("  %-22s : %5d" % (label, count))

# 各阈值下的 NSFW 文件数
print("\n=== 不同阈值下的 NSFW 文件数 ===")
for threshold in [0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]:
    count = sum(1 for v in cache.values() if v >= threshold)
    print("  >= %.1f : %5d" % (threshold, count))

# 列出 0.5-0.7 区间的文件（可能是误检），采样前 20 个
print("\n=== 0.5-0.7 区间采样（可能误检）前 20 ===")
samples = [(h, v) for h, v in cache.items() if 0.5 <= v < 0.7]
samples.sort(key=lambda x: x[1], reverse=True)
for h, v in samples[:20]:
    print("  score=%.4f  hash=%s" % (v, h))
