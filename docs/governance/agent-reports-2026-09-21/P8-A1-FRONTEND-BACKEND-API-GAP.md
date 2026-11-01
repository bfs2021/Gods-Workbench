# P8-A1 前后端 API 缺口基线更新

## 2026-11-02：QIN-514 / GitHub #537

QIN-514 在资产设置中接入旧 JPEG 无损转码工具。前端调用与后端实现均新增以下路径，已加入 `tests/contracts/projects/test_phase8_frontend_backend_api_gap.py` 的 `KNOWN_IMPLEMENTED`：

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/assets/transcode-jpeg-lossless/scan` | 扫描当前主体可见的待转码 JPEG 与工具状态 |
| POST | `/api/assets/transcode-jpeg-lossless` | 创建异步无损转码任务 |
| GET | `/api/assets/transcode-jpeg-lossless/jobs/{p}` | 按稳定 job ID 轮询任务 |
| POST | `/api/assets/transcode-jpeg-lossless/jobs/{p}/cancel` | 请求在当前文件完成后安全取消 |

这些路径是本票有意新增的已实现接口，不属于未实现路径集合。`KNOWN_UNIMPLEMENTED` 与无静态前端调用的契约基线不变。

### 验证

- 首次 CI 在前端路径全集与已实现路径集合两项断言中发现上述新增路径未登记。
- 本地单独复跑确认是基线未更新，不是运行时或时序偶发。
- 基线更新后的窄测：`python -m pytest tests/contracts/projects/test_phase8_frontend_backend_api_gap.py -v`，7 passed。
- 完整 CI 将随 QIN-514 更新提交重新运行；本报告记录基线变更，不提前宣称全量门禁通过。
