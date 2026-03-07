# -*- coding: utf-8 -*-
"""将 mozilla/docornot (DeiT-tiny) 导出为 ONNX 模型"""
import sys, os
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

OUT_DIR = os.path.join(os.path.dirname(__file__), "models", "docornot-onnx")
MODEL_ID = "mozilla/docornot"

print("Step 1: Export to ONNX (fp32)...")
from optimum.onnxruntime import ORTModelForImageClassification
model = ORTModelForImageClassification.from_pretrained(MODEL_ID, export=True)
model.save_pretrained(OUT_DIR)
print("  Saved to:", OUT_DIR)

for f in os.listdir(OUT_DIR):
    fp = os.path.join(OUT_DIR, f)
    size_mb = os.path.getsize(fp) / 1048576
    print("  %s (%.1f MB)" % (f, size_mb))

print("\nDone!")
