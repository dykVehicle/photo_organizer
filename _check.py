# -*- coding: utf-8 -*-
import os, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

root = r"H:\All_相册_20260225_V2"
print("Root exists:", os.path.isdir(root))

nsfw_dir = os.path.join(root, "All_6_NSFW")
print("All_6_NSFW exists:", os.path.isdir(nsfw_dir))

if os.path.isdir(nsfw_dir):
    total = 0
    for dirpath, dirs, files in os.walk(nsfw_dir):
        if files:
            rel = os.path.relpath(dirpath, nsfw_dir)
            total += len(files)
            print("  %s: %d files" % (rel, len(files)))
    print("Total NSFW files:", total)
else:
    print("NSFW directory not found")

# Count all dirs
for d in sorted(os.listdir(root)):
    fp = os.path.join(root, d)
    if os.path.isdir(fp) and d.startswith("All_"):
        cnt = sum(len(f) for _, _, f in os.walk(fp))
        print("%s: %d files" % (d, cnt))
