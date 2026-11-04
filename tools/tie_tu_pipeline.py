# -*- coding: utf-8 -*-
"""OCR waterfall -> click card -> copy link -> download slideshow -> close.
Keeps WeChat window geometry fixed (no move/resize).
"""
from __future__ import annotations

import ctypes
import json
import re
import time
from ctypes import wintypes
from pathlib import Path

import numpy as np
import requests
from PIL import Image, ImageGrab
from rapidocr_onnxruntime import RapidOCR

USER32 = ctypes.windll.user32
KERNEL32 = ctypes.windll.kernel32
USER32.SetProcessDPIAware()

MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_WHEEL = 0x0800
KEYEVENTF_KEYUP = 0x0002
VK_CONTROL = 0x11
VK_ESCAPE = 0x1B
VK_C = 0x43
VK_V = 0x56

ROOT = Path(__file__).resolve().parents[1]
OBS = ROOT / "observation_steps"
TOOLS = Path(__file__).resolve().parent
GEOM = TOOLS / "wx_geom.json"
STATE = TOOLS / "tie_tu_seen.json"
DOWNLOADS = Path.home() / "Downloads"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 MicroMessenger/8.0.43"
    ),
    "Referer": "https://mp.weixin.qq.com/",
}


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


def load_geom():
    data = json.loads(GEOM.read_text(encoding="utf-8-sig"))
    return data


def focus_wx(hwnd: int):
    USER32.SetForegroundWindow(hwnd)
    time.sleep(0.35)


def click(x: int, y: int, button="left"):
    USER32.SetCursorPos(int(x), int(y))
    time.sleep(0.05)
    if button == "left":
        USER32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        time.sleep(0.03)
        USER32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    else:
        USER32.mouse_event(MOUSEEVENTF_RIGHTDOWN, 0, 0, 0, 0)
        time.sleep(0.03)
        USER32.mouse_event(MOUSEEVENTF_RIGHTUP, 0, 0, 0, 0)


def key_tap(vk: int):
    USER32.keybd_event(vk, 0, 0, 0)
    time.sleep(0.03)
    USER32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)


def key_combo(mod: int, vk: int):
    USER32.keybd_event(mod, 0, 0, 0)
    time.sleep(0.03)
    USER32.keybd_event(vk, 0, 0, 0)
    time.sleep(0.03)
    USER32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
    time.sleep(0.03)
    USER32.keybd_event(mod, 0, KEYEVENTF_KEYUP, 0)


def wheel_down(notches=3):
    delta = ctypes.c_uint((-120 * notches) & 0xFFFFFFFF).value
    for _ in range(abs(notches)):
        USER32.mouse_event(MOUSEEVENTF_WHEEL, 0, 0, delta if notches > 0 else ctypes.c_uint((120) & 0xFFFFFFFF).value, 0)
        time.sleep(0.08)


def grab(geom, name: str) -> Path:
    box = (geom["L"], geom["T"], geom["R"], geom["B"])
    img = ImageGrab.grab(bbox=box)
    path = OBS / name
    img.save(path)
    return path


def get_clipboard_text() -> str:
    import tkinter as tk

    r = tk.Tk()
    r.withdraw()
    try:
        return r.clipboard_get()
    except Exception:
        return ""
    finally:
        r.destroy()


def set_clipboard_text(text: str):
    import tkinter as tk

    r = tk.Tk()
    r.withdraw()
    r.clipboard_clear()
    r.clipboard_append(text)
    r.update()
    r.destroy()


def ocr_captions(img_path: Path):
    ocr = RapidOCR()
    img = np.array(Image.open(img_path))
    result, _ = ocr(img)
    caps = []
    skip = ("关注", "私信", "全部", "贴图", "展开", "山西恒泰", "忻州", "承揽", "压机", "赞")
    for box, text, conf in result or []:
        t = text.strip()
        ys = [p[1] for p in box]
        xs = [p[0] for p in box]
        cy = sum(ys) / len(ys)
        cx = sum(xs) / len(xs)
        cn = len(re.findall(r"[\u4e00-\u9fff]", t))
        if float(conf) < 0.55 or cy < 450 or cn < 4 or len(t) > 28:
            continue
        if any(s in t for s in skip):
            continue
        caps.append(
            {
                "text": t,
                "cx": round(cx),
                "cy": round(cy),
                "x0": round(min(xs)),
                "y0": round(min(ys)),
                "x1": round(max(xs)),
                "y1": round(max(ys)),
                "conf": round(float(conf), 3),
            }
        )
    # dedupe by text keep first
    seen = set()
    uniq = []
    for c in sorted(caps, key=lambda z: (z["cy"], z["cx"])):
        k = re.sub(r"\s+", "", c["text"])
        if k in seen:
            continue
        seen.add(k)
        uniq.append(c)
    return uniq


def parse_slide_urls(html: str):
    best = []
    for m in re.finditer(r"picture_page_info_list\s*[:=]\s*\[", html):
        arr_start = html.find("[", m.start())
        depth = 0
        arr_end = arr_start
        for ci in range(arr_start, min(arr_start + 300000, len(html))):
            if html[ci] == "[":
                depth += 1
            elif html[ci] == "]":
                depth -= 1
                if depth == 0:
                    arr_end = ci + 1
                    break
        arr = html[arr_start:arr_end]
        if len(arr) < 50:
            continue
        all_cdn = re.findall(r"cdn_url:\s*'([^']+)'", arr)
        wm = set(re.findall(r"watermark_info\s*:\s*\{[\s\S]*?cdn_url:\s*'([^']+)'", arr))
        cand = []
        for u in all_cdn:
            if u in wm:
                continue
            u = u.replace("\\x26amp;", "&").replace("&amp;", "&").replace("\\x26", "&")
            if "mmbiz.qpic.cn" in u:
                cand.append(u)
        if len(cand) > len(best):
            best = cand
    out, seen = [], set()
    for u in best:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def download_by_url(url: str, title: str) -> Path | None:
    print(f"[DL] 拉取文章 HTML: {title}", flush=True)
    print(f"[DL] URL: {url}", flush=True)
    try:
        resp = requests.get(url, headers=HEADERS, timeout=25)
        resp.raise_for_status()
        html = resp.text
        print(f"[DL] HTML 字节数={len(html)} status={resp.status_code}", flush=True)
    except Exception as e:
        print("FETCH_FAIL", e, flush=True)
        return None
    urls = parse_slide_urls(html)
    print(f"[DL] 解析 picture_page_info_list → {len(urls)} 张图", flush=True)
    if not urls:
        print("[DL] 未找到幻灯片 cdn_url", flush=True)
        return None
    out = DOWNLOADS / safe_name(title)
    imgdir = out / "images"
    imgdir.mkdir(parents=True, exist_ok=True)
    print(f"[DL] 保存目录: {imgdir}", flush=True)
    ok = 0
    for i, u in enumerate(urls, 1):
        ext = ".png" if "wx_fmt=png" in u else ".jpg"
        path = imgdir / f"{i:03d}{ext}"
        try:
            print(f"[DL] ({i}/{len(urls)}) GET {u[:90]}…", flush=True)
            r = requests.get(u, headers={**HEADERS, "Referer": "https://mp.weixin.qq.com/"}, timeout=30)
            r.raise_for_status()
            path.write_bytes(r.content)
            ok += 1
            print(f"[DL] ({i}/{len(urls)}) OK {path.name} {len(r.content)} bytes", flush=True)
        except Exception as e:
            print(f"[DL] ({i}/{len(urls)}) FAIL {e}", flush=True)
    (out / f"{safe_name(title)}.md").write_text(
        f"# {title}\n\n**原文链接**: {url}\n\n", encoding="utf-8"
    )
    print(f"[DL] 完成「{title}」成功 {ok}/{len(urls)}", flush=True)
    return out if ok else None


def safe_name(s: str) -> str:
    s = re.sub(r'[<>:"/\\|?*]', "_", s).strip()
    return s[:80] or "untitled"


def load_seen() -> set[str]:
    if STATE.exists():
        return set(json.loads(STATE.read_text(encoding="utf-8")))
    return set()


def save_seen(seen: set[str]):
    STATE.write_text(json.dumps(sorted(seen), ensure_ascii=False, indent=2), encoding="utf-8")


def try_copy_link(geom) -> str:
    """Open ... menu near top-right of WeChat content and click 复制链接 if present."""
    # clear clipboard marker
    set_clipboard_text("ABOUT_TO_COPY_LINK")
    # click more button (approximate top-right inside OA view)
    more_x = geom["L"] + int((geom["R"] - geom["L"]) * 0.92)
    more_y = geom["T"] + 42
    click(more_x, more_y)
    time.sleep(0.8)
    shot = grab(geom, "b-pipeline-menu.png")
    ocr = RapidOCR()
    img = np.array(Image.open(shot))
    result, _ = ocr(img)
    target = None
    for box, text, conf in result or []:
        t = text.strip()
        if "复制链接" in t or t == "复制":
            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
            target = (geom["L"] + sum(xs) / len(xs), geom["T"] + sum(ys) / len(ys))
            print("MENU_HIT", t, target)
            break
    if not target:
        # try share icon area then copy
        print("NO_COPY_LINK_IN_MENU")
        key_tap(VK_ESCAPE)
        time.sleep(0.3)
        return ""
    click(int(target[0]), int(target[1]))
    time.sleep(0.6)
    text = get_clipboard_text().strip()
    if "mp.weixin.qq.com" in text:
        return text
    # sometimes need Ctrl+C already done by menu; read again
    time.sleep(0.3)
    text = get_clipboard_text().strip()
    return text if "mp.weixin.qq.com" in text else ""


def close_detail(geom):
    # 关闭 = … 坐标的相对坐标（Δx=+40, Δy=0）
    dots = (777, 38)
    close_xy = (dots[0] + 40, dots[1] + 0)
    click(*close_xy)
    time.sleep(0.8)
    # escape fallback
    key_tap(VK_ESCAPE)
    time.sleep(0.4)


def ensure_tie_tu(geom, hwnd):
    focus_wx(hwnd)
    # click 贴图 tab roughly
    click(geom["L"] + 532, geom["T"] + 424)
    time.sleep(1.0)


def process_one(geom, hwnd, cap, seen: set[str]) -> bool:
    title = cap["text"]
    key = re.sub(r"\s+", "", title)
    if key in seen:
        print("SKIP_SEEN", title)
        return False
    focus_wx(hwnd)
    # click image body above caption
    sx = geom["L"] + cap["cx"]
    sy = geom["T"] + max(cap["cy"] - 180, 500)
    print("CLICK", title, sx, sy)
    click(sx, sy)
    time.sleep(2.2)
    grab(geom, f"b-pipeline-open-{safe_name(title)}.png")

    link = try_copy_link(geom)
    print("LINK", link[:120] if link else "")
    out = None
    if link:
        out = download_by_url(link, title)
    if out:
        print("SAVED", out)
        seen.add(key)
        save_seen(seen)
    else:
        print("DOWNLOAD_FAIL_KEEP_UNSEEN")

    close_detail(geom)
    time.sleep(1.0)
    grab(geom, f"b-pipeline-closed-{safe_name(title)}.png")
    # ensure back on 贴图
    ensure_tie_tu(geom, hwnd)
    return bool(out)


def main(max_cards: int = 2):
    geom = load_geom()
    hwnd = int(geom["H"])
    seen = load_seen()
    print("SEEN", seen)
    ensure_tie_tu(geom, hwnd)
    shot = grab(geom, "b-pipeline-start.png")
    caps = ocr_captions(shot)
    print("CAPS", caps)
    done = 0
    for cap in caps:
        if done >= max_cards:
            break
        if process_one(geom, hwnd, cap, seen):
            done += 1
    # if need more, scroll and continue once
    if done < max_cards:
        focus_wx(hwnd)
        click(geom["L"] + 480, geom["T"] + 700)
        time.sleep(0.2)
        # wheel down
        for _ in range(8):
            USER32.mouse_event(MOUSEEVENTF_WHEEL, 0, 0, ctypes.c_uint((-120) & 0xFFFFFFFF).value, 0)
            time.sleep(0.1)
        time.sleep(1.2)
        shot2 = grab(geom, "b-pipeline-scrolled.png")
        caps2 = ocr_captions(shot2)
        print("CAPS2", caps2)
        for cap in caps2:
            if done >= max_cards:
                break
            if process_one(geom, hwnd, cap, seen):
                done += 1
    print("DONE_CARDS", done)
    print("SEEN_NOW", load_seen())


if __name__ == "__main__":
    main(2)
