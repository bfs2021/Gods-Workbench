# 内置 LUT 包（QIN-553）

本目录提供神工坊预置的 Adobe Cube LUT（`.cube`）演示套件。首次进入 LUT 库时，
服务端会把本目录文件复制到运行数据根 `registry_uploads/builtin_luts/` 的受管
副本，并登记为 `builtin` LUT 资产；本目录只是只读分发源。

## 清单

| 文件 | TITLE | 用途 |
| --- | --- | --- |
| `builtin_lut_neutral.cube` | GW Neutral | 恒等基准 |
| `builtin_lut_warm.cube` | GW Warm | 暖调 |
| `builtin_lut_cool.cube` | GW Cool | 冷调 |
| `builtin_lut_high_contrast.cube` | GW High Contrast | 高对比 |
| `builtin_lut_cinematic.cube` | GW Cinematic | 电影感（lifted blacks + rolled highlights） |

规格：`LUT_3D_SIZE 8`，512 采样点，全域 `[0,1]`，纯文本 ASCII。

## 来源与许可

全部文件为**程序生成的演示级基准转换**（通道线性/平滑公式，与
`gw/asset_registry/builtin_luts.py` 中 `BUILTIN_LUT_SPECS` 的 formula 字段
一一对应），不含任何第三方 LUT 数据派生，不来自外部素材库。

- 许可：Apache License 2.0（与仓库主代码一致），(c) 2026 Gods-Workbench Authors。
- 定位是开箱可用的演示基准，用于让 LUT 管线免导入即可验证；专业调色请
  导入自有 LUT。

## 用户侧语义

- 内置条目可**隐藏**（卡片眼睛开关），不可移入回收站、不可归档、不可
  重命名源文件（服务端 403 守卫）。
- 重复启动/重复进入 LUT 库按稳定 `asset_id` 幂等登记，不产生重复条目。
- 隐藏后可通过「恢复显示内置 LUT」一键还原。
