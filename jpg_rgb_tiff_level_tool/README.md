# JPG 调平转 RGB TIFF 独立工具

这个文件夹是独立封装，不改当前已经打包好的 GUI。后续如果要集成进软件，可以直接复用 `raster_level_rgb_tiff.py` 里的函数，也可以让 GUI 调用命令行。

## 目标

把扫描图件批量处理成 MapGIS 更容易加载的栅格底图：

- 输入：`.jpg/.jpeg/.png/.tif/.tiff/.bmp`
- 输出：`.tif`
- 输出模式：`RGB`
- 输出 DPI：`300dpi`
- 输出压缩：无压缩 TIFF，Pillow 中为 `compression=raw`
- 操作：尽量调平；不做黑白化；不增强颜色；不改地质内容
- 原图：不覆盖，输出到单独文件夹

## 核心实现逻辑

1. 读取原图。
2. 转成 `RGB`，保证 MapGIS 侧加载时不是 1-bit、灰度或调色板图。
3. 可选裁掉蓝色拍摄背景。这个逻辑很保守，只有检测到大面积蓝色图纸并且裁切不会明显伤到图面时才执行。
4. 估算调平角度：
   - 优先找图框上边/下边这类长直线；
   - 如果图框不明显，再用 Hough 直线检测找长水平线或长垂直线；
   - 如果没有可靠依据，角度为 `0`，不强行旋转。
5. 旋转：
   - 使用双三次插值；
   - `expand=True`，保留完整图面，不裁边；
   - 旋转后空白角用原图四角的中位颜色填充。
6. 保存为 `RGB + 300dpi + raw TIFF`。
7. 写 `conversion_log.csv` 和 `conversion_errors.csv`。

关键原则：调平依据必须来自纸面结构，如图框、表格线、网格线；不把地层线、剖面曲线、矿体边界当成水平依据。

## 命令行用法

处理单个文件：

```powershell
python C:\maps\jpg_rgb_tiff_level_tool\raster_level_rgb_tiff.py `
  "C:\maps\280-1288（简单）\t01_0013.jpg" `
  "C:\maps\_codex_validation\rgb_tiff_test\t01_0013.tif" `
  --verify
```

处理整个文件夹并保留目录结构：

```powershell
python C:\maps\jpg_rgb_tiff_level_tool\raster_level_rgb_tiff.py `
  "G:\7 档案整理(科研所横向)\整理档案（小兴安岭自然生态）\13档原始资料" `
  "G:\7 档案整理(科研所横向)\整理档案（小兴安岭自然生态）\13档原始资料_RGB调平TIFF" `
  --verify
```

只处理 JPG/JPEG：

```powershell
python C:\maps\jpg_rgb_tiff_level_tool\raster_level_rgb_tiff.py `
  "E:\some_input_folder" `
  "E:\some_output_folder" `
  --ext jpg,jpeg `
  --verify
```

只转换 RGB TIFF，不调平：

```powershell
python C:\maps\jpg_rgb_tiff_level_tool\raster_level_rgb_tiff.py `
  "E:\some_input_folder" `
  "E:\some_output_folder" `
  --no-deskew `
  --verify
```

## 主要参数

- `--ext jpg,jpeg,png,tif,tiff,bmp`：设置要处理的扩展名。
- `--no-recursive`：只处理当前文件夹，不递归子文件夹。
- `--no-deskew`：不调平，只转 RGB TIFF。
- `--no-blue-crop`：关闭蓝色图纸背景自动裁切。
- `--strict-images`：不容忍尾部截断的 JPEG。默认是容忍，适合旧档案扫描文件。
- `--no-preview`：不生成预览总览图。
- `--verify`：处理完成后逐张读取输出 TIFF，确认 `RGB/300dpi/raw`。
- `--enhance [--enhance-strength light|standard|strong]`：额外输出一份视觉增强副本
  （光照拉平 + 清晰化 + 锐化，几何不变）到 `ENHANCED_PREVIEW/` 子文件夹，
  文件名带 `_enhanced` 后缀；**正式调平 TIFF 逐字节不变**。
  仅供人眼查看：实测增强图会降低 rapidocr 检出（见
  `C:\maps\docs\OCR_ENHANCE_AB_20260704.md`），不要把增强图喂给矢量化或 OCR。
  效果对比样张见 `_enhance_demo/compare_*.jpg`。需要在 C:\maps 仓库内运行
  （依赖 `geoscan.raster_enhance`）。

## 输出文件

批量处理时，输出目录会包含：

- 转换后的 `.tif` 文件，目录结构与输入目录一致；
- `conversion_log.csv`：每张图的输入尺寸、输出尺寸、旋转角度、检测方法；
- `conversion_errors.csv`：失败文件和错误信息；
- `preview_contact_sheet.jpg`：前若干张输出图的缩略预览。

## 软件集成建议

GUI 集成时不要把图像处理放在主线程里。建议流程：

1. GUI 选择输入文件夹和输出文件夹。
2. 后台线程调用 `run_batch(source_root, output_root, options)`。
3. 每处理完一张图，把当前文件名、进度、错误信息发回 GUI。
4. GUI 最后读取 `conversion_log.csv` 和 `conversion_errors.csv` 展示结果。

可以直接复用的函数：

```python
from pathlib import Path
from raster_level_rgb_tiff import LevelOptions, run_batch

options = LevelOptions(
    recursive=True,
    extensions=(".jpg", ".jpeg"),
    crop_blue_sheet=True,
    deskew=True,
    allow_truncated_images=True,
)

records, errors = run_batch(
    Path(r"E:\input_folder"),
    Path(r"E:\output_folder"),
    options,
)
```

## 已知边界

- 自动调平不是地质解释。没有可靠图框/表格/网格依据时，工具会保留原角度。
- 无压缩 RGB TIFF 会很大，通常远大于 JPG 和 Group4 TIF。
- 旋转会扩大画布，这是为了不裁掉图面。
- 这个工具不做 MapGIS 坐标纠正，不生成 `.pnt`，也不做地理配准。
