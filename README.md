# 照片/视频自动整理工具

自动扫描电脑所有磁盘中的**照片和视频**，根据 EXIF / 视频元数据区分**相机/手机**拍摄，按**设备型号 + 年-季度-月**整理到目标文件夹，并生成溯源报告。

## 功能特性

- 全盘并行扫描（二级展开 + 多线程，默认 32 线程）
- 支持 **18 种图片格式** + **13 种视频格式**（MP4/MOV/MKV 等）
- 视频元数据读取：ffprobe 优先 → 内置 MP4/MOV 解析器回退 → 同目录照片推断兜底
- 智能设备分类：相机、手机、未识别
- **设备过滤**：默认只复制指定品牌，其余仅记录在报告中
- 已识别设备：按 **日期+设备名** 单层归档（如 `2024-Q1-M1-2-3_Canon EOS R5`），照片视频同目录
- 小图过滤：自动排除表情包/缩略图（<100KB 或 <300px）
- 文件去重（快速哈希：文件大小 + 首尾 64KB）
- 重复文件对比：报告中展示每个重复文件与原始文件的哈希对比
- **同盘复用**：复制前自动扫描目标盘已有文件，相同文件直接 rename（秒移），避免重复跨盘复制
- 三阶段并行处理（并行哈希 → 串行去重 → 同盘复用/并行复制）
- 报告备注：标注每个文件的设备信息来源（自识别 / 同目录推断 / 无法识别 / 无日期等）
- Ctrl+C 安全中断：第一次生成已完成部分报告，第二次强制退出
- 每个目标文件夹生成 `filelist.txt`，记录文件名 → 原始路径对齐映射
- 生成 HTML + CSV 双格式溯源报告（支持全局搜索 + 每列筛选）

## 目标文件夹结构

默认模式（仅复制目标设备）：
```
H:\All_相册_20260225\
  ├── All_1_手机照片\
  │   ├── 2024-Q1-M1-2-3_Xiaomi 14\
  │   └── 2024-Q3-M7-8-9_Apple iPhone 15 Pro\
  ├── All_2_相机照片\
  │   ├── 2024-Q1-M1-2-3_Canon EOS R5\
  │   │   ├── IMG_0001.CR3
  │   │   ├── MVI_0002.MP4
  │   │   └── filelist.txt       ← 记录每个文件的原始路径
  │   └── 2024-Q3-M7-8-9_DJI Mavic 3\
  ├── All_3_未识别设备照片\          ← 需 --copy-unknown 才会有
  ├── 整理报告_xxx.html
  └── 整理报告_xxx.csv
```

`--copy-all` 模式（复制所有设备，自动区分目标/其他）：
```
H:\All_相册_20260225\
  ├── All_1_目标设备_手机照片\       ← 目标设备（如 Xiaomi、Apple…）
  │   └── 2024-Q1-M1-2-3_Xiaomi 14\
  ├── All_2_目标设备_相机照片\
  │   └── 2024-Q1-M1-2-3_Canon EOS R5\
  ├── All_3_其他设备_手机照片\       ← 非目标设备
  │   └── 2024-Q2-M4-5-6_Oppo Find X7\
  ├── All_4_其他设备_相机照片\
  │   └── 2024-Q3-M7-8-9_Pentax K-1\
  ├── All_5_其他设备_未识别设备照片\  ← 未识别设备
  ├── 整理报告_xxx.html
  └── 整理报告_xxx.csv
```

`filelist.txt` 示例（ASCII 表头 + `|` 分隔，严格对齐）：
```
# 文件列表 — 共 3 个文件
# 目标目录: H:\All_相册_20260225\All_2_目标设备_相机照片\2024-Q1-M1-2-3_Canon EOS R5
# 列: 文件名(Filename) | 拍摄时间(Date) | 大小(Size) | 设备(Device) | 参数(Params) | 镜头(Lens) | GPS(GPS) | 原始路径(Source)
#
Filename      | Date                | Size    | Device       | Params                   | Lens            | GPS           | Source
--------------+---------------------+---------+--------------+--------------------------+-----------------+---------------+---------
IMG_0001.CR3  | 2024-01-15 10:30:00 | 25.3MB  | Canon EOS R5 | ISO400 f/2.8 1/200s 50mm | RF 50mm F1.2 L  | 31.23, 121.47 | E:\相机\...
IMG_0002.JPG  | 2024-01-15 10:31:00 | 8.2MB   | Canon EOS R5 | ISO200 f/4.0 1/500s 70mm | RF 24-70mm F2.8 |               | E:\相机\...
MVI_0003.MP4  | 2024-01-15 10:32:00 | 156.7MB | Canon EOS R5 |                          |                 |               | E:\相机\...
```

## 安装

```bash
pip install -r requirements.txt
```

首次运行若系统无 ffmpeg，会自动通过 `static-ffmpeg` 下载 ffprobe 二进制（约 70MB，一次性缓存）。

## 使用

### 组合使用

`--copy-all` 和 `--copy-unknown*` 可自由组合，互不干扰：

```bash
# 默认设备 + 也复制未识别照片（不加 --copy-all 则按默认品牌过滤）
python main.py --copy-unknown-photo --output-dir "H:\All_相册_20260225" --scan-dirs "E:\小米14_dyk"

python main.py --copy-unknown-photo --output-dir "H:\All_相册_20260225" --scan-dirs "E:\小米14_dyk" "E:\相册_E" "F:\相册_F" "G:\相册_G" "I:\相册_I" "S:\media_3t\相册_rpi"

# 不限设备 + 也复制未识别的照片（不含未识别视频）
python main.py --copy-all --copy-unknown-photo --scan-dirs "E:\小米14_dyk" --output-dir "H:\All_相册_20260225"

python main.py --copy-all --copy-unknown-photo --output-dir "H:\All_相册_20260225" --scan-dirs "E:\小米14_dyk" "E:\相册_E" "F:\相册_F" "G:\相册_G" "I:\相册_I" "S:\media_3t\相册_rpi"

# 不限设备 + 也复制未识别的视频（不含未识别照片）
python main.py --copy-all --copy-unknown-video --scan-dirs "E:\小米14_dyk" --output-dir "H:\All_相册_20260225"

# 不限设备 + 复制全部未识别文件（照片+视频）= 复制一切
python main.py --copy-all --copy-unknown --scan-dirs "E:\小米14_dyk" --output-dir "H:\All_相册_20260225"

# 只复制小米+苹果 + 同时复制未识别照片
python main.py --devices xiaomi,apple --copy-unknown-photo --scan-dirs "E:\小米14_dyk" --output-dir "H:\All_相册_20260225"

# 扫描 E 盘，不限设备，复制一切，64 线程
python main.py --scan-dirs "E:\" --copy-all --copy-unknown --workers 64 --output-dir "H:\All_相册_20260225"
```

### 高级选项

```bash
# 调整线程数（CPU 利用率低时可增大）
python main.py --workers 64

# 禁用目录排除规则，扫描一切（最彻底，包括系统目录）
python main.py --no-exclude --include-hidden

# 指定输出盘符
python main.py --dest-drive "F:\"

# 指定完整输出目录（已有目录则续写，不再自动生成时间戳文件夹）
python main.py --output-dir "H:\All_相册_20260225" --scan-dirs "E:\小米14_dyk"

# 试运行（不实际复制，仅生成报告）
python main.py --dry-run --scan-dirs "E:\小米14_dyk"
```

### 基础用法

```bash
# 默认扫描所有磁盘，只复制默认目标设备的文件
python main.py

# 指定扫描目录
python main.py --scan-dirs "E:\小米14_dyk"

# 同时扫描多个目录（空格分隔，每个路径用双引号包裹）
python main.py --scan-dirs --scan-dirs "E:\小米14_dyk" "E:\相册_E" "F:\相册_F" "G:\相册_G" "I:\相册_I" "S:\media_3t\相册_rpi"

# 试运行（不实际复制，仅生成报告预览效果）
python main.py --scan-dirs "E:\小米14_dyk" --dry-run
```

### 设备过滤控制

`--copy-all` 跳过品牌过滤，复制所有已识别设备（相机+手机），但**不影响**未识别文件的控制。

```bash
# 不限制目标设备品牌，复制所有已识别设备的文件
python main.py --copy-all --scan-dirs "E:\小米14_dyk"

# 自定义目标设备品牌（逗号分隔，不区分大小写）
python main.py --devices xiaomi,apple,canon,nikon,sony --scan-dirs "E:\小米14_dyk"
```

### 未识别文件控制

默认情况下，无 EXIF 或未识别设备的文件仅记录在报告中，不复制。可通过以下参数独立控制：

```bash
# 仅复制未识别的照片（不含视频）
python main.py --copy-unknown-photo --scan-dirs "E:\小米14_dyk"

# 仅复制未识别的视频（不含照片）
python main.py --copy-unknown-video --scan-dirs "E:\小米14_dyk"

# 同时复制所有未识别的照片和视频
python main.py --copy-unknown --scan-dirs "E:\小米14_dyk"
```

## CLI 参数一览

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--scan-dirs` | 所有磁盘 | 自定义扫描目录（空格分隔多个路径） |
| `--dest-drive` | `H:\` | 输出根盘符 |
| `--output-dir` | 自动生成 | 指定完整输出目录路径（如 `H:\All_相册_20260225`） |
| `--workers` | 32 | 并行线程数 |
| `--devices` | xiaomi,redmi,apple,huawei,honor,samsung,canon,dji | 目标设备品牌（逗号分隔） |
| `--copy-all` | False | 跳过品牌过滤，复制所有已识别设备的文件 |
| `--copy-unknown` | False | 复制全部未识别文件（照片+视频） |
| `--copy-unknown-photo` | False | 仅复制未识别的照片 |
| `--copy-unknown-video` | False | 仅复制未识别的视频 |
| `--no-exclude` | False | 禁用目录排除规则 |
| `--include-hidden` | False | 扫描隐藏目录 |
| `--dry-run` | False | 试运行，不实际复制 |
| `--verbose` / `-v` | False | 显示详细日志 |

> **组合逻辑**：`--copy-all` 控制已识别设备（相机/手机）的品牌过滤，`--copy-unknown*` 控制未识别文件，两者独立互不干扰。

## 视频元数据识别流程

1. **ffprobe 读取**：优先从视频文件自身提取 make/model/creation_time
2. **compatible_brands 推断**：ffprobe 未返回品牌时，从 MP4 兼容品牌标记推断（如 Canon → CAEP）
3. **内置 MP4 解析器**：ffprobe 失败时，用纯 Python 解析 MP4/MOV 的 `udta` box
4. **同目录照片推断**：以上均无品牌信息时，从同目录照片的 EXIF 多数投票推断

## 安全说明

- **源文件只读**：不修改/删除原始文件（扫描和复制均只读）
- **同盘复用**：目标盘（H:\）上已有的相同文件会被移动到新位置（不跨盘复制），原位置文件消失
- 支持中断后重新运行（已复制的文件自动跳过）
- Ctrl+C 第一次安全中断并生成部分报告，第二次强制退出
