# photoshop-skill

用 Windows 本机已安装的 **Adobe Photoshop**，为照片做色调 / 色彩调色：通过 COM/JSX 驱动**真实 Photoshop**（不是模拟），自动分析曝光与色彩，套用自适应曲线与风格预设，导出独立副本，并生成**前后对比图**与**技术风险报告**。

支持输入 **JPEG / PNG / TIFF / BMP / WebP**，以及解码器支持的**相机 RAW**（经 rawpy 解码为统一的 16 位 sRGB 基线）；PNG / TIFF 可保留透明度与 8/16 位精度。

> Skill 标识为 `photoshop-skill`。适用于照片美化、批量调色、逐张参数、加前后缀导出。
> 不提供抠图、合成、交互式 RAW 开发或通用人像精修。

---

## 功能特性

- **自适应曝光与对比**：按直方图百分位（p25/p50/p75…）自动提亮或压暗、调整对比，不硬拉黑白点。
- **暗部 / 亮部处理**：暗部软提亮（`shadow_lift`）、亮部软肩压缩（`highlight_compression`）；对已偏亮的高调画面保留明亮感、不把最暗百分位硬拉成黑。
- **色彩**：自然饱和度 / 饱和度独立控制；可选冷暖（`temperature` / `tint`，RGB 曲线偏移，非开尔文）。
- **风格预设**：`auto` / `natural` / `clean` / `vivid` / `warm` / `film`。
- **逐张参数**：用 `jobs.json` 给每张照片单独指定模式、强度、调整与 `reason`；同样内容的照片可分组，明显不同的逐张处理。
- **多格式与 RAW**：JPEG / PNG / TIFF / BMP / WebP + 相机 RAW；`--format auto|jpg|png|tiff` 控制输出，auto 按输入保留格式（RAW/TIFF→TIFF，BMP/WebP→PNG）。
- **透明度与位深**：曝光 / 彩度统计排除全透明像素；PNG / TIFF 保留 Alpha 与 8/16 位，JPEG 最后合成白底转 8 位；PNG 导出补写 sRGB iCCP 配置块。
- **安全输出**：嵌入 sRGB、Photoshop 自动应用方向、默认**不覆盖源图**；不同源图映射到同一输出路径（如同名 `.jpg`/`.jpeg`）会报错，并拒绝 32 位 HDR 与多帧 / 多页文件。
- **导出即复查**：每次导出生成 `comparison_*.png` 与 `review.json`，标注剪裁、偏色、过饱和、尺寸 / ICC，以及适用时的**透明度与位深**等技术风险（**技术提示，不是审美分数**）。

---

## 环境要求

- Windows + 可连接 `Photoshop.Application` 的 Adobe Photoshop
- Python 3.9+、Pillow 9.1+、pywin32

缺少依赖时，在**实际运行的解释器**中安装：

```powershell
pip install Pillow pywin32
```

相机 RAW 还需同一解释器中的 `rawpy numpy tifffile`（未安装时普通图片仍可处理）：

```powershell
python -m pip install rawpy numpy tifffile
```

---

## 安装

把本仓库作为技能目录放到对应工具的 `skills` 下（目录名建议 `photoshop-skill`）：

| 工具 | 目标路径 |
| --- | --- |
| opencode | `~/.config/opencode/skills/photoshop-skill` |
| Claude Code | `~/.claude/skills/photoshop-skill` |
| Codex | `~/.codex/skills/photoshop-skill` |

脚本、示例路径均**相对于本 skill 目录**；从其他目录运行时请使用绝对路径。

---

## 快速开始

```powershell
# 只看参数，不启动 PS、不写文件
python scripts\photoshop_tone.py "C:\photos" --dry-run

# 生成原图预览 + 计划（不启动 PS，不导出成品）
python scripts\photoshop_tone.py "C:\photos" --preview-only --review-dir "C:\previews"

# 实机导出候选（比较两个有依据的候选时用不同输出目录）
python scripts\photoshop_tone.py "C:\samples" --out "C:\exports\candidate_A" --mode natural --strength 0.8
python scripts\photoshop_tone.py "C:\samples" --out "C:\exports\candidate_B" --mode clean   --strength 0.9

# 按 jobs.json 逐张处理，递归子目录
python scripts\photoshop_tone.py "C:\photos" --out "C:\exports" --jobs examples\jobs.example.json --recursive

# 原图旁生成副本，加后缀并开启严格技术检查
python scripts\photoshop_tone.py "C:\photos" --prefix "" --suffix "_edited" --strict-review

# 指定输出格式（auto 默认；可显式 jpg / png / tiff）
python scripts\photoshop_tone.py "C:\photos" --out "C:\exports" --format png
```

---

## 命令行选项

| 选项 | 行为 |
| --- | --- |
| `--out DIR` | 输出根目录（默认与输入相同） |
| `--prefix P_` / `--suffix ""` | 输出文件名前后缀（默认前缀 `P_`） |
| `--format auto\|jpg\|png\|tiff` | 输出格式，默认 `auto`（按输入保留格式）；见下表 |
| `--quality 1..12` | JPEG 质量（仅 JPEG 输出），默认 `12` |
| `--mode MODE` | 风格预设，见下表 |
| `--strength 0..1.5` | 自动效果强度，默认 `1`；`0` 关闭自动曲线与预设色彩 |
| `--jobs FILE` | 逐张参数 JSON，匹配项覆盖该照片的命令行风格 / 强度 |
| `--recursive` | 递归子目录，保留相对结构 |
| `--limit N` | 只取排序后的前 N 张（`0` = 全部）；不能替代代表性选择 |
| `--dry-run` | 仅打印参数，不启动 PS、不写目录或图片 |
| `--preview-only` | 写 `sources_*.png` 和 `review.json`，不启动 PS、不导出成品（与 dry-run 互斥） |
| `--review-dir DIR` | 检查目录，默认 `<输出根>/_ps_review`；导出后写 `comparison_*.png` 与 `review.json` |
| `--strict-review` | 技术风险被标记时返回非零（仅提示复查，非自动审美结论） |
| `--overwrite` | 替换已有输出（否则跳过） |
| `--quit` / `--keep-open` | 退出 / 保留本次启动的 Photoshop 会话（互斥） |

**退出码**：`0` 成功或全部跳过；`1` 运行失败、无源图或 strict-review 触发；`2` 配置 / 输入不合法。

`--format auto` 输出映射：

| 输入 | auto 输出 |
| --- | --- |
| JPEG（`.jpg` / `.jpeg`） | `.jpg` |
| PNG | `.png`，保留透明度 |
| TIFF（`.tif` / `.tiff`） | `.tif`，保留支持的 8/16 位与透明度 |
| 相机 RAW | `.tif`，16 位 sRGB 解码基线 |
| BMP / WebP | `.png`，保留存在的透明度 |

显式 `jpg` 会在最终导出前把透明区域合成到白底并转 8 位；需要透明度或 16 位时选 `png` / `tiff`。`--quality` 仅控制 JPEG。

---

## 风格预设

| 风格 | 用途 |
| --- | --- |
| `auto` | 中性 natural 基线；不自动识别场景或猜测白平衡 |
| `natural` | 自适应曝光、适度对比和自然饱和度，保留中性色 |
| `clean` | 较柔和对比、稍明亮的中间调，适合清爽观感 |
| `vivid` | 更强对比和彩度，适合需要鲜明层次的照片 |
| `warm` | 可选暖色中间调，白黑端点保持中性（先确认适合场景） |
| `film` | 抬黑、柔化高光、降低彩度、轻微暖调，适合明确需要的胶片感 |

已鲜艳照片会减弱预设彩度增加；真正的黑白照片在中性风格中保持黑白。
全图 RGB 均值受场景颜色影响，**不能**据此自动判断白平衡。

---

## 逐张设置（jobs.json）

`--jobs` 匹配项覆盖该照片设置；未匹配照片使用命令行风格 / 强度。键用准确文件名或输入相对路径（`/` 分隔），相对路径优先，重名照片必须用相对路径。`_comment` 用于说明。

```json
{
  "room.jpg": {
    "mode": "clean", "strength": 0.85,
    "adjustments": {"vibrance": 12, "temperature": 0, "tint": 0},
    "reason": "室内主体偏暗，提亮并保留白墙中性色"
  },
  "trip/sky.jpg": {
    "curves": [{"ch": 0, "pts": [[0, 0], [64, 70], [128, 134], [224, 218], [255, 255]]}],
    "adjustments": {"vibrance": 10},
    "reason": "提亮中间调并轻压高光"
  }
}
```

- `adjustments`：`vibrance` / `saturation` 为 PS 单位，`-100..100`；`temperature` / `tint` 为 `-30..30` 的 RGB 曲线偏移。**显式 adjustments 覆盖相应预设，且不随 strength 缩放**。
- 手动曲线：`tone: [black, white, gamma, contrast]`（`gamma<1` 压暗、`>1` 提亮）与可选 `channels`，或独立 `curves`；手动曲线**不能**与 `mode`/`strength` 混用。
- `curves`：每条 2–16 个 `[x,y]` 点，值 `0..255`、x 严格递增；通道 `0/1/2/3` = 复合/红/绿/蓝。
- 参数须以**实机成品**验证；硬拉黑白点可能丢失细节。

---

## 工作流（推荐）

1. **先看原图**：`--preview-only` 生成原图预览与计划，逐张记录 1–3 个具体问题。
2. **按内容选参数**：区分暗图、高调、反差大、灰淡、已鲜艳等；亮度 / 彩度 / 冷暖分别决定。
3. **实机导出样片**：选 1–3 张覆盖不同问题的代表照片，各自试两个有依据的候选。
4. **复查结果**：看 `comparison_*.png` 与 `review.json`，兼顾审美与技术两项判断。
5. **有原因地回调**：针对具体问题调曲线 / `strength` / `adjustments`，最多两次。
6. **分组批量**：样片通过后同类同参，特殊照片逐张处理，最后核查全部导出状态。

---

## 输出与会话安全

- 默认在原图旁保存 `P_<stem>`，扩展名由输出格式决定；`--out` 改输出根目录；`--recursive` 保留相对子目录。
- 已有输出默认跳过，`--overwrite` 才替换；**任何情况下输出不能覆盖源图**。
- 脚本保护已在 Photoshop 打开的源图，恢复原对话框设置与活动文档，只关闭本次打开的文档。
- 本次启动的 PS 在完成后正常退出；原有会话默认保留。故障排查见 [`reference/gotchas.md`](reference/gotchas.md)。

---

## 维护检查

```powershell
python -X utf8 -m unittest discover -s tests -v
python tests\smoke_photoshop.py
python tests\smoke_photo_grading.py
python tests\smoke_photo_formats.py
python tests\smoke_photoshop_exit.py
```

单元测试不启动 PS；实机脚本检查真实导出像素、调色、格式、透明度、位深、ICC、方向及会话清理。

---

## 仓库结构

```
SKILL.md                      技能主文档（判断与检查流程、命令、风格）
reference/color-grading.md    按照片内容选择调色的参考
reference/photo-formats.md    输入格式、透明度与 RAW 说明
reference/gotchas.md          故障排查
scripts/photoshop_tone.py     主脚本：分析 + COM/JSX 驱动 Photoshop
scripts/photo_formats.py      格式探测、RAW 解码、输出格式选择
scripts/png_profiles.py       PNG 导出补写 sRGB iCCP
scripts/photo_review.py       前后对比与技术风险报告
examples/jobs.example.json    逐张参数示例
tests/                        单元测试与实机冒烟测试
```

## 许可证

MIT（见 `SKILL.md` frontmatter）。
