# -*- coding: utf-8 -*-
"""生成 NudeNet NSFW 检测报告：按置信度降序，含完整路径"""
import sys, os
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

RESULT_FILE = r"H:\All_相册_20260225_V2\nsfw_nudenet_top500.txt"
OUT_FILE = r"H:\All_相册_20260225_V2\nsfw_nudenet_report.txt"

nsfw_items = []
with open(RESULT_FILE, "r", encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if not line.startswith("[ NSFW]"):
            continue

        parts_start = line.index("nudenet_parts=[") + len("nudenet_parts=[")
        parts_end = line.index("]", parts_start)
        parts_str = line[parts_start:parts_end]
        path_start = parts_end + 2
        filepath = line[path_start:]

        falcon_start = line.index("falcon=") + len("falcon=")
        falcon_end = line.index(" ", falcon_start)
        falcon_score = float(line[falcon_start:falcon_end])

        parts = []
        for p in parts_str.split(", "):
            if ":" in p:
                cls, score = p.rsplit(":", 1)
                parts.append((cls, float(score)))

        max_score = max(s for _, s in parts) if parts else 0
        nsfw_items.append((max_score, falcon_score, parts, filepath))

nsfw_items.sort(key=lambda x: -x[0])

with open(OUT_FILE, "w", encoding="utf-8") as f:
    f.write("# NudeNet NSFW 检测报告（按置信度降序）\n")
    f.write("# 共 %d 个文件\n" % len(nsfw_items))
    f.write("# 格式: 序号 | NudeNet最高置信度 | 检测到的部位 | 文件路径\n")
    f.write("=" * 140 + "\n\n")
    for i, (max_score, falcon_score, parts, filepath) in enumerate(nsfw_items, 1):
        parts_cn = []
        cn_map = {
            "FEMALE_GENITALIA_EXPOSED": "女性生殖器",
            "MALE_GENITALIA_EXPOSED": "男性生殖器",
            "FEMALE_BREAST_EXPOSED": "女性乳房",
            "BUTTOCKS_EXPOSED": "臀部",
            "ANUS_EXPOSED": "肛门",
        }
        for cls, score in parts:
            label = cn_map.get(cls, cls)
            parts_cn.append("%s(%.0f%%)" % (label, score * 100))

        f.write("%2d | 置信度: %.0f%% | 部位: %s\n" % (i, max_score * 100, ", ".join(parts_cn)))
        f.write("   | 路径: %s\n\n" % filepath)

print("报告已生成: %s (%d 个文件)" % (OUT_FILE, len(nsfw_items)))

# 同时打印到控制台
print("\n" + "=" * 100)
print("NudeNet NSFW 检测报告（按置信度降序，共 %d 个文件）" % len(nsfw_items))
print("=" * 100)
for i, (max_score, falcon_score, parts, filepath) in enumerate(nsfw_items, 1):
    parts_cn = []
    cn_map = {
        "FEMALE_GENITALIA_EXPOSED": "女性生殖器",
        "MALE_GENITALIA_EXPOSED": "男性生殖器",
        "FEMALE_BREAST_EXPOSED": "女性乳房",
        "BUTTOCKS_EXPOSED": "臀部",
        "ANUS_EXPOSED": "肛门",
    }
    for cls, score in parts:
        label = cn_map.get(cls, cls)
        parts_cn.append("%s(%.0f%%)" % (label, score * 100))

    print("%2d | 置信度: %2.0f%% | 部位: %-30s | %s" % (
        i, max_score * 100, ", ".join(parts_cn), os.path.basename(filepath)))
