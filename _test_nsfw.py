# -*- coding: utf-8 -*-
"""NSFW GPU 全目录检测"""
import sys
import os

os.environ["PYTHONIOENCODING"] = "utf-8"
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

root = r"H:\All_相册_20260225_V2"
scan_dirs = []
for d in sorted(os.listdir(root)):
    fp = os.path.join(root, d)
    if os.path.isdir(fp) and not d.startswith("All_6_NSFW"):
        count = sum(len(files) for _, _, files in os.walk(fp))
        print("  %s: %d files" % (d, count))
        scan_dirs.append(fp)

print("\nTotal scan dirs: %d" % len(scan_dirs))

sys.argv = [
    "main.py",
    "--scan-dirs", *scan_dirs,
    "--output-dir", root,
    "--nsfw",
    "--nsfw-threshold", "0.5",
    "--copy-all",
]

print("Threshold: 0.5 (实际复制)")
print("=" * 60)

from main import main
main()
