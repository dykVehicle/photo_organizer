"""设备分类模块：根据 EXIF Make/Model 判定相机、手机或未知，并支持目标设备过滤"""

from enum import Enum
from typing import Optional, Set

from config import (
    CAMERA_BRANDS,
    PHONE_BRANDS,
    AMBIGUOUS_BRANDS,
    SONY_CAMERA_MODEL_KEYWORDS,
    SONY_PHONE_MODEL_KEYWORDS,
    DEFAULT_TARGET_BRANDS,
    DEFAULT_TARGET_MODELS,
)
from exif_reader import PhotoInfo


class DeviceType(Enum):
    CAMERA = "camera"
    PHONE = "phone"
    UNKNOWN = "unknown"


def classify_device(info: PhotoInfo) -> DeviceType:
    """
    根据 PhotoInfo 中的 make/model 字段判定设备类型。

    分类优先级：
    1. 无 make 信息 -> UNKNOWN
    2. make 属于 AMBIGUOUS_BRANDS（如 Sony）-> 通过 model 进一步判定
    3. make 匹配 CAMERA_BRANDS -> CAMERA
    4. make 匹配 PHONE_BRANDS -> PHONE
    5. 都不匹配 -> UNKNOWN
    """
    make_lower = info.make.lower().strip()
    model_lower = info.model.lower().strip()

    if not make_lower:
        return DeviceType.UNKNOWN

    if make_lower in AMBIGUOUS_BRANDS:
        return _classify_ambiguous(make_lower, model_lower)

    if _match_brand(make_lower, CAMERA_BRANDS):
        return DeviceType.CAMERA

    if _match_brand(make_lower, PHONE_BRANDS):
        return DeviceType.PHONE

    return DeviceType.UNKNOWN


def _match_brand(make_lower: str, brand_set) -> bool:
    """检查 make 是否匹配品牌集合中的任一关键词"""
    for brand in brand_set:
        if brand in make_lower:
            return True
    return False


def _classify_ambiguous(make_lower: str, model_lower: str) -> DeviceType:
    """处理 Sony 等需要通过 Model 进一步判定的品牌"""
    for kw in SONY_PHONE_MODEL_KEYWORDS:
        if kw in model_lower:
            return DeviceType.PHONE

    for kw in SONY_CAMERA_MODEL_KEYWORDS:
        if kw in model_lower:
            return DeviceType.CAMERA

    return DeviceType.UNKNOWN


def is_target_device(info: PhotoInfo, target_brands: Optional[Set[str]] = None) -> bool:
    """
    检查文件是否属于目标设备。

    匹配规则（满足任一即命中）：
      1. 品牌匹配：make（小写）包含 target_brands 中的任一关键词
      2. 型号匹配：(make + " " + model)（小写）包含 DEFAULT_TARGET_MODELS 中的任一条目

    如果 target_brands 为 None，使用 DEFAULT_TARGET_BRANDS。
    如果文件没有 make 信息，返回 False。
    """
    make_lower = info.make.lower().strip() if info.make else ""
    if not make_lower:
        return False

    brands = target_brands if target_brands is not None else DEFAULT_TARGET_BRANDS
    for keyword in brands:
        if keyword in make_lower:
            return True

    model_lower = info.model.lower().strip() if info.model else ""
    device_str = f"{make_lower} {model_lower}".strip()
    for target_model in DEFAULT_TARGET_MODELS:
        if target_model in device_str:
            return True

    return False
