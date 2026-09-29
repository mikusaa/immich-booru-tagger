# GitHub Actions 与镜像发布

`.github/workflows/publish-image.yml` 负责测试、构建和发布容器，不需要额外的构建服务器。

## 触发规则

| 触发 | 行为 | 标签 |
| --- | --- | --- |
| Pull Request | 运行测试、构建镜像和离线冒烟测试 | 不发布 |
| 推送 `main` | 测试通过后发布 | `main`、完整 SHA |
| 推送 `v1.3.0` | 测试通过后发布正式版本 | `1.3.0`、`1.3`、`latest`、SHA |
| 推送预发布标签 | 发布预发布版本 | 版本标签、SHA，不更新 `latest` |
| 手动运行 | 对选定分支执行相同流程 | 由元数据规则决定 |

镜像名根据 `github.repository` 自动生成；本仓库为 `ghcr.io/mikusaa/immich-booru-tagger`。发布任务使用内置 `GITHUB_TOKEN`，仅需要 `packages: write`；仓库或组织的 Actions 策略必须允许该权限。

## 发布前检查

1. 确认默认分支为 `main`，并在仓库 Actions 设置中允许工作流运行。
2. 提交到 `main` 或推送 `v*` 标签，等待 `test` 和 `image` 两个任务成功。
3. 在 Packages 页面检查镜像可见性。Public 镜像可匿名拉取，Private 镜像需要先 `docker login ghcr.io`。
4. 生产环境建议固定版本或 digest，而不是长期使用 `main`。

工作流当前只构建 `linux/amd64` CPU 镜像，不包含 ARM64、CUDA 或 DeepDanbooru 依赖。需要其他平台时，可以在本地修改 `platforms` 并自行构建：

```bash
docker build --platform linux/amd64 -t immich-booru-tagger:local .
```

新增工作流文件不会自动产生镜像，必须等一次对应的 Actions 运行成功后，GHCR 中才会有可拉取的标签。

## 1.3.0

正式版本使用 Git 标签 `v1.3.0`，GHCR 镜像为 `ghcr.io/mikusaa/immich-booru-tagger:1.3.0`。Compose 和 `.env.example` 默认固定该版本。发布内容与升级说明见 [更新记录](../CHANGELOG.md)。

本版本新增 `ASSET_SORT_ORDER=desc/asc`，默认从新到旧。升级或切换顺序后，下一次任务重新扫描未完成候选；已有完成标记、标签和失败记录保留。排序不变的后续重启继续原队列。

本版本包名为 `app`，入口为 `python -m app.main`。从 `1.1.x` 升级必须同步修改手动命令和自定义脚本，旧镜像使用的 `immich_tagger.main` 不再提供。本地验证使用 `immich-booru-tagger:local`，见 [README](../README.md#从源码构建)。

下一次发布前确认 `app.__version__`、Compose 默认镜像、环境变量示例和 README 的版本一致。推送版本标签后，等待测试、离线重启验收、运行镜像冒烟检查和 GHCR 推送全部成功，再创建 GitHub Release。不要覆盖已发布的版本标签；使用新的版本号。
