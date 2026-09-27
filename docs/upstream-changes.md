# 相对原版的具体改动

对比基准为 fork 时的上游提交 `fb1431d38b59e2d2236d61b14f21cc5ad7bbda4b`。下面列出当前 fork 已落地的行为变化，方便维护者区分原版能力与本项目扩展。

## 用户可见功能

- 增加图库和相册范围筛选，并支持排除图库；范围同时在服务器查询和本地结果上校验。
- 增加 `--dry-run`、`--limit`、`--batch-size`、`--library-id`、`--test-connection` 及失败记录管理命令。
- 增加中文离线词典和 `backfill-zh` 模式，可以为已有英文标签补中文而不下载图片、不加载模型。
- 中文标签从旧的 `zh/属性/...` 形式改为 Immich 顶层层级标签 `属性/...`、`角色/...`、`评级/...`，因此可以直接在标签侧边栏按分类查看。
- 默认中英文标签、手工标签和 `auto:processed` 保持共存；可通过 `ENGLISH_TAGS_ENABLED=false` 优先新增中文标签，缺少译名时使用英文。补全模式不会因为已有完成标记而跳过图片。
- 支持多个 API Key 或命名账号，并为不同账号隔离失败状态。

## Immich API 与可靠性

- 从旧式单一 API 调用整理为独立 API client，支持标签缓存、批量 upsert、有限重试和请求超时。
- 自动兼容带 `filter/orderBy/cursor` 的结构化搜索与旧的 `page/nextPage` 分页；仅在明确的兼容性失败时回退，不把鉴权错误变成无范围查询。
- 增加图库、相册、图片类型、离线和回收站的本地二次校验，保护旧版 API 忽略未知过滤条件时的处理范围。
- 写入标签后读取资产确认关联成功，确认失败不会写入完成标记。
- 写入前先固定候选快照，避免标签写入改变旧分页结果导致漏处理或重复处理。

## 模型与中文词典

- 默认模型固定为 `SmilingWolf/wd-swinv2-tagger-v3`，基于 `timm/wdtagger` 的 CPU 推理依赖单独安装。
- 普通标签、角色标签和评级标签使用不同阈值策略；配置经过 Pydantic 校验。
- 新增 `translation_catalog.py` 和 `scripts/build_catalog.py`，从固定来源生成带修订号、输入哈希和覆盖计数的词典。
- 支持 `TRANSLATION_OVERRIDES` 自定义译名，控制字符和层级路径会在写入前校验；中文半角 `/` 转为全角 `／`。
- 保留 DeepDanbooru 适配，但移到可选依赖，不放入默认发布镜像，也不宣称已完成真实模型验收。

## 状态、运维与安全

- 增加 `state/` 状态目录：失败计数、追加式标签写入记录和跨进程写入锁。
- 失败记录按服务地址和 API Key 摘要分区，不保存原始 Key；达到阈值后只暂停单个资产，后续资产继续处理。
- 健康检查改为读取进程快照，提供 `/health` 和 `/metrics`，不会为了健康检查主动访问 Immich 或加载模型。
- 配置导入不再触发模型或网络副作用，启动时集中校验认证互斥、URL 和 UUID。
- Docker 改为多阶段构建、非 root 用户和 CPU PyTorch 安装，默认只暴露宿主机回环地址的健康端口。

## 构建与文档

- Compose 默认拉取 GHCR 镜像，本地构建通过显式 `docker build` 完成，不会在 `docker compose up` 时隐式构建。
- 新增 GitHub Actions：PR 运行测试和离线冒烟，`main`/版本标签通过 `GITHUB_TOKEN` 发布 `linux/amd64` 镜像。
- 测试拆分为核心、测试和可选 DeepDanbooru 依赖，新增配置、客户端、处理器和服务测试。
- README 改为面向使用者的中文上手文档；旧的实现讨论、完整配置、开发说明和 PRD 移入 `docs/`。

## 保留的原版行为

- 仍通过 Immich 官方 API 工作，不改 Immich 核心代码和原图。
- 仍以 `auto:processed` 做常规增量标记，支持连续运行和定时运行。
- `cleanup_failed_assets.py` 仍作为独立工具保留，但不再被描述为标签处理主流程的一部分。
