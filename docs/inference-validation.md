# ARM64 / MPS 推理验收记录

## 状态（2026-10-03）

本地实测记录对应版本 `1.4.0`；镜像构建和发布状态见 [GitHub Actions](https://github.com/mikusaa/immich-booru-tagger/actions/workflows/publish-image.yml)。按本次交付约定，以 Apple M5、16 GiB 内存、macOS 27.2、原生 ARM64 的实测证据完成 Apple Silicon 验收。M4/M5 使用相同 ARM64/MPS 接口与代码路径，无机型分支；M4 尚未单独实测，以下性能与正确性结论限于当前 M5 和合成样本，真实图库仍需部署时核验。

## 默认模型与本地对比

模型为 `SmilingWolf/wd-swinv2-tagger-v3`，固定缓存快照 `627aef95638667ddcaa3ac8ae625e88ea5b02f51`，FP32、448×448 输入、10861 个类别。原生依赖为 torch 2.6.0、torchvision 0.21.0、timm 1.0.15、wdtagger 0.16.0、Pillow 11.1.0、numpy 1.26.4；容器使用同版本 CPU wheel（torch 带 `+cpu` 本地版本后缀）。

使用 100 张固定合成图片，覆盖五组尺寸/纵横比、RGBA 透明和 JPEG EXIF 旋转。每次额外预热五次，CPU 测量 100 次、MPS 测量 1000 次；完整概率另行采集，未计入吞吐。各报告包含逐图片 SHA256、模型快照、环境及内存采样。

| 环境 | CPU 线程 | 张/秒 | P50 秒 | P95 秒 |
| --- | --- | --- | --- | --- |
| 原生 CPU | 1 | 2.257 | 0.439 | 0.458 |
| 原生 CPU | 2 | 3.307 | 0.300 | 0.321 |
| 原生 CPU | 4 | 3.576 | 0.273 | 0.322 |
| 原生 MPS | 2 | 7.322 | 0.135 | 0.148 |
| Linux ARM64 容器 CPU | 2 | 1.153 | 0.829 | 0.998 |

MPS 与原生 CPU 双线程的全部概率最大绝对偏差为 `8.8810921e-6`，标签集合、评级完全一致。容器 CPU 与原生 CPU 的偏差为 `8.7022781e-6`，标签与评级同样一致，均低于 `1e-3`。

MPS 1000 次运行无设备错误或非有限输出，fallback/fast math 均关闭；后半程 MPS allocated 中位数保持 `417503488` 字节，driver allocated 保持 `1421934592` 字节，RSS 中位数无增长，平台期检查通过。完整报告包含所有采样；平台期检查只是筛查，不等于任何运行时长均不存在泄漏。

这些是单轮工程验证，无严格功耗/温度控制。容器测试发生在后续集成阶段，隔离服务已启动但空闲，虚拟机资源及背景负载没有与原生完全对齐；不可据此认定所有 ARM64 容器都比原生慢。对当前机器与该样本，优先使用原生 MPS；CPU 恢复可从四线程开始测量。其他机型、内存容量和真实图库的性能应在部署时重新测量，不作为本次交付的 M4 专项前置条件。

## 容器、CI 与集成

- 本地、AMD64 和 ARM64 容器各通过 298 项测试；AMD64 在本机通过架构模拟执行，两个架构的原生 runner 由发布 CI 分别验证。
- 两个运行镜像构建成功，真实 PyTorch/timm/wdtagger 的随机权重 SwinV2 离线 CPU 推理通过，输出 10861 类。该冒烟检查不证明默认预训练模型的识别质量。
- 两个架构的 stop/kill/扫描中断后重建恢复均通过；健康检查返回 healthy、版本 1.4.0、未准备时 `progress.inference=null`。
- Actions 工作流通过 actionlint；launchd 示例 XML 解析、Python 编译及 diff 空白检查通过。本地验收未实际安装 launchd，远程流水线结果见上述 Actions 链接。
- 新建隔离 Immich 3.2.2 实例，使用独立账号与四张合成上传图片。预览计划四张且标签未变；MPS 完成一张后注入后端故障，无失败计数，CPU 续跑同一队列后累计完成四张。全部手工标签保留，完成标记正常，重复运行零处理；延迟回读全部标签与写入日志精确一致。
- 隔离实例已停止，测试卷与证据保留；没有使用生产配置、API Key 或图库。

集成发现现有相册接口限制：该 Immich 实例的相册响应不含 `assets`，客户端在扫描时正确拒绝无范围搜索。本次使用只含四张样本的独立测试账号全范围验收，未修改相册兼容逻辑；依赖相册范围的部署仍需单独核验该接口。

## Mac 双击启动器验收

在本机的隔离临时目录（路径含空格）验证了自动下载 uv、原生 Python 3.11.16 与固定依赖，未依赖预装 Python/uv。Python 下载禁止安装全局快捷入口，环境与缓存保存在项目 `.macos/` 内。真实 torch 2.6.0、wdtagger 可导入，MPS 可用。

首次配置、连接检查及空队列预览通过，第二次启动复用环境直接进入菜单。仅连接本地模拟 Immich，调用读取标签和搜索接口；预览未创建持久化任务状态。此安装验收未执行图片推理或写入，模型正确性与隔离写入证据见上文。原始 [安装验收报告](validation/macos-launcher.json) 已归档，详细日志保存在 `/private/tmp/ibt-native-launcher-final-smoke/`。

新增 14 项启动器测试覆盖配置隔离与权限、相册名称选择、预览失败阻断写入、设备切换、launchd 参数及启停、中断等待、下载校验流程、重复启动和安装失败重试。launchd 启停使用模拟命令验证，没有在本机安装登录任务。

## 证据与正式发布门槛

本次 M5 的完整 CPU 双线程、MPS 与隔离 Immich JSON 已原样归档为 [CPU 报告](validation/apple-silicon-cpu.json)、[MPS 报告](validation/apple-silicon-mps.json)、[集成报告](validation/immich-integration.json)。CPU 单/四线程与容器 CPU 报告也保存在 `docs/validation/`，报告保留真实芯片、模型快照及逐图 SHA256。

原始概率 NPZ、样本与集成脚本仍保存在 `/private/tmp/ibt-arm64-acceptance/`，未纳入仓库。报告中的 `scores_file` 保留原名 `cpu-2.scores.npz`、`mps.scores.npz` 等，需在该原始目录解析；长期复核需另行保存这些临时文件及样本。benchmark 的使用方法见 [macOS 文档](macos.md)。

稳定标签的 manifest 发布步骤执行 `scripts.check_release_acceptance`，现接受同一台原生 macOS ARM64 Apple M 系列机器的证据。已归档 M5 报告通过至少 100 张固定图片、FP32、概率误差 ≤ `1e-3`、评级一致、1000 次 MPS 稳定性、禁用 fallback/fast math 及隔离集成检查，解除 M4 专项限制。正式发布还必须等待原生 AMD64/ARM64 CI、容器恢复及冒烟检查全部成功，再合并 manifest；流程见 [发布说明](releasing.md)。
