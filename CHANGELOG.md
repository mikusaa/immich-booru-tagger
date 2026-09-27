# 更新记录

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
