#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
钉钉机器人 - 直连 DB-GPT 口腔客服 API
使用 Stream 模式，无需公网 IP

启动方式: python dingtalk_bot.py
前提: dental_chat_api.py 已在 localhost:8090 运行

环境变量（在 .env 中配置）:
  DINGTALK_CLIENT_ID     - 钉钉 AppKey
  DINGTALK_CLIENT_SECRET - 钉钉 AppSecret
  DENTAL_API_BASE        - DB-GPT API 地址（默认 http://localhost:8090）
"""

import json
import logging
import os
import sys
import time
import threading
from typing import Optional

import dingtalk_stream
from dingtalk_stream import AckMessage
from dotenv import load_dotenv

# 尽早加载 .env
load_dotenv()

# ============================================================
# 配置
# ============================================================

DINGTALK_CLIENT_ID = os.getenv("DINGTALK_CLIENT_ID", "")
DINGTALK_CLIENT_SECRET = os.getenv("DINGTALK_CLIENT_SECRET", "")
DENTAL_API_BASE = os.getenv("DENTAL_API_BASE", "http://localhost:8090")

# ============================================================
# 日志
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("dingtalk_bot")

# ============================================================
# 用户会话管理
# ============================================================

class SessionManager:
    """管理钉钉用户 → DB-GPT 会话映射 + 身份信息"""

    def __init__(self, session_timeout: int = 1800):
        self._sessions: dict = {}       # dingtalk_user_id → {session_id, last_active, kf_info}
        self._timeout = session_timeout  # 秒
        self._lock = threading.Lock()

    def get_session(self, user_id: str) -> Optional[str]:
        """获取用户的 DB-GPT session_id，过期则返回 None"""
        with self._lock:
            info = self._sessions.get(user_id)
            if not info:
                return None
            if time.time() - info["last_active"] > self._timeout:
                del self._sessions[user_id]
                return None
            info["last_active"] = time.time()
            return info["session_id"]

    def set_session(self, user_id: str, session_id: str):
        with self._lock:
            if user_id in self._sessions:
                self._sessions[user_id]["session_id"] = session_id
                self._sessions[user_id]["last_active"] = time.time()
            else:
                self._sessions[user_id] = {
                    "session_id": session_id,
                    "last_active": time.time(),
                    "kf_info": None,
                }

    def get_kf_info(self, user_id: str) -> Optional[dict]:
        """获取用户绑定的客服身份"""
        with self._lock:
            info = self._sessions.get(user_id)
            return info.get("kf_info") if info else None

    def set_kf_info(self, user_id: str, kf_info: dict):
        with self._lock:
            if user_id not in self._sessions:
                self._sessions[user_id] = {
                    "session_id": None,
                    "last_active": time.time(),
                    "kf_info": kf_info,
                }
            else:
                self._sessions[user_id]["kf_info"] = kf_info

    def clear_session(self, user_id: str):
        with self._lock:
            if user_id in self._sessions:
                self._sessions[user_id]["session_id"] = None


sessions = SessionManager()

# ============================================================
# 客服身份映射（本地缓存 + 数据库自动匹配）
# ============================================================

KF_USERS_FILE = os.path.join(os.path.dirname(__file__), "kf_users.json")

# 渠道系统数据库配置（用于查 un_admin 表）
QUDAO_DB_CONFIG = {
    "host": os.getenv("QUDAO_DB_HOST", "localhost"),
    "port": int(os.getenv("QUDAO_DB_PORT", "3306")),
    "user": os.getenv("QUDAO_DB_USER", "root"),
    "password": os.getenv("QUDAO_DB_PASSWORD", ""),
    "database": os.getenv("QUDAO_DB_NAME", "kfsyscb"),
}


def load_kf_users() -> dict:
    """加载本地缓存的客服身份映射表"""
    if os.path.exists(KF_USERS_FILE):
        try:
            with open(KF_USERS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_kf_users(data: dict):
    with open(KF_USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def query_kf_from_db(name: str) -> Optional[dict]:
    """从渠道系统 un_admin 表按姓名自动匹配客服身份"""
    try:
        import pymysql
        conn = pymysql.connect(
            host=QUDAO_DB_CONFIG["host"],
            port=QUDAO_DB_CONFIG["port"],
            user=QUDAO_DB_CONFIG["user"],
            password=QUDAO_DB_CONFIG["password"],
            database=QUDAO_DB_CONFIG["database"],
            charset="utf8mb4",
            connect_timeout=5,
        )
        try:
            with conn.cursor(pymysql.cursors.DictCursor) as cur:
                cur.execute(
                    "SELECT userid, username, realname FROM un_admin "
                    "WHERE realname = %s OR username = %s LIMIT 1",
                    (name, name),
                )
                row = cur.fetchone()
                if row:
                    kf_id = row.get("userid", 0)
                    kf_name = row.get("realname") or row.get("username", name)
                    logger.info(f"数据库匹配成功: {name} → kf_id={kf_id}, kf_name={kf_name}")
                    return {"kf_id": kf_id, "kf_name": kf_name, "role": "客服"}
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"查询 un_admin 失败: {e}")
    return None


def get_dingtalk_user_info(staff_id: str) -> Optional[dict]:
    """通过钉钉 API 用 staffId 获取用户信息（真名、是否管理员等）"""
    if not staff_id:
        return None
    try:
        # 获取 access_token
        token_url = "https://oapi.dingtalk.com/gettoken"
        token_req = urllib.request.Request(
            f"{token_url}?appkey={DINGTALK_CLIENT_ID}&appsecret={DINGTALK_CLIENT_SECRET}"
        )
        with urllib.request.urlopen(token_req, timeout=5) as resp:
            token_data = json.loads(resp.read().decode("utf-8"))
        access_token = token_data.get("access_token")
        if not access_token:
            logger.warning(f"获取钉钉 token 失败: {token_data}")
            return None

        # 用 staffId 查用户详情
        user_url = f"https://oapi.dingtalk.com/topapi/v2/user/get?access_token={access_token}"
        payload = json.dumps({"userid": staff_id}).encode("utf-8")
        user_req = urllib.request.Request(user_url, data=payload, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(user_req, timeout=5) as resp:
            user_data = json.loads(resp.read().decode("utf-8"))

        if user_data.get("errcode") != 0:
            logger.warning(f"钉钉 API 返回错误: {user_data.get('errmsg')}")
            return None

        result = user_data.get("result", {})
        name = result.get("name", "")
        is_admin = result.get("admin", False)
        logger.info(f"钉钉 API: staff_id={staff_id} → name={name}, admin={is_admin}")
        return {"name": name, "admin": is_admin, "userid": staff_id}
    except Exception as e:
        logger.warning(f"钉钉 API 查询失败: {e}")
    return None


def lookup_kf(sender_nick: str, sender_id: str, staff_id: str = "") -> Optional[dict]:
    """
    查找客服身份，优先级：
    1. 本地缓存（按 sender_id）
    2. 本地缓存（按钉钉昵称）
    3. 数据库 un_admin 表（按钉钉昵称匹配）
    4. 钉钉 API 获取真名 → 再拿真名去数据库匹配
    匹配成功后自动缓存到本地
    """
    users = load_kf_users()

    # 1. 本地缓存 - 按 sender_id
    if sender_id in users:
        return users[sender_id]

    # 2. 本地缓存 - 按昵称
    if sender_nick in users:
        info = users[sender_nick]
        users[sender_id] = info
        save_kf_users(users)
        return info

    # 3. 数据库匹配 - 按钉钉昵称
    if sender_nick:
        info = query_kf_from_db(sender_nick)
        if info:
            info["dingtalk_nick"] = sender_nick
            users[sender_id] = info
            users[sender_nick] = info
            save_kf_users(users)
            return info

    # 4. 钉钉 API 获取用户信息 → 真名匹配 + 管理员自动识别
    if staff_id:
        dt_info = get_dingtalk_user_info(staff_id)
        if dt_info:
            real_name = dt_info.get("name", "")
            is_admin = dt_info.get("admin", False)

            # 4a. 如果真名和昵称不同，用真名再查一次数据库
            if real_name and real_name != sender_nick:
                info = query_kf_from_db(real_name)
                if info:
                    info["dingtalk_nick"] = sender_nick
                    info["dingtalk_real_name"] = real_name
                    if is_admin:
                        info["role"] = "管理员"
                    users[sender_id] = info
                    users[sender_nick] = info
                    users[real_name] = info
                    save_kf_users(users)
                    return info

            # 4b. 钉钉管理员但不在客服系统中 → 直接赋予管理员权限（可看全量数据）
            if is_admin:
                info = {
                    "kf_id": None,
                    "kf_name": real_name or sender_nick,
                    "role": "管理员",
                    "dingtalk_nick": sender_nick,
                    "dingtalk_real_name": real_name,
                }
                logger.info(f"钉钉管理员自动识别: {sender_nick} → 管理员（可查看全量数据）")
                users[sender_id] = info
                users[sender_nick] = info
                save_kf_users(users)
                return info

    return None


def register_kf(sender_id: str, sender_nick: str, kf_name: str, kf_id: int, role: str = "客服"):
    """手动注册钉钉用户 → 客服身份映射（当自动匹配失败时使用）"""
    users = load_kf_users()
    info = {"kf_id": kf_id, "kf_name": kf_name, "role": role, "dingtalk_nick": sender_nick}
    users[sender_id] = info
    if sender_nick:
        users[sender_nick] = info
    save_kf_users(users)
    return info


# ============================================================
# 调用 DB-GPT API
# ============================================================

import urllib.request
import urllib.error


def call_dental_chat(question: str, session_id: str = None) -> dict:
    """调用 DB-GPT 口腔客服 chat 接口"""
    url = f"{DENTAL_API_BASE}/api/dental/chat"
    payload = {"question": question}
    if session_id:
        payload["session_id"] = session_id

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        logger.error(f"DB-GPT API error: HTTP {e.code} - {body[:200]}")
        return {"success": False, "error": f"服务异常 ({e.code})"}
    except urllib.error.URLError as e:
        logger.error(f"DB-GPT connection error: {e.reason}")
        return {"success": False, "error": "客服系统暂时无法连接，请稍后重试"}


def call_dental_kf_today(kf_id: int = None, kf_name: str = None) -> dict:
    """查询今日跟进统计"""
    url = f"{DENTAL_API_BASE}/api/dental/kf/today_stats"
    payload = {}
    if kf_id:
        payload["kf_id"] = kf_id
    if kf_name:
        payload["kf_name"] = kf_name
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        logger.error(f"today_stats error: {e}")
        return {"status": 0, "error": str(e)}


def call_dental_kf_warning(kf_id: int = None, kf_name: str = None,
                           follow_status: int = None, page: int = 1, page_size: int = 10) -> dict:
    """查询客户跟进预警列表"""
    url = f"{DENTAL_API_BASE}/api/dental/kf/warning_stats"
    payload = {"page": page, "page_size": page_size}
    if kf_id:
        payload["kf_id"] = kf_id
    if kf_name:
        payload["kf_name"] = kf_name
    if follow_status is not None:
        payload["follow_status"] = follow_status
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        logger.error(f"warning_stats error: {e}")
        return {"status": 0, "error": str(e)}


# ============================================================
# 消息意图识别（简单规则，不依赖 LLM）
# ============================================================

def detect_intent(text: str) -> str:
    """简单关键词意图识别"""
    text = text.strip()

    # 新会话
    if text in ("/new", "新会话", "重新开始"):
        return "new_session"

    # 绑定身份
    if text.startswith("/bind ") or text.startswith("绑定 "):
        return "bind_identity"

    # 今日统计 / 我的工作（仅限第一人称 "我" 或无主语的精确命令）
    # 注意："张静今日派单"这类带他人名字的查询不应在此拦截，应送 API 走 kf_stats
    if any(kw in text for kw in [
        "今天跟了", "今日统计", "今日进度", "完成情况", "今天客户量",
        "我的派单", "我今天的", "我的客户",
        "今日我的", "我的业绩", "今天业绩", "我的数据", "今日数据",
    ]):
        return "today_stats"

    # 待跟进
    if any(kw in text for kw in ["待跟进", "要跟进谁", "跟进列表", "没跟进的"]):
        return "pending_followup"

    # 逾期
    if any(kw in text for kw in ["逾期", "超时", "过期客户"]):
        return "overdue"

    # 其余都走通用 chat
    return "chat"


# ============================================================
# 格式化回复
# ============================================================

def format_today_stats(result: dict, kf_name: str = "") -> str:
    """格式化今日统计"""
    if result.get("status") != 1:
        return f"查询失败: {result.get('error', '未知错误')}"

    data = result.get("data", {})
    finished = data.get("today_finished_num", 0)
    prefix = f"【{kf_name}】" if kf_name else ""
    return f"{prefix}今日跟进统计：\n已完成跟进: {finished} 名客户"


def format_warning_list(result: dict, status_label: str = "待跟进") -> str:
    """格式化客户预警列表"""
    if result.get("status") != 1:
        return f"查询失败: {result.get('error', '未知错误')}"

    data = result.get("data", {})
    total = data.get("all_total", 0)
    items = data.get("list", [])

    lines = [f"【{status_label}客户】共 {total} 名\n"]
    for i, item in enumerate(items[:10], 1):
        name = item.get("client_name", "未知")
        intention_map = {1: "意向好", 2: "一般", 3: "不好", 4: "其他"}
        intention = intention_map.get(item.get("ai_intention"), "未知")
        project = item.get("plastics_intention", "")
        overdue = item.get("overdue_days", 0)
        overdue_str = f" (逾期{overdue}天)" if overdue and overdue > 0 else ""
        lines.append(f"{i}. {name} - {intention} - {project}{overdue_str}")

    if total > 10:
        lines.append(f"\n...还有 {total - 10} 名客户，请说「下一页」查看更多")

    return "\n".join(lines)


# ============================================================
# 钉钉消息处理器
# ============================================================

class DentalBotHandler(dingtalk_stream.ChatbotHandler):
    """口腔客服钉钉机器人"""

    def __init__(self):
        super(dingtalk_stream.ChatbotHandler, self).__init__()
        self.logger = logger

    async def process(self, callback: dingtalk_stream.CallbackMessage):
        try:
            import re as _re
            incoming = dingtalk_stream.ChatbotMessage.from_dict(callback.data)

            # 提取文本：优先从 incoming 对象获取，兜底从原始 callback.data 提取
            text = ""
            if incoming.text and incoming.text.content:
                text = incoming.text.content.strip()
            if not text:
                raw_text_obj = callback.data.get("text", {})
                if isinstance(raw_text_obj, dict):
                    text = (raw_text_obj.get("content") or "").strip()

            # 清理 @mention 标记（群聊中 @机器人 会带上 @XXX 前缀）
            text = _re.sub(r'@\S+\s*', '', text).strip()

            sender_id = incoming.sender_id or ""
            sender_nick = incoming.sender_nick or ""
            sender_staff_id = incoming.sender_staff_id or ""
            conversation_type = incoming.conversation_type  # "1"=单聊 "2"=群聊
            is_group = conversation_type == "2"

            logger.info(
                f"收到消息: [{sender_nick}] staff_id={sender_staff_id} "
                f"sender_id={sender_id[:12]}... text={text[:50]}"
            )
            # 打印完整 callback.data 用于调试（上线后可删除）
            logger.debug(f"callback.data keys: {list(callback.data.keys())}")

            if not text:
                if is_group:
                    self.reply_text("您好！请在 @我 后面输入您的问题 😊", incoming)
                else:
                    self.reply_text("请输入您的问题 😊", incoming)
                return AckMessage.STATUS_OK, "OK"

            # 意图识别
            intent = detect_intent(text)

            # --- 新会话 ---
            if intent == "new_session":
                sessions.clear_session(sender_id)
                self.reply_text("已开启新会话 ✅", incoming)
                return AckMessage.STATUS_OK, "OK"

            # --- 绑定身份 ---
            if intent == "bind_identity":
                parts = text.replace("/bind ", "").replace("绑定 ", "").strip().split()
                if len(parts) >= 2:
                    kf_name = parts[0]
                    try:
                        kf_id = int(parts[1])
                    except ValueError:
                        self.reply_text("格式: 绑定 姓名 工号\n例如: 绑定 张三 5", incoming)
                        return AckMessage.STATUS_OK, "OK"
                    info = register_kf(sender_id, sender_nick, kf_name, kf_id)
                    sessions.set_kf_info(sender_id, info)
                    self.reply_text(f"身份绑定成功 ✅\n姓名: {kf_name}\n工号: {kf_id}\n角色: 客服", incoming)
                elif len(parts) == 1:
                    kf_name = parts[0]
                    info = register_kf(sender_id, sender_nick, kf_name, kf_id=0, role="客服")
                    sessions.set_kf_info(sender_id, info)
                    self.reply_text(f"身份绑定成功 ✅\n姓名: {kf_name}\n（未设置工号，将按姓名查询）", incoming)
                else:
                    self.reply_text("格式: 绑定 姓名 工号\n例如: 绑定 张三 5", incoming)
                return AckMessage.STATUS_OK, "OK"

            # --- 需要身份的操作 ---
            kf_info = sessions.get_kf_info(sender_id) or lookup_kf(sender_nick, sender_id, sender_staff_id)
            if kf_info:
                sessions.set_kf_info(sender_id, kf_info)

            if intent in ("today_stats", "pending_followup", "overdue"):
                # 需要身份才能查客户数据
                if not kf_info:
                    self.reply_text(
                        f"您好！我是小通，神舟商通口腔智能助手 🦷\n\n"
                        f"我没有在系统中找到「{sender_nick}」对应的客服账号。\n"
                        f"请发送以下命令绑定：\n"
                        f"绑定 您的真实姓名 您的工号\n\n"
                        f"例如：绑定 张三 5\n\n"
                        f"（如果您的钉钉昵称和客服系统姓名一致，可能是网络问题，请稍后重试）",
                        incoming,
                    )
                    return AckMessage.STATUS_OK, "OK"

                kf_id = kf_info.get("kf_id") or None
                kf_name = kf_info.get("kf_name", "")
                is_manager = kf_info.get("role") in ("主管", "经理", "管理员") or kf_id is None

                if intent == "today_stats":
                    # "我的派单"等第一人称查询，即使是管理员也查个人数据
                    _asking_self = any(kw in text for kw in ["我的", "我今天", "今日我的"])
                    if is_manager and not (_asking_self and kf_name):
                        result = call_dental_kf_today()
                        reply = format_today_stats(result, "全公司")
                    else:
                        result = call_dental_kf_today(kf_id=kf_id, kf_name=kf_name)
                        reply = format_today_stats(result, kf_name)

                elif intent == "pending_followup":
                    if is_manager:
                        result = call_dental_kf_warning(follow_status=0)
                    else:
                        result = call_dental_kf_warning(kf_id=kf_id, kf_name=kf_name, follow_status=0)
                    reply = format_warning_list(result, "待跟进")

                elif intent == "overdue":
                    if is_manager:
                        result = call_dental_kf_warning(follow_status=2)
                    else:
                        result = call_dental_kf_warning(kf_id=kf_id, kf_name=kf_name, follow_status=2)
                    reply = format_warning_list(result, "逾期")

                self.reply_markdown("查询结果", reply, incoming)
                return AckMessage.STATUS_OK, "OK"

            # --- 通用 chat（医院、医生、价格等） ---
            # 先发送 AI 流式卡片展示"处理中"动画，查询完成后填充内容
            card = None
            try:
                card = self.ai_markdown_card_start(incoming, title="小通助手")
                # 只保留标题和正文区域，去掉 msgSlider 等无用占位
                card.set_order(["msgTitle", "msgContent"])
            except Exception as card_err:
                logger.warning(f"AI卡片创建失败，降级为markdown: {card_err}")

            session_id = sessions.get_session(sender_id)
            logger.info(f"调用 chat: session_id={session_id[:8] + '...' if session_id else 'None'}")
            result = call_dental_chat(text, session_id)

            if result.get("success"):
                new_session_id = result.get("session_id", "")
                if new_session_id:
                    sessions.set_session(sender_id, new_session_id)
                    logger.info(f"session 更新: {new_session_id[:8]}...")
                answer = result.get("answer", "抱歉，没有找到相关信息")
                # 截断过长回复（钉钉单条消息限制）
                if len(answer) > 4000:
                    answer = answer[:4000] + "\n\n...回复过长已截断，请缩小问题范围"
                if card:
                    try:
                        card.ai_streaming(markdown=answer, append=False)
                        card.ai_finish(markdown=answer)
                    except Exception as card_err:
                        logger.warning(f"AI卡片更新失败，降级为markdown: {card_err}")
                        self.reply_markdown("查询结果", answer, incoming)
                else:
                    self.reply_markdown("查询结果", answer, incoming)
            else:
                error = result.get("error", "系统繁忙，请稍后重试")
                error_msg = f"抱歉，{error}"
                if card:
                    try:
                        card.ai_finish(markdown=error_msg)
                    except Exception:
                        self.reply_text(error_msg, incoming)
                else:
                    self.reply_text(error_msg, incoming)

        except Exception as e:
            logger.exception(f"处理消息异常: {e}")
            try:
                self.reply_text("抱歉，系统处理异常，请稍后重试", incoming)
            except Exception:
                pass

        return AckMessage.STATUS_OK, "OK"


# ============================================================
# 启动
# ============================================================

def check_dental_api():
    """检查 DB-GPT 服务是否在线"""
    try:
        req = urllib.request.Request(f"{DENTAL_API_BASE}/health")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("status") == "ok":
                return True
    except Exception:
        pass
    return False


def main():
    if not DINGTALK_CLIENT_ID or not DINGTALK_CLIENT_SECRET:
        print("错误: 请在 .env 中配置 DINGTALK_CLIENT_ID 和 DINGTALK_CLIENT_SECRET")
        sys.exit(1)

    # 检查 DB-GPT
    if check_dental_api():
        logger.info(f"DB-GPT 服务在线: {DENTAL_API_BASE}")
    else:
        logger.warning(f"DB-GPT 服务不可达: {DENTAL_API_BASE}，请确认 dental_chat_api.py 已启动")

    print(f"""
+============================================================+
|           神舟商通 · 口腔智能客服 · 钉钉机器人              |
+============================================================+
|  DB-GPT API: {DENTAL_API_BASE:<43}|
|  钉钉 AppKey: {DINGTALK_CLIENT_ID:<42}|
+============================================================+

支持的命令:
  绑定 姓名 工号     — 绑定客服身份（首次使用必须）
  /new               — 开启新会话
  今天客户量如何      — 查看今日跟进统计（按你的身份）
  待跟进客户          — 查看需要跟进的客户列表
  逾期客户            — 查看逾期未跟进客户
  其他问题            — 自动调用 AI 客服（医院/医生/价格/活动）

启动中...
""")

    credential = dingtalk_stream.Credential(DINGTALK_CLIENT_ID, DINGTALK_CLIENT_SECRET)
    client = dingtalk_stream.DingTalkStreamClient(credential)
    client.register_callback_handler(
        dingtalk_stream.chatbot.ChatbotMessage.TOPIC,
        DentalBotHandler(),
    )
    client.start_forever()


if __name__ == "__main__":
    main()
