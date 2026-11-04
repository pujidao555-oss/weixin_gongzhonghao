# -*- coding: utf-8 -*-
"""整卡 ROI 切割：轮廓优先，底栏第二。

第一优先（数量/位置）：
  页底(#191919)列向非页底游程 → 整卡 bbox
第二优先（标题）：
  关联 #2F2F30 底栏 → OCR 名称；无底栏也不丢卡

旧「先找底栏」路径仍在 extract_cards_bar_expanded / scan method=bar。
保持 DPI Aware、不移动微信窗；几何常量复用 scan_waterfall_catalog。
"""
from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np

TOOLS = Path(__file__).resolve().parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import scan_waterfall_catalog as scan

# 贴图页内容区底色（实测约 #191919）
PAGE_BG_RGB = (0x19, 0x19, 0x19)
PAGE_BG_TOL = 14
CONTENT_Y0 = 520
ROI_H_MIN = 90
ROI_H_MAX = 920
# 列内非页底占比阈值（游程起止）
COL_FILL_ON = 0.18
COL_FILL_TRIM = 0.28
# 底栏须落在 ROI 底部这一比例内才算「完整卡」
BAR_BOTTOM_FRAC = 0.45


def page_bg_mask(arr: np.ndarray) -> np.ndarray:
    tr, tg, tb = PAGE_BG_RGB
    tol = PAGE_BG_TOL
    lo = np.array([max(0, tr - tol), max(0, tg - tol), max(0, tb - tol)], np.uint8)
    hi = np.array([min(255, tr + tol), min(255, tg + tol), min(255, tb + tol)], np.uint8)
    return cv2.inRange(arr, lo, hi)


def non_bg_mask(arr: np.ndarray) -> np.ndarray:
    return cv2.bitwise_not(page_bg_mask(arr))


def find_column_rois(arr: np.ndarray) -> list[tuple[int, int, int, int, float]]:
    """双列各自：垂直非页底游程 → 候选整卡 ROI。

    返回 [(x, y, w, h, fill), ...]，x 用列轨道 COL_*_X0，宽 CARD_W_TYPICAL。
    """
    h_img = arr.shape[0]
    mask = non_bg_mask(arr)
    mask[:CONTENT_Y0, :] = 0
    out: list[tuple[int, int, int, int, float]] = []
    for col_x0 in (scan.COL_LEFT_X0, scan.COL_RIGHT_X0):
        # 略向左扩，覆盖实测底栏 x≈436（轨道 445）
        x_sample0 = max(0, col_x0 - 12)
        x_sample1 = min(arr.shape[1], col_x0 + scan.CARD_W_TYPICAL + 4)
        col = mask[CONTENT_Y0:h_img, x_sample0:x_sample1]
        if col.size == 0:
            continue
        row_fill = col.mean(axis=1) / 255.0
        active = row_fill > COL_FILL_ON
        i = 0
        n = len(active)
        while i < n:
            if not active[i]:
                i += 1
                continue
            j = i
            while j < n and active[j]:
                j += 1
            while i < j and row_fill[i] < COL_FILL_TRIM:
                i += 1
            while j > i and row_fill[j - 1] < COL_FILL_TRIM:
                j -= 1
            h = j - i
            if ROI_H_MIN <= h <= ROI_H_MAX:
                y = CONTENT_Y0 + i
                fill = float(row_fill[i:j].mean()) if j > i else 0.0
                out.append((col_x0, y, scan.CARD_W_TYPICAL, h, fill))
            i = max(j, i + 1)
    out.sort(key=lambda z: (z[1], z[0]))
    return out


def associate_bar_to_roi(
    roi: tuple[int, int, int, int],
    bars: list[tuple[int, int, int, int, float, float]],
) -> tuple[int, int, int, int, float, float] | None:
    """把全局 #2F2F30 底栏挂到整卡 ROI：底栏中心落在 ROI 下半且 x 同列。"""
    rx, ry, rw, rh = roi
    best = None
    best_score = -1.0
    for bar in bars:
        bx, by, bw, bh, fill, ink = bar
        bcy = by + bh / 2.0
        # 同列
        if (bx + bw / 2.0 < scan.COL_MID) != (rx + rw / 2.0 < scan.COL_MID):
            continue
        # 底栏须在 ROI 内，且偏下
        if by < ry - 8 or by + bh > ry + rh + 12:
            continue
        if bcy < ry + rh * (1.0 - BAR_BOTTOM_FRAC):
            continue
        # x 重叠
        overlap = min(rx + rw, bx + bw) - max(rx, bx)
        if overlap < min(rw, bw) * 0.55:
            continue
        # 越靠 ROI 底越好
        score = (bcy - ry) / max(1.0, float(rh)) + 0.1 * fill
        if score > best_score:
            best_score = score
            best = bar
    return best


def expand_bar_to_card_roi(
    arr: np.ndarray, bar: tuple[int, int, int, int, float, float]
) -> tuple[int, int, int, int]:
    """底栏锚点上探海报顶 → 整卡外接 ROI（与现网 estimate_poster_top 一致）。"""
    x, y, w, h, _fill, _ink = bar
    top = scan.estimate_poster_top(arr, x, y, w)
    return (x, top, w, (y + h) - top)


def roi_poster_fingerprint(
    arr: np.ndarray, x: int, y: int, w: int, h: int, bar_y: int | None = None
) -> str:
    """整卡 ROI 内海报区 aHash（默认去掉底部约 18% 标题栏，或裁到 bar_y）。"""
    if bar_y is not None and bar_y > y + 8:
        bot = bar_y
    else:
        bot = y + max(8, int(h * 0.82))
    top = max(0, y)
    left, right = max(0, x), min(arr.shape[1], x + w)
    bot = min(arr.shape[0], bot)
    crop = arr[top:bot, left:right]
    if crop.size == 0:
        return f"empty_{x}_{y}"
    gray = cv2.cvtColor(crop, cv2.COLOR_RGB2GRAY)
    small = cv2.resize(gray, (16, 16), interpolation=cv2.INTER_AREA)
    avg = float(small.mean())
    bits = (small[4:12, 4:12] >= avg).astype(np.uint8).flatten()
    val = 0
    for b in bits:
        val = (val << 1) | int(b)
    return f"{val:016x}"


def click_in_roi(
    x: int, y: int, w: int, h: int, bar_y: int | None = None
) -> tuple[float, float]:
    """点整卡 ROI 内海报中心（避开标题灰条）。"""
    bot = float(bar_y - 10) if bar_y is not None else float(y + h) * 0.82
    top = float(y) + 6.0
    if bot <= top + 12:
        top = float(y) + 4.0
        bot = float(y) + max(40.0, h * 0.5)
    return float(x) + float(w) / 2.0, (top + bot) / 2.0


def extract_cards_by_roi(
    arr: np.ndarray, ocr_titles: bool = True, require_bar: bool = False
) -> list[dict]:
    """整卡 ROI 主路径（轮廓优先）。

    优先级：
    1) 列向非页底游程 → 整卡轮廓 bbox（数量/位置，第一优先）
    2) 可选关联 #2F2F30 底栏 → 收紧几何 + 标题 OCR（第二优先）
    3) 无底栏也保留卡片（不因 fill/墨迹门槛丢卡）

    require_bar=True 为旧行为（底栏门禁）；默认 False。
    """
    bars = scan.find_candidate_2f2f30_boxes(arr)
    rois = find_column_rois(arr)
    caps: list[dict] = []
    used_bars: set[tuple[int, int, int, int]] = set()

    for rx, ry, rw, rh, fill in rois:
        bar = associate_bar_to_roi((rx, ry, rw, rh), bars)
        if bar is None and require_bar:
            continue
        if bar is not None:
            bx, by, bw, bh, bfill, bink = bar
            bkey = (bx, by, bw, bh)
            if bkey in used_bars:
                continue
            used_bars.add(bkey)
            # 用底栏真实 x/w 收紧 ROI（比列轨道更准）
            card_x, card_w = bx, bw
            card_y = ry
            # 若游程顶高于海报（半截上一张），以 bar 上探为准
            poster_top = scan.estimate_poster_top(arr, bx, by, bw)
            card_y = min(ry, poster_top)
            # 不要吃到上一张：poster_top 已停在灰条
            if poster_top > ry + 8:
                card_y = poster_top
            card_h = (by + bh) - card_y
            bar_y = by
            ink = bink
            bar_fill = bfill
            bar_h = bh
            method_tag = "card_roi_bar"
        else:
            # 轮廓已够：无底栏也算一张卡（标题可后补）
            card_x, card_y, card_w, card_h = rx, ry, rw, rh
            bar_y = None
            ink = 0.0
            bar_fill = float(fill)
            bar_h = 0
            bx = by = bw = bh = 0
            method_tag = "card_roi_contour"

        if card_h < ROI_H_MIN:
            continue

        fp = roi_poster_fingerprint(arr, card_x, card_y, card_w, card_h, bar_y=bar_y)
        click_cx, click_cy = click_in_roi(card_x, card_y, card_w, card_h, bar_y=bar_y)
        poster_h = (bar_y - card_y) if bar_y is not None else int(card_h * 0.82)
        short = poster_h > 0 and poster_h < scan.POSTER_H_SHORT

        title = ""
        title_id = ""
        avg_conf = 0.0
        raw: list[str] = []
        # 标题 OCR：第二优先；有底栏用底栏，无底栏则扫 ROI 底部条带
        if ocr_titles:
            if bar is not None:
                title, avg_conf, raw = scan.ocr_text_inside_box(arr, bx, by, bw, bh)
            else:
                band_h = min(64, max(28, card_h // 5))
                title, avg_conf, raw = scan.ocr_text_inside_box(
                    arr, card_x, card_y + card_h - band_h, card_w, band_h
                )
            if not scan.region_has_title_text(title):
                title = ""
            title_id = scan.normalize_title_id(title)

        text = title if title else f"贴图_{fp[:10]}"
        caps.append(
            {
                "text": text,
                "title_id": title_id,
                "key": fp,
                "fp": fp,
                "cx": click_cx,
                "cy": click_cy,
                "bar_x": bx if bar is not None else card_x,
                "bar_y": by if bar is not None else (card_y + card_h - 40),
                "bar_w": bw if bar is not None else card_w,
                "bar_h": bar_h,
                "roi_x": card_x,
                "roi_y": card_y,
                "roi_w": card_w,
                "roi_h": card_h,
                "poster_h": poster_h,
                "short": short,
                "fill": bar_fill if bar is not None else fill,
                "ink": ink,
                "conf": avg_conf,
                "method": (
                    f"{method_tag}_title"
                    if title_id
                    else (method_tag if method_tag == "card_roi_contour" else "card_roi_fp")
                ),
                "raw_ocr": raw,
                "has_bar": bar is not None,
            }
        )

    # 补漏：有底栏但未被任何 ROI 吃掉的 → 用底栏上探整卡（半屏/切缝情况）
    for bar in bars:
        bx, by, bw, bh, bfill, bink = bar
        bkey = (bx, by, bw, bh)
        if bkey in used_bars:
            continue
        card_x, card_y, card_w, card_h = expand_bar_to_card_roi(arr, bar)
        fp = roi_poster_fingerprint(arr, card_x, card_y, card_w, card_h, bar_y=by)
        click_cx, click_cy = click_in_roi(card_x, card_y, card_w, card_h, bar_y=by)
        poster_h = by - card_y
        short = poster_h > 0 and poster_h < scan.POSTER_H_SHORT
        title = ""
        title_id = ""
        avg_conf = 0.0
        raw: list[str] = []
        if ocr_titles:
            title, avg_conf, raw = scan.ocr_text_inside_box(arr, bx, by, bw, bh)
            if not scan.region_has_title_text(title):
                title = ""
            title_id = scan.normalize_title_id(title)
        text = title if title else f"贴图_{fp[:10]}"
        caps.append(
            {
                "text": text,
                "title_id": title_id,
                "key": fp,
                "fp": fp,
                "cx": click_cx,
                "cy": click_cy,
                "bar_x": bx,
                "bar_y": by,
                "bar_w": bw,
                "bar_h": bh,
                "roi_x": card_x,
                "roi_y": card_y,
                "roi_w": card_w,
                "roi_h": card_h,
                "poster_h": poster_h,
                "short": short,
                "fill": bfill,
                "ink": bink,
                "conf": avg_conf,
                "method": "card_roi_bar_orphan" if title_id else "card_roi_fp_orphan",
                "raw_ocr": raw,
            }
        )
        used_bars.add(bkey)

    caps.sort(key=lambda z: (float(z.get("roi_y") or 0), float(z.get("roi_x") or 0)))
    return caps


def extract_cards_bar_expanded(arr: np.ndarray, ocr_titles: bool = True) -> list[dict]:
    """对照：现有底栏方案，但显式产出整卡 ROI 字段（上探海报）。"""
    bars = scan.find_candidate_2f2f30_boxes(arr)
    caps: list[dict] = []
    for bi, bar in enumerate(bars, 1):
        x, y, w, h, fill, ink = bar
        card_x, card_y, card_w, card_h = expand_bar_to_card_roi(arr, bar)
        fp = scan.card_image_fingerprint(arr, x, y, w, h)
        poster_top = card_y
        click_cx, click_cy = scan.click_point_above_bar(x, y, w, h, poster_top=poster_top)
        poster_h = y - poster_top
        short = poster_h > 0 and poster_h < scan.POSTER_H_SHORT
        title = ""
        title_id = ""
        avg_conf = 0.0
        raw: list[str] = []
        if ocr_titles:
            title, avg_conf, raw = scan.ocr_text_inside_box(arr, x, y, w, h)
            if not scan.region_has_title_text(title):
                title = ""
            title_id = scan.normalize_title_id(title)
        text = title if title else f"贴图_{fp[:10]}"
        caps.append(
            {
                "text": text,
                "title_id": title_id,
                "key": fp,
                "fp": fp,
                "cx": click_cx,
                "cy": click_cy,
                "bar_x": x,
                "bar_y": y,
                "bar_w": w,
                "bar_h": h,
                "roi_x": card_x,
                "roi_y": card_y,
                "roi_w": card_w,
                "roi_h": card_h,
                "poster_h": poster_h,
                "short": short,
                "fill": fill,
                "ink": ink,
                "conf": avg_conf,
                "method": "bar_expand_title" if title_id else "bar_expand_fp",
                "raw_ocr": raw,
            }
        )
    return caps
