---
name: photoshop-skill
description: 使用 Windows 本机 Adobe Photoshop 为 JPEG 照片调色，通过曝光、对比度、自然饱和度和可选冷暖曲线改善观感，导出独立副本并生成前后对比与风险检查。适用于 Photoshop 修图、照片美化、批量调色、逐张参数和加前后缀导出；不提供抠图、合成、RAW 开发或通用人像精修。
license: MIT
---

# Photoshop 照片调色

使用 `scripts/photoshop_tone.py` 分析照片，再通过 COM/JSX 驱动真实 Photoshop。
自动亮度曲线使用明度混合模式，色彩由自然饱和度、饱和度及可选 RGB 冷暖曲线独立处理。
输出为嵌入 sRGB 配置的 JPEG 副本；不保留可编辑图层。仅支持 `.jpg` / `.jpeg`。

## 判断与检查流程

1. **先看原图**：使用 `--preview-only` 生成原图预览和计划，再查看图片。
   按主体、光线、曝光、层次、肤色/中性色与现有色彩，记录每张或每组的 1–3 个具体问题。
   读取 [reference/color-grading.md](reference/color-grading.md) 选择风格和参数。
2. **按内容选参数**：区分暗图、明亮高调、反差大、灰淡、已鲜艳等情况；
   亮度、彩度、冷暖分别决定。用 `--jobs` 给不同照片填写设置和 `reason`。
   不给整批照片统一冷调，也不按文件顺序或随机分配风格。
3. **实际导出样片**：选 1–3 张覆盖不同问题的代表照片，在样片目录中比较两个有依据的候选。
   使用不同输出/检查目录；候选必须经 Photoshop 导出。`--limit` 只取排序后的前 N 张，不能代替代表性选择。
4. **查看结果**：打开候选目录中的 `comparison_*.png` 和 `review.json`。
   审美判断看主体曝光、层次、色彩协调、肤色/中性色和用户要求；技术判断看新增剪裁、偏色、过饱和、方向、尺寸和 ICC。
   统计值及像素变化量是风险提示，不能当作“好看分数”；技术通过仍需视觉比较。
5. **有原因地回调**：针对具体问题调节曲线、`strength` 或 `adjustments`，最多回调两次，每次重新导出并复查。
   两次后仍有明显问题，保留较稳妥候选并说明未解决项；不要只为增加差异加重效果。
6. **分组批量处理**：样片通过后，同类照片使用同组参数，明显不同的照片逐张处理。
   完成后检查所有导出状态，查看每组代表照片和被标记的图片，报告导出/跳过/失败数量、结果位置及采用的参数理由。

## 命令

需要 Python 3.9+、Pillow 9.1+、pywin32 和可连接 `Photoshop.Application` 的 Photoshop。
缺少依赖时，在实际运行的解释器中安装 `Pillow pywin32`。
脚本、示例路径均相对于本 skill 目录；从其他目录运行时使用绝对路径。

```powershell
python scripts\photoshop_tone.py "C:\photos" --dry-run
python scripts\photoshop_tone.py "C:\photos" --preview-only --review-dir "C:\previews"
python scripts\photoshop_tone.py "C:\samples" --out "C:\exports\candidate_A" --mode natural --strength 0.8
python scripts\photoshop_tone.py "C:\samples" --out "C:\exports\candidate_B" --mode clean --strength 0.9
python scripts\photoshop_tone.py "C:\photos" --out "C:\exports" --jobs examples\jobs.example.json --recursive
python scripts\photoshop_tone.py "C:\photos" --prefix "" --suffix "_edited" --strict-review
```

两种样片命令是用法示例；实际候选按照片问题选择。

| 选项 | 行为 |
| --- | --- |
| `--dry-run` | 仅打印参数，不启动 PS，不写目录或图片 |
| `--preview-only` | 写 `sources_*.png` 和 `review.json`，不启动 PS，不导出修图结果；与 dry-run 互斥 |
| `--review-dir` | 检查目录，默认 `<输出根目录>/_ps_review`；实际导出后写 `comparison_*.png` 和 `review.json` |
| `--strict-review` | 技术风险被标记时返回非零；提示复查，不代表自动审美结论 |
| `--strength` | 自动效果强度 `0..1.5`，默认 `1`；`0` 将自动曲线和预设色彩设为零效果 |

每次运行会更新同一检查目录中的报告和同名预览；比较候选时使用不同目录。
预览中的 `planned_settings` 是计划，只有实际导出后的检查才反映成品。

## 风格与逐张设置

| 风格 | 用途 |
| --- | --- |
| `auto` | 使用中性 natural 基线；不自动识别场景或猜测白平衡 |
| `natural` | 自适应曝光、适度对比和自然饱和度，保留中性色 |
| `clean` | 较柔和对比和稍明亮的中间调，适合清爽观感 |
| `vivid` | 更强对比和彩度，适合需要更鲜明层次的照片；暗图仍按曝光诊断提亮 |
| `warm` | 可选暖色中间调，白黑端点保持中性；先确认适合场景 |
| `film` | 抬黑、柔化高光、降低彩度和轻微暖调，适合明确需要的胶片感 |

已鲜艳照片会减弱预设的彩度增加；真正的黑白照片在中性风格中保持黑白。
这些是受限的启发式处理，不能还原已丢失的高光或暗部信息。
全图 RGB 均值受场景颜色影响，不能据此自动判断白平衡。

`--jobs` 匹配项覆盖该照片的设置；未匹配照片使用命令行风格/强度。
键使用准确文件名或输入相对路径（`/` 分隔）；相对路径优先，重名照片必须使用相对路径。
`_comment` 是说明。复制 [examples/jobs.example.json](examples/jobs.example.json) 后更换示例名和理由。

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

`adjustments`：`vibrance` / `saturation` 为 PS 单位，范围 `-100..100`；
`temperature` / `tint` 为 `-30..30` 的 RGB 曲线偏移，**不是开尔文色温或 Camera Raw 参数**。
正 temperature 偏暖，正 tint 偏洋红。显式 adjustments 覆盖相应预设，数值不再乘 strength。
因此需要完全零效果时，同时清空或置零显式调整。

旧手动格式继续支持：`tone: [black, white, gamma, contrast]` 和可选 `channels`，或独立 `curves`。
手动曲线不能混用 `mode` / `strength`，可搭配 `adjustments` / `reason`。
`0 <= black < white <= 255`，跨度至少 8；gamma / contrast 必须有限且大于 0。
gamma 小于 1 压暗，大于 1 提亮。通道 `0/1/2/3` 为复合/红/绿/蓝；各通道最多一次，tone 已占通道 0。
每条曲线 2–16 个 `[x,y]` 点，值在 `0..255` 且 x 严格递增；`curves` 不与 tone/channels 混用。
硬拉黑白点可能丢失细节，手动参数须以实机成品验证。

## 输出与会话安全

默认在原图旁保存 `P_<stem>.jpg`；`--out` 改变输出根目录，`--recursive` 保留相对子目录并排除嵌套输出目录。
JPEG quality 为 `1..12`，默认 `12`。已有输出跳过，`--overwrite` 才替换；任何情况下输出不能覆盖源图。
文件名已带当前前缀/后缀的图片不重复处理；原图本来带该标记时更换标记，或使用独立输出目录与空标记。
同目录同名 `.jpg` / `.jpeg` 导致输出冲突并报错。

`--limit 0` 表示全部；正数限制在跳过旧输出前应用。退出码：`0` 成功/全部跳过；
`1` 运行失败、无源图或 strict-review 触发；`2` 配置/输入不合法。

脚本保护已经在 Photoshop 打开的源图，恢复原对话框设置与活动文档，只关闭本次打开的文档。
本次启动的 PS 在完成/异常后空闲时正常退出；原有会话默认保留。
`--keep-open` 保留新会话，`--quit` 也可退出原有空会话，二者互斥；有文档时不退出。
遇到忙碌状态应检查对话框或正在执行的操作。故障排查见 [reference/gotchas.md](reference/gotchas.md)。

## 维护检查

```powershell
python -X utf8 -m unittest discover -s tests -v
python tests\smoke_photoshop.py
python tests\smoke_photo_grading.py
python tests\smoke_photoshop_exit.py
```

单元测试不启动 PS。实机脚本用于检查真实导出像素、调色、ICC、方向及会话清理；退出测试要求 PS 原先关闭。
运行并核对结果后才能声称当前版本通过实机验证，生成图层或 dry-run 成功均不足以证明调色生效。
本目录中的 skill 标识为 `photoshop-skill`；修改此目录不会自动更新其他安装副本。
