#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
口腔智能客服 AWEL 工作流
支持：医院推荐、医生排班、预约查询、活动促销、话术检索
"""

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional

from dbgpt.core import (
    ChatPromptTemplate,
    HumanPromptTemplate,
    SystemPromptTemplate,
)
from dbgpt.core.awel import (
    DAG,
    HttpTrigger,
    JoinOperator,
    MapOperator,
)
from dbgpt.core.awel.trigger.http_trigger import CommonLLMHttpRequestBody
from dbgpt.core.operators import PromptBuilderOperator, RequestBuilderOperator
from dbgpt.datasource.operators import DatasourceOperator
from dbgpt.model.operators import LLMOperator, StreamingLLMOperator
from dbgpt.model.proxy import OpenAILLMClient
from dbgpt_ext.datasource.rdbms.conn_mysql import MySQLConnector

logger = logging.getLogger(__name__)

# ============================================================
# 数据库连接配置
# ============================================================
DB_CONFIG = {
    "host": "localhost",
    "port": 3306,
    "user": "root",
    "password": "ServBay.dev",
    "database": "hospital_db",
}

# ============================================================
# LLM 配置 - 使用远程 vLLM 服务
# ============================================================
LLM_CONFIG = {
    "api_base": "http://182.114.59.224:60329/v1",
    "api_key": "dummy",
    "model": "qwen3-omni-30b",
}

# ============================================================
# 数据库表结构说明
# ============================================================
TABLE_SCHEMA = """
## 核心表结构:

### 1. robot_kb_hospitals (医院信息表)
- id: 医院ID
- hospital_name: 医院名称
- hospital_short_name: 简称
- hospital_category: 类别(口腔/整形美容/眼科等)
- city_name: 城市
- district_name: 区县
- detailed_address: 详细地址
- main_phone: 主电话
- business_hours: 营业时间
- features: 特色项目
- description: 医院介绍
- project_advantages: 项目优势

### 2. robot_kb_doctors (医生信息表)
- id: 医生ID
- doctor_name: 医生姓名
- hospital_id: 所属医院ID
- title: 职称
- position: 职位
- specialties: 擅长领域(JSON数组)
- introduction: 个人简介

### 3. doctor_schedules (医生排班表)
- doctor_id: 医生ID
- hospital_id: 医院ID
- day_of_week: 星期几(1-7, 1=周一)
- time_slot: 时段(上午/下午/全天)
- start_time: 开始时间
- end_time: 结束时间
- room_number: 诊室号
- max_appointments: 最大预约数
- is_expert_clinic: 是否专家门诊
- expert_fee: 专家挂号费

### 4. doctor_career_history (医生履历变动表)
- doctor_id: 医生ID
- doctor_name: 医生姓名
- event_type: 事件类型(join入职/leave离职/transfer调动/promote晋升)
- from_hospital_name: 原医院
- to_hospital_name: 新医院
- from_position: 原职位
- to_position: 新职位
- event_date: 变动日期
- reason: 变动原因

### 5. hospital_promotions (医院活动促销表)
- hospital_id: 医院ID
- hospital_name: 医院名称
- title: 活动标题
- promotion_type: 类型(discount折扣/free_check免费检查/package套餐/festival节日/new_customer新客/group团购)
- original_price: 原价
- promotion_price: 活动价
- discount_rate: 折扣率
- applicable_projects: 适用项目
- start_date: 开始日期
- end_date: 结束日期
- status: 状态(1=进行中,0=已结束,2=未开始)

### 6. appointment_slots (预约时段表)
- doctor_id: 医生ID
- hospital_id: 医院ID
- slot_date: 预约日期
- time_slot: 时段
- total_slots: 总号源
- booked_slots: 已预约
- available_slots: 剩余号源
- is_open: 是否开放
- price: 挂号费

### 7. robot_kb_prices (价格表)
- hospital_id: 医院ID
- project_name: 项目名称
- price_min: 最低价
- price_max: 最高价
- price_unit: 价格单位
"""

# ============================================================
# 系统提示词
# ============================================================
SYSTEM_PROMPT = f"""你是一位专业、热情的口腔医疗客服AI助手。你的任务是帮助顾客解答关于医院、医生、价格、预约等问题。

## 你可以访问的数据库表结构:
{TABLE_SCHEMA}

## 回答原则:
1. **亲切友好**: 像真人客服一样自然交流，使用"您"称呼顾客
2. **专业准确**: 基于数据库信息回答，不编造数据
3. **主动推荐**: 根据顾客需求推荐合适的医院/医生，并说明推荐理由
4. **引导转化**: 适当引导顾客预约或了解更多

## 常见问题处理:
1. **医院推荐**: 查询robot_kb_hospitals，根据城市、区域、特色推荐
2. **医生推荐**: 查询robot_kb_doctors + doctor_schedules，考虑专长和排班
3. **排班查询**: 查询doctor_schedules，告知具体坐诊时间
4. **预约情况**: 查询appointment_slots，告知剩余号源
5. **活动促销**: 查询hospital_promotions WHERE status=1，推荐进行中的活动
6. **价格咨询**: 查询robot_kb_prices，给出价格区间
7. **医生去向**: 查询doctor_career_history，说明医生工作变动

## 回复格式要求:
请直接用自然语言回答顾客问题。如果需要查询数据库，先在内部生成SQL，获取结果后组织成友好的回复。

当前日期: {{current_date}}
"""

RESPONSE_FORMAT = {
    "thoughts": "分析用户意图",
    "sql": "需要执行的SQL(如果需要)",
    "answer": "给用户的回复"
}


# ============================================================
# 自定义算子
# ============================================================
class SQLExecutorOperator(MapOperator[Dict, Dict]):
    """执行SQL并返回结果"""

    def __init__(self, db_config: Dict, **kwargs):
        super().__init__(**kwargs)
        self._db_config = db_config
        self._connector = None

    def _get_connector(self):
        if self._connector is None:
            self._connector = MySQLConnector.from_uri_db(
                host=self._db_config["host"],
                port=self._db_config["port"],
                user=self._db_config["user"],
                pwd=self._db_config["password"],
                db_name=self._db_config["database"],
            )
        return self._connector

    async def map(self, input_dict: Dict) -> Dict:
        sql = input_dict.get("sql", "")
        if not sql or sql.strip().upper() == "NONE":
            return {**input_dict, "sql_result": None}

        try:
            connector = self._get_connector()
            result = await self.blocking_func_to_async(
                connector.run_to_df, sql
            )
            # 将DataFrame转换为字典列表
            if result is not None and not result.empty:
                sql_result = result.to_dict(orient="records")
            else:
                sql_result = []
            return {**input_dict, "sql_result": sql_result}
        except Exception as e:
            logger.error(f"SQL执行失败: {e}")
            return {**input_dict, "sql_result": None, "sql_error": str(e)}


class ResponseFormatterOperator(MapOperator[Dict, str]):
    """格式化最终回复"""

    async def map(self, input_dict: Dict) -> str:
        answer = input_dict.get("answer", "")
        sql_result = input_dict.get("sql_result")

        if sql_result:
            # 将SQL结果整合到回答中
            result_text = json.dumps(sql_result, ensure_ascii=False, indent=2)
            return f"{answer}\n\n[数据参考]\n```json\n{result_text}\n```"
        return answer


# ============================================================
# 创建工作流
# ============================================================
def create_dental_service_dag():
    """创建口腔客服工作流DAG"""

    # 创建LLM客户端
    llm_client = OpenAILLMClient(
        api_base=LLM_CONFIG["api_base"],
        api_key=LLM_CONFIG["api_key"],
    )

    # 创建提示词模板
    prompt = ChatPromptTemplate(
        messages=[
            SystemPromptTemplate.from_template(SYSTEM_PROMPT),
            HumanPromptTemplate.from_template("{user_input}"),
        ]
    )

    with DAG("dental_service_dag") as dag:
        # HTTP触发器
        trigger = HttpTrigger(
            endpoint="/api/v1/awel/trigger/dental/chat",
            methods="POST",
            request_body=CommonLLMHttpRequestBody,
        )

        # 解析请求
        parse_request = MapOperator(
            lambda req: {
                "user_input": req.messages if isinstance(req.messages, str) else req.messages[-1].get("content", ""),
                "current_date": "2026-01-19",
            }
        )

        # 构建提示词
        prompt_builder = PromptBuilderOperator(prompt)

        # 构建LLM请求
        request_builder = RequestBuilderOperator(model=LLM_CONFIG["model"])

        # LLM调用
        llm = LLMOperator(llm_client=llm_client)

        # 解析LLM输出
        parse_output = MapOperator(
            lambda output: json.loads(output.text) if output.text.startswith("{") else {"answer": output.text}
        )

        # SQL执行
        sql_executor = SQLExecutorOperator(db_config=DB_CONFIG)

        # 格式化回复
        formatter = ResponseFormatterOperator()

        # 连接工作流
        (
            trigger
            >> parse_request
            >> prompt_builder
            >> request_builder
            >> llm
            >> parse_output
            >> sql_executor
            >> formatter
        )

    return dag


# ============================================================
# 简化版：直接查询+回复
# ============================================================
async def dental_chat(question: str) -> str:
    """
    简化版客服对话函数
    直接接收问题，返回回答
    """
    from datetime import date

    # 创建数据库连接
    connector = MySQLConnector.from_uri_db(
        host=DB_CONFIG["host"],
        port=DB_CONFIG["port"],
        user=DB_CONFIG["user"],
        pwd=DB_CONFIG["password"],
        db_name=DB_CONFIG["database"],
    )

    # 创建LLM客户端
    llm_client = OpenAILLMClient(
        api_base=LLM_CONFIG["api_base"],
        api_key=LLM_CONFIG["api_key"],
    )

    # 构建提示词
    full_prompt = f"""{SYSTEM_PROMPT.replace('{current_date}', str(date.today()))}

用户问题: {question}

请按以下JSON格式回复:
{{
    "thoughts": "你的分析思路",
    "sql": "需要执行的SQL查询(如果不需要查询数据库，填null)",
    "answer": "给用户的友好回复"
}}
"""

    # 调用LLM
    from dbgpt.core.interface.llm import ModelRequest, ModelRequestContext

    request = ModelRequest(
        model=LLM_CONFIG["model"],
        messages=[{"role": "user", "content": full_prompt}],
    )

    response = await llm_client.generate(request)
    response_text = response.text

    try:
        # 解析JSON响应
        result = json.loads(response_text)
        sql = result.get("sql")
        answer = result.get("answer", response_text)

        # 如果有SQL，执行查询
        if sql and sql.strip().lower() != "null":
            try:
                df = connector.run_to_df(sql)
                if df is not None and not df.empty:
                    # 将查询结果附加到回答
                    data = df.head(10).to_dict(orient="records")
                    answer += f"\n\n📊 查询结果:\n{json.dumps(data, ensure_ascii=False, indent=2)}"
            except Exception as e:
                logger.warning(f"SQL执行失败: {e}")

        return answer
    except json.JSONDecodeError:
        return response_text


# ============================================================
# 测试
# ============================================================
if __name__ == "__main__":
    # 测试问题
    test_questions = [
        "北京朝阳区做种植牙哪家医院好？",
        "北京维乐口腔推荐哪个医生？",
        "明天有哪个医生可以预约？",
        "最近有什么优惠活动？",
        "种植牙大概多少钱？",
    ]

    async def test():
        for q in test_questions[:1]:  # 先测试第一个问题
            print(f"\n{'='*50}")
            print(f"问题: {q}")
            print("="*50)
            answer = await dental_chat(q)
            print(f"回答:\n{answer}")

    asyncio.run(test())
