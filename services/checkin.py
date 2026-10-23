"""签到：日期解析、本机签到状态、连续天数估算、签到接口 payload 解析。"""

import json
import re
from datetime import datetime

from astrbot.api import logger
from jmcomic import JmCryptoTool, JmDailyCheckinResp


class CheckinService:
    def __init__(self, settings):
        self.settings = settings

    # ========== 签到接口兼容 ==========
    @staticmethod
    def _decode_checkin_data(data, ts):
        """解析 /daily_chk 的 data。

        jmcomic 2.7.9 默认把 data 当 base64 字符串；实测首次签到成功时
        服务端可能返回 list，导致 b64decode(list) 抛 TypeError。这里同时
        兼容字符串、list 和已经是 dict 的响应。
        """
        if isinstance(data, dict):
            return data
        if isinstance(data, list):
            if not data:
                return {}
            # 可能是被拆成多段的 base64，也可能是明文消息列表
            candidates = []
            if all(isinstance(item, str) for item in data):
                candidates.append("".join(data))
                candidates.extend(data)
            for candidate in candidates:
                try:
                    text = JmCryptoTool.decode_resp_data(candidate, ts)
                    parsed = json.loads(text)
                except Exception:
                    continue
                if isinstance(parsed, dict):
                    return parsed
            messages = []
            for item in data:
                if isinstance(item, dict):
                    messages.append(str(item.get("msg") or item))
                else:
                    messages.append(str(item))
            return {"msg": "；".join(messages), "status": "ok"}
        if isinstance(data, str):
            try:
                text = JmCryptoTool.decode_resp_data(data, ts)
            except Exception:
                return {"msg": data}
            try:
                parsed = json.loads(text)
            except Exception:
                return {"msg": text}
            return parsed if isinstance(parsed, dict) else {"msg": text}
        return {}

    def daily_checkin(self, client, daily_id=None):
        """执行签到，兼容 jmcomic 2.7.9 的 list 响应。

        返回 JmDailyCheckinResp：code=0 成功，code=1 今日已签。
        其它失败直接抛异常，由上层转成用户提示。
        """
        # HTML 客户端没有 req_api/API_DAILY_CHK，直接复用库实现
        if not hasattr(client, "API_DAILY_CHK") or not hasattr(client, "req_api"):
            return client.daily_checkin()

        user_id = getattr(client, "_user_id", None)
        if not user_id:
            raise RuntimeError("签到需要先登录，请检查插件配置中的账号密码")

        if daily_id is None:
            daily = client.get_daily(user_id)
            daily_id = self.daily_payload(daily).get("daily_id")
        if not daily_id and hasattr(client, "get_or_fetch_daily_id"):
            daily_id = client.get_or_fetch_daily_id()
        if not daily_id:
            raise RuntimeError("签到失败：未能获取 daily_id")

        resp = client.req_api(
            client.API_DAILY_CHK,
            get=False,
            data={"user_id": user_id, "daily_id": daily_id},
        )
        payload = resp.json()
        parsed = self._decode_checkin_data(payload.get("data"), resp.ts)
        msg = str(parsed.get("msg") or payload.get("errorMsg") or "")
        status = parsed.get("status")

        already_keywords = (
            "今天已經簽到過了", "今天已经签到过了", "已簽到", "已签到",
            "簽到過", "签到过", "已完成", "已打卡",
        )
        success_keywords = ("Jcoin", "EXP", "成功", "签到成功", "簽到成功")

        if any(keyword in msg for keyword in already_keywords):
            code = JmDailyCheckinResp.CODE_ALREADY_CHECKED_IN
        elif (
            any(keyword in msg for keyword in success_keywords)
            or status == "ok"
            or (isinstance(payload.get("data"), list) and bool(payload.get("data")))
        ):
            code = JmDailyCheckinResp.CODE_SUCCESS
        else:
            raise RuntimeError(f"签到失败：{msg or payload}")

        return JmDailyCheckinResp(resp.resp, code, msg, parsed)

    # ========== 签到信息解析 ==========
    @staticmethod
    def parse_record_day(value):
        """把签到记录里的日期（"01" / 1 / "2026-10-01"）统一成几号"""
        if value is None or isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value if 1 <= value <= 31 else None
        text = str(value).strip()
        if not text:
            return None
        if '-' in text or '/' in text:
            text = re.split(r'[-/]', text)[-1]
        digits = re.sub(r'\D', '', text)
        if not digits:
            return None
        day = int(digits)
        return day if 1 <= day <= 31 else None

    def load_state(self):
        """读取本机签到状态，接口日历延迟时用于兜底"""
        try:
            if self.settings.checkin_file.exists():
                data = json.loads(self.settings.checkin_file.read_text(encoding="utf-8"))
                return data if isinstance(data, dict) else {}
        except Exception:
            pass
        return {}

    def save_state(self, streak, signed_count=None):
        """保存本机签到状态"""
        try:
            data = {
                "date": datetime.now().strftime("%Y-%m-%d"),
                "streak": int(streak or 0),
            }
            if signed_count is not None:
                data["signed_count"] = signed_count
            self.settings.checkin_file.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"保存签到状态失败: {e}")

    def local_streak(self, today_signed):
        """接口未返回连续天数时，用本机记录估算"""
        state = self.load_state()
        streak = int(state.get("streak", 0) or 0)
        if today_signed:
            return max(1, streak)
        return max(0, streak)

    @staticmethod
    def daily_payload(daily):
        """取出签到接口的字典数据，兼容 API（model_data）与 HTML（json()）"""
        try:
            if hasattr(daily, 'model_data'):
                data = daily.model_data
            elif hasattr(daily, 'json'):
                data = daily.json()
            else:
                data = None
        except Exception:
            data = None
        # jmcomic 的 AdvancedDict 不是 dict 子类，必须取 src_dict 后才能当字典用
        if hasattr(data, 'src_dict'):
            data = data.src_dict
        return data if isinstance(data, dict) else {}

    def parse_daily_info(self, daily, assume_today_signed=False):
        """解析签到信息。
        HTML 客户端有 oldStep / error 字段，API 客户端没有，
        连续天数只能从 record 签到日历里按日期往前逐日统计。
        """
        data = self.daily_payload(daily)
        info = {
            'streak': None,
            'today_signed': False,
            'signed_count': None,
            'signed_days': set(),
            'progress': None,
        }
        if not data:
            return info

        progress = data.get('currentProgress') or data.get('current_progress')
        if progress is not None:
            info['progress'] = str(progress)

        # HTML 客户端直接给连续天数
        for key in ('oldStep', 'old_step', 'continuous', 'continuous_days', 'streak'):
            value = data.get(key)
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)):
                info['streak'] = int(value)
                break
            if isinstance(value, str) and value.strip().isdigit():
                info['streak'] = int(value.strip())
                break

        # 今日是否已签
        if data.get('error') == 'finished' or data.get('today_signed') is True:
            info['today_signed'] = True

        # 签到日历：API 是 record 二维数组，HTML 是 dateArray
        record = data.get('record')
        if hasattr(record, 'src_dict'):
            record = record.src_dict
        if isinstance(record, dict):
            record = list(record.values())
        if isinstance(record, list):
            for week in record:
                if hasattr(week, 'src_dict'):
                    week = week.src_dict
                days = week if isinstance(week, list) else [week]
                for day in days:
                    if hasattr(day, 'src_dict'):
                        day = day.src_dict
                    if not isinstance(day, dict) or not day.get('signed'):
                        continue
                    day_num = self.parse_record_day(day.get('date'))
                    if day_num is not None:
                        info['signed_days'].add(day_num)
        elif isinstance(data.get('dateArray'), list):
            for value in data['dateArray']:
                day_num = self.parse_record_day(value)
                if day_num is not None:
                    info['signed_days'].add(day_num)

        if info['signed_days']:
            info['signed_count'] = len(info['signed_days'])
            if datetime.now().day in info['signed_days']:
                info['today_signed'] = True

        if assume_today_signed:
            info['today_signed'] = True
            info['signed_days'].add(datetime.now().day)

        # 连续天数兜底：从今天（今天没签则从昨天）往前逐日数
        if info['streak'] is None and info['signed_days']:
            today = datetime.now().day
            cursor = today if today in info['signed_days'] else today - 1
            streak = 0
            while cursor >= 1 and cursor in info['signed_days']:
                streak += 1
                cursor -= 1
            info['streak'] = streak

        return info
