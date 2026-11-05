# -*- coding: utf-8 -*-
"""Count 贴图 waterfall cards: close detail if open -> safe scroll to bottom (OCR) -> top -> report."""
from __future__ import annotations

import ctypes
import json
import re
import time
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageGrab
from rapidocr_onnxruntime import RapidOCR

USER32 = ctypes.windll.user32
USER32.SetProcessDPIAware()

ROOT = Path(r"d:\cursor_project\wx_gzh")
OBS = ROOT / "observation_steps"
TOOLS = ROOT / "tools"
CATALOG = TOOLS / "waterfall_catalog.json"
PROGRESS = TOOLS / "waterfall_progress.txt"

# detail tab close X (right of …) — proven earlier
# 图片集合页（卡片详情）：只标定 …；关闭用相对偏移
DOTS_SCREEN = (777, 38)
CLOSE_OFFSET_FROM_DOTS = (40, 0)  # 关闭 = … + (40, 0)


def close_screen_from_dots(dots=None) -> tuple[int, int]:
    """关闭按钮 = … 坐标 + 相对偏移 CLOSE_OFFSET_FROM_DOTS。"""
    dx, dy = dots if dots is not None else DOTS_SCREEN
    ox, oy = CLOSE_OFFSET_FROM_DOTS
    return (int(dx + ox), int(dy + oy))


BACK_SCREEN = (40, 39)  # top-left back/chevron area inside WeChat

OCR = RapidOCR()

SKIP_EXACT = {
    "关注",
    "私信",
    "全部",
    "贴图",
    "展开",
    "阅读原文",
    "写留言",
    "分享",
    "问AI",
    "喜欢作者",
    "欢迎询价",
    "锻件定制",
    "定制",
    "锻件",
}
# 图内海报/侧栏广告常见字样（不是卡片下方标题）
SKIP_SUB = (
    "关注",
    "私信",
    "全部",
    "贴图",
    "展开",
    "山西恒泰",
    "忻州",
    "承揽",
    "压机",
    "HOT",
    "阅读原文",
    "写留言",
    "还没有留言",
    "分享",
    "喜欢作者",
    "更多相关",
    "留言",
    "阅读",
    "问AI",
    "欢迎询价",
    "源头厂家",
    "品质保证",
    "一件起订",
    "材质齐全",
    "优质锻件供应",
    "已无更多",
    "正在加载",
)
OVERLAY_MENU = {
    "齿轮锻件",
    "车轮锻件",
    "轴类锻件",
    "环形锻件",
    "异形锻件",
    "√齿轮锻件",
    "√车轮锻件",
    "√轴类锻件",
    "√环形锻件",
    "√异形锻件",
}
# 卡片标题底色（用户标定 #2F2F30），允许轻微色差
TITLE_BG_RGB = (0x2F, 0x2F, 0x30)
TITLE_BG_TOL = 8
# 标题文字色（底栏内浅灰近白；三张截图实测主体约 195–225，抗锯齿可到 ~175）
# 不含「赞 N」中灰（约 120–180）
TITLE_FG_L_MIN = 195          # 亮度下限（RGB 均值）
TITLE_FG_L_MAX = 235          # 亮度上限（避开纯白噪点也可放宽到 255）
TITLE_FG_CHANNEL_MIN = 175    # 单通道下限，过滤偏色噪点
TITLE_INK_MIN_RATIO = 0.010   # 底栏内标题墨迹占比下限（短卡单行也够）
TITLE_INK_MAX_RATIO = 0.42    # 短底栏文字占比更高，略放宽
LIKE_RE = re.compile(r"^赞\s*\d+$")
# 贴图两列瀑布流中缝（窗口内坐标）；左右「赞」与标题不得跨列合并
COL_MID = 700.0

# —— 双列几何（窗口内坐标，实测约左 x0=445 / 右 x0=745 / 单卡宽≈288）——
# 卡片宽度是硬门禁：单卡标题栏宽必须落在此区间
CARD_W_TYPICAL = 288
CARD_W_MIN = 260
CARD_W_MAX = 310
COL_LEFT_X0 = 445
COL_RIGHT_X0 = 745
COL_X_SLACK = 45  # 左右列左缘允许偏移
# 左卡左缘 → 右卡右缘 总跨度 ≈ 745+288-445 = 588
TWO_COL_SPAN_MIN = 560
TWO_COL_SPAN_MAX = 620
COL_GAP_MIN = 8   # 左卡右缘到右卡左缘
COL_GAP_MAX = 40

# —— 标题底栏 / 短卡（海报很矮）全局放宽 ——
BAR_H_MIN = 24            # 单行/裁切底栏可低于原 36
BAR_H_MAX = 170
BAR_AREA_MIN = 2800       # 原 5000 会丢掉矮底栏；约 260×24×0.45
BAR_FILL_MIN = 0.60       # 烘干机等底栏实测 fill≈0.64，0.65 会误杀
POSTER_H_MIN = 28         # 底栏上方海报最小高度（短卡仍须有图）
POSTER_H_SHORT = 160      # 低于此视为短卡，点击/指纹用更紧的上界
POSTER_H_ENROLL = 250     # 完整海报软门槛（烘干机等约 264；不再用 280 硬拒）
POSTER_LOOKBACK_MAX = 320 # 向上追海报的最大距离
# 增量跟踪：Scroll Registration + Card Track（默认开）
USE_CARD_TRACKER = True

# 向下扫描：固定节奏——1 秒滚 1 格（单次中键滚轮），避免连发跳帧漏卡
SCAN_FAST = True
SCAN_DOWN_NOTCHES = 1          # 每步只滚 1 格
SCAN_DOWN_NOTCHES_SAFE = 1     # --slow 同样单格
SCAN_DOWN_BACK_NOTCHES = 2
SCAN_DOWN_MICRO_NOTCHES = 1
SCAN_UP_NOTCHES = 14
SCAN_MAX_STEPS = 200           # 单格步进，步数上限放宽
SCAN_STAGNANT_LIMIT = 3   # 连续无新增达 3 次则判到底
SCAN_WHEEL_DELTA = 120         # 系统默认一格滚轮增量
SCAN_WHEEL_GAP_S = 0.0         # 单格无需连发间隔
SCAN_SETTLE_S = 1.0            # 1 秒一次：滚完停稳再截/再滚
SCAN_SAVE_EVERY = 3            # 每 N 步落盘一张调试图
SCAN_OCR_DURING = False        # 滚动中不做标题 OCR；结束后可补
SCAN_PARALLEL = True           # 识别与滚动流水线并行（步长已收小，不易漏）
# 贴窗底的半截底栏：OCR 会乱，且同 fp 会挡住后续完整标题
BAR_PARTIAL_EDGE_PX = 8
BAR_MIN_H_NEAR_BOTTOM = 70
BAR_NEAR_BOTTOM_BAND = 220


def load_geom():
    return json.loads((TOOLS / "wx_geom.json").read_text(encoding="utf-8-sig"))


SW_RESTORE = 9


def _window_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long),
            ("top", ctypes.c_long),
            ("right", ctypes.c_long),
            ("bottom", ctypes.c_long),
        ]

    r = RECT()
    if not USER32.GetWindowRect(int(hwnd), ctypes.byref(r)):
        return None
    return int(r.left), int(r.top), int(r.right), int(r.bottom)


def _rect_is_offscreen_minimized(rect: tuple[int, int, int, int] | None) -> bool:
    """Windows 最小化窗常报到约 (-32000,-32000)；半屏停靠不会是这个值。"""
    if not rect:
        return True
    l, t, r, b = rect
    return l <= -10000 or t <= -10000 or (r - l) < 200 or (b - t) < 200


def _ensure_window_restored(hwnd: int) -> tuple[int, int, int, int] | None:
    """若最小化则还原，再取矩形。"""
    hwnd = int(hwnd)
    if USER32.IsIconic(hwnd):
        write_progress(f"检测到微信窗最小化 H={hwnd}，尝试还原…")
        USER32.ShowWindow(hwnd, SW_RESTORE)
        time.sleep(0.35)
    USER32.SetForegroundWindow(hwnd)
    time.sleep(0.2)
    return _window_rect(hwnd)


def find_wechat_hwnd(prefer_geom: dict | None = None) -> int | None:
    """按标题「微信」找**未最小化**的主窗；本机可能同时存在多个微信 HWND。"""
    from ctypes import wintypes

    EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    candidates: list[tuple[int, int, int, int, int, int]] = []

    def _cb(hwnd, _lp):
        hwnd = int(hwnd)
        buf = (ctypes.c_wchar * 512)()
        USER32.GetWindowTextW(hwnd, buf, 512)
        title = buf.value or ""
        if "微信" not in title and "Weixin" not in title and "WeChat" not in title:
            return True
        # 跳过最小化：否则会误锁到 -32000 幽灵窗，而用户半屏看的是另一个 HWND
        if USER32.IsIconic(hwnd):
            return True
        if not USER32.IsWindowVisible(hwnd):
            return True
        rect = _window_rect(hwnd)
        if _rect_is_offscreen_minimized(rect):
            return True
        assert rect is not None
        l, t, r, b = rect
        w, h = r - l, b - t
        if w < 600 or h < 500:
            return True
        candidates.append((hwnd, l, t, r, b, w * h))
        return True

    USER32.EnumWindows(EnumWindowsProc(_cb), 0)
    if not candidates:
        return None
    if prefer_geom:
        pl = int(prefer_geom.get("L", 0))
        pt = int(prefer_geom.get("T", 0))
        pr = int(prefer_geom.get("R", 0))
        pb = int(prefer_geom.get("B", 0))
        # 旧 geom 若已是 -32000，不要用它做亲近排序
        if pl > -10000 and pt > -10000:

            def score(item):
                _h, l, t, r, b, _area = item
                return abs(l - pl) + abs(t - pt) + abs(r - pr) + abs(b - pb)

            candidates.sort(key=score)
        else:
            candidates.sort(key=lambda z: -z[5])
    else:
        candidates.sort(key=lambda z: -z[5])
    return candidates[0][0]


def refresh_geom(geom: dict | None = None, *, save: bool = True) -> dict:
    """校验/刷新 wx_geom.json 的 H；句柄失效或最小化则重找可见微信窗并写回。"""
    g = dict(geom or load_geom())
    hwnd = int(g.get("H") or 0)
    alive = bool(hwnd) and bool(USER32.IsWindow(hwnd))
    if alive:
        rect = _ensure_window_restored(hwnd)
        if rect and not _rect_is_offscreen_minimized(rect):
            l, t, r, b = rect
            if (l, t, r, b) != (
                int(g.get("L", 0)),
                int(g.get("T", 0)),
                int(g.get("R", 0)),
                int(g.get("B", 0)),
            ):
                write_progress(
                    f"窗口矩形已变：{(g.get('L'), g.get('T'), g.get('R'), g.get('B'))} → {(l, t, r, b)}"
                )
                g.update({"L": l, "T": t, "R": r, "B": b, "H": hwnd})
                if save:
                    (TOOLS / "wx_geom.json").write_text(
                        json.dumps(g, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
            return g
        write_progress(
            f"当前句柄不可用或仍最小化 H={hwnd} rect={rect}，改找可见微信主窗…"
        )
    else:
        write_progress(f"窗口句柄失效 H={hwnd}，正在重找微信主窗…")
    new_h = find_wechat_hwnd(g)
    if not new_h:
        raise RuntimeError(
            "未找到可见的微信窗口（已排除最小化）。"
            "请把微信半屏/还原显示在桌面上，并打开公众号贴图页。"
        )
    rect = _ensure_window_restored(new_h)
    if _rect_is_offscreen_minimized(rect):
        raise RuntimeError(
            f"找到微信窗 H={new_h} 但仍像最小化 rect={rect}，请手动点一下微信窗口后再试"
        )
    assert rect is not None
    l, t, r, b = rect
    g.update({"L": l, "T": t, "R": r, "B": b, "H": int(new_h)})
    write_progress(f"已刷新微信窗 H={new_h} rect=({l},{t},{r},{b})")
    if save:
        (TOOLS / "wx_geom.json").write_text(
            json.dumps(g, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return g


def focus(hwnd):
    hwnd = int(hwnd)
    if USER32.IsIconic(hwnd):
        USER32.ShowWindow(hwnd, SW_RESTORE)
        time.sleep(0.25)
    USER32.SetForegroundWindow(hwnd)
    time.sleep(0.25)


def click(x, y):
    USER32.SetCursorPos(int(x), int(y))
    time.sleep(0.05)
    USER32.mouse_event(0x0002, 0, 0, 0, 0)
    time.sleep(0.03)
    USER32.mouse_event(0x0004, 0, 0, 0, 0)


def grab(geom, name):
    img = ImageGrab.grab(bbox=(geom["L"], geom["T"], geom["R"], geom["B"]))
    path = OBS / name
    img.save(path)
    return path


def grab_arr(geom, name: str | None = None, save: bool = True) -> tuple[np.ndarray, Path | None]:
    """截屏为 RGB 数组；快扫时可跳过落盘以省 I/O。"""
    img = ImageGrab.grab(bbox=(geom["L"], geom["T"], geom["R"], geom["B"]))
    arr = np.array(img.convert("RGB"))
    path = None
    if save and name:
        path = OBS / name
        img.save(path)
    return arr, path


def write_progress(msg: str):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    prev = PROGRESS.read_text(encoding="utf-8") if PROGRESS.exists() else ""
    PROGRESS.write_text(prev + line + "\n", encoding="utf-8")


def ocr_all(img_path: Path):
    arr = np.array(Image.open(img_path))
    result, _ = OCR(arr)
    rows = []
    for box, text, conf in result or []:
        t = text.strip()
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        x0, x1 = min(xs), max(xs)
        y0, y1 = min(ys), max(ys)
        rows.append(
            {
                "text": t,
                "cx": sum(xs) / len(xs),
                "cy": sum(ys) / len(ys),
                "x0": x0,
                "x1": x1,
                "y0": y0,
                "y1": y1,
                "w": x1 - x0,
                "h": y1 - y0,
                "conf": float(conf),
            }
        )
    return rows


def is_detail_page(rows) -> bool:
    texts = [r["text"] for r in rows]
    joined = "".join(texts)
    markers = ("喜欢作者", "写留言", "阅读原文", "更多相关贴图", "问AI")
    hits = sum(1 for m in markers if m in joined)
    # slideshow page also often shows numbered thumbs as separate chrome
    if hits >= 2:
        return True
    if "喜欢作者" in joined or ("写留言" in joined and "阅读" in joined):
        return True
    return False


def is_tietu_list(rows) -> bool:
    """是否公众号资料/贴图页。

    主信号：关注/已关注。
    辅信号（满足其一即可）：私信、赞/赞N、或页内多条卡片底栏式短标题。
    不要求「贴图」「全部」；部分号顶栏无「私信」或 OCR 漏读时仍放行。
    """
    texts = [r["text"].strip() for r in rows if r.get("text")]

    def _has_exact_or_prefix(*keys: str) -> bool:
        for t in texts:
            for k in keys:
                if t == k or t.startswith(k):
                    return True
        return False

    # 先匹配「已关注」，再「关注」，避免误伤其它含「关注」的长句时仍以按钮文案为准
    has_follow = _has_exact_or_prefix("已关注", "关注")
    if not has_follow:
        return False
    has_msg = _has_exact_or_prefix("私信")
    has_like = _has_exact_or_prefix("赞")
    # 瀑布卡底栏常见：短中文标题（排除顶栏账号名等单字噪声）
    cardish = [
        t
        for t in texts
        if 4 <= len(t) <= 40 and t not in ("已关注", "关注", "私信", "贴图", "全部")
    ]
    has_cards = len(cardish) >= 3
    return has_msg or has_like or has_cards


def _is_overlay_text(r: dict) -> bool:
    t = r["text"]
    if t in SKIP_EXACT or t in OVERLAY_MENU:
        return True
    if any(s in t for s in SKIP_SUB):
        return True
    if t.startswith("√") or t.startswith("✓"):
        return True
    # 侧栏小字：矮、窄
    if r.get("h", 99) <= 21 and r.get("w", 999) <= 80:
        return True
    return False


def _is_caption_candidate(r: dict) -> bool:
    t = r["text"]
    cn = len(re.findall(r"[\u4e00-\u9fff]", t))
    if r["conf"] < 0.7 or cn < 2 or len(t) > 36:
        return False
    if LIKE_RE.match(t) or LIKE_RE.match(t.replace(" ", "")):
        return False
    if _is_overlay_text(r):
        return False
    if re.fullmatch(r"[A-Za-z0-9_\-！!？?，,。.#\s]+", t):
        return False
    return True


def _same_card_column(like: dict, r: dict) -> bool:
    """同一「赞」只收同列标题：中缝分流 + x 重叠/左缘对齐。

    旧逻辑 |cx-like.cx|<=200 会在左右赞贴近时把左列标题并进右列
    （如 烘干机干燥机回转窑锻件 + 主轴锻件）。
    """
    if (like["cx"] < COL_MID) != (r["cx"] < COL_MID):
        return False
    overlaps_like = (r["x0"] - 15) <= like["cx"] <= (r["x1"] + 15)
    near_card_left = abs(r["x0"] - like["x0"]) <= 45
    if overlaps_like or near_card_left:
        return True
    # 同列兜底：比旧阈值 200 更紧
    return abs(r["cx"] - like["cx"]) <= 130


def ocr_captions_by_like(rows) -> list[dict]:
    """以「赞 N」为锚，向上合并同列多行标题；丢弃图内/侧栏广告字。"""
    likes = [
        r
        for r in rows
        if LIKE_RE.match(r["text"].replace(" ", "")) or LIKE_RE.match(r["text"])
    ]
    if not likes:
        likes = [r for r in rows if re.match(r"^赞\s*\d+$", r["text"])]

    caps: list[dict] = []
    used_ids: set[int] = set()
    for like in sorted(likes, key=lambda z: (z["cy"], z["cx"])):
        candidates = []
        for i, r in enumerate(rows):
            if i in used_ids:
                continue
            if not _is_caption_candidate(r):
                continue
            dy = like["cy"] - r["cy"]
            if dy < 8 or dy > 95:
                continue
            if not _same_card_column(like, r):
                continue
            candidates.append((i, r))
        if not candidates:
            continue
        # 多行同卡标题按 y 拼接（车轮锻件|精工锻造 + 重载工况）
        candidates.sort(key=lambda z: z[1]["cy"])
        parts = []
        cx_sum = 0.0
        cy_sum = 0.0
        for i, r in candidates:
            used_ids.add(i)
            parts.append(r["text"])
            cx_sum += r["cx"]
            cy_sum += r["cy"]
        title = re.sub(r"\s+", "", "".join(parts))
        if len(re.findall(r"[\u4e00-\u9fff]", title)) < 3:
            continue
        if any(s in title for s in SKIP_SUB):
            continue
        caps.append(
            {
                "text": title,
                "cx": cx_sum / len(candidates),
                "cy": cy_sum / len(candidates),
                "conf": float(like["conf"]),
                "like": like["text"],
                "method": "like_anchor",
            }
        )

    seen: set[str] = set()
    uniq: list[dict] = []
    for c in sorted(caps, key=lambda z: (z["cy"], z["cx"])):
        k = re.sub(r"\s+", "", c["text"])
        if k in seen:
            continue
        seen.add(k)
        uniq.append(c)
    return uniq


def color_mask_2f2f30(arr: np.ndarray) -> np.ndarray:
    """像素级：是否接近标题底色 #2F2F30。"""
    tr, tg, tb = TITLE_BG_RGB
    tol = TITLE_BG_TOL
    lo = np.array([max(0, tr - tol), max(0, tg - tol), max(0, tb - tol)], np.uint8)
    hi = np.array([min(255, tr + tol), min(255, tg + tol), min(255, tb + tol)], np.uint8)
    return cv2.inRange(arr, lo, hi)


def title_ink_mask(arr: np.ndarray) -> np.ndarray:
    """底栏内「标题字」浅灰近白墨迹掩码（不含赞数中灰）。

    实测标题主体 RGB ≈ (195–225, 210–225, 195–229)，亮度 L∈[195,235]。
    """
    if arr.size == 0:
        return np.zeros(arr.shape[:2], np.uint8)
    rgb = arr.astype(np.int16)
    lum = rgb.mean(axis=2)
    ch_ok = (
        (rgb[:, :, 0] >= TITLE_FG_CHANNEL_MIN)
        & (rgb[:, :, 1] >= TITLE_FG_CHANNEL_MIN)
        & (rgb[:, :, 2] >= TITLE_FG_CHANNEL_MIN)
    )
    ok = (lum >= TITLE_FG_L_MIN) & (lum <= TITLE_FG_L_MAX) & ch_ok
    return ok.astype(np.uint8) * 255


def bar_title_ink_ratio(arr: np.ndarray, x: int, y: int, w: int, h: int) -> float:
    """标题底栏内标题色像素占比。"""
    crop = arr[y : y + h, x : x + w]
    if crop.size == 0:
        return 0.0
    ink = title_ink_mask(crop)
    return float(np.count_nonzero(ink)) / float(ink.size)


def normalize_title_id(title: str) -> str:
    """标题作卡片识别码：去空白与常见标点，保留中英数。"""
    t = re.sub(r"\s+", "", title or "")
    t = re.sub(r"[^\w\u4e00-\u9fff]", "", t)
    return t


def is_solid_2f2f30_region(
    arr: np.ndarray, x: int, y: int, w: int, h: int, min_fill: float | None = None
) -> tuple[bool, float]:
    """判定外接矩形是否「几乎全部」是 #2F2F30。

    文字笔画会占少量非底色像素；短底栏默认 min_fill 更低。
    """
    if min_fill is None:
        min_fill = BAR_FILL_MIN
    crop = arr[y : y + h, x : x + w]
    if crop.size == 0:
        return False, 0.0
    mask = color_mask_2f2f30(crop)
    fill = float(np.count_nonzero(mask)) / float(mask.size)
    return fill >= min_fill, fill


def is_partial_title_bar(y: int, h: int, h_img: int) -> bool:
    """贴窗底/过矮的半截标题栏：此时 OCR 易乱序截断，不能入库。"""
    if y + h >= h_img - BAR_PARTIAL_EDGE_PX:
        return True
    if h < BAR_MIN_H_NEAR_BOTTOM and y >= h_img - BAR_NEAR_BOTTOM_BAND:
        return True
    if h < 40:
        return True
    return False


def column_poster_cut_at_bottom(arr: np.ndarray, col_x0: int) -> bool:
    """该列窗口底部仍是海报内容、没有完整标题栏 → 高卡标题还没滚进来。"""
    h_img, _w_img = arr.shape[:2]
    x = col_x0
    w = CARD_W_TYPICAL
    if x + w > arr.shape[1]:
        return False
    band = arr[max(0, h_img - 100) : h_img - 4, x : x + w]
    if band.size == 0:
        return False
    fill = float(np.count_nonzero(color_mask_2f2f30(band))) / float(band.size)
    # 几乎没有底栏色，但也不全黑 → 海报被裁切
    return fill < 0.12 and float(band.mean()) > 38.0


def estimate_poster_top(arr: np.ndarray, x: int, y: int, w: int) -> int:
    """从标题底栏上沿向上追海报顶（遇上一张底栏/#2F2F30 带或过暗页底则停）。

    短卡全局关键：不能固定上探 220px，否则会撞到同列上一张底栏，被误判为「上方全是灰条」。
    """
    y_min = max(400, y - POSTER_LOOKBACK_MAX)
    poster_top = y
    for yy in range(y - 4, y_min, -4):
        y1 = min(y, yy + 4)
        strip = arr[yy:y1, x : x + w]
        if strip.size == 0:
            break
        fill = float(np.count_nonzero(color_mask_2f2f30(strip))) / float(strip.size)
        if fill > 0.55:
            # 撞到上一张标题底栏
            break
        # 大片近黑页底（非海报内容）也停
        if float(strip.mean()) < 28.0 and fill < 0.08:
            break
        poster_top = yy
    return int(poster_top)


def _in_left_column(x: int, w: int) -> bool:
    return abs(x - COL_LEFT_X0) <= COL_X_SLACK


def _in_right_column(x: int, w: int) -> bool:
    return abs(x - COL_RIGHT_X0) <= COL_X_SLACK


def _pair_span_ok(left: tuple, right: tuple) -> bool:
    """同行左右卡：总跨度、列间距落在双列固定几何内（宽度硬门禁再校验一次）。"""
    lx, _ly, lw = left[0], left[1], left[2]
    rx, _ry, rw = right[0], right[1], right[2]
    span = (rx + rw) - lx
    gap = rx - (lx + lw)
    return (
        TWO_COL_SPAN_MIN <= span <= TWO_COL_SPAN_MAX
        and COL_GAP_MIN <= gap <= COL_GAP_MAX
        and CARD_W_MIN <= lw <= CARD_W_MAX
        and CARD_W_MIN <= rw <= CARD_W_MAX
    )


def find_candidate_2f2f30_boxes(arr: np.ndarray) -> list[tuple[int, int, int, int, float, float]]:
    """CV 找连通的 #2F2F30 色块，并校验宽度 + 标题墨迹色 + 短卡海报。

    返回 [(x, y, w, h, fill_ratio, ink_ratio), ...]

    硬约束:
    - 底色 ≈ #2F2F30 且填充比足够（短底栏略放宽）
    - 单卡宽度 CARD_W_MIN..CARD_W_MAX（典型 288）
    - 底栏高 BAR_H_MIN..MAX（含很矮的单行/裁切底栏）
    - 底栏内须有标题色墨迹
    - 自适应上探海报高度：短卡不会因撞到上一张底栏被误杀
    - 左/右列轨道；同行成对时再校验双列跨度与列间距
    """
    mask = color_mask_2f2f30(arr)
    # 轻微闭运算：把底栏里被白字打断的底色连成一块
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (11, 5)))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (5, 3)))
    n, _labels, stats, _cent = cv2.connectedComponentsWithStats(mask, connectivity=8)

    raw: list[tuple[int, int, int, int, float, float]] = []
    h_img, w_img = arr.shape[:2]
    for i in range(1, n):
        x, y, w, h, area = (int(v) for v in stats[i])
        if area < BAR_AREA_MIN:
            continue
        # 卡片宽度硬门禁（双列总宽固定 ⇒ 单列宽度也有界）
        if w < CARD_W_MIN or w > CARD_W_MAX:
            continue
        if h < BAR_H_MIN or h > BAR_H_MAX:
            continue
        if y < 520 or y > h_img - 4 or x < 200 or x + w > w_img - 40:
            continue
        # 半截底栏：跳过（等滚完整再认），避免错标题占 fp
        if is_partial_title_bar(y, h, h_img):
            continue
        # 必须落在左列或右列轨道
        if not (_in_left_column(x, w) or _in_right_column(x, w)):
            continue
        ok, fill = is_solid_2f2f30_region(arr, x, y, w, h)
        if not ok:
            continue
        ink = bar_title_ink_ratio(arr, x, y, w, h)
        if ink < TITLE_INK_MIN_RATIO or ink > TITLE_INK_MAX_RATIO:
            continue
        # 全局短卡：自适应海报高度，禁止固定 220px 上探误撞上一张底栏
        poster_top = estimate_poster_top(arr, x, y, w)
        poster_h = y - poster_top
        if poster_h < POSTER_H_MIN:
            continue
        raw.append((x, y, w, h, fill, ink))

    raw.sort(key=lambda b: (b[1], b[0]))

    # 同行左右成对时，用双列总跨度再筛一道；不成对的单卡（半屏）仍保留
    out: list[tuple[int, int, int, int, float, float]] = []
    used = set()
    for i, a in enumerate(raw):
        if i in used:
            continue
        ax, ay, aw, ah, af, _ai = a
        paired = False
        for j, b in enumerate(raw):
            if j <= i or j in used:
                continue
            bx, by, bw, bh, bf, _bi = b
            if abs(by - ay) > 50:
                continue
            left, right = (a, b) if ax < bx else (b, a)
            if _in_left_column(left[0], left[2]) and _in_right_column(right[0], right[2]):
                if _pair_span_ok(left, right):
                    out.append(left)
                    out.append(right)
                    used.add(i)
                    used.add(j)
                    paired = True
                    break
                # 同行但跨度不对 → 两张都丢，避免歪卡片
                used.add(i)
                used.add(j)
                paired = True
                write_progress(
                    f"  丢弃跨度异常的左右对: L=({left[0]},{left[2]}) R=({right[0]},{right[2]}) "
                    f"span={(right[0]+right[2])-left[0]} gap={right[0]-(left[0]+left[2])}"
                )
                break
        if not paired and i not in used:
            # 半屏只露出一列：宽度+列轨道已合格则保留
            out.append(a)
            used.add(i)

    out.sort(key=lambda b: (b[1], b[0]))
    return out


def ocr_text_inside_box(arr: np.ndarray, x: int, y: int, w: int, h: int) -> tuple[str, float, list[str]]:
    """只在给定矩形内 OCR。返回 (标题, 平均conf, 原始行列表)。

    - 直接用原图底栏（不再强行墨迹二值化，避免把「电动车」拆成孤立「电」）
    - 阅读序：先按行(y)再按列(x)
    - 「赞 N」丢掉；过短碎片并入邻近片段
    """
    crop = arr[y : y + h, x : x + w]
    result, _ = OCR(crop)
    parts: list[tuple[float, float, str, float]] = []  # cy, cx, text, conf
    raw_lines: list[str] = []
    for box, text, conf in result or []:
        t = text.strip()
        if not t:
            continue
        raw_lines.append(t)
        if LIKE_RE.match(t) or LIKE_RE.match(t.replace(" ", "")):
            continue
        if t in SKIP_EXACT or t in OVERLAY_MENU:
            continue
        if any(s in t for s in SKIP_SUB):
            continue
        if float(conf) < 0.50:
            continue
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        cy = sum(ys) / len(ys)
        cx = sum(xs) / len(xs)
        parts.append((cy, cx, t, float(conf)))
    # 阅读序：行优先，同行按 x
    parts.sort(key=lambda z: (int(z[0] // 12), z[1]))
    # 单字碎片：保留但靠坐标嵌入，避免「电」+「动车」颠倒成「电动车」丢字或前置
    merged: list[str] = []
    confs: list[float] = []
    for _cy, _cx, t, conf in parts:
        if len(t) == 1 and merged:
            # 单字接到上一段末尾（常见于行首被拆）
            merged[-1] = merged[-1] + t
            confs[-1] = (confs[-1] + conf) / 2.0
        else:
            merged.append(t)
            confs.append(conf)
    title = re.sub(r"\s+", "", "".join(merged))
    avg_conf = sum(confs) / max(1, len(confs))
    return title, avg_conf, raw_lines


def region_has_title_text(title: str) -> bool:
    """区域内是否有可作为标题的文字。"""
    cn = len(re.findall(r"[\u4e00-\u9fff#A-Za-z0-9]", title))
    return cn >= 4 and len(title) >= 4


def title_quality(title: str) -> int:
    """标题完整度粗分：越长越完整；过短/残缺降权。"""
    tid = normalize_title_id(title)
    if not tid:
        return 0
    score = len(tid)
    if tid.endswith(("的", "，", ",", "：", ":")) and len(tid) < 12:
        score -= 6
    return score


def titles_likely_same(a: str, b: str) -> bool:
    """两个 OCR 标题是否像同一张卡（包含 / 公共子串 / 前缀）。"""
    aa, bb = normalize_title_id(a), normalize_title_id(b)
    if not aa or not bb:
        return False
    if aa == bb or aa in bb or bb in aa:
        return True
    n = min(len(aa), len(bb), 10)
    if n >= 6 and aa[:n] == bb[:n]:
        return True
    # 残缺标题常丢行首字：「年度连续三年…」vs「连续三年获平台认可…」
    short, long = (aa, bb) if len(aa) <= len(bb) else (bb, aa)
    if len(short) >= 6 and short in long:
        return True
    if len(short) >= 8:
        for i in range(0, len(short) - 7):
            if short[i : i + 8] in long:
                return True
    return False


def card_image_fingerprint(arr: np.ndarray, x: int, y: int, w: int, h_bar: int) -> str:
    """对标题栏上方的卡片图做 aHash（短卡只取真实海报区，避免吃到上一张）。"""
    poster_top = estimate_poster_top(arr, x, y, w)
    top = max(350, poster_top)
    bot = max(top + 8, y)
    left, right = x, min(arr.shape[1], x + w)
    crop = arr[top:bot, left:right]
    if crop.size == 0:
        return f"empty_{x}_{y}"
    gray = cv2.cvtColor(crop, cv2.COLOR_RGB2GRAY)
    small = cv2.resize(gray, (16, 16), interpolation=cv2.INTER_AREA)
    avg = float(small.mean())
    bits64 = (small[4:12, 4:12] >= avg).astype(np.uint8).flatten()
    val = 0
    for b in bits64:
        val = (val << 1) | int(b)
    return f"{val:016x}"


def click_point_above_bar(
    x: int, y: int, w: int, h_bar: int = 80, poster_top: int | None = None
) -> tuple[float, float]:
    """点标题栏上方的海报图中心（可点击进入详情），绝不能点标题灰条本身。

    短卡：用 estimate_poster_top 得到的真实海报顶，避免点到上一张卡。
    """
    if poster_top is None:
        # 无图时用保守短上界，避免点太高
        img_top = max(430.0, float(y) - float(POSTER_H_SHORT))
    else:
        img_top = float(max(430, poster_top + 4))
    img_bot = float(y) - 10.0
    if img_bot <= img_top + 12:
        img_top = max(430.0, float(y) - 60.0)
        img_bot = float(y) - 6.0
    cx = float(x) + float(w) / 2.0
    cy = (img_top + img_bot) / 2.0
    return cx, cy


def extract_cards_fast(
    img_path: Path | np.ndarray,
    ocr_titles: bool = True,
    method: str = "card_roi",
) -> list[dict]:
    """卡片识别（默认开标题 OCR 作识别码）:

    method:
      - ``card_roi``（默认）: **轮廓优先**（列向游程 bbox=数量/位置）→ 可选关联底栏 OCR
      - ``bar``: 旧路径，先找 #2F2F30 底栏再上探

    去重主键仍是视觉/Track（fp / card_uuid），不是标题。
    ocr_titles=False 时跳过名称 OCR，更快。
    img_path 也可直接传 RGB ndarray（快扫免反复读盘）。
    """
    if isinstance(img_path, np.ndarray):
        arr = img_path
    else:
        arr = np.array(Image.open(img_path).convert("RGB"))
    if method == "card_roi":
        import card_roi as _card_roi

        # 轮廓优先：无底栏也保留卡（数量/位置）；底栏仅服务标题 OCR
        caps = _card_roi.extract_cards_by_roi(
            arr, ocr_titles=ocr_titles, require_bar=False
        )
        write_progress(
            f"CV 整卡ROI: {len(caps)} 张 | 宽∈[{CARD_W_MIN},{CARD_W_MAX}] "
            f"| ocr_titles={ocr_titles}"
        )
        for bi, c in enumerate(caps, 1):
            write_progress(
                f"  卡{bi} roi=({c.get('roi_x')},{c.get('roi_y')},{c.get('roi_w')},{c.get('roi_h')}) "
                f"bar=({c.get('bar_x')},{c.get('bar_y')},{c.get('bar_w')},{c.get('bar_h')}) "
                f"poster_h={c.get('poster_h')}{'短' if c.get('short') else ''} "
                f"fp={str(c.get('fp',''))[:8]} id=「{c.get('title_id') or '-'}」"
            )
        write_progress(f"CV 整卡ROI本屏卡片: {len(caps)}")
        return caps

    boxes = find_candidate_2f2f30_boxes(arr)
    write_progress(
        f"CV 快扫: #2F2F30 底栏 {len(boxes)} 块 | 宽∈[{CARD_W_MIN},{CARD_W_MAX}] "
        f"| 标题色墨迹 | ocr_titles={ocr_titles}"
    )
    caps: list[dict] = []
    for bi, box in enumerate(boxes, 1):
        x, y, w, h, fill, ink = box
        poster_top = estimate_poster_top(arr, x, y, w)
        poster_h = max(0, y - poster_top)
        short = poster_h > 0 and poster_h < POSTER_H_SHORT
        fp = card_image_fingerprint(arr, x, y, w, h)
        click_cx, click_cy = click_point_above_bar(x, y, w, h, poster_top=poster_top)
        title = ""
        title_id = ""
        avg_conf = 0.0
        raw: list[str] = []
        if ocr_titles:
            title, avg_conf, raw = ocr_text_inside_box(arr, x, y, w, h)
            if not region_has_title_text(title):
                title = ""
            title_id = normalize_title_id(title)
            write_progress(
                f"  卡{bi} box=({x},{y},{w},{h}) fill={fill:.1%} ink={ink:.2%} "
                f"poster_h={poster_h}{'短' if short else ''} "
                f"fp={fp[:8]} id=「{title_id or '-'}」 raw={raw}"
            )
        else:
            write_progress(
                f"  卡{bi} box=({x},{y},{w},{h}) fill={fill:.1%} ink={ink:.2%} "
                f"poster_h={poster_h}{'短' if short else ''} "
                f"fp={fp[:8]} click=({click_cx:.0f},{click_cy:.0f})"
            )
        # 唯一键始终用海报指纹；同名卡片互不影响
        key = fp
        text = title if title else f"贴图_{fp[:10]}"
        caps.append(
            {
                "text": text,
                "title_id": title_id,
                "key": key,
                "fp": fp,
                "cx": click_cx,
                "cy": click_cy,
                "bar_x": x,
                "bar_y": y,
                "bar_w": w,
                "bar_h": h,
                "poster_h": poster_h,
                "short": short,
                "fill": fill,
                "ink": ink,
                "conf": avg_conf,
                "method": "cv_bar_w_ink_title" if title_id else "cv_bar_w_ink_fp",
            }
        )
    write_progress(f"CV 快扫本屏卡片: {len(caps)}")
    return caps


def extract_titles_solid_color_with_text(img_path: Path) -> list[dict]:
    """兼容旧接口：强制 OCR 标题。"""
    return extract_cards_fast(img_path, ocr_titles=True)


def ocr_captions(
    img_path: Path | np.ndarray,
    ocr_titles: bool = True,
    method: str = "card_roi",
) -> list[dict]:
    """对外入口：默认整卡 ROI + 底栏关联 + 指纹；method=bar 走旧底栏路径。"""
    return extract_cards_fast(img_path, ocr_titles=ocr_titles, method=method)


def close_detail(hwnd):
    focus(hwnd)
    click(*close_screen_from_dots())
    time.sleep(0.8)


def ensure_tietu_list(geom, hwnd, tries: int = 6) -> bool:
    for i in range(tries):
        focus(hwnd)
        time.sleep(0.35)
        path = grab(geom, f"scan-ensure-{i}.png")
        rows = ocr_all(path)
        sample = [r["text"] for r in rows[:25]]
        write_progress(
            f"ensure[{i+1}/{tries}] OCR样例={sample} | "
            f"资料页={is_tietu_list(rows)} 详情={is_detail_page(rows)}"
        )
        if is_detail_page(rows):
            write_progress(f"检测到文章/贴图详情，关闭 ({i+1})")
            close_detail(hwnd)
            time.sleep(0.6)
            continue
        if is_tietu_list(rows):
            # 若能扫到「贴图」则点一下；扫不到也不阻断（有的号无 Tab 文案）
            clicked = False
            for r in rows:
                if r["text"].strip() == "贴图" and r["conf"] > 0.7:
                    click(geom["L"] + r["cx"], geom["T"] + r["cy"])
                    time.sleep(0.7)
                    clicked = True
                    break
            if not clicked:
                write_progress("资料页已确认，但未 OCR 到「贴图」Tab，继续用当前页扫卡片")
                return True
            path2 = grab(geom, f"scan-tietu-{i}.png")
            rows2 = ocr_all(path2)
            if is_tietu_list(rows2) and not is_detail_page(rows2):
                write_progress("已在公众号资料/贴图列表页")
                return True
            # 点完贴图后仍像资料页也放行
            if is_tietu_list(rows2):
                return True
        else:
            write_progress(
                f"非公众号资料/贴图页（需 关注/已关注 + 私信|赞|多卡），尝试关闭/返回 ({i+1})"
            )
            try:
                close_detail(hwnd)
            except Exception as exc:
                write_progress(f"关闭详情异常: {exc}")
            time.sleep(0.5)
    return False


def scroll_safe(
    geom,
    hwnd,
    notches: int,
    down: bool = True,
    *,
    refocus: bool = False,
    wheel_delta: int | None = None,
    gap_s: float | None = None,
):
    """在右缘滚动槽连发滚轮（不点卡片）。

    refocus=False：不重复前置窗口（避免每次 0.25s）。
    gap_s=0：连续平滑连发，不再一点一顿。
    """
    if refocus:
        focus(hwnd)
    sx = geom["R"] - 18
    sy = geom["T"] + 980
    USER32.SetCursorPos(int(sx), int(sy))
    mag = int(wheel_delta if wheel_delta is not None else SCAN_WHEEL_DELTA)
    gap = SCAN_WHEEL_GAP_S if gap_s is None else float(gap_s)
    raw = -mag if down else mag
    delta = ctypes.c_uint(raw & 0xFFFFFFFF).value
    n = abs(int(notches))
    for i in range(n):
        USER32.mouse_event(0x0800, 0, 0, delta, 0)
        if gap > 0:
            time.sleep(gap)


def scroll_smooth_down(geom, hwnd, notches: int | None = None):
    """向下滚（默认 1 格）+ SCAN_SETTLE_S 停稳（默认 1 秒一次）。"""
    n = SCAN_DOWN_NOTCHES if notches is None else int(notches)
    scroll_safe(geom, hwnd, notches=n, down=True, refocus=False)
    if SCAN_SETTLE_S > 0:
        time.sleep(SCAN_SETTLE_S)


def _upgrade_catalog_item(item: dict, c: dict, tid: str, fp: str) -> None:
    """用更完整的 OCR 标题覆盖残缺条目。"""
    item["text"] = c.get("text") or item.get("text")
    item["title_id"] = tid or item.get("title_id")
    item["key"] = tid or item.get("key") or fp
    if fp:
        item["fp"] = fp
    for fld in (
        "cx",
        "cy",
        "bar_x",
        "bar_y",
        "bar_w",
        "bar_h",
        "roi_x",
        "roi_y",
        "roi_w",
        "roi_h",
        "poster_h",
        "short",
        "ink",
        "method",
    ):
        if c.get(fld) is not None:
            item[fld] = c.get(fld)


def merge_catalog(catalog: list[dict], caps: list[dict]) -> int:
    """旧路径：按 fp 去重。新路径请用 CardTrackManager（USE_CARD_TRACKER）。

    保留作回退；不再用 poster_h 硬拒识别——半截由 Track PARTIAL 承接。
    """
    added = 0
    for c in caps:
        tid = c.get("title_id") or ""
        fp = c.get("fp") or c.get("key") or ""
        text = c.get("text") or ""
        if not fp:
            continue
        hit = None
        for item in catalog:
            if item.get("fp") == fp or item.get("key") == fp:
                hit = item
                break
        if hit is not None:
            old_q = title_quality(hit.get("title_id") or hit.get("text") or "")
            new_q = title_quality(tid or text)
            if new_q > old_q + 1:
                old_t = hit.get("title_id") or hit.get("text") or ""
                new_t = tid or text
                write_progress(
                    f"  标题升级 fp={str(hit.get('fp') or '')[:8]} 「{old_t}」→「{new_t}」"
                )
                _upgrade_catalog_item(hit, c, tid, fp)
            old_ph = int(hit.get("poster_h") or 0)
            new_ph = int(c.get("poster_h") or 0)
            if new_ph > old_ph + 8:
                _upgrade_catalog_item(hit, c, tid or hit.get("title_id") or "", fp)
            continue

        item = {
            "text": text or f"贴图_{fp[:10]}",
            "title_id": tid,
            "key": fp,
            "fp": fp,
            "cx": c.get("cx"),
            "cy": c.get("cy"),
            "bar_x": c.get("bar_x"),
            "bar_y": c.get("bar_y"),
            "bar_w": c.get("bar_w"),
            "bar_h": c.get("bar_h"),
            "roi_x": c.get("roi_x"),
            "roi_y": c.get("roi_y"),
            "roi_w": c.get("roi_w"),
            "roi_h": c.get("roi_h"),
            "poster_h": c.get("poster_h"),
            "short": c.get("short"),
            "ink": c.get("ink"),
            "method": c.get("method"),
        }
        catalog.append(item)
        added += 1
    return added


def clean_catalog(catalog: list[dict]) -> list[dict]:
    """按海报指纹 fp 去重（允许同名标题多张并存）。"""
    seen = set()
    final = []
    for c in catalog:
        k = c.get("fp") or c.get("key")
        if not k or k in seen:
            continue
        seen.add(k)
        final.append(c)
    return final


def require_wechat_window(*, save_geom: bool = True) -> dict:
    """脚本1门禁：必须先拿到有效微信主窗，否则抛错中止。

    1) 校验/刷新 wx_geom.json 句柄与矩形
    2) 前置窗口
    3) 截屏 OCR：需像公众号资料页（关注/已关注 + 私信|赞|多卡）
    """
    PROGRESS.parent.mkdir(parents=True, exist_ok=True)
    try:
        geom = refresh_geom(save=save_geom)
    except RuntimeError as exc:
        write_progress(f"门禁失败：{exc}")
        raise
    hwnd = int(geom["H"])
    if not USER32.IsWindow(hwnd):
        msg = f"门禁失败：刷新后句柄仍无效 H={hwnd}"
        write_progress(msg)
        raise RuntimeError(msg)
    focus(hwnd)
    write_progress(
        f"门禁：已定位微信窗 H={hwnd} "
        f"rect=({geom['L']},{geom['T']},{geom['R']},{geom['B']})"
    )
    if not ensure_tietu_list(geom, hwnd):
        msg = (
            "门禁失败：已找到微信窗，但未识别到公众号资料/贴图页"
            "（需关注或已关注，且私信/赞/多卡）。请先打开目标公众号贴图瀑布流。"
        )
        write_progress(msg)
        raise RuntimeError(msg)
    write_progress("门禁通过：微信公众号贴图页已确认，开始扫描")
    return geom


def main(ocr_titles: bool = True, method: str = "card_roi"):
    from concurrent.futures import ThreadPoolExecutor

    PROGRESS.write_text("", encoding="utf-8")
    # 滚动中：快扫默认关 OCR；用户显式 ocr_titles 且 SCAN_OCR_DURING 才边滚边 OCR
    ocr_during = bool(ocr_titles) and (SCAN_OCR_DURING or not SCAN_FAST)
    mode_label = (
        "轮廓ROI+Track快扫"
        if method == "card_roi"
        else ("底栏+宽+标题色+标题识别码" if ocr_titles else "底栏+宽+标题色+aHash")
    )
    write_progress(
        f"重新扫描贴图瀑布流 | 模式={mode_label}"
        f" | method={method} | CARD_W=[{CARD_W_MIN},{CARD_W_MAX}]"
    )

    try:
        geom = require_wechat_window(save_geom=True)
    except RuntimeError:
        write_progress("脚本1中止：未满足微信公众号窗口门禁")
        return []
    hwnd = int(geom["H"])

    focus(hwnd)  # 只前置一次，后续滚动不再 focus
    catalog: list[dict] = []
    stagnant = 0
    max_steps = SCAN_MAX_STEPS
    prev_bottom_keys: set[str] = set()
    scroll_reg = None
    track_mgr = None
    if USE_CARD_TRACKER:
        import card_tracker as _card_tracker
        import scroll_tracker as _scroll_tracker
        import visual_matcher as _visual_matcher

        scroll_reg = _scroll_tracker.ScrollTracker(log=write_progress)
        track_mgr = _card_tracker.CardTrackManager(
            log=write_progress, col_mid=COL_MID
        )
        _card_tracker.POSTER_H_FULL = POSTER_H_ENROLL
        write_progress(
            f"Track 模式开 | visual={_visual_matcher.backend_name()} | "
            f"FULL门槛 poster_h>={POSTER_H_ENROLL}（半截可 Track，不发 uuid）"
        )
    down_n = SCAN_DOWN_NOTCHES if SCAN_FAST else SCAN_DOWN_NOTCHES_SAFE
    write_progress(
        f"快扫={SCAN_FAST} 连发notches={down_n} delta={SCAN_WHEEL_DELTA} "
        f"gap={SCAN_WHEEL_GAP_S}s settle={SCAN_SETTLE_S}s "
        f"ocr_during={ocr_during} parallel={SCAN_PARALLEL}"
    )

    pool = ThreadPoolExecutor(max_workers=1) if SCAN_PARALLEL else None
    # 下行净滚轮格数：回顶时原路返还（补扫上滚会扣减）
    down_notches_total = 0

    def _detect(arr: np.ndarray) -> list[dict]:
        return extract_cards_fast(arr, ocr_titles=ocr_during, method=method)

    try:
        for step in range(max_steps):
            t0 = time.time()
            do_save = (step % max(1, SCAN_SAVE_EVERY) == 0) or (not SCAN_FAST)
            arr_now, path = grab_arr(
                geom, f"scan-down-{step:02d}.png", save=do_save
            )
            # 本帧截屏时相对顶部的净下滚格数（先记再滚，供点卡回放）
            notches_at_frame = int(down_notches_total)
            scroll_delta = 0.0
            if scroll_reg is not None:
                scroll_delta = float(scroll_reg.register(arr_now).delta_y)

            # 流水线：提交识别后立刻开始连发下滚，与 CV 重叠
            detect_fut = pool.submit(_detect, arr_now) if pool else None
            if step < max_steps - 1:
                scroll_smooth_down(geom, hwnd, notches=down_n)
                down_notches_total += int(down_n)

            caps = detect_fut.result() if detect_fut is not None else _detect(arr_now)
            if not caps and path is not None:
                rows = ocr_all(path)
                if is_detail_page(rows):
                    write_progress(f"滚动中误入详情，关闭后继续 (step {step+1})")
                    close_detail(hwnd)
                    ensure_tietu_list(geom, hwnd, tries=3)
                    focus(hwnd)
                    stagnant = 0
                    prev_bottom_keys = set()
                    if scroll_reg is not None:
                        scroll_reg.reset()
                    continue

            now_keys = {
                (c.get("fp") or c.get("key") or "")
                for c in caps
                if (c.get("fp") or c.get("key"))
            }
            if prev_bottom_keys and now_keys and not (prev_bottom_keys & now_keys):
                write_progress(
                    f"  ⚠ 与上一屏底部无重叠，回退 {SCAN_DOWN_BACK_NOTCHES} 格补扫"
                )
                scroll_safe(
                    geom, hwnd, notches=SCAN_DOWN_BACK_NOTCHES, down=False, refocus=False
                )
                down_notches_total = max(
                    0, down_notches_total - int(SCAN_DOWN_BACK_NOTCHES)
                )
                time.sleep(max(0.05, SCAN_SETTLE_S))
                arr_now, path = grab_arr(
                    geom, f"scan-down-{step:02d}-back.png", save=True
                )
                notches_at_frame = int(down_notches_total)
                if scroll_reg is not None:
                    scroll_delta = float(scroll_reg.register(arr_now).delta_y)
                caps = _detect(arr_now)
                now_keys = {
                    (c.get("fp") or c.get("key") or "")
                    for c in caps
                    if (c.get("fp") or c.get("key"))
                }

            if track_mgr is not None and scroll_reg is not None:
                _new_t, _merge_t, added = track_mgr.observe(
                    caps,
                    arr_now,
                    scroll_reg.cumulative_scroll_y,
                    delta_y=scroll_delta,
                    wheel_notches=notches_at_frame,
                )
                catalog = track_mgr.confirmed_catalog()
                write_progress(
                    f"  tracks={len(track_mgr.tracks)} confirmed={len(catalog)} "
                    f"new_tracks={_new_t} merges={_merge_t} new_uuid={added}"
                )
            else:
                added = merge_catalog(catalog, caps)
            dt = time.time() - t0
            texts = [c.get("text") or c.get("fp", "")[:8] for c in caps]
            write_progress(
                f"向下扫描 {step+1}/{max_steps} | 本屏 {len(caps)} 卡 +新增{added} | "
                f"累计 {len(catalog)} | 净下滚={down_notches_total}格 | "
                f"{dt:.2f}s | {texts}"
            )
            write_progress(
                f"  stagnant={stagnant}/{SCAN_STAGNANT_LIMIT} "
                f"（连续无新增达 {SCAN_STAGNANT_LIMIT} 次则判到底）"
            )
            if step % 3 == 0:
                CATALOG.write_text(
                    json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8"
                )

            bottom = sorted(
                caps, key=lambda z: float(z.get("bar_y") or 0), reverse=True
            )[:4]
            prev_bottom_keys = {
                (c.get("fp") or c.get("key") or "")
                for c in bottom
                if (c.get("fp") or c.get("key"))
            }

            if added == 0:
                stagnant += 1
            else:
                stagnant = 0
            if stagnant >= SCAN_STAGNANT_LIMIT and step >= 2:
                write_progress("连续无新增，判定已到底部")
                break
            # 下滚已在识别前发起（流水线）；此处不再二次滚动
    finally:
        if pool is not None:
            pool.shutdown(wait=False)

    if track_mgr is not None:
        catalog = track_mgr.confirmed_catalog()
        # 滚动中关 OCR 时：对已确认卡用 reference 底带补一次标题（第二优先）
        if ocr_titles and not ocr_during:
            write_progress("补 OCR 标题（仅 confirmed，不阻塞滚动）…")
            for t in track_mgr.tracks:
                if not t.card_uuid or t.title_id:
                    continue
                if t.reference_image is None:
                    continue
                try:
                    rh, rw = t.reference_image.shape[:2]
                    band_h = min(64, max(28, rh // 5))
                    title, _conf, _raw = ocr_text_inside_box(
                        t.reference_image, 0, max(0, rh - band_h), rw, band_h
                    )
                    if region_has_title_text(title):
                        t.title = title
                        t.title_id = normalize_title_id(title)
                except Exception:
                    pass
            catalog = track_mgr.confirmed_catalog()
        dbg = TOOLS / "waterfall_tracks_debug.json"
        dbg.write_text(
            json.dumps(track_mgr.all_tracks_debug(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        write_progress(
            f"Track 汇总: tracks={len(track_mgr.tracks)} confirmed_uuid={len(catalog)} "
            f"debug→{dbg.name}"
        )
    write_progress(
        f"底部扫描完成，共 {len(catalog)} 张，下行净滚轮={down_notches_total} 格；"
        f"标题识别后原路回顶（速度×2）…"
    )
    # 原路回顶：上滚与下行相同格数；间隔取下行 settle 的一半（速度提高一倍）
    up_gap = max(0.02, float(SCAN_SETTLE_S) / 2.0)
    if down_notches_total > 0:
        write_progress(
            f"原路上滚 {down_notches_total} 格 | gap={up_gap:.2f}s "
            f"(下行 settle={SCAN_SETTLE_S:.2f}s 的 1/2)"
        )
        scroll_safe(
            geom,
            hwnd,
            notches=down_notches_total,
            down=False,
            refocus=False,
            gap_s=up_gap,
        )
    else:
        write_progress("下行格数为 0，跳过上滚")
    time.sleep(0.3)
    # 回顶后 OCR 仅作校验日志，不再用 OCR 提前打断上滚
    path = grab(geom, "scan-top.png")
    try:
        rows = ocr_all(path)
        bars = find_candidate_2f2f30_boxes(np.array(Image.open(path).convert("RGB")))
        bar0_y = bars[0][1] if bars else None
        profile_top = is_tietu_list(rows)
        write_progress(
            f"回顶校验 OCR: profile(关注/已关注+私信)={profile_top} "
            f"bar0_y={bar0_y} 返还格数={down_notches_total}"
        )
    except Exception as exc:
        write_progress(f"回顶校验 OCR 异常（不影响继续）: {exc}")
    cleaned = clean_catalog(catalog)
    replay_n = sum(1 for c in cleaned if c.get("click_at_notches") is not None)
    write_progress("=" * 40)
    write_progress(f"统计结果：共 {len(cleaned)} 张贴图（标题识别码/指纹去重）")
    write_progress(
        f"点卡回放字段：{replay_n}/{len(cleaned)} 张已记录 click_at_notches+坐标"
    )
    short_n = sum(1 for c in cleaned if c.get("short"))
    write_progress(f"其中短卡(poster_h<{POSTER_H_SHORT}): {short_n} 张")
    for i, c in enumerate(cleaned, 1):
        tid = c.get("title_id") or "-"
        ph = c.get("poster_h")
        mark = "短" if c.get("short") else ""
        rn = c.get("click_at_notches")
        write_progress(
            f"  {i}/{len(cleaned)}  {c['text']}  id={tid}  "
            f"w={c.get('bar_w','?')} poster_h={ph}{mark} "
            f"fp={str(c.get('fp', c.get('key','')))[:10]} "
            f"replay_n={rn if rn is not None else '-'}"
        )
    write_progress("=" * 40)
    CATALOG.write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")
    (TOOLS / "waterfall_catalog_clean.json").write_text(
        json.dumps(cleaned, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    meta = {
        "down_notches_total": int(down_notches_total),
        "down_notches_per_step": int(down_n),
        "replay_stamped": int(replay_n),
        "total": int(len(cleaned)),
    }
    (TOOLS / "waterfall_scan_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    write_progress(f"扫描元数据 → waterfall_scan_meta.json {meta}")
    print(f"TOTAL={len(cleaned)}", flush=True)
    return cleaned


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="贴图瀑布流扫描（轮廓优先；快扫连发滚动）")
    ap.add_argument(
        "--ocr-titles",
        action="store_true",
        default=True,
        help="扫描结束后补 OCR 标题（默认开；滚动中默认不做 OCR）",
    )
    ap.add_argument(
        "--no-ocr-titles",
        action="store_true",
        help="完全关闭标题 OCR",
    )
    ap.add_argument(
        "--method",
        choices=("bar", "card_roi"),
        default="card_roi",
        help="card_roi=整卡轮廓（默认）；bar=仅底栏色块",
    )
    ap.add_argument(
        "--slow",
        action="store_true",
        help="慢扫：小步停顿 + 滚动中 OCR（旧行为）",
    )
    ap.add_argument(
        "--ocr-during",
        action="store_true",
        help="滚动过程中也做标题 OCR（更慢）",
    )
    args = ap.parse_args()
    if args.slow:
        SCAN_FAST = False
        SCAN_OCR_DURING = True
        SCAN_PARALLEL = False
        SCAN_DOWN_NOTCHES = SCAN_DOWN_NOTCHES_SAFE
        SCAN_WHEEL_GAP_S = 0.0
        SCAN_SETTLE_S = 1.5
        SCAN_SAVE_EVERY = 1
    if args.ocr_during:
        SCAN_OCR_DURING = True
    main(ocr_titles=not args.no_ocr_titles, method=args.method)
