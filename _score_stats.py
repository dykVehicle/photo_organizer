# -*- coding: utf-8 -*-
"""分析 NSFW 缓存分数分布"""
import os, sys, json
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

cache_path = r"H:\All_相册_20260225_V2\.photo_organizer\nsfw_score_cache.json"
with open(cache_path, "r", encoding="utf-8") as f:
    cache = json.load(f)

print("=== NSFW 缓存分数分布 (共 %d 条) ===" % len(cache))

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

print("\n=== 不同阈值下的 NSFW 文件数 ===")
for threshold in [0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]:
    count = sum(1 for v in cache.values() if v >= threshold)
    print("  >= %.1f : %5d" % (threshold, count))
