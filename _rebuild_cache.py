# -*- coding: utf-8 -*-
"""从哈希缓存匹配 All_6_NSFW 文件，重建 NSFW 分数缓存"""
import os, sys, json
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

nsfw_dir = r"H:\All_相册_20260225_V2\All_6_NSFW"
nsfw_cache_path = r"H:\All_相册_20260225_V2\.photo_organizer\nsfw_score_cache.json"

# Load all hash caches
path2hash = {}
for cp in [r"H:\.photo_organizer\hash_cache.json",
           r"H:\.photo_organizer\src_hash_cache.json"]:
    if not os.path.isfile(cp):
        continue
    with open(cp, "r", encoding="utf-8") as f:
        data = json.load(f)
    for fp, info in data.get("entries", {}).items():
        if isinstance(info, dict) and "hash" in info:
            path2hash[fp] = info["hash"]
    print("Loaded %s: %d entries" % (os.path.basename(cp), len(data.get("entries", {}))))

print("Total path->hash: %d" % len(path2hash))

# Build basename+size -> hash for fuzzy matching
name_size_to_hash = {}
for fp, h in path2hash.items():
    bn = os.path.basename(fp).lower()
    name_size_to_hash[bn] = h

# Get NSFW file paths
nsfw_files = []
for root, dirs, fnames in os.walk(nsfw_dir):
    for fn in fnames:
        nsfw_files.append(os.path.join(root, fn))
print("NSFW files: %d" % len(nsfw_files))

# Match by direct path (in hash_cache) or by basename
nsfw_hashes = {}
matched = 0
for fp in nsfw_files:
    # Direct path match
    if fp in path2hash:
        nsfw_hashes[path2hash[fp]] = 1.0
        matched += 1
        continue
    # Basename match
    bn = os.path.basename(fp).lower()
    if bn in name_size_to_hash:
        nsfw_hashes[name_size_to_hash[bn]] = 1.0
        matched += 1

print("Matched: %d / %d" % (matched, len(nsfw_files)))
print("Unique NSFW hashes: %d" % len(nsfw_hashes))

os.makedirs(os.path.dirname(nsfw_cache_path), exist_ok=True)
with open(nsfw_cache_path, "w", encoding="utf-8") as f:
    json.dump(nsfw_hashes, f)
print("Saved -> %s" % nsfw_cache_path)
