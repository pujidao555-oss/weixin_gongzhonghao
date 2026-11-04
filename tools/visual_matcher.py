# -*- coding: utf-8 -*-
"""卡片局部视觉匹配：优先 SuperPoint+LightGlue，否则 OpenCV AKAZE/ORB。

用于 partial↔full / 上下半截 Re-ID，不要求两图尺寸一致。
粗粒度回退：aHash 汉明距离。
"""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

# LightGlue 可选
_HAS_LIGHTGLUE = False
try:
    import torch
    from lightglue import LightGlue, SuperPoint  # noqa: F401

    _HAS_LIGHTGLUE = True
except Exception:
    _HAS_LIGHTGLUE = False


@dataclass
class VisualMatch:
    score: float          # 0..1
    n_matches: int
    n_inliers: int
    cover_area: float     # 匹配点外接框占 query 比例
    method: str
    geometric_ok: bool


def ahash64(rgb: np.ndarray) -> int:
    if rgb is None or rgb.size == 0:
        return 0
    if rgb.ndim == 3:
        gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    else:
        gray = rgb
    small = cv2.resize(gray, (16, 16), interpolation=cv2.INTER_AREA)
    avg = float(small.mean())
    bits = (small[4:12, 4:12] >= avg).astype(np.uint8).flatten()
    val = 0
    for b in bits:
        val = (val << 1) | int(b)
    return int(val)


def ahash_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """1 - hamming/64。"""
    ha, hb = ahash64(a), ahash64(b)
    dist = (ha ^ hb).bit_count()
    return max(0.0, 1.0 - dist / 64.0)


def match_crops(
    query_rgb: np.ndarray,
    ref_rgb: np.ndarray,
    prefer_lightglue: bool = True,
) -> VisualMatch:
    """计算 visual_match_score（有效匹配点 / inlier / 覆盖 / 几何一致性）。"""
    if query_rgb is None or ref_rgb is None or query_rgb.size == 0 or ref_rgb.size == 0:
        return VisualMatch(0.0, 0, 0, 0.0, "empty", False)

    if prefer_lightglue and _HAS_LIGHTGLUE:
        m = _match_lightglue(query_rgb, ref_rgb)
        if m is not None and m.n_matches >= 6:
            return m

    m = _match_local_features(query_rgb, ref_rgb)
    if m.n_matches >= 8 and m.score >= 0.25:
        return m

    # 粗粒度 aHash
    sim = ahash_similarity(query_rgb, ref_rgb)
    return VisualMatch(
        score=sim * 0.85,
        n_matches=0,
        n_inliers=0,
        cover_area=0.0,
        method="ahash",
        geometric_ok=sim >= 0.75,
    )


def _to_gray(rgb: np.ndarray) -> np.ndarray:
    if rgb.ndim == 2:
        return rgb
    return cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)


def _match_local_features(query_rgb: np.ndarray, ref_rgb: np.ndarray) -> VisualMatch:
    """OpenCV 5 无 AKAZE：优先 SIFT，回退 ORB（LightGlue 未装时的局部特征 Re-ID）。"""
    q = _limit_side(_to_gray(query_rgb), 320)
    r = _limit_side(_to_gray(ref_rgb), 320)
    method = "sift"
    norm = cv2.NORM_L2
    try:
        detector = cv2.SIFT_create(nfeatures=800)
    except Exception:
        detector = cv2.ORB_create(nfeatures=1000)
        method = "orb"
        norm = cv2.NORM_HAMMING
    kp1, des1 = detector.detectAndCompute(q, None)
    kp2, des2 = detector.detectAndCompute(r, None)
    if des1 is None or des2 is None or len(kp1) < 6 or len(kp2) < 6:
        return VisualMatch(0.0, 0, 0, 0.0, f"{method}_fail", False)
    bf = cv2.BFMatcher(norm)
    knn = bf.knnMatch(des1, des2, k=2)
    good = []
    pts1, pts2 = [], []
    for pair in knn:
        if len(pair) < 2:
            continue
        m, n = pair
        if m.distance < 0.78 * n.distance:
            good.append(m)
            pts1.append(kp1[m.queryIdx].pt)
            pts2.append(kp2[m.trainIdx].pt)
    n_good = len(good)
    if n_good < 6:
        return VisualMatch(n_good / 40.0, n_good, 0, 0.0, f"{method}_few", False)

    pts1 = np.float32(pts1)
    pts2 = np.float32(pts2)
    H, mask = cv2.findHomography(pts1, pts2, cv2.RANSAC, 5.0)
    if mask is None:
        return VisualMatch(n_good / 50.0, n_good, 0, 0.0, f"{method}_nohom", False)
    inliers = int(mask.ravel().sum())
    inl_pts = pts1[mask.ravel().astype(bool)]
    if len(inl_pts) >= 3:
        x0, y0 = inl_pts[:, 0].min(), inl_pts[:, 1].min()
        x1, y1 = inl_pts[:, 0].max(), inl_pts[:, 1].max()
        cover = ((x1 - x0) * (y1 - y0)) / float(max(1, q.shape[0] * q.shape[1]))
    else:
        cover = 0.0
    geo_ok = H is not None and inliers >= 6
    score = (
        0.45 * min(1.0, inliers / 25.0)
        + 0.25 * min(1.0, n_good / 40.0)
        + 0.20 * min(1.0, cover * 4.0)
        + (0.10 if geo_ok else 0.0)
    )
    return VisualMatch(float(score), n_good, inliers, float(cover), method, geo_ok)


def _limit_side(gray: np.ndarray, max_side: int) -> np.ndarray:
    h, w = gray.shape[:2]
    m = max(h, w)
    if m <= max_side:
        return gray
    scale = max_side / float(m)
    return cv2.resize(gray, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)


def _match_lightglue(query_rgb: np.ndarray, ref_rgb: np.ndarray) -> VisualMatch | None:
    if not _HAS_LIGHTGLUE:
        return None
    try:
        import torch
        from lightglue import LightGlue, SuperPoint
        from lightglue.utils import rbd
    except Exception:
        return None

    device = "cuda" if torch.cuda.is_available() else "cpu"
    # 懒加载单例
    global _LG_EXTRACTOR, _LG_MATCHER
    if "_LG_EXTRACTOR" not in globals() or _LG_EXTRACTOR is None:
        _LG_EXTRACTOR = SuperPoint(max_num_keypoints=512).eval().to(device)
        _LG_MATCHER = LightGlue(features="superpoint").eval().to(device)

    def prep(rgb):
        g = _to_gray(rgb)
        g = _limit_side(g, 480)
        rgb3 = np.stack([g, g, g], axis=-1)
        t = torch.from_numpy(rgb3).float().permute(2, 0, 1)[None] / 255.0
        return t.to(device), g.shape

    with torch.no_grad():
        t0, shape0 = prep(query_rgb)
        t1, shape1 = prep(ref_rgb)
        f0 = _LG_EXTRACTOR.extract(t0)
        f1 = _LG_EXTRACTOR.extract(t1)
        matches01 = _LG_MATCHER({"image0": f0, "image1": f1})
        f0, f1, matches01 = [rbd(x) for x in [f0, f1, matches01]]
        idx = matches01["matches"]
        if idx is None or len(idx) < 6:
            return VisualMatch(0.05, 0, 0, 0.0, "lightglue_few", False)
        k0 = f0["keypoints"][idx[:, 0]].cpu().numpy()
        k1 = f1["keypoints"][idx[:, 1]].cpu().numpy()
        n = len(k0)
        H, mask = cv2.findHomography(k0, k1, cv2.RANSAC, 5.0)
        inliers = int(mask.ravel().sum()) if mask is not None else 0
        if mask is not None and inliers >= 3:
            inl = k0[mask.ravel().astype(bool)]
            cover = (
                (inl[:, 0].max() - inl[:, 0].min())
                * (inl[:, 1].max() - inl[:, 1].min())
            ) / float(max(1, shape0[0] * shape0[1]))
        else:
            cover = 0.0
        geo_ok = H is not None and inliers >= 6
        score = (
            0.45 * min(1.0, inliers / 25.0)
            + 0.25 * min(1.0, n / 40.0)
            + 0.20 * min(1.0, cover * 4.0)
            + (0.10 if geo_ok else 0.0)
        )
        return VisualMatch(float(score), n, inliers, float(cover), "lightglue", geo_ok)


_LG_EXTRACTOR = None
_LG_MATCHER = None


def backend_name() -> str:
    return "lightglue+sift+ahash" if _HAS_LIGHTGLUE else "sift+ahash"
