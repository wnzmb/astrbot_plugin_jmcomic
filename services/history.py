"""下载历史：history.json 读写与按用户裁剪。"""

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
