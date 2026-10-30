"""jmcomic 运行时兼容层：始终取“当前代”的模块与类，避免跨代类不匹配。

AstrBot 桌面版加载插件依赖时，会把 ``jmcomic`` 从 ``sys.modules`` 弹出，
再从 ``data/site-packages`` 重新执行（见
``astrbot.core.utils.pip_installer._prefer_module_from_site_packages``）。
插件在 import 时绑定的模块、类会停留在旧“代”，而 jmcomic 内部的延迟导入
（例如 ``JmModuleConfig.album_class()`` 里的 ``from .jm_entity import ...``）
却解析到新“代”，于是出现：

- ``不支持的类型: <class 'jmcomic.jm_entity.JmAlbumDetail'>``
  （``issubclass(新代类, 旧代类)`` 为 False）
- 异常类型跨代不匹配，日志出现
  ``Can't pickle ... it's not the same object as ...``

因此插件侧不要在模块顶层缓存 jmcomic 的类，统一在调用时通过本模块取。
"""

import importlib
import sys


def module(name="jmcomic"):
    """返回 ``sys.modules`` 中的 jmcomic（或指定子模块），不存在时尝试导入。"""
    loaded = sys.modules.get(name)
    if loaded is not None:
        return loaded
    try:
        return importlib.import_module(name)
    except Exception:
        return None


def generation():
    """jmcomic 顶层的代际标识；模块被重载成另一份后取值会变化。"""
    return id(sys.modules.get("jmcomic"))


def attr(name, default=None, module_name="jmcomic"):
    """取当前代模块里的属性，模块或属性缺失时返回 default。"""
    target = module(module_name)
    if target is None:
        return default
    return getattr(target, name, default)


def exceptions(*names):
    """返回当前代的异常类元组，可直接用于 ``except``；缺失的类会被忽略。

    全部缺失时返回空元组，``except ()`` 不会捕获任何异常，行为安全。
    """
    exc_module = module("jmcomic.jm_exception")
    if exc_module is None:
        return ()
    return tuple(
        cls
        for cls in (getattr(exc_module, name, None) for name in names)
        if isinstance(cls, type)
    )


def is_exception(error, name):
    """判断异常是否属于当前代的指定 jmcomic 异常类。"""
    exc_module = module("jmcomic.jm_exception")
    if exc_module is None:
        return False
    cls = getattr(exc_module, name, None)
    return isinstance(cls, type) and isinstance(error, cls)
