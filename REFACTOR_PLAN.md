# JMComic 插件 main.py 拆分重构计划

> 状态：规划稿（本文件只描述方案，不包含代码改动）  
> 适用版本：v0.7.8（BUG-013 A 版 / BUG-014 已完成）  
> 目标：把当前 5103 行、156 个方法的 `main.py` 拆成可测试、可维护的分层结构，同时保持现有一切对外行为不变。  
> 参考插件：`D:\1.Project\Gitproject\Github\astrbot_plugin_pixiv_reborn`。只借鉴其“入口薄、handlers 分层、core/utils 独立”的组织方式，不照搬它的数据库、aiohttp 客户端和 Fanbox 目录结构。

---

## 1. 结论先行

推荐采用 **“main.py 作为组合根 + 命令薄包装；handlers 负责命令编排；services 负责业务能力；core 负责配置/客户端/状态/底层适配；ui 只放纯文案”** 的结构。

关键约束：

1. `main.py` 仍保留 `@register`、`JmComicPlugin(Star)`、所有 `@filter.command` / `@filter.regex` 包装器和 `initialize/terminate`。
2. 命令函数名、别名、装饰器参数、版本号 `0.7.8` 不改。
3. 业务逻辑从 `main.py` 移到 `handlers/` 与 `services/`，但第一阶段只“搬家”，不顺手改行为。
4. 不引入数据库、不引入新第三方依赖、不改配置 schema、不改缓存目录、不改输出文件命名、不改 QQ 消息预算策略。
5. 每批迁移必须独立可运行、可回滚；仓库与 `C:\Users\Administrator\.astrbot\data\plugins\astrbot_plugin_jmcomic` 在每批完成后同步并校验哈希。

---

## 2. 现状评估

### 2.1 体量与热点

| 指标 | 当前值 |
|---|---:|
| `main.py` 行数 | 5103 |
| 文件大小 | 约 223 KB |
| `JmComicPlugin` 方法数 | 156 |
| 命令数 | 23 个 `@filter.command` + 1 个 `@filter.regex` |
| `self.` 引用 | 约 777 处 |

最大的方法：

| 方法 | 行数 | 当前职责 |
|---|---:|---|
| `_run_download` | 389 | 目标循环、选章、下载、限额、打包、发送、历史、汇总 |
| `jm_view` | 195 | 详情、章节列表、封面 PDF |
| `jm_favorite` | 185 | 收藏夹增删查、导出确认 |
| `jm_comments` | 175 | 评论拉取、回复树、分页、裁剪 |
| `jm_history` | 146 | 个人/全员历史、分页 |
| `jm_chapter_list` | 134 | 话列表、分页、违规检测 |
| `_save_target_metadata` | 130 | 漫画/章节元数据落盘 |
| `jm_subscribe` | 126 | 订阅增删查、基线更新 |
| `_build_pdf` | 123 | 封面、PDF 元数据、img2pdf/reportlab/Pillow 回退 |
| `_render_search_page` | 118 | 搜索分页、简繁双搜、去重、下一页 |

### 2.2 当前耦合点

- `__init__` 同时完成配置解析、路径创建、缓存清理、jmcomic option 构建、客户端缓存、状态字典初始化。
- 命令处理、权限、敏感词、QQ 消息预算、缓存定位、PDF/ZIP 生成、下载重试全部挂在同一个 `Star` 类上。
- `_run_download` 依赖 30 多个 `self._xxx` 方法，任何一处移动都可能牵动主流程。
- `ProgressDownloader` 的类变量 `_progress` / `_album_dirs` 同时被下载器和 `/jmnow`、缓存定位读取。
- `jm_view`、`jmlist`、`jmc` 各自重复“章节 ID 误当漫画 ID”的检测与提示。
- 搜索与排行榜各自重复“QQ 页 → 网站页”的换算。
- 签到与用户面板重复解析签到日历。

### 2.3 重构目标

- `main.py` 控制在约 350–450 行，只保留注册、组装、命令包装、生命周期。
- 单个 handler 建议不超过 400 行；单个 service 建议不超过 500 行。
- 业务方法可以通过普通单元测试直接构造依赖，不必先构造整个插件。
- 依赖方向单向、无环；共享状态只有一个来源。
- 不改变任何命令文案、文件命名、缓存布局、配置语义和 QQ 行为。

---

## 3. 设计原则

### 3.1 分层

| 层 | 职责 | 禁止 |
|---|---|---|
| `main.py` | `Star` 注册、依赖组装、命令包装、`initialize/terminate` | 业务逻辑、文件打包、下载循环 |
| `handlers/` | 解析事件参数、权限检查、调用 service、组织回复 | 直接实现缓存/打包/下载细节 |
| `services/` | 单一业务能力：下载、打包、搜索、缓存、限额、订阅等 | 导入 `handlers` 或 `main` |
| `core/` | 配置、客户端、状态、底层下载器、纯模型/解析 | 导入 `services` / `handlers` |
| `ui/` | 纯文案和帮助文本 | 网络、文件、状态、事件 |

### 3.2 依赖方向

```text
main.py
  ├── handlers/*
  │     ├── services/*
  │     │     ├── core/*
  │     │     └── ui/*
  │     └── ui/*
  └── core/*
```

规则：

- `core` 不依赖 `services`、`handlers`、`main`。
- `services` 不依赖 `handlers`、`main`。
- `handlers` 不依赖 `main`。
- `ui` 不依赖 `services`、`handlers`、`main`。
- 需要类型提示时用 `TYPE_CHECKING`，避免运行时循环导入。
- 不允许 `from main import ...`。
- 不允许把 `JmComicPlugin` 实例作为“万能上下文”传进所有 service；按需注入明确依赖。

### 3.3 状态原则

- 配置只解析一次，放在 `PluginSettings`，初始化后只读。
- 运行期共享状态放在唯一的 `PluginState`。
- `JmClientManager` 只持有一个已登录客户端缓存；不要在多处重新 `option.new_jm_client()`。
- `ProgressDownloader._progress/_album_dirs` 是唯一允许的类级可变状态，因为要适配 jmcomic 的下载器钩子；所有读取方必须导入同一个类对象。
- 每次命令的局部状态（例如 `sent_ranges`、`total_files`）留在 `DownloadRunner.run()` 内，不放进 `PluginState`。

### 3.4 行为冻结

- 迁移前先做“特征测试/快照测试”：关键命令的文本、文件名、分组、消息条数、缓存路径。
- 每批只移动一层，不顺手改算法。
- 发现旧代码有 bug 时，先记录，不在重构批次里修；避免行为和修复混在一起。
- `_run_download` 第一轮原样搬移，禁止同时拆成多个小函数；等测试稳定后再做第二轮内部拆分。

---

## 4. 目标目录

```text
astrbot_plugin_jmcomic/
├── main.py                     # 入口：注册、组装、命令包装、生命周期
├── core/
│   ├── __init__.py
│   ├── config.py               # PluginSettings：配置/路径/jmcomic option
│   ├── state.py                # PluginState：运行期共享字典/任务
│   ├── client.py               # JmClientManager：登录缓存、异常翻译
│   ├── downloader.py           # ProgressDownloader + retry 配置
│   ├── models.py               # PageUnit 等轻量模型
│   └── parsing.py              # ID/正整数等纯解析
├── services/
│   ├── __init__.py
│   ├── access.py               # 群白名单、号主权限
│   ├── text.py                 # 简繁转换、敏感词、文件名清洗
│   ├── messaging.py            # plain/chain、QQ 预算、违规检测
│   ├── quota.py                # 文件发送限额
│   ├── history.py              # 下载历史 JSON
│   ├── cache.py                # 漫画/章节目录、metadata、迁移
│   ├── catalog.py              # 章节标题/话序号/页数/信息页
│   ├── selection.py            # 目标识别、-c、页段、分册规划
│   ├── packaging.py            # PDF/ZIP/封面/命名
│   ├── progress.py             # 当前下载任务登记（/jmnow 用）
│   ├── pagination.py           # 翻页会话、PageEventProxy
│   ├── download.py             # 整本/单章下载、批次去重
│   ├── download_runner.py      # 统一下载编排、限额、发送、汇总
│   ├── search.py               # 搜索参数、搜索结果分页
│   ├── checkin.py              # 签到状态解析
│   ├── favorites.py            # 收藏夹、CSV 导出
│   └── subscriptions.py        # 订阅持久化与后台检查
├── handlers/
│   ├── __init__.py
│   ├── album.py                # /jmv /jmlist
│   ├── download.py             # /jm /jmzip /jmstop /jmnow
│   ├── search.py               # /jms /jma /jmt /jmr /jmw /jmrand
│   ├── library.py              # /jmrank /jmhis /jmc
│   ├── account.py              # /jmcheckin /jmme /jmwhoami
│   ├── navigation.py           # /jmnext、“下一页/下页”
│   ├── favorites.py            # /jmfav
│   ├── subscribe.py            # /jmsub
│   └── system.py               # /jmdomain /jmupdate
└── ui/
    ├── __init__.py
    └── help.py                 # /jm help 文案
```

说明：

- 目录名使用 `core/services/handlers/ui`，与 Pixiv 插件的思路一致，但文件按 JMComic 的实际职责划分，不复制 Fanbox 的多层结构。
- `ui/` 只放纯文案，避免把 80 行 help 文案塞在 `jm_download` 里。
- 如果后续 `library.py` 超过 500 行，可再拆成 `ranking.py`、`history.py`、`comments.py`；本次不预拆，避免一次性增加文件数量。

---

## 5. 模块职责清单

> 行号是当前 `main.py` 的定位信息，迁移后会变化。  
> “逻辑迁移”指方法体搬到目标模块；`main.py` 中的同名命令函数只保留装饰器和 `async for ...: yield` 包装。

### 5.1 `main.py`

| 项 | 内容 |
|---|---|
| 职责 | `@register`；`JmComicPlugin(Star)`；构造所有依赖；保留 23 个命令包装器和 1 个正则包装器；`initialize/terminate` |
| 保留源码 | `161-163` 注册；`2899` 起各 `@filter.command`；`3445` 起 `@filter.regex`；`5085-5103` 生命周期 |
| 不保留 | 配置解析细节、缓存/打包/下载/搜索/签到/收藏/订阅实现 |
| 依赖 | `core/*`、`handlers/*`、`ui/help.py`、`astrbot.api` |
| 目标行数 | 350–450 行 |

包装器映射见附录 B。包装器必须保持 `async def` + `async for` 形式，不能改成普通函数，否则 AstrBot 的异步生成器处理会变化。

### 5.2 `core/config.py`

| 项 | 内容 |
|---|---|
| 职责 | 从 `AstrBotConfig/dict` 解析 `PluginSettings`；路径；数值夹取；代理；域名；排序映射；构建 jmcomic option；创建数据目录；启动时清理过期缓存 |
| 迁移源码 | `163-388` 中配置/路径/option 部分；`361-369` `order_map`；`489-493` `_zip_page_hint`；`38-41` retry 配置可放 `core/downloader.py` |
| 关键字段 | `plugin_dir`、`data_dir`、`download_base_dir`、`zip_base_dir`、`send_state_file`、`history_file`、`checkin_file`、`subscription_file`、`option`、`order_map`、所有配置值 |
| 依赖 | 标准库、`yaml`、`jmcomic`、`astrbot.api.logger` |
| 注意 | **数据目录必须由 `main.py` 传入插件根目录**，不能让 `core/config.py` 用 `Path(__file__).parent` 推算；否则会从 `plugins/astrbot_plugin_jmcomic/core/` 推错两级。 |

建议接口：

```python
@dataclass(frozen=True)
class PluginSettings:
    plugin_dir: Path
    data_dir: Path
    download_base_dir: Path
    zip_base_dir: Path
    ...
    option: object
    order_map: dict[str, str]

    @classmethod
    def from_config(cls, config, plugin_dir: Path) -> "PluginSettings": ...
    def prepare_directories(self) -> None: ...
```

`prepare_directories()` 负责 `mkdir` 和启动时缓存清理，放在 `__init__` 调用，避免 import 时产生副作用。

### 5.3 `core/state.py`

| 项 | 内容 |
|---|---|
| 职责 | 运行期共享状态容器，不做业务 |
| 迁移源码 | `236-247` 的状态字段；`1299` 附近的 `_comment_page_cache` 可纳入 |
| 字段 | `id_type_cache`、`download_tasks`、`active_downloads`、`page_sessions`、`pending_exports`、`comment_page_cache`、`subscribe_task`、`subscription_lock` |
| 依赖 | 标准库、`asyncio` |
| 注意 | 不要把 `sent_ranges`、`total_files` 等命令局部变量放进来。 |

### 5.4 `core/client.py`

| 项 | 内容 |
|---|---|
| 职责 | 创建/缓存已登录客户端；登录失败提示；jmcomic 异常翻译；读取配置中的登录用户名 |
| 迁移源码 | `357-359` 客户端缓存字段；`523-540` `_handle_jm_exception`；`541-569` `_new_logged_client`；`570-573` `_invalidate_client`；`4568-4577` `_configured_username` |
| 接口 | `get_client()`、`invalidate()`、`handle_exception(e, action)`、`configured_username()` |
| 依赖 | `core/config.py`、`jmcomic` 异常、`logger` |
| 注意 | 保持单实例缓存；当前 `_client_lock` 未实际使用，迁移时不要引入新的并发语义。 |

### 5.5 `core/downloader.py`

| 项 | 内容 |
|---|---|
| 职责 | `ProgressDownloader`；异步下载钩子；按 album 记录进度与实际目录；`ADVANCED_RETRY_CONFIG` |
| 迁移源码 | `38-127` |
| 依赖 | `jmcomic`、`pathlib`、`logger` |
| 注意 | `_progress` / `_album_dirs` 必须只有这一份；`/jmnow`、`cache.find_album_dir` 都从本模块导入同一个类。不要复制类。 |

### 5.6 `core/models.py`

| 项 | 内容 |
|---|---|
| 职责 | `PageUnit`；后续可放 `DownloadTarget`、`DownloadPlan` 等纯数据模型 |
| 迁移源码 | `143-159` |
| 依赖 | `typing.Any` |
| 注意 | 暂时保留 `main.PageUnit` re-export，避免旧测试立即断裂；测试更新后可删除。 |

### 5.7 `core/parsing.py`

| 项 | 内容 |
|---|---|
| 职责 | 纯解析工具：正整数页码、从消息提取数字 ID |
| 迁移源码 | `2765-2773` `_parse_positive_page`；`2892-2899` `_extract_id` |
| 依赖 | `re` |
| 注意 | 不把 `-c` 章节表达式放这里；那属于 `services/selection.py` 的领域逻辑。 |

### 5.8 `services/access.py`

| 项 | 内容 |
|---|---|
| 职责 | 群聊总开关、群白名单、私聊例外、号主判断、对应提示文案 |
| 迁移源码 | `389-425` |
| 接口 | `check_permission(event)`、`permission_denied_text(event)`、`is_owner(event)`、`owner_required_text()` |
| 依赖 | `core/config.py` |
| 注意 | 私聊不受群开关影响；`owner_ids` 为空时账号命令统一提示配置号主。 |

### 5.9 `services/text.py`

| 项 | 内容 |
|---|---|
| 职责 | 繁转简、简转繁、敏感词替换、统一 `safe_text`、安全文件名 |
| 迁移源码 | `574-593`；`1130-1173`；`1306-1323` |
| 接口 | `to_simplified`、`to_traditional`、`sanitize_tags`、`safe_text`、`safe_filename` |
| 依赖 | `core/config.py`、`zhconv`、`pypinyin` |
| 注意 | 账号类命令必须绕过转换，这一判断在 `messaging.py`，不要散落到每个 handler。 |

### 5.10 `services/messaging.py`

| 项 | 内容 |
|---|---|
| 职责 | 统一输出；命令名识别；账号命令白名单；QQ 官方消息预算；`backend.log` 违规检测；纯文本/Chain 回复 |
| 迁移源码 | `1178-1194`；`1263-1305`；`1324-1390` |
| 接口 | `plain(event, text)`、`chain(event, components)`、`command_name(event)`、`is_qq_official(event)`、`message_budget(event)`、`log_pos()`、`log_has_violation(offset)`、`is_violation_error(err)` |
| 依赖 | `core/config.py`、`services/text.py`、`Comp`、`logger` |
| 注意 | 这是唯一直接调用 `event.plain_result` / `event.chain_result` 的层。`_ACCOUNT_COMMANDS = {"jmcheckin","jmme","jmfav","jmwhoami"}` 必须保持原样。 |

### 5.11 `services/quota.py`

| 项 | 内容 |
|---|---|
| 职责 | 读取/写入 `send_limits.json`；每日/每小时文件额度；限额提示；记录发送 |
| 迁移源码 | `426-522` |
| 接口 | `file_quota()`、`quota_reached()`、`record_file_send()`、`block_text()`、`status_text()`、`limit_text()` |
| 依赖 | `core/config.py`、`logger` |
| 注意 | 限额为 0 表示不限；保留当前时间窗口算法。 |

### 5.12 `services/history.py`

| 项 | 内容 |
|---|---|
| 职责 | `history.json` 读写；按用户取/加/裁剪历史 |
| 迁移源码 | `1100-1129` |
| 接口 | `load()`、`save(data)`、`get_user(user_id)`、`add_user(user_id, item)` |
| 依赖 | `core/config.py` |
| 注意 | `history_max=0` 表示保留全部；新增记录仍插到最前面。 |

### 5.13 `services/cache.py`

| 项 | 内容 |
|---|---|
| 职责 | 漫画目录定位；目录是否有图片；`metadata.json` 读写；章节目录定位与旧目录迁移；章节图片列表；缓存完整性 |
| 迁移源码 | `594-670`；`1026-1038`；`1648-1654`；`1797-1871` |
| 接口 | `album_dir_has_images`、`find_album_dir`、`read_metadata`、`write_metadata`、`chapter_dir`、`chapter_images`、`chapter_image_paths`、`album_cache_complete` |
| 依赖 | `core/config.py`、`core/downloader.py`、`logger` |
| 注意 | `find_album_dir` 优先使用 `ProgressDownloader._album_dirs`，这是现有缓存命中的关键；迁移时不要改变候选顺序。 |

### 5.14 `services/catalog.py`

| 项 | 内容 |
|---|---|
| 职责 | 标题/话序号/页数；`jmv`/`jmlist` 章节行；保存漫画/章节 metadata；ZIP 漫画信息；PDF 封面文案/属性；章节 ID 畸形 album 检测；父漫画解析 |
| 迁移源码 | `671-1025`；`1039-1099` |
| 接口 | `album_display_title`、`chapter_display_title`、`photo_page_count`、`photo_range_pages`、`format_chapter_line`、`save_target_metadata`、`photo_position`、`album_info_text`、`album_cover_text`、`pdf_metadata`、`looks_like_chapter_album`、`resolve_chapter_parent` |
| 依赖 | `services/cache.py`、`services/text.py`、`logger`；父漫画解析通过参数接收 client，不直接持有客户端 |
| 注意 | 章节行格式固定为 `第n话 [JM章节id] 原标题（N页）`，页数缺失 `页数未知`；BUG-014 只要求封面转 PDF，不要改标题/搜索/排行榜文本。 |

### 5.15 `services/selection.py`

| 项 | 内容 |
|---|---|
| 职责 | 下载参数解析；目标 ID 识别（漫画/章节）；`-c` 章节与页段解析；章节选择；页段展开/装册；PDF/ZIP 分组；续传表达式；话序号格式化 |
| 迁移源码 | `1402-1780` |
| 接口 | `parse_download_args`、`normalize_target`、`resolve_target`、`parse_page_range`、`merge_page_ranges`、`select_chapters`、`expand_page_units`、`pack_units`、`normalize_selected_ranges`、`plan_pdf_groups`、`plan_zip_groups`、`resume_expr`、`format_index_ranges` |
| 依赖 | `core/config.py`、`core/client.py`、`core/models.py`、`core/parsing.py`、`services/cache.py`、`logger` |
| 注意 | `resolve_target` 有网络请求，保持 `asyncio.to_thread`；`_id_type_cache` 使用 `PluginState`；不要改变“首章 ID 与漫画 ID 相同仍按漫画处理”的规则。 |

### 5.16 `services/packaging.py`

| 项 | 内容 |
|---|---|
| 职责 | PDF 构建与回退链；ZIP 构建；封面图片生成；字体查找；输出新鲜度；文件 stem；ZIP 内目录名 |
| 迁移源码 | `1391-1401`（`_zip_folder`，当前无调用，可标记为待删除）；`1872-2191` |
| 接口 | `build_pdf`、`build_zip`、`make_cover_image`、`output_fresh`、`file_stem_for_entries`、`zip_arc_dir`、`chapter_file_stem`、`group_file_stem`、`format_position_token` |
| 依赖 | `core/config.py`、`services/cache.py`、`services/catalog.py`、`services/text.py`、`PageUnit`、`logger` |
| 注意 | 保持 `img2pdf → reportlab → Pillow` 回退顺序；封面临时文件必须删除；PDF 名称/页段后缀/ZIP 单话目录规则不能变；同步函数由调用方 `asyncio.to_thread` 执行。 |

### 5.17 `services/progress.py`

| 项 | 内容 |
|---|---|
| 职责 | 当前用户下载任务登记；供 `/jmnow` 和 `/jmstop` 查询/清理 |
| 迁移源码 | `1195-1232` |
| 接口 | `set_active(user_id, task, target)`、`update(user_id, **kwargs)`、`clear(user_id, task=None)`、`get_active(user_id)` |
| 依赖 | `core/state.py`、`time` |
| 注意 | 必须与 `DownloadRunner` 的 `finally` 清理一致；`/jmnow` 还要读取 `core/downloader.ProgressDownloader._progress`。 |

### 5.18 `services/pagination.py`

| 项 | 内容 |
|---|---|
| 职责 | 翻页会话；按 `(unified_msg_origin, sender_id)` 隔离；超时；`PageEventProxy`；命令名到 handler 名的映射 |
| 迁移源码 | `129-141`；`1233-1263`；`1281-1293` |
| 接口 | `remember(event, next_command, page, total_pages)`、`get(event)`、`clear(event)`、`page_session_key(event)`、`page_handler_name(command)` |
| 依赖 | `core/config.py`、`PluginState` |
| 注意 | `page_turn_timeout=0` 关闭；最后一页不再保存；`PageEventProxy` 只替换 `message_str`，其他事件能力继续委托原事件。 |

### 5.19 `services/search.py`

| 项 | 内容 |
|---|---|
| 职责 | 搜索参数解析；简繁双搜；QQ 页/网站页换算；结果切片、排序、下一页命令；搜索回复 |
| 迁移源码 | `2718-2764`；`2774-2891` |
| 接口 | `parse_search_args`、`render_search_page` |
| 依赖 | `core/config.py`、`core/client.py`、`core/parsing.py`、`services/text.py`、`services/messaging.py`、`services/pagination.py` |
| 注意 | 保留“末尾页码/排序参数”解析规则；保留超大页码 clamp 检测；敏感词关键词在可见命令中替换为 `<关键词>`。 |

### 5.20 `services/download.py`

| 项 | 内容 |
|---|---|
| 职责 | 整本下载重试；单章下载；缓存命中；失败时允许部分完成；批次已发送页段去重 |
| 迁移源码 | `2192-2276`；`2290-2328` |
| 接口 | `download_album_progress(event, album_id, holder, chapters)`、`ensure_photo_downloaded(photo, album_dir)`、`add_sent_ranges`、`range_covered`、`filter_duplicate_selections` |
| 依赖 | `core/config.py`、`core/client.py`、`core/downloader.py`、`services/cache.py`、`services/messaging.py`、`jmcomic` 异常、`logger` |
| 注意 | `_download_album_progress` 是异步生成器，会 yield 用户消息；这是 service 层接受 event 的例外，需在注释中注明。整本 3 次外层重试不能变。 |

### 5.21 `services/download_runner.py`

| 项 | 内容 |
|---|---|
| 职责 | 统一下载编排：多目标、识别、选章、下载、限额、打包、发送、历史、汇总、QQ 消息预算、调侃文案 |
| 迁移源码 | `2329-2717`；`2277-2290` `_tease_for_count` |
| 接口 | `async def run(event, target_str, output_format, chapter_expr)` |
| 依赖 | `core/config.py`、`core/state.py`、`services/access.py`（可选）、`services/cache.py`、`services/catalog.py`、`services/selection.py`、`services/packaging.py`、`services/download.py`、`services/quota.py`、`services/history.py`、`services/messaging.py`、`services/progress.py`、`services/text.py` |
| 注意 | 第一轮原样搬移，**不要同时拆分**。必须完整保留 `sent_messages` 计数、`message_budget` 判断、文件发送后删除、`send_interval`、限额中断续传表达式、历史记录、`tease_enabled`。第二轮才考虑拆成 `run_target()` / `package_target()`。 |

### 5.22 `services/checkin.py`

| 项 | 内容 |
|---|---|
| 职责 | 签到日期解析；本机签到状态；连续天数估算；签到接口 payload 解析 |
| 迁移源码 | `3700-3851` |
| 接口 | `parse_record_day`、`load_state`、`save_state`、`local_streak`、`daily_payload`、`parse_daily_info` |
| 依赖 | `core/config.py`、`logger`、`datetime` |
| 注意 | 保留 API `model_data/src_dict` 与 HTML `json()/dateArray` 两种兼容；签到命令本身仍在 handler。 |

### 5.23 `services/favorites.py`

| 项 | 内容 |
|---|---|
| 职责 | 清客户端缓存；收藏状态；收藏夹渲染；CSV 收集/写入；导出 |
| 迁移源码 | `4532-4567`；`4578-4649` |
| 接口 | `clear_client_cache`、`favorite_state`、`render_favorites`、`collect_rows`、`write_csv`、`export` |
| 依赖 | `core/config.py`、`core/client.py`、`services/text.py`、`services/quota.py`、`services/messaging.py`、`logger` |
| 注意 | CSV `utf-8-sig`、表头 `id,author,name`、去重、导出后删除临时文件；不加密、不设密码。 |

### 5.24 `services/subscriptions.py`

| 项 | 内容 |
|---|---|
| 职责 | `subscriptions.json` 原子读写；订阅快照；后台检查；新话推送；启停循环 |
| 迁移源码 | `4650-4773` |
| 接口 | `load`、`save`、`snapshot`、`check_once`、`loop`、`start`、`stop` |
| 依赖 | `core/config.py`、`core/client.py`、`services/catalog.py`、`services/text.py`、`services/messaging.py`、`AstrBot Context`、`logger` |
| 注意 | 保留 `asyncio.Lock`；保留每本间隔 2 秒；保留 `subscribe_interval=0` 不启动；`initialize/terminate` 仍由 `main.py` 调用。 |

### 5.25 handlers

| 模块 | 职责 | 迁移命令逻辑 | 依赖 |
|---|---|---|---|
| `handlers/album.py` | 详情/话列表 | `jm_view` `2900-3094`、`jm_chapter_list` `3095-3228` | access、client、catalog、cache、pagination、messaging、text、selection（章节 ID 处理） |
| `handlers/download.py` | PDF/ZIP 下载、停止、进度 | `jm_download` `3229-3333`、`_consume_download` `3334-3358`、`jm_stop` `3359-3377`、`jm_now` `3378-3412`、`jm_download_zip` `3668-3700` | access、runner、progress、downloader、messaging、pagination? |
| `handlers/search.py` | 随机与五类搜索 | `jm_random` `3459-3536`、`jm_search` `3537-3571`、`jm_search_author` `3572-3595`、`jm_search_tag` `3596-3619`、`jm_search_actor` `3620-3643`、`jm_search_work` `3644-3667` | access、client、search、pagination、messaging、text |
| `handlers/library.py` | 排行、历史、评论 | `jm_ranking` `4014-4096`、`jm_history` `4097-4242`、`jm_comments` `4243-4417` | access、client、history、pagination、messaging、text、catalog |
| `handlers/account.py` | 签到、用户面板、whoami | `jm_checkin` `3852-3937`、`jm_user_info` `3938-4013`、`jm_whoami` `4418-4437` | access、client、checkin、messaging、text |
| `handlers/navigation.py` | `/jmnext` 与“下一页/下页” | `_follow_page_session` `3413-3427`、`jm_next` `3428-3445`、`jm_next_keyword` `3446-3458` | access、pagination、messaging；dispatch 表由 main 注入 |
| `handlers/favorites.py` | 收藏夹与导出 | `jm_favorite` `4900-5084` | access、favorites、state、messaging、text |
| `handlers/subscribe.py` | 订阅增删查 | `jm_subscribe` `4774-4899` | access、subscriptions、messaging、text |
| `handlers/system.py` | 域名、更新日志 | `jm_domain` `4438-4512`、`jm_changelog` `4513-4531` | access、client、config、messaging |
| `ui/help.py` | `/jm help` 文案 | `3238-3319` 的 help 文案 | `core/config.py`（只读 settings） |

### 5.26 `handlers/navigation.py` 的依赖处理

`/jmnext` 要重新调用某个命令 handler（`/jmlist`、`/jmhis`、`/jmc`、`/jmrank`、`/jms` 等）。为避免 `services -> handlers` 循环，采用：

```python
class NavigationHandler:
    def __init__(self, pagination, access, messaging, dispatch):
        self.dispatch = dispatch  # command name -> async callable(event)

    async def follow(self, event, session):
        handler = self.dispatch.get(command)
        ...
```

`main.py` 在组装时注入 `self._dispatch = {"jmlist": self.album_handler.chapter_list, ...}`。`NavigationHandler` 不 import `main`，也不 import 其他 handler。

---

## 6. 依赖关系

### 6.1 模块依赖图

```text
main
├── core.config
├── core.client
├── core.state
├── core.downloader
├── handlers.album
│   ├── services.access
│   ├── services.catalog
│   ├── services.cache
│   ├── services.pagination
│   ├── services.messaging
│   └── services.selection
├── handlers.download
│   ├── services.access
│   ├── services.download_runner
│   ├── services.progress
│   ├── core.downloader
│   └── services.messaging
├── handlers.search
│   ├── services.access
│   ├── services.search
│   ├── services.pagination
│   └── services.messaging
├── handlers.library
│   ├── services.access
│   ├── services.history
│   ├── services.pagination
│   ├── services.catalog
│   └── services.messaging
├── handlers.account
│   ├── services.access
│   ├── services.checkin
│   └── services.messaging
├── handlers.navigation
│   ├── services.access
│   ├── services.pagination
│   └── services.messaging
├── handlers.favorites
│   ├── services.favorites
│   └── services.access
├── handlers.subscribe
│   ├── services.subscriptions
│   └── services.access
├── handlers.system
│   └── services.access
└── ui.help
```

### 6.2 允许的跨层调用

- `handlers` 可以调用多个 `services`。
- `services/download_runner` 可以调用其他 `services`。
- `services` 之间允许单向依赖，例如 `packaging -> catalog -> cache`。
- `services` 不允许反向依赖 `handlers`。
- `core` 不允许依赖任何 `services`。
- `ui/help` 只依赖 `core/config`，不依赖运行期状态。

### 6.3 需要避免的环

- `cache` 不要 import `packaging`；`packaging` 可以 import `cache`。
- `catalog` 不要 import `packaging`；`packaging` 可以 import `catalog`。
- `download` 不要 import `download_runner`；`download_runner` 可以 import `download`。
- `pagination` 不要 import `handlers`；分页只存 session，导航由 `handlers/navigation.py` 做。
- `messaging` 不要 import `access`；权限检查在 handler 或 access 中完成。

---

## 7. 共享对象与生命周期

### 7.1 组装示例

```python
class JmComicPlugin(Star):
    def __init__(self, context, config=None):
        super().__init__(context)
        self.config = config or {}
        self.settings = PluginSettings.from_config(self.config, Path(__file__).resolve().parent)
        self.settings.prepare_directories()

        self.state = PluginState()
        self.client = JmClientManager(self.settings)
        self.text = TextService(self.settings)
        self.access = AccessService(self.settings)
        self.messaging = MessagingService(self.settings, self.text, self.state)
        self.quota = FileQuotaService(self.settings)
        self.history = HistoryService(self.settings)
        self.cache = CacheService(self.settings)
        self.catalog = CatalogService(self.settings, self.cache, self.text)
        self.selection = SelectionService(self.settings, self.client, self.cache, self.state)
        self.packaging = PackageService(self.settings, self.cache, self.catalog, self.text)
        self.downloads = DownloadService(self.settings, self.client, self.cache, self.messaging)
        self.runner = DownloadRunner(...)
        self.progress = ActiveDownloadService(self.state)
        self.pagination = PageSessionService(self.settings, self.state)
        self.search = SearchService(...)
        self.checkin = CheckinService(self.settings)
        self.favorites = FavoritesService(...)
        self.subscriptions = SubscriptionService(self.settings, self.client, self.catalog, self.context)
        self.album_handler = AlbumHandler(...)
        ...
```

这是示意，不是本次要写的代码。实际实现时按批次逐步引入，避免一次性重写 `__init__`。

### 7.2 生命周期

| 对象 | 创建 | 销毁/清理 |
|---|---|---|
| `PluginSettings` | 插件 `__init__` | 不销毁；只读 |
| `PluginState` | 插件 `__init__` | 插件卸载时随实例释放 |
| `JmClientManager` | 插件 `__init__` | 不需要显式关闭；`invalidate()` 只清缓存 |
| `ProgressDownloader` | 使用 jmcomic 下载时 | 每个 album 开始前 pop 旧状态；完成后保留最近状态供 `/jmnow` 读取 |
| `PageSessionService` | 插件 `__init__` | 读取时惰性清理超时项 |
| `Favorites pending_exports` | 插件 `__init__` | 每次导出前清理超过 30 秒的项 |
| `SubscriptionService` | 插件 `__init__` | `initialize()` 启动，`terminate()` 取消任务 |
| `DownloadRunner` | 插件 `__init__` | 无跨命令状态；每次 `run()` 内局部变量 |

### 7.3 数据目录

当前 `data_dir` 的计算依赖 `main.py` 的位置：

```text
plugins/astrbot_plugin_jmcomic/main.py
  -> parent = plugins
  -> parent.parent = data
  -> data/plugin_data/astrbot_plugin_jmcomic
```

拆出 `core/config.py` 后，`__file__` 会变成 `.../core/config.py`，如果仍按原逻辑会算错。**必须由 `main.py` 把插件根目录传给 `PluginSettings.from_config()`**，或者统一使用 AstrBot 的 `StarTools.get_data_dir`。本次建议保持原路径算法，只把 `plugin_dir` 作为参数传入，避免迁移数据。

`jm_changelog` 的 `Path(__file__).parent / "CHANGELOG.md"` 也有同样问题：迁移到 `handlers/system.py` 后，应使用 `settings.plugin_dir / "CHANGELOG.md"`，不能使用 handler 文件位置。

---

## 8. 分批迁移顺序

> 每批一个 commit（不 push），每批完成后运行全部本地测试并同步 C 盘插件副本。  
> 任何一批出现行为差异，优先回滚该批，不在同批里“顺手修”。

### 批次 0：基线与约束

**目标**：在动代码前固定行为基线。

- 记录当前 `main.py` 的哈希、行数、命令清单。
- 运行现有 7 个测试：
  - `test_jm_073.py`
  - `test_jm_075_bugfix.py`
  - `test_jm_077_features.py`
  - `test_jm_078_fixes.py`
  - `test_jm_078_new.py`
  - `test_jm_078_bug013.py`
  - `test_jm_regressions.py`
- 保存关键输出快照：`/jmv`、`/jmlist`、`/jm help`、`/jmnow`、`/jmnext`、`/jm 1228973` 的消息序列、文件名、消息条数。
- 把测试加载方式从 `spec_from_file_location("jmmain", main.py)` 改为包导入：
  - 将插件父目录加入 `sys.path`；
  - `importlib.import_module("astrbot_plugin_jmcomic.main")`；
  - 或在测试中构造包上下文。
- 这一步不改生产代码，但必须确认新加载方式能跑通，否则后续相对导入会失败。

**退出标准**：基线测试全绿；快照可对比；确认 `main.py` 仍为唯一入口。

### 批次 1：core 基础层

**目标**：把配置、客户端、状态、底层下载器、纯解析从 `main.py` 移出，命令逻辑基本不动。

**新增/修改**：

- `core/__init__.py`
- `core/config.py`：`PluginSettings` + `build_option` + 目录准备 + 缓存清理
- `core/state.py`：`PluginState`
- `core/client.py`：`JmClientManager`
- `core/downloader.py`：`ProgressDownloader` + `ADVANCED_RETRY_CONFIG`
- `core/models.py`：`PageUnit`
- `core/parsing.py`：`parse_positive_page`、`extract_id`

**迁移源码**：`38-127`、`143-159`、`163-388`、`523-573`、`236-247`、`2765-2773`、`2892-2899`、`4568-4577`。

**本批要求**：

- `main.py` 仍保留命令实现；只把构造和底层工具替换为 service 调用。
- `main.py` 暂时 re-export `PageUnit`、`ProgressDownloader`，防止旧测试立刻断裂。
- `PluginSettings` 必须由 `main.py` 传入 `plugin_dir`。
- 不改变 `self.config`、`self.option` 的可见性，避免其他代码同时改。

**验证**：

- 以 AstrBot 包路径导入成功。
- `/jmv 1228973`、`/jm 1484418`、`/jmnow` 冒烟。
- 全部 7 个测试通过。

**回滚**：恢复 `main.py` 与删除新增 `core/` 即可。

### 批次 2：通用服务

**目标**：拆出权限、文本、消息、限额、历史、进度、分页。

**新增**：

- `services/__init__.py`
- `services/access.py`
- `services/text.py`
- `services/messaging.py`
- `services/quota.py`
- `services/history.py`
- `services/progress.py`
- `services/pagination.py`

**迁移源码**：`389-425`、`426-522`、`574-593`、`1100-1129`、`1130-1173`、`1178-1194`、`1195-1232`、`1233-1263`、`1263-1305`、`1306-1323`、`1324-1390`。

**本批要求**：

- `_plain` / `_chain` 统一走 `messaging`。
- 权限、限额、历史、当前下载、分页 session 全部改为 service。
- `_ACCOUNT_COMMANDS` 保持在 `messaging`，不要复制。
- `PageEventProxy` 放到 `pagination`。

**验证**：

- 权限：群白名单、`allow_group=false`、私聊例外。
- 限额：每日/每小时、0=不限、限额中断提示。
- 文本：简繁转换、敏感词、文件名双扩展名清理。
- `/jmnow`、`/jmnext`、`/jmstop` 仍正常。
- R05、R08、R13 中与消息/权限/翻页相关的项。

### 批次 3：缓存与目录元数据

**目标**：拆出缓存定位和章节元数据。

**新增**：

- `services/cache.py`
- `services/catalog.py`

**迁移源码**：`594-670`、`671-1025`、`1026-1038`、`1039-1099`、`1648-1654`、`1797-1871`。

**本批要求**：

- 保持 `find_album_dir` 的候选顺序：`ProgressDownloader` 记录 → `JM{id}` → `JM{id}_*`。
- 保持 legacy `P{id}` → `P{id}_第N话` 原地迁移。
- 保持 metadata schema 字段不变。
- 保持 `_format_chapter_line` 输出格式。
- `resolve_chapter_parent` 通过参数接收 client，catalog 不持有 client manager。

**验证**：

- R06（缓存元数据/标题打包/旧目录迁移）。
- R12（`jmv`/`jmlist` 页数、章节行格式）。
- 删除/写坏 metadata 不崩溃。
- `jmv`、`jmlist`、`jmc` 的章节 ID 父漫画提示。

### 批次 4：目标识别与章节/页段选择

**目标**：拆出下载参数和选择算法。

**新增**：

- `services/selection.py`

**迁移源码**：`1402-1780`。

**本批要求**：

- 保留 `123` / `JM123` / `a123` / `p123` 解析。
- 保留 `-c` 的所有语法：`2`、`2-4`、`2,5,7`、`5:201-400`、`5:201-`、`5:1-5,5:10-15`。
- 保留倒置纠正、越界夹取、超大范围保护、整话优先、页段去重。
- 保留 PDF 按页、ZIP 按章数+页数、页段文件名后缀、续传表达式。
- `resolve_target` 的章节探测顺序不能变。

**验证**：

- R02（页段解析与续传）。
- R03（ZIP 与不连续页段）。
- R04（命名/批量/重复话）。
- 纯函数单测：`parse_page_range`、`merge_page_ranges`、`select_chapters`、`pack_units`、`resume_expr`。

### 批次 5：打包与命名

**目标**：拆出 PDF/ZIP/封面/命名。

**新增**：

- `services/packaging.py`

**迁移源码**：`1391-1401`、`1872-2191`。

**本批要求**：

- 保持 `img2pdf → reportlab → Pillow` 回退链。
- 保持 PDF 封面、PDF 属性、每个分册的信息页。
- 保持 ZIP `漫画信息.txt`、单话也包一层 `P{id}_第N话`。
- 保持文件名：`JM{id}_P{nnn}`、`JM{id}_P031-P035`、`JM{id}_P031,P035,P040`、页段 `_p001-200`。
- 保持 `_output_fresh` 的 mtime 复用逻辑。
- 封面临时 JPG 必须删除；临时输出文件发送后删除。
- `_zip_folder` 当前无调用，可在本批标记、在收尾批删除；不要在本批顺手改行为。

**验证**：

- R01（PDF 分册/信息页/页数上限）。
- R03（ZIP 合并/页段目录/页数上限）。
- R04（命名/双扩展名/缓存复用）。
- BUG-014：`/jmv` 封面 PDF 文件消息、`-pic` 不发。
- 字体缺失、img2pdf 失败、reportlab 失败三条回退链至少各有一个模拟测试。

### 批次 6：下载执行与消息预算

**目标**：拆出下载原语和统一下载编排。

**新增**：

- `services/download.py`
- `services/download_runner.py`

**迁移源码**：`2192-2276`、`2277-2328`、`2329-2717`。

**建议再分三小步**：

1. **6A**：只移 `_download_album_progress`、`_ensure_photo_downloaded`、sent ranges 去重。
2. **6B**：`_run_download` 原样搬到 `DownloadRunner.run()`，`main._consume_download` 暂时保留。
3. **6C**：把 `_consume_download` 的任务登记/清理移到 `handlers/download.py`，并接 `services/progress.py`。

**本批要求**：

- 不改下载算法、重试次数、缓存命中、文件发送顺序。
- 保留 `sent_messages` 计数和 `message_budget` 判断：QQ 官方单聊 4 条、群聊 5 条；预算不够时省略收尾汇总。
- 保留限额中断后的 `-c` 续传表达式。
- 保留历史记录、`tease_enabled`、`send_interval`、发送后 `unlink`。
- 保留 `PartialDownloadFailedException` / `RequestRetryAllFailException` 的部分完成处理。
- `_run_download` 第一轮不拆内部函数；稳定后再考虑 `run_target()` / `package_target()`。

**验证**：

- R04/R05（批量、重试、限额、历史）。
- R08（消息压缩、`/jmnow`、`/jmstop`、单聊/群聊条数）。
- R09（请求级重试与代理）。
- 真实环境至少跑 `JM1228973`（4 话）和 `JM1215253 -c 31-36`。

### 批次 7：只读命令处理器

**目标**：把详情、列表、搜索、排行、历史、评论、账号面板、系统信息迁到 handler。

**新增**：

- `handlers/__init__.py`
- `handlers/album.py`
- `handlers/search.py`
- `handlers/library.py`
- `handlers/account.py`
- `handlers/system.py`
- `ui/help.py`

**迁移源码**：`2900-3228`、`3459-3667`、`3852-4013`、`4014-4417`、`4418-4531`。

**本批要求**：

- `main.py` 只保留装饰器和 `async for ...: yield`。
- `jm_download` 的 help 文本移到 `ui/help.py`，用 settings 渲染。
- `jm_view`/`jmlist`/`jmc` 的章节 ID 提示统一复用 catalog 方法。
- `jm_search*` 统一走 `services/search.py`，不复制分页换算。
- `jm_history -all` 保留管理员判断和每用户 10 条。
- `jm_comments` 保留回复树、剧透、点赞、3500 字截断。
- `jm_changelog` 使用 `settings.plugin_dir / "CHANGELOG.md"`。

**验证**：

- F01–F07、F13–F20 的只读命令冒烟。
- R07、R10、R12。
- `/jm help` 文案与当前一致（只允许 BUG-013/014 已有文案）。
- 所有命令名、别名、权限提示不变。

### 批次 8：下载/翻页/账号命令处理器

**目标**：把下载入口、停止、进度、翻页迁到 handler。

**新增**：

- `handlers/download.py`
- `handlers/navigation.py`

**迁移源码**：`3229-3412`、`3413-3458`、`3668-3700`。

**本批要求**：

- `_consume_download` 的任务登记/清理保留 try/finally。
- `/jmstop` 仍取消当前用户所有未完成任务。
- `/jmnow` 仍合并 `PluginState.active_downloads` 与 `ProgressDownloader._progress`。
- `/jmnext` 和“下一页/下页”仍按 `(umo, sender_id)` 隔离。
- `handlers/navigation.py` 通过 main 注入 dispatch 表，不 import 其他 handler。
- 无有效 session 时，`/jmnext` 提示，纯文本“下一页/下页”静默。

**验证**：

- R08：消息条数、`/jmnow` 三种输出、`/jmstop` 后恢复无任务。
- R13：`/jmlist`、`/jms`、`/jmc`、`/jmhis`、`/jmrank` 后 `/jmnext`；60 秒超时；`0` 关闭；用户/群隔离；`下一页/下页`。
- `/jm 1484418`（1 话/6 页）真实环境成功。

### 批次 9：收藏/订阅与收尾

**目标**：迁移最后两个功能域，删除临时兼容层，完成验收。

**新增**：

- `services/favorites.py`
- `services/subscriptions.py`
- `handlers/favorites.py`
- `handlers/subscribe.py`

**迁移源码**：`4532-4649`、`4650-4773`、`4774-5084`；`5085-5103` 生命周期保留 main。

**本批要求**：

- 收藏夹导出 30 秒二次确认不变；只允许号主；CSV 无加密。
- 订阅 `list`/`del`/新增基线不变；后台循环、锁、2 秒间隔不变。
- `initialize/terminate` 只负责启动/停止订阅任务。
- 删除临时 re-export、死代码、未使用 import。
- 更新 `README.md`、`开发文档与修复日志.md`、`CHANGELOG.md` 中“文件结构/开发说明”部分（如果有必要）。
- 同步 C 盘插件副本并校验哈希。

**验证**：

- R11（导出/订阅）。
- 全量 7 个测试 + 新增包导入测试。
- 真实环境全量冒烟（见第 10 节）。

---

## 9. 风险点与对策

| # | 风险 | 影响 | 对策 | 验证 |
|---|---|---|---|---|
| 1 | AstrBot 命令注册失效 | 所有命令消失 | 保留所有 `@filter.command` / `@filter.regex` 在 `JmComicPlugin` 内；handler 不加装饰器；参考 Pixiv 的“main 薄包装”模式 | 启动后 `/jm help`、`/jmv`、`/jmnext` 可触发 |
| 2 | 数据目录算错 | 下载/历史/订阅写到错误目录 | `PluginSettings` 接收 `main.py` 传入的 `plugin_dir`；禁止用 `core/config.py` 的 `__file__` 推算 | 检查 `data_dir`、`download_base_dir` 与当前一致 |
| 3 | 测试加载方式不兼容 | 本地测试全部 import 失败 | 批次 0 改为包导入；临时 re-export `PageUnit`/`ProgressDownloader`；最后更新测试 | `importlib.import_module("astrbot_plugin_jmcomic.main")` 成功 |
| 4 | 循环导入 | 插件启动失败/热重载失败 | 严格依赖方向；`TYPE_CHECKING`；navigation 用 dispatch 注入；services 不 import handlers | `python -X importtime` 或启动日志无 ImportError |
| 5 | 共享状态被复制 | `/jmnow` 看不到进度、`/jmstop` 取消失败 | `PluginState` 唯一实例；`ProgressDownloader` 单一模块；不在 handler 里 new 第二份 | 下载中 `/jmnow`、`/jmstop` 实测 |
| 6 | 异步阻塞 | 事件循环卡住、QQ 超时 | 保持现有 `asyncio.to_thread`：图片 I/O、Pillow、PDF/ZIP、CSV、同步 jmcomic 请求 | 大文件下载/打包时其他命令仍可响应 |
| 7 | 客户端登录缓存变化 | 账号命令 401、重复登录 | `JmClientManager` 单例缓存；保留登录失败提示；不在 service 内重新 `new_jm_client()` | `/jmme`、`/jmcheckin`、`/jmfav` |
| 8 | 消息预算逻辑丢失 | QQ 官方触发主动消息、延迟/失败 | `sent_messages` 与 `message_budget` 留在 `DownloadRunner`；不把每条消息拆给不同 service 计数 | R08：单聊 ≤4、群聊 ≤5，预算不足省略汇总 |
| 9 | 文件命名/分组变化 | 用户缓存不命中、文件混乱 | `packaging.py` 原样搬移；用固定样例比对文件名；不改 `JM{Aid}/P{Pid}_第{N}话` | R01/R03/R04/R12 |
| 10 | 缓存/元数据变化 | 重复下载、标题丢失 | `cache.py` 保持候选顺序和 metadata schema；legacy 迁移保留 | R06/R12；旧缓存升级测试 |
| 11 | PDF 封面/内存问题 | 大 PDF 崩溃、临时文件残留 | 保留 `img2pdf` 优先、reportlab 逐页、Pillow 回退；封面临时文件 `finally` 删除 | 大页数 PDF、字体缺失、img2pdf 失败 |
| 12 | 章节行/页数格式变化 | BUG-012 回归 | `catalog.format_chapter_line` 单点实现；`jmv`/`jmlist` 共用 | R12：`页数未知` 回退 |
| 13 | 翻页 session 串用户 | A 用户看到 B 用户下一页 | key 固定 `(umo, sender_id)`；`page_turn_timeout=0` 关闭；最后一页清除 | R13：不同用户/群隔离 |
| 14 | 账号命令被敏感词转换 | 用户 ID/账号信息被改 | `messaging.ACCOUNT_COMMANDS` 保持原集合；`_output_text`/`_chain` 绕过 | `/jmwhoami`、`/jmme` 输出原 ID |
| 15 | 订阅后台任务泄漏 | 重载后重复推送/任务未取消 | `SubscriptionService.start/stop`；`terminate()` 取消并 await；保留锁 | 重载插件两次，日志只启动一个检查任务 |
| 16 | 旧测试依赖私有方法 | 迁移后测试大量失败 | 批次 0/1 增加 re-export；后续把测试改为从 service 导入；不要长期保留私有 shim | 全量测试通过 |
| 17 | 热重载模块残留 | 旧 handler 仍注册 | 所有模块在插件包内；避免 `sys.path` 污染；AstrBot 会按 `data.plugins.<name>` 清理 | 重载后命令不重复触发 |
| 18 | 顺手修 bug 导致行为漂移 | 难以定位回归 | 重构批次不修功能 bug；发现的问题记录到 BUG 列表，单独提交 | diff 审查：只允许移动/委托 |

---

## 10. 验收清单

### 10.1 结构验收

- [x] `main.py` 只包含 `@register`、`JmComicPlugin` 组装、命令包装、`initialize/terminate`。
- [x] `main.py` 行数 ≤ 450（不含注释/空行可另算）。
- [x] `main.py` 不再出现 `download_album_async`、`zipfile`、`PIL`、`reportlab`、`img2pdf`、`search_site` 等业务实现。
- [x] `rg "from main import|import main"` 在插件代码中无结果。
- [x] `core` 不 import `services/handlers/main`。
- [x] `services` 不 import `handlers/main`。
- [x] `handlers` 不 import `main`。
- [x] 所有命令函数名、别名、装饰器参数与当前一致。
- [x] `PageUnit`、`ProgressDownloader` 只有一个定义；旧 re-export 已清理或明确保留原因。

### 10.2 命令验收

- [ ] `/jmv`：详情、选项、章节列表、封面 PDF、`-pic`、章节 ID 提示。
- [ ] `/jmlist`：分页、页数、章节行格式、违规检测、`/jmnext`。
- [ ] `/jm` / `/jmphoto`：整本、单章、批量、`-c`、页段续传、PDF 分册。
- [ ] `/jmzip`：整本、单章、批量、`-c`、ZIP 合并、单话目录。
- [ ] `/jmstop`：取消当前用户任务；`/jmnow` 恢复“当前还没有下载”。
- [ ] `/jmnow`：无任务、识别中、已知总数、总数统计中四种输出。
- [ ] `/jmnext`：有效 session、无 session、超时、最后一页、`page_turn_timeout=0`。
- [ ] “下一页/下页”：有效时翻页，无 session 静默。
- [ ] `/jms` `/jma` `/jmt` `/jmr` `/jmw`：关键词、页码、排序、简繁双搜、无结果。
- [ ] `/jmrand`：标签随机、榜单随机、空结果。
- [ ] `/jmrank`：day/week/month、页码、下一页。
- [ ] `/jmhis`：个人、`clear`、`-all`、管理员校验、每用户 10 条。
- [ ] `/jmc`：评论、回复、剧透、点赞、翻页、0 条、超页。
- [ ] `/jmcheckin`、`/jmme`：号主限制、签到状态、连续天数、收藏数。
- [ ] `/jmwhoami`：发送者 ID、管理员、号主状态。
- [ ] `/jmdomain`、`/jmupdate`：域名列表、最新 CHANGELOG。
- [ ] `/jmsub`：新增、`list`、`del`、重复订阅、`subscribe_interval=0`。
- [ ] `/jmfav`：列表、文件夹、add、del、export 二次确认、CSV。

### 10.3 行为验收

- [ ] 配置 schema 未改；`_conf_schema.json` 字段/默认值/描述不变。
- [ ] 版本号仍为 `0.7.8`。
- [ ] 缓存目录仍为 `JM{Aid}/P{Pid}_第N话/图片`，标题只在 `metadata.json`。
- [ ] 旧 `P{id}` 目录仍会自动迁移。
- [ ] 输出文件名仍符合 `JM{漫画id}_P{话序号}` / `_p起始-结束` 规则。
- [ ] PDF 分册、信息页、属性、封面行为不变。
- [ ] ZIP 合并、页数上限、`漫画信息.txt`、单话目录行为不变。
- [ ] BUG-013 A 版：取消逐章/缓存/打包/进度消息；开始状态 + 文件消息 + 必要错误；预算允许时合并汇总。
- [ ] BUG-014：`/jmv` 封面走 PDF 文件消息；`-pic` 不发；临时文件删除。
- [ ] `/jmv`、`/jmlist` 章节页数格式不变，缺失显示“页数未知”。
- [ ] 敏感词/简繁转换仍生效；账号命令绕过。
- [ ] 群白名单、`allow_group`、私聊例外、号主限制不变。
- [ ] 每日/每小时文件限额、0=不限、限额中断续传提示不变。
- [ ] 下载历史、`history_max=0`、`-all` 分页不变。
- [ ] 订阅文件原子写入、后台检查间隔、推送文案不变。

### 10.4 测试验收

- [ ] `test_jm_073.py` 通过。
- [ ] `test_jm_075_bugfix.py` 通过。
- [ ] `test_jm_077_features.py` 通过。
- [ ] `test_jm_078_fixes.py` 通过。
- [ ] `test_jm_078_new.py` 通过。
- [ ] `test_jm_078_bug013.py` 通过。
- [ ] `test_jm_regressions.py` 通过。
- [ ] 新增包导入测试通过。
- [x] `python -m compileall` 通过。
- [ ] 真实环境 R08（消息压缩/进度）通过。
- [ ] 真实环境 R13（翻页/封面 PDF）通过。
- [ ] 真实环境 R01/R02/R03/R04/R06/R12 抽样通过。
- [ ] 真实环境 R11（收藏导出/订阅）通过。

### 10.5 部署验收

- [ ] 仓库 `D:\1.Project\Gitproject\Github\astrbot_plugin_jmcomic` 与运行目录 `C:\Users\Administrator\.astrbot\data\plugins\astrbot_plugin_jmcomic` 文件一致。
- [ ] `main.py`、`core/`、`services/`、`handlers/`、`ui/` 全部同步。
- [ ] 哈希校验一致（至少 `main.py` 与新增模块）。
- [ ] 插件重载成功，日志无 ImportError/AttributeError。
- [ ] 未 push；历史清理问题未处理前不推送。
- [ ] `README.md`、`开发文档与修复日志.md`、`CHANGELOG.md` 的“文件结构/开发说明”已更新；功能行为描述不提前改。

---

## 附录 A：完整方法映射

> 行号为当前 `main.py`，仅用于迁移定位。  
> “→”左侧为现方法，右侧为目标模块/方法名。装饰器包装器保留在 `main.py`。

### A.1 core

| 现方法 | 行号 | 目标 |
|---|---:|---|
| `ProgressDownloader` | 44-127 | `core/downloader.py` |
| `ADVANCED_RETRY_CONFIG` | 38-41 | `core/downloader.py` |
| `PageEventProxy` | 129-141 | `services/pagination.py` |
| `PageUnit` | 143-159 | `core/models.py` |
| `__init__` 配置/路径/option 部分 | 163-388 | `core/config.py`（`PluginSettings`） |
| `__init__` 状态字段 | 236-247 | `core/state.py`（`PluginState`） |
| `_handle_jm_exception` | 523-540 | `core/client.py` |
| `_new_logged_client` | 541-569 | `core/client.py` |
| `_invalidate_client` | 570-573 | `core/client.py` |
| `_configured_username` | 4568-4577 | `core/client.py` |
| `_parse_positive_page` | 2765-2773 | `core/parsing.py` |
| `_extract_id` | 2892-2899 | `core/parsing.py` |

### A.2 services

| 现方法 | 行号 | 目标 |
|---|---:|---|
| `_check_permission` | 389-400 | `services/access.py` |
| `_permission_denied_text` | 401-410 | `services/access.py` |
| `_is_owner` | 411-416 | `services/access.py` |
| `_owner_required_text` | 417-425 | `services/access.py` |
| `_load_send_times` | 426-439 | `services/quota.py` |
| `_save_send_times` | 440-449 | `services/quota.py` |
| `_file_quota` | 450-473 | `services/quota.py` |
| `_quota_reached` | 474-481 | `services/quota.py` |
| `_quota_display` | 482-485 | `services/quota.py` |
| `_limit_text` | 486-488 | `services/quota.py` |
| `_zip_page_hint` | 489-493 | `core/config.py`（或 `quota.py`；建议 config） |
| `_record_file_send` | 494-499 | `services/quota.py` |
| `_quota_block_text` | 500-515 | `services/quota.py` |
| `_quota_status_text` | 516-522 | `services/quota.py` |
| `_to_simplified` | 574-583 | `services/text.py` |
| `_to_traditional` | 584-593 | `services/text.py` |
| `_album_dir_has_images` | 594-603 | `services/cache.py` |
| `_find_album_dir` | 604-637 | `services/cache.py` |
| `_metadata_file` | 638-640 | `services/cache.py` |
| `_read_metadata` | 641-650 | `services/cache.py` |
| `_write_metadata` | 651-670 | `services/cache.py` |
| `_text_list` | 671-677 | `services/catalog.py` |
| `_album_display_title` | 678-684 | `services/catalog.py` |
| `_chapter_display_title` | 685-701 | `services/catalog.py` |
| `_photo_page_count` | 702-720 | `services/catalog.py` |
| `_photo_range_pages` | 721-733 | `services/catalog.py` |
| `_format_chapter_line` | 734-745 | `services/catalog.py` |
| `_save_target_metadata` | 746-875 | `services/catalog.py` |
| `_known_photo_position` | 876-899 | `services/catalog.py` |
| `_photo_position` | 900-906 | `services/catalog.py` |
| `_unit_metadata_index` | 907-913 | `services/catalog.py` |
| `_album_info_text` | 914-956 | `services/catalog.py` |
| `_album_cover_text` | 957-995 | `services/catalog.py` |
| `_pdf_metadata` | 996-1025 | `services/catalog.py` |
| `_album_cache_complete` | 1026-1038 | `services/cache.py` |
| `_looks_like_chapter_album` | 1039-1070 | `services/catalog.py` |
| `_chapter_album_parent_id` | 1071-1085 | `services/catalog.py` |
| `_resolve_chapter_album_parent` | 1086-1099 | `services/catalog.py` |
| `_load_history` | 1100-1110 | `services/history.py` |
| `_save_history` | 1111-1114 | `services/history.py` |
| `_get_user_history` | 1115-1118 | `services/history.py` |
| `_add_user_history` | 1119-1129 | `services/history.py` |
| `_sanitize_tags` | 1130-1160 | `services/text.py` |
| `_safe_text` | 1161-1175 | `services/text.py` |
| `_command_name` | 1178-1184 | `services/messaging.py` |
| `_output_text` | 1185-1190 | `services/messaging.py` |
| `_plain` | 1191-1194 | `services/messaging.py` |
| `_set_active_download` | 1195-1204 | `services/progress.py` |
| `_update_active_download` | 1205-1211 | `services/progress.py` |
| `_clear_active_download` | 1212-1217 | `services/progress.py` |
| `_active_download` | 1218-1232 | `services/progress.py` |
| `_page_session_key` | 1233-1236 | `services/pagination.py` |
| `_remember_page` | 1237-1249 | `services/pagination.py` |
| `_page_session` | 1250-1259 | `services/pagination.py` |
| `_clear_page_session` | 1260-1263 | `services/pagination.py` |
| `_is_qq_official` | 1264-1270 | `services/messaging.py` |
| `_message_budget` | 1271-1280 | `services/messaging.py` |
| `_page_handler_name` | 1281-1293 | `services/pagination.py` |
| `_chain` | 1294-1305 | `services/messaging.py` |
| `_safe_filename` | 1306-1323 | `services/text.py` |
| `_backend_log_path` | 1324-1341 | `services/messaging.py` |
| `_log_pos` | 1342-1350 | `services/messaging.py` |
| `_log_has_violation` | 1351-1367 | `services/messaging.py` |
| `_is_violation_error` | 1368-1390 | `services/messaging.py` |
| `_zip_folder` | 1391-1401 | `services/packaging.py`（当前无调用，收尾批删除） |
| `_parse_download_args` | 1402-1419 | `services/selection.py` |
| `_normalize_target` | 1420-1432 | `services/selection.py` |
| `_resolve_target` | 1433-1493 | `services/selection.py` |
| `_parse_page_range` | 1494-1524 | `services/selection.py` |
| `_merge_page_ranges` | 1525-1561 | `services/selection.py` |
| `_select_chapters` | 1562-1647 | `services/selection.py` |
| `_chapter_image_paths` | 1648-1654 | `services/cache.py` |
| `_expand_page_units` | 1655-1682 | `services/selection.py` |
| `_pack_units` | 1683-1704 | `services/selection.py` |
| `_normalize_selected_ranges` | 1705-1728 | `services/selection.py` |
| `_plan_pdf_groups` | 1729-1733 | `services/selection.py` |
| `_plan_zip_groups` | 1734-1742 | `services/selection.py` |
| `_resume_expr` | 1743-1780 | `services/selection.py` |
| `_format_index_ranges` | 1781-1796 | `services/selection.py` |
| `_migrate_legacy_chapter_dir` | 1797-1819 | `services/cache.py` |
| `_chapter_dir` | 1820-1865 | `services/cache.py` |
| `_chapter_images` | 1866-1871 | `services/cache.py` |
| `_output_fresh` | 1872-1887 | `services/packaging.py` |
| `_find_cover_font` | 1888-1907 | `services/packaging.py` |
| `_make_cover_image` | 1908-1959 | `services/packaging.py` |
| `_build_pdf` | 1960-2082 | `services/packaging.py` |
| `_build_zip` | 2083-2101 | `services/packaging.py` |
| `_zip_chapter_folder` | 2102-2107 | `services/packaging.py` |
| `_chapter_file_stem` | 2108-2117 | `services/packaging.py` |
| `_group_file_stem` | 2118-2126 | `services/packaging.py` |
| `_format_index_token` | 2127-2130 | `services/packaging.py` |
| `_format_position_token` | 2131-2143 | `services/packaging.py` |
| `_file_stem_for_entries` | 2144-2184 | `services/packaging.py` |
| `_zip_arc_dir` | 2185-2191 | `services/packaging.py` |
| `_download_album_progress` | 2192-2254 | `services/download.py` |
| `_ensure_photo_downloaded` | 2255-2276 | `services/download.py` |
| `_tease_for_count` | 2277-2290 | `services/download_runner.py` |
| `_add_sent_ranges` | 2291-2305 | `services/download.py` |
| `_range_covered` | 2306-2311 | `services/download.py` |
| `_filter_duplicate_selections` | 2312-2328 | `services/download.py` |
| `_run_download` | 2329-2717 | `services/download_runner.py` |
| `_parse_search_args` | 2718-2764 | `services/search.py` |
| `_render_search_page` | 2774-2891 | `services/search.py` |
| `_follow_page_session` | 3413-3427 | `handlers/navigation.py` |
| `_consume_download` | 3334-3358 | `handlers/download.py` |
| `_parse_record_day` | 3701-3717 | `services/checkin.py` |
| `_load_checkin_state` | 3718-3727 | `services/checkin.py` |
| `_save_checkin_state` | 3728-3743 | `services/checkin.py` |
| `_local_streak` | 3744-3752 | `services/checkin.py` |
| `_daily_payload` | 3753-3768 | `services/checkin.py` |
| `_parse_daily_info` | 3769-3851 | `services/checkin.py` |
| `_clear_client_cache` | 4532-4540 | `services/favorites.py` |
| `_favorite_state` | 4541-4546 | `services/favorites.py` |
| `_render_favorites` | 4547-4567 | `services/favorites.py` |
| `_collect_favorite_rows` | 4578-4595 | `services/favorites.py` |
| `_write_favorites_csv` | 4596-4603 | `services/favorites.py` |
| `_export_favorites` | 4604-4649 | `services/favorites.py` |
| `_load_subscriptions` | 4650-4657 | `services/subscriptions.py` |
| `_save_subscriptions` | 4658-4669 | `services/subscriptions.py` |
| `_album_photo_ids` | 4670-4680 | `services/subscriptions.py` |
| `_subscription_snapshot` | 4681-4691 | `services/subscriptions.py` |
| `_check_subscriptions_once` | 4692-4758 | `services/subscriptions.py` |
| `_subscription_loop` | 4759-4773 | `services/subscriptions.py` |

### A.3 handlers

| 现命令逻辑 | 行号 | 目标 |
|---|---:|---|
| `jm_view` | 2900-3094 | `handlers/album.py` |
| `jm_chapter_list` | 3095-3228 | `handlers/album.py` |
| `jm_download` | 3229-3333 | `handlers/download.py` |
| `jm_stop` | 3359-3377 | `handlers/download.py` |
| `jm_now` | 3378-3412 | `handlers/download.py` |
| `jm_next` | 3428-3445 | `handlers/navigation.py` |
| `jm_next_keyword` | 3446-3458 | `handlers/navigation.py` |
| `jm_random` | 3459-3536 | `handlers/search.py` |
| `jm_search` | 3537-3571 | `handlers/search.py` |
| `jm_search_author` | 3572-3595 | `handlers/search.py` |
| `jm_search_tag` | 3596-3619 | `handlers/search.py` |
| `jm_search_actor` | 3620-3643 | `handlers/search.py` |
| `jm_search_work` | 3644-3667 | `handlers/search.py` |
| `jm_download_zip` | 3668-3700 | `handlers/download.py` |
| `jm_checkin` | 3852-3937 | `handlers/account.py` |
| `jm_user_info` | 3938-4013 | `handlers/account.py` |
| `jm_ranking` | 4014-4096 | `handlers/library.py` |
| `jm_history` | 4097-4242 | `handlers/library.py` |
| `jm_comments` | 4243-4417 | `handlers/library.py` |
| `jm_whoami` | 4418-4437 | `handlers/account.py` |
| `jm_domain` | 4438-4512 | `handlers/system.py` |
| `jm_changelog` | 4513-4531 | `handlers/system.py` |
| `jm_subscribe` | 4774-4899 | `handlers/subscribe.py` |
| `jm_favorite` | 4900-5084 | `handlers/favorites.py` |
| `initialize` | 5085-5092 | `main.py` + `services/subscriptions.py` |
| `terminate` | 5093-5103 | `main.py` + `services/subscriptions.py` |

---

## 附录 B：命令包装器映射

`main.py` 中保留的包装器建议如下（函数名不变）：

| 命令 | 包装器 | 目标 handler 方法 |
|---|---|---|
| `/jmv` | `jm_view` | `album_handler.view` |
| `/jmlist` | `jm_chapter_list` | `album_handler.chapter_list` |
| `/jm`、`/jmphoto` | `jm_download` | `download_handler.download_pdf` |
| `/jmzip` | `jm_download_zip` | `download_handler.download_zip` |
| `/jmstop` | `jm_stop` | `download_handler.stop` |
| `/jmnow` | `jm_now` | `download_handler.now` |
| `/jmnext` | `jm_next` | `navigation_handler.next` |
| “下一页/下页” | `jm_next_keyword` | `navigation_handler.keyword` |
| `/jmrand` | `jm_random` | `search_handler.random` |
| `/jms` | `jm_search` | `search_handler.search_site` |
| `/jma` | `jm_search_author` | `search_handler.search_author` |
| `/jmt` | `jm_search_tag` | `search_handler.search_tag` |
| `/jmr` | `jm_search_actor` | `search_handler.search_actor` |
| `/jmw` | `jm_search_work` | `search_handler.search_work` |
| `/jmcheckin` | `jm_checkin` | `account_handler.checkin` |
| `/jmme` | `jm_user_info` | `account_handler.user_info` |
| `/jmwhoami` | `jm_whoami` | `account_handler.whoami` |
| `/jmrank` | `jm_ranking` | `library_handler.ranking` |
| `/jmhis` | `jm_history` | `library_handler.history` |
| `/jmc` | `jm_comments` | `library_handler.comments` |
| `/jmdomain` | `jm_domain` | `system_handler.domain` |
| `/jmupdate` | `jm_changelog` | `system_handler.changelog` |
| `/jmsub` | `jm_subscribe` | `subscribe_handler.handle` |
| `/jmfav` | `jm_favorite` | `favorites_handler.handle` |

包装器统一形式：

```python
@filter.command("jmv")
async def jm_view(self, event: AstrMessageEvent):
    async for message in self.album_handler.view(event):
        yield message
```

注意：

- 不要改函数名；AstrBot 的 handler 记录会保存函数名。
- 不要改 alias 集合；`/jmphoto` 仍映射到 `jm_download`。
- 不要把 `@filter.regex` 改成普通事件监听；当前正则只在完整匹配“下一页/下页”时触发。
- `jm_next_keyword` 无 session 时保持静默，不返回提示。

---

## 附录 C：测试矩阵建议

| 批次 | 最低测试 |
|---|---|
| 0 | 7 个现有套件 + 包导入测试 |
| 1 | 导入测试、`/jmv`、`/jm` 单话、`/jmnow` |
| 2 | 权限、限额、文本、翻页、`/jmnow`、`/jmnext` |
| 3 | R06、R12、旧缓存迁移、metadata 损坏 |
| 4 | R02、R03、R04、纯函数单测 |
| 5 | R01、R03、R04、BUG-014、三条 PDF 回退链 |
| 6 | R04、R05、R08、R09、`/jmstop` |
| 7 | F01–F07、F13–F20、R07、R10、R12 |
| 8 | R08、R13、`/jm 1484418` |
| 9 | R11、全量 7 套件、真实环境冒烟、C 盘同步 |

真实环境重点样本：

- `JM1228973`（4 话）：消息压缩、`/jmnow`、`/jmstop`。
- `JM1215253 -c 31-36`（146 话中的 6 话）：多分册、信息页、命名。
- `JM43164`（172 话/4862 页）：大本、分页、限额中断。
- `JM279092`（32 话/892 页）：整本 PDF/ZIP。
- `JM1484418`（1 话/6 页）：单话快速冒烟。
- `/jmv 1228973`：封面 PDF 与 `-pic`。
- `/jmlist 1215253`、`/jms ...`、`/jmc ...`、`/jmhis`、`/jmrank day`：`/jmnext` 与“下一页/下页”。

---

## 附录 D：不纳入本次重构的事项

以下事项容易与“搬家”混淆，明确不做：

- 不实现 BUG-013 B 版消息队列/待发送文件队列。
- 不改 QQ 官方消息预算算法。
- 不改 PDF 封面为图片、不改 BUG-014 的方案。
- 不引入 SQLite/peewee/Redis。
- 不引入新的 HTTP 客户端；继续使用 jmcomic。
- 不改配置 schema、版本号、缓存目录、输出文件名。
- 不做历史提交清理、不 push。
- 不顺手修 `_zip_folder` 未使用、重复分页换算等小问题；这些只在收尾批删除死代码，行为不变。
- 不把命令装饰器移到 handler 类；AstrBot 注册方式保持现状。

---

## 附录 E：实施前的最后检查

- [ ] 确认当前 HEAD 为 `bd9a130`，工作区无未提交代码改动。
- [ ] 确认仓库与 C 盘插件副本当前一致。
- [ ] 确认测试加载方式改造方案可行（包导入）。
- [ ] 确认 `plugin_dir` 传递后 `data_dir` 与现网一致。
- [ ] 确认 `ProgressDownloader` 只有一个模块副本。
- [ ] 确认 `main.py` 中所有命令包装器的函数名/别名清单已抄录。
- [ ] 确认每批迁移只做“移动 + 委托”，不混入功能修复。

---

## 11. 实施进度

### 11.1 批次状态

| 批次 | 内容 | 状态 | 提交 |
|---|---|---|---|
| 0 | 基线与约束（目录骨架、包导入） | 完成 | `a7fcafd` |
| 1 | core 基础层（models/config/state/client/downloader/parsing） | 完成 | `0da473e` `4334503` `fd1b1ce` `8d1d7f0` `2664887` `4a7961a` |
| 2 | 通用服务（access/text/messaging/quota/history/progress/pagination） | 完成 | `4c232d7` `f4d8c31` `90d1182` `28b8df7` `d5622de` `d57896e` `1739da7` |
| 3 | 缓存与目录元数据 | 完成 | `2c356ec` `f0a8639` |
| 4 | 目标识别与章节/页段选择 | 完成 | `6939fa6` |
| 5 | 打包与命名 | 完成 | `755240c` |
| 6 | 下载执行与消息预算 | 完成 | `1032fa1` `d94444f` `b8a9424` `5a3cfca` `aef016c` `9ab91f8` |
| 7 | 只读命令处理器 | 完成 | `2b0daf8` `8753c79` `a5fcdc4` |
| 8 | 下载/翻页/账号命令处理器 | 完成 | `4c227a6` `bed60a7` `b49cb73` `1299512` |
| 9 | 收藏/订阅与收尾 | 完成 | `65afb4e` `5e70ba2` |

### 11.2 收尾结果

- `main.py`：321 行，仅保留 `@register`、`JmComicPlugin` 组装、24 个命令包装器、`initialize/terminate`。
- 命令包装器与重构前逐行一致（`@filter.command` / `@filter.regex` 共 24 条，`git diff` 无差异）。
- 兼容属性层已删除：全部改用 `self.settings.*`；`_zip_folder` 死代码已删除。
- 帮助文案迁至 `ui/help.py`（`build_help_text(settings, quota)`），与迁移前字符串逐字一致（AST 提取 + eval 比对 `IDENTICAL`）。
- `handlers/`、`ui/` 补 `__init__.py`，与 `core/`、`services/` 一致。
- 静态检查：`ruff check --select F821,F811,F401,F841` 全通过；`python -m compileall` 通过。
- 未完成：本地无 `astrbot`/`jmcomic` 运行环境，10.2 命令验收、10.4 真实环境用例需在 AstrBot 实例中执行；仓库内未包含 `test_jm_*.py` 套件，10.4 的单测项待补。