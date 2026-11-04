# -*- coding: utf-8 -*-
"""Scroll Registration：相邻两屏估计真实纵向位移 delta_y，维护 cumulative_scroll_y。

第一版：OpenCV（相位相关 / ORB 中位数 dy / 模板互相关）。
可选：torch+lightglue 时可用 SuperPoint 匹配点中位数。
页面基本只有纵向滚动，只估 delta_y。
"""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

CONTENT_Y0 = 400
CONTENT_Y1_FRAC = 0.92
CONTENT_X0 = 420
CONTENT_X1 = 1040


@dataclass
class ScrollEstimate:
    delta_y: float
    method: str
    confidence: float
    n_matches: int = 0


class ScrollTracker:
    """跨屏滚动累计器。"""

    def __init__(self, log=None):
        self.cumulative_scroll_y = 0.0
        self.prev_gray: np.ndarray | None = None
        self.prev_rgb: np.ndarray | None = None
        self._log = log or (lambda m: None)
        self.last = ScrollEstimate(0.0, "init", 1.0)

    def reset(self) -> None:
        self.cumulative_scroll_y = 0.0
        self.prev_gray = None
        self.prev_rgb = None
        self.last = ScrollEstimate(0.0, "reset", 1.0)

    def register(self, curr_rgb: np.ndarray) -> ScrollEstimate:
        """估计页面下滚位移并累加。

        estimate_delta_y 返回 median(curr_y - prev_y)（屏上位移）。
        向下滚瀑布流时卡片上移 → median 为负 → 页面位移 delta_page = -median。
        """
        if curr_rgb.ndim == 3:
            curr_gray = cv2.cvtColor(curr_rgb, cv2.COLOR_RGB2GRAY)
        else:
            curr_gray = curr_rgb

        if self.prev_gray is None:
            self.prev_gray = curr_gray
            self.prev_rgb = curr_rgb
            self.last = ScrollEstimate(0.0, "first_frame", 1.0)
            self._log("[SCROLL] delta_y=0.0 method=first_frame cum=0.0")
            return self.last

        est = estimate_delta_y(self.prev_gray, curr_gray, self.prev_rgb, curr_rgb)
        delta_page = float(-est.delta_y)
        method = est.method
        conf = est.confidence
        if abs(delta_page) > 420:
            self._log(
                f"[SCROLL] delta_y={delta_page:.1f} CLAMPED method={method} n={est.n_matches}"
            )
            delta_page = float(np.clip(delta_page, -420, 420))
            method = method + "+clamp"
            conf *= 0.5

        self.cumulative_scroll_y += delta_page
        self.last = ScrollEstimate(delta_page, method, conf, est.n_matches)
        self.prev_gray = curr_gray
        self.prev_rgb = curr_rgb
        self._log(
            f"[SCROLL] delta_y={delta_page:.1f} method={method} "
            f"conf={conf:.2f} n={est.n_matches} cum={self.cumulative_scroll_y:.1f}"
        )
        return self.last


def _band(gray: np.ndarray) -> np.ndarray:
    h, w = gray.shape[:2]
    y0, y1 = CONTENT_Y0, int(h * CONTENT_Y1_FRAC)
    x0, x1 = max(0, CONTENT_X0), min(w, CONTENT_X1)
    return gray[y0:y1, x0:x1]


def estimate_delta_y(
    prev_gray: np.ndarray,
    curr_gray: np.ndarray,
    prev_rgb: np.ndarray | None = None,
    curr_rgb: np.ndarray | None = None,
) -> ScrollEstimate:
    """估计 curr 相对 prev 的纵向位移 median(curr_y - prev_y)。"""
    if prev_rgb is not None and curr_rgb is not None:
        lg = _try_lightglue(prev_rgb, curr_rgb)
        if lg is not None and lg.n_matches >= 12 and lg.confidence >= 0.3:
            return lg

    est = _delta_phase(prev_gray, curr_gray)
    if est.confidence >= 0.35 and abs(est.delta_y) < 500:
        return est
    est_orb = _delta_orb(prev_gray, curr_gray)
    if est_orb.n_matches >= 12 and est_orb.confidence >= 0.25:
        return est_orb
    est_tpl = _delta_template(prev_gray, curr_gray)
    if est_tpl.confidence >= 0.3:
        return est_tpl
    return max([est, est_orb, est_tpl], key=lambda z: z.confidence)


def _delta_phase(prev: np.ndarray, curr: np.ndarray) -> ScrollEstimate:
    a = _band(prev).astype(np.float32)
    b = _band(curr).astype(np.float32)
    if a.size < 1000 or a.shape != b.shape:
        return ScrollEstimate(0.0, "phase_fail", 0.0)
    try:
        shift, response = cv2.phaseCorrelate(a, b)
    except Exception:
        return ScrollEstimate(0.0, "phase_fail", 0.0)
    return ScrollEstimate(float(shift[1]), "phase", float(max(0.0, min(1.0, response))), 0)


def _delta_orb(prev: np.ndarray, curr: np.ndarray) -> ScrollEstimate:
    a, b = _band(prev), _band(curr)
    orb = cv2.ORB_create(nfeatures=1200, fastThreshold=12)
    kp1, des1 = orb.detectAndCompute(a, None)
    kp2, des2 = orb.detectAndCompute(b, None)
    if des1 is None or des2 is None or len(kp1) < 8 or len(kp2) < 8:
        return ScrollEstimate(0.0, "orb_fail", 0.0, 0)
    bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=False)
    knn = bf.knnMatch(des1, des2, k=2)
    dys = []
    for pair in knn:
        if len(pair) < 2:
            continue
        m, n = pair
        if m.distance > 0.75 * n.distance:
            continue
        x1, y1 = kp1[m.queryIdx].pt
        x2, y2 = kp2[m.trainIdx].pt
        if abs(x2 - x1) > 40:
            continue
        dys.append(y2 - y1)
    if len(dys) < 8:
        return ScrollEstimate(0.0, "orb_few", 0.05, len(dys))
    med = float(np.median(dys))
    mad = float(np.median(np.abs(np.array(dys) - med))) + 1e-3
    inliers = [d for d in dys if abs(d - med) <= 3.0 * mad]
    conf = min(1.0, len(inliers) / 40.0)
    return ScrollEstimate(
        float(np.median(inliers) if inliers else med), "orb", conf, len(inliers)
    )


def _delta_template(prev: np.ndarray, curr: np.ndarray) -> ScrollEstimate:
    a, b = _band(prev), _band(curr)
    h = a.shape[0]
    band_h = max(48, h // 8)
    y0 = h // 3
    tmpl = a[y0 : y0 + band_h, :]
    if tmpl.size < 100 or tmpl.shape[0] >= b.shape[0] or tmpl.shape[1] > b.shape[1]:
        return ScrollEstimate(0.0, "tpl_fail", 0.0)
    res = cv2.matchTemplate(b, tmpl, cv2.TM_CCOEFF_NORMED)
    _, max_val, _, max_loc = cv2.minMaxLoc(res)
    return ScrollEstimate(
        float(max_loc[1] - y0), "tpl", float(max(0.0, min(1.0, max_val))), 1
    )


def _try_lightglue(prev_rgb: np.ndarray, curr_rgb: np.ndarray) -> ScrollEstimate | None:
    try:
        import torch
        from lightglue import LightGlue, SuperPoint
        from lightglue.utils import rbd
    except Exception:
        return None

    device = "cuda" if torch.cuda.is_available() else "cpu"
    global _SP, _LG
    if "_SP" not in globals() or _SP is None:
        _SP = SuperPoint(max_num_keypoints=512).eval().to(device)
        _LG = LightGlue(features="superpoint").eval().to(device)

    def prep(rgb):
        g = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY) if rgb.ndim == 3 else rgb
        band = _band(g)
        rgb3 = np.stack([band, band, band], axis=-1)
        return torch.from_numpy(rgb3).float().permute(2, 0, 1)[None].to(device) / 255.0

    with torch.no_grad():
        f0 = _SP.extract(prep(prev_rgb))
        f1 = _SP.extract(prep(curr_rgb))
        matches01 = _LG({"image0": f0, "image1": f1})
        f0, f1, matches01 = [rbd(x) for x in [f0, f1, matches01]]
        idx = matches01["matches"]
        if idx is None or len(idx) < 8:
            return ScrollEstimate(0.0, "lightglue_few", 0.05, 0)
        m0 = f0["keypoints"][idx[:, 0]]
        m1 = f1["keypoints"][idx[:, 1]]
        dys = (m1[:, 1] - m0[:, 1]).cpu().numpy()
        return ScrollEstimate(
            float(np.median(dys)), "lightglue", min(1.0, len(dys) / 40.0), len(dys)
        )


_SP = None
_LG = None
