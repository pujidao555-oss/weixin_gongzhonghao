# -*- coding: utf-8 -*-
"""
贴图瀑布流总控：扫描目录 → 采集超链接 → 按链接下载图片集

CMD 推荐:
  run_tietu_pipeline.cmd
  run_tietu_pipeline.cmd --phase scan
  run_tietu_pipeline.cmd --phase scan --ocr-titles
  run_tietu_pipeline.cmd --phase link --limit 3
  run_tietu_pipeline.cmd --phase download
  run_tietu_pipeline.cmd --status

或:
  python -u run_tietu_pipeline.py --phase all
  # 默认扫描：CV 底栏计数+指纹去重（快）；加 --ocr-titles 才 OCR 标题

依赖微信窗口几何: tools/wx_geom.json
状态文件:        tools/tietu_state.json
进度日志:        tools/waterfall_progress.txt
下载目录:        C:\\Users\\boyaq\\Downloads\\<标题>\\
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

# Windows CMD 下尽量立刻刷出中文日志
os.environ.setdefault("PYTHONUNBUFFERED", "1")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass

ROOT = Path(__file__).resolve().parent
TOOLS = ROOT / "tools"
sys.path.insert(0, str(TOOLS))

STATE_PATH = TOOLS / "tietu_state.json"
CATALOG_CLEAN = TOOLS / "waterfall_catalog_clean.json"
CATALOG_RAW = TOOLS / "waterfall_catalog.json"
LINKS_PATH = TOOLS / "waterfall_links.json"
PROGRESS = TOOLS / "waterfall_progress.txt"
DOWNLOADS = Path.home() / "Downloads"

_T0 = time.time()


def elapsed() -> str:
    sec = int(time.time() - _T0)
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def pct(i: int, total: int) -> str:
    if total <= 0:
        return "0%"
    return f"{(100.0 * i / total):5.1f}%"


def bar(i: int, total: int, width: int = 20) -> str:
    if total <= 0:
        return "[" + ("-" * width) + "]"
    filled = int(width * i / total)
    return "[" + ("#" * filled) + ("-" * (width - filled)) + "]"


def log(msg: str, level: str = "INFO") -> None:
    line = f"[{time.strftime('%H:%M:%S')}|+{elapsed()}|{level}] {msg}"
    print(line, flush=True)
    try:
        prev = PROGRESS.read_text(encoding="utf-8") if PROGRESS.exists() else ""
        PROGRESS.write_text(prev + line + "\n", encoding="utf-8")
    except Exception as e:
        print(f"[LOG_WRITE_FAIL] {e}", flush=True)


def banner(title: str) -> None:
    line = "=" * 64
    log(line)
    log(title)
    log(line)


def now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def key_of(title: str) -> str:
    return re.sub(r"\s+", "", title or "")


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"items": [], "updated_at": ""}


def save_state(state: dict) -> None:
    state["updated_at"] = now()
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"状态已保存 → {STATE_PATH} （共 {len(state.get('items', []))} 条）")


def upsert_item(state: dict, title: str, **fields) -> dict:
    key = key_of(title)
    for it in state["items"]:
        if it.get("key") == key or key_of(it.get("title", "")) == key:
            it.update(fields)
            it["title"] = title or it.get("title") or ""
            it["key"] = key
            it["updated_at"] = now()
            return it
    item = {
        "title": title,
        "key": key,
        "link": "",
        "status": "listed",
        "download_dir": "",
        "image_count": 0,
        "updated_at": now(),
    }
    item.update(fields)
    state["items"].append(item)
    return item


def summarize_state(state: dict, prefix: str = "状态摘要") -> None:
    items = state.get("items") or []
    counts: dict[str, int] = {}
    for it in items:
        counts[it.get("status", "?")] = counts.get(it.get("status", "?"), 0) + 1
    linked = sum(1 for it in items if it.get("link"))
    log(f"{prefix}: 总计 {len(items)} | 有链接 {linked} | 分布 {counts}")


def merge_prior_links(state: dict) -> None:
    """Seed state from previous waterfall_links.json successes."""
    if not LINKS_PATH.exists():
        log(f"未找到历史链接文件: {LINKS_PATH}")
        return
    try:
        rows = json.loads(LINKS_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        log(f"读取历史链接失败: {e}", "WARN")
        return
    best: dict[str, str] = {}
    for r in rows:
        if r.get("ok") and r.get("link") and "mp.weixin.qq.com/s/" in r["link"]:
            best[key_of(r.get("title", ""))] = r["link"]
    log(f"历史链接文件 {len(rows)} 条记录，成功去重后 {len(best)} 条")
    for k, link in best.items():
        title = next((r.get("title") for r in rows if key_of(r.get("title", "")) == k), k)
        it = upsert_item(state, title, link=link, status="link_ok")
        if it.get("status") == "downloaded" and it.get("download_dir"):
            continue
        if it.get("status") not in ("downloaded", "dl_fail"):
            it["status"] = "link_ok"
            log(f"  注入链接: 「{title}」 → {link}")


def phase_scan(limit: int | None = None, ocr_titles: bool = False) -> list[dict]:
    import scan_waterfall_catalog as scan

    banner("Phase A / 扫描贴图瀑布流（CV 底栏快扫，默认不 OCR）")
    log(f"微信几何文件: {TOOLS / 'wx_geom.json'}")
    log(f"截图目录: {ROOT / 'observation_steps'}")
    log(f"ocr_titles={ocr_titles} （False=只数卡+指纹，更快）")
    t1 = time.time()
    cleaned = scan.main(ocr_titles=ocr_titles) or []
    log(f"扫描函数返回 {len(cleaned)} 条（耗时 {time.time()-t1:.1f}s）")
    if not cleaned and CATALOG_CLEAN.exists():
        cleaned = json.loads(CATALOG_CLEAN.read_text(encoding="utf-8"))
        log(f"回退读取清理目录: {CATALOG_CLEAN} → {len(cleaned)} 条")
    if limit is not None:
        log(f"--limit={limit}，截断目录 {len(cleaned)} → {limit}")
        cleaned = cleaned[: max(0, int(limit))]

    state = load_state()
    merge_prior_links(state)
    by_key = {it["key"]: it for it in state["items"]}
    new_items = []
    for i, c in enumerate(cleaned, 1):
        title = c.get("text") or c.get("title") or ""
        key = c.get("key") or key_of(title)
        old = by_key.get(key, {})
        status = old.get("status") or ("link_ok" if old.get("link") else "listed")
        new_items.append(
            {
                "title": title,
                "key": key,
                "link": old.get("link", ""),
                "status": status,
                "download_dir": old.get("download_dir", ""),
                "image_count": old.get("image_count", 0),
                "updated_at": now(),
            }
        )
        log(f"目录 {bar(i, len(cleaned))} {i}/{len(cleaned)} {pct(i, len(cleaned))} 「{title}」 [{status}]")
    state["items"] = new_items
    save_state(state)
    summarize_state(state, "Phase A 完成")
    log(f"原始目录: {CATALOG_RAW}")
    log(f"清理目录: {CATALOG_CLEAN}")
    return cleaned


def load_catalog_for_link() -> list[dict]:
    state = load_state()
    if state.get("items"):
        log(f"从状态文件加载目录 {len(state['items'])} 条")
        return [{"text": it["title"], "key": it["key"]} for it in state["items"]]
    if CATALOG_CLEAN.exists():
        data = json.loads(CATALOG_CLEAN.read_text(encoding="utf-8"))
        log(f"从清理目录加载 {len(data)} 条: {CATALOG_CLEAN}")
        return data
    if CATALOG_RAW.exists():
        import scan_waterfall_catalog as scan

        raw = json.loads(CATALOG_RAW.read_text(encoding="utf-8"))
        cleaned = scan.clean_catalog(raw)
        log(f"从原始目录清理后加载 {len(cleaned)} 条")
        return cleaned
    log("未找到任何目录数据", "WARN")
    return []


def phase_link(limit: int | None = None) -> None:
    import collect_waterfall_links as collect

    banner("Phase B / 采集超链接（点卡片 → … → ↓×3@0.5s → Enter）")
    catalog = load_catalog_for_link()
    if not catalog:
        log("无目录，请先运行 --phase scan", "ERROR")
        return
    if limit is not None:
        log(f"--limit={limit}，本阶段只处理前 {limit} 张")
        catalog = catalog[: max(0, int(limit))]

    state = load_state()
    merge_prior_links(state)
    already = sum(1 for c in catalog if (c.get("key") or key_of(c.get("text", ""))) in {
        it["key"] for it in state["items"] if it.get("link")
    })
    log(f"待采目录 {len(catalog)} 张，其中状态里已有链接约 {already} 张（将跳过）")
    for i, c in enumerate(catalog, 1):
        log(f"  待采清单 {i}/{len(catalog)} 「{c.get('text') or c.get('title')}」")
    save_state(state)

    t1 = time.time()
    log("开始调用 collect_from_catalog …")
    link_map = collect.collect_from_catalog(catalog, limit=None)
    log(f"collect_from_catalog 返回 {len(link_map)} 条链接（耗时 {time.time()-t1:.1f}s）")

    state = load_state()
    for i, card in enumerate(catalog, 1):
        title = card.get("text") or card.get("title") or ""
        key = card.get("key") or key_of(title)
        link = link_map.get(key, "")
        if link:
            upsert_item(state, title, link=link, status="link_ok")
            log(f"同步链接 {bar(i, len(catalog))} {i}/{len(catalog)} ✓ 「{title}」 {link}")
        else:
            log(f"同步链接 {bar(i, len(catalog))} {i}/{len(catalog)} … 「{title}」 本轮无新链接")

    if LINKS_PATH.exists():
        rows = json.loads(LINKS_PATH.read_text(encoding="utf-8"))
        latest: dict[str, dict] = {}
        for r in rows:
            latest[key_of(r.get("title", ""))] = r
        log(f"回读 {LINKS_PATH} 共 {len(rows)} 条，按标题取最新结果")
        for card in catalog:
            title = card.get("text") or ""
            key = card.get("key") or key_of(title)
            it = next((x for x in state["items"] if x["key"] == key), None)
            if it and it.get("link"):
                continue
            r = latest.get(key)
            if r and r.get("ok") and r.get("link"):
                upsert_item(state, title, link=r["link"], status="link_ok")
                log(f"  补录成功 「{title}」 {r['link']}")
            elif r and not r.get("ok"):
                upsert_item(state, title, link="", status="link_fail")
                log(f"  标记失败 「{title}」 err={r.get('err', '')}", "WARN")

    save_state(state)
    summarize_state(state, "Phase B 完成")


def phase_download(limit: int | None = None) -> None:
    from tie_tu_pipeline import download_by_url

    banner("Phase C / 按超链接下载图片集（不操作微信）")
    state = load_state()
    merge_prior_links(state)
    items = [it for it in state["items"] if it.get("link")]
    if not items and LINKS_PATH.exists():
        log("状态无链接，尝试从 waterfall_links.json 回填")
        for r in json.loads(LINKS_PATH.read_text(encoding="utf-8")):
            if r.get("ok") and r.get("link"):
                upsert_item(state, r["title"], link=r["link"], status="link_ok")
        items = [it for it in state["items"] if it.get("link")]
    if limit is not None:
        log(f"--limit={limit}，本阶段只下载前 {limit} 条有链接项")
        items = items[: max(0, int(limit))]
    if not items:
        log("无可用链接，请先运行 --phase link", "ERROR")
        return

    total = len(items)
    log(f"下载队列 {total} 条 → 根目录 {DOWNLOADS}")
    for i, it in enumerate(items, 1):
        log(f"  队列 {i}/{total} [{it.get('status')}] 「{it['title']}」 {it['link']}")

    ok_n = fail_n = skip_n = 0
    for i, it in enumerate(items, 1):
        title = it["title"]
        link = it["link"]
        existing = Path(it["download_dir"]) if it.get("download_dir") else DOWNLOADS / _safe(title)
        imgdir = existing / "images"
        head = f"下载 {bar(i, total)} {i}/{total} {pct(i, total)}"

        if it.get("status") == "downloaded" and imgdir.exists() and any(imgdir.glob("*")):
            n = len(list(imgdir.glob("*")))
            log(f"{head} ⏭ 已存在「{title}」 ({n} 张) @ {existing}")
            skip_n += 1
            continue

        log(f"{head} ↓ 开始「{title}」")
        log(f"       URL: {link}")
        t1 = time.time()
        try:
            out = download_by_url(link, title)
        except Exception as e:
            log(f"{head} ✗ 异常「{title}」: {e}", "ERROR")
            upsert_item(state, title, link=link, status="dl_fail")
            fail_n += 1
            save_state(state)
            continue

        dt = time.time() - t1
        if out and (out / "images").exists() and any((out / "images").glob("*")):
            files = sorted((out / "images").glob("*"))
            n = len(files)
            sizes = sum(f.stat().st_size for f in files)
            upsert_item(
                state,
                title,
                link=link,
                status="downloaded",
                download_dir=str(out),
                image_count=n,
            )
            ok_n += 1
            log(f"{head} ✓ 「{title}」 {n} 张 / {sizes/1024:.0f} KB / {dt:.1f}s")
            log(f"       目录: {out}")
            for f in files:
                log(f"         - {f.name} ({f.stat().st_size/1024:.1f} KB)")
        else:
            upsert_item(state, title, link=link, status="dl_fail")
            fail_n += 1
            log(f"{head} ✗ 「{title}」未解析到幻灯片图片（{dt:.1f}s）", "WARN")
        save_state(state)
        summarize_state(state, f"下载中累计")

    log(f"Phase C 完成：成功 {ok_n}，跳过 {skip_n}，失败 {fail_n}")
    summarize_state(state, "Phase C 最终")


def _safe(s: str) -> str:
    s = re.sub(r'[<>:"/\\|?*]', "_", s).strip()
    return s[:80] or "untitled"


def print_status() -> None:
    state = load_state()
    items = state.get("items") or []
    counts: dict[str, int] = {}
    for it in items:
        counts[it.get("status", "?")] = counts.get(it.get("status", "?"), 0) + 1
    banner("当前流水线状态")
    log(f"state 文件: {STATE_PATH}")
    log(f"updated_at: {state.get('updated_at', '')}")
    log(f"total items: {len(items)}")
    for k in sorted(counts):
        log(f"  {k}: {counts[k]}")
    linked = [it for it in items if it.get("link")]
    log(f"with link: {len(linked)}")
    for i, it in enumerate(items, 1):
        link = it.get("link") or ""
        short = (link[:56] + "…") if len(link) > 56 else link
        log(f"  {i:02d}. [{it.get('status')}] {it.get('title')} | {short}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="贴图瀑布流：扫描 → 采链 → 下载（CMD 友好，详细日志）"
    )
    parser.add_argument(
        "--phase",
        choices=["scan", "link", "download", "all"],
        default="all",
        help="执行阶段（默认 all）",
    )
    parser.add_argument("--limit", type=int, default=None, help="限制处理条数（调试用）")
    parser.add_argument(
        "--ocr-titles",
        action="store_true",
        help="扫描时对每个 #2F2F30 底栏再 OCR 标题（默认关闭，更快）",
    )
    parser.add_argument("--status", action="store_true", help="只打印状态摘要")
    parser.add_argument(
        "--seed-links",
        action="store_true",
        help="把已有目录/链接写入状态后退出",
    )
    args = parser.parse_args(argv)

    (ROOT / "observation_steps").mkdir(parents=True, exist_ok=True)
    TOOLS.mkdir(parents=True, exist_ok=True)

    banner("贴图瀑布流总控启动")
    log(f"Python: {sys.executable}")
    log(f"工作目录: {Path.cwd()}")
    log(f"脚本: {Path(__file__).resolve()}")
    log(
        f"参数: phase={args.phase} limit={args.limit} ocr_titles={args.ocr_titles} "
        f"status={args.status} seed={args.seed_links}"
    )
    log(f"进度日志文件: {PROGRESS}")

    if args.status:
        print_status()
        return 0

    if args.seed_links:
        state = load_state()
        if CATALOG_CLEAN.exists():
            cleaned = json.loads(CATALOG_CLEAN.read_text(encoding="utf-8"))
            log(f"载入清理目录 {len(cleaned)} 条")
            by_key = {it["key"]: it for it in state.get("items", [])}
            items = []
            for i, c in enumerate(cleaned, 1):
                title = c.get("text") or c.get("title") or ""
                key = c.get("key") or key_of(title)
                old = by_key.get(key, {})
                items.append(
                    {
                        "title": title,
                        "key": key,
                        "link": old.get("link", ""),
                        "status": old.get("status") or "listed",
                        "download_dir": old.get("download_dir", ""),
                        "image_count": old.get("image_count", 0),
                        "updated_at": now(),
                    }
                )
                log(f"  seed {i}/{len(cleaned)} 「{title}」")
            state["items"] = items
        merge_prior_links(state)
        save_state(state)
        log(f"已写入先验目录/链接 → {STATE_PATH}")
        print_status()
        return 0

    if args.phase in ("scan", "all"):
        phase_scan(limit=args.limit, ocr_titles=args.ocr_titles)
    if args.phase in ("link", "all"):
        phase_link(limit=args.limit)
    if args.phase in ("download", "all"):
        phase_download(limit=args.limit)

    banner("全部阶段结束")
    print_status()
    log(f"总耗时 {elapsed()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
