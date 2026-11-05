# weixin_gongzhonghao

微信公众号「贴图」瀑布流本机流水线：**扫卡目录 → 采集超链接 → 按链接下载图片集**。

> 同步自本机 `wx_gzh` 可用版本（2026-11-05）。轮廓特征是代码写死的几何/色块/ROI 规则，不是开跑前先看截图再总结；每帧同一套规则现检，滚动中用 Track 配准。点卡默认「混合回放」（已实测全对）。

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

```bat
test_open_waterfall_cards.cmd
test_open_waterfall_cards.cmd --limit 3
test_open_waterfall_cards.cmd --no-replay
test_open_waterfall_cards.cmd --scroll-key
```

**默认 Phase2 = 混合回放**（按卡驱动，不追扫卡底部空滚）：

1. 按 `click_at_notches` 滚到目标格  
2. 点前轻校验本屏轮廓+fp；命中且距记录坐标 ≤160px 才用本屏坐标（防串卡）  
3. 已点过的 fp 排除；未采纳则 ±1 微调，再不行用记录坐标，最后回退旧 seek  
4. 点卡前鼠标在目标停 1s；详情约 2s 后关  

`--no-replay`：旧「点一张→小滚→强制重识别」。`--scroll-key`：实验用 ↓/↑（微信未必吃）。

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

### 核心业务逻辑（难点 = 为回放点卡铺路）

脚本1看起来像「滚一圈数卡」，真正难的不是数出 17 张，而是**边扫边记下「以后怎么原路点回来」**：

1. **同一坐标系**：下行每滚 1 格累计 `notches_at_frame`；回顶后点卡用同一套格数，而不是重新猜位置。  
2. **首次完整可点才打标**：半截卡只 Track、不发可点坐标；完整可见时写下  
   `click_at_notches` + `click_cx/cy`（顶卡 cy≈544 也必须能打上，否则首屏会漏顶排）。  
3. **数准 ≠ 能点准**：目录里的数量/uuid 可以全对，若回放字段漏打、串列、或点卡时用软指纹覆盖错坐标，仍会点到同一张卡。  
4. **按卡驱动，不追空滚**：`down_notches_total` 可能大于最后一张卡的 notches（到底确认空滚）；点卡滚到最后一张即可，不必复现扫卡全部空滚。  
5. **下游消费**：测试点卡 / 脚本2 用这些字段做「混合回放」——先滚到格数，再轻校验本屏坐标（须靠近记录坐标防串卡）。

> 以后同步 GitHub 时：每个脚本章节必须保留 **「核心业务逻辑」** 栏目，写清难点与上下游契约，不能只写主流程步骤。

### 主流程

1. 误入详情则点「…」右侧关闭（`DOTS+(40,0)`）
2. 确认资料页并尽量点「贴图」
3. 向下扫描：截屏 → 识别 → 滚轮；Track 确认后可补标题 OCR；**同时打回放字段**
4. **原路回顶**：按下行净滚轮格数上滚（速度×2）

### 规则要点

- 默认 `card_roi`：整卡轮廓 → `#2F2F30` 底栏 → aHash `fp`；`--method bar` 可回退
- 卡宽约 `[260,310]`；列轨道；`poster_h` 区分半截/可确认
- 约 **1 秒 1 格** 下滚；连续无新增判到底
- Track：`delta_y` 配准 + uuid 确认；同名靠 fp/uuid/列+global_y

### 产出

`tools/waterfall_catalog.json`、`waterfall_catalog_clean.json`、`waterfall_scan_meta.json`、`waterfall_progress.txt`

### 回放字段（追加，不改识别算法）

每张确认卡可带：

- `click_at_notches`：首次完整可点时相对顶部的净下滚格数
- `click_cx` / `click_cy`：当时窗口内可点坐标

`waterfall_scan_meta.json` 含 `down_notches_total`（扫卡到底格数；点卡不必追空滚）。

---

## 脚本2：采链（`collect_waterfall_links.py`）

### 目标

按目录逐张打开详情，复制 `mp.weixin.qq.com/s/...` 链接。

### 核心业务逻辑

难点不是「复制剪贴板」，而是**稳定点到目录里那一张、且不串同名卡**：

1. 优先消费脚本1 的 `card_uuid` / `fp` / 回放坐标与格数（与测试点卡同一套契约）。  
2. 软指纹只能辅助，**不能单独覆盖**远离记录坐标的命中（否则会出现第2张与第5张点同一张）。  
3. 打开前先关残留详情；点卡悬停 1s 便于人工核对。

### 主流程

打开前关残留详情 → 贴图列表 → 指纹/位置 seek（或回放格数）→ 点海报 → 等详情 → 复制链接 → 关闭

### 匹配优先级

`card_uuid` → 精确/软 `fp` → 列+预测 `bar_y` → 标题只加分（标题绝不单独决定）

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

### 核心业务逻辑

难点在**解析微信文章里的幻灯片 CDN 列表并稳定落盘**，与 GUI 扫卡/回放无关：

1. 输入只认脚本2 的链接；不碰微信窗口。  
2. HTML → `picture_page_info_list` / 幻灯片 `cdn_url`，过滤水印图。  
3. 带微信 Referer 拉 `mmbiz.qpic.cn`；按标题建目录，断点跳过已下完的。

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
- **文档约定**：每次更新 GitHub README，脚本1/2/3 都必须有 **「核心业务逻辑」** 栏目；脚本1 须写明难点在「扫卡为回放点卡铺路」，不能只列主流程步骤。
