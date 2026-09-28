# 更新记录

## 1.2.0 — 2026-09-28

英文清理新增标签基线与续跑保护，程序包名统一为 `app`。镜像：`ghcr.io/mikusaa/immich-booru-tagger:1.2.0`（Linux AMD64 / CPU）。

- 主程序目录及 Python 包名从 `immich_tagger` 改为 `app`，入口统一为 `python -m app.main`；不保留旧包兼容入口。现有自定义命令、导入及启动脚本需同步更新，CLI 参数保持不变。
- 英文标签清理模块更名为 `app/english_cleanup.py`；独立资产清理工具归入 `tools/`，使用 `python -m tools.cleanup_failed_assets`。
- 公共测试模拟对象和重启辅助程序归入 `tests/support/`，同步调整 Docker、CI 和容器恢复验收。移除未使用的性能监控模块，停止跟踪根目录旧的空失败记录。
- 词典、模型和状态路径保持不变，数据库、任务指纹及现有队列恢复记录无需迁移。Compose 和 `.env.example` 默认镜像更新为 `1.2.0`。
- 英文清理在删除前同步保存完整标签基线，包含标签 ID、路径、计划移除项与保留项。保留标签缺失时列出明细并中止；基线独立于队列恢复记录，续跑、更换范围或处理上限都须先复核，避免将事故后变空的图片当成普通空标签图片跳过。普通打标和补全也会阻止绕过未完成复核。
- 维护模式先排空原有 XMP 队列，再逐个解除英文关联并等待对应 sidecar 队列排空，期间保持元数据提取暂停，减少本脚本对同一图片的并发写入。该措施不能修复 Immich 的 XMP 读写失败；未增加自动标签恢复或修改服务端配置。

### 升级说明

停止写入任务并备份 `state/`，将 `.env` 的 `TAGGER_IMAGE` 改为 `ghcr.io/mikusaa/immich-booru-tagger:1.2.0`，执行 `docker compose pull`。手动命令改为 `python -m app.main`，自定义 Python 导入改为 `app.*`；不保留旧的 `immich_tagger` 包。CLI 参数与挂载路径不变。从 `1.0.x` 升级仍需按历史说明迁移语言配置。

未完成的维护清理使用新入口及原参数续跑，或执行 `python -m app.main --restore-cleanup-queues` 独立恢复队列。新增 `cleanup-pending.json` 保存待复核图片的完整标签基线，复核后归档至 `cleanup-baselines.jsonl`；请连同状态目录备份，不要删除或改目录绕过保护。清理及复核结束后再启动定时服务。

旧版事故发生前没有保存的标签无法由新版本重建。若已出现保留标签缺失，先处理 Immich 文件读写问题并核对恢复；脚本不自动回填。当前修复可减少脚本引起的并发并阻止错误续跑，不能保证服务端文件读写故障不再发生。

本地与 Linux AMD64 容器均通过 227 项测试，含完整基线、写盘失败、变更配置及 SIGKILL 续跑保护。隔离 Immich 3.2.2、默认 sidecar 并发 5 下，7 轮正常清理均从 32 个标签变为 17 个，主动刷新后保留标签完整；只读样本仍触发服务端丢失，但首次及后续两次运行均中止并保留基线。离线容器停止/强杀/扫描恢复、编译和运行镜像健康检查通过。详见 [开发验收记录](docs/development.md)。

## 1.1.2 — 2026-09-28

修复英文清理扫描整库的问题，镜像：`ghcr.io/mikusaa/immich-booru-tagger:1.1.2`（Linux AMD64 / CPU）。

- 修复 `catalog` 英文清理遍历整库、逐张补取详情的问题：先读取已有标签，再按可翻译英文标签筛选关联图片；新旧搜索均保留标签筛选，跨标签去重，继续核对同图中文及图库/相册范围。
- 将扫描日志“补取图片标签 / 补取详情”改为“读取图片现有标签 / 读取标签详情”，明确清理不要求全库打标或中文补全完成。候选快照、预览和断点续跑规则保持兼容。

### 升级说明

从 1.1.1 升级无需迁移配置或状态。停止写入任务并备份 `state/`，将 `.env` 的 `TAGGER_IMAGE` 改为 `ghcr.io/mikusaa/immich-booru-tagger:1.1.2`，执行 `docker compose pull`，再用原清理命令续跑。扫描未完成的任务会按现有标签重新收集候选；扫描已完成的任务继续原队列，无需清空状态或先完成打标。清理及维护队列恢复完成后再执行 `docker compose up -d`。

清理仍先收集相关候选快照再删除，避免分页变化漏图；缺少对应中文的英文关联保留，不阻塞其他图片。`recorded` 和维护模式的使用方式不变。

本地通过 197 项自动化测试，包括新旧搜索的标签筛选、稀疏标签图库、跨标签去重，以及已有的中断恢复与维护队列测试。模拟 1,000 张未打标图片及 7 张带标签图片时，仅查询 4 张相关图片的标签详情，并清理其中 3 张；该结果来自模拟 API，不代表线上吞吐。

## 1.1.1 — 2026-09-28

修复英文清理的异步回写竞态与逐标签固定等待，镜像：`ghcr.io/mikusaa/immich-booru-tagger:1.1.1`（Linux AMD64 / CPU）。

- 英文标签清理增加显式的 `--cleanup-maintenance`：逐张暂停元数据任务、快速清理、先完成 XMP 写入再提取元数据，最终核验后才提交成功，避免 Immich 异步任务恢复旧英文标签；已验证 Immich 3.2.2。
- 单独的 `CLEANUP_ADMIN_API_KEY` 只用于队列控制；持久化保存队列暂停状态，强制退出可续跑恢复，也可用 `--restore-cleanup-queues` 独立恢复。维护流程受原有单写入锁保护。
- 清理日志显示标签序号、重试轮次、本张累计耗时及队列积压，区分等待异步回写与回读请求。未启用维护模式时保留兼容行为。

### 升级说明

从 1.1.0 升级无需迁移配置或 SQLite 状态。停止写入任务并备份 `state/`，将 `.env` 的 `TAGGER_IMAGE` 改为 `ghcr.io/mikusaa/immich-booru-tagger:1.1.1`，执行 `docker compose pull`。使用维护模式前在 `.env` 配置管理员账号的 `CLEANUP_ADMIN_API_KEY`（`queue.read`、`queue.update`），并在原有确认清理命令后追加 `--cleanup-maintenance`。图片仍使用原账号 Key，清理范围保持原设置。

维护模式会临时控制实例全局的 `sidecar`、`metadataExtraction` 队列，包括暂时运行原本暂停的积压任务；请先停止其他标签写入、元数据导入和队列控制任务。强制退出后重新执行同一维护命令恢复并续跑，或单独执行 `python -m immich_tagger.main --restore-cleanup-queues`。清理及队列恢复完成后再运行 `docker compose up -d`。未启用维护模式时保留兼容路径及原有等待行为；从 1.0.x 升级仍须按下方说明迁移语言配置。

本地通过 185 项自动化测试，包含 8 个维护阶段的真实 SIGKILL 续跑场景。隔离的真实 Immich 3.2.2 中，15 个英文标签清理耗时 2.653–2.689 秒（旧代码同图约 86.436 秒）；三轮均保留全部 17 个其他标签，队列恢复原始暂停状态，主动刷新元数据后无英文回流。该数据来自单张合成图片，不代表线上整库吞吐。

## 1.1.0 — 2026-09-28

新增三种语言输出和一次性英文关联清理，镜像：`ghcr.io/mikusaa/immich-booru-tagger:1.1.0`（Linux AMD64 / CPU）。本次升级需要迁移旧语言配置。

- 用 `TAG_LANGUAGE_MODE=bilingual/chinese/english` 统一语言输出；中文优先仍保留没有译名的英文。旧 `ENGLISH_TAGS_ENABLED` / `TRANSLATIONS_ENABLED` 已移除，存在旧键时启动报错并提示迁移。
- 新增一次性 `cleanup-english`：默认按 `assignments.jsonl` 的历史记录筛选，也可显式选择词典范围；只解除已有关联中文的英文标签，不删图片或全局标签。
- 默认只预览，实际执行需 `--confirm-cleanup-english`。删除前写入并同步清理清单，回读后记录确认；复用独立持久队列、范围检查和失败重试，定时任务不恢复清理。
- 验证 Immich 官方解关联接口为 `DELETE /api/tags/{tagId}/assets`（`tag.asset` 权限）。
- 真实图库测试发现立即回读后仍可能由异步元数据任务恢复英文关联；增加逐项间隔、两次整张延迟复核及有限重试，并检查接口响应中的逐项成功状态。

### 升级说明

停止写入任务并备份 `state/`，删除两个旧语言变量：原 `true/true` 改为 `bilingual`，`false/true` 改为 `chinese`，`true/false` 改为 `english`。保留状态和模型挂载。SQLite 结构保持兼容，旧版未完成任务首次升级会重新扫描；已有完成标记继续避免重复推理。

将 `.env` 的 `TAGGER_IMAGE` 改为 `ghcr.io/mikusaa/immich-booru-tagger:1.1.0` 后执行 `docker compose pull` 和 `docker compose up -d`。先补中文，再预览英文清理；只有图片已经具有当前词典对应中文标签时才移除英文。完整使用和人工恢复方法见 [配置文档](docs/configuration.md#一次性英文清理)。

本地及 Linux AMD64 容器均通过 157 项测试；在 Immich 3.2.2 完成单张 15 个英文关联的实际清理、重复运行和恢复核验。异步回写可能导致等待和重试，批量操作前仍应小范围验证。

## 1.0.1 — 2026-09-27

修复中文优先模式下未翻译标签丢失的问题，镜像：`ghcr.io/mikusaa/immich-booru-tagger:1.0.1`（Linux AMD64 / CPU）。

- `ENGLISH_TAGS_ENABLED=false` 改为优先中文：有译名时只新增中文标签，没有译名时写入原英文标签。
- 覆盖文件将译名设为空时同样保留英文，避免丢失识别结果；以后补齐词典后可使用 `backfill-zh` 添加中文。
- 默认双语输出保持不变，不自动删除已有标签，已带完成标记的图片仍会跳过。
- 同步配置示例和使用文档，自动化测试覆盖预览、部分缺少译名及全部缺少译名时的英文回退。

### 升级说明

停止服务后完整备份 `state/`，保留 `state/` 和 `models/` 挂载，更新 `.env` 中的 `TAGGER_IMAGE` 后拉取并重建容器：

```env
TAGGER_IMAGE=ghcr.io/mikusaa/immich-booru-tagger:1.0.1
```

```bash
docker compose pull
docker compose up -d
```

使用中文优先模式时设置 `ENGLISH_TAGS_ENABLED=false`、`TRANSLATIONS_ENABLED=true`；现有配置无需改名。升级不需要迁移状态，已有完成标记和模型缓存继续有效。1.0.0 已处理图片中被跳过且未保留英文的条目，不会因升级自动补回，需要重新推理。详细语言配置、恢复和备份方法见 [配置文档](docs/configuration.md)。

## 1.0.0 — 2026-09-27

首次正式发布，镜像：`ghcr.io/mikusaa/immich-booru-tagger:1.0.0`（Linux AMD64 / CPU）。

- 中文运行日志：默认每 10 秒报告扫描、模型准备、图片处理的进度、当前操作和等待时间；错误与重试即时提示。
- SQLite 持久化任务队列：扫描完成后重启直接续跑，累计计数和整轮处理额度保留；扫描中断后重新核对并去重。
- 定时服务默认启动恢复兼容的未完成任务；处理前检查图片最新状态和范围，标签写入后回读确认。
- `/metrics` 提供阶段及操作状态，`--progress-status` 查询持久化任务摘要；正常健康检查不再刷访问日志。
- 支持按图库、相册限制范围，多账号处理、预览和离线中文标签补全；`ENGLISH_TAGS_ENABLED=false` 可仅新增中文标签。
- 首次写入状态时一次性导入旧失败记录，保留原 JSON 作为备份。
- `--version`、启动日志和服务根路径显示应用版本；默认 Compose 使用固定版本镜像。
- 自动化测试覆盖进程中断、原子检查点和恢复；CI 增加容器正常停止、强制终止与扫描中断验收。

### 升级说明

停止服务后完整备份 `state/`，保留 `state/` 和 `models/` 挂载，更新 `.env` 中的 `TAGGER_IMAGE` 后拉取并重建容器：

```env
TAGGER_IMAGE=ghcr.io/mikusaa/immich-booru-tagger:1.0.0
```

```bash
docker compose pull
docker compose up -d
```

旧版未持久化候选队列，首次升级仍需扫描；已有完成标记和模型缓存继续有效。新版扫描尚未完成时重启也会重新扫描。详细恢复、备份和回退方法见 [配置文档](docs/configuration.md)。
