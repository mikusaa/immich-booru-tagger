# 更新记录

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
