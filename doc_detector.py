"""文档图片检测模块：单阶段 DeiT-tiny 二分类

使用 Mozilla/docornot 模型（ONNX）判断图片是文档/截图还是自然照片。
id2label: {0: "picture", 1: "document"}
"""

import logging
import os
import site
import sys
from typing import List, Optional

import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)

_LOCAL_MODEL_DIR = os.path.join(os.path.dirname(__file__), "models", "docornot-onnx")
_INPUT_SIZE = 224
_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)

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


def _get_local_model() -> str:
    model_path = os.path.join(_LOCAL_MODEL_DIR, "model.onnx")
    if not os.path.isfile(model_path):
        raise FileNotFoundError(
            f"文档检测模型未找到: {model_path}\n"
            "请先运行 _export_docornot_onnx.py 导出模型"
        )
    return model_path


def preprocess_single(filepath: str) -> Optional[np.ndarray]:
    """读取并预处理单张图片: resize 224x224, ImageNet 归一化"""
    ext = os.path.splitext(filepath)[1].lower()
    if ext in _RAW_EXTENSIONS:
        return None
    try:
        img = Image.open(filepath)
        if ext in (".jpg", ".jpeg") and hasattr(img, "draft"):
            img.draft("RGB", (_INPUT_SIZE, _INPUT_SIZE))
            img.load()
        img = img.convert("RGB")
        img = img.resize((_INPUT_SIZE, _INPUT_SIZE), Image.BILINEAR)
        arr = np.array(img, dtype=np.float32) / 255.0
        arr = (arr - _MEAN) / _STD
        return arr.transpose(2, 0, 1)  # HWC → CHW
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


class DocumentDetector:
    """文档图片检测器: mozilla/docornot (DeiT-tiny, ONNX)"""

    def __init__(self, threshold: float = 0.5):
        self.threshold = threshold
        self._session = None
        self._use_gpu = False
        self._input_name = ""

    def _ensure_model(self):
        if self._session is not None:
            return

        _add_nvidia_dll_paths()
        import onnxruntime as ort

        model_path = _get_local_model()
        available = ort.get_available_providers()

        if "CUDAExecutionProvider" in available:
            try:
                sess = ort.InferenceSession(
                    model_path,
                    providers=["CUDAExecutionProvider", "CPUExecutionProvider"],
                )
                if "CUDAExecutionProvider" in sess.get_providers():
                    self._session = sess
                    self._use_gpu = True
                    self._input_name = sess.get_inputs()[0].name
                    logger.info("文档检测: GPU (CUDA), 阈值 %.2f", self.threshold)
                    return
                del sess
            except Exception:
                pass
            logger.info("文档检测: CUDA 不可用, 回退 CPU")

        sess_opts = ort.SessionOptions()
        sess_opts.inter_op_num_threads = 1
        sess_opts.intra_op_num_threads = min(os.cpu_count() or 4, 4)
        self._session = ort.InferenceSession(
            model_path, sess_options=sess_opts,
            providers=["CPUExecutionProvider"],
        )
        self._input_name = self._session.get_inputs()[0].name
        self._use_gpu = False
        logger.info("文档检测: CPU, 阈值 %.2f", self.threshold)

    def run_batch_inference(self, batch: np.ndarray) -> List[float]:
        """批量推理，返回 document 类概率列表"""
        self._ensure_model()
        outputs = self._session.run(None, {self._input_name: batch})
        logits = outputs[0]  # (N, 2)
        probs = _softmax(logits)
        return [float(p[1]) for p in probs]  # index 1 = document

    def predict_image(self, filepath: str) -> float:
        """单图推理"""
        self._ensure_model()
        arr = preprocess_single(filepath)
        if arr is None:
            return 0.0
        scores = self.run_batch_inference(arr[np.newaxis, ...])
        return scores[0]
