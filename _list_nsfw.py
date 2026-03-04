# -*- coding: utf-8 -*-
"""列出所有 NSFW 分数 >= 阈值的文件路径，输出到文件供人工审核"""
import os, sys, json
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

THRESHOLD = 0.5
ROOT = r"H:\All_相册_20260225_V2"

nsfw_cache_path = os.path.join(ROOT, ".photo_organizer", "nsfw_score_cache.json")
src_hash_cache_path = os.path.join("H:\\", ".photo_organizer", "src_hash_cache.json")

print("Loading NSFW cache...")
with open(nsfw_cache_path, "r", encoding="utf-8") as f:
    nsfw_cache = json.load(f)

nsfw_hashes = {h: s for h, s in nsfw_cache.items() if s >= THRESHOLD}
print("NSFW files (>= %.2f): %d" % (THRESHOLD, len(nsfw_hashes)))

print("Loading source hash cache...")
with open(src_hash_cache_path, "r", encoding="utf-8") as f:
    src_data = json.load(f)

# src_hash_cache format: {"version": ..., "entries": {filepath: {"hash": "...", ...}}}
entries = src_data.get("entries", src_data)

hash_to_paths = {}
for fp, info in entries.items():
    h = info.get("hash", "") if isinstance(info, dict) else info
    if h in nsfw_hashes:
        hash_to_paths.setdefault(h, []).append(fp)

# Build result list: (score, filepath)
results = []
for h, score in nsfw_hashes.items():
    paths = hash_to_paths.get(h, [])
    if paths:
        for p in paths:
            results.append((score, p))
    else:
        results.append((score, "[hash=%s, path not found]" % h))

results.sort(key=lambda x: (-x[0], x[1]))

out_path = os.path.join(ROOT, "nsfw_file_list.txt")
with open(out_path, "w", encoding="utf-8") as f:
    f.write("# NSFW 文件列表 (阈值 >= %.2f)\n" % THRESHOLD)
    f.write("# 共 %d 个文件\n" % len(results))
    f.write("# 格式: 分数 | 文件路径\n\n")
    for score, path in results:
        f.write("%.4f | %s\n" % (score, path))

print("Output: %s (%d files)" % (out_path, len(results)))

# Print summary by score range
for lo, hi, label in [(0.8, 0.9, "0.8-0.9"), (0.9, 1.01, "0.9-1.0")]:
    cnt = sum(1 for s, _ in results if lo <= s < hi)
    print("  %s: %d" % (label, cnt))

# Also print first 30 lines to console
print("\n--- 前 30 条 ---")
for score, path in results[:30]:
    print("%.4f | %s" % (score, path))
