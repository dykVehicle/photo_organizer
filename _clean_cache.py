# -*- coding: utf-8 -*-
"""清理 NSFW 缓存中 score=1.0 的条目（来自 rebuild 脚本，非真实模型分数）"""
import os, sys, json
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

cache_path = r"H:\All_相册_20260225_V2\.photo_organizer\nsfw_score_cache.json"
if not os.path.isfile(cache_path):
    print("Cache not found:", cache_path)
    sys.exit(1)

with open(cache_path, "r", encoding="utf-8") as f:
    cache = json.load(f)

total = len(cache)
fake_count = sum(1 for v in cache.values() if v == 1.0)
real_count = total - fake_count

print("Total entries: %d" % total)
print("Fake (score=1.0): %d" % fake_count)
print("Real model scores: %d" % real_count)

# Show score distribution
from collections import Counter
buckets = Counter()
for v in cache.values():
    if v == 1.0:
        buckets["1.0 (fake)"] += 1
    elif v >= 0.5:
        buckets[">=0.5 NSFW"] += 1
    elif v >= 0.3:
        buckets["0.3-0.5"] += 1
    elif v >= 0.1:
        buckets["0.1-0.3"] += 1
    else:
        buckets["<0.1 clean"] += 1

print("\nScore distribution:")
for k in sorted(buckets.keys()):
    print("  %s: %d" % (k, buckets[k]))

# Remove fake entries
cleaned = {k: v for k, v in cache.items() if v != 1.0}
print("\nAfter cleaning: %d entries" % len(cleaned))

with open(cache_path, "w", encoding="utf-8") as f:
    json.dump(cleaned, f)
print("Saved cleaned cache.")
