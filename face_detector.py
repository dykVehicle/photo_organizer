"""人脸识别模块：InsightFace 检测+嵌入 + HDBSCAN 聚类 + 硬链接人物相册

Phase 4：在文件复制完成后对目标盘图片执行人脸检测、512-D 嵌入提取、
密度聚类，按人物创建 NTFS 硬链接文件夹，零额外磁盘空间。
"""

import base64
import json
import hashlib
import logging
import os
import struct
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np
from tqdm import tqdm

logger = logging.getLogger(__name__)

# ── 常量 ──
_EMBEDDING_DIM = 512
_CACHE_SAVE_INTERVAL = 200
_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif", ".heic", ".heif"}


def check_dependencies():
    """检查人脸识别所需依赖"""
    missing = []
    try:
        import insightface  # noqa: F401
    except ImportError:
        missing.append("insightface")
    try:
        import onnxruntime  # noqa: F401
    except ImportError:
        missing.append("onnxruntime")
    try:
        from sklearn.cluster import HDBSCAN  # noqa: F401
    except ImportError:
        try:
            import hdbscan  # noqa: F401
        except ImportError:
            missing.append("scikit-learn>=1.3 (或 hdbscan)")
    if missing:
        raise ImportError(
            f"人脸识别需要额外依赖: {', '.join(missing)}\n"
            "请执行: pip install insightface onnxruntime-gpu scikit-learn"
        )


# ── 数据结构 ──

@dataclass
class FaceInfo:
    bbox: Tuple[int, int, int, int]
    embedding: np.ndarray
    det_score: float


@dataclass
class PersonCluster:
    person_id: str
    face_count: int
    file_hashes: List[str]
    centroid: np.ndarray


@dataclass
class FaceAlbumResult:
    total_faces: int = 0
    total_persons: int = 0
    total_links: int = 0
    outlier_faces: int = 0


# ── 嵌入向量序列化 ──

def _emb_to_b64(emb: np.ndarray) -> str:
    return base64.b64encode(emb.astype(np.float32).tobytes()).decode("ascii")


def _b64_to_emb(s: str) -> np.ndarray:
    return np.frombuffer(base64.b64decode(s), dtype=np.float32).copy()


# ── 缓存管理 ──

def _load_embedding_cache(path: str) -> dict:
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("version") != 1:
            return {}
        return data.get("entries", {})
    except Exception as e:
        logger.debug("嵌入缓存加载失败: %s", e)
        return {}


def _save_embedding_cache(path: str, entries: dict):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = {"version": 1, "entries": entries}
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception as e:
        logger.debug("嵌入缓存保存失败: %s", e)


def _load_clustering_cache(path: str) -> Optional[dict]:
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("version") != 1:
            return None
        return data
    except Exception:
        return None


def _save_clustering_cache(path: str, fingerprint: str,
                           clusters: List[PersonCluster], outlier_hashes: List[str]):
    data = {
        "version": 1,
        "input_fingerprint": fingerprint,
        "clusters": [
            {
                "person_id": c.person_id,
                "file_hashes": c.file_hashes,
                "centroid": _emb_to_b64(c.centroid),
            }
            for c in clusters
        ],
        "outlier_hashes": outlier_hashes,
    }
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception as e:
        logger.debug("聚类缓存保存失败: %s", e)


def _compute_fingerprint(file_hashes: List[str]) -> str:
    combined = "\n".join(sorted(file_hashes))
    return hashlib.sha256(combined.encode()).hexdigest()[:32]


class FaceAnalyzer:
    """人脸检测 + 嵌入提取 + 聚类"""

    def __init__(self, det_size=(640, 640), min_face_size=40):
        self._app = None
        self._det_size = det_size
        self._min_face_size = min_face_size

    def _ensure_model(self):
        if self._app is not None:
            return

        from nsfw_detector import _add_nvidia_dll_paths
        _add_nvidia_dll_paths()

        from insightface.app import FaceAnalysis
        providers = ['CUDAExecutionProvider', 'CPUExecutionProvider']

        try:
            import onnxruntime as ort
            available = ort.get_available_providers()
            if 'CUDAExecutionProvider' not in available:
                providers = ['CPUExecutionProvider']
        except Exception:
            providers = ['CPUExecutionProvider']

        self._app = FaceAnalysis(name="buffalo_l", providers=providers)
        self._app.prepare(ctx_id=0, det_size=self._det_size)

        active = "GPU (CUDA)" if 'CUDAExecutionProvider' in providers else "CPU"
        logger.info("InsightFace buffalo_l 已加载 (%s), det_size=%s", active, self._det_size)

    def detect_faces(self, img_path: str) -> List[FaceInfo]:
        """检测单张图片中所有人脸"""
        self._ensure_model()
        try:
            img = _read_image_for_insightface(img_path)
            if img is None:
                return []
            faces = self._app.get(img)
            result = []
            for face in faces:
                if face.det_score < 0.5:
                    continue
                bbox = tuple(int(x) for x in face.bbox)
                w = bbox[2] - bbox[0]
                h = bbox[3] - bbox[1]
                if w < self._min_face_size or h < self._min_face_size:
                    continue
                if face.embedding is None:
                    continue
                result.append(FaceInfo(
                    bbox=bbox,
                    embedding=face.embedding.astype(np.float32),
                    det_score=float(face.det_score),
                ))
            return result
        except Exception as e:
            logger.debug("人脸检测失败 %s: %s", img_path, e)
            return []

    def extract_all(
        self,
        hash_to_path: Dict[str, str],
        cache_path: str,
        max_workers: int = 4,
    ) -> Dict[str, List[np.ndarray]]:
        """批量提取嵌入: {file_hash: [embedding1, ...]}

        双缓冲流水线：多线程读图 + 串行 GPU 推理。
        支持缓存命中跳过。
        """
        self._ensure_model()

        cache_entries = _load_embedding_cache(cache_path)
        embeddings_map: Dict[str, List[np.ndarray]] = {}

        cached_count = 0
        to_detect: List[Tuple[str, str]] = []

        for fhash, fpath in hash_to_path.items():
            if fhash in cache_entries:
                entry = cache_entries[fhash]
                if entry.get("no_face"):
                    cached_count += 1
                    continue
                embs = [_b64_to_emb(f["embedding"]) for f in entry.get("faces", [])]
                if embs:
                    embeddings_map[fhash] = embs
                cached_count += 1
            else:
                to_detect.append((fhash, fpath))

        need_detect = len(to_detect)
        total = len(hash_to_path)
        logger.info("人脸嵌入: %d 张图片, 缓存命中 %d, 需检测 %d", total, cached_count, need_detect)

        if not to_detect:
            return embeddings_map

        processed_since_save = 0
        new_faces_total = 0

        def _read_img(item):
            fhash, fpath = item
            img = _read_image_for_insightface(fpath)
            return fhash, fpath, img

        read_workers = min(max_workers, 4)
        with tqdm(total=cached_count + need_detect, initial=cached_count,
                  desc="人脸检测", unit="张") as pbar:
            pool = ThreadPoolExecutor(max_workers=read_workers)
            try:
                batch_size = 8
                for bi in range(0, len(to_detect), batch_size):
                    batch = to_detect[bi:bi + batch_size]
                    futs = [pool.submit(_read_img, item) for item in batch]

                    for fut in futs:
                        fhash, fpath, img = fut.result()
                        if img is None:
                            cache_entries[fhash] = {"faces": [], "no_face": True}
                        else:
                            try:
                                faces = self._app.get(img)
                            except Exception:
                                faces = []

                            face_list = []
                            embs = []
                            for face in faces:
                                if face.det_score < 0.5 or face.embedding is None:
                                    continue
                                bbox = tuple(int(x) for x in face.bbox)
                                w = bbox[2] - bbox[0]
                                h = bbox[3] - bbox[1]
                                if w < self._min_face_size or h < self._min_face_size:
                                    continue
                                emb = face.embedding.astype(np.float32)
                                embs.append(emb)
                                face_list.append({
                                    "bbox": list(bbox),
                                    "embedding": _emb_to_b64(emb),
                                    "score": round(float(face.det_score), 3),
                                })
                                new_faces_total += 1

                            cache_entries[fhash] = {
                                "faces": face_list,
                                "no_face": len(face_list) == 0,
                            }
                            if embs:
                                embeddings_map[fhash] = embs

                        pbar.update(1)
                        processed_since_save += 1

                        if processed_since_save >= _CACHE_SAVE_INTERVAL:
                            _save_embedding_cache(cache_path, cache_entries)
                            processed_since_save = 0
            finally:
                pool.shutdown(wait=False, cancel_futures=True)

        _save_embedding_cache(cache_path, cache_entries)
        logger.info("人脸检测完成: 新检测 %d 张, 发现 %d 张人脸, 缓存总计 %d 条",
                     need_detect, new_faces_total, len(cache_entries))

        return embeddings_map

    @staticmethod
    def cluster(
        embeddings_map: Dict[str, List[np.ndarray]],
        min_cluster_size: int = 2,
        cache_path: str = "",
    ) -> Tuple[List[PersonCluster], List[str]]:
        """HDBSCAN 聚类: {hash: [emb...]} → ([PersonCluster], outlier_hashes)"""

        all_embs = []
        all_labels = []  # (file_hash, face_idx)

        for fhash, embs in embeddings_map.items():
            for i, emb in enumerate(embs):
                all_embs.append(emb)
                all_labels.append((fhash, i))

        if not all_embs:
            logger.info("无人脸嵌入，跳过聚类")
            return [], []

        file_hashes_with_faces = list(embeddings_map.keys())
        fingerprint = _compute_fingerprint(file_hashes_with_faces)

        if cache_path:
            cached = _load_clustering_cache(cache_path)
            if cached and cached.get("input_fingerprint") == fingerprint:
                logger.info("聚类缓存命中 (fingerprint=%s...)", fingerprint[:12])
                clusters = []
                for c in cached.get("clusters", []):
                    clusters.append(PersonCluster(
                        person_id=c["person_id"],
                        face_count=len(c["file_hashes"]),
                        file_hashes=c["file_hashes"],
                        centroid=_b64_to_emb(c["centroid"]),
                    ))
                return clusters, cached.get("outlier_hashes", [])

        X = np.array(all_embs, dtype=np.float32)
        from sklearn.preprocessing import normalize
        X_norm = normalize(X, axis=1)

        logger.info("HDBSCAN 聚类: %d 个人脸嵌入, min_cluster_size=%d", len(X_norm), min_cluster_size)
        t0 = time.time()

        try:
            from sklearn.cluster import HDBSCAN as SkHDBSCAN
            clusterer = SkHDBSCAN(
                min_cluster_size=min_cluster_size,
                metric="euclidean",
                cluster_selection_method="eom",
                n_jobs=-1,
            )
        except ImportError:
            import hdbscan
            clusterer = hdbscan.HDBSCAN(
                min_cluster_size=min_cluster_size,
                metric="euclidean",
                cluster_selection_method="eom",
                core_dist_n_jobs=-1,
            )

        labels = clusterer.fit_predict(X_norm)
        elapsed = time.time() - t0

        cluster_map: Dict[int, List[Tuple[str, int]]] = {}
        outlier_set = set()

        for idx, label in enumerate(labels):
            fhash, face_idx = all_labels[idx]
            if label == -1:
                outlier_set.add(fhash)
            else:
                cluster_map.setdefault(label, []).append((fhash, face_idx))

        clusters = []
        for cid in sorted(cluster_map.keys()):
            items = cluster_map[cid]
            file_hashes = list(dict.fromkeys(fhash for fhash, _ in items))
            indices = [idx for idx, (fh, fi) in enumerate(all_labels) if fh in set(file_hashes)]
            centroid = X_norm[indices].mean(axis=0).astype(np.float32)
            person_id = f"人物_{cid + 1:03d}"
            clusters.append(PersonCluster(
                person_id=person_id,
                face_count=len(items),
                file_hashes=file_hashes,
                centroid=centroid,
            ))

        outlier_hashes = list(outlier_set - {h for c in clusters for h in c.file_hashes})

        n_clusters = len(clusters)
        n_outliers = sum(1 for l in labels if l == -1)
        logger.info("聚类完成: %d 个人物, %d 个离群人脸, 耗时 %.1fs", n_clusters, n_outliers, elapsed)

        if cache_path:
            _save_clustering_cache(cache_path, fingerprint, clusters, outlier_hashes)
            logger.info("聚类缓存已保存: %s", cache_path)

        return clusters, outlier_hashes

    @staticmethod
    def create_album(
        clusters: List[PersonCluster],
        file_hash_to_path: Dict[str, str],
        dest_dir: str,
        dry_run: bool = False,
    ) -> FaceAlbumResult:
        """创建人物相册: 硬链接 + face_index.json"""
        result = FaceAlbumResult()

        if not clusters:
            return result

        for c in clusters:
            result.total_faces += c.face_count
        result.total_persons = len(clusters)

        index_data = {
            "version": 1,
            "persons": [],
            "total_persons": len(clusters),
        }

        for cluster in clusters:
            person_dir = os.path.join(dest_dir, cluster.person_id)
            person_files = []

            for fhash in cluster.file_hashes:
                src_path = file_hash_to_path.get(fhash)
                if not src_path or not os.path.isfile(src_path):
                    continue

                link_name = os.path.basename(src_path)
                link_path = os.path.join(person_dir, link_name)
                link_path = _resolve_link_conflict(link_path)

                if not dry_run:
                    os.makedirs(person_dir, exist_ok=True)
                    try:
                        os.link(src_path, link_path)
                    except OSError:
                        try:
                            os.symlink(src_path, link_path)
                        except OSError as e:
                            logger.debug("硬链接/符号链接失败 %s → %s: %s", src_path, link_path, e)
                            continue

                result.total_links += 1
                person_files.append({
                    "filename": os.path.basename(link_path),
                    "source": src_path,
                    "hash": fhash,
                })

            index_data["persons"].append({
                "person_id": cluster.person_id,
                "face_count": cluster.face_count,
                "photo_count": len(person_files),
                "files": person_files,
            })

        if not dry_run:
            index_path = os.path.join(dest_dir, "face_index.json")
            os.makedirs(dest_dir, exist_ok=True)
            try:
                with open(index_path, "w", encoding="utf-8") as f:
                    json.dump(index_data, f, ensure_ascii=False, indent=2)
                logger.info("face_index.json 已生成: %s", index_path)
            except Exception as e:
                logger.warning("face_index.json 写入失败: %s", e)

        return result

    def run_pipeline(
        self,
        records,
        dest_dir: str,
        cache_dir: str,
        max_workers: int = 4,
        skip_nsfw_dir: str = "",
        min_cluster_size: int = 2,
        dry_run: bool = False,
        cache_only: bool = False,
    ) -> FaceAlbumResult:
        """完整流水线: 收集图片 → 检测嵌入 → 聚类 → 创建相册"""

        # F.1 收集目标盘图片路径（跳过 NSFW）
        nsfw_prefix = ""
        if skip_nsfw_dir:
            nsfw_prefix = os.path.normcase(skip_nsfw_dir + os.sep)

        hash_to_path: Dict[str, str] = {}
        for record in records:
            if record.status not in ("ok", "dry_run"):
                continue
            if not record.file_hash or not record.destination:
                continue
            ext = os.path.splitext(record.destination)[1].lower()
            if ext not in _IMAGE_EXTENSIONS:
                continue
            if nsfw_prefix and os.path.normcase(record.destination).startswith(nsfw_prefix):
                continue
            hash_to_path[record.file_hash] = record.destination

        logger.info("人脸识别: 收集 %d 张图片（跳过 NSFW 和非图片文件）", len(hash_to_path))
        if not hash_to_path:
            return FaceAlbumResult()

        # F.2+F.3 人脸检测 + 嵌入提取（带缓存）
        emb_cache_path = os.path.join(cache_dir, "face_embedding_cache.json")
        embeddings_map = self.extract_all(hash_to_path, emb_cache_path, max_workers)

        if not embeddings_map:
            logger.info("未检测到任何人脸")
            return FaceAlbumResult()

        if cache_only:
            total_faces = sum(len(v) for v in embeddings_map.values())
            logger.info("cache_only 模式: 仅保存嵌入缓存, 共 %d 张人脸", total_faces)
            return FaceAlbumResult(total_faces=total_faces)

        # F.4 HDBSCAN 聚类
        cluster_cache_path = os.path.join(cache_dir, "face_clustering_cache.json")
        clusters, outlier_hashes = self.cluster(
            embeddings_map, min_cluster_size, cluster_cache_path
        )

        if not clusters:
            return FaceAlbumResult(
                total_faces=sum(len(v) for v in embeddings_map.values()),
                outlier_faces=len(outlier_hashes),
            )

        # F.5+F.6 创建硬链接相册 + face_index.json
        album_result = self.create_album(clusters, hash_to_path, dest_dir, dry_run)
        album_result.outlier_faces = len(outlier_hashes)

        return album_result


# ── 工具函数 ──

def _read_image_for_insightface(filepath: str) -> Optional[np.ndarray]:
    """读取图片为 BGR numpy 数组（兼容中文路径）"""
    try:
        with open(filepath, "rb") as f:
            data = np.frombuffer(f.read(), np.uint8)
        import cv2
        img = cv2.imdecode(data, cv2.IMREAD_COLOR)
        return img
    except Exception as e:
        logger.debug("图片读取失败 %s: %s", filepath, e)
        return None


def _resolve_link_conflict(link_path: str) -> str:
    """硬链接同名冲突处理"""
    if not os.path.exists(link_path):
        return link_path
    base, ext = os.path.splitext(link_path)
    counter = 1
    while True:
        new_path = f"{base}_f{counter}{ext}"
        if not os.path.exists(new_path):
            return new_path
        counter += 1
