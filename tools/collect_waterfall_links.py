# -*- coding: utf-8 -*-
"""Collect mp.weixin links from 贴图 waterfall via OCR click + fixed copy-link chord.

Flow: quick scroll to bottom (OCR+dedupe titles) -> scroll back to top -> count ->
click each card in order with progress.
"""
from __future__ import annotations

import ctypes
import json
import re
import sys
import time
from dataclasses import dataclass, field
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
OUT = TOOLS / "waterfall_links.json"
SEEN = TOOLS / "waterfall_seen.json"
CATALOG = TOOLS / "waterfall_catalog.json"
PROGRESS = TOOLS / "waterfall_progress.txt"

# Proven on 2880x1800 DPI-aware desktop while detail tab is open
# 图片集合页（卡片详情）：只标定 …；关闭用相对偏移（Δx=+40, Δy=0）
DOTS_SCREEN = (777, 38)
CLOSE_OFFSET_FROM_DOTS = (40, 0)  # 关闭 = … + 该相对坐标


def close_screen_from_dots(dots=None) -> tuple[int, int]:
    """关闭按钮 = … 坐标 + 相对偏移。"""
    dx, dy = dots if dots is not None else DOTS_SCREEN
    ox, oy = CLOSE_OFFSET_FROM_DOTS
    return (int(dx + ox), int(dy + oy))

VK_DOWN = 0x28
VK_RETURN = 0x0D
VK_HOME = 0x24
VK_PRIOR = 0x21  # Page Up
VK_NEXT = 0x22  # Page Down
KEYUP = 0x0002

OCR = RapidOCR()


def load_geom():
    return json.loads((TOOLS / "wx_geom.json").read_text(encoding="utf-8-sig"))


def focus(hwnd):
    USER32.SetForegroundWindow(int(hwnd))
    time.sleep(0.25)


def click(x, y):
    USER32.SetCursorPos(int(x), int(y))
    time.sleep(0.06)
    USER32.mouse_event(0x0002, 0, 0, 0, 0)
    time.sleep(0.03)
    USER32.mouse_event(0x0004, 0, 0, 0, 0)


def key(vk):
    USER32.keybd_event(vk, 0, 0, 0)
    time.sleep(0.03)
    USER32.keybd_event(vk, 0, KEYUP, 0)


def grab(geom, name):
    img = ImageGrab.grab(bbox=(geom["L"], geom["T"], geom["R"], geom["B"]))
    path = OBS / name
    img.save(path)
    return path


CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
KERNEL32 = ctypes.windll.kernel32
USER32.OpenClipboard.argtypes = [ctypes.c_void_p]
USER32.OpenClipboard.restype = ctypes.c_bool
USER32.CloseClipboard.restype = ctypes.c_bool
USER32.EmptyClipboard.restype = ctypes.c_bool
USER32.GetClipboardData.argtypes = [ctypes.c_uint]
USER32.GetClipboardData.restype = ctypes.c_void_p
USER32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
USER32.SetClipboardData.restype = ctypes.c_void_p
KERNEL32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
KERNEL32.GlobalAlloc.restype = ctypes.c_void_p
KERNEL32.GlobalLock.argtypes = [ctypes.c_void_p]
KERNEL32.GlobalLock.restype = ctypes.c_void_p
KERNEL32.GlobalUnlock.argtypes = [ctypes.c_void_p]
KERNEL32.GlobalUnlock.restype = ctypes.c_bool


def clipboard_get() -> str:
    """Read CF_UNICODETEXT via Win32 (avoid tkinter holding clipboard ownership)."""
    for _ in range(8):
        if USER32.OpenClipboard(None):
            try:
                handle = USER32.GetClipboardData(CF_UNICODETEXT)
                if not handle:
                    return ""
                ptr = KERNEL32.GlobalLock(handle)
                if not ptr:
                    return ""
                try:
                    return ctypes.wstring_at(ptr)
                finally:
                    KERNEL32.GlobalUnlock(handle)
            finally:
                USER32.CloseClipboard()
        time.sleep(0.05)
    return ""


def clipboard_set(text: str) -> bool:
    """Write Unicode text via Win32 and release ownership immediately."""
    data = str(text or "")
    for _ in range(8):
        if USER32.OpenClipboard(None):
            try:
                USER32.EmptyClipboard()
                buf = data.encode("utf-16-le") + b"\x00\x00"
                h = KERNEL32.GlobalAlloc(GMEM_MOVEABLE, len(buf))
                if not h:
                    return False
                ptr = KERNEL32.GlobalLock(h)
                if not ptr:
                    return False
                ctypes.memmove(ptr, buf, len(buf))
                KERNEL32.GlobalUnlock(h)
                if not USER32.SetClipboardData(CF_UNICODETEXT, h):
                    return False
                return True
            finally:
                USER32.CloseClipboard()
        time.sleep(0.05)
    return False


def wait_clipboard_link(marker: str = "__WAITING__", timeout: float = 4.0) -> str:
    """Poll clipboard until mp.weixin link appears (or timeout)."""
    deadline = time.time() + timeout
    last = ""
    n = 0
    while time.time() < deadline:
        n += 1
        clip = (clipboard_get() or "").strip()
        last = clip
        if re.search(r"https?://mp\.weixin\.qq\.com/s/[A-Za-z0-9_\-]+", clip):
            # normalize: take first URL if extra text present
            m = re.search(r"https?://mp\.weixin\.qq\.com/s/[A-Za-z0-9_\-]+", clip)
            url = m.group(0) if m else clip
            write_progress(f"复制链接和弦: 轮询#{n} 读到链接 {url}")
            return url
        if clip and clip != marker:
            write_progress(f"复制链接和弦: 轮询#{n} 非链接内容={clip[:80]}")
        time.sleep(0.2)
    write_progress(f"复制链接和弦: 轮询超时({timeout}s) 最后内容={last[:120] if last else '(空)'}")
    return last


def write_progress(msg: str):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    prev = PROGRESS.read_text(encoding="utf-8") if PROGRESS.exists() else ""
    PROGRESS.write_text(prev + line + "\n", encoding="utf-8")


def ocr_captions(img_path: Path):
    arr = np.array(Image.open(img_path))
    result, _ = OCR(arr)
    skip_sub = ("关注", "私信", "全部", "贴图", "展开", "山西恒泰", "忻州", "承揽", "压机", "赞", "HOT")
    caps = []
    for box, text, conf in result or []:
        t = text.strip()
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        cy = sum(ys) / len(ys)
        cx = sum(xs) / len(xs)
        cn = len(re.findall(r"[\u4e00-\u9fff]", t))
        if float(conf) < 0.7 or cy < 600 or cn < 4 or len(t) > 24:
            continue
        if any(s in t for s in skip_sub):
            continue
        if re.fullmatch(r"[A-Za-z0-9_\-]+", t):
            continue
        caps.append({"text": t, "cx": cx, "cy": cy, "conf": float(conf)})
    seen = set()
    uniq = []
    for c in sorted(caps, key=lambda z: (z["cy"], z["cx"])):
        k = re.sub(r"\s+", "", c["text"])
        if k in seen:
            continue
        seen.add(k)
        uniq.append(c)
    return uniq


def copy_link_chord(hwnd):
    marker = f"__WAITING_{int(time.time()*1000)}__"
    write_progress(f"复制链接和弦: 点击… {DOTS_SCREEN}")
    ok_set = clipboard_set(marker)
    write_progress(f"复制链接和弦: 标记剪贴板={marker} ok={ok_set}")
    focus(hwnd)
    click(*DOTS_SCREEN)
    time.sleep(1.0)
    for i in range(3):
        focus(hwnd)
        time.sleep(0.08)
        key(VK_DOWN)
        write_progress(f"复制链接和弦: ↓ 第 {i+1}/3（间隔 0.5s）")
        time.sleep(0.5)
    focus(hwnd)
    time.sleep(0.08)
    write_progress("复制链接和弦: Enter 确认，开始轮询剪贴板…")
    key(VK_RETURN)
    # WeChat often needs >0.6s; poll instead of single read
    clip = wait_clipboard_link(marker=marker, timeout=4.0)
    write_progress(f"复制链接和弦: 最终剪贴板={clip[:120] if clip else '(空)'}")
    return clip


def close_detail(hwnd, settle_s: float = 0.9):
    """点击 … 右侧关闭按钮（DOTS + CLOSE_OFFSET）。"""
    focus(hwnd)
    close_xy = close_screen_from_dots()
    write_progress(f"关闭详情: …={DOTS_SCREEN} → 关闭={close_xy} (相对+{CLOSE_OFFSET_FROM_DOTS})")
    click(*close_xy)
    if settle_s > 0:
        time.sleep(settle_s)


def dismiss_stale_detail(hwnd, *, label: str = ""):
    """打开下一张前先点关闭，防止上一张详情残留挡列表。"""
    prefix = f"{label} " if label else ""
    write_progress(f"{prefix}打开前先点 … 右侧关闭（防残留详情）")
    close_detail(hwnd, settle_s=0.5)


def ensure_tietu(geom, hwnd):
    """Click 贴图 tab if visible via OCR."""
    focus(hwnd)
    path = grab(geom, "collect-ensure.png")
    arr = np.array(Image.open(path))
    result, _ = OCR(arr)
    for box, text, conf in result or []:
        if text.strip() == "贴图" and float(conf) > 0.7:
            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
            click(geom["L"] + sum(xs) / len(xs), geom["T"] + sum(ys) / len(ys))
            time.sleep(1.0)
            return True
    return False


def content_focus(geom, hwnd):
    """Focus scroll gutter near right edge — never click card bodies."""
    focus(hwnd)
    USER32.SetCursorPos(int(geom["R"] - 18), int(geom["T"] + 980))
    time.sleep(0.08)


def wheel(geom, hwnd, notches: int, down: bool = True):
    content_focus(geom, hwnd)
    raw = -120 if down else 120
    delta = ctypes.c_uint(raw & 0xFFFFFFFF).value
    for _ in range(abs(notches)):
        USER32.mouse_event(0x0800, 0, 0, delta, 0)
        time.sleep(0.05)


def _frame_sig(arr: np.ndarray) -> bytes:
    """廉价帧签名：页面没滚时签名不变，避免重复整卡识别。"""
    g = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY) if arr.ndim == 3 else arr
    small = cv2.resize(g, (48, 48), interpolation=cv2.INTER_AREA)
    return small.tobytes()


@dataclass
class ScreenCapsCache:
    """本屏卡片缓存：仅当画面变化（滚动）后才重新 CV/OCR。"""

    caps: list | None = None
    sig: bytes | None = None
    ocr_titles: bool | None = None

    def invalidate(self) -> None:
        self.caps = None
        self.sig = None
        self.ocr_titles = None


@dataclass
class OpenSession:
    """跨多张卡保持滚动累计与本屏缓存，避免每张都当「从顶部 cum=0」重来。"""

    cum_scroll_y: float = 0.0
    cache: ScreenCapsCache = field(default_factory=ScreenCapsCache)
    # 点卡回放：相对顶部的净下滚格数（与 Phase1 click_at_notches 同坐标系）
    wheel_notches_from_top: int = 0


def get_screen_caps(
    geom,
    *,
    cache: ScreenCapsCache | None = None,
    ocr_titles: bool = False,
    shot_name: str = "screen-caps.png",
    force: bool = False,
) -> tuple[Path, np.ndarray, list]:
    """截屏取本屏卡片；cache 命中且 ocr 需求未升高时跳过重识别。"""
    import scan_waterfall_catalog as scan

    shot = grab(geom, shot_name)
    arr = np.array(Image.open(shot).convert("RGB"))
    sig = _frame_sig(arr)
    can_reuse = (
        cache is not None
        and not force
        and cache.caps is not None
        and cache.sig == sig
        and (
            # 已有带标题缓存可满足不要标题的请求；反之则必须重算
            (not ocr_titles)
            or (cache.ocr_titles is True)
        )
    )
    if can_reuse:
        write_progress(
            f"本屏卡片缓存命中（未滚动），跳过重识别 n={len(cache.caps)}"
        )
        return shot, arr, cache.caps

    caps = scan.extract_cards_fast(shot, ocr_titles=ocr_titles)
    if cache is not None:
        cache.caps = caps
        cache.sig = sig
        cache.ocr_titles = bool(ocr_titles) or bool(cache.ocr_titles)
    return shot, arr, caps


def scroll_toward_global_y(
    geom,
    hwnd,
    session: OpenSession,
    global_bar_y: float,
    *,
    prefer_y: float = 720.0,
    band: tuple[float, float] = (520.0, 1100.0),
    max_steps: int = 10,
) -> float:
    """不跑整卡 OCR，只滚到使 global_bar_y 落在可视点击带；更新 session.cum。"""
    import scroll_tracker as _scroll_tracker

    gy = float(global_bar_y)
    lo, hi = band
    tracker = _scroll_tracker.ScrollTracker(log=write_progress)
    # 用当前帧对齐 tracker；cum 仍以 session 为准
    shot0 = grab(geom, "seek-toward-00.png")
    arr0 = np.array(Image.open(shot0).convert("RGB"))
    tracker.register(arr0)
    session.cache.invalidate()
    stagnant = 0

    for step in range(max_steps):
        predict = gy - float(session.cum_scroll_y)
        if lo <= predict <= hi:
            write_progress(
                f"已对准下一张 pred_by={predict:.0f} cum={session.cum_scroll_y:.0f} "
                f"(目标带 {lo:.0f}..{hi:.0f})"
            )
            return float(session.cum_scroll_y)
        down = predict > hi
        notches = 3 if abs(predict - prefer_y) > 400 else 2
        write_progress(
            f"对准滚动 #{step+1}/{max_steps}: pred_by={predict:.0f} → "
            f"{'下' if down else '上'}滚 {notches} 格"
        )
        wheel(geom, hwnd, notches=notches, down=down)
        time.sleep(0.22)
        shot = grab(geom, f"seek-toward-{step+1:02d}.png")
        arr = np.array(Image.open(shot).convert("RGB"))
        est = tracker.register(arr)
        session.cum_scroll_y += float(est.delta_y)
        session.cache.invalidate()
        # 到底（如「已无更多消息」）再滚位移≈0，不必空转满 10 次
        if abs(float(est.delta_y)) < 2.0:
            stagnant += 1
            if stagnant >= 2:
                write_progress(
                    f"对准滚动：连续无位移，可能已到底，提前结束 "
                    f"(pred_by={predict:.0f})"
                )
                break
        else:
            stagnant = 0
    predict = gy - float(session.cum_scroll_y)
    write_progress(
        f"对准结束（达上限 {max_steps}）pred_by={predict:.0f} "
        f"cum={session.cum_scroll_y:.0f}"
    )
    return float(session.cum_scroll_y)


def merge_catalog(catalog: list[dict], caps: list[dict]) -> int:
    """Append new titles; return how many new ones added."""
    have = {re.sub(r"\s+", "", c["text"]) for c in catalog}
    added = 0
    for c in caps:
        k = re.sub(r"\s+", "", c["text"])
        if k in have:
            continue
        have.add(k)
        catalog.append({"text": c["text"], "key": k})
        added += 1
    return added


def scan_catalog(geom, hwnd, max_steps: int = 40) -> list[dict]:
    """Quick scroll to bottom OCR-ing captions, then scroll back to top."""
    catalog: list[dict] = []
    PROGRESS.write_text("", encoding="utf-8")
    write_progress("开始统计：快速滚向瀑布流底部…")
    ensure_tietu(geom, hwnd)
    time.sleep(0.4)

    stagnant = 0
    for step in range(max_steps):
        shot = grab(geom, f"catalog-down-{step:02d}.png")
        caps = ocr_captions(shot)
        added = merge_catalog(catalog, caps)
        write_progress(f"向下扫描 {step+1}/{max_steps} | 本屏+{added} | 累计 {len(catalog)} 张 | {[c['text'] for c in caps]}")
        if added == 0:
            stagnant += 1
        else:
            stagnant = 0
        if stagnant >= 3 and step >= 2:
            write_progress("连续无新增，判定已到底部")
            break
        wheel(geom, hwnd, notches=8, down=True)
        time.sleep(0.35)

    CATALOG.write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")
    write_progress(f"底部扫描完成，共 {len(catalog)} 张，开始回滚到顶部…")

    # scroll back to top quickly
    for up in range(max_steps + 5):
        wheel(geom, hwnd, notches=10, down=False)
        time.sleep(0.08)
        if up % 5 == 4:
            shot = grab(geom, f"catalog-up-{up:02d}.png")
            arr = np.array(Image.open(shot))
            result, _ = OCR(arr)
            texts = [t.strip() for b, t, c in (result or [])]
            joined = "".join(texts)
            # header markers near top of 贴图 list
            if "关注" in joined and "贴图" in joined and (catalog and catalog[0]["text"][:4] in joined):
                write_progress(f"已回到顶部（第 {up+1} 次上滚检测）")
                break
    else:
        # Home as extra insurance while content focused
        content_focus(geom, hwnd)
        key(VK_HOME)
        time.sleep(0.5)
        write_progress("上滚结束（Home 兜底）")

    time.sleep(0.5)
    grab(geom, "catalog-top.png")
    write_progress(f"统计完成：瀑布流共 {len(catalog)} 张图")
    for i, c in enumerate(catalog, 1):
        write_progress(f"  目录 {i}/{len(catalog)}  {c['text']}")
    return catalog


def open_card(geom, hwnd, title: str, hint_cx: float | None = None):
    """Find caption on current view and click image body above it."""
    shot = grab(geom, "collect-find.png")
    caps = ocr_captions(shot)
    target = None
    keyname = re.sub(r"\s+", "", title)
    for c in caps:
        if re.sub(r"\s+", "", c["text"]) == keyname or title[:4] in c["text"]:
            target = c
            break
    if not target:
        return False, "caption_not_visible"
    offset_y = 180 if target["cx"] < 700 else 220
    sx = geom["L"] + target["cx"]
    sy = geom["T"] + max(target["cy"] - offset_y, 620)
    focus(hwnd)
    click(sx, sy)
    return True, (sx, sy)


def _fp_hamming(a: str, b: str) -> int:
    try:
        aa = a.strip().lower()[:16]
        bb = b.strip().lower()[:16]
        if len(aa) < 8 or len(bb) < 8:
            return 64
        return (int(aa, 16) ^ int(bb, 16)).bit_count()
    except ValueError:
        return 64


def _card_matches(
    c: dict,
    title_id: str = "",
    fp: str = "",
    card_uuid: str = "",
    column: str = "",
    predict_bar_y: float | None = None,
    soft_fp: bool = True,
) -> bool:
    """匹配：uuid → 精确/软 fp → 列+预测 bar_y；title 绝不单独决定。"""
    cu = (card_uuid or "").strip()
    if cu and (
        (c.get("card_uuid") or "") == cu
        or (c.get("key") or "") == cu
        or (c.get("track_id") or "") == cu
    ):
        return True
    cfp = str(c.get("fp") or c.get("key") or "")
    if fp:
        if cfp == fp or c.get("key") == fp:
            return True
        if soft_fp and _fp_hamming(cfp, fp) <= 12:
            return True
    # 位置：同列 + 预测底栏 y（回顶后 cum≈0 时尤准）
    if column and predict_bar_y is not None:
        cx = float(c.get("bar_x") or c.get("roi_x") or c.get("cx") or 0)
        col_ok = (column == "left" and cx < 700) or (column == "right" and cx >= 700)
        by = float(c.get("bar_y") or 0)
        if col_ok and abs(by - float(predict_bar_y)) <= 90:
            return True
    if fp or cu:
        return False
    tid = (title_id or "").strip()
    if not tid:
        return False
    ct = c.get("title_id") or ""
    if not ct and c.get("text"):
        import scan_waterfall_catalog as scan

        ct = scan.normalize_title_id(c.get("text", ""))
    if ct and (ct == tid or tid in ct or ct in tid):
        return True
    return (c.get("key") or "") == tid


def _score_target_card(
    c: dict,
    fp: str = "",
    title_id: str = "",
    card_uuid: str = "",
    column: str = "",
    predict_bar_y: float | None = None,
) -> float:
    """单卡打分：uuid > fp > 列+预测y > 标题（标题不单独决定，但可与位置合成）。"""
    score = 0.0
    cfp = str(c.get("fp") or "")
    if card_uuid and (
        c.get("card_uuid") == card_uuid or c.get("key") == card_uuid
    ):
        score += 100
    if fp and cfp:
        if cfp == fp or c.get("key") == fp:
            score += 80
        else:
            ham = _fp_hamming(cfp, fp)
            if ham <= 12:
                score += 70 - ham
            elif ham <= 22:
                # 下滚后裁切/曝光会漂，放宽软指纹
                score += 28 - ham * 0.5
    col_ok = False
    if column:
        cx = float(c.get("bar_x") or c.get("roi_x") or c.get("cx") or 0)
        col_ok = (column == "left" and cx < 700) or (
            column == "right" and cx >= 700
        )
    if col_ok and predict_bar_y is not None:
        by = float(c.get("bar_y") or 0)
        dy = abs(by - float(predict_bar_y))
        if dy <= 160:
            score += 45 * max(0.0, 1.0 - dy / 160.0)
        elif dy <= 240:
            score += 12 * max(0.0, 1.0 - (dy - 160) / 80.0)
    tid = (title_id or "").strip()
    ct = (c.get("title_id") or "").strip()
    if not ct and c.get("text"):
        import scan_waterfall_catalog as scan

        ct = scan.normalize_title_id(c.get("text", ""))
    title_hit = bool(tid and ct and (ct == tid or tid in ct or ct in tid))
    if title_hit:
        # 有列约束时标题可与位置合成过线；无位置时仍不足以单独点同名卡
        score += 18 if col_ok or predict_bar_y is not None else 6
    return score


def _pick_target_card(
    caps: list[dict],
    fp: str = "",
    title_id: str = "",
    card_uuid: str = "",
    column: str = "",
    predict_bar_y: float | None = None,
) -> dict | None:
    """在本屏候选里打分选最优，避免同名误点、避免有卡却去下滚。"""
    best = None
    best_score = -1.0
    for c in caps:
        score = _score_target_card(
            c,
            fp=fp,
            title_id=title_id,
            card_uuid=card_uuid,
            column=column,
            predict_bar_y=predict_bar_y,
        )
        if score > best_score:
            best_score = score
            best = c
    if best is None or best_score < 18:
        return None
    return best


def _log_seek_miss(
    caps: list[dict],
    *,
    label: str,
    fp: str,
    title_id: str,
    card_uuid: str,
    column: str,
    predict_bar_y: float | None,
    step: int,
) -> None:
    """未命中时打印本屏前几名分数，便于排查 fp 漂移/越屏。"""
    ranked = []
    for c in caps:
        sc = _score_target_card(
            c,
            fp=fp,
            title_id=title_id,
            card_uuid=card_uuid,
            column=column,
            predict_bar_y=predict_bar_y,
        )
        ranked.append((sc, c))
    ranked.sort(key=lambda z: z[0], reverse=True)
    bits = []
    for sc, c in ranked[:4]:
        cfp = str(c.get("fp") or "")
        ham = _fp_hamming(cfp, fp) if fp and cfp else -1
        bits.append(
            f"sc={sc:.0f}/fp={(cfp or '-')[:10]}/ham={ham}/"
            f"by={c.get('bar_y')}/id={(c.get('title_id') or c.get('text') or '-')[:10]}"
        )
    pred_s = f"{predict_bar_y:.0f}" if predict_bar_y is not None else "-"
    write_progress(
        f"本屏未命中 key={label} pred_by={pred_s} col={column or '-'} "
        f"→ 下滚 step={step+1} | top: " + " | ".join(bits)
    )


def format_pre_click_inventory(
    caps: list | None,
    hit: dict | None,
    *,
    catalog_idx: int | None = None,
    catalog_total: int | None = None,
) -> str:
    """点击前库存文案：本屏几张、点本屏第几张、还剩几张后需滚动找新卡。"""
    caps = list(caps or [])
    n = len(caps)
    if n == 0 or hit is None:
        return f"当前页识别到 {n} 张卡片；目标不在本屏缓存中"
    ordered = sorted(
        caps,
        key=lambda c: (
            float(c.get("bar_y") or c.get("cy") or 0),
            float(c.get("bar_x") or c.get("cx") or 0),
        ),
    )
    hit_fp = str(hit.get("fp") or "")
    hit_by = float(hit.get("bar_y") or hit.get("cy") or -1)
    hit_bx = float(hit.get("bar_x") or hit.get("cx") or 0)
    pos = 0
    for i, c in enumerate(ordered, 1):
        same_fp = bool(hit_fp) and str(c.get("fp") or "") == hit_fp
        same_pos = abs(float(c.get("bar_y") or c.get("cy") or 0) - hit_by) < 3 and (
            abs(float(c.get("bar_x") or c.get("cx") or 0) - hit_bx) < 40
        )
        if same_fp or c is hit or same_pos:
            pos = i
            break
    if pos <= 0:
        pos = n
    remain = max(0, n - pos)
    cat = ""
    if catalog_idx is not None and catalog_total is not None:
        cat = f"（目录 {catalog_idx}/{catalog_total}）"
    return (
        f"当前页识别到 {n} 张卡片，准备点击本屏第 {pos} 张{cat}；"
        f"本屏还剩 {remain} 张后开始滚动寻找新卡片"
    )


def log_pre_click_inventory(
    caps: list | None,
    hit: dict | None,
    *,
    catalog_idx: int | None = None,
    catalog_total: int | None = None,
) -> str:
    msg = format_pre_click_inventory(
        caps, hit, catalog_idx=catalog_idx, catalog_total=catalog_total
    )
    write_progress(f"点击前库存: {msg}")
    return msg


def open_card_by_fingerprint(
    geom,
    hwnd,
    fp: str,
    title_id: str = "",
    card_uuid: str = "",
    column: str = "",
    global_bar_y: float | None = None,
    cum_scroll_y: float = 0.0,
    target: dict | None = None,
):
    """点本屏可见目标；用 fp/位置，禁止仅靠 title。

    target: 若 seek 已命中，直接点其坐标，避免再截屏+重识别一遍。
    """
    import scan_waterfall_catalog as scan

    predict_by = None
    if global_bar_y is not None:
        predict_by = float(global_bar_y) - float(cum_scroll_y)
    if target is None:
        shot = grab(geom, "collect-find-fp.png")
        use_ocr = bool(title_id)  # 开 OCR 仅作加分，不单独决定
        caps = scan.extract_cards_fast(shot, ocr_titles=use_ocr)
        target = _pick_target_card(
            caps,
            fp=fp,
            title_id=title_id,
            card_uuid=card_uuid,
            column=column,
            predict_bar_y=predict_by,
        )
    if not target:
        return False, "card_not_visible"
    sx = geom["L"] + float(target["cx"])
    sy = geom["T"] + float(target["cy"])
    pred_s = f"{predict_by:.0f}" if predict_by is not None else "-"
    write_progress(
        f"[CLICK] card_uuid={card_uuid or '-'} fp={(fp or '')[:10]} "
        f"col={column or '-'} pred_by={pred_s} "
        f"→ bbox=({target.get('roi_x')},{target.get('roi_y')},"
        f"{target.get('roi_w')},{target.get('roi_h')}) "
        f"click=({sx:.0f},{sy:.0f}) hit_fp={(target.get('fp') or '')[:10]}"
    )
    focus(hwnd)
    # 点卡专用：先停在目标上 1s，方便人工确认要点哪张（不影响关详情等其它 click）
    USER32.SetCursorPos(int(sx), int(sy))
    write_progress(f"[HOVER] 鼠标停在目标卡片 1s 后点击 @ ({sx:.0f},{sy:.0f})")
    time.sleep(1.0)
    click(sx, sy)
    return True, (sx, sy)


def scroll_until_fingerprint(
    geom,
    hwnd,
    fp: str,
    max_steps: int = 40,
    title_id: str = "",
    card_uuid: str = "",
    column: str = "",
    global_bar_y: float | None = None,
    cum_scroll_y: float = 0.0,
    session: OpenSession | None = None,
) -> tuple[dict | None, float]:
    """先在本屏找（fp/软fp/列+预测y）；找不到才小步下滚。

    画面未变时复用 ScreenCapsCache，滚动后才重识别。
    返回 (命中卡或 None, 更新后的 cum_scroll_y)。
    """
    import scroll_tracker as _scroll_tracker

    # 有 fp/uuid/列+y 时 seek 不做标题 OCR（最耗时）；标题只作无视觉键时的兜底
    use_ocr = bool(title_id) and not (fp or card_uuid or (column and global_bar_y is not None))
    label = (
        (card_uuid[:8] if card_uuid else None)
        or (fp[:10] if fp else None)
        or (title_id[:16] if title_id else "?")
    )
    cache = session.cache if session is not None else ScreenCapsCache()
    tracker = _scroll_tracker.ScrollTracker(log=write_progress)
    cum = float(
        session.cum_scroll_y
        if session is not None
        else (cum_scroll_y or 0.0)
    )
    base_cum = cum
    # 点击/seek 专用小滚：1 格，避免一次超过约一张卡高（不影响 Phase1 扫卡）
    seek_notches = 1
    seek_settle = 0.35
    write_progress(
        f"seek 开始 key={label} ocr_titles={use_ocr} "
        f"seek_notches={seek_notches} "
        f"(有指纹/位置则关 OCR，只做轮廓+fp)"
    )

    for step in range(max_steps):
        _shot, arr, caps = get_screen_caps(
            geom,
            cache=cache,
            ocr_titles=use_ocr,
            shot_name=f"seek-fp-{step:02d}.png",
        )
        est = tracker.register(arr)
        if step == 0:
            # first_frame：沿用 session/传入 cum，不把累计清零
            cum = base_cum
        else:
            cum = base_cum + float(tracker.cumulative_scroll_y)
        if session is not None:
            session.cum_scroll_y = cum
        predict_by = (
            float(global_bar_y) - cum if global_bar_y is not None else None
        )
        hit = _pick_target_card(
            caps,
            fp=fp,
            title_id=title_id,
            card_uuid=card_uuid,
            column=column,
            predict_bar_y=predict_by,
        )
        if hit is not None:
            pred_s = f"{predict_by:.0f}" if predict_by is not None else "-"
            write_progress(
                f"卡片命中 step={step+1} key={label} via=screen "
                f"fp={(hit.get('fp') or '')[:10]} bar_y={hit.get('bar_y')} "
                f"pred_by={pred_s} cum={cum:.0f} "
                f"id={hit.get('title_id') or '-'} "
                f"(no-scroll-needed={step==0})"
            )
            return hit, cum
        if predict_by is not None and 420 <= predict_by <= 1550:
            write_progress(
                f"本屏应可见 pred_bar_y={predict_by:.0f} col={column} "
                f"cum={cum:.0f} 但未命中，放宽位置再试（不下滚）"
            )
            for c in caps:
                cx = float(c.get("bar_x") or c.get("roi_x") or 0)
                col_ok = (column == "left" and cx < 700) or (
                    column == "right" and cx >= 700
                ) or not column
                if col_ok and abs(float(c.get("bar_y") or 0) - predict_by) <= 220:
                    write_progress(
                        f"卡片命中 step={step+1} key={label} via=pos-relaxed "
                        f"fp={(c.get('fp') or '')[:10]} bar_y={c.get('bar_y')}"
                    )
                    return c, cum
        if predict_by is not None and predict_by < 380:
            write_progress(
                f"预测已滚过 pred_by={predict_by:.0f} → 上滚补回 step={step+1}"
            )
            wheel(geom, hwnd, notches=seek_notches, down=False)
            time.sleep(seek_settle)
            cache.invalidate()
            continue
        _log_seek_miss(
            caps,
            label=label,
            fp=fp,
            title_id=title_id,
            card_uuid=card_uuid,
            column=column,
            predict_bar_y=predict_by,
            step=step,
        )
        wheel(geom, hwnd, notches=seek_notches, down=True)
        time.sleep(seek_settle)
        cache.invalidate()
    if session is not None:
        session.cum_scroll_y = cum
    return None, cum


def still_on_tietu_list(shot_path: Path) -> bool:
    """详情未打开时，仍能看到双列 #2F2F30 标题栏。"""
    try:
        import scan_waterfall_catalog as scan

        caps = scan.extract_cards_fast(shot_path, ocr_titles=False)
        return len(caps) >= 2
    except Exception:
        return False


def wait_detail(geom, title: str, idx: int) -> bool:
    for w in range(10):
        time.sleep(0.8)
        shot_w = grab(geom, f"collect-wait-{idx}-{w}.png")
        arr = np.array(Image.open(shot_w))
        result, _ = OCR(arr)
        texts = [t.strip() for b, t, c in (result or [])]
        joined = "".join(texts)
        if "正在加载" in joined:
            write_progress(f"进度 {idx} 等待中… 正在加载")
            continue
        # 仍停留在贴图双列列表 → 点击未生效
        if still_on_tietu_list(shot_w) and "喜欢作者" not in joined:
            write_progress(f"进度 {idx} 仍在贴图列表（点击可能未命中海报）")
            return False
        if any(k in joined for k in ("喜欢作者", "写留言", "阅读原文", "问AI", "更多相关")):
            write_progress(f"进度 {idx} 详情已打开")
            return True
        if title and title[:4] in joined and "贴图" not in texts:
            return True
    return False


def scroll_until_caption(geom, hwnd, title: str, max_steps: int = 25) -> bool:
    keyname = re.sub(r"\s+", "", title)
    for step in range(max_steps):
        shot = grab(geom, f"seek-{step:02d}.png")
        caps = ocr_captions(shot)
        for c in caps:
            if re.sub(r"\s+", "", c["text"]) == keyname or title[:4] in c["text"]:
                return True
        wheel(geom, hwnd, notches=6, down=True)
        time.sleep(0.3)
    return False


def collect_one(geom, hwnd, title: str, idx: int, total: int, results: list, link_map: dict, card: dict | None = None):
    pct = f"{(100.0 * idx / total):5.1f}%" if total else "?"
    import scan_waterfall_catalog as scan

    title_id = (card or {}).get("title_id") or scan.normalize_title_id(title)
    fp = (card or {}).get("fp") or ""
    card_uuid = (card or {}).get("card_uuid") or ""
    column = (card or {}).get("column") or ""
    global_bar_y = (card or {}).get("global_bar_y")
    seek_kw = dict(
        title_id=title_id,
        card_uuid=card_uuid,
        column=column,
        global_bar_y=float(global_bar_y) if global_bar_y is not None else None,
        cum_scroll_y=0.0,
    )
    # 视觉键优先（uuid/fp）；title 仅无视觉键时兜底
    use_id = bool(card_uuid) or bool(fp) or bool(title_id)
    write_progress(
        f"======== 采链 {idx}/{total} ({pct}) 「{title}」 "
        f"uuid={card_uuid[:8] if card_uuid else '-'} "
        f"id={title_id[:16] if title_id else '-'} fp={fp[:10] if fp else '-'} ========"
    )
    dismiss_stale_detail(hwnd, label=f"进度 {idx}/{total}")
    write_progress(f"进度 {idx}/{total} 确保贴图列表页…")
    ensure_tietu(geom, hwnd)
    if use_id:
        write_progress(f"进度 {idx}/{total} 按 card_uuid/指纹/位置查找（先本屏）…")
        hit, _cum = scroll_until_fingerprint(geom, hwnd, fp, **seek_kw)
        if not hit:
            write_progress(f"进度 {idx}/{total} 未找到，回顶后再找…")
            content_focus(geom, hwnd)
            for _ in range(20):
                wheel(geom, hwnd, notches=10, down=False)
                time.sleep(0.04)
            hit, _cum = scroll_until_fingerprint(geom, hwnd, fp, **seek_kw)
        if not hit:
            write_progress(
                f"进度 {idx}/{total} ✗ 未找到「{title_id or fp[:10]}」"
            )
            item = {
                "title": title,
                "link": "",
                "ok": False,
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "err": "id_not_found",
                "title_id": title_id,
                "fp": fp,
            }
            results.append(item)
            OUT.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
            return False
        write_progress(f"进度 {idx}/{total} 点击卡片（复用 seek 坐标，不再重识别）…")
        log_pre_click_inventory(
            [hit] if hit else [],
            hit,
            catalog_idx=idx,
            catalog_total=total,
        )
        ok_click, info = open_card_by_fingerprint(
            geom, hwnd, fp, target=hit, **seek_kw
        )
    else:
        write_progress(f"进度 {idx}/{total} 滚动查找标题…")
        if not scroll_until_caption(geom, hwnd, title):
            write_progress(f"进度 {idx}/{total} 未找到，回顶后再找一次…")
            content_focus(geom, hwnd)
            for _ in range(20):
                wheel(geom, hwnd, notches=10, down=False)
                time.sleep(0.05)
            if not scroll_until_caption(geom, hwnd, title):
                write_progress(f"进度 {idx}/{total} ✗ 未找到「{title}」")
                item = {
                    "title": title,
                    "link": "",
                    "ok": False,
                    "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "err": "not_found",
                }
                results.append(item)
                OUT.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
                return False
        write_progress(f"进度 {idx}/{total} 点击卡片图片区域…")
        ok_click, info = open_card(geom, hwnd, title)
    if not ok_click:
        write_progress(f"进度 {idx}/{total} ✗ 点击失败「{title}」 ({info})")
        item = {
            "title": title,
            "link": "",
            "ok": False,
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "err": str(info),
        }
        results.append(item)
        OUT.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        return False
    write_progress(f"进度 {idx}/{total} 已点击 @ {info}")

    write_progress(f"进度 {idx}/{total} 等待详情加载…")
    ready = wait_detail(geom, title, idx)
    if not ready:
        # 再点一次：往上偏一点，专打海报中上部
        sx, sy = info if isinstance(info, tuple) else (geom["L"] + 600, geom["T"] + 700)
        sy2 = max(geom["T"] + 450, sy - 80)
        write_progress(f"进度 {idx}/{total} 重试点击海报偏上 @ ({sx:.0f},{sy2:.0f})")
        focus(hwnd)
        click(sx, sy2)
        ready = wait_detail(geom, title, idx)
    if not ready:
        write_progress(f"进度 {idx}/{total} …详情仍未打开，跳过复制（避免误操作列表页）")
        item = {
            "title": title,
            "link": "",
            "ok": False,
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "err": "detail_not_open",
            "fp": fp,
        }
        results.append(item)
        OUT.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        return False
    write_progress(f"进度 {idx}/{total} 详情已就绪")
    grab(geom, f"collect-open-{idx}.png")

    write_progress(f"进度 {idx}/{total} 执行复制链接…")
    link = copy_link_chord(hwnd)
    if not re.search(r"mp\.weixin\.qq\.com/s/[A-Za-z0-9_\-]+", link or ""):
        write_progress(f"进度 {idx}/{total} …轮询未拿到链接，整段和弦再试一次")
        time.sleep(0.4)
        link = copy_link_chord(hwnd)
    m = re.search(r"https?://mp\.weixin\.qq\.com/s/[A-Za-z0-9_\-]+", link or "")
    if m:
        link = m.group(0)
    ok = bool(m)
    item = {
        "title": title,
        "link": link if ok else "",
        "ok": ok,
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    results.append(item)
    if ok:
        link_map[re.sub(r"\s+", "", title)] = link
    OUT.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    SEEN.write_text(json.dumps(sorted(link_map.keys()), ensure_ascii=False, indent=2), encoding="utf-8")

    mark = "✓" if ok else "✗"
    write_progress(f"进度 {idx}/{total} {mark} 「{title}」 {link if ok else '(无链接)'}")
    write_progress(f"进度 {idx}/{total} 关闭详情并回到贴图…")

    close_detail(hwnd)
    time.sleep(0.7)
    ensure_tietu(geom, hwnd)
    time.sleep(0.4)
    write_progress(f"进度 {idx}/{total} 本张结束，累计成功链接 {len(link_map)}")
    return ok


def collect_from_catalog(catalog: list[dict], limit: int | None = None) -> dict:
    """Click each catalog card and copy mp.weixin link. Returns link_map key->url."""
    geom = load_geom()
    hwnd = int(geom["H"])
    if limit is not None:
        catalog = catalog[: max(0, int(limit))]
    total = len(catalog)
    if total == 0:
        write_progress("未识别到任何贴图卡片，结束")
        return {}

    prior = []
    if OUT.exists():
        prior = json.loads(OUT.read_text(encoding="utf-8"))
    link_map: dict[str, str] = {}
    for p in prior:
        if p.get("ok") and p.get("link"):
            link_map[re.sub(r"\s+", "", p["title"])] = p["link"]

    results = [p for p in prior]
    write_progress(f"开始依次点击采集，共 {total} 张")

    ok_n = 0
    fail_n = 0
    for i, card in enumerate(catalog, 1):
        title = card.get("text") or card.get("title") or ""
        keyname = card.get("key") or card.get("fp") or re.sub(r"\s+", "", title)
        # also skip if title-keyed prior link exists
        title_key = re.sub(r"\s+", "", title)
        if keyname in link_map or title_key in link_map:
            link = link_map.get(keyname) or link_map.get(title_key)
            write_progress(f"进度 {i}/{total} ⏭ 已有链接，跳过「{title}」 {link}")
            ok_n += 1
            continue
        if collect_one(geom, hwnd, title, i, total, results, link_map, card=card):
            ok_n += 1
        else:
            fail_n += 1

    write_progress(f"采链完成：成功 {ok_n}/{total}，失败 {fail_n}")
    write_progress("结果文件: " + str(OUT))
    return link_map


def main():
    catalog = scan_catalog(load_geom(), int(load_geom()["H"]))
    collect_from_catalog(catalog)


if __name__ == "__main__":
    main()
