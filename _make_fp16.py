# -*- coding: utf-8 -*-
"""将 fp32 ONNX 模型转换为 fp16"""
import sys, os
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

model_dir = os.path.join(os.path.dirname(__file__), "models", "falconsai-nsfw-onnx")
src = os.path.join(model_dir, "model.onnx")

import onnx
from onnxconverter_common import float16

print("Loading model.onnx...")
model = onnx.load(src)
print("Converting to fp16...")
fp16_model = float16.convert_float_to_float16(model, keep_io_types=True)
dst = os.path.join(model_dir, "model_fp16.onnx")
onnx.save(fp16_model, dst)
print("Saved: %s (%.1f MB)" % (dst, os.path.getsize(dst) / 1048576))
