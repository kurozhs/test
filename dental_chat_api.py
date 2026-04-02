#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
口腔智能客服 API 服务
支持: 医院推荐、医生排班、预约查询、活动促销、价格咨询、医生履历等

启动方式: python dental_chat_api.py
API端点: POST http://localhost:8090/api/dental/chat

安全修复版本 - 2026-01-22
"""

import json
import logging
import os
import re
from datetime import date, datetime
from collections import OrderedDict
import time
import uuid
from decimal import Decimal
from functools import wraps
from typing import Any, Dict, List, Optional

import pymysql
from pymysql.cursors import DictCursor
from dbutils.pooled_db import PooledDB
import requests
from flask import Flask, Response, jsonify, request, stream_with_context
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from dotenv import load_dotenv

# 加载环境变量
load_dotenv()

# ============================================================
# 配置 - 从环境变量读取，不再硬编码敏感信息
# ============================================================

# 医院知识库数据库
DB_CONFIG = {
    "host": os.getenv("DB_HOST", "localhost"),
    "port": int(os.getenv("DB_PORT", "3306")),
    "user": os.getenv("DB_USER", "root"),
    "password": os.getenv("DB_PASSWORD", ""),  # 必须从环境变量获取
    "database": os.getenv("DB_NAME", "hospital_db"),
}

# 渠道系统数据库（客户和跟进数据）
QUDAO_DB_CONFIG = {
    "host": os.getenv("QUDAO_DB_HOST", ""),
    "port": int(os.getenv("QUDAO_DB_PORT", "3306")),
    "user": os.getenv("QUDAO_DB_USER", ""),
    "password": os.getenv("QUDAO_DB_PASSWORD", ""),
    "database": os.getenv("QUDAO_DB_NAME", ""),
}

LLM_CONFIG = {
    "api_base": os.getenv("LLM_API_BASE", "http://localhost:8000/v1"),
    "api_key": os.getenv("LLM_API_KEY", ""),
    "model": os.getenv("LLM_MODEL", "qwen3-omni-30b"),
}

# API 认证密钥 (用于保护敏感端点)
API_SECRET_KEY = os.getenv("API_SECRET_KEY", "")

# 预计算开关：开启后优先读取 hospital_db 预计算表，回退实时查询
USE_PRECOMPUTED = os.getenv("USE_PRECOMPUTED_STATS", "0") == "1"
PRECOMPUTED_MAX_AGE_HOURS = 48  # 预计算数据超过此时间自动回退实时查询

# LLM 意图分类开关：开启后优先用 LLM 做语义意图分类，失败回退关键词匹配
USE_LLM_INTENT = os.getenv("USE_LLM_INTENT", "1") == "1"

# 允许的 CORS 来源
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "*").split(",")

# ============================================================
# LLM 意图分类 Prompt
# ============================================================
INTENT_CLASSIFICATION_PROMPT = """你是意图分类器，根据用户问题输出JSON。不要输出任何其他文字。

## 意图列表（选最匹配的1-2个）：

知识库类：
- hospital_recommend: 推荐医院、哪家好、派什么医院
- doctor_recommend: 推荐医生、专家、大夫
- schedule: 排班、坐诊、出诊
- appointment: 预约、挂号
- promotion: 活动、优惠、促销
- price: 价格、费用、多少钱
- career: 医生离职、调动、去哪了
- hospital_info: 医院详情、介绍、地址、怎么样

业务分析类：
- hospital_ranking: 医院排名（按派单量维度）
- hospital_deals: 成交数据（金额、项目、客单价，含地区成交排名）
- hospital_analysis: 医院合作分析（成交率、转化率、返款）
- district_hospital: 地区医院表现
- customer_conversion: 客户成交概率/转化分析
- customer_follow: 客户跟进记录
- customer_info: 客户信息查询
- customer_lifecycle: 客户生命周期/漏斗/新增/统计
- kf_stats: 客服工作统计/业绩
- churn_warning: 流失预警/风险客户
- time_analysis: 时段趋势/高峰/进线分析
- source_analysis: 渠道来源分析
- source_compare: 多渠道对比
- source_list: 列出来源
- department_stats: 部门业绩/部门成交（TEG/BCG/CDG/OMG/BSG/MMG/CMG/AMG等事业群）
- company_stats: 公司整体业绩/全公司成交/公司成交情况

## 关键区分规则：
- "成交好的医院"/"成交多的医院"/"XX地区成交排名" → hospital_deals（成交维度）
- "派单多"/"派单排名" → hospital_ranking（派单维度）
- "我的派单"/"今日我的派单"/"我今天的客户" → kf_stats（个人工作统计，不是医院排名！）
- "XX医院成交率/转化率" → hospital_analysis（合作分析）
- "XX医院这个月成交/客单价/业绩" → hospital_deals（具体成交数据）
- "客户XXX成交概率" → customer_conversion
- "新增客户/今日新增/转化漏斗" → customer_lifecycle
- "XX客服业绩/跟进情况" → kf_stats
- "XX今日跟单情况"/"XX近日跟进情况" → kf_stats（XX是人名=客服，跟单/跟进=工作统计）
- "贝色今日注册"/"贝色近日数据" → source_analysis（贝色=来源/渠道名，不是人名）
- 含"我的"/"我今天"等第一人称 → 通常是 kf_stats，不要分到 hospital_ranking
- "TEG成交"/"BCG业绩"/"XX部门成交"/"XX事业群业绩" → department_stats（TEG/BCG/CDG/OMG/BSG/MMG/CMG/AMG是部门代码，不要分到source_analysis或hospital_deals）
- "公司成交"/"公司业绩"/"全公司"/"公司整体"/"公司预测" → company_stats

## 输出格式（纯JSON）：
{"intent":[],"city":"","district":"","hospital":"","doctor":"","project":"","customer_id":null,"kf_name":"","source_keyword":"","time_range":"","limit":0,"risk_level":"","department_name":""}

intent: 数组，1-2个最匹配的意图
city: 城市名（如"上海"、"北京"）
district: 区域名（如"浦东"、"海淀"）
hospital: 医院名简称（如"维乐"、"鼎植"）
doctor: 医生姓名
project: 项目名称（种植、矫正等）
customer_id: 客户ID数字，没有则null
kf_name: 客服姓名
source_keyword: 来源/渠道关键词
time_range: 时间范围（如"今天"、"本月"、"最近30天"、"本周"）
limit: 返回数量限制，没有则0
risk_level: 风险等级筛选（critical/high/medium/all），仅流失预警时填写
department_name: 部门缩写（如TEG/BCG/CDG/OMG/BSG/MMG/CMG/AMG），仅department_stats时填写"""

VALID_INTENTS = {
    "hospital_recommend", "doctor_recommend", "schedule", "appointment",
    "promotion", "price", "career", "hospital_info",
    "hospital_ranking", "hospital_deals", "hospital_analysis", "district_hospital",
    "customer_conversion", "customer_follow", "customer_info", "customer_lifecycle",
    "kf_stats", "churn_warning", "time_analysis",
    "source_analysis", "source_compare", "source_list",
    "department_stats", "company_stats",
}

# 部门代码 → (department_id, 中文全称)
DEPARTMENT_MAP = {
    "CDG": (2, "CDG-企业发展事业群"),
    "BCG": (3, "BCG-商业拓展事业群"),
    "OMG": (4, "OMG-运营管理事业群"),
    "BSG": (5, "BSG-商务服务事业群"),
    "MMG": (7, "MMG-市场营销事业群"),
    "TEG": (9, "TEG-技术工程事业群"),
    "新媒体": (18, "新媒体事业部"),
    "8682": (19, "8682事业部"),
    "CMG": (23, "CMG-客户管理事业群"),
    "重庆": (26, "重庆事业部"),
    "AMG": (27, "AMG-资产管理事业群"),
    "马来": (28, "马来事业部"),
}

# 部门缩写别名（用于意图识别时的模糊匹配）
DEPARTMENT_ALIASES = {
    "技术": "TEG", "工程": "TEG",
    "商业": "BCG", "拓展": "BCG",
    "企业发展": "CDG",
    "运营": "OMG",
    "商务": "BSG",
    "市场": "MMG", "营销": "MMG",
    "客户管理": "CMG",
    "资产": "AMG",
}

# Flask 应用
app = Flask(__name__)

# CORS 配置 - 开发环境允许所有来源
CORS(app, resources={
    r"/api/*": {
        "origins": "*",  # 开发环境允许所有来源
        "methods": ["GET", "POST", "OPTIONS"],
        "allow_headers": ["Content-Type", "Authorization"]
    }
})

# 速率限制
limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=["200 per day", "50 per hour"]
)

# 日志配置 - 不记录敏感信息
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


# ============================================================
# 时间范围工具函数
# ============================================================
def get_current_month_days() -> int:
    """
    计算从本月1号到今天的天数
    默认分析本月数据
    """
    today = datetime.now()
    first_day_of_month = today.replace(day=1)
    days = (today - first_day_of_month).days + 1  # +1 包含今天
    return max(days, 1)  # 至少1天


def normalize_query_days(days: int = None, max_days: int = 365) -> int:
    """
    标准化查询天数
    - 默认: 本月天数
    - 最大: 365天 (一年)
    """
    if days is None or days == 0:
        days = get_current_month_days()
    return min(max(days, 1), max_days)

# ============================================================
# 会话上下文管理 - 支持多轮对话
# ============================================================
class ConversationStore:
    """简单的会话历史存储（内存实现，带TTL）"""
    def __init__(self, max_sessions=1000, ttl_seconds=1800):  # 30分钟过期
        self.sessions = OrderedDict()
        self.max_sessions = max_sessions
        self.ttl_seconds = ttl_seconds

    def _cleanup_expired(self):
        """清理过期会话"""
        now = time.time()
        expired = [sid for sid, data in self.sessions.items()
                   if now - data['last_access'] > self.ttl_seconds]
        for sid in expired:
            del self.sessions[sid]
        # 限制总会话数
        while len(self.sessions) > self.max_sessions:
            self.sessions.popitem(last=False)

    def get_history(self, session_id: str) -> list:
        """获取会话历史"""
        self._cleanup_expired()
        if session_id in self.sessions:
            self.sessions[session_id]['last_access'] = time.time()
            return self.sessions[session_id]['history']
        return []

    def add_message(self, session_id: str, role: str, content: str):
        """添加消息到会话历史"""
        self._cleanup_expired()
        if session_id not in self.sessions:
            self.sessions[session_id] = {
                'history': [],
                'last_access': time.time()
            }
        self.sessions[session_id]['history'].append({
            'role': role,
            'content': content
        })
        self.sessions[session_id]['last_access'] = time.time()
        # 只保留最近10轮对话
        if len(self.sessions[session_id]['history']) > 20:
            self.sessions[session_id]['history'] = self.sessions[session_id]['history'][-20:]

    def create_session(self) -> str:
        """创建新会话"""
        session_id = str(uuid.uuid4())
        self.sessions[session_id] = {
            'history': [],
            'last_access': time.time()
        }
        return session_id

# 全局会话存储
conversation_store = ConversationStore()

# ============================================================
# 数据库连接池 - 提高性能和安全性
# ============================================================
db_pool = None
qudao_db_pool = None

def get_db_pool():
    """获取医院知识库数据库连接池"""
    global db_pool
    if db_pool is None:
        if not DB_CONFIG["password"]:
            raise ValueError("数据库密码未配置，请设置 DB_PASSWORD 环境变量")
        db_pool = PooledDB(
            creator=pymysql,
            maxconnections=10,
            mincached=2,
            maxcached=5,
            blocking=True,
            **DB_CONFIG,
            charset='utf8mb4'
        )
    return db_pool


def get_qudao_db_pool():
    """获取渠道系统数据库连接池"""
    global qudao_db_pool
    if qudao_db_pool is None:
        qudao_db_pool = PooledDB(
            creator=pymysql,
            maxconnections=12,
            mincached=2,
            maxcached=8,
            blocking=True,
            **QUDAO_DB_CONFIG,
            charset='utf8mb4'
        )
    return qudao_db_pool


def query_qudao_db(sql: str, params: tuple = None) -> List[Dict]:
    """
    查询渠道系统数据库

    Args:
        sql: SQL语句
        params: 参数元组

    Returns:
        查询结果列表
    """
    pool = get_qudao_db_pool()
    conn = pool.connection()
    try:
        with conn.cursor(DictCursor) as cursor:
            cursor.execute(sql, params)
            result = cursor.fetchall()
            return list(result)
    except Exception as e:
        logger.error(f"渠道数据库查询错误: {e}")
        return []
    finally:
        conn.close()


# ============================================================
# 工具函数
# ============================================================
class CustomEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, (date, datetime)):
            return obj.isoformat()
        if isinstance(obj, Decimal):
            return float(obj)
        return super().default(obj)


def mask_phone(phone: str) -> str:
    """
    手机号脱敏处理
    例: 13563639631 -> 135****9631
    """
    if not phone:
        return ""
    phone = str(phone).strip()
    if len(phone) >= 7:
        return phone[:3] + "****" + phone[-4:]
    return phone


# Linkage 名称缓存（避免重复查询）
_linkage_cache = {}


def get_linkage_name(linkage_id: int) -> str:
    """
    根据 linkage_id 查询对应的名称
    使用缓存减少数据库查询
    """
    if not linkage_id:
        return "未知"

    linkage_id = int(linkage_id)

    # 检查缓存
    if linkage_id in _linkage_cache:
        return _linkage_cache[linkage_id]

    sql = "SELECT name FROM un_linkage WHERE linkageid = %s LIMIT 1"
    results = query_qudao_db(sql, (linkage_id,))

    if results:
        name = results[0].get('name', '未知')
        _linkage_cache[linkage_id] = name
        return name

    _linkage_cache[linkage_id] = "未知"
    return "未知"


def get_linkage_names_batch(linkage_ids: List[int]) -> Dict[int, str]:
    """
    批量查询 linkage 名称（减少数据库查询次数）
    """
    if not linkage_ids:
        return {}

    # 过滤掉已缓存和无效的ID
    ids_to_query = [lid for lid in linkage_ids if lid and lid not in _linkage_cache]

    if ids_to_query:
        placeholders = ','.join(['%s'] * len(ids_to_query))
        sql = f"SELECT linkageid, name FROM un_linkage WHERE linkageid IN ({placeholders})"
        results = query_qudao_db(sql, tuple(ids_to_query))

        for row in results:
            _linkage_cache[row['linkageid']] = row['name']

    # 返回所有请求的ID对应的名称
    return {lid: _linkage_cache.get(lid, "未知") for lid in linkage_ids if lid}


# 意向项目父类目缓存
_intention_parent_cache = {}


def get_intention_parent_and_siblings(intention_id: int) -> Dict:
    """
    查询意向项目的父类目及同级子项目列表
    用于转化率回退到父类目维度

    Returns:
        {'parent_id': int, 'parent_name': str, 'sibling_ids': [int], 'sibling_count': int}
    """
    if not intention_id:
        return {'parent_id': 0, 'parent_name': '', 'sibling_ids': [], 'sibling_count': 0}

    intention_id = int(intention_id)

    if intention_id in _intention_parent_cache:
        return _intention_parent_cache[intention_id]

    result = {'parent_id': 0, 'parent_name': '', 'sibling_ids': [], 'sibling_count': 0}

    # 查询当前项目的 parentid
    sql_parent = "SELECT parentid FROM un_linkage WHERE linkageid = %s LIMIT 1"
    rows = query_qudao_db(sql_parent, (intention_id,))
    if not rows or not rows[0].get('parentid'):
        _intention_parent_cache[intention_id] = result
        return result

    parent_id = int(rows[0]['parentid'])
    result['parent_id'] = parent_id

    # 查询父节点的名称和 arrchildid
    sql_children = "SELECT name, arrchildid FROM un_linkage WHERE linkageid = %s LIMIT 1"
    rows = query_qudao_db(sql_children, (parent_id,))
    if not rows:
        _intention_parent_cache[intention_id] = result
        return result

    result['parent_name'] = rows[0].get('name', '')
    arrchildid = rows[0].get('arrchildid', '') or ''

    # arrchildid 格式为逗号分隔的 ID 列表
    if arrchildid:
        try:
            sibling_ids = [int(x.strip()) for x in arrchildid.split(',') if x.strip().isdigit()]
            result['sibling_ids'] = sibling_ids
            result['sibling_count'] = len(sibling_ids)
        except (ValueError, AttributeError):
            pass

    _intention_parent_cache[intention_id] = result
    return result


def query_db(sql: str, params: tuple = None) -> List[Dict]:
    """
    执行SQL查询 - 使用连接池和参数化查询

    Args:
        sql: SQL语句，使用 %s 作为占位符
        params: 参数元组

    Returns:
        查询结果列表
    """
    pool = get_db_pool()
    conn = pool.connection()
    try:
        with conn.cursor(DictCursor) as cursor:
            cursor.execute(sql, params)
            result = cursor.fetchall()
            return list(result)
    finally:
        conn.close()


def execute_db(sql: str, params: tuple = None):
    """执行写入操作（INSERT/UPDATE/DELETE）"""
    pool = get_db_pool()
    conn = pool.connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, params)
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.error(f"数据库写入错误: {e}")
        raise
    finally:
        conn.close()


def init_analysis_cache_table():
    """初始化分析缓存表（hospital_db）"""
    pool = get_db_pool()
    conn = pool.connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS customer_analysis_cache (
                    client_id INT PRIMARY KEY,
                    analysis_json LONGTEXT NOT NULL,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
            # 预计算表1: 留电时段转化率
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS precomputed_hourly_conversion (
                    client_region INT NOT NULL COMMENT '业务线(1=医美 2=口腔 4=韩国 8=眼科, 0=全业务线)',
                    register_hour TINYINT NOT NULL COMMENT '留电小时(0-23)',
                    total INT NOT NULL DEFAULT 0,
                    converted INT NOT NULL DEFAULT 0,
                    rate DECIMAL(8,4) NOT NULL DEFAULT 0 COMMENT '转化率(0-1)',
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    PRIMARY KEY (client_region, register_hour)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='预计算-留电时段转化率'
            """)

            # 预计算表2: 同类转化率
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS precomputed_conversion_rate (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    dimension_type VARCHAR(20) NOT NULL COMMENT 'precise/overall/sibling/business_line',
                    intention_id INT NOT NULL DEFAULT 0 COMMENT '意向项目ID',
                    region_id INT NOT NULL DEFAULT 0 COMMENT '意向地区ID',
                    client_region INT NOT NULL DEFAULT 0 COMMENT '业务线',
                    parent_id INT NOT NULL DEFAULT 0 COMMENT '父类目ID(sibling维度)',
                    time_window VARCHAR(10) NOT NULL COMMENT 'd90/d180/d365/all',
                    total INT NOT NULL DEFAULT 0,
                    converted INT NOT NULL DEFAULT 0,
                    rate DECIMAL(8,6) NOT NULL DEFAULT 0 COMMENT '转化率(0-1)',
                    sample_sufficient TINYINT NOT NULL DEFAULT 0 COMMENT '样本>=10',
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    UNIQUE KEY uk_dimension (dimension_type, intention_id, region_id, client_region, parent_id, time_window),
                    INDEX idx_lookup (intention_id, region_id, client_region)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='预计算-同类转化率'
            """)

            # 预计算表3: 成交周期
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS precomputed_conversion_cycle (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    dimension_type VARCHAR(20) NOT NULL COMMENT 'precise/overall/sibling/business_line',
                    intention_id INT NOT NULL DEFAULT 0 COMMENT '意向项目ID',
                    region_id INT NOT NULL DEFAULT 0 COMMENT '意向地区ID',
                    client_region INT NOT NULL DEFAULT 0 COMMENT '业务线',
                    parent_id INT NOT NULL DEFAULT 0 COMMENT '父类目ID(sibling维度)',
                    time_window VARCHAR(10) NOT NULL COMMENT 'd90/d180/d365/all',
                    sample_size INT NOT NULL DEFAULT 0,
                    avg_days DECIMAL(8,1) NOT NULL DEFAULT 0,
                    median_days DECIMAL(8,1) NOT NULL DEFAULT 0,
                    p25_days DECIMAL(8,1) NOT NULL DEFAULT 0,
                    p75_days DECIMAL(8,1) NOT NULL DEFAULT 0,
                    min_days INT NOT NULL DEFAULT 0,
                    max_days INT NOT NULL DEFAULT 0,
                    distribution_json TEXT COMMENT '分布JSON: {"0-7":n, "8-14":n, ...}',
                    sample_sufficient TINYINT NOT NULL DEFAULT 0 COMMENT '样本>=10',
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    UNIQUE KEY uk_dimension (dimension_type, intention_id, region_id, client_region, parent_id, time_window),
                    INDEX idx_lookup (intention_id, region_id, client_region)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='预计算-成交周期'
            """)

            # 预计算表4: 地区医院实力
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS precomputed_district_hospital (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    zx_district INT NOT NULL DEFAULT 0 COMMENT '意向地区ID',
                    client_region INT NOT NULL DEFAULT 0 COMMENT '业务线',
                    district_name VARCHAR(100) NOT NULL DEFAULT '',
                    total_hospitals INT NOT NULL DEFAULT 0,
                    total_deals INT NOT NULL DEFAULT 0,
                    total_amount DECIMAL(14,2) NOT NULL DEFAULT 0,
                    hospitals_with_repeat INT NOT NULL DEFAULT 0,
                    district_score INT NOT NULL DEFAULT 0,
                    hospitals_json TEXT COMMENT 'Top10医院详情JSON',
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    UNIQUE KEY uk_district (zx_district, client_region)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='预计算-地区医院实力'
            """)

            # 预计算表5: 任务日志
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS precompute_job_log (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    job_type VARCHAR(50) NOT NULL COMMENT '任务类型: hourly/rates/cycles/district/full',
                    start_time DATETIME NOT NULL,
                    end_time DATETIME,
                    rows_affected INT DEFAULT 0,
                    duration_seconds DECIMAL(8,2) DEFAULT 0,
                    status VARCHAR(20) DEFAULT 'running' COMMENT 'running/success/error',
                    error_message TEXT,
                    INDEX idx_job_time (job_type, start_time)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='预计算任务日志'
            """)

        conn.commit()
        logger.info("customer_analysis_cache + 预计算表已就绪")
    except Exception as e:
        logger.error(f"创建缓存表失败: {e}")
    finally:
        conn.close()


def get_analysis_cache(client_id: int, max_age_hours: int = 24) -> Dict:
    """
    获取缓存的分析结果

    Args:
        client_id: 客户ID
        max_age_hours: 缓存最大有效期（小时），默认24小时

    Returns:
        缓存的分析结果，过期或不存在返回 None
    """
    rows = query_db(
        "SELECT analysis_json, updated_at FROM customer_analysis_cache WHERE client_id = %s",
        (client_id,)
    )
    if not rows:
        return None

    row = rows[0]
    updated_at = row['updated_at']
    age = datetime.now() - updated_at
    if age.total_seconds() > max_age_hours * 3600:
        logger.info(f"[cache] client={client_id} 缓存已过期({age})")
        return None

    try:
        result = json.loads(row['analysis_json'])
        logger.info(f"[cache] client={client_id} 命中缓存 (age={age})")
        return result
    except Exception as e:
        logger.error(f"[cache] 解析缓存JSON失败: {e}")
        return None


def save_analysis_cache(client_id: int, analysis: Dict):
    """保存分析结果到缓存"""
    try:
        analysis_json = json.dumps(analysis, ensure_ascii=False, cls=CustomEncoder)
        execute_db(
            """INSERT INTO customer_analysis_cache (client_id, analysis_json, created_at, updated_at)
               VALUES (%s, %s, NOW(), NOW())
               ON DUPLICATE KEY UPDATE analysis_json = VALUES(analysis_json), updated_at = NOW()""",
            (client_id, analysis_json)
        )
        logger.info(f"[cache] client={client_id} 已缓存分析结果")
    except Exception as e:
        logger.error(f"[cache] 保存缓存失败: {e}")


def call_llm(prompt: str) -> str:
    """调用LLM（单条 prompt）- 带重试机制"""
    return call_llm_messages([{"role": "user", "content": prompt}])


def call_llm_messages(messages: list) -> str:
    """调用LLM（多轮 messages）- 带重试机制

    Args:
        messages: OpenAI 格式的 messages 数组，如:
            [{"role": "system", "content": "..."},
             {"role": "user", "content": "..."},
             {"role": "assistant", "content": "..."},
             ...]
    """
    max_retries = 3

    for attempt in range(max_retries):
        try:
            response = requests.post(
                f"{LLM_CONFIG['api_base']}/chat/completions",
                headers={"Authorization": f"Bearer {LLM_CONFIG['api_key']}"},
                json={
                    "model": LLM_CONFIG["model"],
                    "messages": messages,
                    "temperature": 0.5,
                    "max_tokens": 2000,
                },
                timeout=30,
            )
            response.raise_for_status()
            return response.json()["choices"][0]["message"]["content"]
        except requests.exceptions.RequestException as e:
            logger.warning(f"LLM调用失败 (尝试 {attempt + 1}/{max_retries})")
            if attempt == max_retries - 1:
                logger.error("LLM调用最终失败")
                return "抱歉，系统繁忙，请稍后再试。"
        except Exception as e:
            logger.error(f"LLM调用异常: {type(e).__name__}")
            return "抱歉，系统繁忙，请稍后再试。"


def require_api_key(f):
    """API 密钥认证装饰器"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not API_SECRET_KEY:
            # 如果未配置密钥，拒绝访问敏感端点
            return jsonify({"error": "此端点未启用"}), 403

        auth_header = request.headers.get('Authorization', '')
        if not auth_header.startswith('Bearer '):
            return jsonify({"error": "需要认证"}), 401

        token = auth_header[7:]
        if token != API_SECRET_KEY:
            return jsonify({"error": "认证失败"}), 401

        return f(*args, **kwargs)
    return decorated_function


def sanitize_like_param(value: str) -> str:
    """
    清理 LIKE 查询参数，防止通配符注入

    Args:
        value: 原始值

    Returns:
        转义后的值
    """
    if not value:
        return ""
    # 转义 SQL LIKE 特殊字符
    return value.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')


# ============================================================
# 客户查询和成交概率分析
# ============================================================
def query_customer_info(client_id: int) -> Optional[Dict]:
    """查询客户基本信息（含客户类型判断字段）"""
    sql = """
        SELECT
            Client_Id, ClientName, Sex, Age, MobilePhone,
            District, zx_District, PlasticsIntention, client_region,
            Status, client_status, RegisterTime, EditTime,
            KfId, ManagerId, from_type, Remarks,
            WeiXin, whatsapp, line
        FROM un_channel_client
        WHERE Client_Id = %s
    """
    results = query_qudao_db(sql, (client_id,))
    return results[0] if results else None


def get_customer_type_info(customer: Dict) -> Dict:
    """
    根据客户信息判断客户类型和业务线

    Returns:
        客户类型详情字典
    """
    # client_region: 1=国内医美, 2=国内口腔, 4=韩国, 5=日本, 8=国内眼科
    client_region = customer.get('client_region', 0) or 0

    region_map = {
        0: ('未知', '未分类'),
        1: ('国内医美', '医美'),
        2: ('国内口腔', '口腔'),
        4: ('韩国', '韩国医美'),
        5: ('日本', '日本医美'),
        8: ('国内眼科', '眼科'),
    }
    region_info = region_map.get(client_region, ('未知', '未分类'))
    # 根据 client_region 判断是否是韩国/海外客户
    is_korean = client_region in (4, 5)  # 韩国或日本

    if is_korean:
        return {
            'is_korean': True,
            'customer_type': '韩国/海外客户',
            'customer_type_badge': 'korean',
            'business_line': region_info[1],
            'business_line_code': client_region,
            'needs_archive': True,
            'needs_schedule': True,
            'workflow': '建档→护照录入→日程安排(接机→术前→手术→术后→送机)→成交',
            'workflow_steps': ['建档', '护照录入', '接机安排', '术前检查', '手术', '术后护理', '送机'],
        }
    else:
        return {
            'is_korean': False,
            'customer_type': '国内客户',
            'customer_type_badge': 'domestic',
            'business_line': region_info[1],
            'business_line_code': client_region,
            'needs_archive': False,
            'needs_schedule': False,
            'workflow': '咨询→派单→预约→到院→成交',
            'workflow_steps': ['咨询', '派单', '预约', '到院', '成交'],
        }


def query_customer_archives(client_id: int) -> List[Dict]:
    """查询客户档案信息（韩国客户专用）"""
    sql = """
        SELECT
            archives_id, archives_name, birth_time, passport_no,
            passport_expiry_date, nationality, go_time
        FROM un_custom_archives
        WHERE client_id = %s
        ORDER BY archives_id DESC
    """
    return query_qudao_db(sql, (client_id,))


def query_archive_users(archives_id: int) -> List[Dict]:
    """查询档案关联人员（同行人员）"""
    sql = """
        SELECT
            id, user_name, py_name, passport_no, is_main, relation
        FROM un_custom_archives_user
        WHERE archives_id = %s AND status = 1
        ORDER BY is_main DESC, sort_order ASC
    """
    return query_qudao_db(sql, (archives_id,))


def query_customer_appointments(client_id: int, archives_id: int = None) -> List[Dict]:
    """查询客户预约/日程信息（含航班、酒店）"""
    if archives_id:
        sql = """
            SELECT
                appoint_id, archives_id, client_id, hospital_id,
                appoint_time, add_time, status, pay_status,
                arrive_status, flight_no, starttime, endtime, hotel, title
            FROM un_ding_client_appoint
            WHERE (client_id = %s OR archives_id = %s)
            ORDER BY appoint_time DESC
            LIMIT 10
        """
        return query_qudao_db(sql, (client_id, archives_id))
    else:
        sql = """
            SELECT
                appoint_id, archives_id, client_id, hospital_id,
                appoint_time, add_time, status, pay_status,
                arrive_status, flight_no, starttime, endtime, hotel, title
            FROM un_ding_client_appoint
            WHERE client_id = %s
            ORDER BY appoint_time DESC
            LIMIT 10
        """
        return query_qudao_db(sql, (client_id,))


def query_hospital_orders(client_id: int) -> List[Dict]:
    """查询医院派单记录"""
    sql = """
        SELECT
            a.order_id, a.hospital_id, a.send_order_time, a.surgery_time,
            a.surgery_status, a.view_status,
            b.companyname as hospital_name
        FROM un_hospital_order a
        LEFT JOIN un_hospital_company b ON a.hospital_id = b.userid
        WHERE a.Client_Id = %s
        ORDER BY a.send_order_time DESC
        LIMIT 10
    """
    return query_qudao_db(sql, (client_id,))


def analyze_korean_customer_status(customer: Dict, archives: List, appointments: List, orders: List) -> Dict:
    """分析韩国客户的流程状态和待办事项"""
    completed = []
    pending = []
    warnings = []
    recommendations = []
    current_stage = '初始咨询'

    if archives:
        completed.append('已建档')
        archive = archives[0]
        if archive.get('passport_no'):
            completed.append('护照已录入')
        else:
            pending.append('护照信息待补充')
            warnings.append('⚠️ 护照信息未录入，可能影响后续流程')
        current_stage = '已建档'
    else:
        pending.append('待建档')
        warnings.append('⚠️ 韩国客户需要建档，请尽快创建档案')
        recommendations.append('建议：立即为客户创建档案并录入护照信息')

    if orders:
        completed.append(f'已派单({len(orders)}个医院)')
        current_stage = '已派单'
        surgery_done = any(o.get('surgery_status') == 10 for o in orders)
        if surgery_done:
            completed.append('手术已完成')
            current_stage = '术后阶段'
    else:
        if archives:
            pending.append('待派单到医院')
            recommendations.append('建议：已建档客户可以开始派单到合适的医院')

    if appointments:
        active_appts = [a for a in appointments if a.get('status') in [1, 2, 99]]
        if active_appts:
            completed.append(f'有{len(active_appts)}个有效预约')
            has_flight = any(a.get('flight_no') for a in active_appts)
            if has_flight:
                completed.append('航班信息已录入')
            else:
                pending.append('航班信息待补充')
                warnings.append('⚠️ 预约已创建但航班信息未录入')
            has_hotel = any(a.get('hotel') for a in active_appts)
            if has_hotel:
                completed.append('酒店信息已录入')
            else:
                pending.append('酒店信息待补充')
            current_stage = '预约确认中'
    else:
        if orders:
            pending.append('待创建预约/日程')
            recommendations.append('建议：已派单客户需要创建详细日程（接机、术前、手术等）')

    if not pending and orders:
        current_stage = '流程完成' if any(o.get('surgery_status') == 10 for o in orders) else '跟进中'

    return {
        'current_stage': current_stage,
        'completed_steps': completed,
        'pending_steps': pending,
        'warnings': warnings,
        'recommendations': recommendations,
        'progress_percent': len(completed) / (len(completed) + len(pending)) * 100 if (completed or pending) else 0
    }


def analyze_domestic_customer_status(customer: Dict, orders: List, follow_records: List) -> Dict:
    """分析国内客户的流程状态"""
    completed = []
    pending = []
    warnings = []
    recommendations = []
    current_stage = '咨询阶段'

    if orders:
        completed.append(f'已派单({len(orders)}个医院)')
        current_stage = '已派单'
        surgery_done = any(o.get('surgery_status') == 10 for o in orders)
        if surgery_done:
            completed.append('已完成')
            current_stage = '已成交'
    else:
        pending.append('待派单')
        if len(follow_records) > 5:
            recommendations.append('建议：跟进次数较多，可考虑派单到合适医院')

    if follow_records:
        completed.append(f'已跟进{len(follow_records)}次')
        recent = follow_records[0] if follow_records else None
        if recent:
            try:
                last_time = recent.get('time', 0)
                if last_time:
                    days_since = (datetime.now() - datetime.fromtimestamp(last_time)).days
                    if days_since > 7:
                        warnings.append(f'⚠️ 已{days_since}天未跟进')
                        recommendations.append('建议：及时跟进，保持联系')
            except:
                pass
    else:
        pending.append('待首次跟进')
        recommendations.append('建议：尽快进行首次跟进了解客户需求')

    return {
        'current_stage': current_stage,
        'completed_steps': completed,
        'pending_steps': pending,
        'warnings': warnings,
        'recommendations': recommendations,
        'progress_percent': len(completed) / (len(completed) + len(pending)) * 100 if (completed or pending) else 0
    }


def query_follow_records(client_id: int, limit: int = 20) -> List[Dict]:
    """查询客户跟进记录"""
    sql = """
        SELECT
            CM_Id as id, Content as content, CMPostTime as time,
            Type as type, KfId as kf_id
        FROM un_channel_managermessage
        WHERE ClientId = %s
        ORDER BY CMPostTime DESC
        LIMIT %s
    """
    return query_qudao_db(sql, (client_id, limit))


def query_order_records(client_id: int) -> List[Dict]:
    """查询客户成交记录"""
    sql = """
        SELECT
            id, hospital_id, hospital as hospital_name,
            true_number, number, time, status, op_type, item
        FROM un_channel_paylist
        WHERE Client_Id = %s
        ORDER BY time DESC
    """
    return query_qudao_db(sql, (client_id,))


def query_failure_cases(intention: int, region: int, client_id: int = 0, limit: int = 3) -> List[Dict]:
    """
    查询同类客户的失败案例（长期未成交）

    筛选条件：注册超过60天、状态为未成交或已取消、无订单
    """
    sql = """
        SELECT
            c.Client_Id,
            c.ClientName,
            c.RegisterTime,
            c.Status,
            DATEDIFF(NOW(), FROM_UNIXTIME(c.RegisterTime)) as days_since_register,
            (SELECT COUNT(*) FROM un_channel_managermessage m WHERE m.ClientId = c.Client_Id) as follow_count
        FROM un_channel_client c
        LEFT JOIN un_channel_paylist p ON c.Client_Id = p.Client_Id AND p.status = 1
        WHERE c.PlasticsIntention = %s
          AND (c.zx_District = %s OR c.client_region = %s)
          AND c.Status IN (0, 2)
          AND c.RegisterTime <= UNIX_TIMESTAMP(DATE_SUB(NOW(), INTERVAL 60 DAY))
          AND p.id IS NULL
          AND c.Client_Id != %s
        ORDER BY c.RegisterTime ASC
        LIMIT %s
    """
    results = query_qudao_db(sql, (intention, region, region, client_id, limit))

    cases = []
    for r in results:
        name = r.get('ClientName', '')
        if len(name) >= 2:
            masked_name = name[0] + '*' * (len(name) - 1)
        else:
            masked_name = '*'

        status = '未成交' if r.get('Status') == 0 else '已取消'

        cases.append({
            'name': masked_name,
            'status': status,
            'days': r.get('days_since_register', 0) or 0,
            'follow_count': r.get('follow_count', 0) or 0,
            'register_date': datetime.fromtimestamp(r.get('RegisterTime', 0)).strftime('%Y-%m-%d') if r.get('RegisterTime') else ''
        })

    return cases


def query_success_cases(intention: int, region: int, client_id: int = 0, limit: int = 3) -> List[Dict]:
    """
    查询相似客户的成功案例（已成交）

    Args:
        intention: 意向项目ID
        region: 意向地区ID
        client_id: 当前客户ID（排除）
        limit: 返回数量

    Returns:
        成功案例列表
    """
    sql = """
        SELECT
            c.Client_Id,
            c.ClientName,
            c.RegisterTime,
            p.hospital as hospital_name,
            p.true_number as amount,
            p.time as deal_time,
            p.item as project,
            DATEDIFF(FROM_UNIXTIME(p.time), FROM_UNIXTIME(c.RegisterTime)) as follow_days
        FROM un_channel_client c
        JOIN un_channel_paylist p ON c.Client_Id = p.Client_Id
        WHERE c.PlasticsIntention = %s
          AND (c.zx_District = %s OR c.client_region = %s)
          AND c.Status = 1
          AND p.status = 1
          AND p.true_number > 0
          AND c.Client_Id != %s
        ORDER BY p.time DESC
        LIMIT %s
    """
    results = query_qudao_db(sql, (intention, region, region, client_id, limit))

    cases = []
    for r in results:
        # 脱敏处理客户名
        name = r.get('ClientName', '')
        if len(name) >= 2:
            masked_name = name[0] + '*' * (len(name) - 1)
        else:
            masked_name = '*'

        cases.append({
            'name': masked_name,
            'hospital': r.get('hospital_name', '未知'),
            'amount': float(r.get('amount', 0) or 0),
            'project': r.get('project', ''),
            'follow_days': r.get('follow_days', 0) or 0,
            'deal_time': datetime.fromtimestamp(r.get('deal_time', 0)).strftime('%Y-%m-%d') if r.get('deal_time') else ''
        })

    return cases


def query_hospital_ranking(area: str, limit: int = 10) -> Dict:
    """
    查询地区医院排名（按派单量）

    Args:
        area: 地区名称（如：上海、北京）
        limit: 返回数量

    Returns:
        医院排名数据
    """
    # 先查询地区ID - 使用 un_linkage 表，keyid=1 表示地区分类
    area_sql = "SELECT linkageid, arrchildid FROM un_linkage WHERE name LIKE %s AND keyid = 1 AND parentid = 0 LIMIT 1"
    area_result = query_qudao_db(area_sql, (f"%{area}%",))

    if not area_result:
        # 尝试模糊匹配（可能是区级）
        area_sql2 = "SELECT linkageid, arrchildid FROM un_linkage WHERE name LIKE %s AND keyid = 1 LIMIT 1"
        area_result = query_qudao_db(area_sql2, (f"%{area}%",))

    if not area_result:
        return {'hospitals': [], 'area': area, 'error': '未找到该地区'}

    district_ids = area_result[0].get('arrchildid', '')
    if not district_ids:
        district_ids = str(area_result[0].get('linkageid', '0'))

    # 查询医院派单排名
    ranking_sql = f"""
        SELECT
            b.userid as hospital_id,
            b.companyname as hospital_name,
            b.hospital_type,
            COUNT(a.hospital_id) as dispatch_count
        FROM un_hospital_order a
        LEFT JOIN un_hospital_company b ON a.hospital_id = b.userid
        WHERE b.points != 10
          AND b.islock = 0
          AND b.is_suspend = 0
          AND b.areaid IN ({district_ids})
        GROUP BY a.hospital_id
        ORDER BY dispatch_count DESC
        LIMIT %s
    """
    hospitals = query_qudao_db(ranking_sql, (limit,))

    # 医院类型映射
    hospital_types = {
        1: '公立医院', 2: '民营连锁', 3: '民营单店',
        4: '门诊部', 5: '诊所'
    }

    result_list = []
    for i, h in enumerate(hospitals):
        result_list.append({
            'rank': i + 1,
            'hospital_name': h.get('companyname') or h.get('hospital_name', '未知'),
            'hospital_type': hospital_types.get(h.get('hospital_type', 0), '其他'),
            'dispatch_count': h.get('dispatch_count', 0)
        })

    return {
        'hospitals': result_list,
        'area': area,
        'total': len(result_list),
        'data_type': 'dispatch',  # 标记是派单数据
        'notice': '成交数据暂不可查，以下为派单量排名'
    }


def query_kf_stats(kf_name: str, days: int = 7) -> Dict:
    """
    查询客服的跟进情况统计

    Args:
        kf_name: 客服姓名（支持真名或用户名）
        days: 查询天数范围

    Returns:
        客服跟进统计数据
    """
    import time
    start_time = int(time.time()) - days * 24 * 3600

    # 查找客服ID
    kf_sql = """
        SELECT userid, username, realname
        FROM un_admin
        WHERE realname LIKE %s OR username LIKE %s
        LIMIT 1
    """
    kf_result = query_qudao_db(kf_sql, (f"%{kf_name}%", f"%{kf_name}%"))

    if not kf_result:
        return {'error': f'未找到客服"{kf_name}"'}

    kf = kf_result[0]
    kf_id = kf.get('userid')
    kf_realname = kf.get('realname', '')
    kf_username = kf.get('username', '')

    # 跟进统计
    stats_sql = """
        SELECT
            COUNT(*) as follow_count,
            COUNT(DISTINCT client_id) as client_count
        FROM un_channel_crm
        WHERE kfid = %s
        AND addtime >= %s
    """
    stats = query_qudao_db(stats_sql, (kf_id, start_time))
    stat = stats[0] if stats else {}

    # 今日统计
    today_start = int(time.mktime(time.strptime(time.strftime('%Y-%m-%d'), '%Y-%m-%d')))
    today_stats = query_qudao_db(stats_sql, (kf_id, today_start))
    today_stat = today_stats[0] if today_stats else {}

    # 最近跟进记录（跟进内容在title字段）
    records_sql = """
        SELECT
            client_id,
            DATE_FORMAT(FROM_UNIXTIME(addtime), '%%Y-%%m-%%d %%H:%%i') as follow_time,
            title,
            client_status
        FROM un_channel_crm
        WHERE kfid = %s
        ORDER BY addtime DESC
        LIMIT 10
    """
    records = query_qudao_db(records_sql, (kf_id,))

    # 客户状态分布
    status_sql = """
        SELECT
            client_status,
            COUNT(DISTINCT client_id) as count
        FROM un_channel_crm
        WHERE kfid = %s
        AND addtime >= %s
        GROUP BY client_status
        ORDER BY count DESC
    """
    status_dist = query_qudao_db(status_sql, (kf_id, start_time))

    # 状态映射
    status_map = {
        0: '待跟进', 1: '已联系', 2: '有意向', 3: '已预约',
        4: '已到院', 5: '已成交', 6: '无效', 7: '流失'
    }

    # 成交业绩统计（通过客户表关联）
    sales_sql = """
        SELECT
            COUNT(*) as deal_count,
            SUM(p.number) as total_amount,
            AVG(p.number) as avg_amount
        FROM un_channel_paylist p
        JOIN un_channel_client c ON p.Client_Id = c.Client_Id
        WHERE c.KfId = %s
        AND p.addtime >= %s
        AND p.number > 0
    """
    sales_stats = query_qudao_db(sales_sql, (kf_id, start_time))
    sale_stat = sales_stats[0] if sales_stats else {}

    # 成交项目明细
    sales_detail_sql = """
        SELECT
            p.hospital,
            p.item,
            p.number as amount,
            DATE_FORMAT(FROM_UNIXTIME(p.addtime), '%%Y-%%m-%%d') as deal_date,
            p.ClientName as client_name
        FROM un_channel_paylist p
        JOIN un_channel_client c ON p.Client_Id = c.Client_Id
        WHERE c.KfId = %s
        AND p.addtime >= %s
        AND p.number > 0
        ORDER BY p.addtime DESC
        LIMIT 10
    """
    sales_records = query_qudao_db(sales_detail_sql, (kf_id, start_time))

    return {
        'kf_id': kf_id,
        'kf_name': kf_realname or kf_username,
        'kf_username': kf_username,
        'query_days': days,
        'statistics': {
            'follow_count': int(stat.get('follow_count', 0) or 0),
            'client_count': int(stat.get('client_count', 0) or 0),
            'today_follow': int(today_stat.get('follow_count', 0) or 0),
            'today_client': int(today_stat.get('client_count', 0) or 0)
        },
        'status_distribution': [
            {
                'status': status_map.get(s.get('client_status', 0), '未知'),
                'count': int(s.get('count', 0) or 0)
            }
            for s in status_dist
        ],
        'recent_records': [
            {
                'client_id': r.get('client_id'),
                'time': r.get('follow_time', ''),
                'content': (r.get('title', '') or '').strip()[:50],
                'status': status_map.get(r.get('client_status', 0), '未知')
            }
            for r in records
        ],
        'sales': {
            'deal_count': int(sale_stat.get('deal_count', 0) or 0),
            'total_amount': float(sale_stat.get('total_amount', 0) or 0),
            'avg_amount': float(sale_stat.get('avg_amount', 0) or 0),
            'recent_deals': [
                {
                    'hospital': r.get('hospital', ''),
                    'project': r.get('item', ''),
                    'amount': float(r.get('amount', 0) or 0),
                    'date': r.get('deal_date', ''),
                    'client_name': r.get('client_name', '')
                }
                for r in sales_records
            ]
        }
    }


def _parse_php_array(php_str: str) -> List[int]:
    """解析PHP array字符串，支持两种格式：
    - array(7062,7063,...) — compact格式
    - array(\n  0 => '5701',\n  1 => '5704',\n ...) — var_export格式
    """
    if not php_str:
        return []
    s = str(php_str)
    # 尝试提取所有数字值（兼容两种PHP数组格式）
    # 格式1: array(7062,7063,...)
    # 格式2: array(\n 0 => '5701', \n 1 => '5704', ...)
    if 'array' not in s.lower():
        return []
    # 提取 => 'xxx' 格式的值（var_export格式）
    vals = re.findall(r"=>\s*'(\d+)'", s)
    if vals:
        return [int(v) for v in vals]
    # 回退到 compact 格式: array(123,456,...)
    m = re.search(r'array\s*\(([^)]*)\)', s, re.IGNORECASE | re.DOTALL)
    if m:
        inner = m.group(1).strip()
        if inner:
            return [int(x.strip()) for x in inner.split(',') if x.strip().isdigit()]
    return []


def _get_department_channel_ids(dept_id: int) -> List[int]:
    """查询部门对应的 from_type(channel_id) 列表"""
    sql = "SELECT channel_id FROM un_admin_department WHERE id = %s LIMIT 1"
    rows = query_qudao_db(sql, (dept_id,))
    if not rows:
        return []
    return _parse_php_array(rows[0].get('channel_id', ''))


def _get_korea_rate(addtime: int = None) -> float:
    """获取韩币汇率，从 un_korea_rate 表动态取，默认 160"""
    from datetime import datetime
    if addtime and addtime > 0:
        month_str = datetime.fromtimestamp(addtime).strftime('%Y-%m')
    else:
        month_str = datetime.now().strftime('%Y-%m')
    rows = query_qudao_db("SELECT rate FROM un_korea_rate WHERE month = %s LIMIT 1", (month_str,))
    if rows and rows[0].get('rate'):
        return float(rows[0]['rate'])
    # 查不到本月查上月
    from dateutil.relativedelta import relativedelta
    prev = datetime.strptime(month_str, '%Y-%m') - relativedelta(months=1)
    prev_str = prev.strftime('%Y-%m')
    rows = query_qudao_db("SELECT rate FROM un_korea_rate WHERE month = %s LIMIT 1", (prev_str,))
    if rows and rows[0].get('rate'):
        return float(rows[0]['rate'])
    return 160.0


def _convert_currency_to_rmb(number: float, currency_type: int, checknumber: float = 0,
                              addtime: int = None) -> float:
    """币种转换为人民币，checknumber > 0 时优先使用（与PHP convert_to_rmb一致）"""
    if checknumber and float(checknumber) > 0:
        return float(checknumber)
    number = float(number or 0)
    currency_type = int(currency_type or 0)
    if currency_type == 1:  # 韩币 — 动态汇率
        rate = _get_korea_rate(addtime)
        return round(number / rate, 2)
    rate_map = {3: 0.14, 4: 4.6, 5: 15}  # 美元/泰铢/日元
    rate = rate_map.get(currency_type, 0)
    if rate > 0:
        return round(number / rate, 2)
    return number


def _parse_verym_project(verym_project_str: str) -> set:
    """解析 'loc_123,loc_456' → {123, 456}"""
    if not verym_project_str:
        return set()
    ids = set()
    for part in str(verym_project_str).split(','):
        part = part.strip()
        m = re.match(r'loc_(\d+)', part)
        if m:
            ids.add(int(m.group(1)))
    return ids


def _get_project_category_ids() -> Dict[str, set]:
    """获取口腔(5707)和眼科(7552)的子分类ID集合"""
    result = {'kq': set(), 'yk': set()}
    for parent_id, key in [(5707, 'kq'), (7552, 'yk')]:
        sql = "SELECT arrchildid FROM un_linkage WHERE linkageid = %s LIMIT 1"
        rows = query_qudao_db(sql, (parent_id,))
        if rows:
            child_str = rows[0].get('arrchildid', '')
            if child_str:
                result[key] = {int(x.strip()) for x in str(child_str).split(',') if x.strip().isdigit()}
                result[key].add(parent_id)
    return result


def _categorize_pay_record(record: Dict, project_ids: Dict[str, set]) -> str:
    """将支付记录分类为 kq/ym/yk/kr/other（与PHP department_statistics.class.php完全一致）

    PHP逻辑：
    1. 用 verym_project 判断口腔/眼科
    2. 既不是口腔也不是眼科的：人民币(currency_type=2)→医美，韩币(currency_type=1)→韩国
    3. 其他币种（美元/泰铢/日元）→ other（只计入总计，不归入任何分项）
    """
    # 用 verym_project 判断口腔/眼科
    verym_ids = _parse_verym_project(record.get('verym_project', ''))
    is_kq = bool(verym_ids and verym_ids & project_ids.get('kq', set()))
    is_yk = bool(verym_ids and verym_ids & project_ids.get('yk', set()))

    if is_kq:
        return 'kq'
    if is_yk:
        return 'yk'

    # 不是口腔也不是眼科，按币种分
    currency_type = int(record.get('currency_type') or 0)
    if currency_type == 2 or currency_type == 0:  # 人民币或默认
        return 'ym'
    if currency_type == 1:  # 韩币
        return 'kr'
    return 'other'  # 美元/泰铢/日元等 — 只计入总计


def _parse_department_time_range(question: str) -> Dict:
    """解析部门业绩查询的时间范围，返回 {start_ts, end_ts, time_label}"""
    from datetime import datetime, timedelta
    import calendar
    import time as _time

    now = datetime.now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    # 上个月/上月
    if any(kw in question for kw in ["上个月", "上月"]):
        if now.month == 1:
            first_day = now.replace(year=now.year - 1, month=12, day=1, hour=0, minute=0, second=0, microsecond=0)
        else:
            first_day = now.replace(month=now.month - 1, day=1, hour=0, minute=0, second=0, microsecond=0)
        last_day_num = calendar.monthrange(first_day.year, first_day.month)[1]
        last_day = first_day.replace(day=last_day_num, hour=23, minute=59, second=59)
        return {
            'start_ts': int(first_day.timestamp()),
            'end_ts': int(last_day.timestamp()),
            'time_label': f"上月（{first_day.month}/{first_day.day}-{last_day.month}/{last_day.day}）"
        }

    # 今日/今天
    if any(kw in question for kw in ["今日", "今天"]):
        return {
            'start_ts': int(today_start.timestamp()),
            'end_ts': int(_time.time()),
            'time_label': f"今日（{now.month}/{now.day}）"
        }

    # 昨天/昨日
    if any(kw in question for kw in ["昨天", "昨日"]):
        yesterday = today_start - timedelta(days=1)
        yesterday_end = today_start - timedelta(seconds=1)
        return {
            'start_ts': int(yesterday.timestamp()),
            'end_ts': int(yesterday_end.timestamp()),
            'time_label': f"昨日（{yesterday.month}/{yesterday.day}）"
        }

    # 本周/这周
    if any(kw in question for kw in ["本周", "这周"]):
        week_start = today_start - timedelta(days=now.weekday())
        return {
            'start_ts': int(week_start.timestamp()),
            'end_ts': int(_time.time()),
            'time_label': f"本周（{week_start.month}/{week_start.day}-{now.month}/{now.day}）"
        }

    # 上周
    if "上周" in question:
        this_week_start = today_start - timedelta(days=now.weekday())
        last_week_start = this_week_start - timedelta(days=7)
        last_week_end = this_week_start - timedelta(seconds=1)
        return {
            'start_ts': int(last_week_start.timestamp()),
            'end_ts': int(last_week_end.timestamp()),
            'time_label': f"上周（{last_week_start.month}/{last_week_start.day}-{last_week_end.month}/{last_week_end.day}）"
        }

    # 半年
    if "半年" in question:
        half_year_ago = now - timedelta(days=180)
        return {
            'start_ts': int(half_year_ago.timestamp()),
            'end_ts': int(_time.time()),
            'time_label': f"近半年（{half_year_ago.month}/{half_year_ago.day}-{now.month}/{now.day}）"
        }

    # 最近N天
    days_match = re.search(r'[最近近]?(\d+)[天日]', question)
    if days_match:
        n = min(int(days_match.group(1)), 365)
        start = now - timedelta(days=n)
        return {
            'start_ts': int(start.timestamp()),
            'end_ts': int(_time.time()),
            'time_label': f"最近{n}天（{start.month}/{start.day}-{now.month}/{now.day}）"
        }

    # N个月
    month_match = re.search(r'(\d+)\s*个?月', question)
    if month_match and "上" not in question and "本" not in question:
        n = min(int(month_match.group(1)), 12)
        start = now - timedelta(days=n * 30)
        return {
            'start_ts': int(start.timestamp()),
            'end_ts': int(_time.time()),
            'time_label': f"近{n}个月（{start.month}/{start.day}-{now.month}/{now.day}）"
        }

    # 默认：本月
    first_day = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return {
        'start_ts': int(first_day.timestamp()),
        'end_ts': int(_time.time()),
        'time_label': f"本月（{now.month}/{first_day.day}-{now.month}/{now.day}）"
    }


def _get_reflect_channel_ids() -> Dict[int, int]:
    """获取老渠道映射：新channel_id → 老channel_id（来源兼容）"""
    # 来自 app_data_config.php 的 reflect 配置
    reflect = {2: 5595, 3: 5596, 4: 5597, 5: 5598, 7: 5599, 8: 5600}
    # PHP: array_flip 后变成 {5595:2, 5596:3, ...}
    return {v: k for k, v in reflect.items()}


def _get_audit_time_range(year: int, month: int) -> Dict:
    """从 hospital_refund_time 表获取审核时间范围"""
    from datetime import datetime
    month_ts = int(datetime(year, month, 1).timestamp())
    rows = query_qudao_db("SELECT start_time, end_time FROM un_hospital_refund_time WHERE month_time = %s LIMIT 1", (month_ts,))
    if rows and rows[0].get('start_time') and rows[0].get('end_time'):
        st = int(rows[0]['start_time'])
        et = int(rows[0]['end_time'])
        st_str = datetime.fromtimestamp(st).strftime('%-m/%-d')
        et_str = datetime.fromtimestamp(et).strftime('%-m/%-d')
        return {'start_ts': st, 'end_ts': et, 'label': f"{month}月审核周期（{st_str}-{et_str}）"}
    # 没有配置则回退到自然月
    first_day = datetime(year, month, 1)
    import calendar
    last_day_num = calendar.monthrange(year, month)[1]
    last_day = datetime(year, month, last_day_num, 23, 59, 59)
    return {
        'start_ts': int(first_day.timestamp()),
        'end_ts': int(last_day.timestamp()),
        'label': f"{month}月（{month}/1-{month}/{last_day_num}）"
    }


def query_department_stats(department_name: str, start_ts: int = None, end_ts: int = None,
                           time_label: str = '', mode: str = 'checktime') -> Dict:
    """
    查询部门业绩统计（复刻 tongji_pay.php show_tongji_detail 逻辑）

    mode:
      - 'checktime'(默认/审核业绩): status=1, 按checktime, 无split_parent过滤
      - 'addtime'(成交时间/流水): status!=3, 按addtime, split_parent=0
    """
    dept_key = department_name.upper()
    if dept_key not in DEPARTMENT_MAP:
        dept_key = DEPARTMENT_ALIASES.get(department_name, department_name)
    if dept_key not in DEPARTMENT_MAP:
        return {'error': f'未找到部门"{department_name}"，支持: {", ".join(DEPARTMENT_MAP.keys())}'}

    dept_id, dept_full_name = DEPARTMENT_MAP[dept_key]
    channel_ids = _get_department_channel_ids(dept_id)
    if not channel_ids:
        return {'error': f'部门"{dept_key}"暂无渠道配置'}

    # 加入 reflect 老渠道兼容（与PHP tongji_pay.php一致）
    reflect_map = _get_reflect_channel_ids()
    for cid in list(channel_ids):
        old_id = reflect_map.get(cid)
        if old_id and old_id not in channel_ids:
            channel_ids.append(old_id)

    import time as _time
    if start_ts is None or end_ts is None:
        from datetime import datetime
        now = datetime.now()
        if mode == 'checktime':
            # 审核模式默认取当月审核周期
            audit_range = _get_audit_time_range(now.year, now.month)
            start_ts = audit_range['start_ts']
            end_ts = audit_range['end_ts']
            time_label = audit_range['label']
        else:
            first_day = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            start_ts = int(first_day.timestamp())
            end_ts = int(_time.time())
            time_label = f"本月（{now.month}/{first_day.day}-{now.month}/{now.day}）"

    placeholders = ','.join(['%s'] * len(channel_ids))

    if mode == 'checktime':
        # 审核业绩：与 tongji_pay.php get_data_detail checktime模式一致
        sql = f"""
            SELECT p.currency_type, p.number, p.checknumber, p.addtime, p.verym_project
            FROM un_channel_paylist p
            LEFT JOIN un_channel_client c ON p.Client_Id = c.Client_Id
            WHERE p.status = 1
              AND p.checktime BETWEEN %s AND %s
              AND c.from_type IN ({placeholders})
        """
    else:
        # 成交时间：与 department_statistics.class.php 一致
        sql = f"""
            SELECT p.currency_type, p.number, p.checknumber, p.addtime, p.verym_project
            FROM un_channel_paylist p
            LEFT JOIN un_channel_client c ON p.Client_Id = c.Client_Id
            WHERE p.status != 3
              AND p.addtime BETWEEN %s AND %s
              AND p.split_parent = 0
              AND c.from_type IN ({placeholders})
        """

    params = [start_ts, end_ts] + channel_ids
    rows = query_qudao_db(sql, tuple(params))

    project_ids = _get_project_category_ids()

    stats = {
        'total_money': 0, 'total_num': 0,
        'kq_money': 0, 'kq_num': 0,
        'ym_money': 0, 'ym_num': 0,
        'yk_money': 0, 'yk_num': 0,
        'kr_money': 0, 'kr_num': 0,
    }

    for row in (rows or []):
        rmb = _convert_currency_to_rmb(
            row.get('number', 0),
            row.get('currency_type', 0),
            row.get('checknumber', 0),
            row.get('addtime', 0)
        )
        cat = _categorize_pay_record(row, project_ids)
        stats['total_money'] += rmb
        stats['total_num'] += 1
        if cat != 'other':
            stats[f'{cat}_money'] += rmb
            stats[f'{cat}_num'] += 1

    for k in stats:
        if k.endswith('_money'):
            stats[k] = round(stats[k], 2)

    mode_label = '审核业绩' if mode == 'checktime' else '成交业绩'
    return {
        'department': {
            'department_id': dept_id,
            'department': dept_full_name,
            'small_dep': dept_key,
        },
        'statistics': stats,
        'time_label': time_label,
        'mode': mode_label,
    }


def query_company_stats(start_ts: int = None, end_ts: int = None,
                        time_label: str = '', mode: str = 'checktime') -> Dict:
    """查询公司整体业绩（遍历所有部门汇总）"""
    company_stats = {
        'total_money': 0, 'total_num': 0,
        'kq_money': 0, 'kq_num': 0,
        'ym_money': 0, 'ym_num': 0,
        'yk_money': 0, 'yk_num': 0,
        'kr_money': 0, 'kr_num': 0,
    }
    departments = []

    for dept_code in DEPARTMENT_MAP:
        dept_data = query_department_stats(dept_code, start_ts=start_ts, end_ts=end_ts,
                                           time_label=time_label, mode=mode)
        if dept_data.get('error'):
            continue
        dept_stats = dept_data.get('statistics', {})
        for k in company_stats:
            company_stats[k] += dept_stats.get(k, 0)
        departments.append(dept_data)

    departments.sort(key=lambda d: d.get('statistics', {}).get('total_money', 0), reverse=True)

    for k in company_stats:
        if k.endswith('_money'):
            company_stats[k] = round(company_stats[k], 2)

    mode_label = '审核业绩' if mode == 'checktime' else '成交业绩'
    return {
        'statistics': company_stats,
        'departments': departments,
        'time_label': time_label,
        'mode': mode_label,
    }


def query_hospital_deals(hospital_name: str, city: str = '', days: int = 30, limit: int = 20) -> Dict:
    """
    查询特定医院的成交项目和客单价

    Args:
        hospital_name: 医院名称（模糊匹配）
        city: 城市名称（可选，用于精确过滤）
        days: 查询天数范围
        limit: 返回数量

    Returns:
        医院成交数据
    """
    import time
    start_time = int(time.time()) - days * 24 * 3600

    # 构建搜索条件：如果有城市，则要求医院名包含城市+医院关键词
    if city:
        search_pattern = f"%{city}%{hospital_name}%"
        display_name = f"{city}{hospital_name}"
    else:
        search_pattern = f"%{hospital_name}%"
        display_name = hospital_name

    # 查询成交统计
    stats_sql = """
        SELECT
            COUNT(*) as deal_count,
            SUM(number) as total_amount,
            AVG(number) as avg_amount,
            MAX(number) as max_amount,
            MIN(CASE WHEN number > 0 THEN number END) as min_amount
        FROM un_channel_paylist
        WHERE hospital LIKE %s
        AND addtime >= %s
        AND number > 0
    """
    stats = query_qudao_db(stats_sql, (search_pattern, start_time))

    if not stats or stats[0].get('deal_count', 0) == 0:
        return {
            'hospital_name': display_name,
            'query_days': days,
            'error': f'未找到{display_name}的成交记录'
        }

    stat = stats[0]

    # 查询成交项目明细
    deals_sql = """
        SELECT
            hospital,
            item,
            number as amount,
            DATE_FORMAT(FROM_UNIXTIME(addtime), '%%Y-%%m-%%d') as deal_date,
            ClientName as client_name
        FROM un_channel_paylist
        WHERE hospital LIKE %s
        AND addtime >= %s
        AND number > 0
        ORDER BY addtime DESC
        LIMIT %s
    """
    deals = query_qudao_db(deals_sql, (search_pattern, start_time, limit))

    # 项目分布统计
    project_sql = """
        SELECT
            item as project,
            COUNT(*) as count,
            SUM(number) as total_amount,
            AVG(number) as avg_amount
        FROM un_channel_paylist
        WHERE hospital LIKE %s
        AND addtime >= %s
        AND number > 0
        GROUP BY item
        ORDER BY count DESC
        LIMIT 10
    """
    projects = query_qudao_db(project_sql, (search_pattern, start_time))

    return {
        'hospital_name': display_name,
        'query_days': days,
        'statistics': {
            'deal_count': int(stat.get('deal_count', 0) or 0),
            'total_amount': float(stat.get('total_amount', 0) or 0),
            'avg_amount': float(stat.get('avg_amount', 0) or 0),
            'max_amount': float(stat.get('max_amount', 0) or 0),
            'min_amount': float(stat.get('min_amount', 0) or 0)
        },
        'projects': [
            {
                'name': p.get('project', '未知'),
                'count': int(p.get('count', 0) or 0),
                'total_amount': float(p.get('total_amount', 0) or 0),
                'avg_amount': float(p.get('avg_amount', 0) or 0)
            }
            for p in projects
        ],
        'recent_deals': [
            {
                'hospital': d.get('hospital', ''),
                'project': d.get('item', ''),
                'amount': float(d.get('amount', 0) or 0),
                'date': d.get('deal_date', ''),
                'client': (d.get('client_name', '') or '')[:1] + '**'  # 脱敏
            }
            for d in deals
        ]
    }


def query_region_hospital_deals(city: str, days: int = 30, limit: int = 10) -> Dict:
    """按地区聚合医院成交数据排名"""
    import time as _time
    start_time = int(_time.time()) - days * 24 * 3600
    search_pattern = f"%{city}%"

    ranking_sql = """
        SELECT
            hospital,
            COUNT(*) as deal_count,
            SUM(number) as total_amount,
            AVG(number) as avg_amount,
            MAX(number) as max_amount
        FROM un_channel_paylist
        WHERE hospital LIKE %s
        AND addtime >= %s
        AND number > 0
        GROUP BY hospital
        ORDER BY total_amount DESC
        LIMIT %s
    """
    rows = query_qudao_db(ranking_sql, (search_pattern, start_time, limit))

    if not rows:
        return {
            'city': city,
            'query_days': days,
            'error': f'未找到{city}地区的成交记录'
        }

    # 项目分布（整个地区）
    project_sql = """
        SELECT
            item as project,
            COUNT(*) as count,
            SUM(number) as total_amount
        FROM un_channel_paylist
        WHERE hospital LIKE %s
        AND addtime >= %s
        AND number > 0
        GROUP BY item
        ORDER BY total_amount DESC
        LIMIT 10
    """
    projects = query_qudao_db(project_sql, (search_pattern, start_time))

    total_deals = sum(int(r.get('deal_count', 0) or 0) for r in rows)
    total_amount = sum(float(r.get('total_amount', 0) or 0) for r in rows)

    return {
        'city': city,
        'query_days': days,
        'statistics': {
            'hospital_count': len(rows),
            'total_deals': total_deals,
            'total_amount': total_amount,
            'avg_amount': round(total_amount / total_deals, 2) if total_deals > 0 else 0,
        },
        'hospital_ranking': [
            {
                'hospital': r.get('hospital', ''),
                'deal_count': int(r.get('deal_count', 0) or 0),
                'total_amount': float(r.get('total_amount', 0) or 0),
                'avg_amount': float(r.get('avg_amount', 0) or 0),
            }
            for r in rows
        ],
        'top_projects': [
            {
                'name': p.get('project', '未知'),
                'count': int(p.get('count', 0) or 0),
                'total_amount': float(p.get('total_amount', 0) or 0),
            }
            for p in projects
        ],
    }


def query_hospital_analysis(hospital_name: str = '', days: int = 30, limit: int = 10) -> Dict:
    """
    医院合作分析 - 查询医院派单、成交、转化率等数据

    Args:
        hospital_name: 医院名称（模糊匹配），为空则查询排名
        days: 查询天数范围
        limit: 返回数量

    Returns:
        医院合作分析数据
    """
    results = {
        'query_days': days,
        'hospital_name': hospital_name,
        'hospitals': [],
        'summary': {}
    }

    if hospital_name:
        # 查询指定医院的详细数据
        sql = f"""
            SELECT
                hc.userid as hospital_id,
                hc.companyname as hospital_name,
                hc.hospital_type,
                COUNT(DISTINCT ho.Client_Id) as dispatch_count,
                COUNT(DISTINCT CASE WHEN p.status = 1 THEN p.Client_Id END) as deal_count,
                COALESCE(SUM(CASE WHEN p.status = 1 THEN p.rmb_number END), 0) as total_amount,
                COUNT(DISTINCT CASE WHEN ho.arrive_status > 0 THEN ho.Client_Id END) as arrive_count
            FROM un_hospital_company hc
            LEFT JOIN un_hospital_order ho ON hc.userid = ho.hospital_id
                AND ho.send_order_time >= UNIX_TIMESTAMP(DATE_SUB(NOW(), INTERVAL {days} DAY))
            LEFT JOIN un_channel_paylist p ON ho.Client_Id = p.Client_Id AND p.hospital_id = hc.userid
                AND p.addtime >= UNIX_TIMESTAMP(DATE_SUB(NOW(), INTERVAL {days} DAY))
            WHERE hc.companyname LIKE %s
              AND hc.companyname NOT LIKE '%%韩国业务部%%'
            GROUP BY hc.userid
            ORDER BY dispatch_count DESC
            LIMIT {limit}
        """
        hospitals = query_qudao_db(sql, (f"%{hospital_name}%",))
    else:
        # 查询医院排名（按成交额）
        sql = f"""
            SELECT
                hc.userid as hospital_id,
                hc.companyname as hospital_name,
                hc.hospital_type,
                COUNT(DISTINCT ho.Client_Id) as dispatch_count,
                COUNT(DISTINCT CASE WHEN p.status = 1 THEN p.Client_Id END) as deal_count,
                COALESCE(SUM(CASE WHEN p.status = 1 THEN p.rmb_number END), 0) as total_amount,
                COUNT(DISTINCT CASE WHEN ho.arrive_status > 0 THEN ho.Client_Id END) as arrive_count
            FROM un_hospital_company hc
            JOIN un_hospital_order ho ON hc.userid = ho.hospital_id
                AND ho.send_order_time >= UNIX_TIMESTAMP(DATE_SUB(NOW(), INTERVAL {days} DAY))
            LEFT JOIN un_channel_paylist p ON ho.Client_Id = p.Client_Id AND p.hospital_id = hc.userid
                AND p.addtime >= UNIX_TIMESTAMP(DATE_SUB(NOW(), INTERVAL {days} DAY))
            WHERE hc.hospital_type NOT IN (3, 4) AND hc.islock = 0
              AND hc.companyname NOT LIKE '%%韩国业务部%%'
            GROUP BY hc.userid
            HAVING deal_count > 0
            ORDER BY total_amount DESC
            LIMIT {limit}
        """
        hospitals = query_qudao_db(sql)

    # 医院类型映射
    hospital_type_map = {
        0: '口腔', 1: '医美', 2: '医美+口腔', 3: '月子中心',
        4: '韩国', 5: '眼科', 6: '眼科+口腔', 7: '日本', 8: '东南亚'
    }

    total_dispatch = 0
    total_deal = 0
    total_amount = 0

    for h in hospitals:
        dispatch = h.get('dispatch_count', 0) or 0
        deal = h.get('deal_count', 0) or 0
        amount = float(h.get('total_amount', 0) or 0)
        arrive = h.get('arrive_count', 0) or 0

        # 计算转化率
        conversion_rate = round(deal / dispatch * 100, 1) if dispatch > 0 else 0
        arrive_rate = round(arrive / dispatch * 100, 1) if dispatch > 0 else 0

        total_dispatch += dispatch
        total_deal += deal
        total_amount += amount

        results['hospitals'].append({
            'hospital_id': h.get('hospital_id'),
            'name': h.get('hospital_name', '未知'),
            'type': hospital_type_map.get(h.get('hospital_type'), '未知'),
            'dispatch_count': dispatch,
            'arrive_count': arrive,
            'deal_count': deal,
            'total_amount': amount,
            'conversion_rate': conversion_rate,
            'arrive_rate': arrive_rate
        })

    # 汇总统计
    results['summary'] = {
        'total_hospitals': len(hospitals),
        'total_dispatch': total_dispatch,
        'total_deal': total_deal,
        'total_amount': total_amount,
        'avg_conversion_rate': round(total_deal / total_dispatch * 100, 1) if total_dispatch > 0 else 0
    }

    return results


def query_district_hospital_performance(zx_district: int, client_region: int = 0, days: int = 90) -> Dict:
    """
    意向地区医院成交能力分析 - 查询地区内医院的成交情况和二开能力

    Args:
        zx_district: 意向地区ID
        client_region: 业务线（1=医美 2=口腔 4=韩国 8=眼科）
        days: 查询天数范围

    Returns:
        地区医院成交数据、二开统计、综合评分
    """
    zx_district = int(zx_district or 0)
    client_region = int(client_region or 0)
    days = int(days or 90)

    empty_result = {
        'district_name': '',
        'hospitals': [],
        'summary': {'total_hospitals': 0, 'total_deals': 0, 'total_amount': 0, 'hospitals_with_repeat': 0, 'has_active_hospitals': False},
        'district_score': 0,
        'fallback': 'empty',
        'fallback_desc': '无数据'
    }

    # 尝试预计算数据
    if USE_PRECOMPUTED:
        pre = _read_precomputed_district_hospital(zx_district, client_region)
        if pre is not None:
            return pre

    if zx_district <= 0 and client_region <= 0:
        return empty_result

    # 获取地区名称
    district_name = get_linkage_name(zx_district) if zx_district > 0 else ''

    def _query_hospitals(filter_col: str, filter_val: int) -> tuple:
        """内部查询：按指定过滤条件查询医院成交和二开"""
        # Query A: 地区医院成交统计（排除韩国业务部）
        sql_deals = f"""
            SELECT p.hospital_id,
                   hc.companyname,
                   COUNT(DISTINCT CASE WHEN p.status=1 THEN p.id END) as deal_count,
                   COUNT(DISTINCT CASE WHEN p.status=1 THEN p.Client_Id END) as unique_clients,
                   COALESCE(SUM(CASE WHEN p.status=1 THEN p.number END),0) as total_amount
            FROM un_channel_paylist p
            JOIN un_channel_client c ON p.Client_Id = c.Client_Id
            JOIN un_hospital_company hc ON p.hospital_id = hc.userid
            WHERE c.{filter_col} = %s AND p.status = 1
              AND p.addtime >= UNIX_TIMESTAMP(DATE_SUB(NOW(), INTERVAL %s DAY))
              AND hc.companyname NOT LIKE '%%韩国业务部%%'
            GROUP BY p.hospital_id
            ORDER BY deal_count DESC
            LIMIT 10
        """
        deal_data = query_qudao_db(sql_deals, (filter_val, days))

        # Query B: 二开统计 — 同一客户在同一医院有2笔以上成交（排除韩国业务部）
        sql_repeat = f"""
            SELECT hospital_id, COUNT(*) as repeat_client_count
            FROM (
                SELECT p.hospital_id, p.Client_Id
                FROM un_channel_paylist p
                JOIN un_channel_client c ON p.Client_Id = c.Client_Id
                JOIN un_hospital_company hc ON p.hospital_id = hc.userid
                WHERE c.{filter_col} = %s AND p.status = 1
                  AND p.addtime >= UNIX_TIMESTAMP(DATE_SUB(NOW(), INTERVAL 365 DAY))
                  AND hc.companyname NOT LIKE '%%韩国业务部%%'
                GROUP BY p.hospital_id, p.Client_Id
                HAVING COUNT(DISTINCT p.id) >= 2
            ) sub
            GROUP BY hospital_id
        """
        repeat_data = query_qudao_db(sql_repeat, (filter_val,))

        return deal_data or [], repeat_data or []

    # Tier 1: 按意向地区查询
    fallback = 'none'
    fallback_desc = ''
    deal_data, repeat_data = [], []

    if zx_district > 0:
        deal_data, repeat_data = _query_hospitals('zx_District', zx_district)

    # Tier 2: 若地区结果不足3家医院，用业务线重查
    if len(deal_data) < 3 and client_region > 0:
        deal_data_cr, repeat_data_cr = _query_hospitals('client_region', client_region)
        if len(deal_data_cr) > len(deal_data):
            deal_data = deal_data_cr
            repeat_data = repeat_data_cr
            fallback = 'client_region'
            fallback_desc = f'地区医院不足，使用业务线整体数据'

    if not deal_data:
        empty_result['district_name'] = district_name
        return empty_result

    # 合并二开数据
    repeat_map = {}
    for r in repeat_data:
        hid = int(r.get('hospital_id', 0) or 0)
        repeat_map[hid] = int(r.get('repeat_client_count', 0) or 0)

    hospitals = []
    total_deals = 0
    total_amount = 0
    hospitals_with_repeat = 0

    for h in deal_data:
        hid = int(h.get('hospital_id', 0) or 0)
        deal_count = int(h.get('deal_count', 0) or 0)
        unique_clients = int(h.get('unique_clients', 0) or 0)
        amount = float(h.get('total_amount', 0) or 0)
        repeat_clients = repeat_map.get(hid, 0)
        has_repeat = repeat_clients > 0

        if has_repeat:
            hospitals_with_repeat += 1

        hospitals.append({
            'hospital_id': hid,
            'hospital_name': h.get('companyname', f'医院#{hid}'),
            'deal_count': deal_count,
            'unique_clients': unique_clients,
            'total_amount': amount,
            'avg_amount': round(amount / deal_count, 0) if deal_count > 0 else 0,
            'repeat_clients': repeat_clients,
            'has_repeat': has_repeat,
        })
        total_deals += deal_count
        total_amount += amount

    # 计算 district_score (0-100)
    score = 0
    # 有医院 = 30分
    if len(hospitals) > 0:
        score += min(len(hospitals) * 10, 30)
    # 成交量 = 40分
    if total_deals >= 20:
        score += 40
    elif total_deals >= 10:
        score += 30
    elif total_deals >= 5:
        score += 20
    elif total_deals >= 1:
        score += 10
    # 有二开 = 30分
    if hospitals_with_repeat >= 3:
        score += 30
    elif hospitals_with_repeat >= 1:
        score += 20

    summary = {
        'total_hospitals': len(hospitals),
        'total_deals': total_deals,
        'total_amount': total_amount,
        'hospitals_with_repeat': hospitals_with_repeat,
        'has_active_hospitals': len(hospitals) > 0,
    }

    return {
        'district_name': district_name,
        'hospitals': hospitals,
        'summary': summary,
        'district_score': score,
        'fallback': fallback,
        'fallback_desc': fallback_desc,
    }


def query_customer_lifecycle(days: int = 30) -> Dict:
    """
    客户生命周期分析 - 新增、到院、成交等各阶段统计

    Args:
        days: 查询天数范围

    Returns:
        客户生命周期数据
    """
    results = {
        'query_days': days,
        'stages': {},
        'conversion_funnel': [],
        'avg_cycle': {}
    }

    # 1. 各阶段客户数统计
    stages_sql = f"""
        SELECT
            COUNT(DISTINCT CASE WHEN c.RegisterTime >= UNIX_TIMESTAMP(DATE_SUB(NOW(), INTERVAL {days} DAY)) THEN c.Client_Id END) as new_customers,
            COUNT(DISTINCT CASE WHEN ho.send_order_time >= UNIX_TIMESTAMP(DATE_SUB(NOW(), INTERVAL {days} DAY)) THEN ho.Client_Id END) as dispatched,
            COUNT(DISTINCT CASE WHEN ho.arrive_time >= UNIX_TIMESTAMP(DATE_SUB(NOW(), INTERVAL {days} DAY)) AND ho.arrive_status > 0 THEN ho.Client_Id END) as arrived,
            COUNT(DISTINCT CASE WHEN p.addtime >= UNIX_TIMESTAMP(DATE_SUB(NOW(), INTERVAL {days} DAY)) AND p.status = 1 THEN p.Client_Id END) as deal_customers
        FROM un_channel_client c
        LEFT JOIN un_hospital_order ho ON c.Client_Id = ho.Client_Id
        LEFT JOIN un_channel_paylist p ON c.Client_Id = p.Client_Id
    """
    stages_data = query_qudao_db(stages_sql)

    if stages_data:
        s = stages_data[0]
        new_count = s.get('new_customers', 0) or 0
        dispatch_count = s.get('dispatched', 0) or 0
        arrive_count = s.get('arrived', 0) or 0
        deal_count = s.get('deal_customers', 0) or 0

        results['stages'] = {
            'new': {'count': new_count, 'label': '新增客户'},
            'dispatched': {'count': dispatch_count, 'label': '已派单'},
            'arrived': {'count': arrive_count, 'label': '已到院'},
            'deal': {'count': deal_count, 'label': '已成交'}
        }

        # 转化漏斗
        results['conversion_funnel'] = [
            {'stage': '新增', 'count': new_count, 'rate': 100},
            {'stage': '派单', 'count': dispatch_count, 'rate': round(dispatch_count/new_count*100, 1) if new_count > 0 else 0},
            {'stage': '到院', 'count': arrive_count, 'rate': round(arrive_count/new_count*100, 1) if new_count > 0 else 0},
            {'stage': '成交', 'count': deal_count, 'rate': round(deal_count/new_count*100, 1) if new_count > 0 else 0}
        ]

    # 2. 平均周期计算（咨询到成交的天数）
    cycle_sql = f"""
        SELECT
            AVG(DATEDIFF(FROM_UNIXTIME(p.addtime), FROM_UNIXTIME(c.RegisterTime))) as avg_register_to_deal,
            AVG(DATEDIFF(FROM_UNIXTIME(p.addtime), FROM_UNIXTIME(ho.send_order_time))) as avg_dispatch_to_deal,
            AVG(DATEDIFF(FROM_UNIXTIME(ho.arrive_time), FROM_UNIXTIME(ho.send_order_time))) as avg_dispatch_to_arrive
        FROM un_channel_paylist p
        JOIN un_channel_client c ON p.Client_Id = c.Client_Id
        JOIN un_hospital_order ho ON p.Client_Id = ho.Client_Id
        WHERE p.status = 1
          AND p.addtime >= UNIX_TIMESTAMP(DATE_SUB(NOW(), INTERVAL {days} DAY))
          AND c.RegisterTime > 0 AND ho.send_order_time > 0
    """
    cycle_data = query_qudao_db(cycle_sql)

    if cycle_data and cycle_data[0]:
        c = cycle_data[0]
        results['avg_cycle'] = {
            'register_to_deal': round(float(c.get('avg_register_to_deal') or 0), 1),
            'dispatch_to_deal': round(float(c.get('avg_dispatch_to_deal') or 0), 1),
            'dispatch_to_arrive': round(float(c.get('avg_dispatch_to_arrive') or 0), 1)
        }

    return results


def query_time_analysis(days: int = 7) -> Dict:
    """
    时间维度分析 - 进线高峰、成交时段等

    Args:
        days: 查询天数范围

    Returns:
        时间维度分析数据
    """
    results = {
        'query_days': days,
        'hourly_distribution': [],
        'daily_distribution': [],
        'weekday_comparison': {}
    }

    # 1. 每小时进线分布
    hourly_sql = f"""
        SELECT
            HOUR(FROM_UNIXTIME(RegisterTime)) as hour,
            COUNT(*) as count
        FROM un_channel_client
        WHERE RegisterTime >= UNIX_TIMESTAMP(DATE_SUB(NOW(), INTERVAL {days} DAY))
        GROUP BY hour
        ORDER BY hour
    """
    hourly_data = query_qudao_db(hourly_sql)

    # 初始化24小时数据
    hourly_counts = {i: 0 for i in range(24)}
    for h in hourly_data:
        hour = h.get('hour', 0)
        hourly_counts[hour] = h.get('count', 0)

    max_hour = max(hourly_counts, key=hourly_counts.get)
    results['hourly_distribution'] = [{'hour': h, 'count': c} for h, c in hourly_counts.items()]
    results['peak_hour'] = {'hour': max_hour, 'count': hourly_counts[max_hour]}

    # 2. 每日进线和成交趋势
    daily_sql = f"""
        SELECT
            DATE(FROM_UNIXTIME(c.RegisterTime)) as date,
            COUNT(DISTINCT c.Client_Id) as new_count,
            COUNT(DISTINCT CASE WHEN p.status = 1 THEN p.Client_Id END) as deal_count
        FROM un_channel_client c
        LEFT JOIN un_channel_paylist p ON c.Client_Id = p.Client_Id
            AND DATE(FROM_UNIXTIME(p.addtime)) = DATE(FROM_UNIXTIME(c.RegisterTime))
        WHERE c.RegisterTime >= UNIX_TIMESTAMP(DATE_SUB(NOW(), INTERVAL {days} DAY))
        GROUP BY date
        ORDER BY date DESC
        LIMIT {days}
    """
    daily_data = query_qudao_db(daily_sql)

    for d in daily_data:
        date_str = d.get('date')
        if date_str:
            results['daily_distribution'].append({
                'date': str(date_str),
                'new_count': d.get('new_count', 0),
                'deal_count': d.get('deal_count', 0)
            })

    # 3. 工作日 vs 周末对比
    weekday_sql = f"""
        SELECT
            CASE WHEN DAYOFWEEK(FROM_UNIXTIME(RegisterTime)) IN (1, 7) THEN 'weekend' ELSE 'weekday' END as day_type,
            COUNT(*) as new_count,
            COUNT(DISTINCT CASE WHEN p.status = 1 THEN p.Client_Id END) as deal_count
        FROM un_channel_client c
        LEFT JOIN un_channel_paylist p ON c.Client_Id = p.Client_Id
        WHERE c.RegisterTime >= UNIX_TIMESTAMP(DATE_SUB(NOW(), INTERVAL {days} DAY))
        GROUP BY day_type
    """
    weekday_data = query_qudao_db(weekday_sql)

    for w in weekday_data:
        day_type = w.get('day_type', 'weekday')
        results['weekday_comparison'][day_type] = {
            'new_count': w.get('new_count', 0),
            'deal_count': w.get('deal_count', 0)
        }

    return results


def query_churn_warning(kf_id: int = 0, risk_level: str = 'all', limit: int = 20, days: int = 30) -> Dict:
    """
    流失预警查询 - 识别高风险流失客户

    风险等级定义:
    - critical: 派单后>14天未跟进 (紧急)
    - high: 派单后7-14天未跟进 (高风险)
    - medium: 派单后3-7天未跟进 (中风险)
    - low: 高意向但3天内未跟进 (低风险)

    Args:
        kf_id: 客服ID，0表示查询所有
        risk_level: 风险等级筛选 (all/critical/high/medium/low)
        limit: 返回数量
        days: 查询最近多少天内派单的客户，默认30天

    Returns:
        流失预警数据
    """
    results = {
        'statistics': {},  # 风险统计
        'clients': [],     # 风险客户列表
        'kf_id': kf_id,
        'risk_level': risk_level,
        'query_days': days
    }

    # 1. 查询风险统计
    stats_sql = f"""
        SELECT
            CASE
                WHEN days_no_follow > 14 THEN 'critical'
                WHEN days_no_follow > 7 THEN 'high'
                WHEN days_no_follow > 3 THEN 'medium'
                ELSE 'low'
            END as risk_level,
            COUNT(*) as cnt
        FROM (
            SELECT
                c.Client_Id,
                c.KfId,
                DATEDIFF(NOW(), FROM_UNIXTIME(COALESCE(
                    (SELECT MAX(CMPostTime) FROM un_channel_managermessage m WHERE m.ClientId = c.Client_Id),
                    c.RegisterTime
                ))) as days_no_follow
            FROM un_channel_client c
            JOIN un_hospital_order ho ON c.Client_Id = ho.Client_Id
            LEFT JOIN un_hospital_company hc ON ho.hospital_id = hc.userid
            LEFT JOIN un_channel_paylist p ON c.Client_Id = p.Client_Id AND p.status = 1
            WHERE p.id IS NULL
              AND ho.send_order_time >= UNIX_TIMESTAMP(DATE_SUB(NOW(), INTERVAL {days} DAY))
              AND ({{kf_condition}})
        ) t
        GROUP BY risk_level
    """

    kf_condition = f"c.KfId = {kf_id}" if kf_id > 0 else "1=1"
    stats_result = query_qudao_db(stats_sql.format(kf_condition=kf_condition))

    # 初始化统计
    stats = {'critical': 0, 'high': 0, 'medium': 0, 'low': 0}
    for r in stats_result:
        level = r.get('risk_level', 'low')
        stats[level] = r.get('cnt', 0)

    results['statistics'] = {
        'critical': {'count': stats['critical'], 'label': '紧急(>14天)', 'color': 'red'},
        'high': {'count': stats['high'], 'label': '高风险(7-14天)', 'color': 'orange'},
        'medium': {'count': stats['medium'], 'label': '中风险(3-7天)', 'color': 'yellow'},
        'low': {'count': stats['low'], 'label': '低风险(≤3天)', 'color': 'green'},
        'total': stats['critical'] + stats['high'] + stats['medium'] + stats['low']
    }

    # 2. 查询风险客户列表
    risk_condition = ""
    if risk_level == 'critical':
        risk_condition = "AND days_no_follow > 14"
    elif risk_level == 'high':
        risk_condition = "AND days_no_follow > 7 AND days_no_follow <= 14"
    elif risk_level == 'medium':
        risk_condition = "AND days_no_follow > 3 AND days_no_follow <= 7"
    elif risk_level == 'low':
        risk_condition = "AND days_no_follow <= 3"
    # all = 不加条件，但优先显示高风险

    clients_sql = f"""
        SELECT
            t.Client_Id,
            t.ClientName,
            t.MobilePhone,
            t.project_name,
            t.hospital_name,
            t.hospital_type,
            t.days_since_dispatch,
            t.days_no_follow,
            t.last_follow_time,
            t.KfId,
            CASE
                WHEN t.days_no_follow > 14 THEN 'critical'
                WHEN t.days_no_follow > 7 THEN 'high'
                WHEN t.days_no_follow > 3 THEN 'medium'
                ELSE 'low'
            END as risk_level
        FROM (
            SELECT
                c.Client_Id,
                c.ClientName,
                c.MobilePhone,
                c.KfId,
                l.name as project_name,
                hc.companyname as hospital_name,
                hc.hospital_type,
                DATEDIFF(NOW(), FROM_UNIXTIME(ho.send_order_time)) as days_since_dispatch,
                DATEDIFF(NOW(), FROM_UNIXTIME(COALESCE(
                    (SELECT MAX(CMPostTime) FROM un_channel_managermessage m WHERE m.ClientId = c.Client_Id),
                    c.RegisterTime
                ))) as days_no_follow,
                (SELECT MAX(CMPostTime) FROM un_channel_managermessage m WHERE m.ClientId = c.Client_Id) as last_follow_time
            FROM un_channel_client c
            JOIN un_hospital_order ho ON c.Client_Id = ho.Client_Id
            LEFT JOIN un_hospital_company hc ON ho.hospital_id = hc.userid
            LEFT JOIN un_linkage l ON c.PlasticsIntention = l.linkageid
            LEFT JOIN un_channel_paylist p ON c.Client_Id = p.Client_Id AND p.status = 1
            WHERE p.id IS NULL
              AND ho.send_order_time >= UNIX_TIMESTAMP(DATE_SUB(NOW(), INTERVAL {days} DAY))
              AND ({{kf_condition}})
            GROUP BY c.Client_Id
        ) t
        WHERE 1=1 {risk_condition}
        ORDER BY
            FIELD(risk_level, 'critical', 'high', 'medium', 'low'),
            days_no_follow DESC
        LIMIT {limit}
    """

    clients_result = query_qudao_db(clients_sql.format(kf_condition=kf_condition))

    # 医院类型映射（来源：app_data_config.php）
    hospital_type_map = {
        0: '口腔',
        1: '医美',
        2: '医美+口腔',
        3: '月子中心',
        4: '韩国',
        5: '眼科',
        6: '眼科+口腔',
        7: '日本',
        8: '东南亚'
    }

    for r in clients_result:
        # 脱敏手机号
        phone = r.get('MobilePhone', '') or ''
        masked_phone = phone[:3] + '****' + phone[7:] if len(phone) >= 11 else phone

        # 计算最后跟进时间描述
        last_follow = r.get('last_follow_time')
        if last_follow:
            try:
                last_follow_desc = datetime.fromtimestamp(last_follow).strftime('%Y-%m-%d %H:%M')
            except:
                last_follow_desc = '未知'
        else:
            last_follow_desc = '从未跟进'

        # 获取医院类型标签
        hospital_type = r.get('hospital_type')
        hospital_name = r.get('hospital_name', '未派单') or '未派单'
        # 特殊处理：如果名称包含"通信"或"通讯"，标记为渠道
        if hospital_name and ('通信' in hospital_name or '通讯' in hospital_name):
            hospital_type_label = '渠道'
        else:
            hospital_type_label = hospital_type_map.get(hospital_type, '未知')

        results['clients'].append({
            'client_id': r.get('Client_Id'),
            'name': r.get('ClientName', '未知'),
            'phone': masked_phone,
            'project': r.get('project_name', '未知'),
            'hospital': hospital_name,
            'hospital_type': hospital_type_label,
            'days_since_dispatch': r.get('days_since_dispatch', 0),
            'days_no_follow': r.get('days_no_follow', 0),
            'last_follow': last_follow_desc,
            'risk_level': r.get('risk_level', 'low'),
            'kf_id': r.get('KfId', 0)
        })

    # 3. 生成预警建议
    critical_count = stats['critical']
    high_count = stats['high']

    if critical_count > 10:
        results['alert'] = f"⚠️ 紧急！有 {critical_count} 个客户超过14天未跟进，建议立即处理"
        results['alert_level'] = 'critical'
    elif critical_count > 0:
        results['alert'] = f"🔴 有 {critical_count} 个紧急客户需要立即跟进"
        results['alert_level'] = 'high'
    elif high_count > 0:
        results['alert'] = f"🟠 有 {high_count} 个高风险客户建议今日跟进"
        results['alert_level'] = 'medium'
    else:
        results['alert'] = "✅ 暂无紧急流失风险客户"
        results['alert_level'] = 'low'

    return results


# ============================================================
# 预计算数据读取函数
# ============================================================

def _is_precomputed_fresh(updated_at) -> bool:
    """检查预计算数据是否在有效期内"""
    if not updated_at:
        return False
    age = datetime.now() - updated_at
    return age.total_seconds() < PRECOMPUTED_MAX_AGE_HOURS * 3600


def _read_precomputed_conversion_rate(intention: int, region: int, client_region: int) -> Optional[Dict]:
    """
    从预计算表读取转化率，按3-tier顺序尝试。
    返回与 query_similar_conversion_rate 兼容的 results dict，或 None（回退实时）。
    """
    intention = int(intention or 0)
    region = int(region or 0)
    client_region = int(client_region or 0)

    if intention <= 0:
        return None

    MIN_SAMPLE = 10

    def _read_buckets(dim_type, int_id, reg_id, cr, pid):
        sql = """SELECT time_window, total, converted, rate, sample_sufficient, updated_at
                 FROM precomputed_conversion_rate
                 WHERE dimension_type=%s AND intention_id=%s AND region_id=%s
                   AND client_region=%s AND parent_id=%s
                 ORDER BY FIELD(time_window, 'd90','d180','d365','all')"""
        return query_db(sql, (dim_type, int_id, reg_id, cr, pid))

    def _pick_best(rows):
        for r in rows:
            if not _is_precomputed_fresh(r.get('updated_at')):
                return None  # stale → fall back to realtime
            if int(r.get('total', 0) or 0) >= MIN_SAMPLE:
                return r
        return None  # no sufficient sample

    def _buckets_to_dict(rows):
        result = {}
        for r in rows:
            tw = r['time_window']
            total = int(r.get('total', 0) or 0)
            converted = int(r.get('converted', 0) or 0)
            rate = float(r.get('rate', 0) or 0)
            result[tw] = {'total': total, 'converted': converted, 'rate': rate}
        return result

    results = {
        'overall': {'total': 0, 'converted': 0, 'rate': 0},
        'precise': {'total': 0, 'converted': 0, 'rate': 0},
        'confidence': 'insufficient',
        'confidence_desc': '样本不足',
        'summary': '',
        'fallback_level': 'none',
        'fallback_desc': '',
        '_from_precomputed': True,
    }

    # Tier 1: precise
    precise_rows = _read_buckets('precise', intention, region, client_region, 0)
    overall_rows = _read_buckets('overall', intention, 0, 0, 0)

    if not precise_rows and not overall_rows:
        return None  # no precomputed data

    # Check freshness
    all_rows = list(precise_rows or []) + list(overall_rows or [])
    if all_rows and not _is_precomputed_fresh(all_rows[0].get('updated_at')):
        logger.info("[precomputed] 转化率数据过期，回退实时查询")
        return None

    precise_dict = _buckets_to_dict(precise_rows) if precise_rows else {}
    overall_dict = _buckets_to_dict(overall_rows) if overall_rows else {}

    # d90 as base
    results['precise'] = precise_dict.get('d90', {'total': 0, 'converted': 0, 'rate': 0}).copy()
    results['overall'] = overall_dict.get('d90', {'total': 0, 'converted': 0, 'rate': 0}).copy()

    # Pick best window for precise
    best_precise = None
    fallback_level = 'none'
    fallback_desc = ''
    for tw, label in [('d90', '90天'), ('d180', '180天'), ('d365', '365天'), ('all', '全量')]:
        d = precise_dict.get(tw)
        if d and d['total'] >= MIN_SAMPLE:
            best_precise = d
            if tw != 'd90':
                fallback_level = 'time_expanded'
                fallback_desc = f"精准匹配已扩展至{label}(样本{d['total']})"
                results['precise'] = d.copy()
            break

    best_overall = None
    for tw, label in [('d90', '90天'), ('d180', '180天'), ('d365', '365天'), ('all', '全量')]:
        d = overall_dict.get(tw)
        if d and d['total'] >= MIN_SAMPLE:
            best_overall = d
            if tw != 'd90' and overall_dict.get('d90', {}).get('total', 0) < MIN_SAMPLE:
                if fallback_level == 'none':
                    fallback_level = 'time_expanded'
                parts = [fallback_desc] if fallback_desc else []
                parts.append(f"整体已扩展至{label}(样本{d['total']})")
                fallback_desc = '；'.join(parts)
                results['overall'] = d.copy()
            break

    # Tier 2: sibling
    if (not best_precise or best_precise['total'] < MIN_SAMPLE) and \
       (not best_overall or best_overall['total'] < MIN_SAMPLE):
        parent_info = get_intention_parent_and_siblings(intention)
        parent_id = parent_info.get('parent_id', 0)
        if parent_id > 0:
            sibling_rows = _read_buckets('sibling', 0, 0, 0, parent_id)
            if sibling_rows and _is_precomputed_fresh(sibling_rows[0].get('updated_at')):
                for r in sibling_rows:
                    if int(r.get('total', 0) or 0) >= MIN_SAMPLE:
                        fallback_level = 'parent_category'
                        parent_name = parent_info.get('parent_name', '')
                        sib_count = parent_info.get('sibling_count', 0)
                        tw = r['time_window']
                        tw_labels = {'d90': '90天', 'd180': '180天', 'd365': '365天', 'all': '全量'}
                        fallback_desc = f"已扩展至同类项目「{parent_name}」({sib_count}个子项目, {tw_labels.get(tw, tw)}, 样本{r['total']})"
                        results['overall'] = {
                            'total': int(r['total']),
                            'converted': int(r['converted']),
                            'rate': float(r['rate']),
                        }
                        best_overall = results['overall']
                        break

    # Tier 3: business line
    if (not best_precise or best_precise.get('total', 0) < MIN_SAMPLE) and \
       (not best_overall or best_overall.get('total', 0) < MIN_SAMPLE) and client_region > 0:
        bl_rows = _read_buckets('business_line', 0, 0, client_region, 0)
        if bl_rows and _is_precomputed_fresh(bl_rows[0].get('updated_at')):
            region_names = {1: '医美', 2: '口腔', 4: '韩国', 8: '眼科'}
            region_name = region_names.get(client_region, f'业务线{client_region}')
            for r in bl_rows:
                if int(r.get('total', 0) or 0) >= MIN_SAMPLE:
                    tw = r['time_window']
                    tw_labels = {'d90': '90天', 'd180': '180天', 'd365': '365天', 'all': '全量'}
                    fallback_level = 'business_line'
                    fallback_desc = f"已回退至{region_name}业务线整体({tw_labels.get(tw, tw)}, 样本{r['total']})"
                    results['overall'] = {
                        'total': int(r['total']),
                        'converted': int(r['converted']),
                        'rate': float(r['rate']),
                    }
                    best_overall = results['overall']
                    break

    results['fallback_level'] = fallback_level
    results['fallback_desc'] = fallback_desc

    # Confidence
    max_sample = max(results['precise'].get('total', 0), results['overall'].get('total', 0))
    if max_sample >= 100:
        results['confidence'], results['confidence_desc'] = 'high', '高置信度'
    elif max_sample >= 30:
        results['confidence'], results['confidence_desc'] = 'medium', '中等置信度'
    elif max_sample >= 10:
        results['confidence'], results['confidence_desc'] = 'low', '低置信度'

    # Summary
    overall_rate = results['overall'].get('rate', 0) * 100
    precise_rate = results['precise'].get('rate', 0) * 100
    precise_total = results['precise'].get('total', 0)
    overall_total = results['overall'].get('total', 0)
    if precise_total >= 30:
        if precise_rate > overall_rate:
            results['summary'] = f"该类客户转化率({precise_rate:.1f}%)高于平均({overall_rate:.1f}%)，建议重点跟进"
        else:
            results['summary'] = f"该类客户转化率({precise_rate:.1f}%)低于平均({overall_rate:.1f}%)，需加强引导"
    elif precise_total >= MIN_SAMPLE:
        results['summary'] = f"同类客户样本量偏少({precise_total})，建议参考整体转化率({overall_rate:.1f}%)"
    elif overall_total >= MIN_SAMPLE:
        results['summary'] = f"精准匹配样本不足，参考整体转化率: {overall_rate:.1f}%(样本{overall_total})"
        if fallback_desc:
            results['summary'] += f"（{fallback_desc}）"
    else:
        results['summary'] = f"样本数据不足(精准{precise_total}/整体{overall_total})"

    logger.info(f"[precomputed] 转化率命中: precise={precise_total}, overall={overall_total}, fallback={fallback_level}")
    return results


def _read_precomputed_conversion_cycle(intention: int, region: int, client_region: int) -> Optional[Dict]:
    """
    从预计算表读取成交周期，按3-tier顺序尝试。
    返回 best_stats dict (sample_size, avg, median, p25, p75, distribution等) 或 None。
    附带 fallback_level/fallback_desc。
    """
    intention = int(intention or 0)
    region = int(region or 0)
    client_region = int(client_region or 0)
    MIN_SAMPLE = 10

    if intention <= 0:
        return None

    def _read_buckets(dim_type, int_id, reg_id, cr, pid):
        sql = """SELECT time_window, sample_size, avg_days, median_days, p25_days, p75_days,
                        min_days, max_days, distribution_json, sample_sufficient, updated_at
                 FROM precomputed_conversion_cycle
                 WHERE dimension_type=%s AND intention_id=%s AND region_id=%s
                   AND client_region=%s AND parent_id=%s
                 ORDER BY FIELD(time_window, 'd90','d180','d365','all')"""
        return query_db(sql, (dim_type, int_id, reg_id, cr, pid))

    def _row_to_stats(r):
        dist = {}
        try:
            dist = json.loads(r.get('distribution_json', '{}') or '{}')
        except:
            pass
        return {
            'sample_size': int(r.get('sample_size', 0) or 0),
            'distribution': dist,
            'avg': float(r.get('avg_days', 0) or 0),
            'median': float(r.get('median_days', 0) or 0),
            'p25': float(r.get('p25_days', 0) or 0),
            'p75': float(r.get('p75_days', 0) or 0),
            'min': int(r.get('min_days', 0) or 0),
            'max': int(r.get('max_days', 0) or 0),
        }

    tw_labels = {'d90': '90天', 'd180': '180天', 'd365': '365天', 'all': '全量'}
    best_stats = None
    fallback_level = 'none'
    fallback_desc = ''

    # Tier 1: precise
    precise_rows = _read_buckets('precise', intention, region, client_region, 0)
    if precise_rows:
        if not _is_precomputed_fresh(precise_rows[0].get('updated_at')):
            return None
        for r in precise_rows:
            if int(r.get('sample_size', 0) or 0) >= MIN_SAMPLE:
                best_stats = _row_to_stats(r)
                tw = r['time_window']
                if tw != 'd90':
                    fallback_level = 'time_expanded'
                    fallback_desc = f"同项目+同地区已扩展至{tw_labels.get(tw, tw)}(样本{best_stats['sample_size']})"
                break

    if best_stats is None:
        overall_rows = _read_buckets('overall', intention, 0, 0, 0)
        if overall_rows:
            if not _is_precomputed_fresh(overall_rows[0].get('updated_at')):
                return None
            for r in overall_rows:
                if int(r.get('sample_size', 0) or 0) >= MIN_SAMPLE:
                    best_stats = _row_to_stats(r)
                    tw = r['time_window']
                    fallback_level = 'time_expanded'
                    fallback_desc = f"同项目整体({tw_labels.get(tw, tw)}, 样本{best_stats['sample_size']})"
                    break

    # Tier 2: sibling
    if best_stats is None:
        parent_info = get_intention_parent_and_siblings(intention)
        parent_id = parent_info.get('parent_id', 0)
        if parent_id > 0:
            sibling_rows = _read_buckets('sibling', 0, 0, 0, parent_id)
            if sibling_rows and _is_precomputed_fresh(sibling_rows[0].get('updated_at')):
                parent_name = parent_info.get('parent_name', '')
                sib_count = parent_info.get('sibling_count', 0)
                for r in sibling_rows:
                    if int(r.get('sample_size', 0) or 0) >= MIN_SAMPLE:
                        best_stats = _row_to_stats(r)
                        tw = r['time_window']
                        fallback_level = 'parent_category'
                        fallback_desc = f"已扩展至同类项目「{parent_name}」({sib_count}个子项目, {tw_labels.get(tw, tw)}, 样本{best_stats['sample_size']})"
                        break

    # Tier 3: business line
    if best_stats is None and client_region > 0:
        bl_rows = _read_buckets('business_line', 0, 0, client_region, 0)
        if bl_rows and _is_precomputed_fresh(bl_rows[0].get('updated_at')):
            region_names = {1: '医美', 2: '口腔', 4: '韩国', 8: '眼科'}
            region_name = region_names.get(client_region, f'业务线{client_region}')
            for r in bl_rows:
                if int(r.get('sample_size', 0) or 0) >= MIN_SAMPLE:
                    best_stats = _row_to_stats(r)
                    tw = r['time_window']
                    fallback_level = 'business_line'
                    fallback_desc = f"已回退至{region_name}业务线整体({tw_labels.get(tw, tw)}, 样本{best_stats['sample_size']})"
                    break

    if best_stats is None:
        return None

    logger.info(f"[precomputed] 成交周期命中: sample={best_stats['sample_size']}, fallback={fallback_level}")
    return {
        '_best_stats': best_stats,
        '_fallback_level': fallback_level,
        '_fallback_desc': fallback_desc,
    }


def _read_precomputed_hourly_conversion(client_region: int) -> Optional[Dict]:
    """
    从预计算表读取留电时段转化率。
    返回 {hourly_rates: {h: {total,converted,rate}}, avg_rate, total_sample} 或 None。
    """
    client_region = int(client_region or 0)

    # Tier 1: 按业务线
    sql = "SELECT register_hour, total, converted, rate, updated_at FROM precomputed_hourly_conversion WHERE client_region = %s"
    rows = query_db(sql, (client_region,))
    fallback = 'none'
    fallback_desc = ''

    if not rows:
        return None

    if not _is_precomputed_fresh(rows[0].get('updated_at')):
        return None

    total_all = sum(int(r.get('total', 0) or 0) for r in rows)

    # Tier 2: 若样本不足50，用全业务线
    if total_all < 50:
        rows = query_db(sql, (0,))
        if not rows or not _is_precomputed_fresh(rows[0].get('updated_at')):
            return None
        total_all = sum(int(r.get('total', 0) or 0) for r in rows)
        if total_all < 50:
            return None
        fallback = 'all_region'
        fallback_desc = '业务线样本不足，使用全业务线数据'

    hourly_rates = {}
    total_converted = 0
    for r in rows:
        h = int(r.get('register_hour', 0) or 0)
        t = int(r.get('total', 0) or 0)
        c = int(r.get('converted', 0) or 0)
        rate = round(float(r.get('rate', 0) or 0) * 100, 2)
        hourly_rates[h] = {'total': t, 'converted': c, 'rate': rate}
        total_converted += c

    avg_rate = round(total_converted / total_all * 100, 2) if total_all > 0 else 0

    logger.info(f"[precomputed] 留电时段命中: cr={client_region}, sample={total_all}, fallback={fallback}")
    return {
        'hourly_rates': hourly_rates,
        'avg_rate': avg_rate,
        'total_sample': total_all,
        'fallback': fallback,
        'fallback_desc': fallback_desc,
    }


def _read_precomputed_district_hospital(zx_district: int, client_region: int) -> Optional[Dict]:
    """
    从预计算表读取地区医院实力。
    返回与 query_district_hospital_performance 兼容的结果或 None。
    """
    zx_district = int(zx_district or 0)
    client_region = int(client_region or 0)

    if zx_district <= 0 and client_region <= 0:
        return None

    fallback = 'none'
    fallback_desc = ''
    row = None

    # Tier 1: 按地区
    if zx_district > 0:
        rows = query_db(
            "SELECT * FROM precomputed_district_hospital WHERE zx_district=%s AND client_region=%s",
            (zx_district, client_region)
        )
        if rows:
            row = rows[0]

    # Tier 2: 按业务线
    if (not row or int(row.get('total_hospitals', 0) or 0) < 3) and client_region > 0:
        rows2 = query_db(
            """SELECT * FROM precomputed_district_hospital
               WHERE client_region=%s ORDER BY total_deals DESC LIMIT 1""",
            (client_region,)
        )
        if rows2 and int(rows2[0].get('total_hospitals', 0) or 0) > int((row or {}).get('total_hospitals', 0) or 0):
            row = rows2[0]
            fallback = 'client_region'
            fallback_desc = '地区医院不足，使用业务线整体数据'

    if not row:
        return None

    if not _is_precomputed_fresh(row.get('updated_at')):
        return None

    hospitals = []
    try:
        hospitals = json.loads(row.get('hospitals_json', '[]') or '[]')
    except:
        pass

    district_name = row.get('district_name', '')

    logger.info(f"[precomputed] 地区医院命中: district={zx_district}, hospitals={len(hospitals)}")
    return {
        'district_name': district_name,
        'hospitals': hospitals,
        'summary': {
            'total_hospitals': int(row.get('total_hospitals', 0) or 0),
            'total_deals': int(row.get('total_deals', 0) or 0),
            'total_amount': float(row.get('total_amount', 0) or 0),
            'hospitals_with_repeat': int(row.get('hospitals_with_repeat', 0) or 0),
            'has_active_hospitals': int(row.get('total_hospitals', 0) or 0) > 0,
        },
        'district_score': int(row.get('district_score', 0) or 0),
        'fallback': fallback,
        'fallback_desc': fallback_desc,
    }


def _count_conversion(where_clause: str, params: tuple) -> Dict:
    """
    通用转化率计数辅助函数

    Args:
        where_clause: WHERE 子句（不含 WHERE 关键字）
        params: SQL参数

    Returns:
        {'total': int, 'converted': int, 'rate': float}
    """
    sql = f"""
        SELECT COUNT(*) as total_count,
               SUM(CASE WHEN Status = 1 THEN 1 ELSE 0 END) as converted_count
        FROM un_channel_client
        WHERE {where_clause}
    """
    data = query_qudao_db(sql, params)
    if data and data[0]:
        total = data[0].get('total_count', 0) or 0
        converted = data[0].get('converted_count', 0) or 0
        rate = converted / total if total > 0 else 0
        return {'total': total, 'converted': converted, 'rate': rate}
    return {'total': 0, 'converted': 0, 'rate': 0}


def _bucketed_time_query(where_base: str, params: tuple) -> Dict:
    """
    分桶SQL：一次查出90/180/365/全量计数，避免多次查询

    Args:
        where_base: 基础 WHERE 子句（不含时间条件，不含 WHERE 关键字）
        params: SQL参数

    Returns:
        {'d90': {...}, 'd180': {...}, 'd365': {...}, 'all': {...}}
    """
    sql = f"""
        SELECT
            COUNT(*) as total_all,
            SUM(CASE WHEN Status = 1 THEN 1 ELSE 0 END) as converted_all,
            SUM(CASE WHEN RegisterTime >= UNIX_TIMESTAMP(DATE_SUB(NOW(), INTERVAL 365 DAY)) THEN 1 ELSE 0 END) as total_365,
            SUM(CASE WHEN RegisterTime >= UNIX_TIMESTAMP(DATE_SUB(NOW(), INTERVAL 365 DAY)) AND Status = 1 THEN 1 ELSE 0 END) as converted_365,
            SUM(CASE WHEN RegisterTime >= UNIX_TIMESTAMP(DATE_SUB(NOW(), INTERVAL 180 DAY)) THEN 1 ELSE 0 END) as total_180,
            SUM(CASE WHEN RegisterTime >= UNIX_TIMESTAMP(DATE_SUB(NOW(), INTERVAL 180 DAY)) AND Status = 1 THEN 1 ELSE 0 END) as converted_180,
            SUM(CASE WHEN RegisterTime >= UNIX_TIMESTAMP(DATE_SUB(NOW(), INTERVAL 90 DAY)) THEN 1 ELSE 0 END) as total_90,
            SUM(CASE WHEN RegisterTime >= UNIX_TIMESTAMP(DATE_SUB(NOW(), INTERVAL 90 DAY)) AND Status = 1 THEN 1 ELSE 0 END) as converted_90
        FROM un_channel_client
        WHERE {where_base}
    """
    data = query_qudao_db(sql, params)
    if not data or not data[0]:
        empty = {'total': 0, 'converted': 0, 'rate': 0}
        return {'d90': empty.copy(), 'd180': empty.copy(), 'd365': empty.copy(), 'all': empty.copy()}

    row = data[0]
    result = {}
    for key, t_col, c_col in [('d90', 'total_90', 'converted_90'),
                                ('d180', 'total_180', 'converted_180'),
                                ('d365', 'total_365', 'converted_365'),
                                ('all', 'total_all', 'converted_all')]:
        total = int(row.get(t_col, 0) or 0)
        converted = int(row.get(c_col, 0) or 0)
        rate = converted / total if total > 0 else 0
        result[key] = {'total': total, 'converted': converted, 'rate': rate}
    return result


def _pick_best_window(buckets: Dict, min_sample: int = 10) -> tuple:
    """
    从分桶结果中选取首个样本 >= min_sample 的时间窗口

    Returns:
        (data_dict, window_label) 如 ({'total':..}, '180天') 或 (None, '')
    """
    windows = [
        ('d90', '90天'),
        ('d180', '180天'),
        ('d365', '365天'),
        ('all', '全量'),
    ]
    for key, label in windows:
        if buckets[key]['total'] >= min_sample:
            return buckets[key], label
    # 全量也不够则返回全量数据（可能为0）
    return buckets['all'], '全量'


def _calculate_cycle_stats(days_list: list) -> Dict:
    """
    纯Python计算成交周期统计信息

    Args:
        days_list: 排序后的成交周期天数列表

    Returns:
        包含分布、均值、中位数、百分位等统计信息
    """
    if not days_list:
        return {
            'sample_size': 0,
            'distribution': {'0-7': 0, '8-14': 0, '15-30': 0, '31-60': 0, '61-90': 0, '90+': 0},
            'avg': 0, 'median': 0, 'p25': 0, 'p75': 0,
            'min': 0, 'max': 0
        }

    sorted_days = sorted(days_list)
    n = len(sorted_days)

    # 分布统计
    distribution = {'0-7': 0, '8-14': 0, '15-30': 0, '31-60': 0, '61-90': 0, '90+': 0}
    for d in sorted_days:
        if d <= 7:
            distribution['0-7'] += 1
        elif d <= 14:
            distribution['8-14'] += 1
        elif d <= 30:
            distribution['15-30'] += 1
        elif d <= 60:
            distribution['31-60'] += 1
        elif d <= 90:
            distribution['61-90'] += 1
        else:
            distribution['90+'] += 1

    # 百分位计算
    def percentile(data, p):
        k = (len(data) - 1) * (p / 100.0)
        f = int(k)
        c = f + 1
        if c >= len(data):
            return data[-1]
        return data[f] + (k - f) * (data[c] - data[f])

    avg_val = sum(sorted_days) / n
    median_val = percentile(sorted_days, 50)
    p25_val = percentile(sorted_days, 25)
    p75_val = percentile(sorted_days, 75)

    return {
        'sample_size': n,
        'distribution': distribution,
        'avg': round(avg_val, 1),
        'median': round(median_val, 1),
        'p25': round(p25_val, 1),
        'p75': round(p75_val, 1),
        'min': sorted_days[0],
        'max': sorted_days[-1]
    }


def _bucketed_cycle_query(where_base: str, params: tuple) -> Dict:
    """
    一条SQL查出所有已成交客户的周期天数，Python端按注册时间分桶

    Args:
        where_base: 基础 WHERE 子句（不含 WHERE 关键字）
        params: SQL参数

    Returns:
        {'d90': stats, 'd180': stats, 'd365': stats, 'all': stats}
    """
    sql = f"""
        SELECT
            DATEDIFF(FROM_UNIXTIME(p.addtime), FROM_UNIXTIME(c.RegisterTime)) as cycle_days,
            c.RegisterTime
        FROM un_channel_client c
        JOIN un_channel_paylist p ON c.Client_Id = p.Client_Id
        WHERE {where_base}
          AND c.Status = 1 AND p.status = 1
          AND c.RegisterTime > 0 AND p.addtime > c.RegisterTime
    """
    data = query_qudao_db(sql, params)
    if not data:
        empty = _calculate_cycle_stats([])
        return {'d90': empty.copy(), 'd180': empty.copy(), 'd365': empty.copy(), 'all': empty.copy()}

    import time as _time
    now_ts = int(_time.time())
    cutoffs = {
        'd90': now_ts - 90 * 86400,
        'd180': now_ts - 180 * 86400,
        'd365': now_ts - 365 * 86400,
    }

    # 按注册时间分桶收集 cycle_days
    buckets = {'d90': [], 'd180': [], 'd365': [], 'all': []}
    for row in data:
        cycle_days = int(row.get('cycle_days', 0) or 0)
        reg_time = int(row.get('RegisterTime', 0) or 0)
        if cycle_days < 0:
            continue
        buckets['all'].append(cycle_days)
        for key, cutoff in cutoffs.items():
            if reg_time >= cutoff:
                buckets[key].append(cycle_days)

    return {key: _calculate_cycle_stats(days) for key, days in buckets.items()}


def _pick_best_cycle_window(buckets: Dict, min_sample: int = 10) -> tuple:
    """
    从周期分桶结果中选取首个 sample_size >= min_sample 的时间窗口

    Returns:
        (stats_dict, window_label) 如 ({'sample_size':..}, '180天') 或 (empty_stats, '全量')
    """
    windows = [
        ('d90', '90天'),
        ('d180', '180天'),
        ('d365', '365天'),
        ('all', '全量'),
    ]
    for key, label in windows:
        if buckets[key]['sample_size'] >= min_sample:
            return buckets[key], label
    return buckets['all'], '全量'


def query_register_hour_conversion(client_region: int = 0, register_hour: int = -1, client_id: int = 0) -> Dict:
    """
    留电时段转化率分析 - 按注册时间的小时分组统计转化率

    Args:
        client_region: 业务线（1=医美 2=口腔 4=韩国 8=眼科）
        register_hour: 当前客户的留电小时（0-23），-1表示未知
        client_id: 当前客户ID（排除自己）

    Returns:
        各时段转化率、当前客户时段排名等
    """
    client_region = int(client_region or 0)
    register_hour = int(register_hour if register_hour is not None else -1)
    client_id = int(client_id or 0)

    empty_result = {
        'hourly_rates': {},
        'customer_hour': register_hour,
        'customer_hour_rate': 0,
        'hour_rank': 'mid',
        'avg_rate': 0,
        'total_sample': 0,
        'fallback': 'none',
        'fallback_desc': ''
    }

    # 尝试预计算数据
    if USE_PRECOMPUTED:
        pre = _read_precomputed_hourly_conversion(client_region)
        if pre is not None:
            hourly_rates = pre['hourly_rates']
            avg_rate = pre['avg_rate']
            total_all = pre['total_sample']
            # 重新计算排名（与原逻辑一致）
            sorted_hours = sorted(hourly_rates.keys(), key=lambda h: hourly_rates[h]['rate'], reverse=True)
            top_hours = set(sorted_hours[:8])
            mid_hours = set(sorted_hours[8:16])
            hour_rank = 'mid'
            customer_hour_rate = 0
            if register_hour >= 0 and register_hour in hourly_rates:
                customer_hour_rate = hourly_rates[register_hour]['rate']
                if register_hour in top_hours:
                    hour_rank = 'top'
                elif register_hour in mid_hours:
                    hour_rank = 'mid'
                else:
                    hour_rank = 'low'
            return {
                'hourly_rates': hourly_rates,
                'customer_hour': register_hour,
                'customer_hour_rate': customer_hour_rate,
                'hour_rank': hour_rank,
                'avg_rate': avg_rate,
                'total_sample': total_all,
                'fallback': pre['fallback'],
                'fallback_desc': pre['fallback_desc'],
            }

    # Tier 1: 按业务线过滤（限近365天，减少扫描量）
    exclude_clause = f"AND c.Client_Id != {int(client_id)}" if client_id > 0 else ""
    time_limit = "AND c.RegisterTime >= UNIX_TIMESTAMP(DATE_SUB(NOW(), INTERVAL 365 DAY))"
    sql_tier1 = f"""
        SELECT HOUR(FROM_UNIXTIME(c.RegisterTime)) as reg_hour,
               COUNT(*) as total,
               SUM(CASE WHEN c.Status = 1 THEN 1 ELSE 0 END) as converted
        FROM un_channel_client c
        WHERE c.RegisterTime > 0 AND c.client_region = %s {time_limit} {exclude_clause}
        GROUP BY reg_hour
        ORDER BY reg_hour
    """
    data = query_qudao_db(sql_tier1, (client_region,))
    total_all = sum(int(r.get('total', 0) or 0) for r in data) if data else 0

    fallback = 'none'
    fallback_desc = ''

    # Tier 2: 若样本不足50，去掉业务线过滤
    if total_all < 50:
        sql_tier2 = f"""
            SELECT HOUR(FROM_UNIXTIME(c.RegisterTime)) as reg_hour,
                   COUNT(*) as total,
                   SUM(CASE WHEN c.Status = 1 THEN 1 ELSE 0 END) as converted
            FROM un_channel_client c
            WHERE c.RegisterTime > 0 {time_limit} {exclude_clause}
            GROUP BY reg_hour
            ORDER BY reg_hour
        """
        data = query_qudao_db(sql_tier2)
        total_all = sum(int(r.get('total', 0) or 0) for r in data) if data else 0
        fallback = 'all_region'
        fallback_desc = '业务线样本不足，使用全业务线数据'

    if not data or total_all < 50:
        empty_result['fallback'] = fallback or 'empty'
        empty_result['fallback_desc'] = fallback_desc or '样本不足'
        return empty_result

    # 构建24小时转化率表
    hourly_rates = {}
    total_converted = 0
    for row in data:
        h = int(row.get('reg_hour', 0) or 0)
        t = int(row.get('total', 0) or 0)
        c = int(row.get('converted', 0) or 0)
        rate = round(c / t * 100, 2) if t > 0 else 0
        hourly_rates[h] = {'total': t, 'converted': c, 'rate': rate}
        total_converted += c

    avg_rate = round(total_converted / total_all * 100, 2) if total_all > 0 else 0

    # 按转化率排序，分高中低三档（各8小时）
    sorted_hours = sorted(hourly_rates.keys(), key=lambda h: hourly_rates[h]['rate'], reverse=True)
    top_hours = set(sorted_hours[:8])
    mid_hours = set(sorted_hours[8:16])
    # bottom_hours = set(sorted_hours[16:])

    # 当前客户时段排名
    hour_rank = 'mid'  # 默认中
    customer_hour_rate = 0
    if register_hour >= 0 and register_hour in hourly_rates:
        customer_hour_rate = hourly_rates[register_hour]['rate']
        if register_hour in top_hours:
            hour_rank = 'top'
        elif register_hour in mid_hours:
            hour_rank = 'mid'
        else:
            hour_rank = 'low'

    return {
        'hourly_rates': hourly_rates,
        'customer_hour': register_hour,
        'customer_hour_rate': customer_hour_rate,
        'hour_rank': hour_rank,
        'avg_rate': avg_rate,
        'total_sample': total_all,
        'fallback': fallback,
        'fallback_desc': fallback_desc
    }


def query_conversion_cycle_analysis(intention: int, region: int, client_id: int = 0,
                                     client_region: int = 0, register_time: int = 0) -> Dict:
    """
    成交周期分析 - 分析同类客户从注册到成交的时间周期

    三级回退策略（与转化率一致）：
    - Tier 1: 同项目+同地区，时间窗口扩展
    - Tier 2: 父类目子项目
    - Tier 3: 业务线

    Args:
        intention: 意向项目ID
        region: 意向地区ID
        client_id: 当前客户ID（排除自己）
        client_region: 业务线（1=医美 2=口腔 4=韩国 8=眼科）
        register_time: 当前客户注册时间戳

    Returns:
        包含周期分布、黄金窗口、客户定位、预测建议等
    """
    import time as _time
    MIN_SAMPLE = 10

    # 确保参数为整数（数据库可能返回字符串）
    intention = int(intention or 0)
    region = int(region or 0)
    client_id = int(client_id or 0)
    client_region = int(client_region or 0)
    register_time = int(register_time or 0)

    empty_result = {
        'distribution': {'0-7': 0, '8-14': 0, '15-30': 0, '31-60': 0, '61-90': 0, '90+': 0},
        'statistics': {'median': 0, 'avg': 0, 'p25': 0, 'p75': 0, 'min': 0, 'max': 0, 'sample_size': 0},
        'golden_window': {'start': 0, 'end': 0, 'concentration': 0, 'description': '样本不足，无法计算'},
        'current_customer': {'days_since_register': 0, 'position': 'unknown', 'position_desc': '无数据', 'percentile': 0},
        'prediction': {'optimal_window': '未知', 'urgency': 'low', 'recommendation': '样本不足，建议持续跟进', 'expected_conversion_time': '未知'},
        'fallback_level': 'none',
        'fallback_desc': '',
        'confidence': 'insufficient',
        'confidence_desc': '样本不足'
    }

    # 尝试预计算数据
    _precomputed_hit = False
    if USE_PRECOMPUTED:
        pre = _read_precomputed_conversion_cycle(intention, region, client_region)
        if pre is not None:
            best_stats = pre['_best_stats']
            fallback_level = pre['_fallback_level']
            fallback_desc = pre['_fallback_desc']
            _precomputed_hit = True

    if not _precomputed_hit:

        best_stats = None
        fallback_level = 'none'
        fallback_desc = ''

    # ---- Tier 1: 同项目+同地区 ----
    if not _precomputed_hit and intention > 0:
        precise_base = "c.PlasticsIntention = %s AND (c.zx_District = %s OR c.client_region = %s) AND c.Client_Id != %s"
        precise_params = (intention, region, region, client_id)
        precise_buckets = _bucketed_cycle_query(precise_base, precise_params)
        precise_stats, precise_window = _pick_best_cycle_window(precise_buckets, MIN_SAMPLE)

        if precise_stats['sample_size'] >= MIN_SAMPLE:
            best_stats = precise_stats
            if precise_window != '90天':
                fallback_level = 'time_expanded'
                fallback_desc = f"同项目+同地区已扩展至{precise_window}(样本{precise_stats['sample_size']})"
        else:
            # 仅项目
            overall_base = "c.PlasticsIntention = %s AND c.Client_Id != %s"
            overall_params = (intention, client_id)
            overall_buckets = _bucketed_cycle_query(overall_base, overall_params)
            overall_stats, overall_window = _pick_best_cycle_window(overall_buckets, MIN_SAMPLE)

            if overall_stats['sample_size'] >= MIN_SAMPLE:
                best_stats = overall_stats
                fallback_level = 'time_expanded'
                fallback_desc = f"同项目整体({overall_window}, 样本{overall_stats['sample_size']})"

    # ---- Tier 2: 父类目回退 ----
    if best_stats is None and intention > 0:
        parent_info = get_intention_parent_and_siblings(intention)
        sibling_ids = parent_info.get('sibling_ids', [])
        parent_name = parent_info.get('parent_name', '')

        if sibling_ids and len(sibling_ids) > 1:
            placeholders = ','.join(['%s'] * len(sibling_ids))
            sibling_base = f"c.PlasticsIntention IN ({placeholders}) AND c.Client_Id != %s"
            sibling_params = tuple(sibling_ids) + (client_id,)
            sibling_buckets = _bucketed_cycle_query(sibling_base, sibling_params)
            sibling_stats, sibling_window = _pick_best_cycle_window(sibling_buckets, MIN_SAMPLE)

            if sibling_stats['sample_size'] >= MIN_SAMPLE:
                best_stats = sibling_stats
                fallback_level = 'parent_category'
                fallback_desc = f"已扩展至同类项目「{parent_name}」({len(sibling_ids)}个子项目, {sibling_window}, 样本{sibling_stats['sample_size']})"

    # ---- Tier 3: 业务线回退 ----
    if best_stats is None and client_region > 0:
        region_names = {1: '医美', 2: '口腔', 4: '韩国', 8: '眼科'}
        region_name = region_names.get(client_region, f'业务线{client_region}')

        region_base = "c.client_region = %s AND c.Client_Id != %s"
        region_params = (client_region, client_id)
        region_buckets = _bucketed_cycle_query(region_base, region_params)
        region_stats, region_window = _pick_best_cycle_window(region_buckets, MIN_SAMPLE)

        if region_stats['sample_size'] >= MIN_SAMPLE:
            best_stats = region_stats
            fallback_level = 'business_line'
            fallback_desc = f"已回退至{region_name}业务线整体({region_window}, 样本{region_stats['sample_size']})"

    # 无足够样本
    if best_stats is None or best_stats['sample_size'] < MIN_SAMPLE:
        return empty_result

    # 置信度
    sample = best_stats['sample_size']
    if sample >= 100:
        confidence, confidence_desc = 'high', '高置信度'
    elif sample >= 30:
        confidence, confidence_desc = 'medium', '中等置信度'
    else:
        confidence, confidence_desc = 'low', '低置信度'

    # 黄金窗口 = p25 ~ p75
    p25 = best_stats['p25']
    p75 = best_stats['p75']
    window_start = int(round(p25))
    window_end = int(round(p75))

    # 计算黄金窗口内成交占比（从原始分布推算）
    dist = best_stats['distribution']
    total_sample = best_stats['sample_size']
    in_window_count = 0
    # 遍历分布桶，累加落在 [window_start, window_end] 的数量（近似）
    bucket_ranges = [('0-7', 0, 7), ('8-14', 8, 14), ('15-30', 15, 30),
                     ('31-60', 31, 60), ('61-90', 61, 90), ('90+', 91, 9999)]
    for bname, bstart, bend in bucket_ranges:
        count = dist.get(bname, 0)
        if count == 0:
            continue
        # 桶与窗口有交集则按重叠比例计入
        overlap_start = max(bstart, window_start)
        overlap_end = min(bend, window_end)
        if overlap_start <= overlap_end:
            bucket_width = bend - bstart + 1
            overlap_width = overlap_end - overlap_start + 1
            in_window_count += count * (overlap_width / bucket_width)

    concentration = round(in_window_count / total_sample * 100, 1) if total_sample > 0 else 0

    golden_window = {
        'start': window_start,
        'end': window_end,
        'concentration': concentration,
        'description': f"{window_start}-{window_end}天内成交占比{concentration}%"
    }

    # 当前客户定位
    now_ts = int(_time.time())
    days_since_register = 0
    position = 'unknown'
    position_desc = '无注册时间'
    customer_percentile = 0

    if register_time and register_time > 0:
        days_since_register = (now_ts - register_time) // 86400

        # 计算百分位：已成交客户中有多少人在更短时间内成交
        # 使用分布数据近似
        shorter_count = 0
        for bname, bstart, bend in bucket_ranges:
            count = dist.get(bname, 0)
            if count == 0:
                continue
            if bend <= days_since_register:
                shorter_count += count
            elif bstart <= days_since_register:
                bucket_width = bend - bstart + 1
                portion = (days_since_register - bstart + 1) / bucket_width
                shorter_count += count * portion

        customer_percentile = round(shorter_count / total_sample * 100, 1) if total_sample > 0 else 0

        if days_since_register < window_start:
            position = 'before_window'
            position_desc = f'距黄金窗口还有{window_start - days_since_register}天，处于早期培育阶段'
        elif days_since_register <= window_end:
            position = 'in_window'
            position_desc = f'正处于黄金窗口({window_start}-{window_end}天)内，是最佳转化时机'
        elif customer_percentile < 90:
            position = 'after_window'
            position_desc = f'已过黄金窗口{days_since_register - window_end}天，但仍有转化机会(超过{customer_percentile:.0f}%的同类成交周期)'
        else:
            position = 'beyond_p75'
            position_desc = f'已超过{customer_percentile:.0f}%同类客户的成交周期，需特别关注'

    current_customer = {
        'days_since_register': days_since_register,
        'position': position,
        'position_desc': position_desc,
        'percentile': customer_percentile
    }

    # 成交预测
    median = best_stats['median']
    if position == 'before_window':
        urgency = 'medium'
        recommendation = f'客户注册{days_since_register}天，同类客户中位成交周期{median:.0f}天。建议在第{window_start}-{window_end}天加强跟进'
        expected_time = f'预计{window_start - days_since_register}天后进入最佳转化期'
    elif position == 'in_window':
        urgency = 'high'
        remaining = window_end - days_since_register
        recommendation = f'客户正处于黄金转化期！同类客户{concentration}%在此阶段成交。剩余窗口约{remaining}天，建议立即加强跟进'
        expected_time = f'当前即为最佳转化期，剩余约{remaining}天'
    elif position == 'after_window':
        urgency = 'medium'
        recommendation = f'已过最佳转化期{days_since_register - window_end}天，但仍有{100 - customer_percentile:.0f}%的同类客户在此之后成交。建议调整策略，重点突破顾虑点'
        expected_time = f'已过黄金窗口，需加大跟进力度'
    elif position == 'beyond_p75':
        urgency = 'low'
        recommendation = f'已超过{customer_percentile:.0f}%同类客户成交周期。建议降低优先级或尝试特殊策略（如大幅优惠、活动邀约）'
        expected_time = f'超出常规周期，转化难度较大'
    else:
        urgency = 'low'
        recommendation = '无注册时间数据，建议持续跟进'
        expected_time = '未知'

    prediction = {
        'optimal_window': f'注册后第{window_start}-{window_end}天',
        'urgency': urgency,
        'recommendation': recommendation,
        'expected_conversion_time': expected_time
    }

    return {
        'distribution': dist,
        'statistics': {
            'median': best_stats['median'],
            'avg': best_stats['avg'],
            'p25': best_stats['p25'],
            'p75': best_stats['p75'],
            'min': best_stats['min'],
            'max': best_stats['max'],
            'sample_size': best_stats['sample_size']
        },
        'golden_window': golden_window,
        'current_customer': current_customer,
        'prediction': prediction,
        'fallback_level': fallback_level,
        'fallback_desc': fallback_desc,
        'confidence': confidence,
        'confidence_desc': confidence_desc
    }


def query_similar_conversion_rate(intention: int, region: int, client_id: int = 0,
                                   from_type: int = 0, kf_id: int = 0,
                                   client_region: int = 0) -> Dict:
    """
    多维度同类客户转化率分析（带三级自动回退）

    回退策略：
    - Tier 1: 扩大时间窗口 90→180→365→全量
    - Tier 2: 回退到父类目（un_linkage.parentid 下的所有子项目）
    - Tier 3: 回退到业务线（client_region: 1=医美 2=口腔 4=韩国 8=眼科）

    Args:
        intention: 意向项目ID
        region: 意向地区ID
        client_id: 当前客户ID（排除自己）
        from_type: 来源类型
        kf_id: 客服ID
        client_region: 业务线（1=医美 2=口腔 4=韩国 8=眼科），用于Tier 3回退

    Returns:
        分层的转化率数据，含 fallback_level 和 fallback_desc
    """
    MIN_SAMPLE = 10

    results = {
        'overall': {'total': 0, 'converted': 0, 'rate': 0},
        'precise': {'total': 0, 'converted': 0, 'rate': 0},
        'channel': {'total': 0, 'converted': 0, 'rate': 0},
        'kf': {'total': 0, 'converted': 0, 'rate': 0},
        'trend_30d': {'total': 0, 'converted': 0, 'rate': 0},
        'confidence': 'insufficient',
        'confidence_desc': '样本不足',
        'summary': '',
        'fallback_level': 'none',
        'fallback_desc': ''
    }

    def get_confidence(sample_size):
        if sample_size >= 100:
            return ('high', '高置信度')
        elif sample_size >= 30:
            return ('medium', '中等置信度')
        elif sample_size >= 10:
            return ('low', '低置信度')
        else:
            return ('insufficient', '样本不足')

    # 尝试预计算数据（channel/kf/trend_30d 仍走实时）
    if USE_PRECOMPUTED:
        pre = _read_precomputed_conversion_rate(intention, region, client_region)
        if pre is not None:
            results.update(pre)
            # channel/kf/trend_30d 保留实时查询
            if from_type and from_type > 0:
                results['channel'] = _count_conversion(
                    "PlasticsIntention = %s AND from_type = %s AND RegisterTime >= UNIX_TIMESTAMP(DATE_SUB(NOW(), INTERVAL 90 DAY)) AND Client_Id != %s",
                    (intention, from_type, client_id)
                )
            if kf_id and kf_id > 0:
                results['kf'] = _count_conversion(
                    "KfId = %s AND RegisterTime >= UNIX_TIMESTAMP(DATE_SUB(NOW(), INTERVAL 90 DAY)) AND Client_Id != %s",
                    (kf_id, client_id)
                )
            results['trend_30d'] = _count_conversion(
                "PlasticsIntention = %s AND (zx_District = %s OR client_region = %s) AND RegisterTime >= UNIX_TIMESTAMP(DATE_SUB(NOW(), INTERVAL 30 DAY)) AND Client_Id != %s",
                (intention, region, region, client_id)
            )
            return results

    # ---- Tier 1: 分桶查询精准匹配（同项目+同地区）和整体（仅项目）----
    precise_base = "PlasticsIntention = %s AND (zx_District = %s OR client_region = %s) AND Client_Id != %s"
    precise_params = (intention, region, region, client_id)
    precise_buckets = _bucketed_time_query(precise_base, precise_params)

    overall_base = "PlasticsIntention = %s AND Client_Id != %s"
    overall_params = (intention, client_id)
    overall_buckets = _bucketed_time_query(overall_base, overall_params)

    # 选取最佳时间窗口
    precise_data, precise_window = _pick_best_window(precise_buckets, MIN_SAMPLE)
    overall_data, overall_window = _pick_best_window(overall_buckets, MIN_SAMPLE)

    # 90天原始数据保留为基准
    results['precise'] = precise_buckets['d90'].copy()
    results['overall'] = overall_buckets['d90'].copy()

    # 判断是否需要时间扩展
    best_precise = precise_data
    best_overall = overall_data
    fallback_level = 'none'
    fallback_desc = ''

    if precise_buckets['d90']['total'] < MIN_SAMPLE and precise_data['total'] >= MIN_SAMPLE:
        fallback_level = 'time_expanded'
        fallback_desc = f"精准匹配已扩展至{precise_window}(样本{precise_data['total']})"
        results['precise'] = precise_data.copy()

    if overall_buckets['d90']['total'] < MIN_SAMPLE and overall_data['total'] >= MIN_SAMPLE:
        if fallback_level == 'none':
            fallback_level = 'time_expanded'
        fallback_desc_parts = [fallback_desc] if fallback_desc else []
        fallback_desc_parts.append(f"整体已扩展至{overall_window}(样本{overall_data['total']})")
        fallback_desc = '；'.join(fallback_desc_parts)
        results['overall'] = overall_data.copy()

    # ---- Tier 2: 父类目回退 ----
    if best_precise['total'] < MIN_SAMPLE and best_overall['total'] < MIN_SAMPLE:
        parent_info = get_intention_parent_and_siblings(intention)
        sibling_ids = parent_info.get('sibling_ids', [])
        parent_name = parent_info.get('parent_name', '')

        if sibling_ids and len(sibling_ids) > 1:
            placeholders = ','.join(['%s'] * len(sibling_ids))
            sibling_base = f"PlasticsIntention IN ({placeholders}) AND Client_Id != %s"
            sibling_params = tuple(sibling_ids) + (client_id,)
            sibling_buckets = _bucketed_time_query(sibling_base, sibling_params)
            sibling_data, sibling_window = _pick_best_window(sibling_buckets, MIN_SAMPLE)

            if sibling_data['total'] >= MIN_SAMPLE:
                fallback_level = 'parent_category'
                fallback_desc = f"已扩展至同类项目「{parent_name}」({len(sibling_ids)}个子项目, {sibling_window}, 样本{sibling_data['total']})"
                results['overall'] = sibling_data.copy()
                best_overall = sibling_data

    # ---- Tier 3: 业务线回退 ----
    if best_precise['total'] < MIN_SAMPLE and best_overall['total'] < MIN_SAMPLE and client_region and client_region > 0:
        region_names = {1: '医美', 2: '口腔', 4: '韩国', 8: '眼科'}
        region_name = region_names.get(client_region, f'业务线{client_region}')

        region_base = "client_region = %s AND Client_Id != %s"
        region_params = (client_region, client_id)
        region_buckets = _bucketed_time_query(region_base, region_params)
        region_data, region_window = _pick_best_window(region_buckets, MIN_SAMPLE)

        if region_data['total'] >= MIN_SAMPLE:
            fallback_level = 'business_line'
            fallback_desc = f"已回退至{region_name}业务线整体({region_window}, 样本{region_data['total']})"
            results['overall'] = region_data.copy()
            best_overall = region_data

    results['fallback_level'] = fallback_level
    results['fallback_desc'] = fallback_desc

    # ---- 渠道维度（保持90天） ----
    if from_type and from_type > 0:
        results['channel'] = _count_conversion(
            "PlasticsIntention = %s AND from_type = %s AND RegisterTime >= UNIX_TIMESTAMP(DATE_SUB(NOW(), INTERVAL 90 DAY)) AND Client_Id != %s",
            (intention, from_type, client_id)
        )

    # ---- 客服维度（保持90天） ----
    if kf_id and kf_id > 0:
        results['kf'] = _count_conversion(
            "KfId = %s AND RegisterTime >= UNIX_TIMESTAMP(DATE_SUB(NOW(), INTERVAL 90 DAY)) AND Client_Id != %s",
            (kf_id, client_id)
        )

    # ---- 近30天趋势 ----
    results['trend_30d'] = _count_conversion(
        "PlasticsIntention = %s AND (zx_District = %s OR client_region = %s) AND RegisterTime >= UNIX_TIMESTAMP(DATE_SUB(NOW(), INTERVAL 30 DAY)) AND Client_Id != %s",
        (intention, region, region, client_id)
    )

    # ---- 置信度：取精准和整体中较大样本量判断 ----
    max_sample = max(results['precise']['total'], results['overall']['total'])
    results['confidence'], results['confidence_desc'] = get_confidence(max_sample)

    # ---- 生成分析摘要 ----
    overall_rate = results['overall']['rate'] * 100
    precise_rate = results['precise']['rate'] * 100
    precise_total = results['precise']['total']
    overall_total = results['overall']['total']

    if precise_total >= 30:
        if precise_rate > overall_rate:
            results['summary'] = f"该类客户转化率({precise_rate:.1f}%)高于平均({overall_rate:.1f}%)，建议重点跟进"
        else:
            results['summary'] = f"该类客户转化率({precise_rate:.1f}%)低于平均({overall_rate:.1f}%)，需加强引导"
    elif precise_total >= MIN_SAMPLE:
        results['summary'] = f"同类客户样本量偏少({precise_total})，建议参考整体转化率({overall_rate:.1f}%)"
    elif overall_total >= MIN_SAMPLE:
        results['summary'] = f"精准匹配样本不足，参考整体转化率: {overall_rate:.1f}%(样本{overall_total})"
        if fallback_desc:
            results['summary'] += f"（{fallback_desc}）"
    else:
        results['summary'] = f"样本数据不足(精准{precise_total}/整体{overall_total})"
        if fallback_desc:
            results['summary'] += f"，{fallback_desc}"

    return results


def analyze_follow_content(records: List[Dict]) -> Dict:
    """分析跟进内容，提取意向信号"""
    positive_keywords = ['感兴趣', '想了解', '预约', '咨询', '想做', '考虑', '价格多少', '什么时候', '有时间', '可以']
    negative_keywords = ['不需要', '太贵', '再考虑', '暂时不', '没时间', '不想', '算了', '不方便']

    positive_count = 0
    negative_count = 0
    total_records = len(records)

    # 检查最近活动
    recent_activity = False
    if records:
        latest_time = records[0].get('time', 0)
        if latest_time:
            try:
                days_since = (datetime.now() - datetime.fromtimestamp(latest_time)).days
                recent_activity = days_since <= 7
            except:
                pass

    for record in records:
        content = record.get('content', '') or ''
        for kw in positive_keywords:
            if kw in content:
                positive_count += 1
                break
        for kw in negative_keywords:
            if kw in content:
                negative_count += 1
                break

    return {
        'total_records': total_records,
        'positive_signals': positive_count,
        'negative_signals': negative_count,
        'recent_activity': recent_activity,
        'engagement_score': min(total_records * 10, 100)
    }


def calculate_conversion_probability(customer: Dict, follow_analysis: Dict,
                                     similar_rate: Dict, orders: List,
                                     cycle_analysis: Dict = None,
                                     register_hour_data: Dict = None,
                                     district_hospital_data: Dict = None) -> Dict:
    """
    计算客户成交概率

    评分维度（7维）：
    1. 客户状态 (20%)
    2. 跟进活跃度 (15%)
    3. 意向信号 (15%)
    4. 相似客户转化率 (15%)
    5. 成交周期匹配 (10%)
    6. 留电时段转化率 (10%)
    7. 地区医院能力 (15%)
    """
    # 已成交
    if customer.get('Status') == 1 or orders:
        total_amount = sum(float(o.get('true_number', 0) or o.get('number', 0) or 0) for o in orders)
        return {
            'probability': 100,
            'level': 'A',
            'level_desc': '已成交',
            'reasons': [f'该客户已有{len(orders)}笔成交记录，总金额¥{total_amount:.0f}'],
            'suggestion': '维护老客户关系，挖掘升单机会'
        }

    score = 0
    reasons = []

    # 1. 客户状态评分 (20%)
    status = customer.get('Status', 0)
    if status == 0:
        status_score = 50
        reasons.append("客户状态：跟进中")
    elif status == 2:
        status_score = 10
        reasons.append("客户状态：已取消（低概率）")
    else:
        status_score = 30
    score += status_score * 0.20

    # 2. 跟进活跃度评分 (15%)
    engagement = follow_analysis.get('engagement_score', 0)
    if follow_analysis.get('recent_activity'):
        engagement += 20
        reasons.append("近7天有跟进活动")
    if follow_analysis.get('total_records', 0) > 5:
        reasons.append(f"已跟进{follow_analysis['total_records']}次，参与度高")
    score += min(engagement, 100) * 0.15

    # 3. 意向信号评分 (15%)
    positive = follow_analysis.get('positive_signals', 0)
    negative = follow_analysis.get('negative_signals', 0)
    if positive > negative:
        signal_score = min(70 + positive * 10, 100)
        reasons.append(f"意向积极：{positive}个正面信号")
    elif negative > positive:
        signal_score = max(30 - negative * 10, 0)
        reasons.append(f"意向消极：{negative}个负面信号")
    else:
        signal_score = 50
    score += signal_score * 0.15

    # 4. 相似客户转化率 (15%) - 使用多维度数据
    precise = similar_rate.get('precise', {})
    overall = similar_rate.get('overall', {})
    confidence = similar_rate.get('confidence', 'insufficient')

    if precise.get('total', 0) >= 10:
        similar_conversion = float(precise['rate'] or 0) * 100
        score += similar_conversion * 0.15
        conf_label = f"[{similar_rate.get('confidence_desc', '')}]"
        reasons.append(f"同类客户转化率：{similar_conversion:.1f}% (样本{precise['total']}) {conf_label}")
    elif overall.get('total', 0) >= 10:
        overall_conversion = float(overall['rate'] or 0) * 100
        score += overall_conversion * 0.15
        fallback_info = similar_rate.get('fallback_desc', '')
        reason_text = f"该项目整体转化率：{overall_conversion:.1f}% (样本{overall['total']})"
        if fallback_info:
            reason_text += f" [{fallback_info}]"
        reasons.append(reason_text)
    else:
        score += 30 * 0.15
        fallback_info = similar_rate.get('fallback_desc', '')
        default_msg = "同类客户样本不足，使用默认值30%"
        if fallback_info:
            default_msg += f" [{fallback_info}]"
        reasons.append(default_msg)

    # 添加分析摘要
    if similar_rate.get('summary'):
        reasons.append(similar_rate['summary'])

    # 5. 成交周期匹配评分 (10%)
    if cycle_analysis and cycle_analysis.get('statistics', {}).get('sample_size', 0) >= 10:
        position = cycle_analysis.get('current_customer', {}).get('position', 'unknown')
        if position == 'in_window':
            cycle_score = 90
            reasons.append(f"周期匹配：正处于黄金窗口内，最佳转化时机")
        elif position == 'before_window':
            cycle_score = 65
            reasons.append(f"周期匹配：尚在早期阶段，可持续培育")
        elif position == 'after_window':
            cycle_score = 40
            reasons.append(f"周期匹配：已过黄金窗口，但仍有机会")
        elif position == 'beyond_p75':
            cycle_score = 25
            reasons.append(f"周期匹配：超过大部分同类成交周期")
        else:
            cycle_score = 50
        score += cycle_score * 0.10
    else:
        score += 50 * 0.10

    # 6. 留电时段转化率评分 (10%)
    if register_hour_data and register_hour_data.get('total_sample', 0) >= 50:
        hour_rank = register_hour_data.get('hour_rank', 'mid')
        customer_hour = register_hour_data.get('customer_hour', -1)
        customer_hour_rate = register_hour_data.get('customer_hour_rate', 0)
        avg_rate = register_hour_data.get('avg_rate', 0)
        if hour_rank == 'top':
            hour_score = 80
            rank_label = '高转化时段'
        elif hour_rank == 'mid':
            hour_score = 50
            rank_label = '中等时段'
        else:
            hour_score = 25
            rank_label = '低转化时段'
        score += hour_score * 0.10
        if customer_hour >= 0:
            reasons.append(f"留电时段：{customer_hour}点（{rank_label}，转化率{customer_hour_rate}%，均值{avg_rate}%）")
    else:
        score += 50 * 0.10

    # 7. 地区医院能力评分 (15%)
    if district_hospital_data and district_hospital_data.get('summary', {}).get('has_active_hospitals'):
        district_score = district_hospital_data.get('district_score', 0)
        score += district_score * 0.15
        total_hospitals = district_hospital_data.get('summary', {}).get('total_hospitals', 0)
        total_deals = district_hospital_data.get('summary', {}).get('total_deals', 0)
        hospitals_with_repeat = district_hospital_data.get('summary', {}).get('hospitals_with_repeat', 0)
        # 评价文本
        if district_score >= 70:
            strength = '强'
        elif district_score >= 40:
            strength = '中等'
        else:
            strength = '较弱'
        reasons.append(f"地区医院：该地区{total_hospitals}家医院有成交，近3月{total_deals}笔（{strength}）")
        if hospitals_with_repeat > 0:
            reasons.append(f"二开能力：{hospitals_with_repeat}家医院有客户二次成交记录")
    else:
        score += 30 * 0.15
        if district_hospital_data:
            reasons.append("地区医院：该地区暂无成交记录")

    # 确定等级
    if score >= 70:
        level, level_desc = 'A', '高意向'
        suggestion = '立即跟进，可推荐优质医院和优惠活动，尽快安排预约'
    elif score >= 50:
        level, level_desc = 'B', '中等意向'
        suggestion = '保持定期跟进，了解顾虑点，推送案例和优惠'
    elif score >= 30:
        level, level_desc = 'C', '低意向'
        suggestion = '轻度维护，发送资讯保持联系，等待主动咨询'
    else:
        level, level_desc = 'D', '极低意向'
        suggestion = '加入长期培育池，重大活动时批量触达'

    return {
        'probability': round(score, 1),
        'level': level,
        'level_desc': level_desc,
        'reasons': reasons,
        'suggestion': suggestion
    }


def generate_follow_scripts(customer: Dict, probability: Dict, follow_records: List,
                            orders: List, success_cases: List, failure_cases: List) -> List[Dict]:
    """
    根据客户情况生成跟进话术建议

    Args:
        customer: 客户信息
        probability: 成交概率分析结果
        follow_records: 跟进记录
        orders: 成交记录
        success_cases: 成功案例
        failure_cases: 失败案例

    Returns:
        话术建议列表，每个包含 scenario(场景) 和 script(话术)
    """
    scripts = []
    level = probability.get('level', 'D')
    prob_value = probability.get('probability', 0)
    customer_name = customer.get('ClientName', '顾客')

    # 获取最近跟进内容
    recent_content = ''
    has_appointment = False
    appointment_hospital = ''
    if follow_records:
        recent_content = follow_records[0].get('Content', '') or ''
        for r in follow_records[:3]:
            content = r.get('Content', '') or ''
            if '预约' in content or '派单' in content:
                has_appointment = True
                # 提取医院名
                import re
                hospital_match = re.search(r'医院[：:]\s*(.+?)[\n\r]', content)
                if hospital_match:
                    appointment_hospital = hospital_match.group(1)
                break

    # === 已成交客户话术 ===
    if orders:
        total_amount = sum(float(o.get('true_number', 0) or o.get('number', 0) or 0) for o in orders)
        last_hospital = orders[0].get('hospital', '') if orders else ''

        scripts.append({
            'scenario': '回访关怀',
            'script': f'{customer_name}您好，我是您的专属客服。您之前在{last_hospital}做的项目恢复得怎么样了？有任何问题随时联系我~'
        })
        scripts.append({
            'scenario': '升单推荐',
            'script': f'{customer_name}您好！感谢您一直以来的信任。最近我们有一些老客户专属优惠活动，想到您可能会感兴趣，方便了解一下吗？'
        })
        scripts.append({
            'scenario': '转介绍',
            'script': f'{customer_name}您好，您之前的治疗效果不错，如果身边有朋友也有这方面需求，可以推荐给我们哦，我们有老带新优惠~'
        })

    # === 高意向客户话术 (A级) ===
    elif level == 'A':
        if has_appointment:
            scripts.append({
                'scenario': '预约确认',
                'script': f'{customer_name}您好，提醒您{appointment_hospital}的预约时间快到了，届时记得带好相关资料，有什么需要提前准备的可以问我~'
            })
            scripts.append({
                'scenario': '到院引导',
                'script': f'{customer_name}您好，{appointment_hospital}的地址和乘车路线我已经整理好了，需要我发给您吗？到院后可以直接报您的姓名。'
            })
        else:
            scripts.append({
                'scenario': '促成预约',
                'script': f'{customer_name}您好，您上次咨询的项目我已经帮您了解清楚了。这周医生有档期，我帮您预约一个面诊时间？现场可以看具体方案和价格。'
            })

        scripts.append({
            'scenario': '限时优惠',
            'script': f'{customer_name}您好，您关注的项目最近有限时活动，优惠力度挺大的，活动月底截止，要不要我帮您锁定名额？'
        })

        if success_cases:
            hospital = success_cases[0].get('hospital', '医院')
            scripts.append({
                'scenario': '案例参考',
                'script': f'{customer_name}您好，跟您情况类似的顾客在{hospital}做完效果很好，我可以给您看看案例做参考，您看方便吗？'
            })

    # === 中等意向客户话术 (B级) ===
    elif level == 'B':
        scripts.append({
            'scenario': '了解顾虑',
            'script': f'{customer_name}您好，之前聊到的项目您还在考虑吗？方便说说您主要担心哪方面呢？价格、效果还是恢复期？我帮您针对性解答~'
        })
        scripts.append({
            'scenario': '价格引导',
            'script': f'{customer_name}您好，理解您想多比较比较。我们这边可以先做个免费的检查评估，出具体方案后价格也会更透明，您看行吗？'
        })
        scripts.append({
            'scenario': '活动提醒',
            'script': f'{customer_name}您好，近期我们有专家会诊活动，可以免费咨询，名额有限，要不要帮您预留一个？'
        })

    # === 低意向客户话术 (C级) ===
    elif level == 'C':
        scripts.append({
            'scenario': '轻度维护',
            'script': f'{customer_name}您好，好久没联系了，近期口腔有什么不适吗？我们可以免费做个检查~'
        })
        scripts.append({
            'scenario': '节日问候',
            'script': f'{customer_name}您好，[节日]快乐！注意饮食健康，口腔有任何问题随时联系我~'
        })

    # === 极低意向客户话术 (D级) ===
    else:
        if failure_cases:
            scripts.append({
                'scenario': '备注建议',
                'script': f'⚠️ 此类客户历史转化率较低，建议暂缓跟进，标记为"长期培育"，活动期间批量触达即可。'
            })
        scripts.append({
            'scenario': '批量触达',
            'script': f'{customer_name}您好，我们医院新引进了先进设备/推出了周年庆优惠，如有需要可以随时联系我~'
        })

    return scripts


def query_customer_full_analysis(client_id: int) -> Dict:
    """
    完整的客户分析 - 整合所有数据（含韩国/国内客户类型分析）

    Args:
        client_id: 客户ID

    Returns:
        包含客户信息、类型判断、跟进记录、成交记录、成交概率的完整分析
    """
    import time as _time
    from concurrent.futures import ThreadPoolExecutor, as_completed
    _t0 = _time.time()

    # 1. 查询客户基本信息
    customer = query_customer_info(client_id)
    if not customer:
        return {'error': f'未找到客户 ID: {client_id}'}

    # 2. 判断客户类型（韩国/国内、医美/口腔/眼科）
    customer_type_info = get_customer_type_info(customer)
    logger.info(f"[perf] client={client_id} step1-2 基本信息+类型 {_time.time()-_t0:.2f}s")

    # ---- 第一批并行查询：跟进/成交/派单 ----
    with ThreadPoolExecutor(max_workers=3) as pool:
        fut_follow = pool.submit(query_follow_records, client_id)
        fut_orders = pool.submit(query_order_records, client_id)
        fut_hospital = pool.submit(query_hospital_orders, client_id)
        follow_records = fut_follow.result(timeout=15)
        orders = fut_orders.result(timeout=15)
        hospital_orders = fut_hospital.result(timeout=15)
    logger.info(f"[perf] client={client_id} step3-5 跟进+成交+派单(并行) {_time.time()-_t0:.2f}s")

    # 6. 韩国客户专属：查询档案、同行人员、预约/日程
    archives = []
    archive_users = []
    appointments = []

    if customer_type_info['is_korean']:
        archives = query_customer_archives(client_id)
        if archives:
            archive_users = query_archive_users(archives[0]['archives_id'])
            appointments = query_customer_appointments(client_id, archives[0]['archives_id'])
        else:
            appointments = query_customer_appointments(client_id)

    # 7. 根据客户类型分析流程状态
    if customer_type_info['is_korean']:
        flow_status = analyze_korean_customer_status(customer, archives, appointments, hospital_orders)
    else:
        flow_status = analyze_domestic_customer_status(customer, hospital_orders, follow_records)
    logger.info(f"[perf] client={client_id} step6-7 韩国专属+流程 {_time.time()-_t0:.2f}s")

    # ---- 第二批并行查询：转化率/周期/成功案例/失败案例/留电时段/地区医院 ----
    intention_id = customer.get('PlasticsIntention', 0)
    region_id = customer.get('zx_District', 0) or customer.get('client_region', 0)
    _cr = int(customer.get('client_region', 0) or 0)
    _zx = int(customer.get('zx_District', 0) or 0)
    register_hour = datetime.fromtimestamp(int(customer.get('RegisterTime', 0) or 0)).hour if int(customer.get('RegisterTime', 0) or 0) > 0 else -1

    _pool2 = ThreadPoolExecutor(max_workers=6)
    if True:
        pool = _pool2
        fut_similar = pool.submit(query_similar_conversion_rate,
            intention=intention_id, region=region_id, client_id=client_id,
            from_type=customer.get('from_type', 0), kf_id=customer.get('KfId', 0),
            client_region=_cr)
        fut_cycle = pool.submit(query_conversion_cycle_analysis,
            intention=intention_id, region=region_id, client_id=client_id,
            client_region=_cr, register_time=customer.get('RegisterTime', 0))
        fut_success = pool.submit(query_success_cases,
            intention=intention_id, region=region_id, client_id=client_id, limit=3)
        fut_failure = pool.submit(query_failure_cases,
            intention=intention_id, region=region_id, client_id=client_id, limit=3)
        fut_hour = pool.submit(query_register_hour_conversion,
            client_region=_cr, register_hour=register_hour, client_id=client_id)
        fut_district = pool.submit(query_district_hospital_performance,
            zx_district=_zx, client_region=_cr, days=90)

        # 等待所有 future，总超时15秒
        _deadline = _time.time() + 15
        _futures_map = {
            'similar': fut_similar, 'cycle': fut_cycle,
            'success': fut_success, 'failure': fut_failure,
            'hour': fut_hour, 'district': fut_district,
        }
        for name, fut in _futures_map.items():
            remain = max(_deadline - _time.time(), 0.1)
            try:
                fut.result(timeout=remain)
            except Exception:
                logger.warning(f"[perf] client={client_id} fut_{name} 超时或异常")
            logger.info(f"[perf] client={client_id} fut_{name} done {_time.time()-_t0:.2f}s")

        # 收集结果，超时的用默认值
        try:
            similar_rate = fut_similar.result(timeout=0.01)
        except Exception:
            similar_rate = {'precise': {'total': 0, 'converted': 0, 'rate': 0}, 'overall': {'total': 0, 'converted': 0, 'rate': 0}, 'confidence': 'insufficient', 'confidence_desc': '查询超时', 'summary': '', 'fallback_level': 'timeout', 'fallback_desc': '查询超时'}
        try:
            cycle_analysis = fut_cycle.result(timeout=0.01)
        except Exception:
            cycle_analysis = {'distribution': {}, 'statistics': {'sample_size': 0}, 'golden_window': {}, 'current_customer': {'position': 'unknown'}, 'prediction': {}, 'fallback_level': 'timeout', 'fallback_desc': '查询超时', 'confidence': 'insufficient', 'confidence_desc': '查询超时'}
        try:
            success_cases = fut_success.result(timeout=0.01)
        except Exception:
            success_cases = []
        try:
            failure_cases = fut_failure.result(timeout=0.01)
        except Exception:
            failure_cases = []
        try:
            register_hour_analysis = fut_hour.result(timeout=0.01)
        except Exception:
            register_hour_analysis = {'hourly_rates': {}, 'customer_hour': register_hour, 'customer_hour_rate': 0, 'hour_rank': 'mid', 'avg_rate': 0, 'total_sample': 0, 'fallback': 'error', 'fallback_desc': '查询超时'}
        try:
            district_hospital_perf = fut_district.result(timeout=0.01)
        except Exception:
            district_hospital_perf = {'district_name': '', 'hospitals': [], 'summary': {'total_hospitals': 0, 'total_deals': 0, 'total_amount': 0, 'hospitals_with_repeat': 0, 'has_active_hospitals': False}, 'district_score': 0, 'fallback': 'error', 'fallback_desc': '查询超时'}

    logger.info(f"[perf] client={client_id} step8-10.7 全部完成 {_time.time()-_t0:.2f}s")

    # 10. 分析跟进内容（纯Python计算，极快）
    follow_analysis = analyze_follow_content(follow_records)

    # 11. 计算成交概率
    probability = calculate_conversion_probability(
        customer, follow_analysis, similar_rate, orders,
        cycle_analysis, register_hour_analysis, district_hospital_perf
    )

    # 12. 生成跟进话术
    scripts = generate_follow_scripts(
        customer=customer,
        probability=probability,
        follow_records=follow_records,
        orders=orders,
        success_cases=success_cases,
        failure_cases=failure_cases
    )

    # 格式化时间
    def format_ts(ts):
        if not ts:
            return '未知'
        try:
            if isinstance(ts, str):
                return ts  # 已经是字符串格式
            return datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M')
        except:
            return '未知'

    # 格式化预约状态
    appoint_status_map = {0: '已删除', 1: '客服待确认', 2: '医院待确认', 3: '已取消', 99: '预约成功'}

    # 整理结果
    return {
        'customer': {
            'id': customer.get('Client_Id'),
            'name': customer.get('ClientName', '未知'),
            'phone': mask_phone(customer.get('MobilePhone', '')),
            'sex': '男' if customer.get('Sex') == 1 else '女' if customer.get('Sex') == 2 else '未知',
            'age': customer.get('Age', 0),
            'intention_project': get_linkage_name(customer.get('PlasticsIntention')),
            'intention_project_id': customer.get('PlasticsIntention'),
            'intention_region': get_linkage_name(customer.get('zx_District')),
            'intention_region_id': customer.get('zx_District'),
            'from_source': get_linkage_name(customer.get('from_type')),
            'from_source_id': customer.get('from_type'),
            'status': '已成交' if customer.get('Status') == 1 else '未成交' if customer.get('Status') == 0 else '已取消',
            'register_time': format_ts(customer.get('RegisterTime')),
            'last_edit_time': format_ts(customer.get('EditTime')),
        },
        # ⭐ 新增：客户类型信息
        'customer_type': customer_type_info,
        # ⭐ 新增：流程状态分析
        'flow_status': flow_status,
        # ⭐ 新增：档案信息（韩国客户）
        'archives': [
            {
                'id': a.get('archives_id'),
                'name': a.get('archives_name', ''),
                'passport_no': a.get('passport_no', ''),
                'birth_time': format_ts(a.get('birth_time')),
                'nationality': a.get('nationality', ''),
            }
            for a in archives
        ] if archives else [],
        # ⭐ 新增：同行人员（韩国客户）
        'archive_users': [
            {
                'name': u.get('user_name', ''),
                'passport_no': u.get('passport_no', ''),
                'is_main': u.get('is_main') == 1,
            }
            for u in archive_users
        ] if archive_users else [],
        # ⭐ 新增：预约/日程信息（韩国客户）
        'appointments': [
            {
                'id': a.get('appoint_id'),
                'time': format_ts(a.get('appoint_time')),
                'status': appoint_status_map.get(a.get('status'), '未知'),
                'flight_no': a.get('flight_no', ''),
                'flight_time': format_ts(a.get('starttime')) if a.get('starttime') else '',
                'hotel': a.get('hotel', ''),
                'hospital_id': a.get('hospital_id'),
            }
            for a in appointments
        ] if appointments else [],
        # ⭐ 新增：医院派单记录
        'hospital_orders': [
            {
                'order_id': o.get('order_id'),
                'hospital_id': o.get('hospital_id'),
                'hospital_name': o.get('hospital_name') or f"医院#{o.get('hospital_id')}",
                'send_time': format_ts(o.get('send_order_time')),
                'surgery_time': format_ts(o.get('surgery_time')) if o.get('surgery_time') else '未安排',
                'surgery_status': '已完成' if o.get('surgery_status') == 10 else '未完成',
            }
            for o in hospital_orders
        ] if hospital_orders else [],
        'follow_records': [
            {
                'time': format_ts(r.get('time')),
                'content': (r.get('content', '') or '')[:200]
            }
            for r in follow_records[:5]
        ],
        'orders': [
            {
                'hospital': o.get('hospital_name', '未知'),
                'amount': float(o.get('true_number', 0) or o.get('number', 0) or 0),
                'time': format_ts(o.get('time')),
                'status': '已确认' if o.get('status') == 1 else '待确认'
            }
            for o in orders
        ],
        'analysis': probability,
        'similar_customers': {
            'precise': similar_rate.get('precise', {}),
            'overall': similar_rate.get('overall', {}),
            'kf': similar_rate.get('kf', {}),
            'trend_30d': similar_rate.get('trend_30d', {}),
            'confidence': similar_rate.get('confidence', 'insufficient'),
            'confidence_desc': similar_rate.get('confidence_desc', '样本不足'),
            'summary': similar_rate.get('summary', ''),
            'fallback_level': similar_rate.get('fallback_level', 'none'),
            'fallback_desc': similar_rate.get('fallback_desc', '')
        },
        'conversion_cycle': {
            'distribution': cycle_analysis.get('distribution', {}),
            'statistics': cycle_analysis.get('statistics', {}),
            'golden_window': cycle_analysis.get('golden_window', {}),
            'current_customer': cycle_analysis.get('current_customer', {}),
            'prediction': cycle_analysis.get('prediction', {}),
            'fallback_level': cycle_analysis.get('fallback_level', 'none'),
            'fallback_desc': cycle_analysis.get('fallback_desc', ''),
            'confidence': cycle_analysis.get('confidence', 'insufficient'),
            'confidence_desc': cycle_analysis.get('confidence_desc', '样本不足')
        },
        'success_cases': success_cases,
        'failure_cases': failure_cases,
        'follow_scripts': scripts,
        'register_hour_analysis': register_hour_analysis,
        'district_hospitals': {
            'hospitals': district_hospital_perf.get('hospitals', [])[:5],
            'summary': district_hospital_perf.get('summary', {}),
            'district_score': district_hospital_perf.get('district_score', 0),
            'district_name': district_hospital_perf.get('district_name', ''),
            'fallback': district_hospital_perf.get('fallback', 'none'),
            'fallback_desc': district_hospital_perf.get('fallback_desc', ''),
        }
    }


# ============================================================
# LLM 意图分类
# ============================================================

def parse_time_range_to_days(time_range: str) -> Optional[int]:
    """将 LLM 返回的语义化时间范围转为天数"""
    if not time_range:
        return None
    tr = time_range.strip()
    if tr in ("今天", "今日"):
        return 1
    if tr in ("本周", "这周"):
        return 7
    if tr in ("本月", "这个月"):
        return get_current_month_days()
    if tr in ("季度", "本季度", "三个月", "最近三个月"):
        return 90
    if tr in ("半年", "六个月", "最近半年"):
        return 180
    if tr in ("一年", "全年", "年度", "今年"):
        return 365
    # "最近N天" / "N天"
    m = re.search(r'(\d+)\s*[天日]', tr)
    if m:
        return min(int(m.group(1)), 365)
    return None


def classify_intent_llm(question: str) -> Optional[Dict]:
    """用 LLM 做语义意图分类，失败返回 None（触发关键词兜底）"""
    try:
        # 处理【当前客户】上下文前缀
        user_question = question
        context_client_id = None
        has_client_context = False
        if '【当前客户】' in question:
            has_client_context = True
            uq_match = re.search(r'用户问题[：:]\s*(.+)', question, re.DOTALL)
            if uq_match:
                user_question = uq_match.group(1).strip()
            ctx_id_match = re.search(r'\(ID[：:](\d+)\)', question)
            if ctx_id_match:
                context_client_id = int(ctx_id_match.group(1))

        response = requests.post(
            f"{LLM_CONFIG['api_base']}/chat/completions",
            headers={"Authorization": f"Bearer {LLM_CONFIG['api_key']}"},
            json={
                "model": LLM_CONFIG["model"],
                "messages": [
                    {"role": "system", "content": INTENT_CLASSIFICATION_PROMPT},
                    {"role": "user", "content": user_question},
                ],
                "temperature": 0.1,
                "max_tokens": 200,
            },
            timeout=8,
        )
        response.raise_for_status()
        raw = response.json()["choices"][0]["message"]["content"].strip()

        # 提取 JSON（LLM 可能包裹在 ```json ... ``` 中）
        json_match = re.search(r'\{.*\}', raw, re.DOTALL)
        if not json_match:
            logger.warning(f"[llm_intent] 无法提取JSON: {raw[:200]}")
            return None
        parsed = json.loads(json_match.group())

        # 验证 intent
        intents = parsed.get("intent", [])
        if isinstance(intents, str):
            intents = [intents]
        intents = [i for i in intents if i in VALID_INTENTS]
        if not intents:
            logger.info(f"[llm_intent] 无有效意图: {raw[:200]}")
            return None

        # 构建结果，兼容 extract_keywords 的输出格式
        result = {
            "city": parsed.get("city", "") or "",
            "district": parsed.get("district", "") or "",
            "hospital": parsed.get("hospital", "") or "",
            "doctor": parsed.get("doctor", "") or "",
            "project": parsed.get("project", "") or "",
            "intent": intents,
            "customer_id": None,
            "kf_name": parsed.get("kf_name", "") or "",
            "source_keyword": parsed.get("source_keyword", "") or "",
            "department_name": parsed.get("department_name", "") or "",
            "_has_client_context": has_client_context,
            "_hospital_from_user_q": bool(parsed.get("hospital")),
            "_from_llm": True,
        }

        # customer_id: 混合策略 — LLM 提取 + regex 兜底（数字模式 regex 更可靠）
        llm_cid = parsed.get("customer_id")
        if llm_cid and isinstance(llm_cid, (int, float)):
            result["customer_id"] = int(llm_cid)
        else:
            # regex 兜底
            cid_patterns = [
                r'(?:id|ID|Id)[是:\s]*(\d+)',
                r'(?:客户|顾客|用户)[ID|id|Id]?[是:\s]*(\d+)',
                r'(?:查询|查看|分析|查一下)[^\d]*(\d{5,})',
                r'(\d{6,})\s*(?:这个|的|这位)?(?:客户|顾客|用户)',
                r'(\d{6,})(?:的|这个)',
                r'(\d{7,})',
            ]
            for pattern in cid_patterns:
                match = re.search(pattern, user_question)
                if match:
                    result["customer_id"] = int(match.group(1))
                    break

        # 客户上下文中的 context_client_id
        if context_client_id:
            result["_context_client_id"] = context_client_id

        # time_range → query_days（供 smart_query 使用）
        time_range = parsed.get("time_range", "")
        if time_range:
            result["_time_range"] = time_range
            days = parse_time_range_to_days(time_range)
            if days:
                result["_query_days"] = days

        # limit
        limit_val = parsed.get("limit", 0)
        if limit_val and isinstance(limit_val, (int, float)) and int(limit_val) > 0:
            result["_limit"] = int(limit_val)

        # risk_level
        risk = parsed.get("risk_level", "")
        if risk and risk in ("critical", "high", "medium", "all"):
            result["_risk_level"] = risk

        logger.info(f"[llm_intent] 成功: intent={intents}, city={result['city']}, "
                     f"hospital={result['hospital']}, customer_id={result['customer_id']}")
        return result

    except requests.exceptions.Timeout:
        logger.warning("[llm_intent] LLM调用超时(8s)，回退关键词匹配")
        return None
    except Exception as e:
        logger.warning(f"[llm_intent] 异常: {type(e).__name__}: {e}")
        return None


def extract_keywords_with_llm(question: str) -> Dict[str, Any]:
    """LLM 意图分类 + 关键词匹配兜底"""
    if USE_LLM_INTENT:
        result = classify_intent_llm(question)
        if result and result.get("intent"):
            return result
    logger.info("[intent] fallback to keyword matching")
    return extract_keywords(question)


# ============================================================
# 智能查询 - 使用参数化查询，防止SQL注入
# ============================================================
def extract_keywords(question: str) -> Dict[str, Any]:
    """从问题中提取关键词
    如果问题包含【当前客户】上下文前缀，会从中提取地区等信息，
    但意图识别和customer_id提取仅基于用户实际提问部分。
    """
    keywords = {
        "city": "",
        "district": "",
        "hospital": "",
        "doctor": "",
        "project": "",
        "intent": [],
        "customer_id": None,  # 客户ID
        "kf_name": "",  # 客服姓名
        "source_keyword": "",  # 来源关键词（用于来源分析）
        "department_name": "",  # 部门代码（TEG/BCG等）
        "_has_client_context": False,  # 标记是否有客户上下文
        "_hospital_from_user_q": False,  # 标记医院名是否来自用户实际问题
    }

    # 如果包含【当前客户】前缀，分离上下文和实际问题
    # 上下文用于提取地区/项目，实际问题用于意图/customer_id识别
    user_question = question  # 用于意图识别的部分
    if '【当前客户】' in question:
        keywords["_has_client_context"] = True
        # 提取"用户问题："后面的部分作为真正的用户问题
        uq_match = re.search(r'用户问题[：:]\s*(.+)', question, re.DOTALL)
        if uq_match:
            user_question = uq_match.group(1).strip()
        # 从上下文中提取客户ID（格式: "xxx(ID:3865536)"）
        ctx_id_match = re.search(r'\(ID[：:](\d+)\)', question)
        if ctx_id_match:
            keywords["_context_client_id"] = int(ctx_id_match.group(1))

    # 提取客服姓名（支持多种格式）
    # "客服刘贞飞", "刘贞飞的跟进", "客服小王"
    kf_patterns = [
        r'客服[：:\s]*([^\s的，,。]+)',  # 客服刘贞飞, 客服：张三
        r'([^\s客服的，,。]{2,4})的跟[进单]',  # 刘贞飞的跟进/跟单
        r'([^\s客服的，,。]{2,4})(?:今日|近日|最近|今天|本周|这周)(?:的)?(?:跟进|跟单|派单|工作|业绩|成交|单量)',  # 李小璐今日跟单情况, 张静近日跟进
        r'([^\s客服的，,。]{2,4})跟[进单]情况',  # 刘贞飞跟进情况/跟单情况
        r'([^\s客服的，,。]{2,4})(?:这个月|本月|最近|今天|今日|本周|这周)的?(?:成交|业绩|单量|工作)',  # 水艺艺这个月的成交业绩
        r'([^\s客服的，,。]{2,4})的(?:成交|业绩|单量)',  # 水艺艺的成交业绩
    ]
    for pattern in kf_patterns:
        match = re.search(pattern, user_question)
        if match:
            name = match.group(1).strip()
            # 排除一些常见词
            if name not in ['医院', '医生', '客户', '情况', '数据', '最近', '今天', '本周']:
                keywords["kf_name"] = name
                break

    # 提取部门名称
    dept_codes = list(DEPARTMENT_MAP.keys())
    for dc in dept_codes:
        if dc.upper() in user_question.upper():
            keywords["department_name"] = dc if dc in DEPARTMENT_MAP else dc.upper()
            break
    if not keywords["department_name"]:
        for alias, code in DEPARTMENT_ALIASES.items():
            if alias in user_question:
                keywords["department_name"] = code
                break

    # 提取客户ID（仅从用户实际问题中提取，不从上下文前缀提取）
    customer_id_patterns = [
        r'(?:id|ID|Id)[是:\s]*(\d+)',
        r'(?:客户|顾客|用户)[ID|id|Id]?[是:\s]*(\d+)',
        r'(?:查询|查看|分析|查一下)[^\d]*(\d{5,})',
        r'(\d{6,})\s*(?:这个|的|这位)?(?:客户|顾客|用户)',
        r'(\d{6,})(?:的|这个)',
        r'^(\d{6,})\s',
        r'(\d{7,})',
    ]
    for pattern in customer_id_patterns:
        match = re.search(pattern, user_question)
        if match:
            keywords["customer_id"] = int(match.group(1))
            break

    # 城市 - 先从客户上下文提取（意向地区/注册地区），再从文本提取
    context_district_match = re.search(r'意向地区[:：]([^\s，,、]+)', question)
    context_reg_district_match = re.search(r'注册地区[:：]([^\s，,、]+)', question)
    context_location = context_district_match.group(1) if context_district_match else (
        context_reg_district_match.group(1) if context_reg_district_match else ''
    )

    cities = [
        "北京", "上海", "广州", "深圳", "杭州", "成都", "武汉", "南京", "西安", "重庆",
        "天津", "苏州", "长沙", "郑州", "东莞", "青岛", "沈阳", "宁波", "昆明", "大连",
        "福州", "厦门", "哈尔滨", "济南", "温州", "佛山", "长春", "贵阳", "常州", "石家庄",
        "南宁", "南昌", "太原", "兰州", "合肥", "海口", "乌鲁木齐", "呼和浩特", "银川",
        "潍坊", "烟台", "泉州", "嘉兴", "珠海", "中山", "惠州", "徐州", "无锡",
        "绍兴", "金华", "台州", "洛阳", "襄阳", "宜昌", "芜湖", "淄博", "威海",
        "柳州", "桂林", "三亚", "遵义", "镇江", "扬州", "邯郸", "唐山", "保定",
        "吉林", "包头", "临沂", "泰安", "秦皇岛", "廊坊", "呼伦贝尔", "赤峰"
    ]
    # 先检查上下文中的地区
    if context_location:
        keywords["city"] = context_location
    else:
        for city in cities:
            if city in question:
                keywords["city"] = city
                break

    # 区域
    districts = ["朝阳", "海淀", "东城", "西城", "丰台", "浦东", "徐汇", "黄浦", "天河", "福田",
                 "南山", "宝安", "龙岗", "罗湖", "潍城", "奎文", "寒亭", "坊子", "高新", "滨海"]
    for d in districts:
        if d in question:
            keywords["district"] = d
            break

    # 医院名 - 扩展列表，增加更多常见医院
    hospitals = [
        "维乐", "圣贝", "中诺", "美奥", "丽都", "拜尔", "欢乐", "致美", "登特", "劲松",
        "瑞泰", "佳美", "广大", "鼎植", "雅悦", "华美", "时光", "首尔丽格", "伯思立",
        "尤旦", "拜博", "美莱", "摩尔", "康贝佳", "固德", "牙博士", "优贝", "德伦",
        "瑞尔", "通策", "可恩", "松丰", "德韩", "爱康健", "拜耳", "同步", "美加",
        "新桥", "柏德", "现代", "爱齿", "小白兔", "兔博士", "欢乐", "马泷", "泰康拜博",
        "好佰年", "三叶", "贝臣", "德亚", "舒适达", "康美", "皓齿", "诺德", "博爱"
    ]
    # 优先从 user_question 匹配，避免客户上下文中的项目名（如"医美"）被误提取
    _hospital_from_user_q = False
    for h in hospitals:
        if h in user_question:
            keywords["hospital"] = h
            _hospital_from_user_q = True
            break
    # 如果 user_question 没匹配到，再从完整 question 中查找（兼容无上下文的场景）
    if not keywords["hospital"]:
        for h in hospitals:
            if h in question:
                keywords["hospital"] = h
                break

    # 如果没匹配到，尝试提取"XX口腔"/"XX医院"格式的医院名
    # 使用 user_question 而非 question，避免从客户上下文前缀中误提取（如"双颚取钛医美"中的"医美"）
    if not keywords["hospital"]:
        hospital_pattern = r'([\u4e00-\u9fa5]{2,6}?)(?:口腔|医院|整形|医美|门诊)'
        hospital_match = re.search(hospital_pattern, user_question)
        if hospital_match:
            extracted = hospital_match.group(1)
            # 排除城市名
            if extracted not in cities:
                keywords["hospital"] = extracted
                _hospital_from_user_q = True

    keywords["_hospital_from_user_q"] = _hospital_from_user_q

    # 医生名 - 提取2-4字中文名
    doctor_pattern = r'([^\u4e00-\u9fa5]|^)([\u4e00-\u9fa5]{2,4}?)(?:医生|大夫|主任|院长|专家|是|在)'
    doctor_match = re.search(doctor_pattern, question)
    if doctor_match:
        keywords["doctor"] = doctor_match.group(2)
    else:
        # 尝试直接匹配常见医生姓名格式（姓+名）
        name_pattern = r'^([\u4e00-\u9fa5]{2,4})(?:是|在|哪|怎)'
        name_match = re.search(name_pattern, question)
        if name_match:
            keywords["doctor"] = name_match.group(1)

    # 项目
    projects = ["种植", "矫正", "正畸", "洗牙", "美白", "补牙", "拔牙", "全瓷", "隐形"]
    for p in projects:
        if p in question:
            keywords["project"] = p
            break

    # 意图识别 - 基于用户实际问题（排除上下文前缀干扰）
    q = user_question
    if any(kw in q for kw in ["医院", "推荐", "哪家", "哪里做", "派什么"]):
        keywords["intent"].append("hospital_recommend")
    if any(kw in q for kw in ["医生", "专家", "大夫"]) or keywords.get("doctor"):
        keywords["intent"].append("doctor_recommend")
    if any(kw in q for kw in ["坐诊", "排班", "出诊", "上班"]):
        keywords["intent"].append("schedule")
    if any(kw in q for kw in ["预约", "挂号", "号源", "能约", "有空", "明天", "后天"]):
        keywords["intent"].append("appointment")
    if any(kw in q for kw in ["活动", "优惠", "促销", "打折", "特惠"]):
        keywords["intent"].append("promotion")
    if any(kw in q for kw in ["价格", "多少钱", "费用", "收费", "客单价", "均价"]):
        keywords["intent"].append("price")
    if any(kw in q for kw in ["离职", "去哪", "调动", "跳槽", "变动"]):
        keywords["intent"].append("career")
    if any(kw in q for kw in ["优势", "特色", "怎么样", "好不好", "介绍", "简介", "详情", "情况", "信息", "在哪", "地址", "位置", "怎么走", "怎么去"]):
        keywords["intent"].append("hospital_info")

    # 医院排名/成交查询
    # 注意：排除第一人称"我的派单"等个人工作查询场景
    _is_personal_query = any(kw in q for kw in ["我的", "我今天", "我今日", "我本周", "我最近"])
    if not _is_personal_query and any(kw in q for kw in ["成交好", "成交多", "排名", "哪些医院", "派单多", "派单量", "派单排名", "业绩好", "单量", "前十", "前五", "top"]):
        keywords["intent"].append("hospital_ranking")

    # 流失预警查询
    if any(kw in q for kw in ["流失", "预警", "风险客户", "未跟进", "没跟进", "待跟进", "超时", "丢失", "流失风险", "高风险", "紧急客户"]):
        keywords["intent"].append("churn_warning")

    # 医院合作分析
    # "成交率"/"转化率"在有客户上下文时通常指客户转化概率，不应触发医院分析
    _hospital_analysis_kws = ["医院合作", "医院分析", "医院业绩", "返款", "合作医院", "医院数据"]
    if not keywords.get("_has_client_context"):
        _hospital_analysis_kws += ["成交率", "转化率"]
    if any(kw in q for kw in _hospital_analysis_kws):
        keywords["intent"].append("hospital_analysis")

    # 特定医院成交查询 - 仅当医院名来自用户实际问题时触发，
    # 避免上下文中的医院名(如Top3推荐医院)与"成交率"等词误组合
    if keywords["hospital"] and _hospital_from_user_q and any(kw in q for kw in ["成交", "客单价", "项目", "业绩", "金额", "这个月", "本月", "最近"]):
        keywords["intent"].append("hospital_deals")

    # 客服跟进查询
    if keywords["kf_name"] and any(kw in q for kw in ["跟进", "跟单", "情况", "工作", "统计", "业绩", "成交", "单量", "派单"]):
        keywords["intent"].append("kf_stats")

    # 第一人称个人工作查询（"我的派单"、"今日我的业绩"等）→ kf_stats
    if _is_personal_query and any(kw in q for kw in ["派单", "业绩", "客户", "数据", "统计", "成交"]):
        if "kf_stats" not in keywords["intent"]:
            keywords["intent"].append("kf_stats")
        # 移除可能误触发的 hospital_ranking
        if "hospital_ranking" in keywords["intent"]:
            keywords["intent"].remove("hospital_ranking")

    # 客户生命周期分析
    if not keywords["hospital"] and any(kw in q for kw in ["生命周期", "新增客户", "今日新增", "本周新增", "到院", "转化漏斗", "漏斗", "客户统计", "多少天成交"]):
        keywords["intent"].append("customer_lifecycle")

    if not keywords["hospital"] and not keywords["kf_name"] and not keywords["department_name"] and "公司" not in q and "成交" in q and "customer_lifecycle" not in keywords["intent"]:
        keywords["intent"].append("customer_lifecycle")

    # 部门业绩查询
    if keywords["department_name"] and any(kw in q for kw in ["成交", "业绩", "数据", "情况", "统计", "单量"]):
        keywords["intent"].append("department_stats")

    # 公司整体业绩查询
    if "公司" in q and any(kw in q for kw in ["成交", "业绩", "数据", "情况", "统计", "预测", "整体"]):
        keywords["intent"].append("company_stats")

    # 时间维度分析
    if any(kw in q for kw in ["高峰", "进线", "几点", "时段", "周末", "工作日", "趋势", "每日", "每天"]):
        keywords["intent"].append("time_analysis")

    # 客户查询意图 - 仅基于用户实际问题
    if keywords["customer_id"] or any(kw in q for kw in ["顾客", "客户", "用户"]):
        if any(kw in q for kw in ["成交概率", "成交率", "转化", "可能性", "意向", "分析"]):
            keywords["intent"].append("customer_conversion")
        elif any(kw in q for kw in ["跟进", "记录", "历史"]):
            if "kf_stats" not in keywords["intent"]:
                keywords["intent"].append("customer_follow")
        elif any(kw in q for kw in ["查询", "查看", "信息", "详情"]):
            keywords["intent"].append("customer_info")

        # 仅当用户主动查客户ID时才默认为综合分析
        if keywords["customer_id"] and not any(i.startswith("customer_") for i in keywords["intent"]):
            keywords["intent"].append("customer_conversion")

    # 如果提取到医院名但没有任何意图，默认为查询医院信息
    if keywords["hospital"] and not keywords["intent"]:
        keywords["intent"].append("hospital_info")

    # 新增：来源分析意图
    # 提取来源关键词
    source_keywords_list = [
        "牙舒丽", "贝色", "美佳", "开立特", "非常", "8682", "牙齿矫正网",
        "皓齿", "医秀", "品牌100", "牙大师", "牙度网", "398口腔",
        "BCG", "BSG", "OMG", "TEG", "CDG", "MMG", "CMG",
        "头条", "快手", "抖音", "小红书", "微信", "400电话", "商务通"
    ]
    for src in source_keywords_list:
        if src in user_question:
            # 如果是部门代码且已识别为部门，不设为来源关键词
            if keywords["department_name"] and src.upper() == keywords["department_name"].upper():
                continue
            keywords["source_keyword"] = src
            break

    # 如果没有匹配预定义，尝试从用户实际问题中提取
    if not keywords["source_keyword"]:
        # 匹配"XX来源"、"XX渠道"、"XX站点"、"XX网站"
        source_pattern = r'([\u4e00-\u9fa5a-zA-Z0-9]{2,10})(?:来源|渠道|站点|网站|swt)'
        source_match = re.search(source_pattern, user_question)
        if source_match:
            extracted = source_match.group(1)
            if extracted not in ['这个', '那个', '什么', '哪个', '所有', '全部']:
                keywords["source_keyword"] = extracted

    # 来源分析意图识别 - 仅基于用户实际问题（排除上下文前缀中的"来源:TEG..."等干扰）
    if any(kw in user_question for kw in ["来源", "渠道", "站点", "注册", "进线"]):
        if any(kw in user_question for kw in ["分析", "统计", "数据", "情况", "分布", "多少", "转化", "地区", "项目", "医院", "趋势"]):
            keywords["intent"].append("source_analysis")
        elif any(kw in user_question for kw in ["对比", "比较", "vs", "VS", "和", "与"]):
            keywords["intent"].append("source_compare")
        elif any(kw in user_question for kw in ["列表", "有哪些", "都有", "所有"]):
            keywords["intent"].append("source_list")

    # 如果提到了具体来源名称+分析相关词，也触发来源分析（仅基于用户实际问题）
    if keywords["source_keyword"] and any(kw in user_question for kw in ["分析", "统计", "数据", "情况", "怎么样", "转化", "成交", "客户", "地区", "项目", "注册", "新增", "进线"]):
        if "source_analysis" not in keywords["intent"]:
            keywords["intent"].append("source_analysis")

    return keywords


def smart_query(question: str) -> Dict[str, Any]:
    """
    根据问题智能查询数据库 - 使用参数化查询防止SQL注入
    """
    keywords = extract_keywords_with_llm(question)
    results = {}

    # 意图修正：有来源关键词 + "注册/新增/进线" → 应走 source_analysis 而非 customer_lifecycle
    if keywords.get("source_keyword") and "customer_lifecycle" in keywords["intent"]:
        _q = question.lower()
        if any(kw in _q for kw in ["注册", "新增", "进线"]):
            keywords["intent"] = [i for i in keywords["intent"] if i != "customer_lifecycle"]
            if "source_analysis" not in keywords["intent"]:
                keywords["intent"].append("source_analysis")

    # 客户查询 - 优先处理
    customer_intents = ["customer_conversion", "customer_follow", "customer_info"]

    # 如果已有客户上下文（从工作台发起的对话）
    # 知识库查询（医院推荐、医生、价格、活动等）仍需查数据库
    # 全局分析类意图（customer_lifecycle、hospital_ranking 等）应跳过，让 LLM 基于上下文回答
    if keywords.get("_has_client_context"):
        # 检测"生成跟进计划"类请求 → 跳过所有查询，直接让 LLM 生成
        if re.search(r'生成第?\d*轮?跟进计划', question):
            keywords["intent"] = []
            return results

        kb_intents = {"hospital_recommend", "doctor_recommend", "schedule",
                      "hospital_info", "price", "promotion", "appointment", "career",
                      "hospital_ranking", "hospital_analysis", "hospital_deals"}
        # 过滤：只保留知识库/医院意图，丢弃纯全局分析意图（customer_lifecycle 等）
        keywords["intent"] = [i for i in keywords["intent"] if i in kb_intents]
        if not keywords["intent"]:
            # 纯上下文问题（如"成交概率多少?"、"怎么跟进?"），让 LLM 基于客户信息回答
            return results
        # 有知识库查询意图 → 跳过下面的客户查询，直接到知识库查询部分

    # kf_stats 优先于 customer 意图 — 避免"张静今日跟进情况"被 customer_follow 拦截
    if "kf_stats" in keywords["intent"] and keywords.get("kf_name"):
        kf_name = keywords['kf_name']
        query_days = 7
        if "今日" in question or "今天" in question:
            query_days = 1
        elif "本周" in question or "这周" in question:
            query_days = 7
        elif "本月" in question or "这个月" in question:
            query_days = get_current_month_days()
        else:
            days_match = re.search(r'[最近近]?(\d+)[天日]', question)
            if days_match:
                query_days = min(int(days_match.group(1)), 365)
        if keywords.get("_query_days"):
            query_days = keywords["_query_days"]
        kf_data = query_kf_stats(kf_name=kf_name, days=query_days)
        results["kf_stats"] = kf_data
        return results

    # 部门业绩查询
    if "department_stats" in keywords["intent"]:
        dept_name = keywords.get("department_name", "")
        if not dept_name:
            results["department_stats_error"] = "请指定部门名称，例如：TEG部门成交情况、BCG本月业绩"
            return results
        # 判断模式：含"成交时间"/"流水"用addtime，其他默认checktime（审核）
        mode = 'addtime' if any(kw in question for kw in ["成交时间", "流水", "录入"]) else 'checktime'
        time_range = _parse_department_time_range(question)
        # 审核模式下，"上月"/"本月"等需要用 hospital_refund_time 表的审核周期
        if mode == 'checktime':
            from datetime import datetime as _dt
            now = _dt.now()
            if any(kw in question for kw in ["上个月", "上月"]):
                m = now.month - 1 if now.month > 1 else 12
                y = now.year if now.month > 1 else now.year - 1
                audit_range = _get_audit_time_range(y, m)
                time_range = {'start_ts': audit_range['start_ts'], 'end_ts': audit_range['end_ts'], 'time_label': audit_range['label']}
            elif not any(kw in question for kw in ["今日", "今天", "昨天", "昨日", "本周", "这周", "上周", "半年"]) \
                 and not re.search(r'[最近近]?\d+[天日]', question) and not re.search(r'\d+\s*个?月', question):
                # 默认本月审核周期
                audit_range = _get_audit_time_range(now.year, now.month)
                time_range = {'start_ts': audit_range['start_ts'], 'end_ts': audit_range['end_ts'], 'time_label': audit_range['label']}
        dept_data = query_department_stats(dept_name, start_ts=time_range['start_ts'],
                                           end_ts=time_range['end_ts'],
                                           time_label=time_range['time_label'], mode=mode)
        results["department_stats"] = dept_data
        return results

    # 公司整体业绩查询
    if "company_stats" in keywords["intent"]:
        mode = 'addtime' if any(kw in question for kw in ["成交时间", "流水", "录入"]) else 'checktime'
        time_range = _parse_department_time_range(question)
        if mode == 'checktime':
            from datetime import datetime as _dt
            now = _dt.now()
            if any(kw in question for kw in ["上个月", "上月"]):
                m = now.month - 1 if now.month > 1 else 12
                y = now.year if now.month > 1 else now.year - 1
                audit_range = _get_audit_time_range(y, m)
                time_range = {'start_ts': audit_range['start_ts'], 'end_ts': audit_range['end_ts'], 'time_label': audit_range['label']}
            elif not any(kw in question for kw in ["今日", "今天", "昨天", "昨日", "本周", "这周", "上周", "半年"]) \
                 and not re.search(r'[最近近]?\d+[天日]', question) and not re.search(r'\d+\s*个?月', question):
                audit_range = _get_audit_time_range(now.year, now.month)
                time_range = {'start_ts': audit_range['start_ts'], 'end_ts': audit_range['end_ts'], 'time_label': audit_range['label']}
        company_data = query_company_stats(start_ts=time_range['start_ts'],
                                           end_ts=time_range['end_ts'],
                                           time_label=time_range['time_label'], mode=mode)
        results["company_stats"] = company_data
        return results

    # 无客户上下文时的客户查询（有客户上下文时跳过，上下文已包含客户信息）
    if not keywords.get("_has_client_context") and any(intent in keywords["intent"] for intent in customer_intents):
        if keywords.get("customer_id"):
            customer_data = query_customer_full_analysis(keywords["customer_id"])
            if 'error' not in customer_data:
                results["customer_analysis"] = customer_data
            else:
                results["customer_error"] = customer_data['error']
        else:
            results["customer_error"] = "请提供客户ID，例如：查询客户ID 4333303 的成交概率"
        return results  # 客户查询直接返回

    # 来源分析查询
    if "source_analysis" in keywords["intent"] or "source_compare" in keywords["intent"] or "source_list" in keywords["intent"]:
        if not SOURCE_ANALYSIS_AVAILABLE:
            results["source_error"] = "来源分析功能暂不可用"
            return results

        # 提取查询天数，默认本月，最大365天
        query_days = get_current_month_days()  # 默认本月
        if "今日" in question or "今天" in question:
            query_days = 1
        elif "近日" in question or "近期" in question:
            query_days = 7
        elif "本周" in question or "这周" in question:
            query_days = 7
        elif "本月" in question or "这个月" in question:
            query_days = get_current_month_days()
        elif "季度" in question or "三个月" in question:
            query_days = 90
        elif "半年" in question or "六个月" in question:
            query_days = 180
        elif "一年" in question or "全年" in question or "年度" in question:
            query_days = 365
        else:
            days_match = re.search(r'[最近近]?(\d+)[天日]', question)
            if days_match:
                query_days = min(int(days_match.group(1)), 365)  # 最大365天

        source_keyword = keywords.get('source_keyword', '')

        if "source_list" in keywords["intent"]:
            # 来源列表查询
            if source_keyword:
                sql = """
                    SELECT c.from_type, l.name as source_name, p.name as parent_name,
                           COUNT(*) as total_count,
                           SUM(CASE WHEN c.Status = 1 THEN 1 ELSE 0 END) as converted_count
                    FROM un_channel_client c
                    LEFT JOIN un_linkage l ON c.from_type = l.linkageid
                    LEFT JOIN un_linkage p ON l.parentid = p.linkageid
                    WHERE c.from_type > 0 AND l.name LIKE %s
                    GROUP BY c.from_type, l.name, p.name
                    HAVING total_count >= 100
                    ORDER BY total_count DESC
                    LIMIT 30
                """
                source_list = query_qudao_db(sql, (f"%{source_keyword}%",))
            else:
                sql = """
                    SELECT c.from_type, l.name as source_name, p.name as parent_name,
                           COUNT(*) as total_count,
                           SUM(CASE WHEN c.Status = 1 THEN 1 ELSE 0 END) as converted_count
                    FROM un_channel_client c
                    LEFT JOIN un_linkage l ON c.from_type = l.linkageid
                    LEFT JOIN un_linkage p ON l.parentid = p.linkageid
                    WHERE c.from_type > 0
                    GROUP BY c.from_type, l.name, p.name
                    HAVING total_count >= 1000
                    ORDER BY total_count DESC
                    LIMIT 30
                """
                source_list = query_qudao_db(sql)

            results["source_list"] = {
                'keyword': source_keyword or '全部',
                'sources': [
                    {
                        'id': s['from_type'],
                        'name': s['source_name'] or f"未知({s['from_type']})",
                        'parent': s['parent_name'] or '',
                        'type': source_analysis.classify_source(s['source_name'], s['parent_name']),
                        'total': s['total_count'],
                        'converted': s['converted_count'] or 0,
                        'rate': round((s['converted_count'] or 0) / s['total_count'] * 100, 2) if s['total_count'] > 0 else 0
                    }
                    for s in source_list
                ]
            }
            return results

        elif "source_compare" in keywords["intent"]:
            # 来源对比 - 需要多个来源关键词
            # 从问题中提取多个来源
            compare_sources = []
            for src in ["牙舒丽", "贝色", "美佳", "开立特", "非常", "8682", "皓齿", "医秀", "牙大师"]:
                if src in question:
                    compare_sources.append(src)

            if len(compare_sources) < 2:
                compare_sources = ["牙舒丽", "贝色"]  # 默认对比

            comparisons = []
            for src in compare_sources[:5]:
                source_ids = source_analysis.get_source_ids(src)
                if source_ids:
                    region_data = source_analysis.analyze_region_distribution(source_ids, query_days)
                    comparisons.append({
                        'source': src,
                        'total': region_data.get('total_customers', 0),
                        'converted': region_data.get('total_converted', 0),
                        'rate': region_data.get('conversion_rate', 0),
                        'top_regions': [r['province'] for r in region_data.get('regions', [])[:3]]
                    })

            results["source_compare"] = {
                'period_days': query_days,
                'comparisons': comparisons
            }
            return results

        else:
            # 来源分析
            if not source_keyword:
                results["source_error"] = "请指定要分析的来源，例如：牙舒丽来源分析、贝色渠道数据"
                return results

            source_ids = source_analysis.get_source_ids(source_keyword)
            if not source_ids:
                results["source_error"] = f"未找到包含'{source_keyword}'的来源"
                return results

            # 执行各维度分析
            source_info = source_analysis.get_all_source_info()
            source_details = [
                {
                    'id': sid,
                    'name': source_info.get(sid, {}).get('name', '未知'),
                    'type': source_analysis.classify_source(
                        source_info.get(sid, {}).get('name', ''),
                        source_info.get(sid, {}).get('parent_name', '')
                    )
                }
                for sid in source_ids
            ]

            results["source_analysis"] = {
                'source_keyword': source_keyword,
                'source_ids': source_ids,
                'source_details': source_details,
                'period_days': query_days,
                'region': source_analysis.analyze_region_distribution(source_ids, query_days),
                'project': source_analysis.analyze_project_distribution(source_ids, query_days),
                'hospital': source_analysis.analyze_hospital_distribution(source_ids, query_days),
                'trend': source_analysis.analyze_time_trend(source_ids, query_days, 'week'),
                'keywords': source_analysis.analyze_consultation_keywords(source_ids, min(query_days, 30))
            }
            return results

    # 客服跟进情况查询（kf_name 有值时已在上方优先处理，此处仅处理无 kf_name 的兜底）
    if "kf_stats" in keywords["intent"]:
        results["kf_stats_error"] = "请指定客服姓名，例如：客服刘贞飞的跟进情况"
        return results

    # 特定医院成交项目/客单价查询（优先处理）
    if "hospital_deals" in keywords["intent"]:
        hospital_name = keywords.get('hospital', '')
        city = keywords.get('city', '')

        # 提取查询天数：优先用 LLM 解析结果，否则从文本提取
        query_days = keywords.get('_query_days') or get_current_month_days()
        if not keywords.get('_query_days'):
            if "今日" in question or "今天" in question:
                query_days = 1
            elif "本周" in question or "这周" in question:
                query_days = 7
            elif "本月" in question or "这个月" in question:
                query_days = get_current_month_days()
            else:
                days_match = re.search(r'[最近近]?(\d+)[天日]', question)
                if days_match:
                    query_days = min(int(days_match.group(1)), 365)

        if hospital_name:
            deals_data = query_hospital_deals(hospital_name=hospital_name, city=city, days=query_days, limit=20)
            results["hospital_deals"] = deals_data
            return results
        elif city:
            # 按地区聚合成交排名（如"上海地区成交好的医院"）
            deals_data = query_region_hospital_deals(city=city, days=query_days, limit=10)
            results["hospital_deals"] = deals_data
            return results
        else:
            results["hospital_deals_error"] = "请指定医院名称或地区，例如：上海鼎植口腔的成交项目 / 上海地区成交好的医院"
            return results

    # 医院排名查询（按派单量）
    # 如果同时有 hospital_recommend 意图（如"推荐派单的医院"），优先走 hospital_recommend 路径
    if "hospital_ranking" in keywords["intent"] and "hospital_recommend" not in keywords["intent"]:
        area = keywords.get("city", "") or keywords.get("district", "")
        if area:
            ranking_data = query_hospital_ranking(area, limit=10)
            results["hospital_ranking"] = ranking_data
            return results  # 排名查询直接返回
        else:
            results["hospital_ranking_error"] = "请指定地区，例如：上海地区成交好的医院"
            return results

    # 流失预警查询
    if "churn_warning" in keywords["intent"]:
        # 提取风险等级筛选
        risk_level = 'all'
        if any(kw in question for kw in ["紧急", "严重", "超过14天", ">14"]):
            risk_level = 'critical'
        elif any(kw in question for kw in ["高风险", "7天以上", "一周"]):
            risk_level = 'high'
        elif any(kw in question for kw in ["中风险", "3天"]):
            risk_level = 'medium'

        # 提取客服ID（如果有）
        kf_id = 0
        kf_match = re.search(r'客服[ID]?[\s:：]?(\d+)', question)
        if kf_match:
            kf_id = int(kf_match.group(1))

        # 提取查询天数（默认7天，最多30天）
        query_days = 7
        days_match = re.search(r'[最近近]?(\d+)[天日]', question)
        if days_match:
            query_days = min(int(days_match.group(1)), 30)  # 最多30天

        churn_data = query_churn_warning(kf_id=kf_id, risk_level=risk_level, limit=20, days=query_days)
        results["churn_warning"] = churn_data
        return results

    # 医院合作分析
    if "hospital_analysis" in keywords["intent"]:
        # 有客户上下文 + 用户问"意向地区"的医院 → 用意向地区医院分析
        if keywords.get("_has_client_context") and "意向地区" in question:
            ctx_client_id = keywords.get("_context_client_id")
            if ctx_client_id:
                # 查客户的意向地区ID和业务线
                cust_sql = "SELECT zx_District, client_region FROM un_channel_client WHERE Client_Id = %s LIMIT 1"
                cust_row = query_qudao_db(cust_sql, (ctx_client_id,))
                if cust_row:
                    zx_district = int(cust_row[0].get('zx_District') or 0)
                    client_region = int(cust_row[0].get('client_region') or 0)
                    if zx_district > 0 or client_region > 0:
                        district_data = query_district_hospital_performance(
                            zx_district=zx_district, client_region=client_region, days=90
                        )
                        results["district_hospital"] = district_data
                        return results

        # 提取医院名称 - 仅使用用户主动提到的医院名，
        # 避免上下文中的Top3医院名被误用为查询条件
        hospital_name = keywords.get('hospital', '')
        if keywords.get("_has_client_context") and not keywords.get("_hospital_from_user_q"):
            hospital_name = ''  # 上下文中的医院名不用于查询，改为查整体排名

        # 提取查询天数（默认本月，最大365天）
        query_days = get_current_month_days()
        days_match = re.search(r'[最近近]?(\d+)[天日]', question)
        if days_match:
            query_days = min(int(days_match.group(1)), 365)

        hospital_data = query_hospital_analysis(hospital_name=hospital_name, days=query_days, limit=15)
        results["hospital_analysis"] = hospital_data
        return results

    # 客户生命周期分析
    if "customer_lifecycle" in keywords["intent"]:
        # 提取查询天数（默认本月，最大365天）
        query_days = get_current_month_days()
        if "今日" in question or "今天" in question:
            query_days = 1
        elif "本周" in question or "这周" in question:
            query_days = 7
        elif "本月" in question or "这个月" in question:
            query_days = get_current_month_days()
        else:
            days_match = re.search(r'[最近近]?(\d+)[天日]', question)
            if days_match:
                query_days = min(int(days_match.group(1)), 365)

        lifecycle_data = query_customer_lifecycle(days=query_days)
        results["customer_lifecycle"] = lifecycle_data
        return results

    # 时间维度分析
    if "time_analysis" in keywords["intent"]:
        # 提取查询天数（默认7天）
        query_days = 7
        days_match = re.search(r'[最近近]?(\d+)[天日]', question)
        if days_match:
            query_days = min(int(days_match.group(1)), 30)

        time_data = query_time_analysis(days=query_days)
        results["time_analysis"] = time_data
        return results

    # 清理参数，防止 LIKE 注入
    city = sanitize_like_param(keywords['city'])
    district = sanitize_like_param(keywords['district'])
    hospital = sanitize_like_param(keywords['hospital'])

    # 医院推荐 - 优先匹配客户所在地区（city_name + hospital_name + detailed_address）
    if "hospital_recommend" in keywords["intent"] or not keywords["intent"]:
        if city:
            # 有地区信息：优先按地区匹配（city_name、医院名、地址三路搜索）
            sql = """
                SELECT h.id, h.hospital_name, h.district_name, h.detailed_address,
                       h.phone, h.features, h.description, h.business_hours,
                       h.cooperation_status, h.main_projects_list
                FROM robot_kb_hospitals h
                WHERE h.cooperation_status = '合作中'
                  AND (h.city_name LIKE %s OR h.hospital_name LIKE %s OR h.detailed_address LIKE %s)
                ORDER BY
                  CASE WHEN h.hospital_name LIKE '%%口腔%%' THEN 0 ELSE 1 END,
                  h.qudao_id DESC
                LIMIT 5
            """
            like_city = f"%{city}%"
            params = (like_city, like_city, like_city)
        else:
            # 无地区信息：返回口腔医院
            sql = """
                SELECT h.id, h.hospital_name, h.district_name, h.detailed_address,
                       h.phone, h.features, h.description, h.business_hours,
                       h.cooperation_status, h.main_projects_list
                FROM robot_kb_hospitals h
                WHERE h.cooperation_status = '合作中'
                  AND h.hospital_name LIKE '%%口腔%%'
                ORDER BY h.qudao_id DESC LIMIT 5
            """
            params = ()
        results["hospitals"] = query_db(sql, params)

    # 医生推荐 + 排班 - 参数化查询
    if "doctor_recommend" in keywords["intent"] or "schedule" in keywords["intent"]:
        doctor = keywords.get("doctor", "")
        sql = """
            SELECT d.doctor_name, d.position, d.specialties,
                   h.hospital_name,
                   GROUP_CONCAT(DISTINCT
                       CONCAT(CASE ds.day_of_week
                           WHEN 1 THEN '周一' WHEN 2 THEN '周二' WHEN 3 THEN '周三'
                           WHEN 4 THEN '周四' WHEN 5 THEN '周五' WHEN 6 THEN '周六' WHEN 7 THEN '周日'
                       END, ds.time_slot) SEPARATOR '、') as schedule
            FROM robot_kb_doctors d
            JOIN robot_kb_hospital_doctors hd ON d.id = hd.doctor_id
            JOIN robot_kb_hospitals h ON hd.hospital_id = h.id
            LEFT JOIN doctor_schedules ds ON d.id = ds.doctor_id AND h.id = ds.hospital_id
            WHERE d.status = 1
              AND (%s = '' OR h.hospital_name LIKE %s)
              AND (%s = '' OR h.city_name LIKE %s)
              AND (%s = '' OR d.doctor_name LIKE %s)
            GROUP BY d.id, d.doctor_name, d.position, d.specialties, h.hospital_name
            LIMIT 10
        """
        params = (
            hospital, f"%{hospital}%" if hospital else "",
            city, f"%{city}%" if city else "",
            doctor, f"%{doctor}%" if doctor else ""
        )
        results["doctors"] = query_db(sql, params)

    # 预约情况 - 参数化查询
    if "appointment" in keywords["intent"]:
        # 确定日期过滤条件
        if "明天" in question:
            date_condition = "a.slot_date = DATE_ADD(CURDATE(), INTERVAL 1 DAY)"
        elif "后天" in question:
            date_condition = "a.slot_date = DATE_ADD(CURDATE(), INTERVAL 2 DAY)"
        else:
            date_condition = "a.slot_date >= CURDATE() AND a.slot_date <= DATE_ADD(CURDATE(), INTERVAL 7 DAY)"

        sql = f"""
            SELECT d.doctor_name, h.hospital_name,
                   a.slot_date, a.time_slot, a.available_slots,
                   CASE WHEN a.available_slots > 5 THEN '充足'
                        WHEN a.available_slots > 0 THEN '紧张' ELSE '已满' END as status
            FROM appointment_slots a
            JOIN robot_kb_doctors d ON a.doctor_id = d.id
            JOIN robot_kb_hospitals h ON a.hospital_id = h.id
            WHERE a.is_open = 1 AND a.available_slots > 0
              AND {date_condition}
              AND (%s = '' OR h.hospital_name LIKE %s)
              AND (%s = '' OR h.city_name LIKE %s)
            ORDER BY a.slot_date, a.available_slots DESC LIMIT 10
        """
        params = (
            hospital, f"%{hospital}%" if hospital else "",
            city, f"%{city}%" if city else ""
        )
        results["appointments"] = query_db(sql, params)

    # 活动促销 - 参数化查询
    if "promotion" in keywords["intent"]:
        # 先查询 hospital_promotions 表
        sql = """
            SELECT h.hospital_name, p.title, p.promotion_type,
                   p.original_price, p.promotion_price, p.discount_rate, p.end_date
            FROM hospital_promotions p
            JOIN robot_kb_hospitals h ON p.hospital_id = h.id
            WHERE p.status = 1 AND p.end_date >= CURDATE()
              AND (%s = '' OR h.hospital_name LIKE %s)
              AND (%s = '' OR h.city_name LIKE %s)
            ORDER BY p.discount_rate ASC LIMIT 10
        """
        params = (
            hospital, f"%{hospital}%" if hospital else "",
            city, f"%{city}%" if city else ""
        )
        results["promotions"] = query_db(sql, params)

        # 如果没有结果，查询 robot_kb_hospitals.current_activities 字段（同步数据）
        if not results["promotions"]:
            sql = """
                SELECT hospital_name, current_activities as title
                FROM robot_kb_hospitals
                WHERE current_activities IS NOT NULL
                  AND current_activities != ''
                  AND (%s = '' OR hospital_name LIKE %s)
                  AND (%s = '' OR city_name LIKE %s)
                LIMIT 10
            """
            params = (
                hospital, f"%{hospital}%" if hospital else "",
                city, f"%{city}%" if city else ""
            )
            results["promotions"] = query_db(sql, params)

    # 价格查询 - 参数化查询
    if "price" in keywords["intent"]:
        project = sanitize_like_param(keywords['project']) if keywords['project'] else "种植"

        # 如果指定了医院，优先从 robot_kb_hospitals.price_list 查询
        if hospital:
            sql = """
                SELECT hospital_name, city_name, price_list
                FROM robot_kb_hospitals
                WHERE price_list IS NOT NULL
                  AND LENGTH(price_list) > 2
                  AND hospital_name LIKE %s
                LIMIT 5
            """
            params = (f"%{hospital}%",)
            results["prices"] = query_db(sql, params)

        # 如果没有指定医院或没查到结果，查 robot_kb_prices 表
        if not results.get("prices"):
            sql = """
                SELECT h.hospital_name, h.city_name,
                       p.project_name, p.price_min, p.price_max, p.price_unit
                FROM robot_kb_prices p
                JOIN robot_kb_hospitals h ON p.hospital_id = h.id
                WHERE p.status = 1
                  AND p.project_name LIKE %s
                  AND (%s = '' OR h.city_name LIKE %s)
                ORDER BY p.price_min LIMIT 10
            """
            params = (
                f"%{project}%",
                city, f"%{city}%" if city else ""
            )
            results["prices"] = query_db(sql, params)

    # 医生履历 - 无需用户输入参数，安全
    if "career" in keywords["intent"]:
        sql = """
            SELECT doctor_name,
                   CASE event_type WHEN 'transfer' THEN '调动' WHEN 'join' THEN '入职'
                        WHEN 'leave' THEN '离职' WHEN 'promote' THEN '晋升' END as event_type_cn,
                   from_hospital_name, to_hospital_name,
                   from_position, to_position, event_date, reason
            FROM doctor_career_history
            WHERE is_public = 1 AND event_type IN ('transfer', 'join')
            ORDER BY event_date DESC LIMIT 10
        """
        results["career"] = query_db(sql)

    # 医院详情 - 参数化查询（适配客服系统同步数据）
    # 支持多家匹配，让用户选择
    if "hospital_info" in keywords["intent"] and keywords["hospital"]:
        sql = """
            SELECT hospital_name, hospital_type, description,
                   main_projects_list, price_list, current_activities,
                   phone, business_hours, detailed_address, city_name,
                   cooperation_status, channel_cooperation
            FROM robot_kb_hospitals
            WHERE hospital_name LIKE %s
              AND (%s = '' OR city_name LIKE %s)
            ORDER BY cooperation_status = '合作中' DESC, id DESC
            LIMIT 15
        """
        params = (
            f"%{hospital}%",
            city, f"%{city}%" if city else ""
        )
        results["hospital_detail"] = query_db(sql, params)

    return results


def generate_response(question: str, query_results: Dict, history: list = None) -> str:
    """基于查询结果生成自然语言回复（支持多轮对话上下文）"""
    history = history or []

    # 处理客户分析结果 - 使用专门的prompt
    if "customer_analysis" in query_results:
        data = query_results["customer_analysis"]
        customer = data.get("customer", {})
        analysis = data.get("analysis", {})
        follow_records = data.get("follow_records", [])
        orders = data.get("orders", [])

        # 从跟进记录中提取医院信息
        mentioned_hospitals = []
        for record in follow_records:
            content = record.get('content', '')
            if '医院' in content:
                mentioned_hospitals.append(content)

        prompt = f"""你是一位专业的客服主管AI助手，帮助客服分析客户情况并提供建议。

## 客户基本信息:
- 客户ID: {customer.get('id')}
- 姓名: {customer.get('name')}
- 手机: {customer.get('phone')}
- 状态: {customer.get('status')}
- 意向项目: {customer.get('intention_project')}
- 意向地区: {customer.get('intention_region')}
- 来源渠道: {customer.get('from_source')}
- 注册时间: {customer.get('register_time')}

## 成交概率分析:
- 成交概率: {analysis.get('probability')}%
- 意向等级: {analysis.get('level')} ({analysis.get('level_desc')})
- 分析依据: {', '.join(analysis.get('reasons', []))}
- 跟进建议: {analysis.get('suggestion')}

## 跟进记录 ({len(follow_records)}条):
{json.dumps(follow_records, ensure_ascii=False, indent=2) if follow_records else '暂无跟进记录'}

## 成交/订单记录 ({len(orders)}条):
{json.dumps(orders, ensure_ascii=False, indent=2) if orders else '暂无成交记录'}

## 回答要求:
1. 必须基于上面的数据回答，数据中有什么就说什么
2. 如果问成交情况：展示订单记录、金额、状态
3. 如果问推荐医院：根据跟进记录中已预约/派单的医院信息推荐，说明客户已经预约了哪家医院
4. 如果数据中有医院名称，直接告诉客服该客户已预约/成交的医院
5. 语气专业友好，面向内部客服人员
6. 不要说"无法提供信息"，数据都在上面，请直接使用

## 客服提问:
{question}

请直接回答:"""

        return call_llm(prompt)

    # 处理客户查询错误
    if "customer_error" in query_results:
        return query_results["customer_error"]

    # 处理来源分析错误
    if "source_error" in query_results:
        return query_results["source_error"]

    # 处理来源列表查询
    if "source_list" in query_results:
        data = query_results["source_list"]
        sources = data.get('sources', [])
        keyword = data.get('keyword', '全部')

        if not sources:
            return f"未找到包含'{keyword}'的来源数据。"

        response = f"**📊 来源列表（{keyword}）**\n\n"
        response += "| 来源名称 | 类型 | 客户数 | 成交数 | 转化率 |\n"
        response += "|---------|------|--------|--------|--------|\n"

        type_labels = {'paid': '💰竞价', 'organic': '🌱自然', 'channel': '🤝渠道', 'unknown': '❓未知'}
        for s in sources[:20]:
            type_label = type_labels.get(s['type'], '未知')
            response += f"| {s['name'][:15]} | {type_label} | {s['total']:,} | {s['converted']:,} | {s['rate']}% |\n"

        return response

    # 处理来源对比查询
    if "source_compare" in query_results:
        data = query_results["source_compare"]
        comparisons = data.get('comparisons', [])
        period = data.get('period_days', 90)

        if not comparisons:
            return "未找到可对比的来源数据。"

        response = f"**⚖️ 来源对比分析（最近{period}天）**\n\n"
        response += "| 来源 | 客户数 | 成交数 | 转化率 | TOP地区 |\n"
        response += "|-----|--------|--------|--------|--------|\n"

        for c in comparisons:
            regions = ', '.join(c.get('top_regions', [])[:3]) or '暂无'
            response += f"| {c['source']} | {c['total']:,} | {c['converted']:,} | {c['rate']}% | {regions} |\n"

        return response

    # 处理来源分析查询
    if "source_analysis" in query_results:
        data = query_results["source_analysis"]
        source_keyword = data.get('source_keyword', '')
        source_details = data.get('source_details', [])
        period = data.get('period_days', 90)
        region = data.get('region', {})
        project = data.get('project', {})
        hospital = data.get('hospital', {})
        trend = data.get('trend', {})
        keywords_data = data.get('keywords', {})

        type_labels = {'paid': '💰竞价', 'organic': '🌱自然', 'channel': '🤝渠道', 'unknown': '❓未知'}

        response = f"**📊 {source_keyword} 来源分析报告（最近{period}天）**\n\n"

        # 来源类型
        if source_details:
            types = [type_labels.get(s['type'], '未知') for s in source_details]
            response += f"**来源类型：** {', '.join(set(types))}\n"
            response += f"**包含来源：** {', '.join([s['name'] for s in source_details[:5]])}\n\n"

        # 数据概览
        response += "**📈 数据概览：**\n"
        response += f"- 总客户数：{region.get('total_customers', 0):,}\n"
        response += f"- 总成交数：{region.get('total_converted', 0):,}\n"
        response += f"- 转化率：{region.get('conversion_rate', 0)}%\n\n"

        # 地区分布TOP5
        regions = region.get('regions', [])[:5]
        if regions:
            response += "**📍 地区分布TOP5：**\n"
            for r in regions:
                response += f"- {r['province']}：{r['count']:,}人 ({r['percentage']}%)\n"
            response += "\n"

        # 意向项目TOP5
        projects = project.get('projects', [])[:5]
        if projects:
            response += "**🦷 意向项目TOP5：**\n"
            for p in projects:
                response += f"- {p['project']}：{p['count']:,}人 ({p['percentage']}%)\n"
            response += "\n"

        # 成交医院TOP5
        hospitals = hospital.get('hospitals', [])[:5]
        if hospitals:
            response += "**🏥 成交医院TOP5：**\n"
            for h in hospitals:
                response += f"- {h['hospital'][:20]}：¥{h['amount']:,.0f}（{h['orders']}单）\n"
            response += "\n"

        # 咨询热词
        kws = keywords_data.get('keywords', [])[:10]
        if kws:
            response += "**🔥 咨询热词：**\n"
            kw_str = ', '.join([f"{k['keyword']}({k['count']})" for k in kws])
            response += f"{kw_str}\n"

        return response

    # 处理医院排名查询
    if "hospital_ranking" in query_results:
        data = query_results["hospital_ranking"]
        if data.get('error'):
            return f"抱歉，{data['error']}"

        hospitals = data.get('hospitals', [])
        area = data.get('area', '该地区')

        if not hospitals:
            return f"抱歉，暂未找到{area}地区的医院数据。"

        response = f"**📊 {area}地区医院派单排名（Top {len(hospitals)}）**\n\n"
        for h in hospitals:
            rank = h.get('rank', '')
            name = h.get('hospital_name', '')
            htype = h.get('hospital_type', '')
            count = h.get('dispatch_count', 0)
            response += f"{rank}. **{name}**（{htype}）— 派单量 {count}\n"
        return response

    if "hospital_ranking_error" in query_results:
        return query_results["hospital_ranking_error"]

    # 处理客服跟进查询
    if "kf_stats" in query_results:
        data = query_results["kf_stats"]
        if data.get('error'):
            return f"抱歉，{data['error']}"

        kf_name = data.get('kf_name', '')
        stats = data.get('statistics', {})
        status_dist = data.get('status_distribution', [])
        records = data.get('recent_records', [])
        query_days = data.get('query_days', 7)

        response = f"**👤 客服 {kf_name} 跟进情况（最近{query_days}天）**\n\n"

        # 统计概览
        response += "**📊 数据概览：**\n"
        response += f"- 跟进次数：{stats.get('follow_count', 0)}次\n"
        response += f"- 跟进客户：{stats.get('client_count', 0)}人\n"
        response += f"- 今日跟进：{stats.get('today_follow', 0)}次（{stats.get('today_client', 0)}人）\n\n"

        # 客户状态分布
        if status_dist:
            response += "**📈 客户状态分布：**\n"
            for s in status_dist[:5]:
                response += f"- {s['status']}：{s['count']}人\n"
            response += "\n"

        # 最近跟进记录
        if records:
            response += "**📋 最近跟进记录：**\n"
            for r in records[:5]:
                content = r['content'] or '(无内容)'
                response += f"- {r['time']} | 客户{r['client_id']} | {content}\n"
            response += "\n"

        # 成交业绩数据
        sales = data.get('sales', {})
        if sales.get('deal_count', 0) > 0:
            response += f"**💰 成交业绩（最近{query_days}天）：**\n"
            response += f"- 成交单数：{sales['deal_count']}单\n"
            response += f"- 成交总额：¥{sales['total_amount']:,.0f}\n"
            response += f"- 平均客单价：¥{sales['avg_amount']:,.0f}\n\n"

            recent_deals = sales.get('recent_deals', [])
            if recent_deals:
                response += "**📋 最近成交明细：**\n"
                for d in recent_deals[:5]:
                    response += f"- {d['date']} | {d['hospital']} | {d['project']} | ¥{d['amount']:,.0f}\n"

        return response

    if "kf_stats_error" in query_results:
        return query_results["kf_stats_error"]

    # 处理部门业绩查询
    if "department_stats" in query_results:
        data = query_results["department_stats"]
        if data.get('error'):
            return f"抱歉，{data['error']}"

        dept = data.get('department', {})
        stats = data.get('statistics', {})
        time_label = data.get('time_label', '')

        dep_name = dept.get('small_dep', '')
        mode_label = data.get('mode', '审核业绩')
        other_mode_label = '成交业绩（按成交时间）' if mode_label == '审核业绩' else '审核业绩（按审核时间）'
        other_mode_kw = '流水' if mode_label == '审核业绩' else '审核'

        response = f"**🏢 {dep_name}（{dept.get('department', '')}）{mode_label} — {time_label}**\n\n"
        response += "**📊 整体数据：**\n"
        response += f"- 总业绩：¥{stats.get('total_money', 0):,.0f}（{stats.get('total_num', 0)}笔）\n\n"

        response += "**📋 分类明细：**\n"
        if stats.get('kq_num', 0) > 0:
            response += f"- 口腔：¥{stats['kq_money']:,.0f}（{stats['kq_num']}笔）\n"
        if stats.get('ym_num', 0) > 0:
            response += f"- 医美：¥{stats['ym_money']:,.0f}（{stats['ym_num']}笔）\n"
        if stats.get('yk_num', 0) > 0:
            response += f"- 眼科：¥{stats['yk_money']:,.0f}（{stats['yk_num']}笔）\n"
        if stats.get('kr_num', 0) > 0:
            response += f"- 韩国：¥{stats['kr_money']:,.0f}（{stats['kr_num']}笔）\n"

        if stats.get('total_num', 0) == 0:
            response += "\n暂无成交数据。\n"

        response += f"\n> 当前为**{mode_label}**，如需查看{other_mode_label}，可输入：「{dep_name}{other_mode_kw}业绩」\n"
        response += f"> 可指定时间，如：「{dep_name}上月业绩」「{dep_name}今日业绩」「{dep_name}最近30天业绩」\n"

        return response

    if "department_stats_error" in query_results:
        return query_results["department_stats_error"]

    # 处理公司整体业绩查询
    if "company_stats" in query_results:
        data = query_results["company_stats"]
        stats = data.get('statistics', {})
        departments = data.get('departments', [])
        time_label = data.get('time_label', '')
        mode_label = data.get('mode', '审核业绩')
        response = f"**🏢 公司整体{mode_label} — {time_label}**\n\n"
        response += "**📊 公司总计：**\n"
        response += f"- 总业绩：¥{stats.get('total_money', 0):,.0f}（{stats.get('total_num', 0)}笔）\n"
        if stats.get('kq_num', 0) > 0:
            response += f"- 口腔：¥{stats['kq_money']:,.0f}（{stats['kq_num']}笔）\n"
        if stats.get('ym_num', 0) > 0:
            response += f"- 医美：¥{stats['ym_money']:,.0f}（{stats['ym_num']}笔）\n"
        if stats.get('yk_num', 0) > 0:
            response += f"- 眼科：¥{stats['yk_money']:,.0f}（{stats['yk_num']}笔）\n"
        if stats.get('kr_num', 0) > 0:
            response += f"- 韩国：¥{stats['kr_money']:,.0f}（{stats['kr_num']}笔）\n"
        response += "\n"

        if departments:
            response += f"**🏆 部门排名：**\n"
            for i, dept_data in enumerate(departments, 1):
                dept = dept_data.get('department', {})
                ds = dept_data.get('statistics', {})
                if ds.get('total_num', 0) > 0:
                    response += f"{i}. **{dept.get('small_dep', '')}** — ¥{ds.get('total_money', 0):,.0f}（{ds.get('total_num', 0)}笔）\n"

        other_mode_kw = '流水' if mode_label == '审核业绩' else '审核'
        response += f"\n> 当前为**{mode_label}**，如需切换可输入：「公司{other_mode_kw}业绩」\n"
        response += f"> 可指定时间，如：「公司上月业绩」「公司今日业绩」\n"
        response += f"> 查看单个部门，如：「TEG部门业绩」「BCG上月业绩」\n"

        return response

    # 处理特定医院成交项目查询 - 根据问题精准回复
    if "hospital_deals" in query_results:
        data = query_results["hospital_deals"]
        if data.get('error'):
            return f"抱歉，{data['error']}"

        # 地区成交排名模式（由 query_region_hospital_deals 返回）
        if data.get('city') and data.get('hospital_ranking'):
            city = data['city']
            stats = data.get('statistics', {})
            ranking = data.get('hospital_ranking', [])
            top_projects = data.get('top_projects', [])
            query_days = data.get('query_days', 30)

            response = f"**🏥 {city}地区成交排名（最近{query_days}天）**\n\n"
            response += f"**📊 整体数据：**\n"
            response += f"- 有成交医院：{stats.get('hospital_count', 0)}家\n"
            response += f"- 总成交笔数：{stats.get('total_deals', 0)}笔\n"
            response += f"- 总成交额：¥{stats.get('total_amount', 0):,.0f}\n"
            response += f"- 平均客单价：¥{stats.get('avg_amount', 0):,.0f}\n\n"

            if ranking:
                response += f"**🏆 医院成交排名 TOP {min(len(ranking), 10)}：**\n"
                for i, r in enumerate(ranking[:10], 1):
                    response += (f"{i}. **{r['hospital']}** - "
                                f"{r['deal_count']}笔，¥{r['total_amount']:,.0f}，"
                                f"均价¥{r['avg_amount']:,.0f}\n")
                response += "\n"

            if top_projects:
                response += "**🦷 热门项目：**\n"
                for p in top_projects[:5]:
                    response += f"- {p['name']}：{p['count']}笔，总额¥{p['total_amount']:,.0f}\n"

            return response

        hospital = data.get('hospital_name', '')
        stats = data.get('statistics', {})
        projects = data.get('projects', [])
        recent = data.get('recent_deals', [])
        query_days = data.get('query_days', 30)

        # 分析用户具体问什么
        ask_avg_price = any(kw in question for kw in ["客单价", "均价", "平均价", "单价"])
        ask_projects = any(kw in question for kw in ["项目", "做什么", "哪些项目"])
        ask_records = any(kw in question for kw in ["记录", "最近", "明细"])
        ask_total = any(kw in question for kw in ["总额", "总共", "一共"])
        ask_count = any(kw in question for kw in ["多少笔", "几笔", "成交量"])
        ask_analysis = any(kw in question for kw in ["分析", "详细", "报告", "汇总"])

        # 如果要分析，返回完整详细信息
        if ask_analysis:
            response = f"**🏥 {hospital} 成交分析（最近{query_days}天）**\n\n"

            # 数据概览
            response += "**📊 数据概览：**\n"
            response += f"- 成交笔数：{stats.get('deal_count', 0)}笔\n"
            response += f"- 成交总额：¥{stats.get('total_amount', 0):,.0f}\n"
            response += f"- 客单价：¥{stats.get('avg_amount', 0):,.0f}\n"
            response += f"- 最高单笔：¥{stats.get('max_amount', 0):,.0f}\n"
            response += f"- 最低单笔：¥{stats.get('min_amount', 0):,.0f}\n\n"

            # 项目分布
            if projects:
                response += "**🦷 项目分布：**\n"
                for p in projects[:5]:
                    pct = p['count'] / stats.get('deal_count', 1) * 100
                    response += f"- {p['name']}：{p['count']}笔（{pct:.0f}%），均价¥{p['avg_amount']:,.0f}\n"
                response += "\n"

            # 最近成交
            if recent:
                response += f"**📋 最近成交（TOP 5）：**\n"
                for d in recent[:5]:
                    response += f"- {d['date']} | {d['project']} | ¥{d['amount']:,.0f}\n"

            return response

        # 如果只问客单价，简洁回复
        if ask_avg_price and not ask_projects and not ask_records:
            return f"**{hospital}** 最近{query_days}天客单价：**¥{stats.get('avg_amount', 0):,.0f}**（共{stats.get('deal_count', 0)}笔成交）"

        # 如果只问成交项目分布
        if ask_projects and not ask_avg_price and not ask_records:
            if not projects:
                return f"**{hospital}** 最近{query_days}天暂无成交项目记录"
            response = f"**{hospital}** 最近{query_days}天成交项目：\n"
            for p in projects[:5]:
                response += f"- {p['name']}：{p['count']}笔，均价¥{p['avg_amount']:,.0f}\n"
            return response

        # 如果只问最近成交记录
        if ask_records and not ask_avg_price and not ask_projects:
            if not recent:
                return f"**{hospital}** 最近{query_days}天暂无成交记录"
            response = f"**{hospital}** 最近成交记录：\n"
            for d in recent[:5]:
                response += f"- {d['date']} | {d['project']} | ¥{d['amount']:,.0f}\n"
            return response

        # 如果只问总额
        if ask_total and not ask_projects and not ask_records:
            return f"**{hospital}** 最近{query_days}天成交总额：**¥{stats.get('total_amount', 0):,.0f}**（共{stats.get('deal_count', 0)}笔）"

        # 如果只问成交量
        if ask_count and not ask_projects and not ask_records:
            return f"**{hospital}** 最近{query_days}天成交：**{stats.get('deal_count', 0)}笔**，总额¥{stats.get('total_amount', 0):,.0f}"

        # 普通查询（成交情况等），返回简洁概览
        response = f"**{hospital}** 最近{query_days}天成交：\n"
        response += f"- {stats.get('deal_count', 0)}笔，总额¥{stats.get('total_amount', 0):,.0f}，客单价¥{stats.get('avg_amount', 0):,.0f}\n"
        if projects:
            top_project = projects[0]
            response += f"- 主要项目：{top_project['name']}（{top_project['count']}笔，均价¥{top_project['avg_amount']:,.0f}）"

        return response

    if "hospital_deals_error" in query_results:
        return query_results["hospital_deals_error"]

    # 处理流失预警查询
    if "churn_warning" in query_results:
        data = query_results["churn_warning"]
        stats = data.get('statistics', {})
        clients = data.get('clients', [])
        alert = data.get('alert', '')
        query_days = data.get('query_days', 30)

        # 构建回复
        response = f"**{alert}**\n\n"

        # 风险统计
        response += f"**📊 流失风险统计（最近{query_days}天派单）：**\n"
        response += f"- 🔴 紧急(>14天): {stats.get('critical', {}).get('count', 0)}人\n"
        response += f"- 🟠 高风险(7-14天): {stats.get('high', {}).get('count', 0)}人\n"
        response += f"- 🟡 中风险(3-7天): {stats.get('medium', {}).get('count', 0)}人\n"
        response += f"- 🟢 低风险(≤3天): {stats.get('low', {}).get('count', 0)}人\n\n"

        # 风险客户列表
        if clients:
            response += f"**⚠️ 需关注客户 TOP {len(clients)}：**\n\n"
            for c in clients[:10]:
                risk_icon = {'critical': '🔴', 'high': '🟠', 'medium': '🟡', 'low': '🟢'}.get(c['risk_level'], '⚪')
                hospital_type = c.get('hospital_type', '未知')
                response += f"{risk_icon} **{c['name']}** (ID:{c['client_id']}) - {c['project']}\n"
                response += f"   派单: {c['hospital']}【{hospital_type}】| 派单{c['days_since_dispatch']}天 | 最后跟进: {c['last_follow']}\n\n"

        response += "💡 建议：优先处理紧急和高风险客户，避免客户流失。"
        return response

    # 处理意向地区医院分析（客户上下文场景）
    if "district_hospital" in query_results:
        data = query_results["district_hospital"]
        hospitals = data.get('hospitals', [])
        summary = data.get('summary', {})
        district_name = data.get('district_name', '该地区')
        fallback_desc = data.get('fallback_desc', '')

        response = f"**🏥 {district_name} 意向地区医院成交分析**\n\n"

        if not hospitals:
            response += f"该地区暂无成交记录。{f'({fallback_desc})' if fallback_desc else ''}\n"
            response += "💡 建议：可尝试扩大地区范围或查看整体医院排名。"
            return response

        response += f"**📊 整体数据：**\n"
        response += f"- 有成交医院：{summary.get('total_hospitals', 0)}家\n"
        response += f"- 总成交：{summary.get('total_deals', 0)}笔\n"
        response += f"- 总成交额：¥{summary.get('total_amount', 0):,.0f}\n"
        response += f"- 有二开医院：{summary.get('hospitals_with_repeat', 0)}家\n\n"

        response += f"**🏆 成交医院排名：**\n\n"
        for i, h in enumerate(hospitals[:10], 1):
            name = h.get('hospital_name', f"医院#{h.get('hospital_id', '?')}")
            deal_count = h.get('deal_count', 0)
            amount = float(h.get('total_amount', 0) or 0)
            repeat = h.get('repeat_client_count', 0)
            repeat_tag = " ✅有二开" if repeat > 0 else ""
            response += f"**{i}. {name}**\n"
            response += f"   成交{deal_count}笔 | 金额¥{amount:,.0f}{repeat_tag}\n\n"

        response += "💡 建议：优先派单给有二开记录的医院，客户转化和复购能力更强。"
        return response

    # 处理医院合作分析
    if "hospital_analysis" in query_results:
        data = query_results["hospital_analysis"]
        hospitals = data.get('hospitals', [])
        summary = data.get('summary', {})
        query_days = data.get('query_days', 30)
        hospital_name = data.get('hospital_name', '')

        if hospital_name:
            response = f"**🏥 {hospital_name} 医院合作分析（最近{query_days}天）**\n\n"
        else:
            response = f"**🏥 医院合作排名（最近{query_days}天，按成交额）**\n\n"

        # 汇总统计
        response += f"**📊 整体数据：**\n"
        response += f"- 医院数量：{summary.get('total_hospitals', 0)}家\n"
        response += f"- 总派单：{summary.get('total_dispatch', 0)}单\n"
        response += f"- 总成交：{summary.get('total_deal', 0)}单\n"
        response += f"- 总成交额：¥{summary.get('total_amount', 0):,.0f}\n"
        response += f"- 平均转化率：{summary.get('avg_conversion_rate', 0)}%\n\n"

        # 医院列表
        if hospitals:
            response += f"**🏆 医院明细 TOP {len(hospitals)}：**\n\n"
            for i, h in enumerate(hospitals[:10], 1):
                response += f"**{i}. {h['name']}**【{h['type']}】\n"
                response += f"   派单{h['dispatch_count']}单 → 到院{h['arrive_count']}单 → 成交{h['deal_count']}单\n"
                response += f"   成交额：¥{h['total_amount']:,.0f} | 转化率：{h['conversion_rate']}%\n\n"

        return response

    # 处理客户生命周期分析
    if "customer_lifecycle" in query_results:
        data = query_results["customer_lifecycle"]
        stages = data.get('stages', {})
        funnel = data.get('conversion_funnel', [])
        avg_cycle = data.get('avg_cycle', {})
        query_days = data.get('query_days', 30)

        response = f"**📈 客户生命周期分析（最近{query_days}天）**\n\n"

        # 各阶段统计
        response += "**📊 各阶段客户数：**\n"
        for stage_key, stage_data in stages.items():
            response += f"- {stage_data['label']}：{stage_data['count']:,}人\n"
        response += "\n"

        # 转化漏斗（纯文字，钉钉不支持进度条渲染）
        response += "**🔄 转化漏斗：**\n"
        for i, f in enumerate(funnel):
            response += f"  {f['stage']}：{f['count']:,}人（{f['rate']}%）\n"
            if i < len(funnel) - 1:
                response += "    ↓\n"
        response += "\n"

        # 平均周期
        if avg_cycle:
            response += "**⏱️ 平均转化周期：**\n"
            response += f"- 注册→成交：{avg_cycle.get('register_to_deal', 0)}天\n"
            response += f"- 派单→成交：{avg_cycle.get('dispatch_to_deal', 0)}天\n"
            response += f"- 派单→到院：{avg_cycle.get('dispatch_to_arrive', 0)}天\n"

        return response

    # 处理时间维度分析
    if "time_analysis" in query_results:
        data = query_results["time_analysis"]
        hourly = data.get('hourly_distribution', [])
        daily = data.get('daily_distribution', [])
        weekday = data.get('weekday_comparison', {})
        peak = data.get('peak_hour', {})
        query_days = data.get('query_days', 7)

        response = f"**⏰ 时间维度分析（最近{query_days}天）**\n\n"

        # 进线高峰
        if peak:
            response += f"**🔥 进线高峰时段：{peak['hour']}:00-{peak['hour']+1}:00**（{peak['count']}人）\n\n"

        # 每小时分布（显示Top5高峰时段）
        response += "**📊 各时段进线分布（TOP5）：**\n"
        sorted_hourly = sorted(hourly, key=lambda x: x['count'], reverse=True)[:5]
        for h in sorted_hourly:
            response += f"- {h['hour']:02d}:00-{h['hour']+1:02d}:00：{h['count']}人\n"
        response += "\n"

        # 每日趋势
        if daily:
            response += "**📅 每日进线趋势：**\n"
            for d in daily[:7]:
                response += f"- {d['date']}：新增{d['new_count']}人，成交{d['deal_count']}人\n"
            response += "\n"

        # 工作日vs周末
        if weekday:
            wd = weekday.get('weekday', {})
            we = weekday.get('weekend', {})
            response += "**📆 工作日 vs 周末：**\n"
            response += f"- 工作日：新增{wd.get('new_count', 0)}人，成交{wd.get('deal_count', 0)}人\n"
            response += f"- 周末：新增{we.get('new_count', 0)}人，成交{we.get('deal_count', 0)}人\n"

        return response

    # 处理多家医院匹配的情况 - 列出让用户选择
    if "hospital_detail" in query_results and len(query_results["hospital_detail"]) > 1:
        hospitals = query_results["hospital_detail"]
        response = f"找到了 {len(hospitals)} 家匹配的医院，请问您想了解哪一家？\n\n"
        for i, h in enumerate(hospitals, 1):
            name = h.get('hospital_name', '')
            city = h.get('city_name', '') or ''
            address = h.get('detailed_address', '') or ''
            status = h.get('cooperation_status', '')
            status_icon = "✅" if status == "合作中" else "⏸️" if "停止" in status else "📋"
            location = city if city else (address[:20] + "..." if len(address) > 20 else address)
            response += f"{i}. {status_icon} **{name}**"
            if location:
                response += f"（{location}）"
            response += "\n"
        response += "\n请告诉我您想了解哪一家，比如说\"第1家\"或直接说城市名如\"西安那家\"。"
        return response

    # 处理多家医院价格查询的情况
    if "prices" in query_results and isinstance(query_results["prices"], list) and len(query_results["prices"]) > 1:
        prices = query_results["prices"]
        # 检查是否是 price_list 格式（来自 robot_kb_hospitals）
        if prices and "price_list" in prices[0]:
            response = f"找到了 {len(prices)} 家匹配的医院有价格信息：\n\n"
            for i, p in enumerate(prices, 1):
                name = p.get('hospital_name', '')
                city = p.get('city_name', '') or ''
                response += f"{i}. **{name}**"
                if city:
                    response += f"（{city}）"
                response += "\n"
            response += "\n请告诉我您想了解哪一家的价格，比如说\"第1家\"或\"西安那家\"。"
            return response

    # 构建上下文 - 普通查询
    context_parts = []
    # 知识库查询 key，即使结果为空也要告知 LLM "已查询但无数据"
    kb_keys = {"doctors", "hospitals", "appointments", "promotions", "prices", "hospital_detail", "career"}
    for key, data in query_results.items():
        if data:
            if isinstance(data, list):
                context_parts.append(f"## {key}:\n{json.dumps(data[:5], ensure_ascii=False, indent=2, cls=CustomEncoder)}")
            else:
                context_parts.append(f"## {key}:\n{json.dumps(data, ensure_ascii=False, indent=2, cls=CustomEncoder)}")
        elif key in kb_keys:
            # 知识库查询执行了但无结果，明确告知 LLM
            context_parts.append(f"## {key}:\n该信息暂未录入知识库，无查询结果。请勿编造。")

    context = "\n\n".join(context_parts) if context_parts else ""

    # --- 使用多轮 messages 格式发给 LLM，提升上下文连贯性 ---

    # 辅助：截断过长的助手回复，防止 prompt 膨胀
    def _truncate(text: str, max_len: int = 600) -> str:
        if len(text) <= max_len:
            return text
        return text[:max_len] + "…（已省略）"

    # 如果问题中包含客户上下文（从工作台发起），使用专门的客服主管 prompt
    if '【当前客户】' in question:
        uq_match = re.search(r'用户问题[：:]\s*(.+)', question, re.DOTALL)
        user_q = uq_match.group(1).strip() if uq_match else question
        ctx_end = question.find('用户问题')
        client_context = question[:ctx_end].strip() if ctx_end > 0 else question

        system_msg = f"""你是一位专业的客服主管AI助手，帮助客服人员分析客户情况并提供实用建议。

## 当前客户信息:
{client_context}
{"" if not context else f'''
## 补充数据:
{context}
'''}
## 回答要求:
- 日常问候简短回应，不要强行输出客户摘要。
- 有明确业务问题时，基于上面的客户信息和补充数据直接回答。
- 语气专业务实，回复简洁实用。
- 不要使用"第一部分""第二部分"标题格式。
- 不要说"无法获取信息"——数据都在上面。"""

        messages = [{"role": "system", "content": system_msg}]
        # 加入多轮历史
        for msg in history[-8:]:
            content = msg['content'] if msg['role'] == 'user' else _truncate(msg['content'])
            messages.append({"role": msg['role'], "content": content})
        messages.append({"role": "user", "content": user_q})
        return call_llm_messages(messages)

    # --- 通用口腔客服对话 ---
    system_msg = f"""你是一位专业、热情的口腔医疗客服AI助手（神舟商通·小通）。

## 本次查询的数据库结果:
{context if context else '没有找到相关数据'}

## 回答要求:
1. 用亲切友好的口吻回答，称呼顾客为"您"
2. 基于数据回答，不要编造信息
3. 如果是推荐，说明推荐理由
4. 如果数据不足，礼貌地说明并提供建议
5. 适当引导顾客预约或咨询更多
6. 注意理解对话上下文，如果顾客说"需要"、"好的"、"可以"等简短回复，要结合之前的对话理解其意图
7. 只回答口腔/牙科相关问题，医美、眼科等非口腔问题请礼貌说明不在服务范围"""

    messages = [{"role": "system", "content": system_msg}]
    # 加入多轮历史（最近4轮=8条消息），助手回复截断防止 prompt 过长
    for msg in history[-8:]:
        content = msg['content'] if msg['role'] == 'user' else _truncate(msg['content'])
        messages.append({"role": msg['role'], "content": content})
    messages.append({"role": "user", "content": question})

    logger.info(f"多轮对话: {len(messages)}条messages, 历史{len(history)}条")
    return call_llm_messages(messages)


def validate_question(question: Any) -> str:
    """
    验证和清理用户输入

    Args:
        question: 用户输入（可能是字符串或列表）

    Returns:
        清理后的问题字符串

    Raises:
        ValueError: 如果输入无效
    """
    if isinstance(question, list):
        if not question:
            raise ValueError("问题列表为空")
        question = question[-1].get("content", "") if isinstance(question[-1], dict) else str(question[-1])

    if not isinstance(question, str):
        raise ValueError("问题必须是字符串")

    question = question.strip()

    if not question:
        raise ValueError("问题不能为空")

    if len(question) > 1000:
        raise ValueError("问题过长，请限制在1000字符以内")

    if len(question) < 2:
        raise ValueError("问题过短")

    return question


# ============================================================
# API 端点
# ============================================================
@app.route("/api/dental/chat", methods=["POST"])
@limiter.limit("30 per minute")  # 速率限制
def dental_chat():
    """口腔客服对话接口（支持多轮对话）"""
    try:
        data = request.json
        if not data:
            return jsonify({"error": "请提供JSON数据"}), 400

        raw_question = data.get("messages", "") or data.get("question", "")

        # 获取或创建会话ID
        session_id = data.get("session_id", "")
        if not session_id:
            session_id = conversation_store.create_session()

        # 验证输入
        try:
            question = validate_question(raw_question)
        except ValueError as e:
            return jsonify({"error": str(e), "session_id": session_id}), 400

        # 记录请求（不记录完整内容）
        logger.info(f"收到问题 (长度: {len(question)}字符, session: {session_id[:8]}...)")

        # 获取会话历史
        history = conversation_store.get_history(session_id)

        # 处理用户从列表中选择医院的情况（如"第1家"、"西安那家"、"1"等）
        query_question = question
        if len(question) <= 10 and history:
            # 检查上一条助手消息是否是医院列表
            last_assistant_msg = ""
            for msg in reversed(history):
                if msg['role'] == 'assistant':
                    last_assistant_msg = msg['content']
                    break

            if "找到了" in last_assistant_msg and "家匹配" in last_assistant_msg:
                # 提取列表中的医院名称
                hospital_pattern = r'\d+\.\s*[✅⏸️📋]?\s*\*\*(.+?)\*\*'
                hospitals_in_list = re.findall(hospital_pattern, last_assistant_msg)

                selected_hospital = None
                # 尝试匹配"第N家"或数字
                num_pattern = r'第?(\d+)家?'
                num_match = re.search(num_pattern, question)
                if num_match:
                    idx = int(num_match.group(1)) - 1
                    if 0 <= idx < len(hospitals_in_list):
                        selected_hospital = hospitals_in_list[idx]
                else:
                    # 尝试匹配城市名（如"西安那家"）
                    for h in hospitals_in_list:
                        if any(city in question for city in ["北京", "上海", "西安", "广州", "深圳", "成都", "武汉", "重庆", "杭州", "南京"]):
                            for city in ["北京", "上海", "西安", "广州", "深圳", "成都", "武汉", "重庆", "杭州", "南京"]:
                                if city in question and city in h:
                                    selected_hospital = h
                                    break
                        if selected_hospital:
                            break

                if selected_hospital:
                    query_question = f"{selected_hospital} 详情介绍"
                    logger.info(f"用户选择医院: {selected_hospital}")

        # 处理简短回复 - 从历史中提取上下文进行查询
        if len(question) <= 6 and history and query_question == question:
            # 从历史中查找医院名称
            hospital_name = None
            for msg in reversed(history):
                if msg['role'] == 'user':
                    hospital_pattern = r'([\u4e00-\u9fa5]{2,8}(?:口腔|医院|诊所|门诊))'
                    matches = re.findall(hospital_pattern, msg['content'])
                    if matches:
                        hospital_name = matches[0]
                        break

            if hospital_name:
                # 根据当前问题关键词扩展查询
                if any(kw in question for kw in ["价格", "多少钱", "费用"]):
                    query_question = f"{hospital_name} 价格"
                elif any(kw in question for kw in ["医生", "专家", "大夫"]):
                    query_question = f"{hospital_name} 医生"
                elif any(kw in question for kw in ["活动", "优惠", "促销"]):
                    query_question = f"{hospital_name} 活动"
                elif any(kw in question for kw in ["需要", "好的", "可以", "好", "要", "是的", "对", "嗯", "行"]):
                    query_question = f"{hospital_name} 价格介绍"
                else:
                    query_question = f"{hospital_name} {question}"
                logger.info(f"短回复扩展查询: {query_question}")

        # 智能查询
        results = smart_query(query_question)
        logger.info(f"查询结果: {list(results.keys())}")

        # 生成回复（包含历史上下文）
        response = generate_response(question, results, history)
        logger.info(f"生成回复成功")

        # 保存对话到历史
        conversation_store.add_message(session_id, "user", question)
        conversation_store.add_message(session_id, "assistant", response)

        # 处理返回数据（列表取前3条，字典直接返回）
        processed_data = {}
        for k, v in results.items():
            if isinstance(v, list):
                processed_data[k] = v[:3] if v else []
            elif isinstance(v, dict):
                processed_data[k] = v
            else:
                processed_data[k] = v

        return app.response_class(
            response=json.dumps({
                "success": True,
                "session_id": session_id,
                "question": question,
                "answer": response,
                "data": processed_data
            }, ensure_ascii=False, cls=CustomEncoder),
            mimetype='application/json'
        )

    except Exception as e:
        import traceback
        logger.error(f"处理请求失败: {type(e).__name__}: {e}\n{traceback.format_exc()}")
        return jsonify({"error": "处理请求时发生错误，请稍后重试"}), 500


def _sse_event(data: dict) -> str:
    """格式化一条 SSE 事件"""
    return f"data: {json.dumps(data, ensure_ascii=False, cls=CustomEncoder)}\n\n"


def _intent_to_step_message(keywords: dict) -> tuple:
    """根据识别到的 intent 返回 (step_name, message, icon)"""
    intents = keywords.get("intent", [])
    has_ctx = keywords.get("_has_client_context", False)

    customer_intents = {"customer_conversion", "customer_follow", "customer_info"}
    hospital_intents = {"hospital_recommend", "doctor_recommend", "schedule",
                        "hospital_info", "price", "promotion", "appointment", "career"}
    source_intents = {"source_analysis", "source_compare", "source_list"}

    if not has_ctx and any(i in intents for i in customer_intents):
        return ("query_customer", "正在客服系统搜寻该客户信息...", "search")
    if any(i in intents for i in source_intents):
        return ("query_source", "正在分析来源渠道数据...", "chart-bar")
    if "kf_stats" in intents:
        return ("query_kf", "正在查询客服工作数据...", "user-tie")
    if "department_stats" in intents or "company_stats" in intents:
        return ("query_department", "正在查询部门业绩数据...", "building")
    if "hospital_ranking" in intents:
        return ("query_ranking", "正在查询医院排名数据...", "trophy")
    if "churn_warning" in intents:
        return ("query_churn", "正在排查流失风险客户...", "exclamation-triangle")
    if "hospital_analysis" in intents or "hospital_deals" in intents:
        return ("query_hospital_data", "正在查询医院经营数据...", "hospital")
    if "customer_lifecycle" in intents:
        return ("query_lifecycle", "正在分析客户生命周期...", "chart-line")
    if any(i in intents for i in hospital_intents):
        if has_ctx:
            return ("query_kb_ctx", "正在根据客户情况检索知识库...", "search")
        return ("query_kb", "正在医院知识库中检索...", "search")
    return ("query_general", "正在检索相关数据...", "search")


@app.route("/api/dental/chat/stream", methods=["POST"])
@limiter.limit("30 per minute")
def dental_chat_stream():
    """口腔客服对话接口 - SSE 流式进度推送"""
    data = request.json
    if not data:
        return jsonify({"error": "请提供JSON数据"}), 400

    raw_question = data.get("messages", "") or data.get("question", "")
    session_id = data.get("session_id", "")
    if not session_id:
        session_id = conversation_store.create_session()

    try:
        question = validate_question(raw_question)
    except ValueError as e:
        return jsonify({"error": str(e), "session_id": session_id}), 400

    def generate():
        try:
            # Step 1: 理解问题
            yield _sse_event({"type": "step", "step": "intent", "message": "正在理解您的问题...", "icon": "brain"})

            logger.info(f"[stream] 收到问题 (长度: {len(question)}字符, session: {session_id[:8]}...)")

            # 获取会话历史
            history = conversation_store.get_history(session_id)

            # 处理选择/短回复（与原接口相同逻辑）
            query_question = question
            if len(question) <= 10 and history:
                last_assistant_msg = ""
                for msg in reversed(history):
                    if msg['role'] == 'assistant':
                        last_assistant_msg = msg['content']
                        break

                if "找到了" in last_assistant_msg and "家匹配" in last_assistant_msg:
                    hospital_pattern = r'\d+\.\s*[✅⏸️📋]?\s*\*\*(.+?)\*\*'
                    hospitals_in_list = re.findall(hospital_pattern, last_assistant_msg)
                    selected_hospital = None
                    num_pattern = r'第?(\d+)家?'
                    num_match = re.search(num_pattern, question)
                    if num_match:
                        idx = int(num_match.group(1)) - 1
                        if 0 <= idx < len(hospitals_in_list):
                            selected_hospital = hospitals_in_list[idx]
                    else:
                        for h in hospitals_in_list:
                            if any(city in question for city in ["北京", "上海", "西安", "广州", "深圳", "成都", "武汉", "重庆", "杭州", "南京"]):
                                for city in ["北京", "上海", "西安", "广州", "深圳", "成都", "武汉", "重庆", "杭州", "南京"]:
                                    if city in question and city in h:
                                        selected_hospital = h
                                        break
                            if selected_hospital:
                                break
                    if selected_hospital:
                        query_question = f"{selected_hospital} 详情介绍"
                        logger.info(f"[stream] 用户选择医院: {selected_hospital}")

            if len(question) <= 6 and history and query_question == question:
                hospital_name = None
                for msg in reversed(history):
                    if msg['role'] == 'user':
                        hospital_pattern = r'([\u4e00-\u9fa5]{2,8}(?:口腔|医院|诊所|门诊))'
                        matches = re.findall(hospital_pattern, msg['content'])
                        if matches:
                            hospital_name = matches[0]
                            break
                if hospital_name:
                    if any(kw in question for kw in ["价格", "多少钱", "费用"]):
                        query_question = f"{hospital_name} 价格"
                    elif any(kw in question for kw in ["医生", "专家", "大夫"]):
                        query_question = f"{hospital_name} 医生"
                    elif any(kw in question for kw in ["活动", "优惠", "促销"]):
                        query_question = f"{hospital_name} 活动"
                    elif any(kw in question for kw in ["需要", "好的", "可以", "好", "要", "是的", "对", "嗯", "行"]):
                        query_question = f"{hospital_name} 价格介绍"
                    else:
                        query_question = f"{hospital_name} {question}"
                    logger.info(f"[stream] 短回复扩展查询: {query_question}")

            # Step 2: 意图识别完成，推送即将查询的步骤
            keywords = extract_keywords_with_llm(query_question)
            step_name, step_msg, step_icon = _intent_to_step_message(keywords)
            yield _sse_event({"type": "step", "step": step_name, "message": step_msg, "icon": step_icon})

            # Step 3: 执行智能查询
            results = smart_query(query_question)
            logger.info(f"[stream] 查询结果: {list(results.keys())}")

            # Step 4: 数据就绪
            if results:
                yield _sse_event({"type": "step", "step": "data_ready", "message": "数据检索完成，正在整理分析...", "icon": "check-circle"})

            # Step 5: 判断是否需要 LLM
            needs_llm = "customer_analysis" in results or not results
            if needs_llm:
                yield _sse_event({"type": "step", "step": "llm", "message": "正在生成专业分析报告...", "icon": "pen-nib"})

            # 生成回复
            response = generate_response(question, results, history)
            logger.info(f"[stream] 生成回复成功")

            # 保存对话
            conversation_store.add_message(session_id, "user", question)
            conversation_store.add_message(session_id, "assistant", response)

            # 处理返回数据
            processed_data = {}
            for k, v in results.items():
                if isinstance(v, list):
                    processed_data[k] = v[:3] if v else []
                elif isinstance(v, dict):
                    processed_data[k] = v
                else:
                    processed_data[k] = v

            # 最终结果
            yield _sse_event({
                "type": "complete",
                "success": True,
                "session_id": session_id,
                "question": question,
                "answer": response,
                "data": processed_data
            })

        except Exception as e:
            import traceback
            logger.error(f"[stream] 处理请求失败: {type(e).__name__}: {e}\n{traceback.format_exc()}")
            yield _sse_event({
                "type": "complete",
                "success": False,
                "session_id": session_id,
                "answer": "处理请求时发生错误，请稍后重试",
                "data": {}
            })

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
        }
    )


@app.route("/api/dental/query", methods=["POST"])
@require_api_key  # 需要认证
@limiter.limit("10 per minute")  # 更严格的速率限制
def dental_query():
    """
    直接SQL查询接口（仅供授权调试使用）

    需要设置 API_SECRET_KEY 环境变量并在请求头中提供:
    Authorization: Bearer <your_api_key>
    """
    try:
        data = request.json
        if not data:
            return jsonify({"error": "请提供JSON数据"}), 400

        sql = data.get("sql", "").strip()

        if not sql:
            return jsonify({"error": "请提供SQL"}), 400

        # 更严格的安全检查
        sql_upper = sql.upper()

        # 必须以 SELECT 开头
        if not sql_upper.startswith("SELECT"):
            return jsonify({"error": "只允许SELECT查询"}), 400

        # 禁止危险关键词
        dangerous_keywords = [
            "INSERT", "UPDATE", "DELETE", "DROP", "CREATE", "ALTER",
            "TRUNCATE", "EXEC", "EXECUTE", "UNION", "--", "/*", "*/",
            "INTO OUTFILE", "INTO DUMPFILE", "LOAD_FILE"
        ]
        for keyword in dangerous_keywords:
            if keyword in sql_upper:
                return jsonify({"error": f"不允许使用 {keyword}"}), 400

        results = query_db(sql)
        return jsonify({
            "success": True,
            "count": len(results),
            "data": results[:50]
        }, cls=CustomEncoder)

    except pymysql.Error as e:
        logger.warning(f"SQL执行错误: {type(e).__name__}")
        return jsonify({"error": "SQL执行错误"}), 400
    except Exception as e:
        logger.error(f"查询端点错误: {type(e).__name__}")
        return jsonify({"error": "处理请求时发生错误"}), 500


@app.route("/api/dental/customer/<int:client_id>", methods=["GET"])
@limiter.limit("30 per minute")
def get_customer_analysis(client_id: int):
    """
    客户成交概率分析接口（带缓存）

    查询参数:
        refresh=1  强制刷新缓存

    流程: 先查缓存 → 命中直接返回 → 未命中则分析并入库
    """
    try:
        if client_id <= 0:
            return jsonify({"error": "无效的客户ID"}), 400

        force_refresh = request.args.get('refresh') == '1'

        # 1. 先查缓存（非强制刷新时）
        if not force_refresh:
            cached = get_analysis_cache(client_id)
            if cached:
                return jsonify({
                    "success": True,
                    "data": cached,
                    "cached": True
                })

        # 2. 缓存未命中或强制刷新 → 完整分析
        result = query_customer_full_analysis(client_id)

        if 'error' in result:
            return jsonify({"success": False, "error": result['error']}), 404

        # 3. 存入缓存
        save_analysis_cache(client_id, result)

        return jsonify({
            "success": True,
            "data": result,
            "cached": False
        })

    except Exception as e:
        logger.error(f"客户查询失败: {type(e).__name__}")
        return jsonify({"error": "查询失败，请稍后重试"}), 500


# ============================================================
# 来源分析 API
# ============================================================
# 导入来源分析模块
try:
    import source_analysis
    SOURCE_ANALYSIS_AVAILABLE = True
except ImportError:
    SOURCE_ANALYSIS_AVAILABLE = False
    logger.warning("source_analysis 模块未找到，来源分析功能不可用")


@app.route("/api/dental/source/analyze", methods=["POST"])
@limiter.limit("10 per minute")
def analyze_source():
    """
    来源分析接口 - 分析指定来源的多维度数据

    请求体:
    {
        "source": "牙舒丽",     # 必填，来源关键词
        "days": "本月天数",      # 可选，分析天数，默认本月，最大365天
        "dimensions": ["region", "project", "hospital", "trend", "keywords"]  # 可选，分析维度
    }

    Returns:
        来源综合分析报告
    """
    if not SOURCE_ANALYSIS_AVAILABLE:
        return jsonify({"error": "来源分析功能不可用"}), 503

    try:
        data = request.get_json() or {}
        source_keyword = data.get("source", "").strip()

        if not source_keyword:
            return jsonify({"error": "请提供来源关键词，如: 牙舒丽"}), 400

        # 默认本月，最大365天
        days = normalize_query_days(data.get("days"), 365)
        dimensions = data.get("dimensions", ["region", "project", "hospital", "comparison", "trend", "keywords"])

        # 获取来源ID
        source_ids = source_analysis.get_source_ids(source_keyword)

        if not source_ids:
            return jsonify({"error": f"未找到包含'{source_keyword}'的来源"}), 404

        # 获取来源详情
        source_info = source_analysis.get_all_source_info()
        source_details = [
            {
                'id': sid,
                'name': source_info.get(sid, {}).get('name', '未知'),
                'type': source_analysis.classify_source(
                    source_info.get(sid, {}).get('name', ''),
                    source_info.get(sid, {}).get('parent_name', '')
                ),
                'type_label': {
                    'paid': '竞价/付费',
                    'organic': '自然来源',
                    'channel': '渠道合作',
                    'unknown': '未分类'
                }.get(source_analysis.classify_source(
                    source_info.get(sid, {}).get('name', ''),
                    source_info.get(sid, {}).get('parent_name', '')
                ), '未知')
            }
            for sid in source_ids
        ]

        result = {
            'source_keyword': source_keyword,
            'source_ids': source_ids,
            'source_details': source_details,
            'analysis_period_days': days,
            'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        }

        # 按需执行各维度分析
        if "region" in dimensions:
            result['region_distribution'] = source_analysis.analyze_region_distribution(source_ids, days)

        if "project" in dimensions:
            result['project_distribution'] = source_analysis.analyze_project_distribution(source_ids, days)

        if "hospital" in dimensions:
            result['hospital_distribution'] = source_analysis.analyze_hospital_distribution(source_ids, days)

        if "comparison" in dimensions:
            result['source_type_comparison'] = source_analysis.analyze_source_type_comparison(source_keyword, days)

        if "trend" in dimensions:
            result['time_trend'] = source_analysis.analyze_time_trend(source_ids, days, 'week')

        if "keywords" in dimensions:
            result['consultation_keywords'] = source_analysis.analyze_consultation_keywords(source_ids, min(days, 30))

        return jsonify({
            "success": True,
            "data": result
        })

    except Exception as e:
        logger.error(f"来源分析失败: {type(e).__name__}: {e}")
        return jsonify({"error": "分析失败，请稍后重试"}), 500


@app.route("/api/dental/source/list", methods=["GET"])
@limiter.limit("30 per minute")
def list_sources():
    """
    获取来源列表接口 - 查询所有可用的来源及其统计

    查询参数:
        keyword: 可选，过滤来源名称
        min_count: 可选，最小客户数，默认100

    Returns:
        来源列表及统计
    """
    if not SOURCE_ANALYSIS_AVAILABLE:
        return jsonify({"error": "来源分析功能不可用"}), 503

    try:
        keyword = request.args.get("keyword", "").strip()
        min_count = int(request.args.get("min_count", 100))

        # 查询来源统计
        if keyword:
            sql = """
                SELECT
                    c.from_type,
                    l.name as source_name,
                    p.name as parent_name,
                    COUNT(*) as total_count,
                    SUM(CASE WHEN c.Status = 1 THEN 1 ELSE 0 END) as converted_count
                FROM un_channel_client c
                LEFT JOIN un_linkage l ON c.from_type = l.linkageid
                LEFT JOIN un_linkage p ON l.parentid = p.linkageid
                WHERE c.from_type > 0 AND l.name LIKE %s
                GROUP BY c.from_type, l.name, p.name
                HAVING total_count >= %s
                ORDER BY total_count DESC
                LIMIT 100
            """
            results = query_qudao_db(sql, (f"%{keyword}%", min_count))
        else:
            sql = """
                SELECT
                    c.from_type,
                    l.name as source_name,
                    p.name as parent_name,
                    COUNT(*) as total_count,
                    SUM(CASE WHEN c.Status = 1 THEN 1 ELSE 0 END) as converted_count
                FROM un_channel_client c
                LEFT JOIN un_linkage l ON c.from_type = l.linkageid
                LEFT JOIN un_linkage p ON l.parentid = p.linkageid
                WHERE c.from_type > 0
                GROUP BY c.from_type, l.name, p.name
                HAVING total_count >= %s
                ORDER BY total_count DESC
                LIMIT 100
            """
            results = query_qudao_db(sql, (min_count,))

        sources = []
        for r in results:
            source_type = source_analysis.classify_source(r['source_name'], r['parent_name'])
            rate = (r['converted_count'] or 0) / r['total_count'] * 100 if r['total_count'] > 0 else 0

            sources.append({
                'id': r['from_type'],
                'name': r['source_name'] or f"未知({r['from_type']})",
                'parent': r['parent_name'] or '',
                'type': source_type,
                'type_label': {
                    'paid': '竞价/付费',
                    'organic': '自然来源',
                    'channel': '渠道合作',
                    'unknown': '未分类'
                }.get(source_type, '未知'),
                'total_count': r['total_count'],
                'converted_count': r['converted_count'] or 0,
                'conversion_rate': round(rate, 2)
            })

        return jsonify({
            "success": True,
            "count": len(sources),
            "data": sources
        }, cls=CustomEncoder)

    except Exception as e:
        logger.error(f"来源列表查询失败: {type(e).__name__}: {e}")
        return jsonify({"error": "查询失败，请稍后重试"}), 500


@app.route("/api/dental/source/compare", methods=["POST"])
@limiter.limit("10 per minute")
def compare_sources():
    """
    来源对比接口 - 对比多个来源的数据

    请求体:
    {
        "sources": ["牙舒丽", "贝色", "美佳"],  # 必填，来源关键词列表
        "days": "本月天数"                       # 可选，分析天数，默认本月，最大365天
    }

    Returns:
        多来源对比数据
    """
    if not SOURCE_ANALYSIS_AVAILABLE:
        return jsonify({"error": "来源分析功能不可用"}), 503

    try:
        data = request.get_json() or {}
        source_keywords = data.get("sources", [])

        if not source_keywords or len(source_keywords) < 2:
            return jsonify({"error": "请提供至少2个来源进行对比"}), 400

        if len(source_keywords) > 5:
            return jsonify({"error": "最多支持5个来源对比"}), 400

        # 默认本月，最大365天
        days = normalize_query_days(data.get("days"), 365)

        comparisons = []
        for keyword in source_keywords:
            source_ids = source_analysis.get_source_ids(keyword)
            if source_ids:
                region_data = source_analysis.analyze_region_distribution(source_ids, days)
                project_data = source_analysis.analyze_project_distribution(source_ids, days)
                hospital_data = source_analysis.analyze_hospital_distribution(source_ids, days)

                # 获取来源类型
                source_info = source_analysis.get_all_source_info()
                source_types = [
                    source_analysis.classify_source(
                        source_info.get(sid, {}).get('name', ''),
                        source_info.get(sid, {}).get('parent_name', '')
                    )
                    for sid in source_ids
                ]
                main_type = max(set(source_types), key=source_types.count) if source_types else 'unknown'

                comparisons.append({
                    'source': keyword,
                    'source_ids': source_ids,
                    'source_type': main_type,
                    'type_label': {
                        'paid': '竞价/付费',
                        'organic': '自然来源',
                        'channel': '渠道合作',
                        'unknown': '未分类'
                    }.get(main_type, '未知'),
                    'total_customers': region_data.get('total_customers', 0),
                    'total_converted': region_data.get('total_converted', 0),
                    'conversion_rate': region_data.get('conversion_rate', 0),
                    'top_regions': [r['province'] for r in region_data.get('regions', [])[:5]],
                    'top_projects': [p['project'] for p in project_data.get('projects', [])[:5]],
                    'total_orders': hospital_data.get('total_orders', 0),
                    'total_amount': hospital_data.get('total_amount', 0),
                })
            else:
                comparisons.append({
                    'source': keyword,
                    'error': f"未找到包含'{keyword}'的来源"
                })

        return jsonify({
            "success": True,
            "analysis_period_days": days,
            "data": comparisons
        }, cls=CustomEncoder)

    except Exception as e:
        logger.error(f"来源对比失败: {type(e).__name__}: {e}")
        return jsonify({"error": "对比失败，请稍后重试"}), 500


@app.route("/health", methods=["GET"])
def health():
    """健康检查"""
    return jsonify({"status": "ok", "service": "dental-chat-api", "version": "2.1"})


# ============================================================
# 错误处理
# ============================================================
@app.errorhandler(429)
def ratelimit_handler(e):
    """速率限制错误处理"""
    return jsonify({"error": "请求过于频繁，请稍后再试"}), 429


@app.errorhandler(500)
def internal_error_handler(e):
    """内部错误处理"""
    return jsonify({"error": "服务器内部错误"}), 500


# ============================================================
# 客服工作台 API 端点 (替代 chadm.beise.com 的接口，直连同库)
# ============================================================

@app.route("/api/dental/kf/warning_stats", methods=["POST"])
@limiter.limit("30 per minute")
def kf_warning_stats():
    """
    客服工作台统计 + 客户列表（高性能版 v3）
    v3 修复：Status/client_status 整数→文本映射，check_time 处理 EditTime=0，
    from_manager 始终关联，kf_name→kf_id 查找，follow_status 过滤，重单/逾期统计
    """
    try:
        data = request.json or {}

        kf_id = data.get('kf_id', 0)
        kf_name = data.get('kf_name', '')  # 支持按客服姓名查找
        follow_status = data.get('follow_status')
        ai_intention = data.get('ai_intention')
        client_type = data.get('client_type')
        register_start = data.get('register_start')
        register_end = data.get('register_end')
        from_types = data.get('from_types')
        zx_district = data.get('zx_district')
        plastics_intention = data.get('plastics_intention')
        client_status_filter = data.get('client_status')
        keyword_type = data.get('keyword_type')
        keyword = data.get('keyword')
        page = max(1, int(data.get('page', 1)))
        page_size = min(100, max(1, int(data.get('page_size', 20))))

        # ---- kf_name → kf_id 查找 ----
        if not kf_id and kf_name:
            kf_result = query_qudao_db(
                "SELECT userid FROM un_admin WHERE realname = %s LIMIT 1",
                (str(kf_name),)
            )
            if kf_result:
                kf_id = int(kf_result[0]['userid'])

        # ---- ai_intention 预过滤（跨库，需先从 hospital_db 查符合条件的 client_id） ----
        ai_intention_client_ids = None  # None=不过滤, []=无匹配, [id,...]=有匹配
        if ai_intention is not None:
            ai_val = int(ai_intention)
            # 根据意向等级确定概率区间
            if ai_val == 1:
                prob_cond = "probability >= 60"
            elif ai_val == 2:
                prob_cond = "probability >= 30 AND probability < 60"
            elif ai_val == 3:
                prob_cond = "probability >= 10 AND probability < 30"
            elif ai_val == 4:
                prob_cond = "probability < 10"
            else:
                prob_cond = None

            if prob_cond:
                try:
                    # 从 hospital_db 的缓存表查询，提取 probability 字段
                    cache_sql = f"""
                        SELECT client_id FROM customer_analysis_cache
                        WHERE JSON_EXTRACT(analysis_json, '$.analysis.probability') IS NOT NULL
                          AND CAST(JSON_EXTRACT(analysis_json, '$.analysis.probability') AS DECIMAL(5,2)) >= %s
                          AND CAST(JSON_EXTRACT(analysis_json, '$.analysis.probability') AS DECIMAL(5,2)) < %s
                        LIMIT 5000
                    """
                    # 映射概率区间
                    prob_ranges = {1: (60, 9999), 2: (30, 60), 3: (10, 30), 4: (0, 10)}
                    low, high = prob_ranges.get(ai_val, (0, 9999))
                    cache_result = query_db(cache_sql, (low, high))
                    ai_intention_client_ids = [r['client_id'] for r in cache_result] if cache_result else []
                except Exception as e:
                    logger.warning(f"AI意向预过滤失败(不影响主流程): {e}")
                    ai_intention_client_ids = None  # 失败时不过滤

        # ---- 构建 WHERE ----
        conditions = []
        params = []
        has_filter = False

        # ai_intention 过滤（基于预查询的 client_id 列表）
        if ai_intention_client_ids is not None:
            if len(ai_intention_client_ids) == 0:
                # 没有符合条件的客户，直接返回空
                conditions.append("1=0")
            else:
                placeholders = ','.join(['%s'] * len(ai_intention_client_ids))
                conditions.append(f"c.Client_Id IN ({placeholders})")
                params.extend(ai_intention_client_ids)
            has_filter = True

        if kf_id:
            conditions.append("c.KfId = %s")
            params.append(int(kf_id))
            has_filter = True

        if register_start:
            conditions.append("c.RegisterTime >= UNIX_TIMESTAMP(%s)")
            params.append(str(register_start))
            has_filter = True
        elif not kf_id:
            # 无客服限制时，默认只查近30天注册的客户，避免全表扫描
            conditions.append("c.RegisterTime >= UNIX_TIMESTAMP(DATE_SUB(NOW(), INTERVAL 30 DAY))")
            has_filter = True

        if register_end:
            conditions.append("c.RegisterTime <= UNIX_TIMESTAMP(CONCAT(%s, ' 23:59:59'))")
            params.append(str(register_end))
            has_filter = True

        if zx_district:
            conditions.append("c.zx_District = %s")
            params.append(str(zx_district))
            has_filter = True

        if plastics_intention:
            conditions.append("c.PlasticsIntention LIKE %s")
            params.append(f"%{plastics_intention}%")
            has_filter = True

        if client_status_filter:
            conditions.append("c.client_status = %s")
            params.append(str(client_status_filter))
            has_filter = True

        if keyword and keyword_type:
            kw = str(keyword).strip()
            if keyword_type == 'client_id' and kw.isdigit():
                conditions.append("c.Client_Id = %s")
                params.append(int(kw))
            elif keyword_type == 'client_name':
                conditions.append("c.ClientName LIKE %s")
                params.append(f"%{kw}%")
            elif keyword_type == 'mobilephone':
                conditions.append("c.MobilePhone LIKE %s")
                params.append(f"%{kw}%")
            elif keyword_type == 'wx':
                conditions.append("c.WeiXin LIKE %s")
                params.append(f"%{kw}%")
            has_filter = True

        if from_types:
            conditions.append("l.name LIKE %s")
            params.append(f"%{from_types}%")
            has_filter = True

        # client_type: 1=新客, 2=老客（新客=注册30天内，老客=注册超30天）
        if client_type:
            ct = int(client_type)
            if ct == 1:
                conditions.append("c.RegisterTime >= UNIX_TIMESTAMP(DATE_SUB(NOW(), INTERVAL 30 DAY))")
            elif ct == 2:
                conditions.append("c.RegisterTime < UNIX_TIMESTAMP(DATE_SUB(NOW(), INTERVAL 30 DAY))")
            has_filter = True

        # ---- 今日已跟进 client_id（按需查询：follow_status 过滤时 + AI 意向统计时） ----
        from datetime import datetime as _dt
        _today_ts = int(_dt.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
        today_followed_ids = None  # None=未查询, []=已查无结果

        def _get_today_followed():
            nonlocal today_followed_ids
            if today_followed_ids is not None:
                return today_followed_ids
            if kf_id:
                sql = f"""SELECT DISTINCT crm.client_id FROM un_channel_crm crm
                    INNER JOIN un_channel_client cc ON crm.client_id = cc.Client_Id
                    WHERE crm.addtime >= {_today_ts} AND cc.KfId = %s LIMIT 10000"""
                result = query_qudao_db(sql, (int(kf_id),))
            else:
                sql = f"SELECT DISTINCT client_id FROM un_channel_crm WHERE addtime >= {_today_ts} LIMIT 10000"
                result = query_qudao_db(sql)
            today_followed_ids = [r['client_id'] for r in result] if result else []
            return today_followed_ids

        # follow_status 过滤
        # 0=待跟进（今天无跟进记录）, 1=已跟进（今天有跟进记录）, 2=逾期（超3天未跟进）
        follow_status_join = ""
        if follow_status is not None:
            fs = int(follow_status)
            if fs in (0, 1):
                _followed = _get_today_followed()
                if fs == 0:
                    # 待跟进：今天没有跟进记录，且未完成
                    conditions.append("c.Status NOT IN (1, 10, 11)")
                    if _followed:
                        ph = ','.join(['%s'] * len(_followed))
                        conditions.append(f"c.Client_Id NOT IN ({ph})")
                        params.extend(_followed)
                else:
                    # 已跟进：今天有跟进记录
                    if _followed:
                        ph = ','.join(['%s'] * len(_followed))
                        conditions.append(f"c.Client_Id IN ({ph})")
                        params.extend(_followed)
                    else:
                        conditions.append("1=0")  # 今天没人跟进，返回空
                has_filter = True
            elif fs == 2:
                # 逾期：超过3天未跟进，且未完成
                conditions.append("c.Status NOT IN (1, 10, 11)")
                follow_status_join = """LEFT JOIN (
                    SELECT client_id, MAX(addtime) as last_time
                    FROM un_channel_crm GROUP BY client_id
                ) _crm_last ON c.Client_Id = _crm_last.client_id"""
                conditions.append("(_crm_last.last_time IS NULL OR FROM_UNIXTIME(_crm_last.last_time) < DATE_SUB(NOW(), INTERVAL 3 DAY))")
                has_filter = True

        where_clause = " AND ".join(conditions) if conditions else "1=1"

        # ---- Status/client_status 映射 ----
        status_case = """
            CASE c.Status
                WHEN 0 THEN '待审核' WHEN 1 THEN '无效客户' WHEN 2 THEN '跟进中'
                WHEN 3 THEN '签证办理' WHEN 4 THEN '购买机票' WHEN 5 THEN '到达韩国'
                WHEN 6 THEN '医疗服务中' WHEN 7 THEN '旅游购物' WHEN 8 THEN '回国'
                WHEN 9 THEN '财务核算' WHEN 10 THEN '已完成' WHEN 11 THEN '已成交'
                ELSE '未知'
            END"""
        client_status_case = """
            CASE c.client_status
                WHEN 0 THEN '暂无' WHEN 1 THEN '极好' WHEN 2 THEN '好' WHEN 3 THEN '较好'
                WHEN 4 THEN '一般' WHEN 5 THEN '无意向' WHEN 6 THEN '确定赴韩'
                WHEN 7 THEN '旅游意向' WHEN 8 THEN '已在韩国' WHEN 9 THEN '已预约'
                WHEN 10 THEN '已到院' WHEN 11 THEN '已消费' WHEN 12 THEN '未联系上'
                ELSE '暂无'
            END"""

        # ---- 1. 总数 ----
        if has_filter:
            count_sql = f"""SELECT COUNT(*) as cnt FROM un_channel_client c
                LEFT JOIN un_linkage l ON c.from_type = l.linkageid
                {follow_status_join}
                WHERE {where_clause}"""
            count_result = query_qudao_db(count_sql, tuple(params))
            total_count = int(count_result[0]['cnt']) if count_result else 0
        else:
            approx_sql = "SELECT TABLE_ROWS FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'un_channel_client'"
            approx_result = query_qudao_db(approx_sql)
            total_count = int(approx_result[0]['TABLE_ROWS']) if approx_result else 0

        # ---- 2. 分页列表 ----
        offset = (page - 1) * page_size
        list_params = list(params) + [page_size, offset]

        list_sql = f"""
            SELECT
                c.Client_Id as client_id,
                c.ClientName as client_name,
                {status_case} as status,
                {client_status_case} as client_status,
                0 as ai_intention,
                COALESCE(ld.name, c.zx_District) as zx_district,
                COALESCE(lp.name, c.PlasticsIntention) as plastics_intention,
                c.MobilePhone as mobilephone,
                c.WeiXin as wx,
                COALESCE(ld2.name, c.District) as district,
                DATE_FORMAT(FROM_UNIXTIME(c.RegisterTime), '%%Y-%%m-%%d %%H:%%i') as register_time,
                CASE WHEN c.EditTime > 0
                     THEN DATE_FORMAT(FROM_UNIXTIME(c.EditTime), '%%Y-%%m-%%d %%H:%%i')
                     ELSE NULL
                END as check_time,
                c.Remarks as remarks,
                a.realname as kf_name,
                l.name as from_manager,
                1 as client_type
            FROM un_channel_client c
            LEFT JOIN un_admin a ON c.KfId = a.userid
            LEFT JOIN un_linkage l ON c.from_type = l.linkageid
            LEFT JOIN un_linkage lp ON c.PlasticsIntention = lp.linkageid
            LEFT JOIN un_linkage ld ON c.zx_District = ld.linkageid
            LEFT JOIN un_linkage ld2 ON c.District = ld2.linkageid
            {follow_status_join}
            WHERE {where_clause}
            ORDER BY c.Client_Id DESC
            LIMIT %s OFFSET %s
        """
        customer_list = query_qudao_db(list_sql, tuple(list_params))

        # ---- 3. 为当前页客户查 last_crm ----
        if customer_list:
            client_ids = [r['client_id'] for r in customer_list]
            placeholders = ','.join(['%s'] * len(client_ids))

            crm_sql = f"""
                SELECT client_id,
                       DATE_FORMAT(FROM_UNIXTIME(MAX(addtime)), '%%Y-%%m-%%d %%H:%%i') as last_crm,
                       GREATEST(0, DATEDIFF(NOW(), FROM_UNIXTIME(MAX(addtime)))) as overdue_days
                FROM un_channel_crm
                WHERE client_id IN ({placeholders})
                GROUP BY client_id
            """
            crm_result = query_qudao_db(crm_sql, tuple(client_ids))
            crm_map = {r['client_id']: r for r in crm_result}

            for item in customer_list:
                cid = item['client_id']
                crm = crm_map.get(cid)
                item['last_crm'] = crm['last_crm'] if crm else None
                item['overdue_days'] = int(crm['overdue_days']) if crm else 0
                if item.get('from_manager') is None:
                    item['from_manager'] = ''

        # ---- 4. 查 AI 意向（从分析缓存） ----
        # ai_intention_wait/already: 各意向等级的客户数，用于前端筛选按钮显示计数
        ai_intention_wait = {1: 0, 2: 0, 3: 0, 4: 0}
        ai_intention_already = {1: 0, 2: 0, 3: 0, 4: 0}
        if customer_list:
            try:
                client_ids = [r['client_id'] for r in customer_list]
                placeholders = ','.join(['%s'] * len(client_ids))
                cache_sql = f"SELECT client_id, analysis_json FROM customer_analysis_cache WHERE client_id IN ({placeholders})"
                cache_result = query_db(cache_sql, tuple(client_ids))
                if cache_result:
                    _today_ids_set = set(_get_today_followed())  # 仅在有缓存命中时才查
                    cache_map = {}
                    for row in cache_result:
                        try:
                            aj = json.loads(row['analysis_json']) if isinstance(row['analysis_json'], str) else row['analysis_json']
                            prob = aj.get('analysis', {}).get('probability', 0)
                            if prob >= 60:
                                intention = 1  # 意向较好
                            elif prob >= 30:
                                intention = 2  # 意向一般
                            elif prob >= 10:
                                intention = 3  # 意向不好
                            else:
                                intention = 4  # 其他
                            cache_map[row['client_id']] = intention
                            # 统计意向分布
                            if row['client_id'] in _today_ids_set:
                                ai_intention_already[intention] = ai_intention_already.get(intention, 0) + 1
                            else:
                                ai_intention_wait[intention] = ai_intention_wait.get(intention, 0) + 1
                        except Exception:
                            pass
                    for item in customer_list:
                        cid = item['client_id']
                        if cid in cache_map:
                            item['ai_intention'] = cache_map[cid]
            except Exception as e:
                logger.warning(f"查询AI意向缓存失败(不影响主流程): {e}")

        # ---- 5. 统计：逾期数、重单数（仅当有 kf_id 时计算，否则太慢） ----
        today_over_num = 0
        rep_num_1 = 0
        rep_num_2 = 0
        rep_num_3 = 0
        rep_num_4 = 0

        if kf_id:
            try:
                # 逾期：该客服名下超过3天未跟进的客户
                over_sql = """
                    SELECT COUNT(*) as cnt FROM (
                        SELECT c.Client_Id
                        FROM un_channel_client c
                        LEFT JOIN (
                            SELECT client_id, MAX(addtime) as last_time
                            FROM un_channel_crm GROUP BY client_id
                        ) cr ON c.Client_Id = cr.client_id
                        WHERE c.KfId = %s
                          AND c.Status NOT IN (1, 10, 11)
                          AND (cr.last_time IS NULL OR FROM_UNIXTIME(cr.last_time) < DATE_SUB(NOW(), INTERVAL 3 DAY))
                        LIMIT 10000
                    ) t
                """
                over_result = query_qudao_db(over_sql, (int(kf_id),))
                today_over_num = int(over_result[0]['cnt']) if over_result else 0
            except Exception as e:
                logger.warning(f"逾期统计查询失败: {e}")

            try:
                # 重单：该客服名下手机号重复的客户
                rep_sql = """
                    SELECT COUNT(*) as cnt FROM un_channel_client c
                    WHERE c.KfId = %s
                      AND c.MobilePhone IN (
                          SELECT MobilePhone FROM un_channel_client
                          WHERE MobilePhone IS NOT NULL AND MobilePhone != ''
                          GROUP BY MobilePhone HAVING COUNT(*) > 1
                      )
                """
                rep_result = query_qudao_db(rep_sql, (int(kf_id),))
                rep_num_1 = int(rep_result[0]['cnt']) if rep_result else 0
            except Exception as e:
                logger.warning(f"重单统计查询失败: {e}")

        today_finished = 0

        return app.response_class(
            response=json.dumps({
                "status": 1,
                "data": {
                    "all_total": total_count,
                    "today_finished_num": today_finished,
                    "today_progress_num": max(0, total_count - today_finished),
                    "today_over_num": today_over_num,
                    "finance_deal_sum": 0,
                    "finance_deal_amount": 0,
                    "completionProgress": round(today_finished / total_count * 100, 1) if total_count > 0 else 0,
                    "new_num": total_count,
                    "old_num": 0,
                    "ai_intention_wait": ai_intention_wait,
                    "ai_intention_already": ai_intention_already,
                    "replication_num_1": rep_num_1,
                    "replication_num_2": rep_num_2,
                    "replication_num_3": rep_num_3,
                    "replication_num_4": rep_num_4,
                    "list": customer_list,
                    "count": total_count
                }
            }, cls=CustomEncoder),
            status=200,
            mimetype='application/json'
        )

    except Exception as e:
        logger.error(f"客服工作台统计查询失败: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"status": 0, "msg": str(e)}), 500


@app.route("/api/dental/kf/today_stats", methods=["POST"])
@limiter.limit("10 per minute")
def kf_today_stats():
    """
    今日跟进统计（独立接口，前端异步调用，不阻塞列表加载）
    """
    try:
        data = request.json or {}
        kf_id = data.get('kf_id', 0)
        kf_name = data.get('kf_name', '')

        # kf_name → kf_id 查找（同 warning_stats）
        if not kf_id and kf_name:
            kf_result = query_qudao_db(
                "SELECT userid FROM un_admin WHERE realname = %s",
                (str(kf_name),)
            )
            if kf_result:
                kf_id = int(kf_result[0]['userid'])

        from datetime import datetime as _dt
        _now = _dt.now()
        _today_ts = int(_now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())

        if kf_id:
            today_sql = """
                SELECT COUNT(DISTINCT client_id) as cnt
                FROM un_channel_crm
                WHERE addtime >= %s AND client_id IN (
                    SELECT Client_Id FROM un_channel_client WHERE KfId = %s
                )
            """
            today_result = query_qudao_db(today_sql, (_today_ts, kf_id))
        else:
            today_sql = "SELECT COUNT(DISTINCT client_id) as cnt FROM un_channel_crm WHERE addtime >= %s"
            today_result = query_qudao_db(today_sql, (_today_ts,))

        today_finished = int(today_result[0]['cnt']) if today_result else 0

        return jsonify({
            "status": 1,
            "data": {
                "today_finished_num": today_finished
            }
        })
    except Exception as e:
        logger.error(f"今日统计查询失败: {e}")
        return jsonify({"status": 0, "msg": str(e)}), 500


@app.route("/api/dental/kf/replication", methods=["POST"])
@limiter.limit("30 per minute")
def kf_replication_list():
    """
    重单列表 - 按手机号检测重复客户（高性能版 v2）
    默认只查近180天内注册的客户，避免全表扫描
    """
    try:
        data = request.json or {}
        page = max(1, int(data.get('page', 1)))
        page_size = min(100, max(1, int(data.get('page_size', 20))))
        days = int(data.get('days', 180))  # 默认180天
        offset = (page - 1) * page_size

        import time as _time
        cutoff_ts = int(_time.time()) - days * 86400

        # 两步法：先找近 N 天内重复手机号，再取客户
        # 第一步：找重复手机号（只扫描近 N 天数据）
        dup_sql = """
            SELECT MobilePhone FROM un_channel_client
            WHERE MobilePhone IS NOT NULL AND MobilePhone != ''
              AND RegisterTime >= %s
            GROUP BY MobilePhone HAVING COUNT(*) > 1
            LIMIT 500
        """
        dup_result = query_qudao_db(dup_sql, (cutoff_ts,))

        if not dup_result:
            return app.response_class(
                response=json.dumps({"status": 1, "data": {"list": [], "count": 0}}, cls=CustomEncoder),
                status=200, mimetype='application/json'
            )

        dup_phones = [r['MobilePhone'] for r in dup_result]
        ph = ','.join(['%s'] * len(dup_phones))

        # 第二步：取这些手机号的所有客户（包含历史客户）
        count_sql = f"SELECT COUNT(*) as cnt FROM un_channel_client WHERE MobilePhone IN ({ph})"
        count_result = query_qudao_db(count_sql, tuple(dup_phones))
        total = int(count_result[0]['cnt']) if count_result else 0

        list_sql = f"""
            SELECT
                c.Client_Id as client_id,
                c.Client_Id as id,
                c.ClientName as client_name,
                CASE c.Status
                    WHEN 0 THEN '待审核' WHEN 1 THEN '无效客户' WHEN 2 THEN '跟进中'
                    WHEN 3 THEN '签证办理' WHEN 4 THEN '购买机票' WHEN 5 THEN '到达韩国'
                    WHEN 6 THEN '医疗服务中' WHEN 7 THEN '旅游购物' WHEN 8 THEN '回国'
                    WHEN 9 THEN '财务核算' WHEN 10 THEN '已完成' WHEN 11 THEN '已成交'
                    ELSE '未知'
                END as status,
                CASE c.client_status
                    WHEN 0 THEN '暂无' WHEN 1 THEN '极好' WHEN 2 THEN '好' WHEN 3 THEN '较好'
                    WHEN 4 THEN '一般' WHEN 5 THEN '无意向' WHEN 6 THEN '确定赴韩'
                    WHEN 7 THEN '旅游意向' WHEN 8 THEN '已在韩国' WHEN 9 THEN '已预约'
                    WHEN 10 THEN '已到院' WHEN 11 THEN '已消费' WHEN 12 THEN '未联系上'
                    ELSE '暂无'
                END as client_status,
                COALESCE(ld.name, c.zx_District) as zx_district,
                c.MobilePhone as mobilephone,
                c.WeiXin as wx,
                COALESCE(ld2.name, c.District) as district,
                COALESCE(lp.name, c.PlasticsIntention) as plastics_intention,
                DATE_FORMAT(FROM_UNIXTIME(c.RegisterTime), '%%Y-%%m-%%d %%H:%%i') as register_time,
                DATE_FORMAT(FROM_UNIXTIME(c.RegisterTime), '%%Y-%%m-%%d %%H:%%i') as replication_time,
                a.realname as kf_name,
                1 as col_type,
                '手机号重复' as col_type_text,
                'red' as color
            FROM un_channel_client c
            LEFT JOIN un_admin a ON c.KfId = a.userid
            LEFT JOIN un_linkage lp ON c.PlasticsIntention = lp.linkageid
            LEFT JOIN un_linkage ld ON c.zx_District = ld.linkageid
            LEFT JOIN un_linkage ld2 ON c.District = ld2.linkageid
            WHERE c.MobilePhone IN ({ph})
            ORDER BY c.MobilePhone, c.RegisterTime DESC
            LIMIT %s OFFSET %s
        """
        result_list = query_qudao_db(list_sql, tuple(dup_phones) + (page_size, offset))

        return app.response_class(
            response=json.dumps({
                "status": 1,
                "data": {"list": result_list, "count": total}
            }, cls=CustomEncoder),
            status=200,
            mimetype='application/json'
        )

    except Exception as e:
        logger.error(f"重单列表查询失败: {e}")
        return jsonify({"status": 0, "msg": str(e)}), 500


# ============================================================
# 主程序
# ============================================================
if __name__ == "__main__":
    # 检查必要的环境变量
    if not DB_CONFIG["password"]:
        print("\n[错误] 请设置 DB_PASSWORD 环境变量")
        print("示例: export DB_PASSWORD='your_password'\n")
        exit(1)

    print("""
+============================================================+
|           口腔智能客服 API 服务 (安全版 v2.1)               |
+============================================================+
|  API端点: POST http://localhost:8090/api/dental/chat       |
|  健康检查: GET  http://localhost:8090/health               |
+============================================================+

数据源:
  - 医院知识库: hospital_db (医院、医生、价格等)
  - 渠道系统: kfsyscb (客户、跟进记录、成交数据)

安全特性:
  - SQL 注入防护 (参数化查询)
  - 配置从环境变量读取
  - API 认证保护敏感端点
  - CORS 来源限制
  - 速率限制 (30次/分钟)
  - 数据库连接池

=============================================================
  新增: 来源分析 API
=============================================================

  【来源分析】POST /api/dental/source/analyze
  分析指定来源的多维度数据（地区、项目、医院、趋势等）

  curl -X POST http://localhost:8090/api/dental/source/analyze \\
       -H "Content-Type: application/json" \\
       -d '{"source": "牙舒丽"}'
  # 默认分析本月数据，可指定days参数（最大365天）

  【来源列表】GET /api/dental/source/list
  获取所有来源及其统计数据

  curl "http://localhost:8090/api/dental/source/list?keyword=牙舒丽"

  【来源对比】POST /api/dental/source/compare
  对比多个来源的数据（默认本月，最大一年）

  curl -X POST http://localhost:8090/api/dental/source/compare \\
       -H "Content-Type: application/json" \\
       -d '{"sources": ["牙舒丽", "贝色", "美佳"]}'

=============================================================
  支持的问题类型
=============================================================

  【客户分析】
  - 成交概率: "查询客户ID 4333303 的成交概率"
  - 客户分析: "分析一下id是4333303的顾客"
  - 跟进情况: "客户4333303的跟进记录"

  【医院推荐】
  - "北京朝阳区做种植牙哪家医院好？"
  - "北京维乐口腔怎么样？"

  【医生&排班】
  - "北京维乐口腔推荐哪个医生？"
  - "王医生什么时候坐诊？"
  - "明天有哪个医生可以预约？"

  【价格&活动】
  - "种植牙多少钱？"
  - "最近有什么优惠活动？"

请求示例:
  # 客户成交概率查询
  curl -X POST http://localhost:8090/api/dental/chat \\
       -H "Content-Type: application/json" \\
       -d '{"question": "查询客户ID 4333303 的成交概率"}'

  # 医院推荐
  curl -X POST http://localhost:8090/api/dental/chat \\
       -H "Content-Type: application/json" \\
       -d '{"question": "北京朝阳区做种植牙哪家医院好？"}'
""")

    # 初始化缓存表
    try:
        init_analysis_cache_table()
    except Exception as e:
        print(f"[警告] 缓存表初始化失败: {e}，分析功能仍可正常使用（无缓存）")

    # 从环境变量读取调试模式
    debug_mode = os.getenv("FLASK_DEBUG", "False").lower() == "true"
    app.run(host="0.0.0.0", port=8090, debug=debug_mode)
