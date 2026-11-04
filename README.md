# weixin_gongzhonghao

微信公众号「贴图」瀑布流：卡片识别 → 逐个打开详情（可选复制链接）的本机 RPA 脚本集。

> 同步自本机 `wx_gzh` 昨晚可用版本（2026-11-04）。轮廓特征是**代码写死的几何/色块/ROI 规则**，不是开跑前先看截图再总结，也不是离线训练后再扫；每帧同一套规则现检，滚动中用 Track 配准。

## 脚本一览

| 编号 | 入口 | 作用 |
|------|------|------|
| **脚本1** | `tools/scan_waterfall_catalog.py` | 扫瀑布流：数卡、定位、指纹/Track、回顶，写出目录 |
| **脚本2** | `tools/collect_waterfall_links.py` | 按目录找卡、点击、复制 mp 链接、关详情（采链） |
| **脚本3** | `tools/test_open_waterfall_cards.py` + `.cmd` | 测试：脚本1 扫完后逐个打开→约 2s 关详情（不复制链接） |

依赖模块（脚本1/2/3 共用）：

- `card_roi.py` — 整卡轮廓 ROI
- `scroll_tracker.py` — 相邻帧纵向位移 / 累计滚动
- `card_tracker.py` — PARTIAL→CONFIRMED Track
- `visual_matcher.py` — Re-ID（SIFT 默认；有 torch+LightGlue 可升）

---

## 环境

- Windows + 已登录微信 PC，打开目标公众号资料页「贴图」瀑布流
- Python 3.10+ 建议

```bat
pip install -r requirements.txt
copy tools\wx_geom.example.json tools\wx_geom.json
```

按本机微信窗口改 `tools/wx_geom.json`（`L/T/R/B/H`）。

快速测试：

```bat
test_open_waterfall_cards.cmd
test_open_waterfall_cards.cmd --limit 3
test_open_waterfall_cards.cmd --skip-scan --limit 5
```

任务结束后 CMD **不会自动关闭**（需按键）。

---

## 脚本1：`scan_waterfall_catalog.py`（扫目录）

### 目标

准确得到贴图卡片**数量与位置**（`waterfall_catalog.json` / `waterfall_catalog_clean.json`），标题 OCR 为第二优先。

### 主流程

1. 若误入详情：点「…」右侧关闭（`DOTS + (40,0)`）
2. `ensure_tietu_list`：确认资料页（关注/已关注 + 私信|赞|多卡），尽量点「贴图」Tab
3. **向下扫描**：每步截屏 →（可选并行）识别 → 滚轮
4. Track 确认后可补 OCR 标题（默认滚动中不做 OCR）
5. **原路回顶**：记录下行净滚轮格数，标题补完后按相同格数上滚（速度 ×2）；OCR 只做回顶校验日志

### 轮廓 / 识别规则（写死，非样本总结）

- 默认 `method=card_roi`：双列整卡轮廓 → 关联 `#2F2F30` 底栏 → aHash 指纹 `fp`
- `--method bar`：仅旧底栏色块路径
- 卡宽门禁约 `CARD_W ∈ [260, 310]`；列轨道 `COL_*`；`poster_h` 门槛区分半截/可 enroll
- 底栏灰条服务标题 OCR，**不是**数卡的第一依据
- `require_bar` 可关：轮廓优先，避免底栏 fill 误杀

### 滚动规则

- 默认节奏：约 **1 秒 1 格** 中键滚轮（`SCAN_DOWN_NOTCHES=1`，`SCAN_SETTLE_S=1.0`）
- 连续无新增达 `SCAN_STAGNANT_LIMIT` 判到底
- 与上一屏底部 key 无重叠时回退若干格补扫
- 回顶：**按下行累计格数返还**，不是靠 OCR 提前打断上滚

### Track 规则

- `ScrollTracker` 估计 `delta_y` / `cumulative_scroll_y`
- `CardTrackManager`：位置续跟 → Re-ID → 确认后发 `card_uuid`
- 目录以 **confirmed uuid** 为准；同名卡靠 fp/uuid/列+global_y 区分

### 产出

- `tools/waterfall_catalog.json`
- `tools/waterfall_catalog_clean.json`
- `tools/waterfall_progress.txt`
- `tools/waterfall_tracks_debug.json`（调试）

---

## 脚本2：`collect_waterfall_links.py`（采链）

### 目标

按脚本1 目录，逐张打开详情并复制 `mp.weixin.qq.com/s/...` 链接。

### 主流程（单张）

1. 打开前点「…」右侧关闭（防残留详情）
2. `ensure_tietu` 回贴图列表
3. `scroll_until_fingerprint`：本屏匹配 → 小步下滚；画面未变复用本屏缓存
4. 点击海报（复用 seek 命中坐标，避免二次整卡识别）
5. `wait_detail` 确认进入详情
6. 复制链接和弦 → 关详情 → 回贴图

### 匹配优先级（禁止仅靠标题点卡）

1. `card_uuid`
2. 精确 / 软 `fp`（aHash 汉明距离）
3. 列 + 预测 `bar_y`（`global_bar_y - cum_scroll_y`）
4. 标题只加分，不单独决定

### 其它规则

- `OpenSession`：跨卡保持 `cum_scroll_y` + `ScreenCapsCache`（未滚动不重识别）
- `scroll_toward_global_y`：点下一张前对准滚动，**最多 10 次**；连续无位移提前停（到底）
- 点击前日志：本屏几张、点本屏第几张、还剩几张后需滚动

### 产出

- `tools/waterfall_links.json`
- `tools/waterfall_seen.json`
- `tools/waterfall_progress.txt`

---

## 脚本3：`test_open_waterfall_cards.py`（打开测试）

### 目标

验证「扫目录 → 逐个打开 → 关闭」全链路，**不复制链接**，方便调点击/滚动。

### 主流程

1. Phase1：调用脚本1 `scan.main`（可用 `--skip-scan` 读已有目录）
2. Phase2：对目录每张卡
   - 关残留详情 → 贴图页
   - `scroll_toward_global_y` 对准
   - `scroll_until_fingerprint` 命中
   - 打印本屏库存日志 → 点击
   - **固定等待约 2 秒后关详情**（不等待详情加载完成）
   - 继续下一张（点一张、关一张、再小滚对准）

### 策略说明

「点一张 → 关详情 → 再对准下一张」适合双列瀑布流：本屏能点的点完，不够再滚；比每次从顶重找更稳，也减少同名误点。

### 常用参数

| 参数 | 含义 |
|------|------|
| `--limit N` | 只测前 N 张 |
| `--skip-scan` | 跳过扫描，用已有 catalog |
| `--method card_roi\|bar` | 扫描方法（默认 card_roi） |
| `--no-ocr-titles` | 扫描不做标题 OCR |

### 产出

- `tools/test_open_cards_result.json`
- `tools/test_open_cards_progress.txt`

---

## 目录结构

```
weixin_gongzhonghao/
├── README.md
├── requirements.txt
├── test_open_waterfall_cards.cmd          # 根目录快捷启动
└── tools/
    ├── scan_waterfall_catalog.py          # 脚本1
    ├── collect_waterfall_links.py         # 脚本2
    ├── test_open_waterfall_cards.py       # 脚本3
    ├── test_open_waterfall_cards.cmd
    ├── card_roi.py
    ├── card_tracker.py
    ├── scroll_tracker.py
    ├── visual_matcher.py
    └── wx_geom.example.json               # 复制为 wx_geom.json 后改坐标
```

## 注意

- 仅供本人授权窗口上的自动化；需本机前台微信窗口与 DPI 坐标匹配
- 运行产物（截图、catalog、进度）默认 gitignore，勿提交隐私截图
- 关闭按钮坐标相对「…」标定；分辨率/布局变化时需重标定 `DOTS_SCREEN`
