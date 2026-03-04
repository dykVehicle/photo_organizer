# -*- coding: utf-8 -*-
import sys, os, glob
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

cache_dir = r"H:\All_相册_20260225_V2\.photo_organizer"
cache_file = os.path.join(cache_dir, "nsfw_score_cache.json")

print("Cache dir exists:", os.path.isdir(cache_dir))
if os.path.isdir(cache_dir):
    for f in os.listdir(cache_dir):
        fp = os.path.join(cache_dir, f)
        size = os.path.getsize(fp) / 1048576
        print("  %s (%.1f MB)" % (f, size))

if os.path.isfile(cache_file):
    os.remove(cache_file)
    print("\nDeleted:", cache_file)
else:
    print("\nCache file not found")

print("Verify gone:", not os.path.isfile(cache_file))
