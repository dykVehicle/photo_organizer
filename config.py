"""全局配置：路径、排除规则、设备品牌关键词、并行参数"""

import string
import ctypes
import os

# ── 并行参数 ──
DEFAULT_WORKERS = 32

# ── 目标路径 ──
DEST_DRIVE = "H:\\"
DEST_PHONE_NAME = "All_1_手机照片"
DEST_CAMERA_NAME = "All_2_相机照片"
DEST_UNKNOWN_NAME = "All_3_未识别设备照片"

# --copy-all 模式下的命名（目标设备 / 其他设备）
DEST_TARGET_PHONE_NAME = "All_1_目标设备_手机照片"
DEST_TARGET_CAMERA_NAME = "All_2_目标设备_相机照片"
DEST_OTHER_PHONE_NAME = "All_3_其他设备_手机照片"
DEST_OTHER_CAMERA_NAME = "All_4_其他设备_相机照片"
DEST_OTHER_UNKNOWN_NAME = "All_5_其他设备_未识别设备照片"

# 运行时由 main.py 设置的完整路径（不要手动修改）
DEST_CAMERA = ""
DEST_PHONE = ""
DEST_UNKNOWN = ""
DEST_CAMERA_OTHER = ""
DEST_PHONE_OTHER = ""
REPORT_DIR = ""

NO_EXIF_DATE_FOLDER = "未知日期_无EXIF"

# ── 小图过滤（仅对未识别设备照片生效） ──
MIN_FILE_SIZE_BYTES = 100 * 1024        # 100 KB
MIN_IMAGE_DIMENSION = 300               # 像素

# ── 支持的图片扩展名（全小写） ──
IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".tiff", ".tif", ".bmp",
    ".cr2", ".cr3", ".nef", ".arw", ".orf", ".rw2",
    ".dng", ".raf", ".pef", ".heic", ".heif", ".srw",
}

# ── 支持的视频扩展名（全小写） ──
VIDEO_EXTENSIONS = {
    ".mp4", ".mov", ".avi", ".mkv", ".mts", ".m2ts",
    ".m4v", ".wmv", ".flv", ".webm", ".3gp", ".mpg", ".mpeg",
}

MEDIA_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS

# ── 扫描排除目录（大小写不敏感匹配） ──
EXCLUDED_DIRS = {
    "$recycle.bin",
    "system volume information",
    "windows",
    "recovery",
    "boot",
    "msocache",
    "node_modules",
    ".git",
    "__pycache__",
    ".venv",
    "venv",
}

# ── 排除的盘符（目标盘，避免重复扫描） ──
EXCLUDED_DRIVES = set()


def get_all_drives():
    """获取 Windows 系统所有可用盘符，如 ['C:\\', 'D:\\', ...]"""
    drives = []
    if os.name == "nt":
        bitmask = ctypes.windll.kernel32.GetLogicalDrives()
        for letter in string.ascii_uppercase:
            if bitmask & 1:
                drives.append(f"{letter}:\\")
            bitmask >>= 1
    return drives


def get_excluded_drive_letters():
    """从目标路径中提取盘符，避免重复扫描目标盘"""
    letters = set()
    for dest in (DEST_CAMERA, DEST_PHONE, DEST_UNKNOWN, DEST_DRIVE):
        if dest and len(dest) >= 2 and dest[1] == ":":
            letters.add(dest[0].upper())
    return letters


# ── 相机品牌关键词（Make 字段，小写匹配） ──
CAMERA_BRANDS = {
    "canon",
    "nikon",
    "fujifilm", "fuji",
    "olympus", "om digital solutions",
    "panasonic", "lumix",
    "pentax", "ricoh",
    "leica",
    "hasselblad",
    "sigma",
    "phase one",
    "mamiya",
    "dji",
    "gopro",
    "insta360",
    "blackmagic",
}

SONY_CAMERA_MODEL_KEYWORDS = {
    "ilce", "dsc", "slta", "nex", "alpha",
    "a7", "a6", "a9", "a1", "a99",
    "rx", "zv-e", "fx",
}

# ── 手机品牌关键词（Make 字段，小写匹配） ──
PHONE_BRANDS = {
    "apple",
    "samsung",
    "huawei",
    "xiaomi", "redmi", "poco",
    "oneplus",
    "google",
    "oppo",
    "vivo", "iqoo",
    "realme",
    "honor",
    "motorola", "lenovo",
    "nothing",
    "meizu",
    "zte",
    "asus",
    "nokia", "hmd global",
    "tecno", "infinix", "itel",
    "fairphone",
}

SONY_PHONE_MODEL_KEYWORDS = {
    "xperia",
}

AMBIGUOUS_BRANDS = {"sony", "sony corporation"}

# ── 小米/红米/POCO 型号代码 → 营销名映射（用于文件夹命名美化） ──
# key: 型号代码小写, value: 营销名（会拼接在代码前面，如 "Xiaomi 14 23127PN0CC"）
XIAOMI_MODEL_NAMES = {
    # ── Xiaomi 旗舰 ──
    "24129pn74c": "Xiaomi 15",
    "24129pn74g": "Xiaomi 15",
    "24129pn74i": "Xiaomi 15",
    "2410dpn6cc": "Xiaomi 15 Pro",
    "25010pn30c": "Xiaomi 15 Ultra",
    "25010pn30g": "Xiaomi 15 Ultra",
    "25010pn30i": "Xiaomi 15 Ultra",
    "25019pnf3c": "Xiaomi 15 Ultra",
    "23127pn0cc": "Xiaomi 14",
    "23127pn0cg": "Xiaomi 14",
    "23116pn5bc": "Xiaomi 14 Pro",
    "23116pn5bg": "Xiaomi 14 Pro",
    "2304fpn6dc": "Xiaomi 13 Ultra",
    "2304fpn6dg": "Xiaomi 13 Ultra",
    "2211133c": "Xiaomi 13",
    "2211133g": "Xiaomi 13",
    "2210132c": "Xiaomi 13 Pro",
    "2210132g": "Xiaomi 13 Pro",
    "2206123sc": "Xiaomi 12S",
    "2206122sc": "Xiaomi 12S Pro",
    "2203121c": "Xiaomi 12S Ultra",
    "2201123c": "Xiaomi 12",
    "2201123g": "Xiaomi 12",
    "2201122c": "Xiaomi 12 Pro",
    "2201122g": "Xiaomi 12 Pro",
    "2112123ac": "Xiaomi 12X",
    "2112123ag": "Xiaomi 12X",
    "22071212ag": "Xiaomi 12T",
    "22081212g": "Xiaomi 12T Pro",
    "21081111rg": "Xiaomi 11T",
    "2107113sg": "Xiaomi 11T Pro",
    "2107113si": "Xiaomi 11T Pro",
    "2109119dg": "Xiaomi 11 Lite 5G NE",
    "2109119di": "Xiaomi 11 Lite 5G NE",
    "2109119bc": "Xiaomi Civi",
    "2209129sc": "Xiaomi Civi 2",
    "23046pnc9c": "Xiaomi Civi 3",
    "2203129g": "Xiaomi 12 Lite",
    "2306epn60g": "Xiaomi 13T",
    "23078pnd5g": "Xiaomi 13T Pro",
    # ── Xiaomi Mi 系列（旧） ──
    "m2011k2g": "Mi 11",
    "m2011k2c": "Mi 11",
    "m2001j2g": "Mi 10",
    "m2001j2i": "Mi 10",
    "m2001j2c": "Mi 10",
    "m2007j1sc": "Mi 10 Ultra",
    "m2102j2sc": "Mi 10S",
    "m1902f1g": "Mi 9",
    "m1902f1c": "Mi 9",
    "m1903f2g": "Mi 9 SE",
    # ── Redmi K 系列 ──
    "23113rkc6c": "Redmi K70",
    "23117rk66c": "Redmi K70 Pro",
    "22081212c": "Redmi K50 Ultra",
    "23078rkd5c": "Redmi K60 Ultra",
    "23013rk75c": "Redmi K60",
    "22122rk93c": "Redmi K60E",
    "22127rk46c": "Redmi K60 Pro",
    "22041211ac": "Redmi K50",
    "21121210c": "Redmi K50 Gaming",
    # ── Redmi Note 系列 ──
    "23129raa4g": "Redmi Note 13 4G",
    "2312dra50c": "Redmi Note 13 Pro",
    "2312dra50g": "Redmi Note 13 Pro",
    "23090ra98g": "Redmi Note 13 Pro+",
    "23090ra98i": "Redmi Note 13 Pro+",
    "2312draabc": "Redmi Note 13 5G",
    "2312draabi": "Redmi Note 13 5G",
    "22101316c": "Redmi Note 12 Pro",
    "22101316g": "Redmi Note 12 Pro",
    "22101316i": "Redmi Note 12 Pro",
    "22101316uc": "Redmi Note 12 Explorer",
    "22101317c": "Redmi Note 12",
    "23021raaei": "Redmi Note 12 4G",
    "23021raaeg": "Redmi Note 12 4G",
    "22031116bg": "Redmi Note 11S 5G",
    "2201117sg": "Redmi Note 11S",
    "2201117sy": "Redmi Note 11S",
    "2201117si": "Redmi Note 11S",
    "2201116sg": "Redmi Note 11 Pro 5G",
    "2201116si": "Redmi Note 11 Pro+ 5G",
    "2201117tg": "Redmi Note 11",
    "2201116tg": "Redmi Note 11 Pro 4G",
    "21091116c": "Redmi Note 11 Pro",
    "21091116uc": "Redmi Note 11 Pro+",
    "m2101k7ag": "Redmi Note 10",
    "m2101k7ai": "Redmi Note 10",
    "m2101k7bny": "Redmi Note 10S",
    "m2101k7bg": "Redmi Note 10S",
    "m2101k7bi": "Redmi Note 10S",
    "m1908c3jh": "Redmi Note 8",
    "m1908c3ji": "Redmi Note 8",
    "m1908c3jc": "Redmi Note 8",
    # ── POCO 系列 ──
    "23122pcd1g": "POCO X6 5G",
    "2311drk48g": "POCO X6 Pro",
    "23013pc75g": "POCO F5 Pro",
    "23049pcd8g": "POCO F5",
    "22021211rg": "POCO F4",
    "21121210g": "POCO F4 GT",
    "m2012k11ag": "POCO F3",
    "m2004j11g": "POCO F2 Pro",
    # ── Redmi 数字系列 ──
    "23106rn0da": "Redmi 13C",
    "23053rn02y": "Redmi 12",
    "23076rn8dy": "Redmi 12 5G",
    # ── MiOne 等旧型号 ──
    "mione": "Mi 1",
    "mionep": "Mi 1S",
}

# 反向映射：营销名(小写) → 首个型号代码（用于 EXIF model 已是营销名时补充代码）
_XIAOMI_NAME_TO_CODE: dict = {}
for _code, _name in XIAOMI_MODEL_NAMES.items():
    _key = _name.lower()
    if _key not in _XIAOMI_NAME_TO_CODE:
        _XIAOMI_NAME_TO_CODE[_key] = _code.upper()

# ── 默认目标设备（只复制这些品牌的文件，其余仅报告） ──
# 品牌级匹配：make 字段包含关键词即命中
DEFAULT_TARGET_BRANDS = {
    "xiaomi", "redmi",
    "apple",
    "huawei", "honor",
    "samsung",
    "dji",
}

# 型号级匹配：make+model 组合精确匹配（小写比较）
# 格式: "品牌 型号" 或 "品牌_型号"，匹配时用 (make + " " + model).lower() 包含检查
DEFAULT_TARGET_MODELS = {
    "canon eos 700d",
    "canon eos 550d",
    "canon eos rp",
    "sony dsc-tx100",
    "sony dsc-hx400",
    "nikon d3300",
    "nikon d3x",
    "nikon d7100",
    "nikon d90",
    "fujifilm x-t20",
    "oppo u705t",
    "vivo x6d",
}

# 合并用于向后兼容（--devices 命令行参数仍按品牌匹配）
DEFAULT_TARGET_DEVICES = DEFAULT_TARGET_BRANDS

COPY_UNKNOWN = False
