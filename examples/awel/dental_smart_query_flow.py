"""Dental Smart Query AWEL Flow.

This flow provides intelligent query capabilities for dental customer service,
supporting hospital recommendations, doctor schedules, appointments, promotions, etc.

Usage:
    1. Start DB-GPT with this flow registered
    2. Access via: POST /api/v2/serve/awel/trigger/dental/smart/chat
"""

import json
import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, List

import pymysql
from dbgpt.core.awel import DAG, HttpTrigger, MapOperator
from dbgpt.core.operators import BaseLLMOperator

logger = logging.getLogger(__name__)

# Database configuration
DB_CONFIG = {
    "host": "localhost",
    "port": 3306,
    "user": "root",
    "password": "ServBay.dev",
    "database": "hospital_db",
    "charset": "utf8mb4",
}

# LLM configuration
LLM_MODEL = "qwen3-omni-30b"


class CustomJSONEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, (date, datetime)):
            return obj.isoformat()
        if isinstance(obj, Decimal):
            return float(obj)
        return super().default(obj)


def query_db(sql: str, params: tuple = None) -> List[Dict]:
    """Execute SQL query."""
    conn = pymysql.connect(**DB_CONFIG)
    cursor = conn.cursor(pymysql.cursors.DictCursor)
    try:
        cursor.execute(sql, params)
        return list(cursor.fetchall())
    finally:
        cursor.close()
        conn.close()


def extract_keywords(question: str) -> Dict[str, Any]:
    """Extract keywords from user question."""
    keywords = {
        "city": "",
        "district": "",
        "hospital": "",
        "project": "",
        "intent": [],
    }

    # City
    cities = ["北京", "上海", "广州", "深圳", "杭州", "成都"]
    for city in cities:
        if city in question:
            keywords["city"] = city
            break

    # District
    districts = ["朝阳", "海淀", "东城", "西城", "丰台", "浦东"]
    for d in districts:
        if d in question:
            keywords["district"] = d
            break

    # Hospital
    hospitals = ["维乐", "圣贝", "中诺", "美奥", "丽都", "劲松", "瑞泰"]
    for h in hospitals:
        if h in question:
            keywords["hospital"] = h
            break

    # Project
    projects = ["种植", "矫正", "正畸", "洗牙", "美白", "补牙"]
    for p in projects:
        if p in question:
            keywords["project"] = p
            break

    # Intent detection
    if any(kw in question for kw in ["医院", "推荐", "哪家"]):
        keywords["intent"].append("hospital_recommend")
    if any(kw in question for kw in ["医生", "专家", "大夫"]):
        keywords["intent"].append("doctor_recommend")
    if any(kw in question for kw in ["坐诊", "排班", "出诊"]):
        keywords["intent"].append("schedule")
    if any(kw in question for kw in ["预约", "挂号", "有空", "明天", "后天"]):
        keywords["intent"].append("appointment")
    if any(kw in question for kw in ["活动", "优惠", "促销"]):
        keywords["intent"].append("promotion")
    if any(kw in question for kw in ["价格", "多少钱", "费用"]):
        keywords["intent"].append("price")

    return keywords


def smart_query(question: str) -> Dict[str, Any]:
    """Execute smart queries based on intent."""
    keywords = extract_keywords(question)
    results = {}

    city_filter = f"AND h.city_name LIKE '%{keywords['city']}%'" if keywords["city"] else ""
    hospital_filter = f"AND h.hospital_name LIKE '%{keywords['hospital']}%'" if keywords["hospital"] else ""

    # Hospital recommendation
    if "hospital_recommend" in keywords["intent"] or not keywords["intent"]:
        sql = f"""
            SELECT h.hospital_name, h.district_name, h.detailed_address,
                   h.main_phone, h.features
            FROM robot_kb_hospitals h
            WHERE h.status = 1 AND h.hospital_category = '口腔'
              {city_filter}
            ORDER BY h.completeness_score DESC LIMIT 5
        """
        results["hospitals"] = query_db(sql)

    # Doctor + Schedule
    if "doctor_recommend" in keywords["intent"] or "schedule" in keywords["intent"]:
        sql = f"""
            SELECT d.doctor_name, d.position, d.specialties, h.hospital_name,
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
        results["doctors"] = query_db(sql)

    # Appointments
    if "appointment" in keywords["intent"]:
        date_filter = ""
        if "明天" in question:
            date_filter = "AND a.slot_date = DATE_ADD(CURDATE(), INTERVAL 1 DAY)"
        elif "后天" in question:
            date_filter = "AND a.slot_date = DATE_ADD(CURDATE(), INTERVAL 2 DAY)"
        else:
            date_filter = "AND a.slot_date >= CURDATE() AND a.slot_date <= DATE_ADD(CURDATE(), INTERVAL 7 DAY)"

        sql = f"""
            SELECT d.doctor_name, h.hospital_name, a.slot_date, a.time_slot, a.available_slots,
                   CASE WHEN a.available_slots > 5 THEN '充足'
                        WHEN a.available_slots > 0 THEN '紧张' ELSE '已满' END as status
            FROM appointment_slots a
            JOIN robot_kb_doctors d ON a.doctor_id = d.id
            JOIN robot_kb_hospitals h ON a.hospital_id = h.id
            WHERE a.is_open = 1 AND a.available_slots > 0
              {date_filter} {hospital_filter} {city_filter}
            ORDER BY a.slot_date, a.available_slots DESC LIMIT 10
        """
        results["appointments"] = query_db(sql)

    # Promotions
    if "promotion" in keywords["intent"]:
        sql = f"""
            SELECT h.hospital_name, p.title, p.original_price, p.promotion_price, p.end_date
            FROM hospital_promotions p
            JOIN robot_kb_hospitals h ON p.hospital_id = h.id
            WHERE p.status = 1 AND p.end_date >= CURDATE()
              {hospital_filter} {city_filter}
            ORDER BY p.discount_rate ASC LIMIT 10
        """
        results["promotions"] = query_db(sql)

    # Prices
    if "price" in keywords["intent"]:
        project_filter = f"%{keywords['project']}%" if keywords["project"] else "%种植%"
        sql = f"""
            SELECT h.hospital_name, p.project_name, p.price_min, p.price_max
            FROM robot_kb_prices p
            JOIN robot_kb_hospitals h ON p.hospital_id = h.id
            WHERE p.status = 1 AND p.project_name LIKE %s {city_filter}
            ORDER BY p.price_min LIMIT 10
        """
        results["prices"] = query_db(sql, (project_filter,))

    return results


class DentalQueryOperator(MapOperator[Dict, str]):
    """Operator that queries dental database and generates context."""

    async def map(self, request_body: Dict) -> str:
        """Process request and return query results context."""
        # Extract question from request
        messages = request_body.get("messages", [])
        if messages and isinstance(messages, list):
            question = messages[-1].get("content", "") if messages else ""
        else:
            question = str(messages)

        logger.info(f"Dental query: {question}")

        # Execute smart query
        results = await self.blocking_func_to_async(smart_query, question)
        logger.info(f"Query results: {list(results.keys())}")

        # Build context
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

        return prompt


class LLMResponseOperator(MapOperator[str, str]):
    """Operator that calls LLM with the context."""

    def __init__(self, model_name: str = LLM_MODEL, **kwargs):
        super().__init__(**kwargs)
        self._model_name = model_name

    async def map(self, prompt: str) -> str:
        """Call LLM and return response."""
        import requests

        try:
            response = requests.post(
                "http://182.114.59.224:60329/v1/chat/completions",
                headers={"Authorization": "Bearer dummy"},
                json={
                    "model": self._model_name,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.7,
                    "max_tokens": 2000,
                },
                timeout=60,
            )
            return response.json()["choices"][0]["message"]["content"]
        except Exception as e:
            logger.error(f"LLM call failed: {e}")
            return "抱歉，系统繁忙，请稍后再试。"


# Create the DAG
with DAG("dental_smart_query_flow") as dag:
    trigger = HttpTrigger(
        "/dental/smart/chat",
        methods="POST",
        request_body=Dict,
    )
    query_op = DentalQueryOperator()
    llm_op = LLMResponseOperator()

    trigger >> query_op >> llm_op


if __name__ == "__main__":
    # For testing
    import asyncio

    async def test():
        question = "劲松口腔总院哪些医生明天有空"
        results = smart_query(question)
        print(f"Results: {json.dumps(results, ensure_ascii=False, indent=2, cls=CustomJSONEncoder)}")

    asyncio.run(test())
