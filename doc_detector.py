"""文档图片检测模块：双模型集成 (docornot + doctype) 条件融合

模型 A — mozilla/docornot (DeiT-tiny, ViT): 224×224, 2类 (picture/document)
模型 B — monkt/doctype (MobileNetV3-Large): 320×320, 7类 (chart/diagram/handwritten/printed/map/photo/screenshot)

条件融合策略:
  score_A = P(document)           from docornot
  score_B = 1.0 - P(photo)       from doctype

  判定为文档:
    规则1: max(A, B) >= 0.8       任一模型高置信即可
    规则2: A >= 0.4 AND B >= 0.4  双模型共识
"""

import logging
import os
import site
import sys
from typing import Dict, List, Optional, Tuple

import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)

_MODEL_DIR = os.path.join(os.path.dirname(__file__), "models")
_DOCORNOT_DIR = os.path.join(_MODEL_DIR, "docornot-onnx")
_DOCTYPE_DIR = os.path.join(_MODEL_DIR, "doctype-onnx")

CACHE_MODEL_VERSION = "ensemble-v2"

_INPUT_SIZE_A = 224
_INPUT_SIZE_B = 320

_MEAN_A = np.array([0.485, 0.456, 0.406], dtype=np.float32)
_STD_A = np.array([0.229, 0.224, 0.225], dtype=np.float32)

_DOCTYPE_LABELS = [
    "chart", "diagram", "document_handwritten", "document_printed",
    "map", "photo", "screenshot",
]
_PHOTO_INDEX = 5

_RAW_EXTENSIONS = {
    ".cr2", ".cr3", ".nef", ".arw", ".orf", ".rw2",
    ".dng", ".raf", ".pef", ".srw",
}


def check_dependencies():
    """检查文档检测所需依赖"""
    try:
        import onnxruntime  # noqa: F401
    except ImportError:
        raise ImportError(
            "文档检测需要 onnxruntime\n"
            "请执行: pip install onnxruntime-gpu  (或 onnxruntime)"
        )


def _get_model_path(model_dir: str, filename: str) -> str:
    path = os.path.join(model_dir, filename)
    if not os.path.isfile(path):
        raise FileNotFoundError(f"文档检测模型未找到: {path}")
    return path


def _open_image(filepath: str, target_size: int) -> Optional[Image.Image]:
    ext = os.path.splitext(filepath)[1].lower()
    if ext in _RAW_EXTENSIONS:
        return None
    try:
        img = Image.open(filepath)
        if ext in (".jpg", ".jpeg") and hasattr(img, "draft"):
            img.draft("RGB", (target_size, target_size))
            img.load()
        return img.convert("RGB")
    except Exception as e:
        logger.debug("文档检测: 无法打开 %s: %s", filepath, e)
        return None


def preprocess_chunk(filepaths: list) -> list:
    """顺序预处理一批连续文件（HDD 友好：同一工作进程内顺序读取，减少磁头跳跃）"""
    return [preprocess_single(fp) for fp in filepaths]


def preprocess_single(filepath: str) -> Optional[Tuple[np.ndarray, np.ndarray]]:
    """预处理单张图片，同时为两个模型生成输入张量。

    Returns:
        (arr_A, arr_B) — arr_A shape (3, 224, 224) CHW, arr_B shape (320, 320, 3) HWC
        如果文件不可读则返回 None
    """
    img = _open_image(filepath, _INPUT_SIZE_B)
    if img is None:
        return None

    try:
        img_a = img.resize((_INPUT_SIZE_A, _INPUT_SIZE_A), Image.BILINEAR)
        arr_a = np.array(img_a, dtype=np.float32) / 255.0
        arr_a = (arr_a - _MEAN_A) / _STD_A
        arr_a = arr_a.transpose(2, 0, 1)  # HWC → CHW

        img_b = img.resize((_INPUT_SIZE_B, _INPUT_SIZE_B), Image.BILINEAR)
        arr_b = np.array(img_b, dtype=np.float32) / 255.0  # HWC, [0, 1]

        return arr_a, arr_b
    except Exception as e:
        logger.debug("文档检测预处理失败 %s: %s", filepath, e)
        return None


def _softmax(x: np.ndarray) -> np.ndarray:
    e = np.exp(x - np.max(x, axis=-1, keepdims=True))
    return e / np.sum(e, axis=-1, keepdims=True)


def _add_nvidia_dll_paths():
    """复用 NSFW 检测器的 NVIDIA DLL 路径设置"""
    try:
        from nsfw_detector import _add_nvidia_dll_paths as _add
        _add()
    except ImportError:
        if sys.platform == "win32":
            for sp in site.getsitepackages():
                nv_root = os.path.join(sp, "nvidia")
                if not os.path.isdir(nv_root):
                    continue
                for pkg in os.listdir(nv_root):
                    dll_dir = os.path.join(nv_root, pkg, "bin")
                    if os.path.isdir(dll_dir):
                        try:
                            os.add_dll_directory(dll_dir)
                        except OSError:
                            pass


def fuse_score(score_a: float, score_b: float) -> float:
    """条件融合（含 A 模型否决权）。

    当 docornot (A) 强烈否定 (<0.15) 时，即使 doctype (B) 给出高分也大幅降权，
    避免 B 将风景/建筑照误判为 map/diagram 导致的假阳性。
    """
    hi = max(score_a, score_b)
    if score_a < 0.15:
        return hi * 0.15
    if hi >= 0.8:
        return hi
    if score_a >= 0.4 and score_b >= 0.4:
        return hi
    return hi * 0.3


class DocumentDetector:
    """双模型集成文档检测器 (docornot + doctype, 条件融合)"""

    def __init__(self, threshold: float = 0.5):
        self.threshold = threshold
        self._sess_a = None  # docornot
        self._sess_b = None  # doctype
        self._use_gpu = False
        self._input_name_a = ""
        self._input_name_b = ""

    def _ensure_model(self):
        if self._sess_a is not None:
            return

        _add_nvidia_dll_paths()
        import onnxruntime as ort

        path_a = _get_model_path(_DOCORNOT_DIR, "model.onnx")
        path_b = _get_model_path(_DOCTYPE_DIR, "doctype_classifier.onnx")
        available = ort.get_available_providers()

        if "CUDAExecutionProvider" in available:
            try:
                providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]
                sa = ort.InferenceSession(path_a, providers=providers)
                sb = ort.InferenceSession(path_b, providers=providers)
                if "CUDAExecutionProvider" in sa.get_providers():
                    self._sess_a = sa
                    self._sess_b = sb
                    self._use_gpu = True
                    self._input_name_a = sa.get_inputs()[0].name
                    self._input_name_b = sb.get_inputs()[0].name
                    logger.info(
                        "文档检测: GPU (CUDA) 双模型集成 [ensemble-v1], 阈值 %.2f",
                        self.threshold,
                    )
                    return
                del sa, sb
            except Exception:
                pass
            logger.info("文档检测: CUDA 不可用, 回退 CPU")

        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = min(os.cpu_count() or 4, 4)

        self._sess_a = ort.InferenceSession(
            path_a, sess_options=opts, providers=["CPUExecutionProvider"],
        )
        self._sess_b = ort.InferenceSession(
            path_b, sess_options=opts, providers=["CPUExecutionProvider"],
        )
        self._input_name_a = self._sess_a.get_inputs()[0].name
        self._input_name_b = self._sess_b.get_inputs()[0].name
        self._use_gpu = False
        logger.info(
            "文档检测: CPU 双模型集成 [ensemble-v1], 阈值 %.2f", self.threshold,
        )

    def run_batch_inference(
        self, batch_a: np.ndarray, batch_b: np.ndarray,
    ) -> List[Tuple[float, float]]:
        """双模型并行批量推理，返回 (score_a, score_b) 原始分数对列表。

        Args:
            batch_a: (N, 3, 224, 224) CHW — for docornot
            batch_b: (N, 320, 320, 3) HWC — for doctype
        Returns:
            [(P(document), 1-P(photo)), ...] — 融合在调用方执行
        """
        self._ensure_model()

        import threading
        out_b_holder: List = [None]
        err_holder: List = [None]

        def _infer_b():
            try:
                out_b_holder[0] = self._sess_b.run(
                    None, {self._input_name_b: batch_b},
                )
            except Exception as e:
                err_holder[0] = e

        t = threading.Thread(target=_infer_b, daemon=True)
        t.start()
        out_a = self._sess_a.run(None, {self._input_name_a: batch_a})
        t.join()
        if err_holder[0]:
            raise err_holder[0]

        logits_a = out_a[0]
        probs_a = _softmax(logits_a)
        scores_a = probs_a[:, 1]

        probs_b = out_b_holder[0][0]
        scores_b = 1.0 - probs_b[:, _PHOTO_INDEX]

        return [
            (float(a), float(b))
            for a, b in zip(scores_a, scores_b)
        ]

    def predict_detail(
        self, filepath: str,
    ) -> Dict[str, object]:
        """单图推理，返回两个模型各自的详细分数（调试用）"""
        self._ensure_model()
        result = preprocess_single(filepath)
        if result is None:
            return {"score_a": 0.0, "score_b": 0.0, "ensemble": 0.0, "doctype_probs": {}}

        arr_a, arr_b = result
        out_a = self._sess_a.run(None, {self._input_name_a: arr_a[np.newaxis]})
        probs_a = _softmax(out_a[0])[0]
        score_a = float(probs_a[1])

        out_b = self._sess_b.run(None, {self._input_name_b: arr_b[np.newaxis]})
        probs_b = out_b[0][0]
        score_b = float(1.0 - probs_b[_PHOTO_INDEX])

        doctype_probs = {_DOCTYPE_LABELS[i]: float(probs_b[i]) for i in range(7)}
        ensemble = fuse_score(score_a, score_b)

        return {
            "score_a": score_a,
            "score_b": score_b,
            "ensemble": ensemble,
            "doctype_probs": doctype_probs,
        }

    def predict_image(self, filepath: str) -> float:
        """单图推理，返回融合分数"""
        self._ensure_model()
        result = preprocess_single(filepath)
        if result is None:
            return 0.0
        arr_a, arr_b = result
        raw = self.run_batch_inference(
            arr_a[np.newaxis], arr_b[np.newaxis],
        )
        sa, sb = raw[0]
        return fuse_score(sa, sb)
