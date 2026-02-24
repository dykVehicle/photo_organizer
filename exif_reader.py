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


def _parse_exif_datetime(value: str) -> Optional[datetime]:
    formats = [
        "%Y:%m:%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y/%m/%d %H:%M:%S",
        "%Y:%m:%d",
        "%Y-%m-%d",
    ]
    cleaned = value.strip().rstrip("\x00")
    if not cleaned or cleaned.startswith("0000"):
        return None
    for fmt in formats:
        try:
            return datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
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

    return info


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
_META_CACHE_VERSION = 1


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


def _meta_cache_path_for_drive(drive: str) -> str:
    """返回指定盘符的元数据缓存文件路径"""
    cache_dir = os.path.join(drive + os.sep, _CACHE_DIR)
    os.makedirs(cache_dir, exist_ok=True)
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
    """多线程并行读取元数据信息（带磁盘缓存，mtime+size 未变的文件直接复用）。"""
    results: List[Optional[PhotoInfo]] = [None] * len(filepaths)

    _get_ffprobe_path()

    # ── 加载缓存，分离命中/未命中 ──
    cache_paths: Dict[str, str] = {}
    all_cached: Dict[str, dict] = {}
    drives = {os.path.splitdrive(fp)[0].upper() for fp in filepaths if os.path.splitdrive(fp)[0]}
    for drv in drives:
        cp = _meta_cache_path_for_drive(drv)
        cache_paths[drv] = cp
        all_cached.update(_load_meta_cache(cp))

    to_read: List[int] = []
    cache_hits = 0
    for i, fp in enumerate(filepaths):
        entry = all_cached.get(fp)
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

    if cache_hits:
        logger.info(f"元数据缓存命中 {cache_hits}/{len(filepaths)} 个文件，"
                     f"需读取: {len(to_read)} 个")

    # ── 仅对未命中缓存的文件读取元数据 ──
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

    for i, r in enumerate(results):
        if r is None:
            results[i] = PhotoInfo(filepath=filepaths[i])

    # ── 保存更新后的缓存（按盘符分别保存） ──
    valid_entries_by_drive: Dict[str, Dict[str, dict]] = {drv: {} for drv in drives}
    for fp in filepaths:
        drv = os.path.splitdrive(fp)[0].upper()
        entry = new_entries.get(fp) or all_cached.get(fp)
        if entry:
            valid_entries_by_drive.setdefault(drv, {})[fp] = entry
    for drv, entries in valid_entries_by_drive.items():
        if entries and drv in cache_paths:
            _save_meta_cache(cache_paths[drv], entries)

    return results  # type: ignore
