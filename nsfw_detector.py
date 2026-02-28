"""NSFW 检测模块：两阶段方案

阶段 1（粗筛）: Falconsai/nsfw_image_detection (ViT-base-patch16-224) — GPU batch 推理，快速排除正常文件
阶段 2（精检）: NudeNet (YOLOv8) — 目标检测，识别具体暴露身体部位，消除误检
"""

import logging
import os
import site
import sys
import tempfile
import subprocess
from typing import List, Optional, Tuple

import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)

_nvidia_dll_paths_added = False


def _add_nvidia_dll_paths():
    """将 pip 安装的 nvidia CUDA/cuDNN DLL 目录加入 DLL 搜索路径（仅 Windows）"""
    global _nvidia_dll_paths_added
    if _nvidia_dll_paths_added or sys.platform != "win32":
        return
    _nvidia_dll_paths_added = True

    for sp in site.getsitepackages():
        nv_root = os.path.join(sp, "nvidia")
        if not os.path.isdir(nv_root):
            continue
        for pkg in os.listdir(nv_root):
            dll_dir = os.path.join(nv_root, pkg, "bin")
            if not os.path.isdir(dll_dir):
                continue
            has_dlls = any(f.endswith(".dll") for f in os.listdir(dll_dir))
            if not has_dlls:
                continue
            try:
                os.add_dll_directory(dll_dir)
            except OSError:
                pass
            if dll_dir not in os.environ.get("PATH", ""):
                os.environ["PATH"] = dll_dir + os.pathsep + os.environ.get("PATH", "")
            logger.debug("NVIDIA DLL 路径: %s", dll_dir)

# ── Falconsai 模型配置 ──
_LOCAL_MODEL_DIR = os.path.join(os.path.dirname(__file__), "models", "falconsai-nsfw-onnx")
_INPUT_SIZE = 224
_MEAN = np.float32(0.5)
_STD = np.float32(0.5)

# ── NudeNet 配置 ──
_NUDENET_NSFW_CLASSES = {
    "FEMALE_GENITALIA_EXPOSED",
    "MALE_GENITALIA_EXPOSED",
    "FEMALE_BREAST_EXPOSED",
    "ANUS_EXPOSED",
    "BUTTOCKS_EXPOSED",
}
_NUDENET_MIN_CONFIDENCE = 0.50


def check_dependencies():
    """检查 NSFW 检测所需依赖，缺失时抛出 ImportError 并给出安装提示"""
    missing = []
    try:
        import onnxruntime  # noqa: F401
    except ImportError:
        missing.append("onnxruntime")
    try:
        from nudenet import NudeDetector  # noqa: F401
    except ImportError:
        missing.append("nudenet")
    if missing:
        raise ImportError(
            f"NSFW 检测需要额外依赖: {', '.join(missing)}\n"
            "请执行: pip install onnxruntime-gpu nudenet"
        )


def _get_local_model() -> str:
    """返回本地 ONNX 模型路径（Falconsai 二分类）"""
    model_path = os.path.join(_LOCAL_MODEL_DIR, "model.onnx")
    if not os.path.isfile(model_path):
        raise FileNotFoundError(
            f"NSFW 模型未找到: {model_path}\n"
            "请先运行 _export_onnx.py 导出 Falconsai 模型"
        )
    return model_path


_RAW_EXTENSIONS = {
    ".cr2", ".cr3", ".nef", ".arw", ".orf", ".rw2",
    ".dng", ".raf", ".pef", ".srw",
}


def _preprocess_single(filepath: str) -> Optional[np.ndarray]:
    """读取并预处理单张图片: resize 224x224, rescale 1/255, normalize (0.5, 0.5)"""
    ext = os.path.splitext(filepath)[1].lower()
    if ext in _RAW_EXTENSIONS:
        return None

    try:
        img = Image.open(filepath)
        # JPEG 缩放加载：在 DCT 层直接降采样，避免全量解码大图
        if ext in (".jpg", ".jpeg") and hasattr(img, "draft"):
            img.draft("RGB", (_INPUT_SIZE, _INPUT_SIZE))
            img.load()
        img = img.convert("RGB")
        img = img.resize((_INPUT_SIZE, _INPUT_SIZE), Image.BILINEAR)
        arr = np.array(img, dtype=np.float32) / 255.0
        arr = (arr - _MEAN) / _STD
        return arr.transpose(2, 0, 1)  # HWC → CHW
    except Exception as e:
        logger.debug("NSFW 预处理失败 %s: %s", filepath, e)
        return None


def _read_image_cv2(filepath: str):
    """用 numpy buffer 读取图片，绕过 OpenCV 无法处理中文路径的问题"""
    import cv2
    try:
        with open(filepath, "rb") as f:
            data = np.frombuffer(f.read(), np.uint8)
        return cv2.imdecode(data, cv2.IMREAD_COLOR)
    except Exception as e:
        logger.debug("NudeNet 读取失败 %s: %s", filepath, e)
        return None


class NsfwDetector:
    """两阶段 NSFW 检测器: Falconsai 粗筛 + NudeNet 精检"""

    COARSE_THRESHOLD = 0.3  # Falconsai 粗筛阈值

    def __init__(self, threshold: float = 0.5):
        self.threshold = threshold
        self._session = None
        self._use_gpu = False
        self._input_name = ""
        self._nude_detector = None

    # ── Falconsai 模型管理 ──

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
                    logger.info("NSFW 粗筛: Falconsai GPU (CUDA), 粗筛阈值 %.2f", self.COARSE_THRESHOLD)
                    logger.info("NSFW 精检: NudeNet 目标检测, 最终阈值 %.0f%%", self.threshold * 100)
                    return
                del sess
            except Exception:
                pass
            logger.info("CUDA 不可用, 回退 CPU 模式")

        sess_opts = ort.SessionOptions()
        sess_opts.inter_op_num_threads = 1
        sess_opts.intra_op_num_threads = min(os.cpu_count() or 4, 4)
        self._session = ort.InferenceSession(
            model_path, sess_options=sess_opts,
            providers=["CPUExecutionProvider"],
        )
        self._input_name = self._session.get_inputs()[0].name
        self._use_gpu = False
        logger.info("NSFW 粗筛: Falconsai CPU, 粗筛阈值 %.2f", self.COARSE_THRESHOLD)
        logger.info("NSFW 精检: NudeNet 目标检测, 最终阈值 %.0f%%", self.threshold * 100)

    # ── NudeNet 模型管理 ──

    def _ensure_nudenet(self):
        if self._nude_detector is not None:
            return
        from nudenet import NudeDetector
        self._nude_detector = NudeDetector()
        logger.info("NudeNet 已加载")

    # ── NudeNet 精检 ──

    def nudenet_check_image(self, filepath: str) -> float:
        """NudeNet 精检单张图片，返回最高暴露部位置信度（无暴露返回 0）"""
        self._ensure_nudenet()
        img = _read_image_cv2(filepath)
        if img is None:
            return 0.0
        try:
            detections = self._nude_detector.detect(img)
        except Exception as e:
            logger.debug("NudeNet 检测异常 %s: %s", filepath, e)
            return 0.0
        return _nudenet_max_score(detections)

    def nudenet_check_video(self, filepath: str, max_frames: int = 3) -> float:
        """NudeNet 精检视频：抽帧后逐帧检测，返回最高分"""
        self._ensure_nudenet()
        frames = _extract_video_frames(filepath, max_frames)
        if not frames:
            return 0.0
        max_score = 0.0
        for fp in frames:
            img = _read_image_cv2(fp)
            if img is not None:
                try:
                    detections = self._nude_detector.detect(img)
                    score = _nudenet_max_score(detections)
                    max_score = max(max_score, score)
                except Exception:
                    pass
        for fp in frames:
            try:
                os.unlink(fp)
            except OSError:
                pass
        return max_score

    # ── Falconsai 粗筛接口（供 organizer 双缓冲流水线调用） ──

    def run_batch_inference(self, batch: np.ndarray) -> List[float]:
        """Falconsai batch 推理，返回粗筛分数列表"""
        self._ensure_model()
        return self._run_batch(batch)

    def _run_batch(self, batch: np.ndarray) -> List[float]:
        """ONNX 推理, 返回每张图的 Falconsai 分数 (二分类: normal=0, nsfw=1)"""
        outputs = self._session.run(None, {self._input_name: batch})
        logits = outputs[0]  # (N, 2)
        probs = _softmax(logits)
        return [float(p[1]) for p in probs]

    # ── 单文件两阶段检测（独立使用） ──

    def predict_image(self, filepath: str) -> float:
        """两阶段检测单张图片"""
        self._ensure_model()
        arr = _preprocess_single(filepath)
        if arr is None:
            return 0.0
        coarse = self._run_batch(arr[np.newaxis, ...])[0]
        if coarse < self.COARSE_THRESHOLD:
            return 0.0
        return self.nudenet_check_image(filepath)

    def predict_batch(self, filepaths: List[str]) -> List[float]:
        """两阶段 batch 检测多张图片"""
        self._ensure_model()
        if not filepaths:
            return []

        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(len(filepaths), 8)) as pool:
            results = list(pool.map(_preprocess_single, filepaths))

        arrays, valid_indices = [], []
        for i, arr in enumerate(results):
            if arr is not None:
                arrays.append(arr)
                valid_indices.append(i)

        scores = [0.0] * len(filepaths)
        if not arrays:
            return scores

        coarse_scores = self._run_batch(np.stack(arrays, axis=0))
        for idx, cs in zip(valid_indices, coarse_scores):
            if cs >= self.COARSE_THRESHOLD:
                scores[idx] = self.nudenet_check_image(filepaths[idx])
        return scores

    def predict_video(self, filepath: str, max_frames: int = 3) -> float:
        """两阶段视频检测：Falconsai 粗筛帧 → NudeNet 精检可疑帧"""
        self._ensure_model()
        frames = _extract_video_frames(filepath, max_frames)
        if not frames:
            return 0.0

        # Stage 1: Falconsai 粗筛所有帧
        arrays = []
        valid_frames = []
        for fp in frames:
            arr = _preprocess_single(fp)
            if arr is not None:
                arrays.append(arr)
                valid_frames.append(fp)

        if not arrays:
            _cleanup_frames(frames)
            return 0.0

        coarse_scores = self._run_batch(np.stack(arrays, axis=0))

        # Stage 2: NudeNet 精检超过粗筛阈值的帧
        suspicious_frames = [
            fp for fp, cs in zip(valid_frames, coarse_scores)
            if cs >= self.COARSE_THRESHOLD
        ]

        max_score = 0.0
        if suspicious_frames:
            self._ensure_nudenet()
            for fp in suspicious_frames:
                img = _read_image_cv2(fp)
                if img is not None:
                    try:
                        detections = self._nude_detector.detect(img)
                        score = _nudenet_max_score(detections)
                        max_score = max(max_score, score)
                    except Exception:
                        pass

        _cleanup_frames(frames)
        return max_score


def _nudenet_max_score(detections: list) -> float:
    """从 NudeNet 检测结果中提取 NSFW 部位的最高置信度"""
    max_score = 0.0
    for d in detections:
        cls = d.get("class", "")
        score = d.get("score", 0.0)
        if cls in _NUDENET_NSFW_CLASSES and score >= _NUDENET_MIN_CONFIDENCE:
            max_score = max(max_score, score)
    return max_score


def _cleanup_frames(frames: List[str]):
    for fp in frames:
        try:
            os.unlink(fp)
        except OSError:
            pass


def _softmax(x: np.ndarray) -> np.ndarray:
    e = np.exp(x - np.max(x, axis=-1, keepdims=True))
    return e / np.sum(e, axis=-1, keepdims=True)


# ── 视频抽帧 ──

def _extract_video_frames(video_path: str, max_frames: int) -> List[str]:
    """用 ffmpeg 从视频均匀抽取帧"""
    tmpdir = tempfile.mkdtemp(prefix="nsfw_frames_")
    try:
        ffmpeg = _get_ffmpeg_path()
        if not ffmpeg:
            return []

        duration = _get_video_duration(ffmpeg, video_path)
        if duration <= 0:
            duration = 30.0

        interval = max(duration / (max_frames + 1), 1.0)
        paths = []
        for i in range(max_frames):
            ts = interval * (i + 1)
            if ts >= duration:
                break
            out = os.path.join(tmpdir, f"f_{i:03d}.jpg")
            try:
                subprocess.run(
                    [ffmpeg, "-ss", f"{ts:.2f}", "-i", video_path,
                     "-frames:v", "1", "-q:v", "2", "-y", out],
                    capture_output=True, timeout=15,
                    encoding="utf-8", errors="replace",
                )
                if os.path.isfile(out) and os.path.getsize(out) > 0:
                    paths.append(out)
            except (subprocess.TimeoutExpired, OSError):
                pass
        return paths
    except Exception as e:
        logger.debug("视频抽帧失败 %s: %s", video_path, e)
        return []


def _get_ffmpeg_path() -> Optional[str]:
    try:
        import static_ffmpeg
        static_ffmpeg.add_paths()
    except ImportError:
        pass
    import shutil
    return shutil.which("ffmpeg")


def _get_video_duration(ffmpeg: str, path: str) -> float:
    import re
    try:
        r = subprocess.run(
            [ffmpeg, "-i", path], capture_output=True,
            encoding="utf-8", errors="replace", timeout=10,
        )
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+)\.(\d+)", r.stderr)
        if m:
            return int(m[1]) * 3600 + int(m[2]) * 60 + int(m[3]) + int(m[4]) / 100.0
    except Exception:
        pass
    return 0.0
