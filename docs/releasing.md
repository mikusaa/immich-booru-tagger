# GitHub Actions 与镜像发布

`.github/workflows/publish-image.yml` 负责测试、构建和发布容器，不需要额外的构建服务器。

## 触发规则

| 触发 | 行为 | 标签 |
| --- | --- | --- |
| Pull Request | 运行测试、构建镜像和离线冒烟测试 | 不发布 |
| 推送 `main` | 测试通过后发布 | `main`、完整 SHA |
| 推送 `v1.4.0` | 测试及实体验收门槛通过后发布正式版本 | `1.4.0`、`1.4`、`latest`、SHA |
| 推送预发布标签 | 发布预发布版本 | 版本标签、SHA，不更新 `latest` |
| 手动运行 | 对选定分支执行相同流程 | 由元数据规则决定 |

镜像名根据 `github.repository` 自动生成；本仓库为 `ghcr.io/mikusaa/immich-booru-tagger`。发布任务使用内置 `GITHUB_TOKEN`，仅需要 `packages: write`；仓库或组织的 Actions 策略必须允许该权限。

## 发布前检查

1. 确认默认分支为 `main`，并在仓库 Actions 设置中允许工作流运行。
2. 提交到 `main` 或推送 `v*` 标签，等待 `test`、两个架构的 `image` 和 `manifest` 任务成功。
3. 在 Packages 页面检查镜像可见性。Public 镜像可匿名拉取，Private 镜像需要先 `docker login ghcr.io`。
4. 生产环境建议固定版本或 digest，而不是长期使用 `main`。

工作流在 `ubuntu-24.04` / `ubuntu-24.04-arm` 原生 runner 上分别测试并构建 `linux/amd64` / `linux/arm64` CPU 镜像，缓存按架构隔离。每个架构通过核心测试、离线恢复、真实依赖的随机权重推理与健康检查后，才推送对应 digest；`manifest` 必须等待两个架构均成功，再合并并添加版本标签。PR 不推送镜像。`macos-14` 另跑核心测试及原生 CPU 推理契约，不代表实体 MPS 验收。

默认镜像不包含 CUDA 或 DeepDanbooru。本地构建自动使用宿主架构，也可显式指定：

```bash
docker build --platform linux/amd64 -t immich-booru-tagger:local .
docker build --platform linux/arm64 -t immich-booru-tagger:local-arm64 .
```

新增工作流文件不会自动产生镜像，必须等一次对应的 Actions 运行成功后，GHCR 中才会有可拉取的标签。

## 1.3.0

历史正式版本使用 Git 标签 `v1.3.0`，GHCR 镜像为 `ghcr.io/mikusaa/immich-booru-tagger:1.3.0`（仅 AMD64）。发布内容与升级说明见 [更新记录](../CHANGELOG.md)。

本版本新增 `ASSET_SORT_ORDER=desc/asc`，默认从新到旧。升级或切换顺序后，下一次任务重新扫描未完成候选；已有完成标记、标签和失败记录保留。排序不变的后续重启继续原队列。

本版本包名为 `app`，入口为 `python -m app.main`。从 `1.1.x` 升级必须同步修改手动命令和自定义脚本，旧镜像使用的 `immich_tagger.main` 不再提供。本地验证使用 `immich-booru-tagger:local`，见 [README](../README.md#从源码构建)。

下一次发布前确认 `app.__version__`、Compose 默认镜像、环境变量示例和 README 的版本一致。推送版本标签后，等待测试、离线重启验收、运行镜像冒烟检查和 GHCR 推送全部成功，再创建 GitHub Release。不要覆盖已发布的版本标签；使用新的版本号。

## 1.4.0 发布门槛

源码、Compose 与示例配置的版本为 `1.4.0`，正式版本使用标签 `v1.4.0`，不覆盖 `v1.3.0`。发布前必须查看 [推理验收记录](inference-validation.md)：至少 100 张固定图片的同一台 Apple Silicon 实机 CPU/MPS 完整概率对比、1000 次 MPS 稳定性及隔离 Immich 小批量集成均通过后，才可推送正式标签。当前采用 M5 实测证据，不要求重复完成 M4 专项验收。GitHub Release 标题仅使用版本号 `1.4.0`。

普通 CI 仅验证构建、架构及客户端契约，不能替代实机 MPS 验收。M4/M5 使用通用 ARM64/MPS 路径，性能结论仅适用于报告中的机器与样本；M4 尚未单独实测。发布后检查 manifest 同时包含 AMD64/ARM64；切换设备无需数据库迁移或重新扫描队列。

稳定 `v*` 标签（不含预发布后缀）在合并 manifest 前执行 `python -m scripts.check_release_acceptance`。CPU/MPS benchmark JSON 分别保存为 `docs/validation/apple-silicon-cpu.json`、`apple-silicon-mps.json`，隔离集成报告保存为 `immich-integration.json`；完整概率 NPZ 与原始样本单独保存供复核。当前已归档 M5 报告，检查通过。缺少或不合格的记录会阻断正式版本 manifest；开发版和预发布镜像仍可构建。此检查核验原生 macOS ARM64 M 系列机器、样本一致性、阈值、稳定性和集成结果，报告真实性仍需人工审阅。
