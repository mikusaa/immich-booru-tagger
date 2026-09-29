# 配置与运行方式

本文补充 README 中的高级配置。所有环境变量都写在 `.env`，Compose 会通过 `env_file` 传入容器。

本文命令适用于 `1.3.0` 镜像及当前源码的 `python -m app.main` 入口。使用 Compose 时请先更新镜像版本，或按 [README 的源码构建步骤](../README.md#从源码构建)选择本地镜像；`1.1.x` 旧镜像仍须使用 `python -m immich_tagger.main`，其余参数相同。

## 认证与处理范围

三种认证方式只能启用一种：

```env
IMMICH_API_KEY=single-user-key
# IMMICH_API_KEYS=["key1","key2"]
# IMMICH_LIBRARIES={"Alice":"key1","Bob":"key2"}
```

`IMMICH_LIBRARIES` 只是兼容上游的名称，实际是“账号名称 → API Key”的映射，并不是图库配置。多账号运行时，每个账号使用相同的范围设置。

```env
IMMICH_INCLUDE_LIBRARY_IDS=library-uuid-1,library-uuid-2
IMMICH_INCLUDE_ALBUM_IDS=album-uuid-1
IMMICH_EXCLUDE_LIBRARY_IDS=library-uuid-3
SEARCH_API=auto
```

列表也可以写成 JSON 数组。图库 include 取并集，相册 include 取并集；两者同时设置时取交集；exclude 始终优先。没有 include 范围时会处理 API 用户可见的所有图片并打印警告。上传资产可能没有 `libraryId`，此时应通过相册范围限制。

程序只接受图片，并跳过离线、回收站和已删除资产。常规推理会跳过带有 `PROCESSED_TAG_NAME` 的资产；中文补全和英文清理会检查已经处理过的资产。

从 `1.3.0` 开始，可用 `ASSET_SORT_ORDER` 控制自动打标和中文补全的图片顺序：`desc`（默认）按图片时间 `fileCreatedAt` 从新到旧，`asc` 从旧到新。结构化搜索和旧版搜索均遵守该设置，处理上限也按此顺序选取候选。多账号、多个 include 图库仍按配置顺序逐个扫描，各自内部按所选顺序处理。

在 `.env` 设置 `ASSET_SORT_ORDER=desc` 或 `ASSET_SORT_ORDER=asc` 后，重新创建容器以加载环境变量（`docker compose up -d --force-recreate`）。切换顺序会在下一次任务执行时重新扫描候选。

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `IMMICH_BASE_URL` | 必填 | Immich 根地址，不要包含 `/api` |
| `IMMICH_API_KEY` / `IMMICH_API_KEYS` / `IMMICH_LIBRARIES` | 必须三选一 | 单账号、多 Key 或命名账号 |
| `IMMICH_INCLUDE_LIBRARY_IDS` | 空 | 包含的外部图库 UUID |
| `IMMICH_INCLUDE_ALBUM_IDS` | 空 | 包含的相册 UUID |
| `IMMICH_EXCLUDE_LIBRARY_IDS` | 空 | 排除的外部图库 UUID |
| `SEARCH_API` | `auto` | `auto`、`structured` 或 `legacy` |
| `ASSET_SORT_ORDER` | `desc` | 图片时间顺序：`desc` 从新到旧，`asc` 从旧到新 |
| `GENERAL_THRESHOLD` | `0.35` | 普通标签阈值；为空时沿用 `CONFIDENCE_THRESHOLD` |
| `CONFIDENCE_THRESHOLD` | `0.35` | 上游兼容名称 |
| `CHARACTER_THRESHOLD` | `0.90` | 角色标签阈值 |
| `BATCH_SIZE` | `250` | API 分页大小，范围 1–1000 |
| `PROCESSED_TAG_NAME` | `auto:processed` | 常规推理成功后的标记 |
| `FAILURE_TIMEOUT` | `3` | 同一资产失败达到次数后暂停重试；`0` 等同一次失败即暂停 |
| `TAGGING_MODEL` | `wd14` | `wd14` 或可选的 `deepdanbooru` |
| `MODEL_REPO` | `SmilingWolf/wd-swinv2-tagger-v3` | Hugging Face 模型仓库 |
| `MODEL_CACHE_DIR` | `/app/models` | 模型和词表缓存目录 |
| `DEEPDANBOORU_PROJECT_DIR` | 空 | DeepDanbooru 项目目录 |
| `STATE_DIR` | `/app/state` | SQLite 任务队列、失败记录、锁和写入日志 |
| `TAG_LANGUAGE_MODE` | `bilingual` | `bilingual`（中英）、`chinese`（中文优先，缺译名回退英文）、`english`（仅英文） |
| `TRANSLATION_OVERRIDES` | 空 | 自定义译名 JSON 文件 |
| `MAX_RETRIES` / `RETRY_DELAY` | `3` / `1` | HTTP 重试次数和退避秒数 |
| `REQUEST_TIMEOUT` | `30` | 单次 HTTP 请求超时秒数 |
| `TAG_CACHE_TTL` | `300` | 标签列表缓存秒数 |
| `LOG_LEVEL` | `INFO` | `DEBUG`、`INFO`、`WARNING`、`ERROR` 或 `CRITICAL` |
| `LOG_PROGRESS_INTERVAL_SECONDS` | `10` | 运行状态输出间隔，正整数秒 |
| `LOG_SLOW_OPERATION_SECONDS` | `60` | 操作等待提示阈值，正整数秒；同一操作后续最多每 60 秒警告一次 |
| `HEALTH_PORT` | `8000` | 健康检查端口 |
| `ENABLE_SCHEDULER` | `true` | scheduler 模式是否循环等待下一轮 |
| `CRON_SCHEDULE` | `0 2 * * *` | cron 表达式 |
| `TIMEZONE` | `Asia/Shanghai` | 定时任务时区 |
| `RUN_ON_STARTUP` | `false` | 是否在 scheduler 启动时立即执行一轮 |
| `RESUME_ON_STARTUP` | `true` | scheduler 启动时优先恢复配置兼容的未完成任务 |

`BATCH_SIZE` 是搜索分页大小，不代表推理并发数。WD14 的评级标签使用专门阈值，普通标签使用 `GENERAL_THRESHOLD`。

语言模式只控制后续新增标签，不删除已有标签。常规推理仍使用 `PROCESSED_TAG_NAME` 完成标记，并跳过已处理资产；中文优先模式在所有预测都缺少译名时写入英文并标记完成。显式的 `backfill-zh` 和 `cleanup-english` 独立于日常语言模式，始终读取中文词典。

### 语言配置迁移

旧的两个变量已移除。即使同时设置新变量，环境或所加载 `.env` 文件中仍有旧键（包括空值）也会报错，须删除旧键：

| 旧 `ENGLISH_TAGS_ENABLED` | 旧 `TRANSLATIONS_ENABLED` | 新 `TAG_LANGUAGE_MODE` |
| --- | --- | --- |
| `true` | `true` | `bilingual` |
| `false` | `true` | `chinese` |
| `true` | `false` | `english` |

未设置新变量时默认 `bilingual`；其他取值报错。不提供双关闭模式。`1.0.x` 镜像使用旧配置；三种语言模式与英文清理从 `1.1.0` 开始提供。升级会让旧版未完成推理/补全队列重新扫描，已有标签、完成标记和失败记录保留，SQLite 结构无需迁移。

## 运行模式

所有命令都可以放在 `docker compose run --rm immich-tagger` 后面：

| 模式 | 用途 |
| --- | --- |
| `single` | 最多处理一个 `BATCH_SIZE`，适合手动验证 |
| `continuous` | 处理本轮所有候选后退出 |
| `scheduler` | 按 cron 定时重复运行 |
| `backfill-zh` | 从已有英文标签补中文，不加载模型 |
| `cleanup-english` | 一次性解除已有中文对应项的英文关联，默认仅预览 |
| `health-only` | 只提供健康检查服务 |

附加参数：`--limit N` 限制所有账号合计处理数，`--batch-size N` 临时覆盖分页大小，`--library-id UUID` 临时指定包含图库，`--dry-run` 只预览标签计划。旧参数 `--max-cycles N` 仍可用，表示总上限 `N × BATCH_SIZE`。

`--test-connection` 只读取标签列表；`--show-failures`、`--reset-failure ASSET_ID` 和 `--reset-failures` 管理失败记录；`--progress-status` 只读查询最近一次持久化任务摘要，输出 `source=persisted`、`live=false`、`running=null`；实时状态使用 `/metrics`。没有数据库时显示尚无记录，不创建状态目录。`--reset-progress` 已弃用，不会重置计数或删除队列。

## 一次性英文清理

`--mode cleanup-english` 使用独立的队列和失败记录，定时服务只处理常规推理，不执行或恢复清理。清理直接使用图片现有标签，不要求全库打标或中文补全完成；缺中文的英文关联保留，不阻塞其他已有中文的条目。若希望为缺中文的条目补中文，可单独执行 `backfill-zh`。清理命令本身不补中文、不下载图片、不加载模型。

```bash
# 查看前 20 张可清理图片的计划
python -m app.main --mode cleanup-english --cleanup-scope recorded --dry-run --limit 20
# 兼容路径；同一条命令可在中断后重新运行续跑
python -m app.main --mode cleanup-english --cleanup-scope recorded --confirm-cleanup-english
# 维护模式；需要管理员账号的 CLEANUP_ADMIN_API_KEY（queue.read、queue.update）
python -m app.main --mode cleanup-english --cleanup-scope recorded --confirm-cleanup-english --cleanup-maintenance
# 强制退出后只恢复后台队列，不继续清理
python -m app.main --restore-cleanup-queues
# 历史记录不完整时，显式扩大到词典匹配项；先预览
python -m app.main --mode cleanup-english --cleanup-scope catalog --dry-run
```

`recorded` 默认读取 `state/assignments.jsonl`，按服务地址和 API Key 摘要匹配账号，逐张读取这些图片的当前标签和范围，避免整库搜索。文件缺失或任一行损坏会中止，不会自动切换到 `catalog`。更换 API Key 或地址后摘要改变，旧账号记录不会自动匹配；应保留原身份或审阅后显式使用 `catalog`。日志表示历史新增行为，无法判断用户后来删除又手工重加的同名标签。

`catalog` 先读取账号已有标签，筛出词典中可翻译且账号已有对应中文标签的英文标签，只检索这些英文标签关联的图片，再核对同一图片是否已有中文；不逐张读取整库无关图片。结构化搜索每组最多 100 个标签 ID，使用 `filter.tagIds.any` 取并集；旧版按单个 `tagIds` 查询并去重，避免多个 ID 的交集语义漏图。自动回退保留标签筛选，接口拒绝时会报错，不放宽成整库扫描。图库/相册和排除范围照常生效。没有可匹配标签时直接结束，不查询图片。

该模式无需 `assignments.jsonl`，但会包含同名手工英文标签。两种模式都使用当前词典和覆盖文件，支持原始英文及 `general/`、`character/`、`rating/` 前缀；不凭 ASCII 字符判定来源。中文路径必须与当前译名完整一致，旧 `zh/...` 路径不算对应中文。无译名、空译名、中文层级、旧 `zh/...`、`auto:processed` 及自定义完成标记均保留。

清理仍先收集候选快照再删除，避免解除标签后搜索分页变化导致漏图；达到 `--limit` 即结束本轮候选收集。日志中的“读取标签详情”仅表示读取现有标签，不是补标签。已完成扫描的旧清理任务可直接续跑；扫描中断后会按现有标签重新收集候选，无需删除状态或完成其他打标任务。

只有 `--confirm-cleanup-english` 才允许写入；未带此参数默认预览，`--dry-run` 与确认参数不能同时使用，清理参数不能用于其他模式。预览不写 Immich、正式队列、失败记录或清理日志。`--limit` / `--max-cycles` 限制整轮可清理图片数量，恢复不会补充额度。实际执行与常规打标签共用写入锁，应先停掉正在运行的写入任务。

维护模式需要在 `.env` 设置 `CLEANUP_ADMIN_API_KEY`，使用管理员账号创建并授予 `queue.read`、`queue.update` 权限。已在 Immich 3.2.2 验证，要求支持 `GET/PUT /api/queues/{name}`；不兼容或权限不足时不会退回无协调的快速删除。该密钥只用于队列控制，资产范围、图片读写及历史记录仍使用原账号 Key。不要用管理员 Key 替换原账号配置。

当前源码的维护模式以单张图片为一批，在同一写入锁下执行：暂停 `metadataExtraction` 和 `sidecar`，等待活动任务归零，重新核对资产并持久化完整标签基线；先排空已有 sidecar 积压，再逐个解除英文关联，每次都先运行并排空 sidecar、重新暂停后才进行下一次解除，期间保持 metadataExtraction 暂停。最后排空两队列并还原原来的暂停状态，回读检查已删除英文未回流、中文和其他保留标签仍完整，才提交本张成功。每个等待阶段需连续两次观察到队列空闲。维护期间暂停的是实例全局队列，原本暂停的队列也会暂时运行完成积压任务；请先停止其他标签写入、元数据导入和队列控制任务。

修改队列前会同步保存 `state/cleanup-queues.json`，只含服务地址摘要、原始暂停状态、失败计数和恢复阶段，不含密钥。正常停止会先恢复队列；强制退出后，重新执行维护命令会先将两队列暂停并按上述顺序恢复。也可使用独立的 `--restore-cleanup-queues`，仅恢复队列，不读取词典或资产、不继续清理。不要删除恢复文件或改用另一状态目录绕过恢复；普通写入模式发现该文件会停止。

`state/cleanup-pending.json` 独立记录待复核图片，恢复后台队列不会删除它。清理启动时先验证其服务和账号身份、读取原图片并核对保留标签，之后才选择任务队列、扫描候选或应用当前处理范围。更改 `--limit`、清理依据、相册、词典或失败重试策略不会跳过该检查；正常打标和中文补全发现此文件也会停止，要求先显式续跑清理。预览不恢复队列、不修改基线，仍会报告已知标签缺失。不存在待复核记录的空标签图片照常跳过。

`CLEANUP_QUEUE_TIMEOUT` 默认为 300 秒，限制每个等待阶段，不含单次 HTTP 请求及重试耗时。积压较大时恢复可能超过 Docker 默认的 90 秒停止宽限期；可用 `docker compose stop -t 600 immich-tagger` 留出恢复时间。队列恢复失败会保留恢复文件并停止整轮；恢复成功才移除文件。后台新增失败任务或保留标签缺失会中止整轮；英文回流记为本张失败，不计成功。这些最终校验失败不代表队列仍待恢复，应以恢复文件及错误信息为准。

删除前重新检查资产范围和中文标签，使用 `DELETE /api/tags/{tagId}/assets`、请求体 `{"ids":["asset-id"]}`，只解除一个标签在该图片上的关联；检查响应中该资产的 `success` 并逐项回读确认。API Key 需要 `tag.asset` 和读取资产的权限，不需要 `tag.delete` 或资产删除权限。不会删除全局标签，所以可能留下空标签。

Immich 会异步写入 XMP / 重建标签关联，立即回读成功的标签也可能随后重新出现。未启用维护模式时保留兼容路径：逐项解除间隔为 `RETRY_DELAY` 秒，至少 5 秒、最多 60 秒；每轮删除后对整张图片连续做两次延迟回读，每次等待至少 5 秒，并随本图片重试轮次指数退避、最多 60 秒。发现原计划内的英文重新出现时，重新核对中文和范围，最多重试 `MAX_RETRIES` 次；不会扩大到原计划之外的标签。15 个英文标签至少固定等待 85 秒，且延时不能消除后台竞态。日志区分异步等待与回读请求，并显示当前标签序号、轮次及本张累计耗时。维护模式不执行这些固定延时。任何模式的复核都无法保证后续外部元数据任务不再添加标签。

隔离的 Immich 3.2.2 实测中，XMP 并发冲突、只读目录等读写失败可能被服务端捕获后仍报告任务成功，随后元数据提取将图片标签清空，队列失败计数也可能保持为零。因此“排空 XMP 队列”只表示任务结束，不能证明文件写入成功。逐项排空减少本脚本产生的并发，无法控制原有积压、其他写入者或修复文件权限。两种清理模式都保留标签基线并检查缺失，但不能保证服务端永不丢失标签，也不会自动恢复它们。

HTTP 临时故障也按 `MAX_RETRIES` 重试；复核重试耗尽或其他单张处理失败计入独立失败记录，下次显式执行重试，达到 `FAILURE_TIMEOUT` 后暂停该图片。`--show-failures` / 重置命令包含 `cleanup-recorded` 和 `cleanup-catalog` 两类。鉴权或日志写入失败中止整轮。恢复时刷新相册范围、重读资产标签，已移除项不会再请求删除；清理范围、词典或历史写入记录变化会建立新队列。

### 清理记录与人工恢复

当前源码在第一次删除之前同步落盘 `state/cleanup-pending.json`：保存服务摘要、账号摘要、图片 ID、任务/操作 ID、时间、维护模式、完整 `tags`（ID → 路径）、`planned`（待删除英文 ID → 对应中文路径）和 `protected`（必须保留的 ID）。文件损坏、账号 Key 或服务地址不匹配时停止，不猜测归属；图片不可读取或保留标签缺失时也不会清除它。

保留标签验证通过后，完整基线及验证时间、剩余英文 ID 会先追加并同步到 `state/cleanup-baselines.jsonl`，再移除待复核文件。`outcome` 为 `verified`、`incomplete`、`interrupted` 或 `recovered`；记录说明保留标签在当时通过核验，任务是否清理成功仍以 SQLite 结果为准。强制中断可能产生同一 `operation_id` 的重复归档，可据此去重。基线归档与逐项删除审计都应随 `state/` 备份。

出现“保留标签缺失”时，先停止其他写入并保留上述文件，处理 Immich 的 XMP 路径、权限或残留临时文件问题。核对基线的服务、账号与图片，按 `protected` 对照当前标签，**只恢复确实缺失的保留关联**，不要把 `planned` 中刻意删除的英文一并加回。全局标签仍存在时可使用下述关联 API；程序按原标签 ID 复核，新建同名标签不能冒充原 ID。恢复后执行原清理命令（维护模式继续带 `--cleanup-maintenance`），检查通过才解除阻塞并按当前规则继续。没有自动恢复或强制忽略基线的命令；不要通过删除文件、更换状态目录或重置失败计数绕过。

旧版本不会保存完整保留标签基线；已经发生的丢失不能靠升级重建，`assignments.jsonl` 也只是历史新增记录，不能当作清理前的准确快照。

`state/cleanup.jsonl` 每项保存账号摘要、资产 ID、标签 ID、完整英文路径、对应中文路径、任务 ID、操作 ID 和 UTC 时间。每个删除请求前先追加并同步落盘 `status=prepared`，逐项回读成功后再追加相同 `operation_id` 的 `status=confirmed`。`confirmed` 表示该次回读时已移除，整张图片是否通过后续延迟复核应查看任务结果；只有 `prepared` 的操作也可能已执行，需以 Immich 实际标签为准。没有自动回滚命令。

人工恢复时先停止清理，按账号核对清单和当前资产，筛选确需恢复且当前缺失的英文关联。全局标签仍存在时，通过 `PUT /api/tags/assets`，请求体 `{"assetIds":["asset-id"],"tagIds":["tag-id"]}` 恢复关联，并回读确认；如果标签对象已被另行删除，可按日志完整路径重新创建后关联。恢复关联不会移除中文。仅恢复本地状态目录不能撤销 Immich 上的删除。

## 中文词典与自定义译名

词典覆盖默认 WD14 词表中的大部分普通和角色标签。运行时不访问翻译服务，缺失项保留英文并在日志中汇总，后续更新词典后可通过 `backfill-zh` 补中文。优先中文模式下，已有译名的条目不额外保存英文，后续更换这些条目的译名需要重新推理或手动整理。词典来源、固定版本和许可见 [data/NOTICE.md](../data/NOTICE.md)。

覆盖文件示例：

```json
{
  "blue_hair": {"zh": "蓝发", "kind": "general"},
  "hatsune_miku": {"zh": "初音未来", "kind": "character"},
  "explicit": {"zh": "成人", "kind": "rating"}
}
```

`kind` 只能是 `general`、`character` 或 `rating`。把 `zh` 设为空字符串可关闭该项的中文译名，推理时仍写入英文标签；中文文本中的半角 `/` 会转为全角 `／`，避免意外创建额外层级。覆盖文件挂载在 `state/` 中，更新镜像不会覆盖它。改译名后，补全模式会为仍有对应英文标签的资产增加新标签，旧标签需要在 Immich 中自行整理。

## 失败记录、写入与健康检查

只有标签关联回读确认成功后，才会添加处理标记。部分写入失败时，重试会保留已成功的标签并补齐缺项；模型初始化失败会终止整轮，不把所有图片记为失败。达到 `FAILURE_TIMEOUT` 后，资产只会被跳过，修复原因后使用重置命令恢复。

`state/progress.sqlite3` 保存候选队列、逐张结果和失败计数，`state/assignments.jsonl` 记录新增标签（账号只保存服务地址和 Key 的摘要），`state/writer.lock` 防止同一目录多进程同时写入。SQLite 使用 WAL，目录中的 `-wal`、`-shm` 文件属于数据库运行状态，不要在运行中删除。清理另有 `state/cleanup.jsonl` 记录；当前没有自动回滚命令。

健康检查默认绑定宿主机 `127.0.0.1:8000`，提供 `/health` 和 `/metrics`。它们读取线程安全的进程快照，不主动请求 Immich 或加载模型；最近一轮失败时 `/health` 返回 503。仅等待较久不判为故障。正常健康检查访问不再打印访问日志，健康状态变化会单独提示。

## 中文日志与实时进度

```bash
docker compose logs -f immich-tagger
curl -s http://127.0.0.1:8000/metrics
docker compose run --rm immich-tagger python -m app.main --progress-status
```

日志时间使用 `TIMEZONE` 并带 UTC 偏移。阶段切换、重试和错误立即记录；运行时由独立报告线程定时显示状态，即使当前请求或模型调用尚未返回，也能看到操作名称和等待时间。默认 INFO 输出汇总，DEBUG 输出单张结果和标签明细；dry-run 始终在 INFO 显示计划添加的标签；清理预览显示待移除英文及对应中文。

```text
2026-09-27 17:21:10+08:00 INFO [扫描] 已读取 12 页｜已读取记录 3000 条｜已选候选 2400 张｜读取标签详情 1850 次
2026-09-27 17:22:18+08:00 INFO [模型] 正在准备模型 SmilingWolf/wd-swinv2-tagger-v3｜首次运行可能需要下载权重
2026-09-27 17:24:40+08:00 INFO [处理] 已完成 120/4260（2.8%）｜成功 118｜失败 2｜跳过 0｜本次完成 120 张
```

`/metrics` 保留原有字段，增加 `progress`：

| 字段 | 含义 |
| --- | --- |
| `run_id` / `task_status` / `resumed` | 任务 ID、任务状态、是否续跑 |
| `phase` / `account` | 当前阶段和账号；阶段为 `idle`、`recovering`、`scanning`、`preparing_model`、`processing`、`stopping`、`paused`、`completed`、`error`、`waiting` |
| `scan_pages` / `scan_records` | 接受的搜索分页数及其返回记录数；不等同于全库唯一图片总数 |
| `detail_requests` / `candidates` / `scan_skips` | 读取标签详情次数、已选候选、本地跳过原因计数 |
| `total` / `completed` / `remaining` | 固定队列总量、已结束尝试数、剩余量；扫描结束前总量和剩余量为 `null` |
| `session_completed` | 本次执行完成数；恢复时从零开始 |
| `current_asset_id` / `operation` | 当前图片及操作代码，如 `search`、`read_details`、`load_model`、`download`、`inference`、`assign_tags`、`remove_tags`、`readback`、`checkpoint` |
| `phase_elapsed_seconds` / `operation_elapsed_seconds` / `session_elapsed_seconds` | 当前阶段、操作及本次执行耗时，停机时间不计入 |
| `assets_per_second` | 本次处理阶段完成数除以处理阶段耗时，排除扫描、模型准备和停机时间 |
| `last_progress_at` / `next_run_at` | 最近实际推进时间、下一轮计划时间；定时状态日志不会刷新推进时间 |

`completed` 包含队列中的成功、失败、预览和处理时跳过；扫描阶段被过滤的记录不计入百分比分母。`last_run` 继续保留原有计数语义，`skipped` 还包含扫描时因失败次数达到上限而排除的图片。业务提示中文，接口阶段和操作代码保持英文便于工具使用。模型准备显示步骤与等待时间，不伪造下载百分比。

## 重启续跑与配置变更

从 `1.2.0` 或更早版本升级到 `1.3.0`，或切换 `ASSET_SORT_ORDER` 后，未完成队列会在下一次任务执行时被替代并按所选顺序重新扫描。已完成的自动打标图片凭完成标记跳过，已有标签和失败记录保留，无需删除 `state/`。替代后的任务重新计算本轮处理上限；英文清理也会重新收集候选，仍先恢复队列并核验待复核标签基线。

扫描已完成的任务在重启后使用持久队列继续处理，无需再次全库搜索。恢复会刷新相册成员，并在图片推理前和写入前读取资产最新状态；已有完成标记、已删除、离线或已移出范围的图片会跳过。单张写入完成但本地尚未提交便断电时，通过实际标签和完成标记核对；未完成图片可能重新推理，但只补缺少的标签。

扫描未完成的任务会重新扫描并按账号和图片 ID 去重：只保留本次重新确认的候选，不使用旧页码或旧游标直接续扫。整个队列固定后才开始写入。达到 `--limit` / `single` / `--max-cycles` 的合并上限也算本轮扫描完成；额度针对整轮，多次重启不会扩大额度。恢复中的新图片由下一轮扫描发现。

默认 `RESUME_ON_STARTUP=true`：兼容的未完成任务启动后立即恢复，恢复后不再额外执行 `RUN_ON_STARTUP` 新任务。设置为 false 只关闭立即恢复，下一次定时触发仍会优先续跑。`ENABLE_SCHEDULER=false` 只执行一轮，优先恢复；`health-only` 不恢复任务、不迁移状态、不调用 Immich。预览使用临时队列，不创建或更新正式恢复状态。

账号身份或顺序、图库/相册范围、图片排序、处理类型、模型、阈值、语言输出、词典内容、完成标记、失败策略、有效处理上限发生变化时，旧任务标记为已替代，新任务重新扫描。已有 Immich 标签仍然保留。日志设置、时区、cron、请求超时不影响续跑；分页大小仅在影响有效总上限时使任务不兼容。自动打标签、中文补全和英文清理任务独立。

成功、失败、跳过与失败次数在同一 SQLite 事务中提交；单张失败本轮不无限重试，下一轮按失败策略重试。模型、词典、鉴权、数据库等任务级错误保留队列，不把所有图片记为失败。SIGTERM 停止领取新图片并尽量完成当前图片；被强制终止后从最后已提交结果继续。已结束任务仅保留最近 100 条汇总，未完成队列不自动清理。

## 从旧版升级

1. 停止服务，完整备份状态目录，保留原镜像版本或 digest：

   ```bash
   docker compose stop immich-tagger
   cp -a state state.before-progress-upgrade
   ```

2. 按上面的语言配置迁移表删除旧键并设置 `TAG_LANGUAGE_MODE`。更新到包含这些变更的镜像并启动，继续挂载原有 `state/` 和 `models/`。首次需要写状态时，旧 `failures-*.json` 在写入锁内一次性导入 SQLite，原文件保留作为备份。失败查询只读；导入后重置失败记录不会被旧 JSON 重新覆盖。
3. **旧版没有保存候选队列，第一次升级仍需扫描。** Immich 完成标记和模型缓存继续有效。从新版建立并固定的队列开始，后续重启即可续跑。

SQLite 已成为失败状态和任务队列的权威来源，不要继续修改备份 JSON。迁移失败、数据库损坏或版本不兼容会明确报错，不自动清空恢复数据。状态卷应使用支持本地文件锁的存储，备份时先停止写入服务并复制整个目录。

回退旧镜像时先停止新版，把当前 `state/` 整体另存，再从 `state.before-progress-upgrade` 恢复旧状态目录。旧版无法读取新队列；升级后的新标签仍保存在 Immich，不会因回退状态目录被撤销。

## Immich 兼容性

客户端优先使用带 `filter/orderBy/cursor` 的结构化搜索，自动模式在服务器返回 HTTP 400 或旧分页格式时回退到 `page/nextPage`。鉴权错误不会触发无范围回退。标签写入依赖 Immich 的 `PUT /api/tags` 和 `PUT /api/tags/assets`；英文清理使用 `DELETE /api/tags/{tagId}/assets`。Immich 没有批量 `DELETE /api/tags/assets` 接口。
