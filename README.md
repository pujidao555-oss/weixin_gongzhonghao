# weixin_gongzhonghao

微信公众号「贴图」瀑布流本机流水线：**扫卡目录 → 采集超链接 → 按链接下载图片集**。

> 同步自本机 `wx_gzh` 可用版本（2026-11-04）。轮廓特征是代码写死的几何/色块/ROI 规则，不是开跑前先看截图再总结；每帧同一套规则现检，滚动中用 Track 配准。

## 三脚本对应关系

| 编号 | 入口 | 作用 |
|------|------|------|
| **脚本1** | `tools/scan_waterfall_catalog.py` | 扫瀑布流：数卡、定位、指纹/Track、回顶，写出目录 |
| **脚本2** | `tools/collect_waterfall_links.py` | 按目录找卡、打开详情、复制 `mp.weixin` 链接 |
| **脚本3** | `run_tietu_pipeline.py --phase download` + `tools/tie_tu_pipeline.py` | **根据链接下载图片集**（不操作微信） |

总控（可一次跑完三段）：

```bat
run_tietu_pipeline.cmd
run_tietu_pipeline.cmd --phase scan
run_tietu_pipeline.cmd --phase link --limit 3
run_tietu_pipeline.cmd --phase download
run_tietu_pipeline.cmd --status
```

辅助测试（**不是脚本3**）：`test_open_waterfall_cards.cmd` — 只验证打开/关闭卡片，不复制链接、不下载。

依赖模块：`card_roi.py` / `scroll_tracker.py` / `card_tracker.py` / `visual_matcher.py`

---

## 环境

- Windows + 已登录微信 PC，打开目标公众号资料页「贴图」
- Python 3.10+ 建议

```bat
pip install -r requirements.txt
copy tools\wx_geom.example.json tools\wx_geom.json
```

按本机窗口改 `tools/wx_geom.json`（`L/T/R/B/H`）。  
图片默认下到 `%USERPROFILE%\Downloads\<标题>\images\`。

任务结束后 CMD 会暂停，按任意键再关。

---

## 脚本1：扫目录（`scan_waterfall_catalog.py`）

### 目标

准确得到贴图卡片**数量与位置**（标题 OCR 第二优先）。

### 主流程

1. 误入详情则点「…」右侧关闭（`DOTS+(40,0)`）
2. 确认资料页并尽量点「贴图」
3. 向下扫描：截屏 → 识别 → 滚轮；Track 确认后可补标题 OCR
4. **原路回顶**：按下行净滚轮格数上滚（速度×2）

### 规则要点

- 默认 `card_roi`：整卡轮廓 → `#2F2F30` 底栏 → aHash `fp`；`--method bar` 可回退
- 卡宽约 `[260,310]`；列轨道；`poster_h` 区分半截/可确认
- 约 **1 秒 1 格** 下滚；连续无新增判到底
- Track：`delta_y` 配准 + uuid 确认；同名靠 fp/uuid/列+global_y

### 产出

`tools/waterfall_catalog.json`、`waterfall_catalog_clean.json`、`waterfall_progress.txt`

---

## 脚本2：采链（`collect_waterfall_links.py`）

### 目标

按目录逐张打开详情，复制 `mp.weixin.qq.com/s/...` 链接。

### 主流程

打开前关残留详情 → 贴图列表 → 指纹/位置 seek → 点海报 → 等详情 → 复制链接 → 关闭

### 匹配优先级

`card_uuid` → 精确/软 `fp` → 列+预测 `bar_y` → 标题只加分

### 其它

- 本屏缓存：画面未变不重识别
- 点下一张前 `scroll_toward` 最多 **10** 次；到底无位移提前停

### 产出

`tools/waterfall_links.json`、`waterfall_seen.json`

---

## 脚本3：按链接下载图片集

### 入口

```bat
run_tietu_pipeline.cmd --phase download
python -u run_tietu_pipeline.py --phase download --limit 3
```

核心实现：`tools/tie_tu_pipeline.py` → `download_by_url(url, title)`  
总控阶段：`run_tietu_pipeline.py` → `phase_download()`

### 目标

读取脚本2 产出的超链接（`waterfall_links.json` / `tietu_state.json`），**不操作微信**，批量下载每条贴图文章里的幻灯片图片。

### 主流程

1. 汇总状态里 `link_ok` / 有 `link` 的条目（可从 `waterfall_links.json` 回填）
2. 已下载且 `images/` 非空则跳过
3. 对每条链接：
   - HTTP GET 文章 HTML（微信 UA + Referer）
   - 解析 `picture_page_info_list` / 幻灯片 `cdn_url`（过滤水印）
   - 逐张下载到 `Downloads/<安全标题>/images/001.jpg…`
   - 写同目录 Markdown（标题+原文链接）
4. 更新 `tools/tietu_state.json` 状态（`downloaded` / `dl_fail`）

### 规则要点

- **不依赖微信窗口**；只需网络能访问 `mp.weixin.qq.com` 与 `mmbiz.qpic.cn`
- CDN 图带 `Referer: https://mp.weixin.qq.com/`
- 文件名剔除 Windows 非法字符，标题截断约 80 字
- 支持 `--limit N` 只下前 N 条有链接项

### 产出示例

```
%USERPROFILE%\Downloads\直线筛1028-2型…\
  ├── images\
  │     ├── 001.jpg
  │     └── 002.jpg
  └── 直线筛1028-2型….md
```

---

## 目录结构

```
weixin_gongzhonghao/
├── README.md
├── requirements.txt
├── run_tietu_pipeline.py / .cmd          # 总控（含脚本3 download 阶段）
├── test_open_waterfall_cards.cmd         # 辅助：只测打开卡片
└── tools/
    ├── scan_waterfall_catalog.py         # 脚本1
    ├── collect_waterfall_links.py        # 脚本2
    ├── tie_tu_pipeline.py                # 脚本3 核心 download_by_url
    ├── test_open_waterfall_cards.py      # 辅助测试
    ├── card_roi.py / card_tracker.py / scroll_tracker.py / visual_matcher.py
    └── wx_geom.example.json
```

## 注意

- 仅供本人授权场景；运行截图与 catalog 默认 gitignore
- 关闭按钮相对「…」标定，布局变化需重标 `DOTS_SCREEN`
- 微信 CDN 直链可能过期；失败时重新跑脚本2 采链后再 download
