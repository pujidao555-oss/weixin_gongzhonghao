# -*- coding: utf-8 -*-
"""Card Track Manager：用 global_y + 列 + 视觉 Re-ID 跟踪瀑布流卡片。

状态：PARTIAL → TRACKING → FULL → CONFIRMED
半截卡可进 Track，但不立即发放 card_uuid；完整后才 CONFIRMED 入库。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Callable

import numpy as np

import visual_matcher as vm

# 与 scan 几何对齐（可被外部覆盖）
COL_MID = 700.0
POSTER_H_FULL = 280          # 完整海报软门槛（非「是否识别」硬条件）
GLOBAL_Y_MATCH_PX = 120.0    # 同列 global_y 宽松窗（超出则不考虑）
GLOBAL_Y_TIGHT_PX = 55.0     # 位置追踪：足够近则直接延续 Track，不调 Re-ID
IOU_MIN = 0.12
VISUAL_MATCH_MIN = 0.42
VISUAL_MATCH_STRONG = 0.58
# 滚动跳变过大 / 海报过残 → 才请 Re-ID 当裁判
REID_SCROLL_JUMP_PX = 220.0
REID_PARTIAL_PH = 200


@dataclass
class CardTrack:
    track_id: str
    column: str  # "left" | "right"
    global_y: float          # 卡片顶（或 bar 中心）虚拟页坐标
    global_bar_y: float
    status: str              # PARTIAL|TRACKING|FULL|CONFIRMED
    card_uuid: str | None = None
    title: str = ""
    title_id: str = ""
    fp: str = ""
    poster_h: int = 0
    screen_bbox: tuple[int, int, int, int] = (0, 0, 0, 0)  # x,y,w,h
    cx: float = 0.0
    cy: float = 0.0
    bar_x: int = 0
    bar_y: int = 0
    bar_w: int = 0
    bar_h: int = 0
    roi_x: int = 0
    roi_y: int = 0
    roi_w: int = 0
    roi_h: int = 0
    reference_image: np.ndarray | None = None
    bbox_history: list[tuple[float, tuple[int, int, int, int]]] = field(default_factory=list)
    hit_count: int = 0
    method: str = ""
    ink: float = 0.0

    def to_catalog_item(self) -> dict:
        return {
            "text": self.title or f"贴图_{(self.fp or self.track_id)[:10]}",
            "title_id": self.title_id,
            "key": self.card_uuid or self.track_id,
            "fp": self.fp,
            "card_uuid": self.card_uuid,
            "track_id": self.track_id,
            "column": self.column,
            "global_y": self.global_y,
            "global_bar_y": self.global_bar_y,
            "status": self.status,
            "cx": self.cx,
            "cy": self.cy,
            "bar_x": self.bar_x,
            "bar_y": self.bar_y,
            "bar_w": self.bar_w,
            "bar_h": self.bar_h,
            "roi_x": self.roi_x,
            "roi_y": self.roi_y,
            "roi_w": self.roi_w,
            "roi_h": self.roi_h,
            "poster_h": self.poster_h,
            "short": self.poster_h > 0 and self.poster_h < POSTER_H_FULL,
            "ink": self.ink,
            "method": self.method or "track",
        }


class CardTrackManager:
    def __init__(self, log: Callable[[str], None] | None = None, col_mid: float = COL_MID):
        self.tracks: list[CardTrack] = []
        self._log = log or (lambda m: None)
        self.col_mid = col_mid
        self.frame_idx = 0

    def reset(self) -> None:
        self.tracks.clear()
        self.frame_idx = 0

    def column_of(self, det: dict) -> str:
        x = float(det.get("bar_x") or det.get("roi_x") or det.get("cx") or 0)
        return "left" if x < self.col_mid else "right"

    def _crop_ref(self, frame: np.ndarray, det: dict) -> np.ndarray | None:
        if frame is None:
            return None
        x = int(det.get("roi_x") or det.get("bar_x") or 0)
        y = int(det.get("roi_y") or max(0, int(det.get("bar_y") or 0) - int(det.get("poster_h") or 80)))
        w = int(det.get("roi_w") or det.get("bar_w") or 0)
        h = int(det.get("roi_h") or (int(det.get("poster_h") or 80) + int(det.get("bar_h") or 40)))
        if w < 20 or h < 20:
            return None
        H, W = frame.shape[:2]
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(W, x + w), min(H, y + h)
        if x1 <= x0 + 8 or y1 <= y0 + 8:
            return None
        return frame[y0:y1, x0:x1].copy()

    def observe(
        self,
        detections: list[dict],
        frame_rgb: np.ndarray,
        cumulative_scroll_y: float,
        delta_y: float = 0.0,
    ) -> tuple[int, int, int]:
        """吃进本屏检测。返回 (new_tracks, merges, new_uuids)。

        流程：轮廓检测结果 → 位置追踪延续 Track → 歧义才 Re-ID → 新卡建 Track。
        """
        self.frame_idx += 1
        new_n = 0
        merge_n = 0
        new_uuids = 0
        used_tracks: set[str] = set()
        scroll_jump = abs(float(delta_y)) >= REID_SCROLL_JUMP_PX

        # 按 y 排序，稳定匹配
        dets = sorted(
            detections,
            key=lambda z: (self.column_of(z), float(z.get("bar_y") or z.get("roi_y") or 0)),
        )
        for det in dets:
            col = self.column_of(det)
            screen_y = float(det.get("roi_y") or det.get("bar_y") or 0)
            screen_bar_y = float(det.get("bar_y") or screen_y)
            gy = cumulative_scroll_y + screen_y
            gbar = cumulative_scroll_y + screen_bar_y
            poster_h = int(det.get("poster_h") or 0)
            crop = self._crop_ref(frame_rgb, det)

            hit, how = self._match_track(
                det, col, gy, gbar, crop, used_tracks, poster_h, scroll_jump
            )
            if hit is not None:
                used_tracks.add(hit.track_id)
                before_uuid = hit.card_uuid
                self._update_track(hit, det, gy, gbar, crop, poster_h)
                merge_n += 1
                if hit.card_uuid and not before_uuid:
                    new_uuids += 1
                self._log(
                    f"[TRACK] matched track={hit.track_id[:8]} "
                    f"uuid={hit.card_uuid or '-'} col={col} "
                    f"gy={gbar:.0f} status={hit.status} via={how}"
                )
                continue

            # 新 track（半截也建，但不发 uuid）
            track = self._spawn_track(det, col, gy, gbar, crop, poster_h)
            self.tracks.append(track)
            used_tracks.add(track.track_id)
            new_n += 1
            if track.card_uuid:
                new_uuids += 1
            self._log(
                f"[NEW] track={track.track_id[:8]} uuid={track.card_uuid or '-'} "
                f"col={col} gy={gbar:.0f} status={track.status} "
                f"ph={poster_h} title={track.title_id or '-'}"
            )

        return new_n, merge_n, new_uuids

    def _match_track(
        self,
        det: dict,
        col: str,
        gy: float,
        gbar: float,
        crop: np.ndarray | None,
        used: set[str],
        poster_h: int,
        scroll_jump: bool,
    ) -> tuple[CardTrack | None, str]:
        """位置追踪优先；Re-ID 仅作裁判。

        1) column + global_y 预测：唯一且足够近 → 直接延续（不看图）
        2) 多候选 / 位置模糊 / 半截 / 大跳滚 → LightGlue/SIFT 认亲
        3) title 只加分，绝不单独决定
        """
        # —— 第一层：同列位置候选 ——
        loose: list[tuple[float, CardTrack]] = []
        tight: list[tuple[float, CardTrack]] = []
        for t in self.tracks:
            if t.track_id in used or t.column != col:
                continue
            dy = abs(t.global_bar_y - gbar)
            if dy > GLOBAL_Y_MATCH_PX * 2.5:
                continue
            loose.append((dy, t))
            if dy <= GLOBAL_Y_TIGHT_PX:
                tight.append((dy, t))

        loose.sort(key=lambda z: z[0])
        tight.sort(key=lambda z: z[0])

        # 唯一紧匹配 + 非大跳滚 + 非残缺半截 → 纯位置延续
        second_close = len(loose) >= 2 and loose[1][0] < GLOBAL_Y_TIGHT_PX * 1.8
        need_reid = (
            scroll_jump
            or poster_h < REID_PARTIAL_PH
            or len(tight) != 1
            or second_close
        )
        if len(tight) == 1 and not need_reid:
            dy, t = tight[0]
            self._log(
                f"[TRACK] pos-continue track={t.track_id[:8]} "
                f"dgy={dy:.0f} (skip Re-ID)"
            )
            return t, "pos"

        # 无任何位置候选且非大跳/半截 → 当作新卡（不乱认亲）
        if not loose and not scroll_jump:
            return None, ""

        # —— 第二层：Re-ID 裁判（只对位置候选或大跳时的近邻）——
        pool = tight if tight else loose
        if not pool:
            # 大跳滚：放宽同列全体未占用 track 做视觉比对（最多 4 个最近）
            pool = []
            for t in self.tracks:
                if t.track_id in used or t.column != col:
                    continue
                pool.append((abs(t.global_bar_y - gbar), t))
            pool.sort(key=lambda z: z[0])
            pool = pool[:4]
        if not pool:
            return None, ""

        reason = []
        if scroll_jump:
            reason.append("scroll_jump")
        if poster_h < REID_PARTIAL_PH:
            reason.append("partial")
        if len(tight) != 1:
            reason.append(f"ambig_n={len(tight)}/{len(loose)}")
        self._log(f"[REID] referee reason={'+'.join(reason) or 'fallback'}")

        best: CardTrack | None = None
        best_score = -1.0
        best_method = "reid"
        for dy, t in pool:
            score = max(0.0, 1.0 - dy / (GLOBAL_Y_MATCH_PX * 2.5)) * 0.35
            vis = None
            if crop is not None and t.reference_image is not None:
                vis = vm.match_crops(crop, t.reference_image)
                self._log(
                    f"[REID] lightglue score={vis.score:.3f} method={vis.method} "
                    f"inl={vis.n_inliers}/{vis.n_matches} track={t.track_id[:8]} "
                    f"dgy={dy:.0f}"
                )
                score += 0.55 * vis.score
            elif det.get("fp") and t.fp and det.get("fp") == t.fp:
                score += 0.45
            tid = det.get("title_id") or ""
            if tid and t.title_id and tid == t.title_id:
                score += 0.05  # OCR 仅加分
            if vis is not None and vis.score < VISUAL_MATCH_MIN and dy > GLOBAL_Y_TIGHT_PX:
                continue
            if score > best_score:
                best_score = score
                best = t
                best_method = vis.method if vis else "fp/pos"

        if best is None or best_score < 0.40:
            return None, ""
        return best, f"reid:{best_method}"

    def _spawn_track(
        self,
        det: dict,
        col: str,
        gy: float,
        gbar: float,
        crop: np.ndarray | None,
        poster_h: int,
    ) -> CardTrack:
        status = "FULL" if poster_h >= POSTER_H_FULL else "PARTIAL"
        card_uuid = None
        if status == "FULL":
            card_uuid = str(uuid.uuid4())
            status = "CONFIRMED"
            self._log(f"[NEW] card_uuid={card_uuid}")
        tid = str(uuid.uuid4())
        bbox = (
            int(det.get("roi_x") or det.get("bar_x") or 0),
            int(det.get("roi_y") or 0),
            int(det.get("roi_w") or det.get("bar_w") or 0),
            int(det.get("roi_h") or 0),
        )
        return CardTrack(
            track_id=tid,
            column=col,
            global_y=gy,
            global_bar_y=gbar,
            status=status,
            card_uuid=card_uuid,
            title=str(det.get("text") or ""),
            title_id=str(det.get("title_id") or ""),
            fp=str(det.get("fp") or det.get("key") or ""),
            poster_h=poster_h,
            screen_bbox=bbox,
            cx=float(det.get("cx") or 0),
            cy=float(det.get("cy") or 0),
            bar_x=int(det.get("bar_x") or 0),
            bar_y=int(det.get("bar_y") or 0),
            bar_w=int(det.get("bar_w") or 0),
            bar_h=int(det.get("bar_h") or 0),
            roi_x=int(det.get("roi_x") or 0),
            roi_y=int(det.get("roi_y") or 0),
            roi_w=int(det.get("roi_w") or 0),
            roi_h=int(det.get("roi_h") or 0),
            reference_image=crop,
            bbox_history=[(gy, bbox)],
            hit_count=1,
            method=str(det.get("method") or "track"),
            ink=float(det.get("ink") or 0),
        )

    def _update_track(
        self,
        t: CardTrack,
        det: dict,
        gy: float,
        gbar: float,
        crop: np.ndarray | None,
        poster_h: int,
    ) -> None:
        old_status = t.status
        t.hit_count += 1
        # EMA 更新 global 位置
        t.global_y = 0.7 * t.global_y + 0.3 * gy
        t.global_bar_y = 0.7 * t.global_bar_y + 0.3 * gbar
        t.screen_bbox = (
            int(det.get("roi_x") or det.get("bar_x") or t.screen_bbox[0]),
            int(det.get("roi_y") or t.screen_bbox[1]),
            int(det.get("roi_w") or det.get("bar_w") or t.screen_bbox[2]),
            int(det.get("roi_h") or t.screen_bbox[3]),
        )
        t.cx = float(det.get("cx") or t.cx)
        t.cy = float(det.get("cy") or t.cy)
        t.bar_x = int(det.get("bar_x") or t.bar_x)
        t.bar_y = int(det.get("bar_y") or t.bar_y)
        t.bar_w = int(det.get("bar_w") or t.bar_w)
        t.bar_h = int(det.get("bar_h") or t.bar_h)
        t.roi_x = int(det.get("roi_x") or t.roi_x)
        t.roi_y = int(det.get("roi_y") or t.roi_y)
        t.roi_w = int(det.get("roi_w") or t.roi_w)
        t.roi_h = int(det.get("roi_h") or t.roi_h)
        if det.get("fp"):
            t.fp = str(det.get("fp"))
        if det.get("title_id") and (
            not t.title_id or len(str(det.get("title_id"))) > len(t.title_id) + 1
        ):
            t.title_id = str(det.get("title_id"))
            t.title = str(det.get("text") or t.title)

        # 半截 → 完整：更新 reference，发 uuid
        if poster_h > t.poster_h + 8 and crop is not None:
            t.reference_image = crop
            t.poster_h = poster_h
        elif t.reference_image is None and crop is not None:
            t.reference_image = crop
        if poster_h > t.poster_h:
            t.poster_h = poster_h

        if t.status == "PARTIAL":
            t.status = "TRACKING"
        if poster_h >= POSTER_H_FULL:
            if t.card_uuid is None:
                t.card_uuid = str(uuid.uuid4())
                self._log(
                    f"[MERGE] partial → existing track={t.track_id[:8]} "
                    f"card_uuid={t.card_uuid} ph={poster_h}"
                )
            t.status = "CONFIRMED" if t.hit_count >= 1 else "FULL"
        elif t.status == "TRACKING" and t.hit_count >= 2:
            pass

        if old_status == "PARTIAL" and t.status in ("FULL", "CONFIRMED", "TRACKING"):
            self._log(
                f"[MERGE] partial → existing track={t.track_id[:8]} "
                f"status={old_status}→{t.status}"
            )

        t.bbox_history.append((gy, t.screen_bbox))
        if len(t.bbox_history) > 24:
            t.bbox_history = t.bbox_history[-24:]

    def confirmed_catalog(self) -> list[dict]:
        """正式目录：仅 CONFIRMED/FULL（已发 card_uuid）。"""
        out = []
        for t in sorted(self.tracks, key=lambda z: (z.global_bar_y, z.column)):
            if t.card_uuid and t.status in ("FULL", "CONFIRMED"):
                out.append(t.to_catalog_item())
        return out

    def all_tracks_debug(self) -> list[dict]:
        return [t.to_catalog_item() for t in self.tracks]

    def find_by_uuid(self, card_uuid: str) -> CardTrack | None:
        for t in self.tracks:
            if t.card_uuid == card_uuid:
                return t
        return None


def _bbox_iou(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    if aw <= 0 or ah <= 0 or bw <= 0 or bh <= 0:
        return 0.0
    ax2, ay2 = ax + aw, ay + ah
    bx2, by2 = bx + bw, by + bh
    ix0, iy0 = max(ax, bx), max(ay, by)
    ix1, iy1 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0, ix1 - ix0), max(0, iy1 - iy0)
    inter = iw * ih
    union = aw * ah + bw * bh - inter
    return float(inter / union) if union > 0 else 0.0
