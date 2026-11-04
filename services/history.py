"""下载历史：history.json 读写与按用户裁剪。"""

import asyncio
import json
import os
import threading


class HistoryService:
    def __init__(self, settings):
        self.settings = settings
        self._lock = threading.RLock()

    def load(self):
        """加载下载历史，结构是 {user_id: [历史列表]}"""
        with self._lock:
            try:
                data = json.loads(self.settings.history_file.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
                else:
                    return {}
            except Exception:
                return {}

    def save(self, data):
        """保存下载历史（tmp + os.replace，避免写一半损坏）"""
        with self._lock:
            path = self.settings.history_file
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            os.replace(tmp, path)

    def get_user(self, user_id):
        data = self.load()
        return data.get(user_id, [])

    def add_user(self, user_id, item):
        with self._lock:
            data = self.load()
            if user_id not in data:
                data[user_id] = []
            data[user_id].insert(0, item)
            # history_max 为 0 表示保留全部历史
            if self.settings.history_max > 0:
                data[user_id] = data[user_id][:self.settings.history_max]
            self.save(data)
            return len(data[user_id])

    def clear_user(self, user_id):
        """删除某用户的历史。

        load + del + save 必须在同一把锁内完成，否则可能与 ``add_user``
        并发时把刚写入的一条历史覆盖掉。
        """
        with self._lock:
            data = self.load()
            if user_id not in data:
                return False
            del data[user_id]
            self.save(data)
            return True

    # ========== asyncio 包装 ==========
    # history.json 可能随着用户数增长到几 MB，全量读写不能跑在事件循环里。
    # RLock 在转线程后仍然需要，用于串行化并发写。
    async def load_async(self):
        return await asyncio.to_thread(self.load)

    async def save_async(self, data):
        await asyncio.to_thread(self.save, data)

    async def get_user_async(self, user_id):
        return await asyncio.to_thread(self.get_user, user_id)

    async def add_user_async(self, user_id, item):
        return await asyncio.to_thread(self.add_user, user_id, item)

    async def clear_user_async(self, user_id):
        return await asyncio.to_thread(self.clear_user, user_id)
