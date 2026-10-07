# 输入格式、透明度与 RAW

处理 RAW、透明图片、16 位图片，或选择不同导出格式时阅读。
实现以 `scripts/photo_formats.py`、`scripts/photoshop_tone.py` 和 `scripts/photo_review.py` 为准。

## 支持范围与输出

- 普通照片：`.jpg`、`.jpeg`、`.png`、`.tif`、`.tiff`、`.bmp`、`.webp`。
- 相机 RAW 候选扩展主要包括：`.raw`、`.dng`、`.nef`、`.nrw`、`.cr2`、`.cr3`、`.crw`、`.arw`、`.sr2`、`.srf`、`.raf`、`.rw2`、`.pef`、`.orf`、`.srw`。
  完整候选列表见 [photo_formats.py 的 RAW_EXTS](../scripts/photo_formats.py)。
  扩展名只负责扫描；实际解码取决于 rawpy 所带 LibRaw 对文件编码及相机型号的支持。
  任意无文件头的 `.raw` 或新相机格式不保证可读；解码失败须报告具体文件，不能改后缀或用内嵌 JPEG 冒充成功。
- 不承诺 HEIC/HEIF、PSD、多帧 WebP/APNG 或多页 TIFF；多帧/多页文件直接拒绝，避免只改第一帧。
  32 位或有符号/浮点 TIFF，以及 16 位 RGB PNG 使用 tRNS 透明键的特殊组合会明确报错；后者可改用带独立 Alpha 通道的 RGBA PNG 副本。
- `--format auto`：JPEG→JPEG、PNG→PNG、TIFF/RAW→TIFF、BMP/WebP→PNG。
  显式 `jpg`、`png`、`tiff` 可改变输出类型；同名源图映射到同一路径时会报错。

PNG/TIFF 保留支持的透明度及 8/16 位精度；JPEG 不支持透明度或 16 位。
选择 JPEG 时，最后合成白背景并转为 8 位，先完成调色再进行有损输出转换。
文件后缀、Photoshop 保存选项和检查方式必须一致；不要只把 JPEG 的文件名改成 `.png` 或 `.tif`。

## RAW 的统一基线

RAW 额外依赖 `rawpy`、`numpy`、`tifffile`。在执行脚本的同一个 Python 环境安装：

```powershell
python -m pip install rawpy numpy tifffile
```

使用 rawpy 从原始数据全尺寸解码，固定主要参数：

| 参数 | 值 / 含义 |
| --- | --- |
| `use_camera_wb` | `True`，使用拍摄白平衡 |
| `use_auto_wb` | `False` |
| `no_auto_bright` | `True`，关闭自动增亮 |
| `output_color` | `rawpy.ColorSpace.sRGB` |
| `output_bps` | `16` |
| `gamma` | `(2.4, 12.92)`，sRGB 转换参数 |

按原文件方向解码为临时 16 位 sRGB TIFF，并嵌入 ICC。
分析、Photoshop 输入及处理前对比共用该基线；报告保留原 RAW 路径与解码设置，处理前一栏标为 RAW render。
该 TIFF 是解码后的照片，后续曲线和自然饱和度仍由真实 Photoshop 完成。
临时基线在本次处理及检查结束后清理；原 RAW 和旁边的 XMP 不写入。

此流程提供统一、可核对的调色起点，不提供 Camera Raw 的交互式曝光、镜头配置、去马赛克算法选择或高光重建参数界面。
若用户明确需要这些 RAW 开发控制，应说明本 skill 的边界。
拍摄白平衡和固定解码参数不保证符合审美；继续执行主 skill 的内容判断、候选比较与回调流程。

## 透明度与位深检查

- 曝光和彩度统计排除全透明像素，避免隐藏 RGB 影响参数。全透明图片没有可分析的照片内容，应报错。
- 预览按统一背景显示透明区域；保留透明度的导出不得烘焙该背景。
- PNG/TIFF 检查透明度、方向、尺寸、ICC 和位深；明确转换为 JPEG 时，记录白背景及 8 位转换。
  透明图片转 JPEG 的前后检查共用白背景，原始透明图统计另保留在报告中，避免把格式转换误判成新增剪裁。
- 16 位图片的分析预览转换到 8 位统计尺度，不能把 16 位灰度直接裁到 `0..255`；成品保持支持的精度。
- RAW 技术检查使用同一解码基线。与相机 JPEG、其他 RAW 软件或不同解码参数比较，不能单独证明本次 Photoshop 调色生效。

维护 RAW 参数时核对 [rawpy Params 文档](https://letmaik.github.io/rawpy/api/rawpy.Params.html) 与 [LibRaw 输出参数](https://www.libraw.org/docs/API-datastruct.html)。
PNG 导出通过 [W3C PNG 的 iCCP 配置块](https://www.w3.org/TR/png-3/#11iCCP) 补写 sRGB；原有像素数据和透明通道保持不变。
