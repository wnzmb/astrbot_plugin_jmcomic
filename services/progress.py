"""当前下载任务登记：供 /jmnow 与 /jmstop 查询/清理。"""

import time


class ActiveDownloadService:
    def __init__(self, state):
        self.state = state

    def set_active(self, user_id, task, target=""):
        self.state.active_downloads[str(user_id)] = {
            "task": task,
            "album_id": str(target or ""),
            "total": 0,
            "done": 0,
            "stage": "downloading",
            "updated": time.monotonic(),
        }

    def update(self, user_id, **kwargs):
        state = self.state.active_downloads.get(str(user_id))
        if not state:
            return
        state.update(kwargs)
        state["updated"] = time.monotonic()

    def clear(self, user_id, task=None):
        user_id = str(user_id)
        state = self.state.active_downloads.get(user_id)
        if state and (task is None or state.get("task") is task):
            self.state.active_downloads.pop(user_id, None)

    def get_active(self, user_id):
        state = self.state.active_downloads.get(str(user_id))
        if not state:
            return None
        task = state.get("task")
        if task is not None:
            try:
                if task.done():
                    self.state.active_downloads.pop(str(user_id), None)
                    return None
            except Exception:
                pass
        return state
