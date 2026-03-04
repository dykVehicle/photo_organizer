# -*- coding: utf-8 -*-
import sys, os
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Check video count in the test dirs
for d in [
    r"H:\All_相册_20260225_V2\All_1_目标设备_手机照片\2015-Q2-M4-5-6_Xiaomi MI 6",
    r"H:\All_相册_20260225_V2\All_1_目标设备_手机照片\2017-Q1-M1-2-3_Xiaomi HM NOTE",
]:
    if not os.path.isdir(d):
        print("NOT FOUND:", d)
        continue
    photos = videos = 0
    for f in os.listdir(d):
        ext = os.path.splitext(f)[1].lower()
        if ext in (".jpg", ".jpeg", ".png", ".heic", ".heif"):
            photos += 1
        elif ext in (".mp4", ".mov", ".avi", ".mkv", ".3gp"):
            videos += 1
    print("%s: %d photos, %d videos" % (os.path.basename(d), photos, videos))

# Scan ALL photos in the 2nd dir and find top NSFW scores
sys.path.insert(0, ".")
from nsfw_detector import NsfwDetector
det = NsfwDetector(threshold=0.3)

test_dir = r"H:\All_相册_20260225_V2\All_1_目标设备_手机照片\2017-Q1-M1-2-3_Xiaomi HM NOTE"
all_imgs = [os.path.join(test_dir, f) for f in os.listdir(test_dir)
            if os.path.splitext(f)[1].lower() in (".jpg", ".jpeg", ".png")]
print("\nScanning %d images for NSFW..." % len(all_imgs))
scores = det.predict_batch(all_imgs)

# Show top 10 scores
ranked = sorted(zip(scores, all_imgs), reverse=True)
print("\nTop 10 highest NSFW scores:")
for score, path in ranked[:10]:
    print("  %.4f %s" % (score, os.path.basename(path)))

nsfw_count = sum(1 for s in scores if s >= 0.3)
print("\nNSFW detected (>=0.3):", nsfw_count)
nsfw_count2 = sum(1 for s in scores if s >= 0.1)
print("NSFW detected (>=0.1):", nsfw_count2)
