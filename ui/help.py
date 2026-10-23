"""`/jm help` 帮助文案。"""


def build_help_text(settings, quota):
    return (
        "📖 漫画站插件 帮助 v0.7.8\n"
        "━━━━━━━━━━━━━━━\n"
        "🔍 搜索：\n"
        "  /jms 关键词 [页码] [排序]   全站搜索\n"
        "  /jma 作者名 [页码] [排序]   按作者搜索\n"
        "  /jmt 标签名 [页码] [排序]   按标签搜索\n"
        "  /jmr 角色名 [页码] [排序]   按角色搜索\n"
        "  /jmw 原作名 [页码] [排序]   按原作搜索\n"
        "━━━━━━━━━━━━━━━\n"
        "📖 详情：\n"
        "  /jmv <漫画id> [选项]       查详情(含话列表/标签/人物/作品)\n"
        "    选项：-author -work -role -tag -pic(不发封面)\n"
        "  /jmlist <漫画id> [页码]    查看全部话（纯文本，不发封面）\n"
        "  /jmc <漫画id> [页码]        看评论(翻页/回复/剧透，超页自动提示)\n"
        "━━━━━━━━━━━━━━━\n"
        "⬇️ 下载：\n"
        "  /jm <漫画id>                下载整本，PDF按后台页数上限自动分册\n"
        "  /jm <章节id>                自动识别为单话，发一个PDF\n"
        "  /jm <章节id> -c 1:201-      章节ID页段续传（话序号固定写1）\n"
        "  /jm id1,id2,id3             批量下载，支持漫画/章节ID混合\n"
        "  /jm <id> -c 31              只下第31话\n"
        "  /jm <id> -c 31-40           下第31到40话（范围倒置会自动纠正并提示）\n"
        "  /jm <id> -c 31,35,40        下指定话\n"
        "  /jm <id> -c 5:201-          从第5话第201页继续（页段续传）\n"
        "  /jm <id> -c 5:201-400       只下第5话第201-400页\n"
        "  /jmzip <漫画id>             下载整本，按后台话数自动合并ZIP\n"
        "  /jmzip <章节id>             自动识别为单话，发一个ZIP\n"
        "  /jmzip id1,id2,id3          批量下载，支持漫画/章节ID混合\n"
        "  /jmzip <id> -c 31-40        下第31到40话\n"
        "  /jmzip <id> -c 31,35,40     下指定话\n"
        "  /jmphoto <id>               等同 /jm <id>（兼容旧命令）\n"
        "  /jmnow [-all]               查看自己下载进度（-all 管理员看全部）\n"
        "  /jmstop [-all]              停止自己的下载（-all 管理员停全部）\n"
        "━━━━━━━━━━━━━━━\n"
        "⭐ 账号（仅号主）：\n"
        "  /jmcheckin                  每日签到领金币经验\n"
        "  /jmme                       用户信息面板(签到/收藏/进度)\n"
        "  /jmfav                      收藏夹文件夹列表\n"
        "  /jmfav <文件夹id>           查看指定收藏夹\n"
        "  /jmfav add <漫画id>         添加收藏\n"
        "  /jmfav del <漫画id>         移除收藏\n"
        "  /jmfav export [文件夹id]    导出收藏夹CSV（30秒内再输一次确认）\n"
        "━━━━━━━━━━━━━━━\n"
        "🔔 订阅：\n"
        "  /jmsub <漫画id>             订阅漫画更新\n"
        "  /jmsub list                 查看当前会话订阅\n"
        "  /jmsub del <漫画id>         取消订阅\n"
        "━━━━━━━━━━━━━━━\n"
        "📊 其他：\n"
        "  /jmwhoami                   查看自己的ID（配置号主用，所有人可用）\n"
        "  /jmrank day/week/month [页码]  排行榜\n"
        "  /jmhis [页码]               下载历史（每页{per_page}条）\n"
        "    用法：/jmhis clear 清空历史 | /jmhis -all [页码] 查看全部成员(管理员，每用户每页10条)\n"
        "  /jmdomain                   查看可用域名\n"
        "  /jmupdate                   查看更新日志\n"
        "  /jmrand [关键词]            随机抽一个\n"
        "  /jmnext                     翻到最近一次列表的下一页\n"
        "    也可在 {page_turn_timeout} 秒内直接发送“下一页/下页”\n"
        "━━━━━━━━━━━━━━━\n"
        "📝 排序参数：mr(最新) mv(最多浏览) mp(最多页数) tf(最多收藏) tr(评分最高) md(最多评论)\n"
        "💡 搜索小提示：建议只打前几个关键词，打全容易因简繁差异漏结果\n"
        "💾 下载缓存：JM{{漫画id}}/P{{章节id}}_第N话/图片，标题存入 metadata.json，启动时清理 {cache_days} 天未访问的缓存目录（0=永久保留）\n"
        "📎 打包信息：ZIP 附漫画信息.txt；PDF 写入标题属性，每册都加漫画信息页\n"
        "📄 PDF：每 {pdf_page_limit} 页一册（整话优先，单话超限按页切分）\n"
        "🗜️ ZIP：最多 {zip_group_size} 话/册，{zip_page_text}\n"
        "⚙️ 当前客户端：{client_type}（插件配置可切换 api/html）\n"
        "📦 需 jmcomic 2.7.9+（requirements 会自动安装/升级）\n"
        "⏳ 文件限额：每日{daily_limit_text}、每小时{hourly_limit_text}（0=不限）".format(
            client_type=settings.client_type.upper(),
            per_page=settings.per_page,
            page_turn_timeout=settings.page_turn_timeout,
            cache_days=settings.cache_days,
            pdf_page_limit=settings.pdf_page_limit,
            zip_group_size=settings.zip_group_size,
            zip_page_text=settings.zip_page_hint(),
            daily_limit_text=quota.limit_text(settings.daily_file_limit),
            hourly_limit_text=quota.limit_text(settings.hourly_file_limit),
        )
    )
