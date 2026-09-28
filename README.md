# Immich Booru Tagger

当前稳定版：**1.1.2**。修复英文清理扫描整库的问题，按已有标签检索关联图片，不要求完成全库打标或中文补全；从 `1.0.x` 升级仍须迁移语言配置，见下方说明。

给 Immich 图片自动添加 Booru/WD14 标签，并可按离线词典补充中文层级标签。

它以独立容器运行，通过 Immich 官方 API 读取图片、创建标签并关联到资产，不直接访问 Immich 数据库，也不会修改原图。默认使用 `SmilingWolf/wd-swinv2-tagger-v3`，当前只处理图片。

默认同时写入英文标签和中文层级标签，可通过 `TAG_LANGUAGE_MODE=chinese` 改为优先中文、缺少译名时使用英文。例如，默认输出：

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

默认 Compose 固定使用 GHCR 的 `1.1.2` 镜像：

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

希望有中文就只新增中文、没有中文时使用英文，在 `.env` 中设置：

```env
TAG_LANGUAGE_MODE=chinese
```

模型仍生成英文标签供词典查询：有译名时只写中文，缺少译名或覆盖文件将译名设为空时写英文。例如 `blue_hair` 写为 `属性/蓝发`，没有译名的标签保留原文。该开关只影响后续新增标签，不删除已有英文或手工标签，也不影响 `auto:processed` 完成标记。已有完成标记的图片仍会跳过。`TAG_LANGUAGE_MODE` 还支持 `bilingual`（默认，中英）和 `english`（仅英文）。旧的两个语言开关已移除，配置仍有旧键时会拒绝启动并提示迁移。

默认保留双语，是为了兼容标准 Booru 标签搜索与其他工具，并支持更新词典后直接从已有英文标签补中文，无需重新推理。优先中文模式也会保留缺少译名的识别结果，这些英文标签以后可以补中文；已输出中文而未保留英文的条目，后续更换译名需要重新推理或手动整理。

如果图片已经有英文标签，只想补中文，可以运行：

```bash
docker compose run --rm immich-tagger \
  python -m immich_tagger.main --mode backfill-zh --dry-run --limit 20
docker compose run --rm immich-tagger \
  python -m immich_tagger.main --mode backfill-zh
```

补全模式不需要下载图片或加载推理模型，会遵守配置的图库/相册范围，重复运行只补缺少的中文标签。旧版 `zh/...` 标签不会自动删除；需要清理时请在 Immich 中手动处理。

## 一次性清理旧英文

先把日常输出设为 `TAG_LANGUAGE_MODE=chinese`。清理直接使用图片现有标签，不要求全库打标或中文补全完成：同一图片已有对应中文就清理英文，缺少中文则保留英文并继续处理其他图片。只有希望为缺中文的条目补中文时，才需要单独运行上面的 `backfill-zh`。两项操作都不加载模型。

```bash
# 预览（不加确认参数时也默认预览）
docker compose run --rm immich-tagger \
  python -m immich_tagger.main --mode cleanup-english --cleanup-scope recorded --dry-run

# 实际清理前先停止定时服务及其他标签写入任务
docker compose stop immich-tagger
# 维护模式；在 .env 配置管理员账号的 CLEANUP_ADMIN_API_KEY（queue.read、queue.update）
docker compose run --rm immich-tagger \
  python -m immich_tagger.main --mode cleanup-english --cleanup-scope recorded \
  --confirm-cleanup-english --cleanup-maintenance
# 清理结束且后台队列恢复后，再启动定时服务
docker compose up -d
```

- `recorded`（默认）：从 `state/assignments.jsonl` 读取本账号曾由程序新增的标签，直接检查记录中的图片，不搜索整库。记录缺失或损坏会报错；记录不完整的条目不会清理。
- `catalog`：显式传入 `--cleanup-scope catalog`，先读取账号已有标签，只检索可翻译英文标签关联的图片，再核对图片当前是否有对应中文，不逐张读取整库无关图片。可覆盖没有历史记录的标签，也会处理同名手工英文标签。

清理先收集关联图片的候选快照，再执行解除操作，避免分页过程中删除标签造成漏图。日志中的“读取标签详情”是只读查询现有标签，不是补标签；可用 `--limit 20` 先清理少量候选。新版搜索按多个英文标签取并集，旧版搜索逐标签查询并去重，均保留图库/相册范围，不退回无标签筛选的整库扫描。

两种方式都遵守图库/相册和排除范围；没有 include 时处理账号可见图片。只有图片当前已存在对应的 `属性/...`、`角色/...` 或 `评级/...` 标签才会解除英文关联。无译名、空译名、自定义完成标记、`auto:processed`、旧版 `zh/...` 都保留。不会删除图片或全局标签对象，因此标签侧边栏仍可能留下空的英文标签。

实际执行必须带 `--confirm-cleanup-english`，与 `--dry-run` 互斥。`--cleanup-maintenance` 显式启用队列维护模式（已验证 Immich 3.2.2）：每张图片清理前暂停 `metadataExtraction`、`sidecar` 并等待活动任务结束，快速解除英文关联，先完成 XMP 写入、再提取元数据，恢复原有暂停状态，最终核对英文缺失及保留标签完整后才提交成功。管理员 Key 仅用于队列，图片仍由原账号操作。维护期间这两个全局队列会暂时影响其他图片的元数据处理，原本暂停的队列也会暂时运行以完成复核。

强制中断后保留 `state/cleanup-queues.json`，重新执行同一条维护命令会先恢复队列；也可单独运行 `python -m immich_tagger.main --restore-cleanup-queues`。支持 `--limit`、断点续跑和失败重试，定时服务不会自动恢复清理任务。逐项审计保存在 `state/cleanup.jsonl`。详见 [清理与恢复](docs/configuration.md#一次性英文清理)。

没有管理员队列权限时，去掉 `--cleanup-maintenance` 使用兼容路径：逐标签至少等待 5 秒，并做两次延迟回读及有限重试。15 个标签即有至少 85 秒固定等待；日志会显示当前标签、本张累计耗时和实际等待阶段。延时只能降低 Immich 异步回写的冲突概率；两种模式都无法阻止清理结束后的外部元数据导入重新添加标签。

## 从 1.0.x 升级

先停止写入任务并备份 `state/`。删除 `.env` 中的 `ENGLISH_TAGS_ENABLED` / `TRANSLATIONS_ENABLED`，按 [迁移对照表](docs/configuration.md#语言配置迁移) 设置 `TAG_LANGUAGE_MODE`；保留原 `state/` 和 `models/` 挂载。

在 `.env` 中更新镜像版本：

```env
TAGGER_IMAGE=ghcr.io/mikusaa/immich-booru-tagger:1.1.2
```

然后拉取镜像并重建容器：

```bash
docker compose pull
docker compose up -d
```

保留旧语言配置会导致启动报错。升级后旧版未完成队列会重新扫描，已有标签和完成标记保留；英文清理仍须单独执行，不会自动启动。

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
ghcr.io/mikusaa/immich-booru-tagger:1.1.2
```

正式版本使用 Git 标签 `v1.1.2`，对应镜像标签 `1.1.2`；`1.1` 和 `latest` 会随对应正式发布更新，`main` 用于开发版。运行 `python -m immich_tagger.main --version` 或访问服务根路径 `/` 可查询版本。当前没有 ARM64 或 CUDA 构建；需要其他平台请参考 [镜像发布说明](docs/releasing.md) 自行构建。

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
