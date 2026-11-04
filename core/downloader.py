"""jmcomic 异步下载器适配：带进度跟踪的 ProgressDownloader。"""

from contextvars import ContextVar
from pathlib import Path

from astrbot.api import logger

from . import jmcompat


# 单章下载页数上限：设置后只下载 1..N 页，保证缓存仍是连续前缀。
# 页段请求（-c 5:201-400）只需要前 400 页，没必要拉整话（BUG-030）。
_PAGE_LIMIT: ContextVar = ContextVar("jmcomic_page_limit", default=None)


def _should_skip_image(image, limit):
    """判断图片是否超出当前页数上限；无法判断页码时不跳过。"""
    if not limit:
        return False
    try:
        index = int(getattr(image, "index", -1))
        limit = int(limit)
    except (TypeError, ValueError):
        return False
    return index > limit


class page_limit:
    """临时限制单章下载页数。

    只下载前 N 页而不是「第 start-end 页」，是为了让缓存始终是 1..N 的连续
    前缀：图片名 00001..0000N 与页码一一对应，打包时按 `start-1:end` 取片
    不会错位。同一章后续请求更大的页码时，jmcomic 会复用已有图片、只补差额。
    """

    def __init__(self, max_page):
        self.max_page = max_page
        self._token = None

    def __enter__(self):
        self._token = _PAGE_LIMIT.set(self.max_page)
        return self

    def __exit__(self, exc_type, exc, tb):
        _PAGE_LIMIT.reset(self._token)
        return False



# 库内置 advanced_retry 的请求级域名轮询配置：
# 最多轮询 3 轮，单个域名失败 3 次后暂时跳过；按历史失败次数排序，失败多的后置。
# 整本下载仍由 download_album_progress 保留 3 次外层任务重试，二者不冲突。
ADVANCED_RETRY_CONFIG = {
    "retry_rounds": 3,
    "retry_domain_max_times": 3,
}


class _ProgressHooks:
    """下载器钩子实现，由 ProgressDownloader.current_impl() 混入当前代基类。"""

    async def _super_hook(self, name, *args):
        """调用上游同名钩子。

        上游改钩子名或签名时只记警告并跳过，让下载继续，避免「进度功能
        失效」升级成「整本下载失败」。
        """
        hook = getattr(super(), name, None)
        if hook is None:
            logger.warning(f"jmcomic 缺少下载钩子 {name}，进度统计可能不准")
            return None
        try:
            return await hook(*args)
        except TypeError as exc:
            logger.warning(f"jmcomic 钩子 {name} 签名已变化，已跳过: {exc}")
            return None

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
        await self._super_hook("before_album", album)
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
        await self._super_hook("before_photo", photo)
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

    async def before_image(self, image, img_save_path):
        # 页段请求只下载前缀页，超出上限的图片直接跳过（不产生网络请求）
        if _should_skip_image(image, _PAGE_LIMIT.get()):
            image.skip = True
        await self._super_hook("before_image", image, img_save_path)

    async def after_image(self, image, img_save_path):
        await self._super_hook("after_image", image, img_save_path)
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
        await self._super_hook("after_album", album)
        aid = str(album.album_id)
        state = ProgressDownloader._progress.get(aid)
        if state is not None:
            state['finished'] = True
            if state['total'] > 0:
                state['done'] = state['total']


class ProgressDownloader:
    """进度存储 + 当前代下载器类工厂。

    AstrBot 重载 jmcomic 后，旧的 ``JmAsyncDownloader`` 子类与新基类不属于
    同一“代”，传给 ``download_album_async`` 会触发跨代类不匹配（BUG-027）。
    因此按代缓存子类，调用方用 ``current_impl()`` 取实际下载器类。
    """

    # key: album_id, value: 进度状态
    _progress = {}
    # key: album_id, value: 本次下载实际写入的漫画根目录（不依赖 dir_rule 命名）
    _album_dirs = {}

    _impl = None
    _impl_generation = None

    @classmethod
    def current_impl(cls):
        """返回继承当前代 JmAsyncDownloader 的下载器类。"""
        generation = jmcompat.generation()
        if cls._impl is None or cls._impl_generation != generation:
            base = jmcompat.attr(
                "JmAsyncDownloader", module_name="jmcomic.jm_async_downloader"
            )
            if not isinstance(base, type):
                raise RuntimeError("未找到 jmcomic 异步下载器，请检查 jmcomic 安装")
            cls._impl = type("JmProgressDownloader", (_ProgressHooks, base), {})
            cls._impl_generation = generation
        return cls._impl

    # 兼容直接调用 ProgressDownloader._album_page_count(album)
    @classmethod
    def _album_page_count(cls, album):
        return _ProgressHooks._album_page_count(album)
