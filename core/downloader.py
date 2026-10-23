"""jmcomic 异步下载器适配：带进度跟踪的 ProgressDownloader。"""

from pathlib import Path

from jmcomic.jm_async_downloader import JmAsyncDownloader


# 库内置 advanced_retry 的请求级域名轮询配置：
# 最多轮询 3 轮，单个域名失败 3 次后暂时跳过；按历史失败次数排序，失败多的后置。
# 整本下载仍由 download_album_progress 保留 3 次外层任务重试，二者不冲突。
ADVANCED_RETRY_CONFIG = {
    "retry_rounds": 3,
    "retry_domain_max_times": 3,
}


class ProgressDownloader(JmAsyncDownloader):
    """带进度跟踪的下载器，进度存在类变量中按 album_id 隔离
    适配 jmcomic 2.7.9+ 的异步下载器架构（async 上下文管理器 + 异步钩子）
    """

    # key: album_id, value: 进度状态
    _progress = {}
    # key: album_id, value: 本次下载实际写入的漫画根目录（不依赖 dir_rule 命名）
    _album_dirs = {}

    @staticmethod
    def _album_page_count(album):
        """优先使用 album.page_count；旧的逐章累加在 before_album 时 page_arr 尚未拉取。"""
        try:
            total = int(getattr(album, "page_count", 0) or 0)
        except (TypeError, ValueError):
            total = 0
        if total > 0:
            return total
        # 兜底：若调用方已经预取过 page_arr，仍可累加出总数
        for photo in album:
            total += len(getattr(photo, "page_arr", None) or [])
        return max(0, total)

    async def before_album(self, album):
        await super().before_album(album)
        try:
            photo_total = len(album)
        except Exception:
            photo_total = 0
        ProgressDownloader._progress[str(album.album_id)] = {
            'total': self._album_page_count(album),
            'done': 0,
            'finished': False,
            'photo_total': photo_total,
            'photo_done': 0,
            'current_photo': 0,
            'current_name': '',
        }

    async def before_photo(self, photo):
        await super().before_photo(photo)
        try:
            state = ProgressDownloader._progress.get(str(photo.album_id))
            if state is None:
                return
            state['photo_done'] += 1
            try:
                state['current_photo'] = int(getattr(photo, 'index', 0) or state['photo_done'])
            except (TypeError, ValueError):
                state['current_photo'] = state['photo_done']
            state['current_name'] = str(getattr(photo, 'name', '') or '')
            # album.page_count 不可用时，至少用已预取章节补一个可见的进度分母
            if state['total'] <= 0:
                state['total'] += len(getattr(photo, 'page_arr', None) or [])
        except Exception:
            pass

    async def after_image(self, image, img_save_path):
        await super().after_image(image, img_save_path)
        try:
            album_id = str(image.from_photo.album_id)
            state = ProgressDownloader._progress.get(album_id)
            if state is not None:
                state['done'] += 1
                if state['total'] > 0:
                    state['done'] = min(state['done'], state['total'])

            # 记录实际保存目录：取 base_dir 下的第一层目录，无论 dir_rule 怎么配都能定位
            base_dir = Path(self.option.dir_rule.base_dir).resolve()
            rel_path = Path(img_save_path).resolve().relative_to(base_dir)
            if rel_path.parts:
                ProgressDownloader._album_dirs[album_id] = str(base_dir / rel_path.parts[0])
        except Exception:
            pass

    async def after_album(self, album):
        await super().after_album(album)
        aid = str(album.album_id)
        state = ProgressDownloader._progress.get(aid)
        if state is not None:
            state['finished'] = True
            if state['total'] > 0:
                state['done'] = state['total']
