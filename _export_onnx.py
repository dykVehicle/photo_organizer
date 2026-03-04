# -*- coding: utf-8 -*-
"""将 Falconsai/nsfw_image_detection 导出为 ONNX 模型（fp32 + fp16 + quantized）"""
import sys, os
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

OUT_DIR = os.path.join(os.path.dirname(__file__), "models", "falconsai-nsfw-onnx")
MODEL_ID = "Falconsai/nsfw_image_detection"

print("Step 1: Export to ONNX (fp32)...")
from optimum.onnxruntime import ORTModelForImageClassification
model = ORTModelForImageClassification.from_pretrained(MODEL_ID, export=True)
model.save_pretrained(OUT_DIR)
print("  Saved to:", OUT_DIR)

# List generated files
for f in os.listdir(OUT_DIR):
    fp = os.path.join(OUT_DIR, f)
    size_mb = os.path.getsize(fp) / 1048576
    print("  %s (%.1f MB)" % (f, size_mb))

# Generate fp16 version
print("\nStep 2: Convert to fp16...")
import numpy as np
try:
    import onnx
    from onnxconverter_common import float16
    onnx_model = onnx.load(os.path.join(OUT_DIR, "model.onnx"))
    fp16_model = float16.convert_float_to_float16(onnx_model, keep_io_types=True)
    fp16_path = os.path.join(OUT_DIR, "model_fp16.onnx")
    onnx.save(fp16_model, fp16_path)
    print("  Saved fp16:", fp16_path, "(%.1f MB)" % (os.path.getsize(fp16_path) / 1048576))
except ImportError:
    print("  onnx/onnxconverter-common not installed, skipping fp16 conversion")
    print("  Install: pip install onnx onnxconverter-common")

# Generate quantized version
print("\nStep 3: Quantize to int8...")
try:
    from onnxruntime.quantization import quantize_dynamic, QuantType
    q_path = os.path.join(OUT_DIR, "model_quantized.onnx")
    quantize_dynamic(
        os.path.join(OUT_DIR, "model.onnx"),
        q_path,
        weight_type=QuantType.QUInt8,
    )
    print("  Saved quantized:", q_path, "(%.1f MB)" % (os.path.getsize(q_path) / 1048576))
except Exception as e:
    print("  Quantization failed:", e)

print("\nDone!")
