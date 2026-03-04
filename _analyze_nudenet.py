# -*- coding: utf-8 -*-
"""分析 NudeNet 检测结果的置信度分布，帮助确定最佳阈值"""
import sys, os
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

RESULT_FILE = r"H:\All_相册_20260225_V2\nsfw_nudenet_top500.txt"

nsfw_items = []
with open(RESULT_FILE, "r", encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[ NSFW]"):
            nsfw_items.append(line)

print("共 %d 个 NSFW 检测结果\n" % len(nsfw_items))
print("按置信度排序（从低到高）：")
print("=" * 120)

# 解析并按最高分数排序
parsed = []
for line in nsfw_items:
    # 提取 nudenet_parts 内容
    parts_start = line.index("nudenet_parts=[") + len("nudenet_parts=[")
    parts_end = line.index("]", parts_start)
    parts_str = line[parts_start:parts_end]
    
    # 提取文件名
    path_start = parts_end + 2  # skip "] "
    filepath = line[path_start:]
    
    # 解析各部位分数
    parts = []
    for p in parts_str.split(", "):
        if ":" in p:
            cls, score = p.rsplit(":", 1)
            parts.append((cls, float(score)))
    
    max_score = max(s for _, s in parts) if parts else 0
    parsed.append((max_score, parts, filepath))

parsed.sort(key=lambda x: x[0])

for max_score, parts, filepath in parsed:
    parts_str = ", ".join("%s:%.2f" % (c, s) for c, s in parts)
    print("  最高=%.2f  [%s]  %s" % (max_score, parts_str, os.path.basename(filepath)))

print("\n" + "=" * 120)
print("\n阈值分析（如果将阈值设为 X，会有多少文件被判为 NSFW）：")
for threshold in [0.45, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80]:
    count = sum(1 for ms, _, _ in parsed if ms >= threshold)
    print("  阈值 %.2f → %d 个 NSFW" % (threshold, count))
