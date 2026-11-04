# -*- coding: utf-8 -*-
"""单独测试：贴图瀑布流快扫回顶 → 逐个打开卡片详情（不复制链接）。

用法:
  test_open_waterfall_cards.cmd
  test_open_waterfall_cards.cmd --limit 3
  test_open_waterfall_cards.cmd --skip-scan --limit 5
  python -u tools\\test_open_waterfall_cards.py --limit 3

流程:
  1) Phase1: scan_waterfall_catalog（滚到底 + 指纹/Track + 回顶）
  2) 按目录：对准滚动 →（仅画面变化时）识别本屏 → 点海报
     → 等约 2s 关详情（不等待详情加载完成）→ 继续下一张
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

# 点击后等待再关详情（秒）——测试加速，不校验详情是否加载完
DETAIL_HOLD_S = 2.0


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with PROGRESS.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_catalog() -> list[dict]:
    path = CATALOG_CLEAN if CATALOG_CLEAN.exists() else CATALOG_RAW
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def _card_gy(card: dict) -> float | None:
    if card.get("global_bar_y") is not None:
        return float(card["global_bar_y"])
    if card.get("bar_y") is not None:
        return float(card["bar_y"])
    return None


def open_one(
    geom,
    hwnd,
    card: dict,
    idx: int,
    total: int,
    session: collect.OpenSession,
) -> dict:
    title = card.get("text") or card.get("title") or f"card_{idx}"
    title_id = card.get("title_id") or scan.normalize_title_id(title)
    fp = card.get("fp") or ""
    card_uuid = card.get("card_uuid") or ""
    column = card.get("column") or ""
    global_bar_y = _card_gy(card)
    row = {
        "idx": idx,
        "title": title,
        "title_id": title_id,
        "fp": fp,
        "card_uuid": card_uuid,
        "ok": False,
        "err": "",
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

    # 打开前先点 … 右侧关闭，防止上一张详情未关挡住列表
    collect.dismiss_stale_detail(hwnd, label=f"打开 {idx}/{total}")
    collect.ensure_tietu(geom, hwnd)
    time.sleep(0.2)
    session.cache.invalidate()

    if not card_uuid and not title_id and not fp:
        row["err"] = "no_id"
        log(f"  ✗ 无 card_uuid/指纹/标题识别码，跳过")
        return row

    # 先按目录 global_y 对准滚动（不跑整卡 OCR），避免下一张仍停在已点过的屏
    if global_bar_y is not None:
        collect.scroll_toward_global_y(
            geom, hwnd, session, float(global_bar_y)
        )

    seek_kw = dict(
        title_id=title_id,
        card_uuid=card_uuid,
        column=column,
        global_bar_y=float(global_bar_y) if global_bar_y is not None else None,
        cum_scroll_y=session.cum_scroll_y,
        session=session,
    )
    hit, cum = collect.scroll_until_fingerprint(
        geom, hwnd, fp, max_steps=30, **seek_kw
    )
    session.cum_scroll_y = cum
    if not hit:
        log(f"  …回顶后再找")
        collect.content_focus(geom, hwnd)
        for _ in range(22):
            collect.wheel(geom, hwnd, notches=10, down=False)
            time.sleep(0.04)
        session.cum_scroll_y = 0.0
        session.cache.invalidate()
        seek_kw["cum_scroll_y"] = 0.0
        hit, cum = collect.scroll_until_fingerprint(
            geom, hwnd, fp, max_steps=30, **seek_kw
        )
        session.cum_scroll_y = cum
    if not hit:
        row["err"] = "id_not_found"
        log(f"  ✗ 未找到卡片")
        return row

    # 点击前：本屏几张 / 点第几张 / 还剩几张后滚动
    inv = collect.log_pre_click_inventory(
        session.cache.caps, hit, catalog_idx=idx, catalog_total=total
    )
    log(f"  {inv}")

    ok_click, info = collect.open_card_by_fingerprint(
        geom, hwnd, fp, target=hit, **{
            k: v for k, v in seek_kw.items() if k != "session"
        }
    )
    row["click"] = list(info) if isinstance(info, tuple) else info
    if not ok_click:
        row["err"] = str(info)
        log(f"  ✗ 点击失败: {info}")
        return row
    log(f"  已点击 @ {info}")

    # 测试加速：不等待详情加载，固定 hold 后关
    log(f"  等待 {DETAIL_HOLD_S:.0f}s 后关详情（不校验是否加载完）…")
    time.sleep(DETAIL_HOLD_S)
    log(f"  关闭详情…")
    collect.close_detail(hwnd, settle_s=0.45)
    session.cache.invalidate()
    collect.ensure_tietu(geom, hwnd)
    time.sleep(0.25)
    session.cache.invalidate()
    row["ok"] = True
    log(f"  ✓ 打开→{DETAIL_HOLD_S:.0f}s→关闭完成 ({idx}/{total})")
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
    args = ap.parse_args()
    ocr_titles = not args.no_ocr_titles
    method = args.method

    PROGRESS.write_text("", encoding="utf-8")
    log("=" * 56)
    log("测试脚本：瀑布流快扫回顶 → 逐个打开卡片（无复制链接）")
    log(
        f"参数 limit={args.limit} skip_scan={args.skip_scan} "
        f"ocr_titles={ocr_titles} method={method} "
        f"CARD_W=[{scan.CARD_W_MIN},{scan.CARD_W_MAX}] "
        f"detail_hold={DETAIL_HOLD_S}s"
    )
    log("=" * 56)

    geom = scan.load_geom()
    hwnd = int(geom["H"])
    scan.focus(hwnd)

    if args.skip_scan:
        catalog = load_catalog()
        log(f"跳过扫描，读取目录 {len(catalog)} 条 ← {CATALOG_CLEAN}")
    else:
        mode_txt = (
            "整卡ROI+底栏OCR+指纹"
            if method == "card_roi"
            else "底栏+宽度+标题色+标题识别码"
        )
        log(f"Phase1: {mode_txt}，滚到底并回顶…")
        catalog = scan.main(ocr_titles=ocr_titles, method=method) or []
        log(f"Phase1 完成：共 {len(catalog)} 张")

    if not catalog:
        log("目录为空，结束")
        return 1

    if args.limit is not None:
        catalog = catalog[: max(0, int(args.limit))]
        log(f"--limit → 本轮只打开 {len(catalog)} 张")

    log(f"Phase2: 开始逐个打开，共 {len(catalog)} 张（不复制链接）")
    session = collect.OpenSession()
    results = []
    ok_n = fail_n = 0
    t0 = time.time()
    for i, card in enumerate(catalog, 1):
        # 进入本张前若目录 y 更深，scroll_toward 会先下滚（解决 8→9 不滚）
        r = open_one(geom, hwnd, card, i, len(catalog), session)
        results.append(r)
        if r["ok"]:
            ok_n += 1
        else:
            fail_n += 1
        OUT_JSON.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        log(
            f"累计 成功 {ok_n} / 失败 {fail_n} / 已测 {i}/{len(catalog)} "
            f"| cum={session.cum_scroll_y:.0f}"
        )

    dt = time.time() - t0
    log("=" * 56)
    log(f"测试结束：成功 {ok_n}，失败 {fail_n}，耗时 {dt:.1f}s")
    log(f"结果: {OUT_JSON}")
    log(f"进度: {PROGRESS}")
    for r in results:
        mark = "✓" if r["ok"] else "✗"
        log(f"  {mark} {r['idx']:02d} {r['title']}  {r.get('err') or ''}")
    log("=" * 56)
    return 0 if fail_n == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
