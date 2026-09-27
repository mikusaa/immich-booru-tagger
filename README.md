# Immich Booru Tagger

当前稳定版：**1.0.0**。

给 Immich 图片自动添加 Booru/WD14 标签，并可按离线词典补充中文层级标签。

它以独立容器运行，通过 Immich 官方 API 读取图片、创建标签并关联到资产，不直接访问 Immich 数据库，也不会修改原图。默认使用 `SmilingWolf/wd-swinv2-tagger-v3`，当前只处理图片。

默认同时写入英文标签和中文层级标签，可通过 `ENGLISH_TAGS_ENABLED=false` 关闭英文标签输出。例如，默认输出：

```text
blue_hair
hatsune_miku
属性/蓝发
角色/初音未来（VOCALOID）
评级/全年龄
```

## 快速开始

### 1. 准备 API Key

在 Immich 中创建专用 API Key，并授予：

- `asset.read`、`asset.view`、`asset.update`
- `tag.read`、`tag.create`、`tag.asset`
- 如果按相册筛选，再授予 `album.read`

不需要资产删除权限。建议先用一个范围明确的账号和测试相册验证。

### 2. 启动容器

```bash
git clone https://github.com/mikusaa/immich-booru-tagger.git
cd immich-booru-tagger
cp .env.example .env
mkdir -p models state
```

编辑 `.env`，至少填写 Immich 地址、API Key 和处理范围：

```env
IMMICH_BASE_URL=http://immich-server:2283
IMMICH_API_KEY=replace-with-your-api-key
IMMICH_INCLUDE_ALBUM_IDS=00000000-0000-0000-0000-000000000000
```

`IMMICH_BASE_URL` 不要带 `/api`。图库和相册 ID 必须是 UUID，不是目录名；不设置任何 include 范围时，会处理当前 API 用户可见的全部图片，请谨慎使用。

默认 Compose 固定使用 GHCR 的 `1.0.0` 镜像：

```bash
docker compose pull
```

首次启动前，先检查连接和读取标签的权限：

```bash
docker compose run --rm immich-tagger python -m immich_tagger.main --test-connection
```

### 3. 预览后再写入

先预览少量图片。预览不会写 Immich，也不会写失败记录，但首次运行仍会下载模型并缓存到 `models`：

```bash
docker compose run --rm immich-tagger \
  python -m immich_tagger.main --dry-run --limit 20
```

确认日志中的标签后，再执行实际写入：

```bash
docker compose run --rm immich-tagger \
  python -m immich_tagger.main --limit 20
```

确认结果后启动定时服务：

```bash
docker compose up -d
docker compose logs -f
```

默认每天上海时间 02:00 执行；没有未完成任务时，启动容器不会立即处理整库。配置兼容的未完成任务默认启动即续跑。模型与运行状态分别保存在 `models/` 和 `state/`，更新镜像时不会丢失。

## 中文标签

中文来自随镜像发布的离线词典，不调用在线翻译服务。标签直接使用 Immich 的层级标签：

```text
属性/蓝发
角色/初音未来（VOCALOID）
评级/全年龄
```

在 Immich 的“账户设置 → 功能 → 标签”中启用标签后，可在标签侧边栏展开“属性、角色、评级”。

只想新增中文标签时，在 `.env` 中设置：

```env
ENGLISH_TAGS_ENABLED=false
TRANSLATIONS_ENABLED=true
```

模型仍生成英文标签供词典查询，但只把有译名的中文标签写入 Immich；缺少译名或被覆盖文件禁用的条目会跳过。该开关只影响后续新增标签，不删除已有英文或手工标签，也不影响 `auto:processed` 完成标记。已有完成标记的图片仍会跳过。英文和中文输出不能同时关闭。

默认保留双语，是为了兼容标准 Booru 标签搜索与其他工具、保留缺少译名的识别结果，并支持更新词典后直接从已有英文标签补中文，无需重新推理。仅中文模式下，后续补全无法恢复没有保留下来的英文标签，需要重新推理。

如果图片已经有英文标签，只想补中文，可以运行：

```bash
docker compose run --rm immich-tagger \
  python -m immich_tagger.main --mode backfill-zh --dry-run --limit 20
docker compose run --rm immich-tagger \
  python -m immich_tagger.main --mode backfill-zh
```

补全模式不需要下载图片或加载推理模型，会遵守配置的图库/相册范围，重复运行只补缺少的中文标签。旧版 `zh/...` 标签不会自动删除；需要清理时请在 Immich 中手动处理。

## 常用操作

```bash
# 处理一轮（最多 BATCH_SIZE 张）
docker compose run --rm immich-tagger python -m immich_tagger.main --mode single

# 本轮处理所有候选后退出
docker compose run --rm immich-tagger python -m immich_tagger.main --mode continuous

# 查看或重置失败记录
docker compose run --rm immich-tagger python -m immich_tagger.main --show-failures
docker compose run --rm immich-tagger python -m immich_tagger.main --reset-failure ASSET_ID
docker compose run --rm immich-tagger python -m immich_tagger.main --reset-failures
```

成功写入并回读确认后，程序才会添加 `auto:processed` 标记。部分标签写入失败时，下一轮会保留已成功的标签并补齐缺项。`state/` 同时保存 SQLite 候选队列、累计进度、失败记录、单写入锁和追加式写入记录，不要让多个容器共享同一状态目录并发运行。

运行日志使用中文，默认每 10 秒显示当前阶段、扫描或处理进度、当前操作与耗时。已扫描完成的任务重启后直接处理剩余队列；扫描中断则重新核对候选并去重。实时进度见 `curl -s http://127.0.0.1:8000/metrics`，离线任务摘要使用 `--progress-status`。旧版首次升级仍需扫描，具体迁移与回退方法见 [完整配置](docs/configuration.md)。

## 镜像与平台

仓库的 GitHub Actions 会在测试通过后构建并发布 `linux/amd64` CPU 镜像到 GHCR：

```text
ghcr.io/mikusaa/immich-booru-tagger:1.0.0
```

正式版本使用 Git 标签 `v1.0.0`，对应镜像标签 `1.0.0`；`1.0` 和 `latest` 会随正式发布更新，`main` 用于开发版。运行 `python -m immich_tagger.main --version` 或访问服务根路径 `/` 可查询版本。当前没有 ARM64 或 CUDA 构建；需要其他平台请参考 [镜像发布说明](docs/releasing.md) 自行构建。

## 文档

- [完整配置与运行方式](docs/configuration.md)
- [产品需求与当前范围](docs/PRD.md)
- [相对原版的具体改动](docs/upstream-changes.md)
- [本地开发、测试与词典构建](docs/development.md)
- [GitHub Actions 与 GHCR 发布](docs/releasing.md)
- [词典来源与许可](data/NOTICE.md)

## 常见问题

**看不到标签？** 在 Immich 的账户设置中启用标签功能，并刷新页面。标签在侧边栏的“属性、角色、评级”下，不会出现在 CLIP 搜索模型设置里。

**为什么没有立即处理整库？** 默认服务只在每天 02:00 执行；先用 `--limit` 预览和验收，再调整定时配置。

**为什么第一次很慢？** 模型权重不放进镜像，首次推理需要从 Hugging Face 下载并缓存。

**如何缩小范围？** 设置 `IMMICH_INCLUDE_LIBRARY_IDS` 或 `IMMICH_INCLUDE_ALBUM_IDS`；排除范围使用 `IMMICH_EXCLUDE_LIBRARY_IDS`，排除优先于包含。

**能否换模型？** 仅支持与当前 `timm/wdtagger` 加载方式和 `selected_tags.csv` 兼容的模型。详细限制见 [完整配置](docs/configuration.md)。
