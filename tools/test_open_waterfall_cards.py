# -*- coding: utf-8 -*-
"""单独测试：贴图瀑布流快扫回顶 → 逐个打开卡片详情（不复制链接）。

用法:
  test_open_waterfall_cards.cmd
  test_open_waterfall_cards.cmd --limit 3
  test_open_waterfall_cards.cmd --skip-scan --limit 5
  test_open_waterfall_cards.cmd --no-replay   # 旧：点一张→小滚→重识别

默认 Phase2 = 混合回放：
  1) 按 Phase1 的 click_at_notches 滚到目标格（不强制滚完扫卡空滚）
  2) 点前轻校验本屏轮廓+fp；命中则用本屏实时坐标点
  3) 未命中则 ±1 格微调再校验；仍失败才回退旧 seek
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))

import collect_waterfall_links as collect  # noqa: E402
import scan_waterfall_catalog as scan  # noqa: E402

CATALOG_CLEAN = TOOLS / "waterfall_catalog_clean.json"
CATALOG_RAW = TOOLS / "waterfall_catalog.json"
OUT_JSON = TOOLS / "test_open_cards_result.json"
PROGRESS = TOOLS / "test_open_cards_progress.txt"

DETAIL_HOLD_S = 2.0
# 旧 seek 模式用（--no-replay）
POST_CLICK_SCROLL_NOTCHES = 1
SEEK_SCROLL_NOTCHES = 1
SEEK_MAX_STEPS = 40
# 混合回放：校验未命中时的微调格数（±1 交替）
REPLAY_CORRECT_MAX = 4
# 轻校验命中点须靠近扫卡记录坐标，否则视为误匹配（第2/5张同点根因）
VERIFY_MAX_DIST_PX = 160.0
# 点卡滚动：wheel=鼠标滚轮（默认）；key=尝试 VK_DOWN/UP（微信未必吃）
SCROLL_MODE = "wheel"


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with PROGRESS.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_catalog() -> list[dict]:
    path = CATALOG_CLEAN if CATALOG_CLEAN.exists() else CATALOG_RAW
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, list) else []


def _card_gy(card: dict):
    for k in ("global_bar_y", "global_y"):
        if card.get(k) is not None:
            try:
                return float(card[k])
            except (TypeError, ValueError):
                pass
    return None


def _force_redetect(geom, session: collect.OpenSession, tag: str) -> list:
    """清缓存并强制对本屏做整卡 CV（轮廓+底栏补漏+fp，不做标题 OCR）。"""
    session.cache.invalidate()
    _shot, _arr, caps = collect.get_screen_caps(
        geom,
        cache=session.cache,
        ocr_titles=False,
        shot_name=f"redetect-{tag}.png",
        force=True,
    )
    log(f"  强制重识别本屏：{len(caps)} 张卡片（轮廓+底栏补漏+fp，无 OCR）")
    return caps


def _pick_on_screen(
    caps: list,
    *,
    fp: str,
    title_id: str,
    card_uuid: str,
    column: str,
    global_bar_y,
    cum: float,
):
    predict = (
        float(global_bar_y) - float(cum) if global_bar_y is not None else None
    )
    return collect._pick_target_card(
        caps,
        fp=fp,
        title_id=title_id,
        card_uuid=card_uuid,
        column=column,
        predict_bar_y=predict,
    )


def backfill_top_replay_fields(catalog: list[dict]) -> int:
    """补打首屏顶卡回放字段。

    旧门槛曾拒掉 cy≈544 / roi_y≈402 的顶卡，导致 notches=0 漏记、点卡跳过顶部。
    对「明显在首屏顶部」且缺字段的卡，用目录里的 cx/cy 补 notches=0。
    """
    n = 0
    for c in catalog:
        if c.get("click_at_notches") is not None:
            continue
        cx = c.get("cx")
        cy = c.get("cy")
        gy = c.get("global_bar_y")
        if cx is None or cy is None:
            continue
        try:
            fcx, fcy = float(cx), float(cy)
            fgy = float(gy) if gy is not None else None
        except (TypeError, ValueError):
            continue
        # 首屏顶排：点击中心大约 500–650，全局 bar 大约 <800
        if fcy < 500 or fcy > 660:
            continue
        if fgy is not None and fgy > 820:
            continue
        c["click_at_notches"] = 0
        c["click_cx"] = fcx
        c["click_cy"] = fcy
        n += 1
    return n


def sort_for_replay(catalog: list[dict]) -> list[dict]:
    """按扫卡时记录的滚轮格数 → 同屏再按 y/x，保证回放顺序稳定。"""

    def key(c: dict):
        n = c.get("click_at_notches")
        n_key = int(n) if n is not None else 10**9
        cy = float(c.get("click_cy") or c.get("cy") or 0)
        cx = float(c.get("click_cx") or c.get("cx") or 0)
        return (n_key, cy, cx)

    return sorted(catalog, key=key)


def _scroll_steps(geom, hwnd, notches: int, down: bool) -> None:
    """点卡阶段滚动：默认鼠标滚轮；--scroll-key 时发方向键。"""
    n = abs(int(notches))
    if n <= 0:
        return
    if SCROLL_MODE == "key":
        collect.content_focus(geom, hwnd)
        vk = 0x28 if down else 0x26  # DOWN / UP
        for _ in range(n):
            collect.key(vk)
            time.sleep(0.08)
        return
    collect.wheel(geom, hwnd, notches=n, down=down)


def _scroll_to_notches(
    geom, hwnd, session: collect.OpenSession, target_n: int
) -> None:
    cur = int(session.wheel_notches_from_top)
    delta = int(target_n) - cur
    if delta == 0:
        log(f"  回放：已在 notches={cur}，无需滚动")
        return
    down = delta > 0
    n = abs(delta)
    how = "方向键" if SCROLL_MODE == "key" else "滚轮"
    log(f"  回放：{how}{'下' if down else '上'} {n} 格  ({cur} → {target_n})")
    _scroll_steps(geom, hwnd, n, down=down)
    session.wheel_notches_from_top = int(target_n)
    session.cum_scroll_y = float(session.wheel_notches_from_top) * 80.0
    time.sleep(0.35)
    session.cache.invalidate()


def _nudge_one_notch(geom, hwnd, session: collect.OpenSession, down: bool) -> None:
    _scroll_steps(geom, hwnd, 1, down=down)
    session.wheel_notches_from_top += 1 if down else -1
    session.wheel_notches_from_top = max(0, int(session.wheel_notches_from_top))
    session.cum_scroll_y = float(session.wheel_notches_from_top) * 80.0
    time.sleep(0.30)
    session.cache.invalidate()


def _dist(ax: float, ay: float, bx: float, by: float) -> float:
    dx = float(ax) - float(bx)
    dy = float(ay) - float(by)
    return (dx * dx + dy * dy) ** 0.5


def _resolve_verify_hit(
    caps: list,
    *,
    scored: dict | None,
    fp: str,
    column: str,
    rec_cx: float,
    rec_cy: float,
    exclude_fps: set[str],
) -> tuple[dict | None, str]:
    """用记录坐标约束轻校验，避免软指纹把第5张认成第2张。

    规则（只影响点卡回放，不改扫卡）:
      1) 排除已点过的 fp
      2) 打分命中且距记录坐标 ≤ VERIFY_MAX_DIST_PX → 采用
      3) 否则在本屏同列候选里选距记录最近且指纹够近者
      4) 仍过远 → None（改用记录坐标软点）
    """
    filtered = []
    for c in caps:
        cfp = str(c.get("fp") or "")
        if cfp and cfp in exclude_fps:
            continue
        filtered.append(c)

    if scored is not None:
        sfp = str(scored.get("fp") or "")
        if not (sfp and sfp in exclude_fps):
            d = _dist(scored.get("cx") or 0, scored.get("cy") or 0, rec_cx, rec_cy)
            if d <= VERIFY_MAX_DIST_PX:
                return scored, f"score_near dist={d:.0f}"

    best = None
    best_key = None  # (ham, dist)
    for c in filtered:
        cx = float(c.get("cx") or 0)
        cy = float(c.get("cy") or 0)
        if column:
            col_ok = (column == "left" and cx < 700) or (
                column == "right" and cx >= 700
            )
            if not col_ok:
                continue
        cfp = str(c.get("fp") or "")
        ham = collect._fp_hamming(cfp, fp) if fp and cfp else 64
        dist = _dist(cx, cy, rec_cx, rec_cy)
        # 位置优先：先要近，再看指纹
        key = (dist, ham)
        if best_key is None or key < best_key:
            best_key = key
            best = c
    if best is None or best_key is None:
        return None, "no_cand"
    dist, ham = best_key
    if dist <= VERIFY_MAX_DIST_PX and ham <= 22:
        return best, f"near_fp dist={dist:.0f} ham={ham}"
    if dist <= VERIFY_MAX_DIST_PX:
        return best, f"near_only dist={dist:.0f} ham={ham}"
    # 精确指纹但偏远：仍不信（多半是误配另一张），交给记录坐标
    if ham == 0 and dist <= VERIFY_MAX_DIST_PX * 2:
        return best, f"exact_fp dist={dist:.0f}"
    return None, f"reject dist={dist:.0f} ham={ham}"


def _verify_on_screen(
    geom,
    session: collect.OpenSession,
    *,
    tag: str,
    fp: str,
    title_id: str,
    card_uuid: str,
    column: str,
    global_bar_y,
    rec_cx: float,
    rec_cy: float,
    exclude_fps: set[str],
) -> tuple[dict | None, list, str]:
    """点前轻校验：本屏轮廓+fp，并用记录坐标防串卡。"""
    caps = _force_redetect(geom, session, tag=tag)
    scored = _pick_on_screen(
        caps,
        fp=fp,
        title_id=title_id,
        card_uuid=card_uuid,
        column=column,
        global_bar_y=global_bar_y,
        cum=session.cum_scroll_y,
    )
    hit, why = _resolve_verify_hit(
        caps,
        scored=scored,
        fp=fp,
        column=column,
        rec_cx=rec_cx,
        rec_cy=rec_cy,
        exclude_fps=exclude_fps,
    )
    return hit, caps, why


def open_one_replay(
    geom,
    hwnd,
    card: dict,
    idx: int,
    total: int,
    session: collect.OpenSession,
    clicked_keys: set[str],
    clicked_fps: set[str] | None = None,
) -> dict:
    """混合回放：notches 定位 → 轻校验本屏坐标 → 微调 → 必要时旧 seek。"""
    title = card.get("text") or card.get("title") or f"card_{idx}"
    title_id = card.get("title_id") or scan.normalize_title_id(title)
    fp = card.get("fp") or ""
    card_uuid = card.get("card_uuid") or ""
    column = card.get("column") or ""
    global_bar_y = _card_gy(card)
    key = card_uuid or fp or title_id or title
    if clicked_fps is None:
        clicked_fps = set()
    row = {
        "idx": idx,
        "title": title,
        "title_id": title_id,
        "fp": fp,
        "card_uuid": card_uuid,
        "ok": False,
        "err": "",
        "mode": "replay_hybrid",
        "click": None,
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    notches = card.get("click_at_notches")
    cx = card.get("click_cx")
    cy = card.get("click_cy")
    log(
        f"======== 打开 {idx}/{total} 「{title}」 [replay+verify] "
        f"uuid={card_uuid[:8] if card_uuid else '-'} "
        f"notches={notches} recorded=({cx},{cy}) ========"
    )
    if key in clicked_keys:
        row["err"] = "already_clicked"
        log("  ⏭ 本轮已点过，跳过")
        return row

    if notches is None or cx is None or cy is None:
        log("  无 click_at_notches/坐标 → 回退旧 seek 路径")
        row_old = open_one(geom, hwnd, card, idx, total, session, clicked_keys)
        row_old["mode"] = "seek_fallback"
        return row_old

    collect.dismiss_stale_detail(hwnd, label=f"打开 {idx}/{total}")
    collect.ensure_tietu(geom, hwnd)
    time.sleep(0.15)

    _scroll_to_notches(geom, hwnd, session, int(notches))

    exclude_fps = set(clicked_fps)

    hit, caps, why0 = _verify_on_screen(
        geom,
        session,
        tag=f"{idx:02d}-v0",
        fp=fp,
        title_id=title_id,
        card_uuid=card_uuid,
        column=column,
        global_bar_y=global_bar_y,
        rec_cx=float(cx),
        rec_cy=float(cy),
        exclude_fps=exclude_fps,
    )
    verify_how = "verify_ok"
    if hit is not None:
        inv = collect.format_pre_click_inventory(
            caps, hit, catalog_idx=idx, catalog_total=total
        )
        log(f"  {inv}")
        log(
            f"  轻校验采纳 → 本屏 "
            f"({hit.get('cx'):.0f},{hit.get('cy'):.0f}) "
            f"记录 ({float(cx):.0f},{float(cy):.0f}) [{why0}]"
        )
    else:
        log(f"  轻校验未采纳 [{why0}] → ±1 格微调再校验")
        for step in range(REPLAY_CORRECT_MAX):
            down = step % 2 == 0
            log(
                f"  微调 {step+1}/{REPLAY_CORRECT_MAX}: "
                f"{'下' if down else '上'} 1 格 "
                f"(now={session.wheel_notches_from_top})"
            )
            _nudge_one_notch(geom, hwnd, session, down=down)
            hit, caps, why = _verify_on_screen(
                geom,
                session,
                tag=f"{idx:02d}-v{step+1}",
                fp=fp,
                title_id=title_id,
                card_uuid=card_uuid,
                column=column,
                global_bar_y=global_bar_y,
                rec_cx=float(cx),
                rec_cy=float(cy),
                exclude_fps=exclude_fps,
            )
            if hit is not None:
                verify_how = f"verify_nudge_{step+1}"
                inv = collect.format_pre_click_inventory(
                    caps, hit, catalog_idx=idx, catalog_total=total
                )
                log(f"  {inv}")
                log(
                    f"  微调后采纳 @ "
                    f"({hit.get('cx'):.0f},{hit.get('cy'):.0f}) "
                    f"notches={session.wheel_notches_from_top} [{why}]"
                )
                break
        if hit is None:
            log(
                f"  微调仍无近邻命中 → 回到 notches={int(notches)}，"
                f"用扫卡记录坐标点（不做错误覆盖）"
            )
            _scroll_to_notches(geom, hwnd, session, int(notches))
            hit = {
                "cx": float(cx),
                "cy": float(cy),
                "fp": fp,
                "roi_x": card.get("roi_x"),
                "roi_y": card.get("roi_y"),
                "roi_w": card.get("roi_w"),
                "roi_h": card.get("roi_h"),
                "bar_y": card.get("bar_y"),
            }
            verify_how = "recorded_coords"
            log(
                f"  使用记录坐标点击目录 {idx}/{total} "
                f"@ ({float(cx):.0f},{float(cy):.0f})"
            )

    ok_click, info = collect.open_card_by_fingerprint(
        geom,
        hwnd,
        fp,
        target=hit,
        title_id=title_id,
        card_uuid=card_uuid,
        column=column,
        global_bar_y=float(global_bar_y) if global_bar_y is not None else None,
        cum_scroll_y=session.cum_scroll_y,
    )
    row["click"] = list(info) if isinstance(info, tuple) else info
    row["mode"] = f"replay_{verify_how}"
    if not ok_click:
        row["err"] = str(info)
        log(f"  ✗ 混合回放点击失败: {info} → 旧 seek")
        return open_one(geom, hwnd, card, idx, total, session, clicked_keys)

    log(f"  已点击 @ {info} via={verify_how}")
    clicked_keys.add(key)
    if fp:
        clicked_fps.add(fp)
    hit_fp = str((hit or {}).get("fp") or "")
    if hit_fp:
        clicked_fps.add(hit_fp)
    log(f"  等待 {DETAIL_HOLD_S:.0f}s 后关详情…")
    time.sleep(DETAIL_HOLD_S)
    collect.close_detail(hwnd, settle_s=0.45)
    collect.ensure_tietu(geom, hwnd)
    time.sleep(0.15)
    # 不点后盲滚、不追扫卡空滚；下一张用绝对 notches 对齐
    row["ok"] = True
    log(
        f"  ✓ 混合回放完成 ({idx}/{total}) "
        f"notches={session.wheel_notches_from_top} via={verify_how}"
    )
    return row


def open_one(
    geom,
    hwnd,
    card: dict,
    idx: int,
    total: int,
    session: collect.OpenSession,
    clicked_keys: set[str],
) -> dict:
    title = card.get("text") or card.get("title") or f"card_{idx}"
    title_id = card.get("title_id") or scan.normalize_title_id(title)
    fp = card.get("fp") or ""
    card_uuid = card.get("card_uuid") or ""
    column = card.get("column") or ""
    global_bar_y = _card_gy(card)
    key = card_uuid or fp or title_id or title
    row = {
        "idx": idx,
        "title": title,
        "title_id": title_id,
        "fp": fp,
        "card_uuid": card_uuid,
        "ok": False,
        "err": "",
        "mode": "seek",
        "click": None,
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    log(
        f"======== 打开 {idx}/{total} 「{title}」 "
        f"uuid={card_uuid[:8] if card_uuid else '-'} "
        f"col={column or '-'} gy={global_bar_y} "
        f"id={title_id[:16] if title_id else '-'} fp={fp[:10] if fp else '-'} "
        f"cum={session.cum_scroll_y:.0f} ========"
    )

    if key in clicked_keys:
        row["err"] = "already_clicked"
        log("  ⏭ 本轮已点过，跳过")
        return row

    collect.dismiss_stale_detail(hwnd, label=f"打开 {idx}/{total}")
    collect.ensure_tietu(geom, hwnd)
    time.sleep(0.2)

    if not card_uuid and not title_id and not fp:
        row["err"] = "no_id"
        log("  ✗ 无 card_uuid/指纹/标题识别码，跳过")
        return row

    hit = None
    for step in range(SEEK_MAX_STEPS):
        caps = _force_redetect(geom, session, tag=f"{idx:02d}-s{step:02d}")
        hit = _pick_on_screen(
            caps,
            fp=fp,
            title_id=title_id,
            card_uuid=card_uuid,
            column=column,
            global_bar_y=global_bar_y,
            cum=session.cum_scroll_y,
        )
        if hit is not None:
            inv = collect.format_pre_click_inventory(
                caps, hit, catalog_idx=idx, catalog_total=total
            )
            log(f"  {inv}")
            log(
                f"  本屏命中 step={step+1} fp={(hit.get('fp') or '')[:10]} "
                f"bar_y={hit.get('bar_y')}"
            )
            break
        log(
            f"  本屏未命中 → 小滚 {SEEK_SCROLL_NOTCHES} 格后再识别 "
            f"(seek {step+1}/{SEEK_MAX_STEPS})"
        )
        collect.wheel(geom, hwnd, notches=SEEK_SCROLL_NOTCHES, down=True)
        session.cum_scroll_y += float(SEEK_SCROLL_NOTCHES) * 80.0
        session.wheel_notches_from_top += int(SEEK_SCROLL_NOTCHES)
        time.sleep(0.35)
        session.cache.invalidate()

    if hit is None:
        row["err"] = "id_not_found"
        log("  ✗ 未找到卡片")
        return row

    ok_click, info = collect.open_card_by_fingerprint(
        geom,
        hwnd,
        fp,
        target=hit,
        title_id=title_id,
        card_uuid=card_uuid,
        column=column,
        global_bar_y=float(global_bar_y) if global_bar_y is not None else None,
        cum_scroll_y=session.cum_scroll_y,
    )
    row["click"] = list(info) if isinstance(info, tuple) else info
    if not ok_click:
        row["err"] = str(info)
        log(f"  ✗ 点击失败: {info}")
        return row
    log(f"  已点击 @ {info}")
    clicked_keys.add(key)

    log(f"  等待 {DETAIL_HOLD_S:.0f}s 后关详情（不校验是否加载完）…")
    time.sleep(DETAIL_HOLD_S)
    log("  关闭详情…")
    collect.close_detail(hwnd, settle_s=0.45)
    collect.ensure_tietu(geom, hwnd)
    time.sleep(0.2)

    log(f"  点后小滚 {POST_CLICK_SCROLL_NOTCHES} 格，准备重识别…")
    collect.wheel(geom, hwnd, notches=POST_CLICK_SCROLL_NOTCHES, down=True)
    session.cum_scroll_y += float(POST_CLICK_SCROLL_NOTCHES) * 80.0
    session.wheel_notches_from_top += int(POST_CLICK_SCROLL_NOTCHES)
    time.sleep(0.35)
    session.cache.invalidate()

    row["ok"] = True
    log(f"  ✓ 打开→{DETAIL_HOLD_S:.0f}s→关闭→小滚完成 ({idx}/{total})")
    return row


def main() -> int:
    ap = argparse.ArgumentParser(description="测试：快扫回顶后逐个打开贴图卡片（不复制链接）")
    ap.add_argument("--limit", type=int, default=None, help="只测前 N 张")
    ap.add_argument("--skip-scan", action="store_true", help="跳过扫描，直接用已有 catalog")
    ap.add_argument(
        "--no-ocr-titles",
        action="store_true",
        help="扫描时关闭标题 OCR（仅 aHash；默认开 OCR 作 title_id）",
    )
    ap.add_argument(
        "--method",
        choices=("bar", "card_roi"),
        default="card_roi",
        help="扫描方法：card_roi=整卡ROI（默认）；bar=旧底栏色块",
    )
    ap.add_argument(
        "--no-replay",
        action="store_true",
        help="关闭回放：改用旧逻辑（点一张→小滚→强制重识别）",
    )
    ap.add_argument(
        "--scroll-key",
        action="store_true",
        help="点卡滚动改用键盘↓/↑（实验；微信贴图页未必响应，默认仍用滚轮）",
    )
    args = ap.parse_args()
    ocr_titles = not args.no_ocr_titles
    method = args.method
    use_replay = not args.no_replay
    global SCROLL_MODE
    SCROLL_MODE = "key" if args.scroll_key else "wheel"

    PROGRESS.write_text("", encoding="utf-8")
    log("=" * 56)
    log(
        "测试脚本：瀑布流快扫回顶 → "
        + ("回放 notches+坐标点卡" if use_replay else "点一张→小滚→重识别")
        + "（无复制链接）"
    )
    log(
        f"参数 limit={args.limit} skip_scan={args.skip_scan} "
        f"ocr_titles={ocr_titles} method={method} replay={use_replay} "
        f"scroll={SCROLL_MODE} detail_hold={DETAIL_HOLD_S}s"
    )
    log("=" * 56)

    try:
        geom = scan.require_wechat_window(save_geom=True)
    except RuntimeError as exc:
        log(f"中止：{exc}")
        return 1
    hwnd = int(geom["H"])
    scan.focus(hwnd)

    if args.skip_scan:
        catalog = load_catalog()
        log(f"跳过扫描，读取目录 {len(catalog)} 条 ← {CATALOG_CLEAN}")
        stamped = sum(1 for c in catalog if c.get("click_at_notches") is not None)
        log(f"目录内含回放字段 {stamped}/{len(catalog)} 张")
        if use_replay and stamped == 0:
            log("⚠ 旧目录无 click_at_notches，请去掉 --skip-scan 重新扫一次")
    else:
        mode_txt = (
            "整卡ROI+底栏OCR+指纹"
            if method == "card_roi"
            else "底栏+宽度+标题色+标题识别码"
        )
        log(f"Phase1: {mode_txt}，滚到底并回顶（追加回放字段）…")
        catalog = scan.main(ocr_titles=ocr_titles, method=method) or []
        log(f"Phase1 完成：共 {len(catalog)} 张")
        geom = scan.load_geom()
        hwnd = int(geom["H"])

    if not catalog:
        log("目录为空，结束")
        return 1

    if args.limit is not None:
        catalog = catalog[: max(0, int(args.limit))]
        log(f"--limit → 本轮只打开 {len(catalog)} 张")

    if use_replay:
        filled = backfill_top_replay_fields(catalog)
        if filled:
            log(f"回放补打：为首屏顶卡补 click_at_notches=0 × {filled}")
        catalog = sort_for_replay(catalog)
        n0 = sum(1 for c in catalog if c.get("click_at_notches") == 0)
        log(
            f"Phase2[replay+verify]: 按 notches 定位→轻校验本屏坐标→必要时微调，"
            f"共 {len(catalog)} 张（其中 notches=0 首屏 {n0} 张；"
            f"不追扫卡空滚到底）"
        )
        for i, c in enumerate(catalog[:8], 1):
            log(
                f"  回放序{i}: notches={c.get('click_at_notches')} "
                f"click=({c.get('click_cx')},{c.get('click_cy')}) "
                f"「{(c.get('text') or '')[:18]}」"
            )
    else:
        log(
            f"Phase2[seek]: 点一张→关详情→小滚{POST_CLICK_SCROLL_NOTCHES}格→重识别，"
            f"共 {len(catalog)} 张"
        )

    session = collect.OpenSession()
    clicked_keys: set[str] = set()
    clicked_fps: set[str] = set()
    results = []
    ok_n = fail_n = 0
    t0 = time.time()
    for i, card in enumerate(catalog, 1):
        if use_replay:
            r = open_one_replay(
                geom,
                hwnd,
                card,
                i,
                len(catalog),
                session,
                clicked_keys,
                clicked_fps=clicked_fps,
            )
        else:
            r = open_one(geom, hwnd, card, i, len(catalog), session, clicked_keys)
        results.append(r)
        if r["ok"]:
            ok_n += 1
        else:
            fail_n += 1
        OUT_JSON.write_text(
            json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        log(
            f"累计 成功 {ok_n} / 失败 {fail_n} / 已测 {i}/{len(catalog)} "
            f"| notches={session.wheel_notches_from_top}"
        )

    dt = time.time() - t0
    log("=" * 56)
    log(f"测试结束：成功 {ok_n}，失败 {fail_n}，耗时 {dt:.1f}s")
    log(f"结果: {OUT_JSON}")
    log(f"进度: {PROGRESS}")
    for r in results:
        mark = "✓" if r["ok"] else "✗"
        log(
            f"  {mark} {r['idx']:02d} [{r.get('mode','')}] "
            f"{r['title']}  {r.get('err') or ''}"
        )
    log("=" * 56)
    return 0 if fail_n == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
