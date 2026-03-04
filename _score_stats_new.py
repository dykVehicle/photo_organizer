# -*- coding: utf-8 -*-
"""新模型 NSFW 分数分布统计"""
import sys, os, json
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

cache_path = r"H:\All_相册_20260225_V2\.photo_organizer\nsfw_score_cache.json"
with open(cache_path, "r", encoding="utf-8") as f:
    cache = json.load(f)

print("总缓存条目:", len(cache))

bins = [0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.01]
labels = ["[0.0-0.1)", "[0.1-0.2)", "[0.2-0.3)", "[0.3-0.4)", "[0.4-0.5)",
          "[0.5-0.6)", "[0.6-0.7)", "[0.7-0.8)", "[0.8-0.9)", "[0.9-1.0]"]
counts = [0] * len(labels)

for score in cache.values():
    for i in range(len(bins) - 1):
        if bins[i] <= score < bins[i + 1]:
            counts[i] += 1
            break

print("\n分数分布:")
for label, count in zip(labels, counts):
    bar = "#" * min(count // 100, 80)
    print("  %s  %6d  %s" % (label, count, bar))

nsfw_count = sum(1 for s in cache.values() if s >= 0.5)
print("\nNSFW (>= 0.5):", nsfw_count)
print("CLEAN (< 0.5):", len(cache) - nsfw_count)
