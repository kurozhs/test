"""Dental Customer Service Query Operators for AWEL flows.

This module provides intelligent query operators for dental customer service,
with predefined SQL templates based on intent detection.
"""

import json
import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

import pymysql
from dbgpt.core.awel import MapOperator
from dbgpt.core.awel.flow import (
    TAGS_ORDER_HIGH,
    IOField,
    OperatorCategory,
    Parameter,
    ViewMetadata,
)
from dbgpt.util.i18n_utils import _

from .llm import HOContextBody

logger = logging.getLogger(__name__)


class CustomJSONEncoder(json.JSONEncoder):
    """Custom JSON encoder for datetime and Decimal types."""

    def default(self, obj):
        if isinstance(obj, (date, datetime)):
            return obj.isoformat()
        if isinstance(obj, Decimal):
            return float(obj)
        return super().default(obj)


def extract_keywords(question: str) -> Dict[str, Any]:
    """Extract keywords from user question."""
    keywords = {
        "city": "",
        "district": "",
        "hospital": "",
        "doctor": "",
        "project": "",
        "intent": [],
    }

    # City extraction
    cities = ["北京", "上海", "广州", "深圳", "杭州", "成都", "武汉", "南京", "西安", "重庆"]
    for city in cities:
        if city in question:
            keywords["city"] = city
            break

    # District extraction
    districts = ["朝阳", "海淀", "东城", "西城", "丰台", "浦东", "徐汇", "黄浦", "天河", "福田"]
    for d in districts:
        if d in question:
            keywords["district"] = d
            break

    # Hospital name extraction
    hospitals = ["维乐", "圣贝", "中诺", "美奥", "丽都", "拜尔", "欢乐", "致美", "登特", "劲松", "瑞泰", "佳美"]
    for h in hospitals:
        if h in question:
            keywords["hospital"] = h
            break

    # Project extraction
    projects = ["种植", "矫正", "正畸", "洗牙", "美白", "补牙", "拔牙", "全瓷", "隐形"]
    for p in projects:
        if p in question:
            keywords["project"] = p
            break

    # Intent detection
    if any(kw in question for kw in ["医院", "推荐", "哪家", "哪里做"]):
        keywords["intent"].append("hospital_recommend")
    if any(kw in question for kw in ["医生", "专家", "大夫"]):
        keywords["intent"].append("doctor_recommend")
    if any(kw in question for kw in ["坐诊", "排班", "出诊", "上班"]):
        keywords["intent"].append("schedule")
    if any(kw in question for kw in ["预约", "挂号", "号源", "能约", "有空", "明天", "后天"]):
        keywords["intent"].append("appointment")
    if any(kw in question for kw in ["活动", "优惠", "促销", "打折", "特惠"]):
        keywords["intent"].append("promotion")
    if any(kw in question for kw in ["价格", "多少钱", "费用", "收费"]):
        keywords["intent"].append("price")
    if any(kw in question for kw in ["离职", "去哪", "调动", "跳槽", "变动"]):
        keywords["intent"].append("career")
    if any(kw in question for kw in ["优势", "特色", "怎么样", "好不好"]):
        keywords["intent"].append("hospital_info")

    return keywords


class DentalSmartQueryOperator(MapOperator[str, HOContextBody]):
    """Smart query operator for dental customer service.

    This operator extracts intent from user questions and executes
    predefined SQL queries to retrieve relevant data from the database.
    """

    metadata = ViewMetadata(
        label=_("口腔客服智能查询"),
        name="dental_smart_query_operator",
        description=_("根据用户问题智能查询口腔医院数据库，支持医院推荐、医生排班、预约、促销等查询"),
        category=OperatorCategory.DATABASE,
        parameters=[
            Parameter.build_from(
                _("数据库主机"),
                "db_host",
                type=str,
                optional=True,
                default="localhost",
                description=_("MySQL数据库主机地址"),
            ),
            Parameter.build_from(
                _("数据库端口"),
                "db_port",
                type=int,
                optional=True,
                default=3306,
                description=_("MySQL数据库端口"),
            ),
            Parameter.build_from(
                _("数据库用户名"),
                "db_user",
                type=str,
                optional=True,
                default="root",
                description=_("MySQL数据库用户名"),
            ),
            Parameter.build_from(
                _("数据库密码"),
                "db_password",
                type=str,
                optional=True,
                default="",
                description=_("MySQL数据库密码"),
            ),
            Parameter.build_from(
                _("数据库名"),
                "db_name",
                type=str,
                optional=True,
                default="hospital_db",
                description=_("MySQL数据库名称"),
            ),
            Parameter.build_from(
                _("上下文键"),
                "context_key",
                type=str,
                optional=True,
                default="dental_data",
                description=_("输出上下文的键名"),
            ),
        ],
        inputs=[
            IOField.build_from(
                _("用户问题"),
                "query",
                str,
                description=_("用户输入的问题"),
            )
        ],
        outputs=[
            IOField.build_from(
                _("查询结果上下文"),
                "context",
                HOContextBody,
                description=_("包含查询结果的上下文"),
            )
        ],
        tags={"order": TAGS_ORDER_HIGH},
    )

    def __init__(
        self,
        db_host: str = "localhost",
        db_port: int = 3306,
        db_user: str = "root",
        db_password: str = "",
        db_name: str = "hospital_db",
        context_key: str = "dental_data",
        **kwargs,
    ):
        """Initialize the operator."""
        super().__init__(**kwargs)
        self._db_config = {
            "host": db_host,
            "port": db_port,
            "user": db_user,
            "password": db_password,
            "database": db_name,
            "charset": "utf8mb4",
        }
        self._context_key = context_key

    def _query_db(self, sql: str, params: tuple = None) -> List[Dict]:
        """Execute SQL query and return results."""
        conn = pymysql.connect(**self._db_config)
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        try:
            cursor.execute(sql, params)
            return list(cursor.fetchall())
        finally:
            cursor.close()
            conn.close()

    def _smart_query(self, question: str) -> Dict[str, Any]:
        """Execute smart queries based on extracted keywords."""
        keywords = extract_keywords(question)
        results = {}

        city_filter = f"AND h.city_name LIKE '%{keywords['city']}%'" if keywords["city"] else ""
        district_filter = f"AND h.district_name LIKE '%{keywords['district']}%'" if keywords["district"] else ""
        hospital_filter = f"AND h.hospital_name LIKE '%{keywords['hospital']}%'" if keywords["hospital"] else ""

        # Hospital recommendation
        if "hospital_recommend" in keywords["intent"] or not keywords["intent"]:
            sql = f"""
                SELECT h.hospital_name, h.district_name, h.detailed_address,
                       h.main_phone, h.features, h.business_hours
                FROM robot_kb_hospitals h
                WHERE h.status = 1 AND h.hospital_category = '口腔'
                  {city_filter} {district_filter}
                ORDER BY h.completeness_score DESC LIMIT 5
            """
            results["hospitals"] = self._query_db(sql)

        # Doctor recommendation + Schedule
        if "doctor_recommend" in keywords["intent"] or "schedule" in keywords["intent"]:
            sql = f"""
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
                WHERE d.status = 1 {hospital_filter} {city_filter}
                GROUP BY d.id, d.doctor_name, d.position, d.specialties, h.hospital_name
                LIMIT 10
            """
            results["doctors"] = self._query_db(sql)

        # Appointment availability
        if "appointment" in keywords["intent"]:
            date_filter = ""
            if "明天" in question:
                date_filter = "AND a.slot_date = DATE_ADD(CURDATE(), INTERVAL 1 DAY)"
            elif "后天" in question:
                date_filter = "AND a.slot_date = DATE_ADD(CURDATE(), INTERVAL 2 DAY)"
            else:
                date_filter = "AND a.slot_date >= CURDATE() AND a.slot_date <= DATE_ADD(CURDATE(), INTERVAL 7 DAY)"

            sql = f"""
                SELECT d.doctor_name, h.hospital_name,
                       a.slot_date, a.time_slot, a.available_slots,
                       CASE WHEN a.available_slots > 5 THEN '充足'
                            WHEN a.available_slots > 0 THEN '紧张' ELSE '已满' END as status
                FROM appointment_slots a
                JOIN robot_kb_doctors d ON a.doctor_id = d.id
                JOIN robot_kb_hospitals h ON a.hospital_id = h.id
                WHERE a.is_open = 1 AND a.available_slots > 0
                  {date_filter}
                  {hospital_filter} {city_filter}
                ORDER BY a.slot_date, a.available_slots DESC LIMIT 10
            """
            results["appointments"] = self._query_db(sql)

        # Promotions
        if "promotion" in keywords["intent"]:
            sql = f"""
                SELECT h.hospital_name, p.title, p.promotion_type,
                       p.original_price, p.promotion_price, p.discount_rate, p.end_date
                FROM hospital_promotions p
                JOIN robot_kb_hospitals h ON p.hospital_id = h.id
                WHERE p.status = 1 AND p.end_date >= CURDATE()
                  {hospital_filter} {city_filter}
                ORDER BY p.discount_rate ASC LIMIT 10
            """
            results["promotions"] = self._query_db(sql)

        # Price inquiry
        if "price" in keywords["intent"]:
            project_filter = f"%{keywords['project']}%" if keywords["project"] else "%种植%"
            sql = f"""
                SELECT h.hospital_name, h.city_name,
                       p.project_name, p.price_min, p.price_max, p.price_unit
                FROM robot_kb_prices p
                JOIN robot_kb_hospitals h ON p.hospital_id = h.id
                WHERE p.status = 1 AND p.project_name LIKE %s {city_filter}
                ORDER BY p.price_min LIMIT 10
            """
            results["prices"] = self._query_db(sql, (project_filter,))

        # Doctor career history
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
            results["career"] = self._query_db(sql)

        # Hospital details
        if "hospital_info" in keywords["intent"] and keywords["hospital"]:
            sql = f"""
                SELECT hospital_name, features, description, project_advantages,
                       dental_chairs, building_area, business_hours
                FROM robot_kb_hospitals
                WHERE hospital_name LIKE '%{keywords['hospital']}%' {city_filter}
                LIMIT 1
            """
            results["hospital_detail"] = self._query_db(sql)

        return results

    async def map(self, question: str) -> HOContextBody:
        """Execute smart query and return context."""
        logger.info(f"Dental query received: {question}")

        # Run query
        results = await self.blocking_func_to_async(self._smart_query, question)
        logger.info(f"Query results: {list(results.keys())}")

        # Build context for LLM
        context_parts = []
        for key, data in results.items():
            if data:
                context_parts.append(
                    f"## {key}:\n{json.dumps(data[:5], ensure_ascii=False, indent=2, cls=CustomJSONEncoder)}"
                )

        context = "\n\n".join(context_parts) if context_parts else "没有找到相关数据"

        prompt = f"""你是一位专业、热情的口腔医疗客服AI助手。请基于以下数据库查询结果回答顾客问题。

## 数据库查询结果:
{context}

## 回答要求:
1. 用亲切友好的口吻回答，称呼顾客为"您"
2. 基于数据回答，不要编造信息
3. 如果是推荐，说明推荐理由
4. 如果数据不足，礼貌地说明并提供建议
5. 适当引导顾客预约或咨询更多

## 顾客问题:
{question}

请直接用自然语言回答:"""

        return HOContextBody(
            context_key=self._context_key,
            context=prompt,
        )
