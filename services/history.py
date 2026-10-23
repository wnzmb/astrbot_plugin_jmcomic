"""下载历史：history.json 读写与按用户裁剪。"""

import json


class HistoryService:
    def __init__(self, settings):
        self.settings = settings

    def load(self):
        """加载下载历史，结构是 {user_id: [历史列表]}"""
        try:
            data = json.loads(self.settings.history_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
            else:
                return {}
        except Exception:
            return {}

    def save(self, data):
        """保存下载历史"""
        self.settings.history_file.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def get_user(self, user_id):
        data = self.load()
        return data.get(user_id, [])

    def add_user(self, user_id, item):
        data = self.load()
        if user_id not in data:
            data[user_id] = []
        data[user_id].insert(0, item)
        # history_max 为 0 表示保留全部历史
        if self.settings.history_max > 0:
            data[user_id] = data[user_id][:self.settings.history_max]
        self.save(data)
        return len(data[user_id])
