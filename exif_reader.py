"""EXIF / 视频元数据 读取与解析模块（多线程优化版）"""

import json
import logging
import os
import shutil
import struct
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed, wait, FIRST_COMPLETED
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import exifread

from interrupt import is_interrupted

from config import VIDEO_EXTENSIONS

logger = logging.getLogger(__name__)


def _interruptible_as_completed(futures, timeout_per_poll=1.0):
    pending = set(futures)
    while pending:
        if is_interrupted():
            for f in pending:
                f.cancel()
            return
        done, pending = wait(pending, timeout=timeout_per_poll, return_when=FIRST_COMPLETED)
        yield from done

try:
    from PIL import Image
    from pillow_heif import register_heif_opener
    register_heif_opener()
    _HEIF_SUPPORT = True
except ImportError:
    _HEIF_SUPPORT = False

_SKIP_EXIF_EXTENSIONS = {".bmp", ".gif", ".svg"}

# ── ffprobe 路径缓存 ──
_ffprobe_path: Optional[str] = None
_ffprobe_checked = False


def _get_ffprobe_path() -> Optional[str]:
    """获取 ffprobe 路径：系统 PATH → static-ffmpeg 自动下载 → None"""
    global _ffprobe_path, _ffprobe_checked
    if _ffprobe_checked:
        return _ffprobe_path
    _ffprobe_checked = True

    path = shutil.which("ffprobe")
    if path:
        _ffprobe_path = path
        logger.info("使用系统 ffprobe: %s", path)
        return _ffprobe_path

    try:
        import static_ffmpeg
        static_ffmpeg.add_paths()
        path = shutil.which("ffprobe")
        if path:
            _ffprobe_path = path
            logger.info("使用 static-ffmpeg 提供的 ffprobe: %s", path)
            return _ffprobe_path
    except ImportError:
        logger.debug("static-ffmpeg 未安装，跳过")
    except Exception as e:
        logger.warning("static-ffmpeg 初始化失败: %s", e)

    logger.info("ffprobe 不可用，视频将使用内置 MP4/MOV 解析器 + 文件修改时间")
    return None


@dataclass
class PhotoInfo:
    """媒体文件元数据信息"""
    filepath: str
    media_type: str = "photo"    # "photo" | "video"
    make: str = ""
    model: str = ""
    date_taken: Optional[datetime] = None
    has_exif_date: bool = False
    file_modified: Optional[datetime] = None
    file_size: int = 0
    width: int = 0
    height: int = 0
    extra: Dict = field(default_factory=dict)


import re as _re

_EXIF_DATE_FORMATS = [
    "%Y:%m:%d %H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y/%m/%d %H:%M:%S",
    "%Y:%m:%d",
    "%Y-%m-%d",
]
_LOOSE_DATE_RE = _re.compile(
    r"(\d{4})\D+(\d{1,2})\D+(\d{1,2})"
    r"(?:\D+(\d{1,2})\D+(\d{1,2})(?:\D+(\d{1,2}))?)?"
)


def _parse_exif_datetime(value: str) -> Optional[datetime]:
    cleaned = str(value).strip().rstrip("\x00")
    if not cleaned or cleaned.startswith("0000"):
        return None
    for fmt in _EXIF_DATE_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
    m = _LOOSE_DATE_RE.search(cleaned)
    if m:
        try:
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            h = int(m.group(4)) if m.group(4) else 0
            mi = int(m.group(5)) if m.group(5) else 0
            s = int(m.group(6)) if m.group(6) else 0
            if 1970 <= y <= 2099 and 1 <= mo <= 12 and 1 <= d <= 31:
                return datetime(y, mo, d, h, mi, s)
        except (ValueError, OverflowError):
            pass
    return None


_FNAME_DATE_PATTERNS = [
    # ── 带前缀 + 完整日期时间 ──
    # IMG_20120906_101406, VID_20190501_120000, Screenshot_20210101_120000
    _re.compile(r"(?:IMG|VID|C360|Screenshot|PANO|MVIMG)_(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})"),
    # ── 无前缀 + 完整日期时间 ──
    # YYYYMMDD_HHMMSS
    _re.compile(r"^(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})"),
    # YYYY_MM_DD_HH_MM_SS 或 YYYY_MM_DD_HH_MM
    _re.compile(r"^(\d{4})_(\d{2})_(\d{2})_(\d{2})_(\d{2})(?:_(\d{2}))?"),
    # YYYY-MM-DD + 时间（分隔符灵活：. : - 或无）
    _re.compile(r"(\d{4})-(\d{2})-(\d{2})[\s_T](\d{2})[:\-.]?(\d{2})[:\-.]?(\d{2})"),
    # ── 带前缀 + 仅日期 ──
    # IMG_20120906, DSC_20190501, DSCN1234 等常见相机前缀
    _re.compile(r"(?:IMG|VID|C360|Screenshot|PANO|DSC|DSCN|DSCF|SAM)_(\d{4})(\d{2})(\d{2})(?!\d)"),
    # ── 仅日期（灵活分隔符） ──
    # YYYY-MM-DD 或 YYYY_MM_DD
    _re.compile(r"(?<!\d)(\d{4})[-_](\d{2})[-_](\d{2})(?!\d)"),
    # ── 仅日期（8 位连写） ──
    # YYYYMMDD（独立 8 位数字，不属于更长数字的一部分）
    _re.compile(r"(?<!\d)(\d{4})(\d{2})(\d{2})(?!\d)"),
]
# 带已知前缀的时间戳
_FNAME_TS_PATTERN = _re.compile(r"(?:mmexport|wx_camera|wx)_?(\d{10,13})")
# 任意位置的独立 10/13 位时间戳
_FNAME_PURE_TS = _re.compile(r"(?<!\d)(\d{10}|\d{13})(?!\d)")


_NOW_TS = datetime.now().timestamp() + 86400  # 当前时间 + 1 天容差
_MAX_YEAR = datetime.now().year  # 最多允许当年


def _ts_to_datetime(ts: int) -> Optional[datetime]:
    """将 Unix 秒级时间戳转换为 datetime，拒绝未来时间"""
    if 738892800 <= ts <= _NOW_TS:  # 1993-06-01 ~ 运行时刻+1天
        try:
            return datetime.fromtimestamp(ts)
        except (OSError, ValueError):
            pass
    return None


def _parse_date_from_filename(filename: str) -> Optional[datetime]:
    """从文件名中提取日期信息。支持 IMG_YYYYMMDD、mmexport 时间戳等常见格式。"""
    stem = os.path.splitext(filename)[0]

    for pat in _FNAME_DATE_PATTERNS:
        m = pat.search(stem)
        if m:
            try:
                groups = m.groups()
                y, mo, d = int(groups[0]), int(groups[1]), int(groups[2])
                h = int(groups[3]) if len(groups) > 3 and groups[3] else 0
                mi = int(groups[4]) if len(groups) > 4 and groups[4] else 0
                s = int(groups[5]) if len(groups) > 5 and groups[5] else 0
                if 1993 <= y <= _MAX_YEAR and 1 <= mo <= 12 and 1 <= d <= 31:
                    return datetime(y, mo, d, h, mi, s)
            except (ValueError, OverflowError):
                continue

    m = _FNAME_TS_PATTERN.search(stem)
    if m:
        ts_str = m.group(1)
        ts = int(ts_str)
        if len(ts_str) == 13:
            ts = ts // 1000
        dt = _ts_to_datetime(ts)
        if dt:
            return dt

    m = _FNAME_PURE_TS.search(stem)
    if m:
        ts_str = m.group(1)
        ts = int(ts_str)
        if len(ts_str) == 13:
            ts = ts // 1000
        dt = _ts_to_datetime(ts)
        if dt:
            return dt

    return None


def _parse_iso_datetime(value: str) -> Optional[datetime]:
    """解析 ISO 8601 格式（视频常用）: 2024-01-15T10:30:00.000000Z"""
    if not value or value.startswith("0000"):
        return None
    cleaned = value.strip().rstrip("\x00")
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y:%m:%d %H:%M:%S",
    ):
        try:
            dt = datetime.strptime(cleaned, fmt)
            if dt.tzinfo is not None:
                dt = dt.astimezone(tz=None).replace(tzinfo=None)
            return dt
        except ValueError:
            continue
    return None


# ── 图片 EXIF 读取 ──


def _read_exif_standard(filepath: str) -> dict:
    try:
        with open(filepath, "rb") as f:
            tags = exifread.process_file(f, details=False, stop_tag="EXIF DateTimeDigitized")
        return {str(k): str(v) for k, v in tags.items()}
    except Exception:
        return {}


def _read_exif_pillow(filepath: str) -> dict:
    if not _HEIF_SUPPORT:
        return {}
    try:
        img = Image.open(filepath)
        exif_data = img.getexif()
        if not exif_data:
            return {}

        from PIL.ExifTags import TAGS
        result = {}
        for tag_id, value in exif_data.items():
            tag_name = TAGS.get(tag_id, str(tag_id))
            result[tag_name] = str(value)

        ifd = exif_data.get_ifd(0x8769)
        if ifd:
            for tag_id, value in ifd.items():
                tag_name = TAGS.get(tag_id, str(tag_id))
                result[f"EXIF {tag_name}"] = str(value)

        return result
    except Exception:
        return {}


# ── 视频元数据读取 ──


def _read_video_ffprobe(filepath: str) -> dict:
    """通过 ffprobe 提取视频元数据，返回 {make, model, creation_time} 或空 dict"""
    if is_interrupted():
        return {}
    ffprobe = _get_ffprobe_path()
    if not ffprobe:
        return {}
    try:
        cmd = [
            ffprobe, "-v", "quiet",
            "-print_format", "json",
            "-show_format", "-show_streams",
            filepath,
        ]
        proc = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if proc.returncode != 0:
            return {}
        data = json.loads(proc.stdout.decode("utf-8", errors="replace"))
    except Exception:
        return {}

    tags = {}
    fmt_tags = data.get("format", {}).get("tags", {})
    tags.update({k.lower(): v for k, v in fmt_tags.items()})

    for stream in data.get("streams", []):
        stream_tags = stream.get("tags", {})
        for k, v in stream_tags.items():
            key = k.lower()
            if key not in tags:
                tags[key] = v

    result = {}
    make = (
        tags.get("com.apple.quicktime.make", "")
        or tags.get("com.android.manufacturer", "")
        or tags.get("manufacturer", "")
        or tags.get("make", "")
    )
    model = (
        tags.get("com.apple.quicktime.model", "")
        or tags.get("com.android.model", "")
        or tags.get("model", "")
    )

    if not make:
        brands = tags.get("compatible_brands", "").lower()
        if "caep" in brands:
            make = "Canon"
        elif "niko" in brands:
            make = "Nikon"

    result["make"] = make
    result["model"] = model
    result["creation_time"] = (
        tags.get("com.apple.quicktime.creationdate", "")
        or tags.get("creation_time", "")
        or tags.get("date", "")
    )

    width = None
    height = None
    for stream in data.get("streams", []):
        if stream.get("codec_type") == "video":
            width = stream.get("width")
            height = stream.get("height")
            break
    if width:
        result["width"] = width
    if height:
        result["height"] = height

    return result


def _read_mp4_box_metadata(filepath: str) -> dict:
    """
    内置 MP4/MOV 解析器：读取 mvhd 的 creation_time 和 udta 的 make/model。
    覆盖 iPhone/Android/DJI/Canon 等 95% 的常见视频格式，无需外部依赖。
    """
    result = {}
    try:
        with open(filepath, "rb") as f:
            _parse_mp4_boxes(f, os.path.getsize(filepath), result)
    except Exception:
        pass
    return result


def _parse_mp4_boxes(f, end_pos: int, result: dict, depth: int = 0):
    """递归解析 ISO BMFF / QuickTime box 结构"""
    if depth > 10:
        return
    container_types = {b"moov", b"trak", b"mdia", b"udta", b"meta"}

    while f.tell() < end_pos:
        box_start = f.tell()
        header = f.read(8)
        if len(header) < 8:
            break
        size = struct.unpack(">I", header[:4])[0]
        box_type = header[4:8]

        if size == 0:
            break
        if size == 1:
            ext = f.read(8)
            if len(ext) < 8:
                break
            size = struct.unpack(">Q", ext)[0]

        box_end = box_start + size
        if box_end > end_pos or size < 8:
            break

        if box_type == b"mvhd":
            _parse_mvhd(f, box_end, result)
        elif box_type in (b"\xa9mak", b"\xa9mod", b"\xa9day"):
            _parse_udta_text(f, box_type, box_end, result)
        elif box_type in container_types:
            inner_start = f.tell()
            if box_type == b"meta":
                f.read(4)
                inner_start = f.tell()
            _parse_mp4_boxes(f, box_end, result, depth + 1)

        f.seek(box_end)


def _parse_mvhd(f, box_end: int, result: dict):
    """从 mvhd box 中提取 creation_time"""
    data = f.read(4)
    if len(data) < 4:
        return
    version = data[0]
    if version == 0:
        ts_data = f.read(4)
        if len(ts_data) < 4:
            return
        timestamp = struct.unpack(">I", ts_data)[0]
    else:
        ts_data = f.read(8)
        if len(ts_data) < 8:
            return
        timestamp = struct.unpack(">Q", ts_data)[0]

    if timestamp > 0 and "creation_time" not in result:
        epoch_diff = 2082844800
        unix_ts = timestamp - epoch_diff
        if 0 < unix_ts < 4102444800:
            try:
                result["creation_time"] = datetime.fromtimestamp(unix_ts).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
            except (OSError, ValueError):
                pass


def _parse_udta_text(f, box_type: bytes, box_end: int, result: dict):
    """从 udta 下的文本 atom（©mak, ©mod, ©day）中提取字符串"""
    remaining = box_end - f.tell()
    if remaining < 4:
        return
    data = f.read(min(remaining, 512))
    if len(data) < 4:
        return

    text = ""
    data_size = struct.unpack(">H", data[:2])[0]
    if 4 <= data_size <= len(data):
        raw = data[4:data_size]
        try:
            text = raw.decode("utf-8", errors="ignore").strip("\x00").strip()
        except Exception:
            pass
    if not text and len(data) > 8:
        try:
            text = data[8:].decode("utf-8", errors="ignore").strip("\x00").strip()
        except Exception:
            pass

    if text:
        key_map = {b"\xa9mak": "make", b"\xa9mod": "model", b"\xa9day": "creation_time"}
        key = key_map.get(box_type)
        if key and key not in result:
            result[key] = text


def _read_video_metadata(filepath: str) -> dict:
    """
    读取视频元数据，优先 ffprobe → 回退内置 MP4 解析器。
    返回 dict: {make, model, creation_time, width, height}
    """
    meta = _read_video_ffprobe(filepath)
    if meta.get("creation_time") or meta.get("make"):
        return meta

    mp4_meta = _read_mp4_box_metadata(filepath)
    if mp4_meta:
        for k, v in mp4_meta.items():
            if v and not meta.get(k):
                meta[k] = v

    return meta


# ── 统一入口 ──


def read_photo_info(filepath: str) -> PhotoInfo:
    """读取媒体文件的元数据信息，返回 PhotoInfo 对象。"""
    if is_interrupted():
        return PhotoInfo(filepath=filepath)
    ext = os.path.splitext(filepath)[1].lower()
    is_video = ext in VIDEO_EXTENSIONS

    info = PhotoInfo(filepath=filepath, media_type="video" if is_video else "photo")

    try:
        stat = os.stat(filepath)
        info.file_size = stat.st_size
        info.file_modified = datetime.fromtimestamp(stat.st_mtime)
    except OSError:
        pass

    if is_video:
        meta = _read_video_metadata(filepath)
        info.make = (meta.get("make") or "").strip()
        info.model = (meta.get("model") or "").strip()

        ct = meta.get("creation_time", "")
        if ct:
            dt = _parse_iso_datetime(ct) or _parse_exif_datetime(ct)
            if dt:
                info.date_taken = dt
                info.has_exif_date = True

        try:
            info.width = int(meta.get("width", 0))
            info.height = int(meta.get("height", 0))
        except (ValueError, TypeError):
            pass

        return info

    # ── 图片 EXIF 流程 ──
    if ext in _SKIP_EXIF_EXTENSIONS:
        return info

    tags = _read_exif_standard(filepath)

    if not tags and ext in (".heic", ".heif"):
        tags = _read_exif_pillow(filepath)

    if not tags:
        return info

    info.make = (
        tags.get("Image Make", "") or tags.get("Make", "")
    ).strip()
    info.model = (
        tags.get("Image Model", "") or tags.get("Model", "")
    ).strip()

    date_str = (
        tags.get("EXIF DateTimeOriginal", "")
        or tags.get("EXIF DateTimeDigitized", "")
        or tags.get("Image DateTime", "")
        or tags.get("DateTimeOriginal", "")
        or tags.get("DateTime", "")
    )
    if date_str:
        info.date_taken = _parse_exif_datetime(date_str)
        info.has_exif_date = info.date_taken is not None

    width_tag = tags.get("EXIF ExifImageWidth", "") or tags.get("ExifImageWidth", "")
    height_tag = tags.get("EXIF ExifImageLength", "") or tags.get("ExifImageLength", "")
    try:
        info.width = int(width_tag) if width_tag else 0
        info.height = int(height_tag) if height_tag else 0
    except (ValueError, TypeError):
        pass

    info.extra = {
        "lens": str(tags.get("EXIF LensModel", "") or tags.get("LensModel", "")),
        "iso": str(tags.get("EXIF ISOSpeedRatings", "") or tags.get("ISOSpeedRatings", "")),
        "focal_length": str(tags.get("EXIF FocalLength", "") or tags.get("FocalLength", "")),
        "aperture": str(tags.get("EXIF FNumber", "") or tags.get("FNumber", "")),
        "shutter": str(tags.get("EXIF ExposureTime", "") or tags.get("ExposureTime", "")),
        "software": str(tags.get("Image Software", "") or tags.get("Software", "")),
    }

    gps_lat = _extract_gps(tags)
    if gps_lat:
        info.extra["gps"] = gps_lat

    if not info.make:
        _infer_device_from_filename(info)

    return info


def _infer_device_from_filename(info: PhotoInfo) -> None:
    """根据文件名前缀推断设备（无 EXIF 品牌信息时的补充）"""
    if info.make:
        return
    basename = os.path.basename(info.filepath)
    name_lower = basename.lower()
    if name_lower.startswith("c360_") or name_lower.startswith("http_imgload"):
        info.make = "Xiaomi"
        info.model = "MiOne"


def _extract_gps(tags) -> str:
    """从 EXIF tags 提取 GPS 坐标，返回 '纬度, 经度' 或空串"""
    try:
        lat_ref = str(tags.get("GPS GPSLatitudeRef", ""))
        lon_ref = str(tags.get("GPS GPSLongitudeRef", ""))
        lat_tag = tags.get("GPS GPSLatitude", None)
        lon_tag = tags.get("GPS GPSLongitude", None)
        if not lat_tag or not lon_tag:
            return ""

        def _dms_to_decimal(dms_values):
            vals = dms_values.values
            d = float(vals[0])
            m = float(vals[1])
            s = float(vals[2])
            return d + m / 60.0 + s / 3600.0

        lat = _dms_to_decimal(lat_tag)
        lon = _dms_to_decimal(lon_tag)
        if lat_ref == "S":
            lat = -lat
        if lon_ref == "W":
            lon = -lon
        return f"{lat:.6f}, {lon:.6f}"
    except Exception:
        return ""


# ── 元数据缓存 ──
_CACHE_DIR = ".photo_organizer"
_META_CACHE_FILENAME = "meta_cache.json"
_META_CACHE_VERSION = 2  # v2: 修复华为等设备 EXIF 日期带中文后缀解析


def _photo_info_to_dict(info: PhotoInfo) -> dict:
    """将 PhotoInfo 序列化为可 JSON 存储的字典"""
    return {
        "media_type": info.media_type,
        "make": info.make,
        "model": info.model,
        "date_taken": info.date_taken.isoformat() if info.date_taken else None,
        "has_exif_date": info.has_exif_date,
        "file_modified": info.file_modified.isoformat() if info.file_modified else None,
        "file_size": info.file_size,
        "width": info.width,
        "height": info.height,
        "extra": info.extra,
    }


def _dict_to_photo_info(filepath: str, d: dict) -> PhotoInfo:
    """从缓存字典恢复 PhotoInfo"""
    dt = datetime.fromisoformat(d["date_taken"]) if d.get("date_taken") else None
    fm = datetime.fromisoformat(d["file_modified"]) if d.get("file_modified") else None
    return PhotoInfo(
        filepath=filepath,
        media_type=d.get("media_type", "photo"),
        make=d.get("make", ""),
        model=d.get("model", ""),
        date_taken=dt,
        has_exif_date=d.get("has_exif_date", False),
        file_modified=fm,
        file_size=d.get("file_size", 0),
        width=d.get("width", 0),
        height=d.get("height", 0),
        extra=d.get("extra", {}),
    )


def _try_make_cache_dir(base: str, subdir: str) -> Optional[str]:
    """尝试在 base 下创建缓存目录，成功返回目录路径，失败返回 None"""
    cache_dir = os.path.join(base, subdir)
    try:
        os.makedirs(cache_dir, exist_ok=True)
        return cache_dir
    except OSError:
        return None


def _meta_cache_path_for_drive(drive: str, hint_paths: Optional[list] = None) -> Optional[str]:
    """返回指定盘符的元数据缓存文件路径，无写入权限时沿扫描路径上溯"""
    cache_dir = _try_make_cache_dir(drive + os.sep, _CACHE_DIR)
    if not cache_dir and hint_paths:
        for hp in sorted(hint_paths, key=len):
            d = _try_make_cache_dir(hp, _CACHE_DIR)
            if d:
                cache_dir = d
                break
    if not cache_dir:
        logger.debug(f"无法创建缓存目录 {drive}\\{_CACHE_DIR}，跳过该盘缓存")
        return None
    return os.path.join(cache_dir, _META_CACHE_FILENAME)


def _load_meta_cache(cache_path: str) -> Dict[str, dict]:
    """加载元数据缓存，返回 {filepath: {mtime, size, info_dict}} """
    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("version") != _META_CACHE_VERSION:
            return {}
        return data.get("entries", {})
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError, ValueError) as e:
        logger.warning(f"加载元数据缓存失败: {e}，将重建")
        return {}


def _save_meta_cache(cache_path: str, entries: Dict[str, dict]) -> None:
    """原子写入元数据缓存"""
    data = {"version": _META_CACHE_VERSION, "entries": entries}
    tmp = cache_path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, cache_path)
        logger.info(f"元数据缓存已保存: {len(entries)} 条 → {cache_path}")
    except OSError as e:
        logger.warning(f"保存元数据缓存失败: {e}")
        try:
            os.unlink(tmp)
        except OSError:
            pass


def read_photo_infos_parallel(
    filepaths: List[str],
    max_workers: int = 8,
    progress_callback=None,
) -> List[PhotoInfo]:
    """多线程并行读取元数据信息（按盘逐个处理，读完一个盘立即保存缓存）。"""
    results: List[Optional[PhotoInfo]] = [None] * len(filepaths)

    _get_ffprobe_path()

    # ── 按盘符分组，记录每个文件在 results 中的索引 ──
    drive_indices: Dict[str, List[int]] = {}
    drive_dirs: Dict[str, List[str]] = {}
    for i, fp in enumerate(filepaths):
        drv = os.path.splitdrive(fp)[0].upper()
        if drv:
            drive_indices.setdefault(drv, []).append(i)
            drive_dirs.setdefault(drv, []).append(os.path.dirname(fp))

    total_cache_hits = 0

    # ── 逐盘处理：加载缓存 → 读取 → 保存缓存 ──
    for drv, indices in drive_indices.items():
        unique_hints = sorted(set(drive_dirs[drv]), key=len)[:5]
        cache_path = _meta_cache_path_for_drive(drv, unique_hints)
        cached = _load_meta_cache(cache_path) if cache_path else {}

        to_read: List[int] = []
        cache_hits = 0
        for i in indices:
            fp = filepaths[i]
            entry = cached.get(fp)
            if entry:
                try:
                    st = os.stat(fp)
                    if abs(st.st_mtime - entry["mtime"]) < 0.01 and st.st_size == entry["size"]:
                        results[i] = _dict_to_photo_info(fp, entry["info"])
                        cache_hits += 1
                        if progress_callback:
                            progress_callback()
                        continue
                except OSError:
                    pass
            to_read.append(i)

        total_cache_hits += cache_hits

        new_entries: Dict[str, dict] = {}
        if to_read:
            pool = ThreadPoolExecutor(max_workers=max_workers)
            try:
                futures = {pool.submit(read_photo_info, filepaths[i]): i for i in to_read}
                for future in _interruptible_as_completed(futures):
                    idx = futures[future]
                    fp = filepaths[idx]
                    try:
                        info = future.result()
                    except Exception:
                        info = PhotoInfo(filepath=fp)
                    results[idx] = info
                    try:
                        st = os.stat(fp)
                        new_entries[fp] = {
                            "mtime": st.st_mtime,
                            "size": st.st_size,
                            "info": _photo_info_to_dict(info),
                        }
                    except OSError:
                        pass
                    if progress_callback:
                        progress_callback()
            finally:
                pool.shutdown(wait=False, cancel_futures=True)

        # 立即保存该盘缓存
        if cache_path:
            valid_entries: Dict[str, dict] = {}
            for i in indices:
                fp = filepaths[i]
                entry = new_entries.get(fp) or cached.get(fp)
                if entry:
                    valid_entries[fp] = entry
            if valid_entries:
                _save_meta_cache(cache_path, valid_entries)

    if total_cache_hits:
        logger.info(f"元数据缓存命中 {total_cache_hits}/{len(filepaths)} 个文件，"
                     f"需读取: {len(filepaths) - total_cache_hits} 个")

    for i, r in enumerate(results):
        if r is None:
            results[i] = PhotoInfo(filepath=filepaths[i])

    return results  # type: ignore
