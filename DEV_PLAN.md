# JMComic 插件开发计划

> 基于 jmcomic 源码库深度分析后的功能扩展计划
> 起始版本：v0.5.2 → 目标版本：v0.6.0

---

## 一、项目背景

基于 hect0x7/JMComic-Crawler-Python 库开发的 AstrBot QQ 机器人插件，纯自用。
当前版本 v0.5.2 已实现搜索、详情、评论、下载(ZIP/PDF)、排行榜、随机、收藏夹列表、历史记录等基础功能。

经深度分析 jmcomic 源码库（14个核心模块、21个内置插件、同步/异步双客户端），发现大量高价值能力未被利用，且当前使用 HTML 客户端存在功能瓶颈。

---

## 二、核心问题诊断

### 2.1 最大瓶颈：使用了 HTML 客户端

当前配置 `"client": {"impl": "html"}`，而 jmcomic 提供两套客户端：

| 能力 | HTML 客户端（当前） | API 客户端（`impl: "api"`） |
|---|---|---|
| 搜索 | 支持全部参数 | 不支持 category/sub_category |
| 收藏添加 | **报错**（已验证做不通） | `toggle_favorite_album` 正常 |
| 评论翻页 | **不支持**（最多10条） | 支持，有 total/page_count |
| 评论总数 | **解析不出** | 正常返回 |
| 每日签到 | 支持 | 支持，返回更详细 |
| 用户信息 | 无 | `setting()` 返回金币/等级/经验 |
| 域名自动更新 | 无 | 自动从 API 服务器拉取 |

**结论：总结中标记的三个"做不通"方向（收藏添加报错、评论不翻页、总数解析不出）本质是 HTML 客户端限制，切换 API 客户端即可解决。**

### 2.2 其他技术债

1. **手写 ZIP/PDF 生成** — 未使用 jmcomic 内置 `Feature.export_zip` / `Feature.export_pdf`，缺少加密、自动清理等能力
2. **`asyncio.to_thread` 包装同步调用** — 未使用原生 `AsyncJmApiClient`，性能有损耗
3. **异常处理粗糙** — 统一 `except Exception`，未区分 ID不存在/网络失败/部分下载失败
4. **每次新建 client** — 未复用客户端缓存，重复请求
5. **下载无进度反馈** — 大文件只有"开始"和"完成"两条消息

---

## 三、目标功能清单

### P0 — 基础架构升级（必须先做）

| 编号 | 功能 | 说明 |
|---|---|---|
| P0-1 | 切换 API 客户端 | `impl: "html"` → `"api"`，解锁收藏/评论翻页/签到 |
| P0-2 | 异常细化 | 区分 MissingAlbumPhotoException / RequestRetryAllFailException / PartialDownloadFailedException |
| P0-3 | 下载进度推送 | 基于 DownloadCallback 钩子，定期推送进度百分比 |

### P1 — 核心功能扩展

| 编号 | 功能 | 命令 | 说明 |
|---|---|---|---|
| P1-1 | 批量下载 | `/jm id1,id2,id3` | 一次下载多本，失败单独提示 |
| P1-2 | 单章节下载 | `/jmphoto <photo_id>` | 多章长篇只下最新一话 |
| P1-3 | 评论翻页+回复 | `/jmc <id> [页码]` | 展示回复楼、剧透标记、点赞数、时间 |
| P1-4 | 收藏夹文件夹分类 | `/jmfav` → 列文件夹 | 按文件夹浏览收藏 |
| P1-5 | 添加/删除收藏 | `/jmfav add <id>` | API 客户端 toggle_favorite_album |

### P2 — 体验增强

| 编号 | 功能 | 命令 | 说明 |
|---|---|---|---|
| P2-1 | 长图导出 | `/jmlong <id>` | 所有图片竖向拼接成 PNG，手机直接看 |
| P2-2 | 每日签到 | `/jmcheckin` | 签到领金币经验，可配置定时自动签到 |
| P2-3 | 用户面板 | `/jmme` | 金币、等级、经验、收藏数/上限 |
| P2-4 | 详情增强 | `/jmv` | 加简介、发布/更新日期、喜欢数、观看数、相关推荐 |

### P3 — 进阶功能（后续迭代）

| 编号 | 功能 | 说明 |
|---|---|---|
| P3-1 | 订阅更新推送 | `/jmsub <id>` 订阅，有新章节 QQ 提醒 |
| P3-2 | 分类筛选 | `/jmcat doujin chinese` 按分类浏览 |
| P3-3 | 搜索高级语法 | 支持 `+tag` `-tag` 包含/排除语法 |
| P3-4 | 加密导出 | `/jm <id> -p 密码` ZIP/PDF 加密 |
| P3-5 | 下载取消 | 下载中 `/jmcancel` 中断任务 |
| P3-6 | 全站评论广场 | `/jmforum` 随机看全站评论 |
| P3-7 | 发表评论 | `/jmcomment <id> 内容` |

---

## 四、技术方案详解

### 4.1 API 客户端切换

```python
# 修改前
option_dict = {
    "client": {
        "impl": "html",
        ...
    }
}

# 修改后
option_dict = {
    "client": {
        "impl": "api",
        "retry_times": 5,
        "cache": True,
        "domain": {
            "api": custom_domains  # API 客户端用 api 域名配置
        },
        ...
    }
}
```

**注意事项：**
- API 客户端需要登录 cookies，未登录时部分接口受限
- API 客户端不支持 `category`/`sub_category` 参数（分类筛选功能需延后或用 HTML 客户端做）
- 域名配置 key 从 `html` 改为 `api`
- API 客户端会自动从 API 服务器拉取最新域名列表

### 4.2 异常细化处理

```python
from jmcomic import (
    MissingAlbumPhotoException,
    RequestRetryAllFailException,
    PartialDownloadFailedException,
    DownloadCancelledException,
)

try:
    result = await jmcomic.download_album_async(album_id, self.option)
except MissingAlbumPhotoException:
    yield event.plain_result("❌ 查无此本，请检查 ID 是否正确")
except RequestRetryAllFailException as e:
    yield event.plain_result(f"❌ 网络请求全部失败，可尝试在配置中更换域名\n详情: {e}")
except PartialDownloadFailedException:
    yield event.plain_result("⚠️ 部分图片下载失败，已发送已完成部分")
except DownloadCancelledException:
    yield event.plain_result("⏹️ 下载已取消")
except Exception as e:
    yield event.plain_result(f"❌ 未知错误：{e}")
```

### 4.3 下载进度推送

基于 jmcomic 的 `DownloadCallback` 机制，自定义下载器类：

```python
from jmcomic import JmDownloader

class ProgressDownloader(JmDownloader):
    def __init__(self, option, progress_callback=None):
        super().__init__(option)
        self.progress_callback = progress_callback
        self.total_images = 0
        self.downloaded_images = 0

    def before_album(self, album):
        super().before_album(album)
        self.total_images = album.page_count
        self.downloaded_images = 0

    def after_image(self, image, img_save_path):
        super().after_image(image, img_save_path)
        self.downloaded_images += 1
        if self.progress_callback and self.downloaded_images % 10 == 0:
            pct = int(self.downloaded_images / self.total_images * 100)
            self.progress_callback(pct, self.downloaded_images, self.total_images)
```

QQ 端实现：下载开始后记录消息 ID，每 10 张图编辑一次进度消息，或每隔固定间隔推送。

### 4.4 批量下载

```python
# jmcomic 原生支持传入可迭代对象
result = await jmcomic.download_album_async([id1, id2, id3], option)
# result 是 BatchResult，可遍历成功项，失败项在 result.failed
for album_id, error in result.failed.items():
    # 单独报告失败
```

### 4.5 单章节下载

```python
# 先查专辑详情获取章节列表
album = client.get_album_detail(album_id)
for photo in album:
    # photo.photo_id, photo.name, photo.album_index
    pass

# 下载单章节
result = await jmcomic.download_photo_async(photo_id, option)
```

### 4.6 评论翻页

```python
# API 客户端支持翻页
page = client.album_pagination(album_id, page=page_num)
# page.total — 主评论总数
# page.page_count — 总页数
# page.comment_count — 当前页主评论+回复总数
for comment in page:
    # comment.nickname, comment.content, comment.is_spoiler
    # comment.likes, comment.created_at
    for reply in comment.replies:
        # 嵌套回复
```

### 4.7 收藏夹操作

```python
# 列出收藏夹文件夹
page = client.favorite_folder(page=1)
folders = list(page.iter_folder_id_name())  # [(fid, fname), ...]

# 查看指定文件夹
page = client.favorite_folder(page=1, folder_id=fid)

# 添加/删除收藏（API 客户端）
client.toggle_favorite_album(album_id)  # 切换收藏状态
# 或显式调用
client.add_favorite_album(album_id, folder_id='0')
client.delete_favorite_album(album_id, folder_id='0')
```

### 4.8 长图导出

```python
from jmcomic import Feature
# 下载时挂载长图导出 Feature
result = await jmcomic.download_album_async(
    album_id, option,
    extra=Feature.export_long_img
)
# 长图路径在 manifest 中
```

或直接复用 jmcomic 的 `LongImgPlugin` 逻辑，用 PIL 拼接。

### 4.9 每日签到

```python
# 获取签到信息
daily = client.get_daily()
# API 客户端: daily.model_data → daily_id, event_name, record(签到日历矩阵)

# 执行签到
resp = client.daily_checkin()
# resp.code == 0 → 签到成功，resp.msg 包含奖励
# resp.code == 1 → 今日已签
```

### 4.10 用户面板

```python
# API 客户端登录后可获取
# login() 返回: uid, username, coin, level, exp, expPercent, album_favorites, album_favorites_max
# 或通过 client.setting() 获取
```

---

## 五、分阶段实施计划

### 阶段一：P0 架构升级（v0.6.0-alpha）

1. 切换 API 客户端，验证搜索/详情/下载/排行榜等基础功能正常
2. 细化所有命令的异常处理
3. 实现下载进度推送
4. 回归测试所有现有命令

**验收标准：** 所有现有功能在 API 客户端下正常工作；下载时有进度反馈；错误提示对用户友好。

### 阶段二：P1 核心功能（v0.6.0-beta）

1. 批量下载（逗号分隔多 ID）
2. 单章节下载 + 详情页章节列表
3. 评论翻页 + 回复展示
4. 收藏夹文件夹分类浏览
5. 添加/删除收藏

**验收标准：** 5个新命令全部可用；收藏添加功能恢复（之前因 HTML 客户端报错已放弃）。

### 阶段三：P2 体验增强（v0.6.0 正式版）

1. 长图导出
2. 每日签到
3. 用户面板
4. 详情页信息补全（简介/日期/喜欢数/相关推荐）
5. 更新帮助文本、CHANGELOG、版本号

**验收标准：** 4个新功能可用；详情页信息更丰富；版本号升至 v0.6.0。

---

## 六、风险与注意事项

### 6.1 API 客户端风险

1. **登录要求** — API 客户端部分接口需要登录 cookies，未配置账号时功能可能受限
   - 应对：未登录时降级提示，搜索/排行榜/详情等公开接口不受影响

2. **域名配置变化** — API 客户端域名配置 key 不同
   - 应对：配置项兼容处理，同时支持 `domain` 字符串自动适配

3. **分类筛选不可用** — API 客户端不支持 category/sub_category
   - 应对：P3 分类筛选功能暂不做，或保留 HTML 客户端做分类专用

### 6.2 下载进度风险

1. **QQ 消息编辑限制** — 频繁编辑消息可能触发限流
   - 应对：每 10 张图或每 5 秒更新一次，大文件才启用进度

2. **多章节计数** — 多章 album 的总图片数需要累加
   - 应对：`before_album` 时遍历所有 photo 累加 page_arr 长度

### 6.3 批量下载风险

1. **QQ 文件发送限制** — 一次性发送多个大文件可能超时
   - 应对：逐个发送，每本之间留间隔

2. **缓存冲突** — 多本同时下载可能缓存目录冲突
   - 应对：jmcomic 内部按 album_id 隔离目录，无冲突

### 6.4 签到风险

1. **账号安全** — 自动签到需要保存账号密码
   - 应对：仅在用户配置了账号时启用，不硬编码

2. **签到频率** — 每日只能签一次，重复签到返回 code=1
   - 应对：检查返回码，已签时提示"今日已签到"

---

## 七、测试验证方法

### 7.1 单元验证清单

| 命令 | 验证点 |
|---|---|
| `/jms 关键词` | 搜索结果正常、简繁转换正常、敏感词过滤正常 |
| `/jmv <id>` | 详情字段完整、封面发送正常、新增字段显示 |
| `/jm <id>` | 下载成功、ZIP 发送、进度推送、历史记录 |
| `/jmpdf <id>` | PDF 生成发送正常 |
| `/jmrank day/week/month` | 排行榜正常 |
| `/jmrand` | 随机抽漫正常 |
| `/jmc <id> [页码]` | 评论翻页、回复展示、剧透标记 |
| `/jmfav` | 文件夹列表、文件夹内容、添加收藏 |
| `/jmphoto <id>` | 单章节下载 |
| `/jmlong <id>` | 长图生成发送 |
| `/jmcheckin` | 签到成功/已签提示 |
| `/jmme` | 用户信息展示 |

### 7.2 异常场景验证

- 错误 ID → 提示"查无此本"
- 断网 → 提示"网络异常，可换域名"
- 部分图片失败 → 提示"部分失败，已发送完成部分"
- 未登录使用收藏 → 提示"请先配置账号"

### 7.3 回归验证

- 群聊权限开关正常
- 敏感词转拼音正常
- 简繁转换正常
- 下载缓存复用正常
- 自动清理正常
- 调侃提醒正常

---

## 八、版本规划

| 版本 | 内容 | 状态 |
|---|---|---|
| v0.5.2 | 当前版本，基础功能完善 | 已发布 |
| v0.6.0-alpha | P0 架构升级（API客户端+异常细化+进度推送） | 开发中 |
| v0.6.0-beta | P1 核心功能（批量/单章/评论翻页/收藏） | 待开发 |
| v0.6.0 | P2 体验增强（长图/签到/用户面板/详情增强） | 待开发 |
| v0.7.0 | P3 进阶功能（订阅/分类/加密/取消等） | 规划中 |

---

## 九、文件修改清单

| 文件 | 修改内容 |
|---|---|
| `main.py` | 核心逻辑：API客户端切换、异常处理、进度推送、所有新命令 |
| `metadata.yaml` | 版本号升级 |
| `_conf_schema.json` | 新增配置项（签到开关、进度推送开关等） |
| `CHANGELOG.md` | 更新日志 |
| `README.md` | 命令帮助更新 |
| `requirements.txt` | 如有新依赖则补充 |

---

*文档生成时间：基于 jmcomic 源码库深度分析*
*分析覆盖：jm_entity / jm_client_interface / jm_client_impl / jm_plugin / jm_feature / jm_downloader / jm_option / jm_config / jm_runtime / jm_exception / jm_toolkit / api / jm_async_client / jm_async_downloader*
