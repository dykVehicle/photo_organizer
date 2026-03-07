"""文件整理模块：复制、去重、目录管理、设备过滤（三阶段并行优化）"""

import json
import logging
import os
import re
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed, wait, FIRST_COMPLETED
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Set

try:
    import xxhash
    _USE_XXHASH = True
except ImportError:
    import hashlib
    _USE_XXHASH = False

from tqdm import tqdm

import config
from classifier import DeviceType, classify_device, is_target_device
from exif_reader import PhotoInfo, _parse_date_from_filename
from interrupt import is_interrupted

logger = logging.getLogger(__name__)

BUF_SIZE = 1024 * 1024
CACHE_DIR = ".photo_organizer"
HASH_CACHE_FILENAME = "hash_cache.json"
SRC_HASH_CACHE_FILENAME = "src_hash_cache.json"
_HASH_CACHE_VERSION = 2  # v2: xxhash + 16KB sample (v1 was MD5 + 64KB)


def _time_match(entry: dict, mtime: float, ctime: float = 0.0) -> bool:
    """缓存时间戳匹配：当前文件的 mtime/ctime 任一与缓存中的 mtime/ctime 任一匹配即命中。
    兼容复制后文件只保留 mtime 或 ctime 的情况。"""
    tol = 0.01
    cached_mtime = entry.get("mtime", 0.0)
    cached_ctime = entry.get("ctime", 0.0)
    candidates = [t for t in (cached_mtime, cached_ctime) if t > 0]
    for cur in (mtime, ctime):
        if cur <= 0:
            continue
        for ref in candidates:
            if abs(cur - ref) < tol:
                return True
    return False


def _get_backup_cache_dir():
    """获取目标盘上的统一备份缓存目录"""
    ref = config.REPORT_DIR or config.DEST_CAMERA
    if not ref:
        return None
    drv = os.path.splitdrive(ref)[0]
    if not drv:
        return None
    d = os.path.join(drv + os.sep, CACHE_DIR)
    try:
        os.makedirs(d, exist_ok=True)
        return d
    except OSError:
        return None


def _load_hash_cache(cache_path: str) -> Dict[str, dict]:
    """从磁盘加载哈希缓存，返回 {filepath: {hash, mtime, size}} 字典"""
    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("version") != _HASH_CACHE_VERSION:
            logger.info("哈希缓存版本不匹配，将重建")
            return {}
        return data.get("entries", {})
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError, ValueError) as e:
        logger.warning(f"加载哈希缓存失败: {e}，将重建")
        return {}


def _save_hash_cache(cache_path: str, entries: Dict[str, dict]) -> None:
    """原子写入哈希缓存到磁盘（先写临时文件再 rename）"""
    data = {"version": _HASH_CACHE_VERSION, "entries": entries}
    tmp = cache_path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, cache_path)
        logger.info(f"哈希缓存已保存: {len(entries)} 条 → {cache_path}")
    except OSError as e:
        logger.warning(f"保存哈希缓存失败: {e}")
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _interruptible_as_completed(futures, timeout_per_poll=1.0):
    """
    可中断的 as_completed 替代：每隔 timeout_per_poll 秒检查一次中断标志。
    解决 Windows 上 as_completed 阻塞导致 Ctrl+C 无法中断的问题。
    """
    pending = set(futures)
    while pending:
        if is_interrupted():
            for f in pending:
                f.cancel()
            return
        done, pending = wait(pending, timeout=timeout_per_poll, return_when=FIRST_COMPLETED)
        yield from done
COPY_BUF = 4 * 1024 * 1024  # 4 MB — 复制文件缓冲
MAX_COPY_WORKERS = 8        # 复制阶段最大并发（减少 HDD 磁头跳转）


@dataclass
class CopyRecord:
    """单条复制记录"""
    source: str
    destination: str
    device_type: str
    media_type: str          # "photo" | "video"
    make: str
    model: str
    date_taken: str
    has_exif_date: bool
    file_size: int
    # ok / skipped_dup / skipped_exists / skipped_filtered /
    # skipped_not_target / skipped_no_device / error / dry_run
    status: str = "ok"
    error_msg: str = ""
    file_hash: str = ""
    dup_of: str = ""         # 重复时记录原始文件路径
    extra_info: Dict = field(default_factory=dict)  # 拍摄参数等额外信息


@dataclass
class OrganizeResult:
    """整理结果汇总"""
    records: List[CopyRecord] = field(default_factory=list)
    total_found: int = 0
    copied: int = 0
    skipped_dup: int = 0
    skipped_exists: int = 0
    skipped_filtered: int = 0
    skipped_not_target: int = 0
    skipped_no_device: int = 0
    nsfw_count: int = 0
    document_count: int = 0
    errors: int = 0


def _get_dest_root(device_type: DeviceType, is_target: bool = True) -> str:
    if is_target or not config.DEST_CAMERA_OTHER:
        mapping = {
            DeviceType.CAMERA: config.DEST_CAMERA,
            DeviceType.PHONE: config.DEST_PHONE,
            DeviceType.UNKNOWN: config.DEST_UNKNOWN,
        }
    else:
        mapping = {
            DeviceType.CAMERA: config.DEST_CAMERA_OTHER,
            DeviceType.PHONE: config.DEST_PHONE_OTHER,
            DeviceType.UNKNOWN: config.DEST_UNKNOWN,
        }
    return mapping[device_type]


def _get_quarter(month: int) -> int:
    return (month - 1) // 3 + 1


def _quarter_label(month: int) -> str:
    q = _get_quarter(month)
    m_start = (q - 1) * 3 + 1
    m_end = q * 3
    months = "-".join(str(m) for m in range(m_start, m_end + 1))
    return f"Q{q}-M{months}"


def _sanitize_folder_name(name: str) -> str:
    name = name.strip()
    name = re.sub(r'[\x00-\x1f<>:"/\\|?*]', '_', name)
    name = re.sub(r'\s+', ' ', name)
    name = name.strip('. ')
    return name or "未知型号"


def _build_device_label(info: PhotoInfo) -> str:
    make = info.make.strip() if info.make else ""
    model = info.model.strip() if info.model else ""
    make_lower = make.lower()

    # 1. Make 规范化（如 "NIKON CORPORATION" → "Nikon"）
    normalized = config.MAKE_NORMALIZE.get(make_lower)
    if normalized:
        make = normalized
        make_lower = make.lower()

    # 2. 去除 model 中重复的 make 前缀（如 Nikon "NIKON D70s" → "D70s"）
    model_lower = model.lower()
    if make_lower and model_lower.startswith(make_lower):
        model = model[len(make_lower):].strip()
        model_lower = model.lower()

    # 3. Model 别名规范化（合并变体，如 "mione_plus" → "MiOne"）
    alias = config.MODEL_ALIASES.get(model_lower)
    if alias:
        model = alias
        model_lower = model.lower()

    if not make and not model:
        label = "未知型号"
    else:
        # 4. 按品牌查找营销名
        marketing = None
        if "dji" in make_lower:
            merged = config.DJI_MODEL_NAMES.get(model_lower)
            if not merged:
                writing_app = (info.extra.get("writing_application") or "").lower()
                for app_key, name in config.DJI_APP_NAMES.items():
                    if app_key in writing_app:
                        merged = name
                        break
            if merged:
                label = merged
                marketing = "__done__"
        elif any(kw in make_lower for kw in ("xiaomi", "redmi", "poco")):
            marketing = config.XIAOMI_MODEL_NAMES.get(model_lower)
            if not marketing:
                code = config._XIAOMI_NAME_TO_CODE.get(f"{make_lower} {model_lower}".strip())
                if code:
                    label = _sanitize_folder_name(f"{make} {model} {code}")
                    marketing = "__done__"
            if marketing != "__done__":
                pass
        elif "huawei" in make_lower:
            marketing = config.HUAWEI_MODEL_NAMES.get(model_lower)
        elif "samsung" in make_lower:
            marketing = config.SAMSUNG_MODEL_NAMES.get(model_lower)

        # 5. 构建标签
        if marketing == "__done__":
            pass
        elif marketing and model:
            if marketing.lower().startswith(make_lower):
                label = _sanitize_folder_name(f"{marketing} {model}")
            else:
                label = _sanitize_folder_name(f"{make} {marketing} {model}")
        elif make and model:
            label = _sanitize_folder_name(f"{make} {model}")
        else:
            label = _sanitize_folder_name(make or model)

    # 6. 源目录后缀（区分同型号不同手机）
    for src_dir, suffix in config.SOURCE_DEVICE_SUFFIX.items():
        if os.path.normcase(info.filepath).startswith(os.path.normcase(src_dir) + os.sep):
            label = f"{label} ({suffix})"
            break
    else:
        # 7. 从输出目录的设备文件夹名继承后缀（防止 wz 等自定义后缀丢失）
        _out = config.REPORT_DIR
        if _out:
            _snc = os.path.normcase(info.filepath)
            _onc = os.path.normcase(_out) + os.sep
            if _snc.startswith(_onc):
                _parts = info.filepath[len(_out) + 1:].split(os.sep)
                if len(_parts) >= 3:
                    _top = _parts[0].lower()
                    if any(_top.startswith(p) for p in
                           ("all_1_", "all_2_", "all_3_", "all_4_", "all_5_")):
                        _existing = _parts[1]
                        _m = re.match(r'^(.+?)\s*\((\w+)\)$', _existing)
                        if _m and _m.group(1).strip().lower() == label.lower():
                            label = _existing

    return label


_MIN_REASONABLE_YEAR = 1993
from datetime import timedelta as _timedelta
_MAX_REASONABLE_DATE = datetime.now() + _timedelta(days=1)


def _is_reasonable_date(dt: datetime) -> bool:
    return dt.year >= _MIN_REASONABLE_YEAR and dt <= _MAX_REASONABLE_DATE


def _get_effective_date(info: PhotoInfo, device_type: DeviceType):
    # 优先级: EXIF 日期 > 文件修改时间 > 文件名日期
    if info.has_exif_date and info.date_taken:
        return info.date_taken, True
    if info.file_modified and _is_reasonable_date(info.file_modified):
        return info.file_modified, True
    fname_date = _parse_date_from_filename(os.path.basename(info.filepath))
    if fname_date:
        return fname_date, True
    return None, False


def _build_dest_path(device_type: DeviceType, info: PhotoInfo, is_target: bool = True) -> str:
    root = _get_dest_root(device_type, is_target)
    filename = os.path.basename(info.filepath)
    effective_date, is_exif = _get_effective_date(info, device_type)

    if device_type in (DeviceType.CAMERA, DeviceType.PHONE):
        device_label = _build_device_label(info)
        if effective_date:
            date_folder = f"{effective_date.year}-{_quarter_label(effective_date.month)}"
            return os.path.join(root, device_label, date_folder, filename)
        else:
            return os.path.join(root, device_label, config.NO_EXIF_DATE_FOLDER, filename)
    else:
        if effective_date:
            date_folder = f"{effective_date.year}-{_quarter_label(effective_date.month)}"
            if not is_exif:
                date_folder += "_按修改时间"
            return os.path.join(root, date_folder, filename)
        else:
            return os.path.join(root, config.NO_EXIF_DATE_FOLDER, filename)


def _resolve_conflict(dest_path: str, assigned: Optional[Set[str]] = None) -> str:
    """路径去冲突：同时检查磁盘已有文件和本批次已分配的目标路径。"""
    norm = os.path.normcase(dest_path)
    if not os.path.exists(dest_path) and (assigned is None or norm not in assigned):
        return dest_path
    base, ext = os.path.splitext(dest_path)
    counter = 1
    while True:
        new_path = f"{base}_po{counter}{ext}"
        norm_new = os.path.normcase(new_path)
        if not os.path.exists(new_path) and (assigned is None or norm_new not in assigned):
            return new_path
        counter += 1


HASH_SAMPLE_SIZE = 16 * 1024  # 16 KB（从 64KB 减少，单次 SMB 读取即可完成）


def _file_fast_hash(filepath: str, file_size: int) -> str:
    """
    快速去重哈希：file_size + 首 16KB + 尾 16KB。
    使用 xxhash（比 MD5 快 ~10x），碰撞概率可忽略。
    小文件（≤32KB）只读一次，避免 seek 开销。
    """
    if _USE_XXHASH:
        h = xxhash.xxh3_64()
    else:
        import hashlib
        h = hashlib.md5()
    h.update(file_size.to_bytes(8, "little"))
    with open(filepath, "rb") as f:
        head = f.read(HASH_SAMPLE_SIZE)
        h.update(head)
        if file_size > HASH_SAMPLE_SIZE * 2:
            f.seek(-HASH_SAMPLE_SIZE, 2)
            h.update(f.read())
    return h.hexdigest()


def should_filter_small_image(info: PhotoInfo) -> bool:
    if info.media_type == "video":
        return False
    if info.file_size > 0 and info.file_size < config.MIN_FILE_SIZE_BYTES:
        return True
    if config.MIN_IMAGE_DIMENSION > 0 and info.width > 0 and info.height > 0:
        if info.width < config.MIN_IMAGE_DIMENSION and info.height < config.MIN_IMAGE_DIMENSION:
            return True
    return False


def copy_photo(
    info: PhotoInfo,
    device_type: DeviceType,
    seen_hashes: Set[str],
    dry_run: bool = False,
) -> CopyRecord:
    dest_path = _build_dest_path(device_type, info)
    effective_date, _ = _get_effective_date(info, device_type)
    date_str = effective_date.strftime("%Y-%m-%d %H:%M:%S") if effective_date else ""

    record = CopyRecord(
        source=info.filepath,
        destination=dest_path,
        device_type=device_type.value,
        media_type=info.media_type,
        make=info.make,
        model=info.model,
        date_taken=date_str,
        has_exif_date=info.has_exif_date,
        file_size=info.file_size,
    )

    if device_type == DeviceType.UNKNOWN and should_filter_small_image(info):
        record.status = "skipped_filtered"
        return record

    try:
        file_hash = _file_fast_hash(info.filepath, info.file_size)
    except Exception as e:
        record.status = "error"
        record.error_msg = f"计算哈希失败: {e}"
        return record

    if file_hash in seen_hashes:
        record.status = "skipped_dup"
        return record

    seen_hashes.add(file_hash)

    dest_path = _resolve_conflict(dest_path)
    record.destination = dest_path

    if dry_run:
        record.status = "dry_run"
        return record

    try:
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        shutil.copy2(info.filepath, dest_path)
        record.status = "ok"
    except Exception as e:
        record.status = "error"
        record.error_msg = str(e)

    return record


# ── 三阶段并行处理（带设备过滤） ──


def _is_screenshot(filepath: str) -> bool:
    """通过文件名关键词判断是否为截图"""
    filename = os.path.basename(filepath).lower()
    return any(kw in filename for kw in config.SCREENSHOT_KEYWORDS)


@dataclass
class _PreparedItem:
    info: PhotoInfo
    device_type: DeviceType
    record: CopyRecord
    file_hash: Optional[str] = None
    needs_copy: bool = True
    is_target: bool = True
    is_screenshot: bool = False
    is_dji: bool = False
    _file_mtime: float = 0.0
    _file_ctime: float = 0.0
    nsfw_score: float = -1.0  # -1 表示未检测
    document_score: float = -1.0  # -1 表示未检测


def _make_record(info: PhotoInfo, device_type: DeviceType, is_target: bool = True) -> CopyRecord:
    effective_date, _ = _get_effective_date(info, device_type)
    date_str = effective_date.strftime("%Y-%m-%d %H:%M:%S") if effective_date else ""
    dest_path = _build_dest_path(device_type, info, is_target) if device_type != DeviceType.UNKNOWN or info.has_exif_date or info.file_modified else ""

    note = ""
    device_source = info.extra.get("device_source", "")
    if device_source:
        note = device_source
    elif device_type == DeviceType.UNKNOWN:
        if info.media_type == "video" and not info.make:
            note = "无法识别视频设备信息"
        elif not info.make:
            note = "无EXIF设备信息"
    elif not info.has_exif_date and not info.date_taken:
        note = "无EXIF日期，使用文件修改时间" if info.file_modified else "无任何日期信息"

    return CopyRecord(
        source=info.filepath,
        destination=dest_path,
        device_type=device_type.value,
        media_type=info.media_type,
        make=info.make,
        model=info.model,
        date_taken=date_str,
        has_exif_date=info.has_exif_date,
        file_size=info.file_size,
        error_msg=note,
        extra_info={k: v for k, v in info.extra.items() if v and k != "device_source"},
    )


def _prepare_one(
    info: PhotoInfo,
    target_devices: Optional[Set[str]],
    copy_all: bool,
    copy_unknown: bool,
    copy_unknown_photo: bool = False,
    copy_unknown_video: bool = False,
    src_hash_entries: Optional[Dict[str, dict]] = None,
) -> _PreparedItem:
    """阶段 1：分类 + 设备过滤 + 小图过滤 + 计算哈希（带源盘缓存）"""
    if is_interrupted():
        device_type = classify_device(info)
        record = _make_record(info, device_type)
        return _PreparedItem(info=info, device_type=device_type, record=record, needs_copy=False)

    device_type = classify_device(info)

    # 截图检测（截图文件走独立目录，绕过设备过滤）
    screenshot = _is_screenshot(info.filepath)

    # DJI 检测（通过 make 或 writing_application 识别）
    is_dji = "dji" in (info.make or "").lower()
    if not is_dji:
        writing_app = (info.extra.get("writing_application") or "").lower()
        if writing_app.startswith("dji"):
            is_dji = True
            if not info.make:
                info.make = "DJI"

    # 判断是否为目标设备（影响文件夹路由）
    if device_type == DeviceType.UNKNOWN:
        is_target = False
    else:
        is_target = is_target_device(info, target_devices) if target_devices else True

    record = _make_record(info, device_type, is_target)
    item = _PreparedItem(info=info, device_type=device_type, record=record,
                         is_target=is_target, is_screenshot=screenshot, is_dji=is_dji)

    # 文件已在输出目录的已分类文件夹中 → 跳过设备过滤，后续路由会保留原位
    _src_already_classified = False
    if config.REPORT_DIR:
        _snc = os.path.normcase(info.filepath)
        _onc = os.path.normcase(config.REPORT_DIR) + os.sep
        if _snc.startswith(_onc):
            _top = _snc[len(_onc):].split(os.sep)[0]
            _CLS = ("all_1_", "all_2_", "all_3_", "all_4_", "all_5_", "all_7_", "all_999_")
            _src_already_classified = any(_top.startswith(p) for p in _CLS)

    # ── 设备过滤（截图和 DJI 不受设备过滤影响，已分类文件跳过） ──
    if not screenshot and not is_dji and not _src_already_classified:
        if device_type == DeviceType.UNKNOWN:
            should_copy_unknown = (
                copy_unknown
                or (copy_unknown_photo and info.media_type == "photo")
                or (copy_unknown_video and info.media_type == "video")
            )
            if not should_copy_unknown:
                if info.make:
                    record.status = "skipped_not_target"
                else:
                    record.status = "skipped_no_device"
                item.needs_copy = False
                return item
        elif not copy_all:
            if not is_target:
                record.status = "skipped_not_target"
                item.needs_copy = False
                return item

    # ── 小图过滤（已分类文件夹中的文件跳过） ──
    if (not _src_already_classified
            and device_type == DeviceType.UNKNOWN and should_filter_small_image(info)):
        record.status = "skipped_filtered"
        item.needs_copy = False
        return item

    # ── 计算哈希（带源盘缓存，复用 PhotoInfo 已有的 size/mtime 避免额外 stat） ──
    try:
        file_mtime = info.file_modified.timestamp() if info.file_modified else None
        try:
            _st = os.stat(info.filepath)
            file_ctime = _st.st_ctime
            if file_mtime is None:
                file_mtime = _st.st_mtime
        except OSError:
            file_ctime = 0.0
        if src_hash_entries and file_mtime is not None:
            cached = src_hash_entries.get(info.filepath)
            if (cached
                    and _time_match(cached, file_mtime, file_ctime)
                    and info.file_size == cached["size"]):
                item.file_hash = cached["hash"]
                item._file_mtime = file_mtime
                item._file_ctime = file_ctime
                return item
        item.file_hash = _file_fast_hash(info.filepath, info.file_size)
        if file_mtime is None:
            file_mtime = os.stat(info.filepath).st_mtime
        item._file_mtime = file_mtime
        item._file_ctime = file_ctime
    except Exception as e:
        record.status = "error"
        record.error_msg = f"计算哈希失败: {e}"
        item.needs_copy = False

    return item


def _fast_copy(src: str, dst: str) -> str:
    """
    4MB 缓冲复制 + 尝试保留时间戳。
    返回空字符串表示完全成功，非空字符串为 copystat 警告（文件已复制）。
    """
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        while True:
            buf = fin.read(COPY_BUF)
            if not buf:
                break
            fout.write(buf)
    try:
        shutil.copystat(src, dst)
    except OSError as e:
        return f"文件已复制，但时间戳保留失败: {e}"
    return ""


def _scan_dest_drive_for_reuse(dest_drive: str, max_workers: int = 8):
    """
    扫描目标盘已有的媒体文件，建立 fast_hash → filepath 索引。
    复制阶段可以先查此索引：命中时直接 rename（同盘秒移），避免跨盘复制。

    返回 (hash_index, dup_files):
      hash_index: {hash → filepath} 每个哈希保留一个文件用于复用
      dup_files: 同哈希的多余重复文件列表，可安全删除

    使用磁盘缓存加速：对 mtime+size 未变的文件直接复用缓存哈希，
    仅对新增/变更文件计算哈希，大幅减少重复运行的耗时。
    """
    from config import MEDIA_EXTENSIONS, EXCLUDED_DIRS
    hash_index: Dict[str, str] = {}
    dup_files: List[str] = []
    all_files: List[str] = []

    def _is_album_dir(path: str) -> bool:
        """判断文件是否在 All_相册 开头的目录下（旧整理输出，优先复用）"""
        parts = os.path.normpath(path).split(os.sep)
        return any(p.startswith("All_相册") for p in parts)

    logger.info(f"扫描目标盘 {dest_drive} 已有文件用于同盘复用...")
    for root, dirs, files in os.walk(dest_drive):
        dirs[:] = [d for d in dirs if d.lower() not in EXCLUDED_DIRS]
        for fn in files:
            if os.path.splitext(fn)[1].lower() in MEDIA_EXTENSIONS:
                all_files.append(os.path.join(root, fn))

    if not all_files:
        logger.info("目标盘无已有媒体文件")
        return hash_index, dup_files

    logger.info(f"目标盘发现 {len(all_files)} 个已有媒体文件，正在建立哈希索引...")

    # ── 加载缓存，分离命中/未命中 ──
    cache_dir = os.path.join(dest_drive, CACHE_DIR)
    os.makedirs(cache_dir, exist_ok=True)
    cache_path = os.path.join(cache_dir, HASH_CACHE_FILENAME)
    cached_entries = _load_hash_cache(cache_path)
    to_hash: List[str] = []
    valid_entries: Dict[str, dict] = {}
    cache_hits = 0

    for fp in all_files:
        entry = cached_entries.get(fp)
        if entry:
            try:
                st = os.stat(fp)
                if _time_match(entry, st.st_mtime, st.st_ctime) and st.st_size == entry["size"]:
                    h = entry["hash"]
                    if h not in hash_index:
                        hash_index[h] = fp
                    elif _is_album_dir(fp) and not _is_album_dir(hash_index[h]):
                        dup_files.append(hash_index[h])
                        hash_index[h] = fp
                    else:
                        dup_files.append(fp)
                    valid_entries[fp] = entry
                    cache_hits += 1
                    continue
            except OSError:
                pass
        to_hash.append(fp)

    if cache_hits:
        logger.info(f"哈希缓存命中 {cache_hits}/{len(all_files)} 个文件，"
                     f"需计算哈希: {len(to_hash)} 个")

    # ── 仅对未命中缓存的文件计算哈希 ──
    if to_hash:
        def _hash_one(fp: str):
            if is_interrupted():
                return fp, None, 0.0, 0
            try:
                st = os.stat(fp)
                h = _file_fast_hash(fp, st.st_size)
                return fp, h, st.st_mtime, st.st_size
            except Exception:
                return fp, None, 0.0, 0

        workers = max_workers
        with tqdm(total=len(to_hash), desc="索引目标盘(新)", unit="个") as pbar:
            pool = ThreadPoolExecutor(max_workers=workers)
            try:
                futs = {pool.submit(_hash_one, fp): fp for fp in to_hash}
                for fut in _interruptible_as_completed(futs):
                    fp, h, mtime, size = fut.result()
                    if h:
                        if h not in hash_index:
                            hash_index[h] = fp
                        elif _is_album_dir(fp) and not _is_album_dir(hash_index[h]):
                            dup_files.append(hash_index[h])
                            hash_index[h] = fp
                        else:
                            dup_files.append(fp)
                        e = {"hash": h, "mtime": mtime, "size": size}
                        try:
                            e["ctime"] = os.stat(fp).st_ctime
                        except OSError:
                            pass
                        valid_entries[fp] = e
                    pbar.update(1)
            finally:
                pool.shutdown(wait=False, cancel_futures=True)

    # ── 保存更新后的缓存 ──
    _save_hash_cache(cache_path, valid_entries)

    logger.info(f"目标盘哈希索引: {len(hash_index)} 个唯一文件")
    if dup_files:
        logger.info(f"目标盘发现 {len(dup_files)} 个重复文件待清理")
    return hash_index, dup_files


def _append_note(record: CopyRecord, msg: str):
    """向备注追加信息，不覆盖已有内容"""
    if record.error_msg:
        record.error_msg += "; " + msg
    else:
        record.error_msg = msg


_JUNK_FILES = {"filelist.txt", "thumbs.db", "desktop.ini", ".ds_store"}


def _cleanup_empty_dirs(root: str, log: logging.Logger) -> None:
    """自底向上删除空文件夹（含级联：子目录删除后父目录也会被检查）。
    仅含系统垃圾文件（Thumbs.db、desktop.ini 等）或 filelist.txt 的文件夹也视为空。
    跳过 .photo_organizer 缓存目录。"""
    removed = 0
    for dirpath, _dirnames, _filenames in os.walk(root, topdown=False):
        if dirpath == root:
            continue
        if os.path.basename(dirpath) == CACHE_DIR:
            continue
        try:
            entries = os.listdir(dirpath)
            if not entries or {e.lower() for e in entries} <= _JUNK_FILES:
                for e in entries:
                    os.remove(os.path.join(dirpath, e))
                os.rmdir(dirpath)
                removed += 1
        except OSError:
            pass
    if removed:
        log.info(f"清理空文件夹: {removed} 个")


_reuse_lock = threading.Lock()


def _same_drive(a: str, b: str) -> bool:
    return os.path.splitdrive(a)[0].upper() == os.path.splitdrive(b)[0].upper()


def _do_copy(item: _PreparedItem, reuse_index: Optional[Dict[str, str]] = None) -> None:
    """
    阶段 3：复制文件（目录已预建）。
    优先级：
      1. 源文件与目标同盘 → 直接 shutil.copy2（保留源文件）
      2. reuse_index 命中且同盘 → os.rename 旧文件到新位置（秒移）
      3. 跨盘 → _fast_copy
    注意：源文件不做 rename/move，因为我们不应修改源盘内容。
    同盘 copy2 走 NTFS 缓存，比跨盘快 10x+。
    """
    if is_interrupted():
        item.record.status = "error"
        item.record.error_msg = "用户中断"
        return

    dest = item.record.destination
    src = item.info.filepath

    # 优先：reuse_index 命中 → rename 旧文件到新位置（秒移，零 I/O）
    # NSFW 文件不用 reuse（移动会导致原始文件丢失，删 NSFW 目录后不可恢复）
    nsfw_dest = config.DEST_NSFW and os.path.normcase(dest).startswith(
        os.path.normcase(config.DEST_NSFW))
    reused = False
    if reuse_index and item.file_hash and not nsfw_dest:
        existing = None
        with _reuse_lock:
            existing = reuse_index.pop(item.file_hash, None)
        if existing and _same_drive(existing, dest):
            protected = any(
                os.path.normcase(existing).startswith(os.path.normcase(d) + os.sep)
                for d in config.REUSE_PROTECTED_DIRS
            )
            try:
                if protected:
                    shutil.copy2(existing, dest)
                    item.record.status = "ok"
                    _append_note(item.record, f"同盘复制自(保护目录): {existing}")
                else:
                    os.rename(existing, dest)
                    item.record.status = "ok"
                    _append_note(item.record, f"同盘移动自: {existing}")
                reused = True
            except Exception:
                reused = False

    if not reused:
        try:
            if _same_drive(src, dest):
                shutil.copy2(src, dest)
                item.record.status = "ok"
                _append_note(item.record, "同盘复制")
            else:
                warning = _fast_copy(src, dest)
                item.record.status = "ok"
                if warning:
                    _append_note(item.record, warning)
        except Exception as e:
            item.record.status = "error"
            item.record.error_msg = str(e)


def _build_nsfw_dest(item: _PreparedItem) -> str:
    """为 NSFW 文件构建目标路径: All_7_NSFW/{原始分类路径}，保留设备/时间目录结构"""
    original_dest = item.record.destination
    output_root = config.REPORT_DIR
    try:
        rel = os.path.relpath(original_dest, output_root)
    except ValueError:
        rel = os.path.basename(original_dest)
    return os.path.join(config.DEST_NSFW, rel)


def _build_screenshot_dest(item: _PreparedItem) -> str:
    """为截图文件构建目标路径: All_8_截图/{设备名/日期/文件名}，保留设备/时间目录结构"""
    info = item.info
    filename = os.path.basename(info.filepath)
    device_type = item.device_type
    effective_date, _ = _get_effective_date(info, device_type)

    if device_type in (DeviceType.CAMERA, DeviceType.PHONE):
        device_label = _build_device_label(info)
        if effective_date:
            date_folder = f"{effective_date.year}-{_quarter_label(effective_date.month)}"
            return os.path.join(config.DEST_SCREENSHOT, device_label, date_folder, filename)
        else:
            return os.path.join(config.DEST_SCREENSHOT, device_label, config.NO_EXIF_DATE_FOLDER, filename)
    else:
        if effective_date:
            date_folder = f"{effective_date.year}-{_quarter_label(effective_date.month)}"
            return os.path.join(config.DEST_SCREENSHOT, date_folder, filename)
        else:
            return os.path.join(config.DEST_SCREENSHOT, config.NO_EXIF_DATE_FOLDER, filename)


def _build_document_dest(item: _PreparedItem) -> str:
    """为文档图片构建目标路径: All_7_文档图片/{原始分类相对路径}"""
    original_dest = item.record.destination
    output_root = config.REPORT_DIR
    try:
        rel = os.path.relpath(original_dest, output_root)
    except ValueError:
        rel = os.path.basename(original_dest)
    return os.path.join(config.DEST_DOCUMENT, rel)


def _build_dji_dest_path(item: _PreparedItem) -> str:
    """为 DJI 文件构建目标路径: All_3_DJI_大疆/{设备名}/{日期}/{文件名}"""
    info = item.info
    filename = os.path.basename(info.filepath)
    device_label = _build_device_label(info)
    effective_date, _ = _get_effective_date(info, item.device_type)

    if effective_date:
        date_folder = f"{effective_date.year}-{_quarter_label(effective_date.month)}"
        return os.path.join(config.DEST_DJI, device_label, date_folder, filename)
    else:
        return os.path.join(config.DEST_DJI, device_label, config.NO_EXIF_DATE_FOLDER, filename)


def _load_nsfw_cache(cache_path: str) -> dict:
    """加载 NSFW 分数缓存 {file_hash: score}"""
    if os.path.isfile(cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            logger.info("NSFW 缓存命中 %d 条: %s", len(data), cache_path)
            return data
        except Exception as e:
            logger.debug("NSFW 缓存加载失败: %s", e)
    return {}


def _save_nsfw_cache(cache_path: str, cache: dict) -> None:
    """增量保存 NSFW 分数缓存"""
    try:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(cache, f)
        logger.info("NSFW 缓存已保存: %d 条 → %s", len(cache), cache_path)
    except Exception as e:
        logger.debug("NSFW 缓存保存失败: %s", e)


def _run_nsfw_detection(items: List[_PreparedItem], detector, max_workers: int):
    """阶段 1.5：双缓冲流水线 NSFW 检测（GPU 推理与 CPU 预处理并行，支持缓存断点续测）"""
    from nsfw_detector import _preprocess_single
    from concurrent.futures import as_completed
    import numpy as np

    from nsfw_detector import _RAW_EXTENSIONS
    image_exts = config.IMAGE_EXTENSIONS
    video_exts = config.VIDEO_EXTENSIONS

    image_items = []
    video_items = []
    raw_skipped = 0
    for it in items:
        if not it.needs_copy:
            continue
        ext = os.path.splitext(it.info.filepath)[1].lower()
        if ext in _RAW_EXTENSIONS:
            raw_skipped += 1
            continue
        if ext in image_exts:
            image_items.append(it)
        elif ext in video_exts:
            video_items.append(it)

    # 按路径排序减少 HDD 磁头跳转
    image_items.sort(key=lambda it: it.info.filepath)

    total = len(image_items) + len(video_items)
    if total == 0:
        return
    if raw_skipped:
        logger.info(f"NSFW 跳过 RAW 文件: {raw_skipped} 个（CR2/CR3/NEF/ARW 等不检测）")

    # ── 加载 NSFW 缓存（放在目标盘根目录，跨输出目录复用）──
    dest_drive = os.path.splitdrive(config.DEST_CAMERA or config.REPORT_DIR)[0]
    if dest_drive:
        cache_dir = os.path.join(dest_drive + os.sep, CACHE_DIR)
    elif config.REPORT_DIR:
        cache_dir = os.path.join(config.REPORT_DIR, CACHE_DIR)
    else:
        cache_dir = ""
    cache_path = os.path.join(cache_dir, "nsfw_score_cache.json") if cache_dir else ""
    nsfw_cache = _load_nsfw_cache(cache_path) if cache_path else {}

    _original_cached = set(nsfw_cache.keys())
    cached_count = 0
    need_detect_images = []
    need_detect_videos = []
    for it in image_items:
        h = it.file_hash
        if h and h in nsfw_cache:
            it.nsfw_score = nsfw_cache[h]
            cached_count += 1
        else:
            need_detect_images.append(it)
    for it in video_items:
        h = it.file_hash
        if h and h in nsfw_cache:
            it.nsfw_score = nsfw_cache[h]
            cached_count += 1
        else:
            need_detect_videos.append(it)

    need_total = len(need_detect_images) + len(need_detect_videos)

    BATCH_SIZE = 256
    PREPROCESS_WORKERS = min(max_workers, 8)
    VIDEO_WORKERS = min(max_workers, 4)

    logger.info(
        f"NSFW 检测: {len(image_items)} 图片 + {len(video_items)} 视频, "
        f"缓存命中 {cached_count}, 需检测 {need_total} "
        f"(batch={BATCH_SIZE}, {PREPROCESS_WORKERS} 预处理线程)"
    )

    nsfw_found = sum(1 for it in image_items + video_items
                     if it.nsfw_score >= detector.threshold)
    save_interval = 2000

    with tqdm(total=total, desc="NSFW 检测", unit="个", initial=cached_count) as pbar:
        # ── 图片：双缓冲流水线 ──
        if need_detect_images and not is_interrupted():
            detector._ensure_model()
            pool = ThreadPoolExecutor(max_workers=PREPROCESS_WORKERS)

            batches = [need_detect_images[i:i + BATCH_SIZE]
                       for i in range(0, len(need_detect_images), BATCH_SIZE)]

            def _submit_preprocess(batch_items):
                return [(it, pool.submit(_preprocess_single, it.info.filepath))
                        for it in batch_items]

            pending = _submit_preprocess(batches[0])
            processed_since_save = 0

            for bi in range(len(batches)):
                if is_interrupted():
                    break

                current = pending
                if bi + 1 < len(batches):
                    pending = _submit_preprocess(batches[bi + 1])

                arrays, valid_items = [], []
                for it, fut in current:
                    arr = fut.result()
                    if arr is not None:
                        arrays.append(arr)
                        valid_items.append(it)

                if arrays:
                    try:
                        scores = detector.run_batch_inference(np.stack(arrays))
                    except Exception as e:
                        logger.debug("NSFW batch 推理异常: %s", e)
                        scores = [0.0] * len(arrays)
                    for it, score in zip(valid_items, scores):
                        it.nsfw_score = score
                        if it.file_hash:
                            nsfw_cache[it.file_hash] = score

                batch_len = len(batches[bi])
                pbar.update(batch_len)
                processed_since_save += batch_len
                if cache_path and processed_since_save >= save_interval:
                    _save_nsfw_cache(cache_path, nsfw_cache)
                    processed_since_save = 0

            pool.shutdown(wait=False)

        # ── 图片阶段 2：NudeNet 精检（只检 Falconsai 粗筛通过的） ──
        coarse_thr = detector.COARSE_THRESHOLD
        suspect_images = [it for it in image_items
                         if it.nsfw_score >= coarse_thr and it.file_hash not in _original_cached]
        if suspect_images and not is_interrupted():
            logger.info("NudeNet 精检: %d 张可疑图片 (Falconsai >= %.1f)", len(suspect_images), coarse_thr)
            detector._ensure_nudenet()
            for i, it in enumerate(suspect_images):
                if is_interrupted():
                    break
                fine_score = detector.nudenet_check_image(it.info.filepath)
                it.nsfw_score = fine_score
                if it.file_hash:
                    nsfw_cache[it.file_hash] = fine_score
                if fine_score >= detector.threshold:
                    nsfw_found += 1
                if (i + 1) % 50 == 0:
                    logger.info("  NudeNet 精检进度: %d/%d", i + 1, len(suspect_images))
                    if cache_path:
                        _save_nsfw_cache(cache_path, nsfw_cache)

        # ── 视频：两阶段抽帧检测 ──
        if need_detect_videos and not is_interrupted():
            def _detect_video(it):
                try:
                    return detector.predict_video(it.info.filepath, max_frames=3)
                except Exception:
                    return 0.0

            vpool = ThreadPoolExecutor(max_workers=VIDEO_WORKERS)
            futs = {vpool.submit(_detect_video, it): it for it in need_detect_videos}
            processed_since_save = 0
            for fut in as_completed(futs):
                if is_interrupted():
                    break
                it = futs[fut]
                it.nsfw_score = fut.result()
                if it.file_hash:
                    nsfw_cache[it.file_hash] = it.nsfw_score
                if it.nsfw_score >= detector.threshold:
                    nsfw_found += 1
                pbar.update(1)
                processed_since_save += 1
                if cache_path and processed_since_save >= save_interval:
                    _save_nsfw_cache(cache_path, nsfw_cache)
                    processed_since_save = 0
            vpool.shutdown(wait=False)

    # 最终保存缓存
    if cache_path and nsfw_cache:
        _save_nsfw_cache(cache_path, nsfw_cache)

    nsfw_found = sum(1 for it in image_items + video_items if it.nsfw_score >= detector.threshold)
    logger.info(f"NSFW 检测完成: 检出 {nsfw_found} 个 NSFW 文件")


def _load_document_cache(cache_path: str) -> dict:
    """加载文档检测分数缓存 {file_hash: score}，校验模型版本"""
    from doc_detector import CACHE_MODEL_VERSION
    if os.path.isfile(cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and data.get("_model") == CACHE_MODEL_VERSION:
                scores = {k: v for k, v in data.items() if not k.startswith("_")}
                logger.info("文档检测缓存命中 %d 条 [%s]: %s",
                            len(scores), CACHE_MODEL_VERSION, cache_path)
                return scores
            old_ver = data.get("_model", "单模型") if isinstance(data, dict) else "单模型"
            logger.info("文档检测缓存版本不匹配 (%s → %s)，将重建",
                        old_ver, CACHE_MODEL_VERSION)
        except Exception as e:
            logger.debug("文档检测缓存加载失败: %s", e)
    return {}


def _save_document_cache(cache_path: str, cache: dict) -> None:
    """增量保存文档检测分数缓存（含模型版本标记）"""
    from doc_detector import CACHE_MODEL_VERSION
    try:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        save_data = dict(cache)
        save_data["_model"] = CACHE_MODEL_VERSION
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(save_data, f)
        logger.info("文档检测缓存已保存: %d 条 [%s] → %s",
                     len(cache), CACHE_MODEL_VERSION, cache_path)
    except Exception as e:
        logger.debug("文档检测缓存保存失败: %s", e)


def _run_document_detection(items: List[_PreparedItem], detector, max_workers: int):
    """阶段 1.6：双模型集成文档检测（docornot + doctype, 条件融合v2, GPU 加速, 支持缓存）"""
    from doc_detector import preprocess_chunk as _doc_preprocess_chunk
    from doc_detector import _RAW_EXTENSIONS as _DOC_RAW_EXTS
    from doc_detector import fuse_score as _fuse_score
    import numpy as np

    image_exts = config.IMAGE_EXTENSIONS

    image_items = []
    raw_skipped = 0
    screenshot_skipped = 0
    camera_skipped = 0
    for it in items:
        if not it.needs_copy:
            continue
        if it.is_screenshot:
            screenshot_skipped += 1
            continue
        if it.is_dji:
            continue
        if it.device_type == DeviceType.CAMERA:
            camera_skipped += 1
            continue
        ext = os.path.splitext(it.info.filepath)[1].lower()
        if ext in _DOC_RAW_EXTS:
            raw_skipped += 1
            continue
        if ext in image_exts and it.info.media_type == "photo":
            image_items.append(it)

    image_items.sort(key=lambda it: it.info.filepath)

    if not image_items:
        return

    if raw_skipped:
        logger.info(f"文档检测跳过 RAW: {raw_skipped} 个")
    if camera_skipped:
        logger.info(f"文档检测跳过相机照片: {camera_skipped} 个")
    if screenshot_skipped:
        logger.info(f"文档检测跳过截图: {screenshot_skipped} 个（默认归入文档类）")

    dest_drive = os.path.splitdrive(config.DEST_CAMERA or config.REPORT_DIR)[0]
    if dest_drive:
        cache_dir = os.path.join(dest_drive + os.sep, CACHE_DIR)
    elif config.REPORT_DIR:
        cache_dir = os.path.join(config.REPORT_DIR, CACHE_DIR)
    else:
        cache_dir = ""
    cache_path = os.path.join(cache_dir, "document_score_cache.json") if cache_dir else ""
    doc_cache = _load_document_cache(cache_path) if cache_path else {}

    cached_count = 0
    need_detect = []
    for it in image_items:
        h = it.file_hash
        if h and h in doc_cache:
            cached = doc_cache[h]
            if isinstance(cached, (list, tuple)) and len(cached) == 2:
                it.document_score = _fuse_score(cached[0], cached[1])
            else:
                it.document_score = float(cached)
            cached_count += 1
        else:
            need_detect.append(it)

    BATCH_SIZE = 48
    CHUNK_SIZE = 12
    PREPROCESS_WORKERS = min(max_workers, 6)
    PREFETCH = 4

    logger.info(
        f"文档检测 [ensemble-v2]: {len(image_items)} 图片, "
        f"缓存命中 {cached_count}, 需检测 {len(need_detect)} "
        f"(batch={BATCH_SIZE}, chunk={CHUNK_SIZE}, "
        f"{PREPROCESS_WORKERS} 进程, prefetch={PREFETCH})"
    )

    save_interval = 2000

    with tqdm(total=len(image_items), desc="文档检测", unit="个", initial=cached_count) as pbar:
        if need_detect and not is_interrupted():
            detector._ensure_model()

            from concurrent.futures import ProcessPoolExecutor as _PPE
            try:
                pool = _PPE(max_workers=PREPROCESS_WORKERS)
            except Exception:
                logger.info("ProcessPoolExecutor 不可用，回退到线程池")
                pool = ThreadPoolExecutor(max_workers=PREPROCESS_WORKERS)

            batches = [need_detect[i:i + BATCH_SIZE]
                       for i in range(0, len(need_detect), BATCH_SIZE)]

            def _submit_preprocess(batch_items):
                chunks = []
                for ci in range(0, len(batch_items), CHUNK_SIZE):
                    chunk = batch_items[ci:ci + CHUNK_SIZE]
                    fps = [it.info.filepath for it in chunk]
                    chunks.append((chunk, pool.submit(
                        _doc_preprocess_chunk, fps)))
                return chunks

            from collections import deque
            pq = deque()
            pf_end = min(PREFETCH, len(batches))
            for pi in range(pf_end):
                pq.append(_submit_preprocess(batches[pi]))
            next_sub = pf_end
            processed_since_save = 0

            for bi in range(len(batches)):
                if is_interrupted():
                    break

                current = pq.popleft()
                if next_sub < len(batches):
                    pq.append(_submit_preprocess(batches[next_sub]))
                    next_sub += 1

                arrays_a, arrays_b, valid_items = [], [], []
                for chunk_items, fut in current:
                    results = fut.result()
                    for it, res in zip(chunk_items, results):
                        if res is not None:
                            arr_a, arr_b = res
                            arrays_a.append(arr_a)
                            arrays_b.append(arr_b)
                            valid_items.append(it)

                if arrays_a:
                    try:
                        raw_pairs = detector.run_batch_inference(
                            np.stack(arrays_a), np.stack(arrays_b),
                        )
                    except Exception as e:
                        logger.debug("文档检测 batch 推理异常: %s", e)
                        raw_pairs = [(0.0, 0.0)] * len(arrays_a)
                    for it, (sa, sb) in zip(valid_items, raw_pairs):
                        it.document_score = _fuse_score(sa, sb)
                        if it.file_hash:
                            doc_cache[it.file_hash] = [sa, sb]

                batch_len = len(batches[bi])
                pbar.update(batch_len)
                processed_since_save += batch_len
                if cache_path and processed_since_save >= save_interval:
                    _save_document_cache(cache_path, doc_cache)
                    processed_since_save = 0

            pool.shutdown(wait=True)

    if cache_path and doc_cache:
        _save_document_cache(cache_path, doc_cache)

    doc_found = sum(1 for it in image_items if it.document_score >= detector.threshold)
    logger.info(f"文档检测完成 [ensemble-v2]: 检出 {doc_found} 个文档图片")


def copy_photos_parallel(
    photo_infos: List[PhotoInfo],
    max_workers: int = 8,
    dry_run: bool = False,
    target_devices: Optional[Set[str]] = None,
    copy_all: bool = False,
    copy_unknown: bool = False,
    copy_unknown_photo: bool = False,
    copy_unknown_video: bool = False,
    nsfw_detector=None,
    nsfw_copy: bool = False,
    doc_detector=None,
) -> OrganizeResult:
    """
    多阶段并行整理：
      1.   并行：分类 + 设备过滤 + 小图过滤 + 计算哈希
      1.5  NSFW batch 检测（GPU 加速，仅 --nsfw 时）
      1.6  文档图片 batch 检测（仅 --document 时）
      2.   串行：去重 + 路径冲突解决 + 文档/NSFW/截图路由
      3.   并行：复制文件
    """
    total = len(photo_infos)

    # ── 加载源盘哈希缓存（含目标盘备份兜底） ──
    src_hash_entries: Dict[str, dict] = {}
    src_cache_paths: Dict[str, str] = {}
    paths_by_drive: Dict[str, list] = {}
    for info in photo_infos:
        drv = os.path.splitdrive(info.filepath)[0].upper()
        if drv:
            paths_by_drive.setdefault(drv, []).append(os.path.dirname(info.filepath))

    # 加载目标盘备份的源哈希缓存
    backup_dir = _get_backup_cache_dir()
    backup_src_path = os.path.join(backup_dir, SRC_HASH_CACHE_FILENAME) if backup_dir else None
    backup_src_entries = _load_hash_cache(backup_src_path) if backup_src_path else {}
    if backup_src_entries:
        logger.info(f"目标盘源哈希备份缓存: {len(backup_src_entries)} 条")

    for drv, hint_paths in paths_by_drive.items():
        cache_dir = None
        root_dir = os.path.join(drv + os.sep, CACHE_DIR)
        try:
            os.makedirs(root_dir, exist_ok=True)
            cache_dir = root_dir
        except OSError:
            for hp in sorted(set(hint_paths), key=len)[:5]:
                fallback = os.path.join(hp, CACHE_DIR)
                try:
                    os.makedirs(fallback, exist_ok=True)
                    cache_dir = fallback
                    break
                except OSError:
                    continue
        if cache_dir:
            cp = os.path.join(cache_dir, SRC_HASH_CACHE_FILENAME)
            src_cache_paths[drv] = cp
            src_hash_entries.update(_load_hash_cache(cp))

        # 从备份缓存补充该盘未命中的条目
        if backup_src_entries:
            drv_prefix = drv.upper()
            for k, v in backup_src_entries.items():
                if os.path.splitdrive(k)[0].upper() == drv_prefix and k not in src_hash_entries:
                    src_hash_entries[k] = v

        if not cache_dir:
            logger.debug(f"无法创建缓存目录 {drv}\\{CACHE_DIR}，跳过该盘哈希缓存")

    if src_hash_entries:
        logger.info(f"已加载源盘哈希缓存: {len(src_hash_entries)} 条")

    # ── 阶段 1：并行哈希 + 分类 + 过滤（按路径排序提交以减少 HDD 寻道） ──
    items: List[Optional[_PreparedItem]] = [None] * total
    idx_map = {id(info): i for i, info in enumerate(photo_infos)}
    sorted_infos = sorted(photo_infos, key=lambda x: x.filepath)

    with tqdm(total=total, desc="哈希+分类", unit="个") as pbar:
        pool = ThreadPoolExecutor(max_workers=max_workers)
        try:
            futures = {
                pool.submit(_prepare_one, info, target_devices, copy_all, copy_unknown,
                            copy_unknown_photo, copy_unknown_video,
                            src_hash_entries): info
                for info in sorted_infos
            }
            for future in _interruptible_as_completed(futures):
                info = futures[future]
                try:
                    item = future.result()
                except Exception as e:
                    item = _PreparedItem(
                        info=info,
                        device_type=DeviceType.UNKNOWN,
                        record=CopyRecord(
                            source=info.filepath, destination="",
                            device_type="unknown", media_type=info.media_type,
                            make="", model="",
                            date_taken="", has_exif_date=False,
                            file_size=0, status="error", error_msg=str(e),
                        ),
                        needs_copy=False,
                    )
                items[idx_map[id(info)]] = item
                pbar.update(1)
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

    # ── 保存源盘哈希缓存 + 同步到目标盘备份 ──
    all_src_entries: Dict[str, dict] = {}
    valid_by_drive: Dict[str, Dict[str, dict]] = {drv: {} for drv in paths_by_drive}
    for item in items:
        if item and item.file_hash and item._file_mtime:
            fp = item.info.filepath
            drv = os.path.splitdrive(fp)[0].upper()
            cached = src_hash_entries.get(fp)
            if cached and cached["hash"] == item.file_hash:
                if "ctime" not in cached and item._file_ctime:
                    cached["ctime"] = item._file_ctime
                valid_by_drive[drv][fp] = cached
            else:
                entry = {
                    "hash": item.file_hash,
                    "mtime": item._file_mtime,
                    "size": item.info.file_size,
                }
                if item._file_ctime:
                    entry["ctime"] = item._file_ctime
                valid_by_drive[drv][fp] = entry
    for drv, entries in valid_by_drive.items():
        if entries and drv in src_cache_paths:
            _save_hash_cache(src_cache_paths[drv], entries)
        all_src_entries.update(entries)

    # 同步到目标盘备份
    if backup_src_path and all_src_entries:
        merged = dict(backup_src_entries)
        merged.update(all_src_entries)
        _save_hash_cache(backup_src_path, merged)
        new_count = len(merged) - len(backup_src_entries)
        if new_count > 0:
            logger.info(f"源哈希备份缓存同步: +{new_count} 条, 总计 {len(merged)} 条")

    # 填充未完成的条目（中断时部分 items 可能仍为 None）
    for i, item in enumerate(items):
        if item is None:
            info = photo_infos[i]
            items[i] = _PreparedItem(
                info=info,
                device_type=DeviceType.UNKNOWN,
                record=CopyRecord(
                    source=info.filepath, destination="",
                    device_type="unknown", media_type=info.media_type,
                    make="", model="",
                    date_taken="", has_exif_date=False,
                    file_size=0, status="error", error_msg="用户中断",
                ),
                needs_copy=False,
            )

    # ── 阶段 1.2：DJI 同文件夹推断（未识别文件继承同目录下 DJI 设备标签） ──
    if not is_interrupted():
        dji_folder_map: Dict[str, str] = {}  # {源目录: DJI设备标签}
        for item in items:
            if item and item.is_dji and item.info.make:
                src_dir = os.path.dirname(item.info.filepath)
                if src_dir not in dji_folder_map:
                    dji_folder_map[src_dir] = _build_device_label(item.info)

        if dji_folder_map:
            inferred_count = 0
            need_hash: List[_PreparedItem] = []
            for item in items:
                if item is None or item.is_dji or item.is_screenshot:
                    continue
                if not item.needs_copy and item.record.status in ("skipped_no_device", "skipped_not_target"):
                    src_dir = os.path.dirname(item.info.filepath)
                    dji_label = dji_folder_map.get(src_dir)
                    if dji_label:
                        item.is_dji = True
                        item.needs_copy = True
                        item.record.status = ""
                        if not item.info.make:
                            item.info.make = "DJI"
                        inferred_count += 1
                        if not item.file_hash:
                            need_hash.append(item)

            if need_hash:
                for item in need_hash:
                    try:
                        item.file_hash = _file_fast_hash(item.info.filepath, item.info.file_size)
                        _st = os.stat(item.info.filepath)
                        item._file_mtime = _st.st_mtime
                        item._file_ctime = _st.st_ctime
                    except Exception as e:
                        item.record.status = "error"
                        item.record.error_msg = f"DJI推断补算哈希失败: {e}"
                        item.needs_copy = False

            if inferred_count:
                logger.info(f"DJI 同文件夹推断: {inferred_count} 个未识别文件继承 DJI 设备标签")

    # ── 阶段 1.5：NSFW batch 检测（GPU 加速） ──
    if nsfw_detector and not is_interrupted():
        _run_nsfw_detection(items, nsfw_detector, max_workers)

    # ── 阶段 1.6：文档图片 batch 检测（GPU 加速） ──
    if doc_detector and not is_interrupted():
        _run_document_detection(items, doc_detector, max_workers)

    # ── 阶段 2-0：扫描目标盘已有文件，建立哈希索引 ──
    dest_drive = os.path.splitdrive(config.DEST_CAMERA)[0].upper() + os.sep
    reuse_index: Optional[Dict[str, str]] = None
    dup_files: List[str] = []
    if dest_drive and os.path.isdir(dest_drive):
        reuse_index, dup_files = _scan_dest_drive_for_reuse(dest_drive, max_workers)

    # ── 阶段 2：串行去重 + 冲突解决 ──
    # 只将**输出目录内**的已有文件加入 seen_hashes（防止重复运行时产生 _po 后缀）
    # 输出目录外的文件保留在 reuse_index 中供同盘移动复用
    seen_hashes: Dict[str, str] = {}  # hash → 首个文件路径
    if reuse_index:
        output_root = os.path.normcase(config.REPORT_DIR) + os.sep
        in_output = 0
        for h, fp in reuse_index.items():
            if os.path.normcase(fp).startswith(output_root):
                seen_hashes[h] = fp
                in_output += 1
        if in_output:
            logger.info(f"输出目录已有 {in_output} 个唯一文件，重复源文件将自动跳过")
        outside = len(reuse_index) - in_output
        if outside:
            logger.info(f"目标盘其余 {outside} 个文件可用于同盘复用")

    assigned_dests: Set[str] = set()  # 本批次已分配的目标路径（normcase）
    to_copy: List[_PreparedItem] = []
    dji_accepted: List[_PreparedItem] = []  # 通过去重的 DJI 项（含 dry_run）

    nsfw_prefix = (os.path.normcase(config.DEST_NSFW) + os.sep) if config.DEST_NSFW else ""
    _output_norm = (os.path.normcase(config.REPORT_DIR) + os.sep) if config.REPORT_DIR else ""
    _KEEP_PREFIXES = ("all_1_", "all_2_", "all_3_", "all_4_", "all_5_", "all_7_", "all_999_")
    _unknown_norm = (os.path.normcase(config.DEST_UNKNOWN) + os.sep) if config.DEST_UNKNOWN else ""

    for item in items:
        if not item.needs_copy:
            continue

        item.record.file_hash = item.file_hash

        # 检查源文件是否已在输出目录的已分类设备文件夹中
        _src_in_device_folder = False
        _src_frozen = False  # 已在 NSFW / 文档图片等特殊目录，保留原位
        if _output_norm:
            _snc = os.path.normcase(item.info.filepath)
            if _snc.startswith(_output_norm):
                _top = _snc[len(_output_norm):].split(os.sep)[0]
                _src_in_device_folder = any(_top.startswith(p) for p in _KEEP_PREFIXES)
                _src_frozen = _top.startswith("all_999_") or _top.startswith("all_7_")

        # NSFW / 文档图片文件夹中的文件直接保留原位，不参与任何重新路由
        if _src_frozen:
            item.record.destination = item.info.filepath

        # 截图路由：截图文件重定向到文档图片/截图目录
        if not _src_frozen and item.is_screenshot and config.DEST_SCREENSHOT:
            item.record.destination = _build_screenshot_dest(item)

        # 文档路由：高分文档图片重定向到文档图片目录（跳过截图、DJI、相机）
        if (not _src_frozen and not item.is_screenshot and not item.is_dji
                and item.device_type != DeviceType.CAMERA
                and item.document_score >= 0
                and doc_detector and item.document_score >= doc_detector.threshold
                and config.DEST_DOCUMENT):
            item.record.destination = _build_document_dest(item)

        # DJI 路由：DJI 文件重定向到 DJI 专属目录
        if not _src_frozen and item.is_dji and config.DEST_DJI:
            item.record.destination = _build_dji_dest_path(item)

        # NSFW 路由：nsfw_copy=True 时移到 NSFW 目录，否则仅标记不移动
        is_nsfw = (nsfw_detector and item.nsfw_score >= nsfw_detector.threshold)
        if is_nsfw:
            item.record.extra_info["nsfw_score"] = f"{item.nsfw_score:.3f}"
        if not _src_frozen and is_nsfw and nsfw_copy and config.DEST_NSFW:
            item.record.destination = _build_nsfw_dest(item)

        # 防降级：已在设备文件夹的文件不被分到未识别设备目录
        if _src_in_device_folder and _unknown_norm:
            _dst_nc = os.path.normcase(item.record.destination)
            if _dst_nc.startswith(_unknown_norm):
                item.record.destination = item.info.filepath

        # 目标 == 源（文件已在正确位置）→ 标记为已就位，注册哈希防止重复
        if os.path.normcase(item.record.destination) == os.path.normcase(item.info.filepath):
            if item.file_hash not in seen_hashes:
                seen_hashes[item.file_hash] = item.info.filepath
            item.record.status = "ok"
            item.needs_copy = False
            if item.is_dji:
                dji_accepted.append(item)
            continue

        if item.file_hash in seen_hashes:
            existing = seen_hashes[item.file_hash]
            if is_nsfw and nsfw_copy and nsfw_prefix and not os.path.normcase(existing).startswith(nsfw_prefix):
                pass
            else:
                item.record.status = "skipped_dup"
                item.record.dup_of = existing
                item.needs_copy = False
                continue

        seen_hashes[item.file_hash] = item.record.destination if (is_nsfw and nsfw_copy) else item.info.filepath

        resolved = _resolve_conflict(item.record.destination, assigned_dests)
        item.record.destination = resolved
        assigned_dests.add(os.path.normcase(resolved))

        if item.is_dji:
            dji_accepted.append(item)

        if dry_run:
            item.record.status = "dry_run"
            item.needs_copy = False
            continue

        to_copy.append(item)

    # ── 阶段 2.5：DJI 伴随文件发现（.SRT 等非媒体同名文件随行复制） ──
    companion_items: List[_PreparedItem] = []
    seen_companions: Set[str] = set()
    for item in dji_accepted:
        src_path = item.info.filepath
        stem = os.path.splitext(os.path.basename(src_path))[0]
        src_dir = os.path.dirname(src_path)
        dest_dir = os.path.dirname(item.record.destination)
        try:
            for fname in os.listdir(src_dir):
                if os.path.splitext(fname)[0] != stem:
                    continue
                ext = os.path.splitext(fname)[1].lower()
                if ext in config.MEDIA_EXTENSIONS:
                    continue
                comp_src = os.path.join(src_dir, fname)
                if not os.path.isfile(comp_src):
                    continue
                nc = os.path.normcase(comp_src)
                if nc in seen_companions:
                    continue
                seen_companions.add(nc)
                comp_dest = os.path.join(dest_dir, fname)
                # 伴随文件已在正确位置 → 跳过
                if os.path.normcase(comp_dest) == nc:
                    continue
                comp_dest = _resolve_conflict(comp_dest, assigned_dests)
                assigned_dests.add(os.path.normcase(comp_dest))
                try:
                    comp_stat = os.stat(comp_src)
                    comp_size = comp_stat.st_size
                    comp_mtime = datetime.fromtimestamp(comp_stat.st_mtime)
                except OSError:
                    comp_size = 0
                    comp_mtime = None
                comp_info = PhotoInfo(
                    filepath=comp_src, media_type="companion",
                    file_size=comp_size, file_modified=comp_mtime,
                )
                comp_record = CopyRecord(
                    source=comp_src, destination=comp_dest,
                    device_type="dji_companion", media_type="companion",
                    make=item.info.make or "DJI", model=item.info.model or "",
                    date_taken="", has_exif_date=False,
                    file_size=comp_size, status="",
                )
                comp_item = _PreparedItem(
                    info=comp_info, device_type=item.device_type,
                    record=comp_record, is_dji=True,
                )
                if dry_run:
                    comp_record.status = "dry_run"
                    comp_item.needs_copy = False
                companion_items.append(comp_item)
        except OSError:
            pass

    if companion_items:
        logger.info(f"DJI 伴随文件: {len(companion_items)} 个（.SRT 等随行复制）")
        to_copy.extend([c for c in companion_items if c.needs_copy])

    # ── 阶段 3：并行复制 ──
    if to_copy:
        # 3-0) 统计同盘复用
        if reuse_index:
            reuse_count = sum(1 for item in to_copy if item.file_hash and item.file_hash in reuse_index)
            if reuse_count:
                logger.info(f"可同盘复用: {reuse_count}/{len(to_copy)} 个文件（秒移，无需跨盘复制）")
            else:
                logger.info("目标盘无可复用文件，将全部跨盘复制")
                reuse_index = None

        # 3-1) 预建所有目标目录（消除逐文件 makedirs 开销）
        dest_dirs = {os.path.dirname(item.record.destination) for item in to_copy}
        for d in dest_dirs:
            os.makedirs(d, exist_ok=True)

        # 3-2) 按源路径排序 → 顺序读取，减少 HDD 磁头跳转
        to_copy.sort(key=lambda item: item.info.filepath)

        # 3-3) 并行复制（限制线程数减轻磁盘争用）
        copy_workers = min(max_workers, MAX_COPY_WORKERS)
        write_bytes = 0
        moved_count = 0
        write_count = 0
        import time as _time
        t_copy_start = _time.time()
        with tqdm(total=len(to_copy), desc=f"复制文件({copy_workers}线程)", unit="个",
                  bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}] {postfix}") as pbar:
            pool = ThreadPoolExecutor(max_workers=copy_workers)
            try:
                futures = {pool.submit(_do_copy, item, reuse_index): item for item in to_copy}
                for future in _interruptible_as_completed(futures):
                    item = futures[future]
                    try:
                        future.result()
                    except Exception:
                        pass
                    note = item.record.error_msg or ""
                    is_rename = "同盘移动自:" in note
                    if is_rename:
                        moved_count += 1
                        tag = "移"
                    else:
                        write_bytes += item.info.file_size
                        write_count += 1
                        tag = "写"
                    elapsed = _time.time() - t_copy_start
                    fname = os.path.basename(item.info.filepath)
                    if len(fname) > 50:
                        name, ext = os.path.splitext(fname)
                        fname = name[:46 - len(ext)] + "..." + ext
                    if write_bytes > 0 and elapsed > 0:
                        mb_s = (write_bytes / 1048576) / elapsed
                        pbar.set_postfix_str(f"{mb_s:.1f}MB/s 移{moved_count}写{write_count} [{tag}]{fname}")
                    else:
                        done = pbar.n + 1
                        rate = done / elapsed if elapsed > 0 else 0
                        pbar.set_postfix_str(f"{rate:.0f}个/s 移{moved_count}写{write_count} [{tag}]{fname}")
                    pbar.update(1)
            finally:
                pool.shutdown(wait=False, cancel_futures=True)

        if moved_count or write_count:
            elapsed_total = _time.time() - t_copy_start
            parts = []
            if moved_count:
                parts.append(f"同盘移动: {moved_count} 个")
            if write_count:
                write_mb = write_bytes / 1048576
                write_speed = write_mb / elapsed_total if elapsed_total > 0 else 0
                parts.append(f"写入复制: {write_count} 个 ({write_mb:.0f}MB, {write_speed:.1f}MB/s)")
            logger.info(", ".join(parts))

    # 删除目标盘上的重复文件（同哈希多余副本，无论是否有新文件要复制）
    # 保护目录中的文件不删除
    if dup_files:
        protected_prefixes = [
            os.path.normcase(d) + os.sep for d in config.REUSE_PROTECTED_DIRS
        ]
        dup_deleted = 0
        dup_skipped = 0
        dup_bytes = 0
        for fp in dup_files:
            norm_fp = os.path.normcase(fp)
            if any(norm_fp.startswith(p) for p in protected_prefixes):
                dup_skipped += 1
                continue
            try:
                if os.path.isfile(fp):
                    sz = os.path.getsize(fp)
                    os.remove(fp)
                    dup_deleted += 1
                    dup_bytes += sz
            except OSError:
                pass
        if dup_deleted:
            logger.info(f"删除目标盘重复文件: {dup_deleted} 个, "
                         f"释放 {_human_size(dup_bytes)}")
        if dup_skipped:
            logger.info(f"保护目录重复文件跳过: {dup_skipped} 个（不删除）")

    # ── 汇总结果 ──
    nsfw_root = os.path.normcase(config.DEST_NSFW + os.sep) if config.DEST_NSFW else ""
    doc_root = os.path.normcase(config.DEST_DOCUMENT + os.sep) if config.DEST_DOCUMENT else ""
    result = OrganizeResult(total_found=total)
    result.records = [item.record for item in items]
    if companion_items:
        result.records.extend(c.record for c in companion_items)
    for record in result.records:
        if record.status in ("ok", "dry_run"):
            result.copied += 1
            norm_dest = os.path.normcase(record.destination)
            if nsfw_root and norm_dest.startswith(nsfw_root):
                result.nsfw_count += 1
            elif record.extra_info.get("nsfw_score"):
                result.nsfw_count += 1
            if doc_root and norm_dest.startswith(doc_root):
                result.document_count += 1
        elif record.status == "skipped_dup":
            result.skipped_dup += 1
        elif record.status == "skipped_exists":
            result.skipped_exists += 1
        elif record.status == "skipped_filtered":
            result.skipped_filtered += 1
        elif record.status == "skipped_not_target":
            result.skipped_not_target += 1
        elif record.status == "skipped_no_device":
            result.skipped_no_device += 1
        elif record.status == "error":
            result.errors += 1

    # ── 生成每个目标文件夹的 filelist.txt ──
    _write_filelists(result.records)

    # ── 补全历史文件夹缺失的 filelist.txt ──
    dest_roots = {config.DEST_CAMERA, config.DEST_PHONE, config.DEST_UNKNOWN}
    if config.DEST_NSFW:
        dest_roots.add(config.DEST_NSFW)
    if config.DEST_DOCUMENT:
        dest_roots.add(config.DEST_DOCUMENT)
    if config.DEST_SCREENSHOT:
        dest_roots.add(config.DEST_SCREENSHOT)
    if config.DEST_CAMERA_OTHER:
        dest_roots.update({config.DEST_CAMERA_OTHER, config.DEST_PHONE_OTHER})
    _backfill_filelists(dest_roots)

    # ── 清理目标盘上的空文件夹（始终执行，放在 filelist 生成之后） ──
    dest_drive = os.path.splitdrive(config.DEST_CAMERA)[0].upper() + os.sep
    if dest_drive and os.path.isdir(dest_drive):
        _cleanup_empty_dirs(dest_drive, logger)

    return result


def _human_size(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(size) < 1024:
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}TB"


def _fmt_fraction(val: str) -> str:
    """格式化 EXIF 分数值，如 '1424131/5000000' → '1/4' 或 '8/5' → '1.6'"""
    if not val or "/" not in val:
        return val
    try:
        num, den = val.split("/", 1)
        n, d = float(num), float(den)
        if d == 0:
            return val
        result = n / d
        if result < 1:
            simplified_den = round(1 / result)
            return f"1/{simplified_den}"
        if result == int(result):
            return str(int(result))
        return f"{result:.1f}"
    except (ValueError, ZeroDivisionError):
        return val


def _str_width(s: str) -> int:
    """字符串在等宽字体中的显示宽度（East Asian 全角/宽字符按 2 列计算）"""
    import unicodedata
    w = 0
    for ch in s:
        w += 2 if unicodedata.east_asian_width(ch) in ("F", "W") else 1
    return w


def _pad(s: str, width: int) -> str:
    """用空格将字符串填充到指定显示宽度"""
    return s + " " * max(0, width - _str_width(s))


_FILELIST_COLS_CN = ("文件名", "拍摄时间", "大小", "设备", "参数", "镜头", "GPS", "原始路径")
_FILELIST_COLS_EN = ("Filename", "Date", "Size", "Device", "Params", "Lens", "GPS", "Source")
_COL_SEP = " | "


def _write_filelist_file(filepath: str, dest_dir: str, rows: list, suffix: str = "") -> None:
    """将行数据写入 filelist.txt，用 | 分隔列并自动对齐宽度。
    表头使用 ASCII 英文列名保证严格对齐，中文列名写在注释行。"""
    cols = _FILELIST_COLS_EN
    col_widths = [len(c) for c in cols]
    for row in rows:
        for i, val in enumerate(row):
            col_widths[i] = max(col_widths[i], _str_width(val))

    def _fmt_line(values):
        parts = []
        for i, v in enumerate(values):
            parts.append(v if i == len(values) - 1 else _pad(v, col_widths[i]))
        return _COL_SEP.join(parts)

    sep_line = "-+-".join("-" * col_widths[i] for i in range(len(cols)))

    cn_header = _COL_SEP.join(f"{cn}({en})" for cn, en in zip(_FILELIST_COLS_CN, _FILELIST_COLS_EN))

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(f"# 文件列表 — 共 {len(rows)} 个文件{suffix}\n")
        f.write(f"# 目标目录: {dest_dir}\n")
        f.write(f"# 列: {cn_header}\n")
        f.write("#\n")
        f.write(_fmt_line(cols) + "\n")
        f.write(sep_line + "\n")
        for row in rows:
            f.write(_fmt_line(row) + "\n")


def _write_filelists(records: List[CopyRecord]) -> None:
    """在每个目标文件夹下生成 filelist.txt（空格对齐），记录文件的原始路径和关键拍摄信息。"""
    from collections import defaultdict

    dir_entries: Dict[str, List[CopyRecord]] = defaultdict(list)
    for r in records:
        if r.status not in ("ok", "dry_run") or not r.destination:
            continue
        dir_entries[os.path.dirname(r.destination)].append(r)

    for dest_dir, recs in dir_entries.items():
        recs.sort(key=lambda r: os.path.basename(r.destination))

        rows = []
        for r in recs:
            fname = os.path.basename(r.destination)
            device = (r.make + " " + r.model).strip() if r.make else ""
            size = _human_size(r.file_size)
            date = r.date_taken or ""
            ex = r.extra_info

            iso = ex.get("iso", "")
            aperture = _fmt_fraction(ex.get("aperture", ""))
            shutter = _fmt_fraction(ex.get("shutter", ""))
            focal = _fmt_fraction(ex.get("focal_length", ""))
            lens = ex.get("lens", "")
            gps = ex.get("gps", "")

            param_parts = []
            if iso:
                param_parts.append(f"ISO{iso}")
            if aperture:
                param_parts.append(f"f/{aperture}")
            if shutter:
                param_parts.append(f"{shutter}s")
            if focal:
                param_parts.append(f"{focal}mm")
            params = " ".join(param_parts)

            rows.append((fname, date, size, device, params, lens, gps, r.source))

        try:
            _write_filelist_file(os.path.join(dest_dir, "filelist.txt"), dest_dir, rows)
        except Exception as e:
            logger.warning(f"生成 filelist.txt 失败: {dest_dir} → {e}")


def _backfill_filelists(dest_roots: set) -> None:
    """扫描输出目录，为缺少 filelist.txt 的子文件夹补生成（读取已有文件的元数据）。"""
    from config import MEDIA_EXTENSIONS
    from exif_reader import read_photo_info

    backfilled = 0
    for root_dir in dest_roots:
        if not root_dir or not os.path.isdir(root_dir):
            continue
        for dirpath, dirnames, filenames in os.walk(root_dir):
            if "filelist.txt" in filenames:
                continue
            media = [f for f in filenames if os.path.splitext(f)[1].lower() in MEDIA_EXTENSIONS]
            if not media:
                continue

            rows = []
            for fname in sorted(media):
                fp = os.path.join(dirpath, fname)
                try:
                    info = read_photo_info(fp)
                    date = info.date_taken.strftime("%Y-%m-%d %H:%M:%S") if info.date_taken else ""
                    size = _human_size(info.file_size)
                    device = (info.make + " " + info.model).strip() if info.make else ""
                    ex = info.extra

                    iso = ex.get("iso", "")
                    aperture = _fmt_fraction(ex.get("aperture", ""))
                    shutter = _fmt_fraction(ex.get("shutter", ""))
                    focal = _fmt_fraction(ex.get("focal_length", ""))
                    lens = ex.get("lens", "")
                    gps = ex.get("gps", "")

                    param_parts = []
                    if iso:
                        param_parts.append(f"ISO{iso}")
                    if aperture:
                        param_parts.append(f"f/{aperture}")
                    if shutter:
                        param_parts.append(f"{shutter}s")
                    if focal:
                        param_parts.append(f"{focal}mm")
                    params = " ".join(param_parts)

                    rows.append((fname, date, size, device, params, lens, gps, fp))
                except Exception:
                    rows.append((fname, "", "", "", "", "", "", fp))

            if not rows:
                continue

            try:
                _write_filelist_file(
                    os.path.join(dirpath, "filelist.txt"), dirpath, rows,
                    suffix="（补全生成）",
                )
                backfilled += 1
            except Exception as e:
                logger.warning(f"补全 filelist.txt 失败: {dirpath} → {e}")

    if backfilled:
        logger.info(f"补全历史文件夹 filelist.txt: {backfilled} 个")


# ── 修复 _po 后缀文件 ──

_PO_SUFFIX_RE = re.compile(r"_po(\d+)(\.[^.]+)$", re.IGNORECASE)


def restore_po_files(output_root: str) -> None:
    """扫描输出目录，将 _poN 后缀的文件恢复为原始文件名。"""
    if not os.path.isdir(output_root):
        logger.error(f"目录不存在: {output_root}")
        return

    renamed = 0
    deleted_dup = 0
    skipped = 0

    for dirpath, _dirs, files in os.walk(output_root):
        for fn in files:
            m = _PO_SUFFIX_RE.search(fn)
            if not m:
                continue

            po_path = os.path.join(dirpath, fn)
            original_name = _PO_SUFFIX_RE.sub(r"\2", fn)
            original_path = os.path.join(dirpath, original_name)

            if not os.path.exists(original_path):
                try:
                    os.rename(po_path, original_path)
                    renamed += 1
                except OSError as e:
                    logger.warning(f"重命名失败: {fn} → {original_name}: {e}")
                    skipped += 1
            else:
                try:
                    po_size = os.path.getsize(po_path)
                    orig_size = os.path.getsize(original_path)
                    if po_size == orig_size:
                        po_hash = _file_fast_hash(po_path, po_size)
                        orig_hash = _file_fast_hash(original_path, orig_size)
                        if po_hash == orig_hash:
                            os.remove(po_path)
                            deleted_dup += 1
                            continue
                except OSError:
                    pass
                skipped += 1

    logger.info(f"修复 _po 后缀文件完成: "
                f"重命名恢复 {renamed} 个, 删除重复 {deleted_dup} 个, 跳过 {skipped} 个")
