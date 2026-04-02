#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
口腔 CRM 数据查询 MCP Server
供 DeerFlow Agent 通过 MCP 协议调用

启动方式:
    python dental_mcp_server.py                    # HTTP 模式 (默认, 端口 8091)
    python dental_mcp_server.py --transport stdio   # stdio 模式 (调试用)

MCP 开发调试:
    mcp dev dental_mcp_server.py
"""

import json
import logging
import os
import sys
import time
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv()

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("dental_mcp")

# ============================================================
# 导入 dental_chat_api 中的纯数据查询函数
# ============================================================

from dental_chat_api import (
    query_customer_info,
    query_customer_full_analysis,
    query_follow_records,
    query_order_records,
    query_hospital_orders,
    query_similar_conversion_rate,
    query_conversion_cycle_analysis,
    query_register_hour_conversion,
    query_district_hospital_performance,
    calculate_conversion_probability,
    analyze_follow_content,
    query_hospital_ranking,
    query_hospital_deals,
    query_region_hospital_deals,
    query_hospital_analysis,
    query_kf_stats,
    query_churn_warning,
    query_customer_lifecycle,
    query_time_analysis,
    query_department_stats,
    query_company_stats,
    query_success_cases,
    query_failure_cases,
    query_qudao_db,
    get_linkage_name,
    query_db,
    DEPARTMENT_MAP,
    _get_department_channel_ids,
)

from dental_kb_queries import (
    query_hospital_recommend,
    query_doctor_recommend,
    query_schedule,
    query_appointment,
    query_promotion,
    query_price,
    query_career,
    query_hospital_info,
)

# 来源分析模块（可选）
try:
    import source_analysis as sa_module
    SOURCE_ANALYSIS_AVAILABLE = True
except ImportError:
    SOURCE_ANALYSIS_AVAILABLE = False
    logger.warning("source_analysis 模块不可用，来源分析工具将被禁用")


# ============================================================
# JSON 序列化辅助
# ============================================================

def _serialize(obj: Any) -> Any:
    """处理 MySQL 返回的特殊类型，使其可 JSON 序列化"""
    if isinstance(obj, dict):
        return {k: _serialize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_serialize(item) for item in obj]
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")
    return obj


def _mask_phone(phone: str) -> str:
    """手机号掩码: 13912345678 → 139****5678"""
    if not phone or not isinstance(phone, str):
        return phone or ""
    digits = ''.join(c for c in phone if c.isdigit())
    if len(digits) >= 11:
        return digits[:3] + "****" + digits[-4:]
    return phone


def _mask_sensitive(obj: Any, mask_phones: bool = True) -> Any:
    """递归脱敏：对含有手机号字段的 dict 做掩码处理"""
    if not mask_phones:
        return obj
    PHONE_KEYS = {"MobilePhone", "mobilephone", "phone", "Phone", "mobile", "Mobile"}
    if isinstance(obj, dict):
        return {
            k: (_mask_phone(v) if k in PHONE_KEYS and isinstance(v, str) else _mask_sensitive(v, mask_phones))
            for k, v in obj.items()
        }
    if isinstance(obj, (list, tuple)):
        return [_mask_sensitive(item, mask_phones) for item in obj]
    return obj


def _safe_call(func, *args, **kwargs) -> str:
    """安全调用查询函数并返回 JSON 字符串"""
    try:
        result = func(*args, **kwargs)
        result = _mask_sensitive(_serialize(result))
        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.exception(f"查询失败: {func.__name__}")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


# ============================================================
# MCP Server 定义
# ============================================================

MCP_HOST = os.getenv("MCP_HOST", "0.0.0.0")
MCP_PORT = int(os.getenv("MCP_PORT", "8091"))

mcp = FastMCP(
    "dental-crm",
    description="口腔CRM数据查询系统 — 客户分析、医院推荐、转化率、客服业绩、流失预警",
    host=MCP_HOST,
    port=MCP_PORT,
)


# ---- 1. 客户全维度分析 ----

@mcp.tool()
def customer_full_analysis(client_id: int) -> str:
    """客户全维度深度分析。返回客户基本信息、跟进记录、成交记录、派单记录、
    成交概率（7维评分）、同类转化率、成交周期、成功/失败案例、留电时段分析、地区医院实力。
    当用户问"分析客户XXX"、"客户XXX情况"、"XXX成交概率"时使用。
    注意：此工具查询较重（可能需要10-15秒），如果只需要基本信息请用 customer_info。

    Args:
        client_id: 客户ID（数字）
    """
    return _safe_call(query_customer_full_analysis, client_id)


# ---- 2. 客户搜索 ----

@mcp.tool()
def customer_search(keyword: str) -> str:
    """根据姓名或手机号后四位搜索客户。
    客服经常记不住客户ID，只记得名字或手机尾号，用此工具快速查找。
    当用户说"搜索客户XXX"、"找一下手机尾号1234的客户"时使用。

    Args:
        keyword: 搜索关键词（客户姓名或手机号后四位）
    """
    if not keyword or len(keyword.strip()) < 2:
        return json.dumps({"error": "搜索关键词至少需要2个字符"}, ensure_ascii=False)
    
    keyword = keyword.strip()
    
    try:
        # 构建搜索SQL，支持姓名模糊搜索和手机号后四位精确匹配
        search_sql = """
            SELECT 
                c.Client_Id,
                c.ClientName,
                c.MobilePhone,
                c.client_status,
                c.PlasticsIntention,
                c.RegisterTime,
                c.KfId,
                a.realname as kf_name,
                CASE
                    WHEN c.PlasticsIntention = 1 THEN '种植牙'
                    WHEN c.PlasticsIntention = 2 THEN '矫正'
                    WHEN c.PlasticsIntention = 3 THEN '美白'
                    WHEN c.PlasticsIntention = 4 THEN '洁牙'
                    WHEN c.PlasticsIntention = 5 THEN '补牙'
                    WHEN c.PlasticsIntention = 6 THEN '拔牙'
                    WHEN c.PlasticsIntention = 7 THEN '口腔检查'
                    WHEN c.PlasticsIntention = 8 THEN '牙周治疗'
                    WHEN c.PlasticsIntention = 9 THEN '根管治疗'
                    WHEN c.PlasticsIntention = 10 THEN '烤瓷牙'
                    ELSE '其他'
                END as intention_name,
                CASE
                    WHEN c.client_status = 0 THEN '待跟进'
                    WHEN c.client_status = 1 THEN '已联系'
                    WHEN c.client_status = 2 THEN '有意向'
                    WHEN c.client_status = 3 THEN '已预约'
                    WHEN c.client_status = 4 THEN '已到院'
                    WHEN c.client_status = 5 THEN '已成交'
                    WHEN c.client_status = 6 THEN '无效'
                    WHEN c.client_status = 7 THEN '流失'
                    ELSE '未知'
                END as status_name
            FROM un_channel_client c
            LEFT JOIN un_admin a ON c.KfId = a.userid
            WHERE (
                c.ClientName LIKE %s 
                OR RIGHT(c.MobilePhone, 4) = %s
            )
            ORDER BY c.RegisterTime DESC
            LIMIT 50
        """
        
        # 参数：姓名模糊搜索 和 手机号后四位精确匹配
        name_pattern = f"%{keyword}%"
        phone_suffix = keyword if keyword.isdigit() and len(keyword) == 4 else ""
        
        customers = query_qudao_db(search_sql, (name_pattern, phone_suffix))
        
        if not customers:
            return json.dumps({
                "message": f"未找到匹配'{keyword}'的客户",
                "keyword": keyword,
                "customers": []
            }, ensure_ascii=False)
        
        # 处理结果，进行手机号脱敏
        result_customers = []
        for customer in customers:
            customer_info = {
                "client_id": customer.get('Client_Id'),
                "name": customer.get('ClientName', ''),
                "phone": _mask_phone(customer.get('MobilePhone', '')),
                "status": customer.get('status_name', ''),
                "intention_project": customer.get('intention_name', ''),
                "kf_name": customer.get('kf_name', ''),
                "register_time": datetime.fromtimestamp(customer.get('RegisterTime', 0)).strftime('%Y-%m-%d %H:%M') if customer.get('RegisterTime') else ''
            }
            result_customers.append(customer_info)
        
        result = {
            "message": f"找到 {len(result_customers)} 个匹配的客户",
            "keyword": keyword,
            "total_found": len(result_customers),
            "customers": result_customers
        }
        
        return json.dumps(_serialize(result), ensure_ascii=False, indent=2)
        
    except Exception as e:
        logger.exception("customer_search failed")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


# ---- 3. 客户基本信息 ----

@mcp.tool()
def customer_info(client_id: int) -> str:
    """查询客户基本信息（轻量级），包括姓名、性别、年龄、意向项目、状态、注册时间、所属客服等。
    当只需要快速查看客户是谁时使用。

    Args:
        client_id: 客户ID（数字）
    """
    return _safe_call(query_customer_info, client_id)


# ---- 4. 客户跟进记录 ----

@mcp.tool()
def customer_follow_records(client_id: int, limit: int = 20) -> str:
    """查询客户的跟进记录列表（时间、内容、客服姓名）。
    当用户问"客户XXX跟进记录"、"最近跟进了什么"时使用。

    Args:
        client_id: 客户ID
        limit: 返回条数，默认20
    """
    return _safe_call(query_follow_records, client_id, limit)


# ---- 5. 客户成交和派单记录 ----

@mcp.tool()
def customer_orders(client_id: int) -> str:
    """查询客户的成交记录（金额、项目、医院）和派单记录（派单医院、手术状态）。
    当用户问"客户XXX成交了吗"、"派了哪家医院"时使用。

    Args:
        client_id: 客户ID
    """
    orders = _serialize(query_order_records(client_id))
    hospital_orders = _serialize(query_hospital_orders(client_id))
    result = {
        "deal_records": orders,
        "hospital_dispatch_records": hospital_orders,
    }
    return json.dumps(result, ensure_ascii=False, indent=2)


# ---- 6. 转化分析（独立于 full_analysis 的轻量版） ----

@mcp.tool()
def conversion_analysis(client_id: int) -> str:
    """针对特定客户的转化率分析，包括同类客户转化率、成交周期、成功/失败案例。
    比 customer_full_analysis 更聚焦于转化维度。
    当用户问"这个客户转化率多少"、"类似客户成交情况"时使用。

    Args:
        client_id: 客户ID
    """
    try:
        customer = query_customer_info(client_id)
        if not customer:
            return json.dumps({"error": f"客户 {client_id} 不存在"}, ensure_ascii=False)

        intention = int(customer.get("PlasticsIntention", 0) or 0)
        region = int(customer.get("zx_District", 0) or 0)
        from_type = int(customer.get("from_type", 0) or 0)
        kf_id = int(customer.get("KfId", 0) or 0)
        client_region = int(customer.get("client_region", 0) or 0)
        register_time = int(customer.get("RegisterTime", 0) or 0)

        similar = query_similar_conversion_rate(intention, region, client_id, from_type, kf_id, client_region)
        cycle = query_conversion_cycle_analysis(intention, region, client_id, client_region, register_time)
        success = query_success_cases(intention, region, client_id, limit=3)
        failure = query_failure_cases(intention, region, client_id, limit=3)

        result = {
            "customer_id": client_id,
            "similar_conversion_rate": similar,
            "conversion_cycle": cycle,
            "success_cases": success,
            "failure_cases": failure,
        }
        return json.dumps(_serialize(result), ensure_ascii=False, indent=2)
    except Exception as e:
        logger.exception("conversion_analysis failed")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


# ---- 7. 医院排名 ----

@mcp.tool()
def hospital_ranking(area: str, limit: int = 10) -> str:
    """查询指定地区的医院排名（按派单量排序）。
    当用户问"深圳医院排名"、"哪家医院派单多"时使用。

    Args:
        area: 城市或地区名，如"深圳"、"广州天河"
        limit: 返回数量，默认10
    """
    return _safe_call(query_hospital_ranking, area, limit)


# ---- 8. 医院成交数据 ----

@mcp.tool()
def hospital_deals(hospital_name: str = "", city: str = "", days: int = 30, limit: int = 20) -> str:
    """查询医院成交数据（成交量、金额、项目分布、客单价）。
    指定医院名查单个医院，指定城市查该城市医院成交排名。
    当用户问"XX医院成交多少"、"深圳哪家医院成交最多"时使用。

    Args:
        hospital_name: 医院名（可选，模糊匹配）
        city: 城市名（可选，查地区排名时使用）
        days: 统计天数，默认30
        limit: 返回数量，默认20
    """
    if hospital_name:
        return _safe_call(query_hospital_deals, hospital_name, city, days, limit)
    elif city:
        return _safe_call(query_region_hospital_deals, city, days, limit)
    else:
        return json.dumps({"error": "请提供医院名或城市名"}, ensure_ascii=False)


# ---- 9. 医院分析（转化漏斗 + 地区实力） ----

@mcp.tool()
def hospital_analysis(hospital_name: str = "", zx_district: int = 0,
                      client_region: int = 0, days: int = 30, limit: int = 10) -> str:
    """医院合作分析。
    指定医院名：查转化漏斗（派单→到院→成交转化率、返款情况）。
    指定意向地区(zx_district)：查该地区所有医院的综合实力和成交能力。
    当用户问"XX医院转化率"、"医院合作分析"、"XX地区医院实力"时使用。

    Args:
        hospital_name: 医院名（可选，模糊匹配）
        zx_district: 意向地区ID（可选，查地区医院实力时使用）
        client_region: 业务线（1=医美 2=口腔 4=韩国 8=眼科），配合 zx_district 使用
        days: 统计天数，默认30
        limit: 返回数量，默认10
    """
    results = {}
    if hospital_name or not zx_district:
        results["hospital_performance"] = _serialize(
            query_hospital_analysis(hospital_name, days, limit)
        )
    if zx_district:
        results["district_hospitals"] = _serialize(
            query_district_hospital_performance(zx_district, client_region, days)
        )
    return json.dumps(results, ensure_ascii=False, indent=2)


# ---- 10. 客服业绩 ----

@mcp.tool()
def sales_rep_stats(kf_name: str, days: int = 7) -> str:
    """查询客服/销售代表的业绩数据（新客数、联系率、跟进数、成交数、转化率、同类平均对比）。
    当用户问"小王业绩"、"我的数据"、"XX今天/本周表现"时使用。

    Args:
        kf_name: 客服姓名
        days: 统计天数，默认7天
    """
    return _safe_call(query_kf_stats, kf_name, days)


# ---- 11. 流失预警 ----

@mcp.tool()
def churn_warning(kf_id: int = 0, risk_level: str = "all",
                  limit: int = 20, days: int = 30) -> str:
    """查询有流失风险的客户列表（按未跟进天数分级：critical/high/medium/low）。
    当用户问"哪些客户要流失"、"需要关注的客户"、"高风险客户"时使用。

    Args:
        kf_id: 客服ID（0=全部客服）
        risk_level: 风险等级筛选（critical/high/medium/low/all），默认all
        limit: 返回数量，默认20
        days: 统计范围天数，默认30
    """
    return _safe_call(query_churn_warning, kf_id, risk_level, limit, days)


# ---- 12. 运营概览 ----

@mcp.tool()
def operations_overview(scope: str, days: int = 30,
                        department_name: str = "",
                        start_ts: int = 0, end_ts: int = 0,
                        time_label: str = "", mode: str = "checktime") -> str:
    """运营数据概览。根据 scope 参数查询不同维度。
    当用户问"漏斗数据"、"公司/部门业绩"、"注册趋势"等运营问题时使用。

    Args:
        scope: 查询范围，可选值:
            - "lifecycle": 客户生命周期漏斗（新客→派单→到院→成交的转化率）
            - "time_trends": 时间分布分析（按小时/天/周的注册和成交趋势）
            - "department": 部门业绩（需提供 department_name，如 TEG/BCG/CDG）
            - "company": 公司整体业绩
        days: 统计天数，默认30
        department_name: 部门代码（scope=department 时必填），如 TEG/BCG/CDG/OMG/BSG/MMG/CMG/AMG
        start_ts: 开始时间戳（scope=department/company 时可选）
        end_ts: 结束时间戳（scope=department/company 时可选）
        time_label: 时间标签（如"本月"/"上月"）
        mode: 统计模式，checktime=审核时间 / addtime=交易时间
    """
    if scope == "lifecycle":
        return _safe_call(query_customer_lifecycle, days)
    elif scope == "time_trends":
        return _safe_call(query_time_analysis, days)
    elif scope == "department":
        if not department_name:
            return json.dumps({"error": "请提供部门代码，如 TEG/BCG/CDG"}, ensure_ascii=False)
        return _safe_call(query_department_stats, department_name,
                          start_ts or None, end_ts or None, time_label, mode)
    elif scope == "company":
        return _safe_call(query_company_stats,
                          start_ts or None, end_ts or None, time_label, mode)
    else:
        return json.dumps({
            "error": f"未知 scope: {scope}",
            "valid_scopes": ["lifecycle", "time_trends", "department", "company"],
        }, ensure_ascii=False)


# ---- 13. 知识库查询 ----

@mcp.tool()
def knowledge_base_query(query_type: str, city: str = "", hospital: str = "",
                         doctor: str = "", project: str = "",
                         date_hint: str = "") -> str:
    """知识库查询（医院推荐、医生推荐、排班、预约、活动、价格、医生变动、医院详情）。
    当用户咨询医院/医生/排班/价格/活动等知识性问题时使用。

    Args:
        query_type: 查询类型，可选值:
            - "hospital_recommend": 推荐医院
            - "doctor_recommend": 推荐医生
            - "schedule": 排班/坐诊查询
            - "appointment": 预约号源
            - "promotion": 活动/优惠
            - "price": 价格/费用
            - "career": 医生变动（离职/调动）
            - "hospital_info": 医院详情
        city: 城市名（可选）
        hospital: 医院名（可选，模糊匹配）
        doctor: 医生名（可选）
        project: 项目名（可选，如"种植"/"矫正"）
        date_hint: 日期提示（预约时用，如"明天"/"后天"）
    """
    dispatch = {
        "hospital_recommend": lambda: query_hospital_recommend(city=city, district="", project=project),
        "doctor_recommend": lambda: query_doctor_recommend(hospital=hospital, city=city, doctor=doctor),
        "schedule": lambda: query_schedule(hospital=hospital, city=city, doctor=doctor),
        "appointment": lambda: query_appointment(hospital=hospital, city=city, date_hint=date_hint),
        "promotion": lambda: query_promotion(hospital=hospital, city=city),
        "price": lambda: query_price(hospital=hospital, city=city, project=project),
        "career": lambda: query_career(),
        "hospital_info": lambda: query_hospital_info(hospital=hospital, city=city),
    }

    handler = dispatch.get(query_type)
    if not handler:
        return json.dumps({
            "error": f"未知 query_type: {query_type}",
            "valid_types": list(dispatch.keys()),
        }, ensure_ascii=False)

    return _safe_call(handler)


# ---- 14. 渠道来源分析 ----

@mcp.tool()
def source_analysis(source_keyword: str, days: int = 30) -> str:
    """渠道来源效果分析。分析指定来源的注册量、转化率、地区分布、项目分布等。
    当用户问"XX渠道效果"、"贝色今天数据"、"百度最近表现"时使用。

    Args:
        source_keyword: 来源/渠道关键词，如"贝色"/"百度"/"牙舒丽"
        days: 统计天数，默认30
    """
    if not SOURCE_ANALYSIS_AVAILABLE:
        return json.dumps({"error": "来源分析模块不可用"}, ensure_ascii=False)

    try:
        source_ids = sa_module.get_source_ids(source_keyword)
        if not source_ids:
            return json.dumps({"error": f"未找到包含'{source_keyword}'的来源"}, ensure_ascii=False)

        comparison = sa_module.analyze_source_type_comparison(source_keyword, days)
        return json.dumps(_serialize(comparison), ensure_ascii=False, indent=2)
    except Exception as e:
        logger.exception("source_analysis failed")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


# ---- 15. 多来源对比 ----

@mcp.tool()
def source_compare(sources: str, days: int = 30) -> str:
    """多来源/渠道对比分析。对比 2-5 个来源的注册量、转化率、成交额、地区分布等。
    当用户说"对比一下XX和YY"、"XX和YY哪个好"、或在前面提到了某个来源后说"对比一下"时使用。
    **重要**：如果用户之前问过某个来源的数据，现在说"对比一下"，应把之前的来源和当前的来源一起传入。

    Args:
        sources: 逗号分隔的来源关键词，如 "贝色,牙舒丽" 或 "百度,贝色,牙舒丽"（2-5个）
        days: 统计天数，默认30
    """
    if not SOURCE_ANALYSIS_AVAILABLE:
        return json.dumps({"error": "来源分析模块不可用"}, ensure_ascii=False)

    source_list = [s.strip() for s in sources.split(",") if s.strip()]
    if len(source_list) < 2:
        return json.dumps({"error": "请提供至少2个来源进行对比，用逗号分隔"}, ensure_ascii=False)
    if len(source_list) > 5:
        return json.dumps({"error": "最多支持5个来源对比"}, ensure_ascii=False)

    try:
        comparisons = []
        source_info = sa_module.get_all_source_info()

        for keyword in source_list:
            source_ids = sa_module.get_source_ids(keyword)
            if source_ids:
                region_data = sa_module.analyze_region_distribution(source_ids, days)
                project_data = sa_module.analyze_project_distribution(source_ids, days)
                hospital_data = sa_module.analyze_hospital_distribution(source_ids, days)

                source_types = [
                    sa_module.classify_source(
                        source_info.get(sid, {}).get('name', ''),
                        source_info.get(sid, {}).get('parent_name', '')
                    )
                    for sid in source_ids
                ]
                main_type = max(set(source_types), key=source_types.count) if source_types else 'unknown'

                comparisons.append({
                    'source': keyword,
                    'source_type': {'paid': '竞价/付费', 'organic': '自然来源',
                                    'channel': '渠道合作', 'unknown': '未分类'}.get(main_type, '未知'),
                    'total_customers': region_data.get('total_customers', 0),
                    'total_converted': region_data.get('total_converted', 0),
                    'conversion_rate': region_data.get('conversion_rate', 0),
                    'top_regions': [r['province'] for r in region_data.get('regions', [])[:5]],
                    'top_projects': [p['project'] for p in project_data.get('projects', [])[:5]],
                    'total_orders': hospital_data.get('total_orders', 0),
                    'total_amount': hospital_data.get('total_amount', 0),
                })
            else:
                comparisons.append({'source': keyword, 'error': f"未找到包含'{keyword}'的来源"})

        result = {"analysis_period_days": days, "comparisons": comparisons}
        return json.dumps(_serialize(result), ensure_ascii=False, indent=2)
    except Exception as e:
        logger.exception("source_compare failed")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


# ---- 16. 批量客户概览 ----

@mcp.tool()
def batch_customer_overview(kf_id: int) -> str:
    """获取指定客服名下所有客户的精简列表，适合表格展示。
    返回每个客户的ID、姓名、状态、意向项目、最后跟进天数、风险等级。
    当用户问"我的客户列表"、"XX客服的所有客户"、"显示客户概览"时使用。

    Args:
        kf_id: 客服ID
    """
    try:
        # 查询该客服的基本信息
        kf_sql = """
            SELECT userid, username, realname
            FROM un_admin
            WHERE userid = %s
            LIMIT 1
        """
        kf_result = query_qudao_db(kf_sql, (kf_id,))
        if not kf_result:
            return json.dumps({"error": f"客服ID {kf_id} 不存在"}, ensure_ascii=False)
        
        kf_info = kf_result[0]
        kf_name = kf_info.get('realname') or kf_info.get('username')

        # 查询该客服的所有客户列表
        customers_sql = """
            SELECT 
                c.Client_Id,
                c.ClientName,
                c.Sex,
                c.Age,
                c.PlasticsIntention,
                c.client_status,
                c.Status,
                c.RegisterTime,
                c.client_region,
                CASE
                    WHEN c.PlasticsIntention = 1 THEN '种植牙'
                    WHEN c.PlasticsIntention = 2 THEN '矫正'
                    WHEN c.PlasticsIntention = 3 THEN '美白'
                    WHEN c.PlasticsIntention = 4 THEN '洁牙'
                    WHEN c.PlasticsIntention = 5 THEN '补牙'
                    WHEN c.PlasticsIntention = 6 THEN '拔牙'
                    WHEN c.PlasticsIntention = 7 THEN '口腔检查'
                    WHEN c.PlasticsIntention = 8 THEN '牙周治疗'
                    WHEN c.PlasticsIntention = 9 THEN '根管治疗'
                    WHEN c.PlasticsIntention = 10 THEN '烤瓷牙'
                    ELSE '其他'
                END as intention_name,
                -- 计算最后跟进天数
                COALESCE(
                    DATEDIFF(NOW(), FROM_UNIXTIME(
                        (SELECT MAX(CMPostTime) 
                         FROM un_channel_managermessage m 
                         WHERE m.ClientId = c.Client_Id AND m.CMPostTime > 0)
                    )),
                    DATEDIFF(NOW(), FROM_UNIXTIME(c.RegisterTime))
                ) as days_since_last_follow,
                -- 是否已成交
                CASE WHEN EXISTS(
                    SELECT 1 FROM un_channel_paylist p 
                    WHERE p.Client_Id = c.Client_Id AND p.status = 1 AND p.number > 0
                ) THEN 1 ELSE 0 END as has_deal,
                -- 是否已派单
                CASE WHEN EXISTS(
                    SELECT 1 FROM un_hospital_order ho 
                    WHERE ho.Client_Id = c.Client_Id
                ) THEN 1 ELSE 0 END as has_dispatch
            FROM un_channel_client c
            WHERE c.KfId = %s
            ORDER BY 
                -- 优先显示：未成交且已派单的客户（重点关注）
                CASE WHEN has_deal = 0 AND has_dispatch = 1 THEN 0 ELSE 1 END,
                -- 其次按最后跟进时间倒序
                days_since_last_follow DESC,
                c.RegisterTime DESC
        """
        
        customers = query_qudao_db(customers_sql, (kf_id,))
        
        # 状态映射
        status_map = {
            0: '待跟进', 1: '已联系', 2: '有意向', 3: '已预约',
            4: '已到院', 5: '已成交', 6: '无效', 7: '流失'
        }
        
        # 处理客户数据并计算风险等级
        customer_list = []
        for customer in customers:
            days_no_follow = customer.get('days_since_last_follow', 0) or 0
            has_deal = customer.get('has_deal', 0)
            has_dispatch = customer.get('has_dispatch', 0)
            client_status = customer.get('client_status', 0)
            
            # 风险等级计算
            if has_deal:
                risk_level = "已成交"
                risk_color = "green"
            elif client_status == 6:  # 无效客户
                risk_level = "无效"
                risk_color = "gray"
            elif client_status == 7:  # 流失客户
                risk_level = "已流失"
                risk_color = "black"
            elif has_dispatch and days_no_follow > 14:
                risk_level = "紧急"
                risk_color = "red"
            elif has_dispatch and days_no_follow > 7:
                risk_level = "高风险"
                risk_color = "orange"
            elif days_no_follow > 3:
                risk_level = "中风险"
                risk_color = "yellow"
            else:
                risk_level = "正常"
                risk_color = "green"
            
            customer_overview = {
                "client_id": customer.get('Client_Id'),
                "name": customer.get('ClientName', ''),
                "sex": "女" if customer.get('Sex') == 1 else "男" if customer.get('Sex') == 2 else "未知",
                "age": customer.get('Age', 0),
                "intention_project": customer.get('intention_name', '其他'),
                "status": status_map.get(client_status, '未知'),
                "days_since_last_follow": days_no_follow,
                "risk_level": risk_level,
                "risk_color": risk_color,
                "has_deal": bool(has_deal),
                "has_dispatch": bool(has_dispatch),
                "register_time": datetime.fromtimestamp(customer.get('RegisterTime', 0)).strftime('%Y-%m-%d') if customer.get('RegisterTime') else '',
            }
            customer_list.append(customer_overview)
        
        # 统计信息
        total_customers = len(customer_list)
        active_customers = len([c for c in customer_list if c['risk_level'] not in ['已成交', '无效', '已流失']])
        high_risk_customers = len([c for c in customer_list if c['risk_level'] in ['紧急', '高风险']])
        deal_customers = len([c for c in customer_list if c['has_deal']])
        
        result = {
            "kf_id": kf_id,
            "kf_name": kf_name,
            "summary": {
                "total_customers": total_customers,
                "active_customers": active_customers,
                "high_risk_customers": high_risk_customers,
                "deal_customers": deal_customers,
                "conversion_rate": round(deal_customers / total_customers * 100, 2) if total_customers > 0 else 0
            },
            "customers": customer_list
        }
        
        return json.dumps(_serialize(result), ensure_ascii=False, indent=2)
        
    except Exception as e:
        logger.exception("batch_customer_overview failed")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


# ---- 17. CRM 跟进提醒 ----

@mcp.tool()
def my_crm_reminders(kf_id: int) -> str:
    """查询今天需要跟进的客户列表（按客服ID查询 CRM 系统中需要今日跟进的客户）。
    返回客户ID、姓名、状态、下次跟进时间等信息，按跟进时间排序。
    当用户问"今天跟进什么"、"跟进提醒"、"CRM提醒"、"今天要跟谁"时使用。

    Args:
        kf_id: 客服ID
    """
    try:
        # client_status 映射
        status_map = {
            0: '暂无',
            1: '极好', 
            2: '好',
            3: '较好',
            4: '一般',
            5: '无意向',
            6: '确定赴韩',
            9: '已预约',
            10: '已到院',
            11: '已消费',
            12: '未联系上'
        }
        
        # 查询 CRM 提醒数据，JOIN 客户表获取详细信息
        sql = """
            SELECT 
                crm.Client_Id,
                crm.nexttime,
                crm.processed,
                c.ClientName,
                c.client_status,
                c.MobilePhone,
                c.PlasticsIntention,
                CASE
                    WHEN c.PlasticsIntention = 1 THEN '种植牙'
                    WHEN c.PlasticsIntention = 2 THEN '矫正'
                    WHEN c.PlasticsIntention = 3 THEN '美白'
                    WHEN c.PlasticsIntention = 4 THEN '洁牙'
                    WHEN c.PlasticsIntention = 5 THEN '补牙'
                    WHEN c.PlasticsIntention = 6 THEN '拔牙'
                    WHEN c.PlasticsIntention = 7 THEN '口腔检查'
                    WHEN c.PlasticsIntention = 8 THEN '牙周治疗'
                    WHEN c.PlasticsIntention = 9 THEN '根管治疗'
                    WHEN c.PlasticsIntention = 10 THEN '烤瓷牙'
                    ELSE '其他'
                END as intention_name,
                FROM_UNIXTIME(crm.nexttime) as next_follow_time
            FROM un_channel_crm crm
            JOIN un_channel_client c ON crm.Client_Id = c.Client_Id
            WHERE crm.kfid = %s 
            AND crm.nexttime <= UNIX_TIMESTAMP(CURDATE() + INTERVAL 1 DAY)
            AND crm.processed = 0
            ORDER BY crm.nexttime ASC
        """
        
        reminders = query_qudao_db(sql, (kf_id,))
        
        if not reminders:
            return json.dumps({
                "message": "今天没有需要跟进的客户",
                "kf_id": kf_id,
                "total_reminders": 0,
                "reminders": []
            }, ensure_ascii=False, indent=2)
        
        # 处理结果
        result_reminders = []
        for reminder in reminders:
            client_status = int(reminder.get('client_status', 0) or 0)
            status_name = status_map.get(client_status, '未知')
            
            reminder_info = {
                "client_id": reminder.get('Client_Id'),
                "client_name": reminder.get('ClientName', ''),
                "phone": _mask_phone(reminder.get('MobilePhone', '')),
                "client_status": status_name,
                "intention_project": reminder.get('intention_name', '其他'),
                "next_follow_time": reminder.get('next_follow_time', ''),
                "nexttime_timestamp": reminder.get('nexttime', 0),
                "processed": reminder.get('processed', 0)
            }
            result_reminders.append(reminder_info)
        
        result = {
            "message": f"今天有 {len(result_reminders)} 个客户需要跟进",
            "kf_id": kf_id,
            "total_reminders": len(result_reminders),
            "reminders": result_reminders
        }
        
        return json.dumps(_serialize(result), ensure_ascii=False, indent=2)
        
    except Exception as e:
        logger.exception("my_crm_reminders failed")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


# ---- 18. 我最近三天跟了什么客户 ----

@mcp.tool()
def my_recent_followups(kf_id: int, days: int = 3) -> str:
    """查询客服最近N天的跟进记录（默认3天）。返回跟进时间、内容摘要、客户信息、客户状态。
    当用户问"最近跟了什么客户"、"跟进记录"、"最近联系了谁"时使用。

    Args:
        kf_id: 客服ID
        days: 查询天数，默认3天
    """
    try:
        # 客户状态映射
        status_map = {
            0: '待跟进', 1: '已联系', 2: '有意向', 3: '已预约',
            4: '已到院', 5: '已成交', 6: '无效', 7: '流失'
        }
        
        sql = """
            SELECT 
                l.addtime,
                l.content,
                c.Client_Id,
                c.ClientName,
                c.client_status,
                c.MobilePhone,
                c.PlasticsIntention,
                CASE
                    WHEN c.PlasticsIntention = 1 THEN '种植牙'
                    WHEN c.PlasticsIntention = 2 THEN '矫正'
                    WHEN c.PlasticsIntention = 3 THEN '美白'
                    WHEN c.PlasticsIntention = 4 THEN '洁牙'
                    WHEN c.PlasticsIntention = 5 THEN '补牙'
                    WHEN c.PlasticsIntention = 6 THEN '拔牙'
                    WHEN c.PlasticsIntention = 7 THEN '口腔检查'
                    WHEN c.PlasticsIntention = 8 THEN '牙周治疗'
                    WHEN c.PlasticsIntention = 9 THEN '根管治疗'
                    WHEN c.PlasticsIntention = 10 THEN '烤瓷牙'
                    ELSE '其他'
                END as intention_name
            FROM un_channel_crm_log l
            JOIN un_channel_client c ON l.client_id = c.Client_Id
            WHERE l.kfid = %s 
            AND l.addtime >= UNIX_TIMESTAMP(DATE_SUB(CURDATE(), INTERVAL %s DAY))
            ORDER BY l.addtime DESC
            LIMIT 50
        """
        
        followups = query_qudao_db(sql, (kf_id, days))
        
        if not followups:
            return json.dumps({
                "message": f"最近{days}天没有跟进记录",
                "kf_id": kf_id,
                "days": days,
                "total_followups": 0,
                "followups": []
            }, ensure_ascii=False, indent=2)
        
        # 处理结果
        result_followups = []
        for followup in followups:
            client_status = int(followup.get('client_status', 0) or 0)
            status_name = status_map.get(client_status, '未知')
            addtime = int(followup.get('addtime', 0) or 0)
            content = followup.get('content', '')
            
            followup_info = {
                "client_id": followup.get('Client_Id'),
                "client_name": followup.get('ClientName', ''),
                "phone": _mask_phone(followup.get('MobilePhone', '')),
                "client_status": status_name,
                "intention_project": followup.get('intention_name', '其他'),
                "followup_time": datetime.fromtimestamp(addtime).strftime('%Y-%m-%d %H:%M:%S') if addtime > 0 else '',
                "content_summary": content[:100] + "..." if len(content) > 100 else content,
                "full_content": content
            }
            result_followups.append(followup_info)
        
        result = {
            "message": f"最近{days}天共跟进了 {len(result_followups)} 条记录",
            "kf_id": kf_id,
            "days": days,
            "total_followups": len(result_followups),
            "followups": result_followups
        }
        
        return json.dumps(_serialize(result), ensure_ascii=False, indent=2)
        
    except Exception as e:
        logger.exception("my_recent_followups failed")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


# ---- 19. 钉钉用户身份解析（含角色和团队） ----

def _resolve_role_and_team(kf_id: int) -> dict:
    """判断用户角色和团队成员。
    逻辑：查询是否有下属（un_admin 中 parentId 指向自己的人）来判断管理者身份。
    """
    role = "kf"  # 默认是客服
    team_kf_ids = []

    try:
        # 检查是否有下属
        subordinates = query_qudao_db(
            "SELECT userid FROM un_admin WHERE parentId = %s AND Status = 1",
            (kf_id,),
        )
        if subordinates:
            role = "manager"
            team_kf_ids = [int(s["userid"]) for s in subordinates if s.get("userid")]
            team_kf_ids.append(kf_id)  # 包含自己

            # 检查下属是否也有下属（总监级）
            for sub in subordinates:
                sub_subs = query_qudao_db(
                    "SELECT COUNT(*) as cnt FROM un_admin WHERE parentId = %s AND Status = 1",
                    (sub["userid"],),
                )
                if sub_subs and int(sub_subs[0].get("cnt", 0)) > 0:
                    role = "director"
                    break
    except Exception:
        logger.debug("role detection failed, defaulting to kf", exc_info=True)

    return {"role": role, "team_kf_ids": team_kf_ids}


@mcp.tool()
def resolve_staff_identity(staff_id: str = "", staff_name: str = "") -> str:
    """根据钉钉工号或姓名查询客服身份信息（kf_id、姓名、部门、角色、团队）。
    返回的 role 字段决定数据权限：
    - role="kf": 普通客服，只能查自己的数据
    - role="manager": 主管，可以查 team_kf_ids 中所有成员的数据
    - role="director": 总监，可以查全公司数据
    当钉钉用户发消息时，首条消息必须调用此工具识别身份。

    Args:
        staff_id: 钉钉工号/staff_id
        staff_name: 姓名
    """
    if not staff_id and not staff_name:
        return json.dumps({"error": "请提供 staff_id 或 staff_name"}, ensure_ascii=False)

    conditions = []
    params = []
    if staff_id:
        conditions.append("a.staffId = %s")
        params.append(staff_id)
    if staff_name:
        conditions.append("a.AdminName LIKE %s")
        params.append(f"%{staff_name}%")

    where = " OR ".join(conditions)
    sql = f"""
        SELECT a.AdminId as kf_id, a.AdminName as kf_name,
               a.staffId as staff_id, a.department,
               a.userid, a.parentId
        FROM un_admin a
        WHERE ({where}) AND a.Status = 1
        LIMIT 5
    """
    try:
        results = query_qudao_db(sql, tuple(params))
        if not results:
            return json.dumps({"error": "未找到匹配的员工", "staff_id": staff_id, "staff_name": staff_name},
                              ensure_ascii=False)

        user = results[0]
        kf_id = int(user.get("kf_id") or user.get("userid") or 0)
        role_info = _resolve_role_and_team(kf_id)
        user.update(role_info)

        # 清理内部字段
        user.pop("parentId", None)
        user.pop("userid", None)

        return json.dumps(_serialize(user), ensure_ascii=False, indent=2)
    except Exception as e:
        logger.exception("resolve_staff_identity failed")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


# ---- 20. 我的每日数据统计 ----

@mcp.tool()
def my_daily_stats(kf_id: int, days: int = 1) -> str:
    """查询客服的每日业绩统计数据。包括新客数、跟进数、派单数、成交数和成交金额。
    当用户问"我的数据"、"今天数据"、"业绩"、"我今天怎么样"时使用。

    Args:
        kf_id: 客服ID
        days: 统计天数，默认1天（今天）
    """
    try:
        # 1. 新客数 - 在指定天数内注册的客户数量
        new_clients_sql = """
            SELECT COUNT(*) as new_clients 
            FROM un_channel_client 
            WHERE KfId = %s 
            AND RegisterTime >= UNIX_TIMESTAMP(DATE_SUB(CURDATE(), INTERVAL %s DAY))
        """
        
        # 2. 跟进数 - 在指定天数内的跟进记录数量
        follow_count_sql = """
            SELECT COUNT(*) as follow_count 
            FROM un_channel_crm_log 
            WHERE kfid = %s 
            AND addtime >= UNIX_TIMESTAMP(DATE_SUB(CURDATE(), INTERVAL %s DAY))
        """
        
        # 3. 派单数 - 在指定天数内该客服的客户被派单的数量
        dispatch_count_sql = """
            SELECT COUNT(*) as dispatch_count 
            FROM un_hospital_order 
            WHERE send_order_time >= UNIX_TIMESTAMP(DATE_SUB(CURDATE(), INTERVAL %s DAY))
            AND Client_Id IN (SELECT Client_Id FROM un_channel_client WHERE KfId = %s)
        """
        
        # 4. 成交数和成交金额 - 在指定天数内该客服客户的成交情况
        deal_stats_sql = """
            SELECT 
                COUNT(*) as deal_count, 
                COALESCE(SUM(true_number), 0) as deal_amount 
            FROM un_channel_paylist 
            WHERE ManagerId = %s 
            AND status = 1 
            AND checktime >= UNIX_TIMESTAMP(DATE_SUB(CURDATE(), INTERVAL %s DAY))
        """
        
        # 执行查询
        new_clients_result = query_qudao_db(new_clients_sql, (kf_id, days))
        follow_count_result = query_qudao_db(follow_count_sql, (kf_id, days))
        dispatch_count_result = query_qudao_db(dispatch_count_sql, (days, kf_id))
        deal_stats_result = query_qudao_db(deal_stats_sql, (kf_id, days))
        
        # 提取数据
        new_clients = new_clients_result[0].get('new_clients', 0) if new_clients_result else 0
        follow_count = follow_count_result[0].get('follow_count', 0) if follow_count_result else 0
        dispatch_count = dispatch_count_result[0].get('dispatch_count', 0) if dispatch_count_result else 0
        deal_count = deal_stats_result[0].get('deal_count', 0) if deal_stats_result else 0
        deal_amount = float(deal_stats_result[0].get('deal_amount', 0)) if deal_stats_result else 0.0
        
        # 获取客服姓名
        kf_info_sql = "SELECT realname, AdminName FROM un_admin WHERE userid = %s LIMIT 1"
        kf_info_result = query_qudao_db(kf_info_sql, (kf_id,))
        kf_name = "未知"
        if kf_info_result:
            kf_name = kf_info_result[0].get('realname') or kf_info_result[0].get('AdminName', '未知')
        
        # 计算转化率
        conversion_rate = 0.0
        if new_clients > 0:
            conversion_rate = round((deal_count / new_clients) * 100, 2)
        
        # 计算平均客单价
        avg_deal_amount = 0.0
        if deal_count > 0:
            avg_deal_amount = round(deal_amount / deal_count, 2)
        
        # 构造结果
        result = {
            "kf_id": kf_id,
            "kf_name": kf_name,
            "period_days": days,
            "period_desc": f"最近{days}天" if days > 1 else "今天",
            "stats": {
                "new_clients": new_clients,
                "follow_count": follow_count,
                "dispatch_count": dispatch_count,
                "deal_count": deal_count,
                "deal_amount": deal_amount,
                "conversion_rate": conversion_rate,
                "avg_deal_amount": avg_deal_amount
            },
            "summary": f"新客{new_clients}个，跟进{follow_count}次，派单{dispatch_count}个，成交{deal_count}单，成交金额{deal_amount}元"
        }
        
        return json.dumps(_serialize(result), ensure_ascii=False, indent=2)
        
    except Exception as e:
        logger.exception("my_daily_stats failed")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


# ---- 21. 部门周期对比分析 ----

def _parse_period(text: str) -> tuple:
    """解析时间周期文本，返回 (start_ts, end_ts) 元组
    
    支持的时间格式：
    - "本月": 当月1日0点到现在
    - "上月": 上月1日到上月最后一天
    - "本周": 本周一到现在
    - "近7天": 7天前到现在
    - "近30天": 30天前到现在
    """
    from datetime import datetime, timedelta
    import calendar
    
    now = datetime.now()
    text = text.strip()
    
    if text == "本月":
        # 当月1日0点到现在
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end = now
    elif text == "上月":
        # 上月1日到上月最后一天
        if now.month == 1:
            last_month = now.replace(year=now.year-1, month=12, day=1)
        else:
            last_month = now.replace(month=now.month-1, day=1)
        start = last_month.replace(hour=0, minute=0, second=0, microsecond=0)
        # 上月最后一天
        last_day = calendar.monthrange(last_month.year, last_month.month)[1]
        end = last_month.replace(day=last_day, hour=23, minute=59, second=59, microsecond=999999)
    elif text == "本周":
        # 本周一到现在
        weekday = now.weekday()  # 0=周一, 6=周日
        start = (now - timedelta(days=weekday)).replace(hour=0, minute=0, second=0, microsecond=0)
        end = now
    elif text.startswith("近") and text.endswith("天"):
        # 近N天：N天前到现在
        try:
            days = int(text[1:-1])
            start = now - timedelta(days=days)
            end = now
        except ValueError:
            raise ValueError(f"无法解析时间格式: {text}")
    else:
        raise ValueError(f"不支持的时间格式: {text}")
    
    return int(start.timestamp()), int(end.timestamp())


@mcp.tool()
def kf_period_comparison(kf_name: str, period1: str = "本月", period2: str = "上月") -> str:
    """客服期间对比分析。对比某个客服在两个时间段的业绩表现（新客数、跟进数、派单数、成交数和金额）。
    当用户问"XX客服上个月/对比/环比/趋势"时使用。
    
    Args:
        kf_name: 客服姓名
        period1: 第一个对比周期，默认"本月"，支持: 本月/上月/本周/近7天/近30天
        period2: 第二个对比周期，默认"上月"，支持同上
    """
    try:
        # 1. 查询客服ID
        kf_sql = "SELECT userid FROM un_admin WHERE realname LIKE %s LIMIT 1"
        kf_result = query_qudao_db(kf_sql, (f"%{kf_name}%",))
        if not kf_result:
            return json.dumps({"error": f"未找到客服: {kf_name}"}, ensure_ascii=False)
        
        userid = kf_result[0]['userid']
        
        # 2. 解析时间周期
        start_ts1, end_ts1 = _parse_period(period1)
        start_ts2, end_ts2 = _parse_period(period2)
        
        # 3. 查询两个时间段的各项指标
        metrics = []
        
        # 3.1 新客数
        new_clients_sql = """
            SELECT COUNT(*) as count 
            FROM un_channel_client 
            WHERE KfId = %s AND RegisterTime BETWEEN %s AND %s
        """
        p1_new = query_qudao_db(new_clients_sql, (userid, start_ts1, end_ts1))[0]['count']
        p2_new = query_qudao_db(new_clients_sql, (userid, start_ts2, end_ts2))[0]['count']
        
        # 3.2 跟进数
        follow_sql = """
            SELECT COUNT(*) as count
            FROM un_channel_crm_log
            WHERE kfid = %s AND addtime BETWEEN %s AND %s
        """
        p1_follow = query_qudao_db(follow_sql, (userid, start_ts1, end_ts1))[0]['count']
        p2_follow = query_qudao_db(follow_sql, (userid, start_ts2, end_ts2))[0]['count']
        
        # 3.3 派单数（通过关联查询）
        dispatch_sql = """
            SELECT COUNT(DISTINCT ho.Client_Id) as count
            FROM un_hospital_order ho
            INNER JOIN un_channel_client cc ON ho.Client_Id = cc.Client_Id
            WHERE cc.KfId = %s AND ho.createtime BETWEEN %s AND %s
        """
        p1_dispatch = query_qudao_db(dispatch_sql, (userid, start_ts1, end_ts1))[0]['count']
        p2_dispatch = query_qudao_db(dispatch_sql, (userid, start_ts2, end_ts2))[0]['count']
        
        # 3.4 成交数和金额
        deal_sql = """
            SELECT COUNT(*) as count, COALESCE(SUM(true_number), 0) as amount
            FROM un_channel_paylist
            WHERE KfId = %s AND status = 1 AND checktime BETWEEN %s AND %s
        """
        p1_deal = query_qudao_db(deal_sql, (userid, start_ts1, end_ts1))[0]
        p2_deal = query_qudao_db(deal_sql, (userid, start_ts2, end_ts2))[0]
        
        # 4. 计算各指标的变化百分比和方向
        def calculate_change(p1_val, p2_val):
            if p2_val > 0:
                change_pct = round((p1_val - p2_val) / p2_val * 100, 2)
            else:
                change_pct = 100.0 if p1_val > 0 else 0.0
            
            if change_pct > 0:
                direction = "涨"
            elif change_pct < 0:
                direction = "跌"
            else:
                direction = "平"
                
            return change_pct, direction
        
        # 新客数
        new_change, new_direction = calculate_change(p1_new, p2_new)
        metrics.append({
            "name": "新客数",
            "p1_value": p1_new,
            "p2_value": p2_new,
            "change_pct": new_change,
            "direction": new_direction
        })
        
        # 跟进数
        follow_change, follow_direction = calculate_change(p1_follow, p2_follow)
        metrics.append({
            "name": "跟进数",
            "p1_value": p1_follow,
            "p2_value": p2_follow,
            "change_pct": follow_change,
            "direction": follow_direction
        })
        
        # 派单数
        dispatch_change, dispatch_direction = calculate_change(p1_dispatch, p2_dispatch)
        metrics.append({
            "name": "派单数",
            "p1_value": p1_dispatch,
            "p2_value": p2_dispatch,
            "change_pct": dispatch_change,
            "direction": dispatch_direction
        })
        
        # 成交数
        deal_count_change, deal_count_direction = calculate_change(p1_deal['count'], p2_deal['count'])
        metrics.append({
            "name": "成交数",
            "p1_value": p1_deal['count'],
            "p2_value": p2_deal['count'],
            "change_pct": deal_count_change,
            "direction": deal_count_direction
        })
        
        # 成交金额
        deal_amount_change, deal_amount_direction = calculate_change(
            float(p1_deal['amount'] or 0), 
            float(p2_deal['amount'] or 0)
        )
        metrics.append({
            "name": "成交金额",
            "p1_value": float(p1_deal['amount'] or 0),
            "p2_value": float(p2_deal['amount'] or 0),
            "change_pct": deal_amount_change,
            "direction": deal_amount_direction
        })
        
        # 5. 构建结果
        result = {
            "kf_name": kf_name,
            "period1_label": period1,
            "period2_label": period2,
            "metrics": metrics,
            "summary": {
                "best_metric": max(metrics, key=lambda x: x['change_pct'])['name'] if metrics else None,
                "worst_metric": min(metrics, key=lambda x: x['change_pct'])['name'] if metrics else None,
                "rising_count": len([m for m in metrics if m['change_pct'] > 0]),
                "falling_count": len([m for m in metrics if m['change_pct'] < 0])
            }
        }
        
        return json.dumps(_serialize(result), ensure_ascii=False, indent=2)
        
    except Exception as e:
        logger.exception("kf_period_comparison failed")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@mcp.tool()
def department_period_comparison(period1: str = "本月", period2: str = "上月") -> str:
    """各部门周期对比分析。对比两个时间段内各部门的成交金额变化。
    当用户问"各部门对比上个月"、"公司成交环比"、"部门业绩涨跌"时使用。
    
    Args:
        period1: 第一个对比周期，默认"本月"，支持: 本月/上月/本周/近7天/近30天
        period2: 第二个对比周期，默认"上月"，支持同上
    """
    try:
        # 解析时间周期
        start_ts1, end_ts1 = _parse_period(period1)
        start_ts2, end_ts2 = _parse_period(period2)
        
        # 分别查询两个时间段的数据
        data1 = query_company_stats(start_ts1, end_ts1, period1, 'checktime')
        data2 = query_company_stats(start_ts2, end_ts2, period2, 'checktime')
        
        if data1.get('error') or data2.get('error'):
            return json.dumps({
                "error": "查询失败",
                "period1_error": data1.get('error'),
                "period2_error": data2.get('error')
            }, ensure_ascii=False)
        
        # 提取各部门数据（department 字段是 dict，需要用 small_dep 作为 key）
        depts1 = {}
        for d in data1.get('departments', []):
            dept_key = d['department'].get('small_dep', d['department'].get('department', 'Unknown'))
            depts1[dept_key] = d['statistics']
            
        depts2 = {}
        for d in data2.get('departments', []):
            dept_key = d['department'].get('small_dep', d['department'].get('department', 'Unknown'))
            depts2[dept_key] = d['statistics']
        
        # 计算部门对比
        departments = []
        all_dept_names = set(depts1.keys()) | set(depts2.keys())
        
        for dept_name in all_dept_names:
            stats1 = depts1.get(dept_name, {})
            stats2 = depts2.get(dept_name, {})
            
            p1_money = float(stats1.get('total_money', 0) or 0)
            p2_money = float(stats2.get('total_money', 0) or 0)
            
            # 计算变化百分比
            if p2_money > 0:
                change_pct = round((p1_money - p2_money) / p2_money * 100, 2)
            else:
                change_pct = 100.0 if p1_money > 0 else 0.0
            
            # 判断涨跌方向
            if change_pct > 0:
                direction = "涨"
                arrow = "📈"
            elif change_pct < 0:
                direction = "跌"
                arrow = "📉"
            else:
                direction = "平"
                arrow = "➡️"
            
            departments.append({
                "name": dept_name,
                "p1_money": p1_money,
                "p2_money": p2_money,
                "change_pct": change_pct,
                "direction": direction,
                "arrow": arrow
            })
        
        # 按成交金额排序（period1）
        departments.sort(key=lambda x: x['p1_money'], reverse=True)
        
        # 计算总体对比
        total_p1 = float(data1.get('statistics', {}).get('total_money', 0) or 0)
        total_p2 = float(data2.get('statistics', {}).get('total_money', 0) or 0)
        
        if total_p2 > 0:
            total_change_pct = round((total_p1 - total_p2) / total_p2 * 100, 2)
        else:
            total_change_pct = 100.0 if total_p1 > 0 else 0.0
        
        # 构建结果
        result = {
            "period1_label": period1,
            "period2_label": period2,
            "departments": departments,
            "summary": {
                "total_p1": total_p1,
                "total_p2": total_p2,
                "total_change_pct": total_change_pct,
                "total_direction": "涨" if total_change_pct > 0 else ("跌" if total_change_pct < 0 else "平")
            },
            "report": _generate_comparison_report(departments, period1, period2, total_change_pct)
        }
        
        return json.dumps(_serialize(result), ensure_ascii=False, indent=2)
        
    except Exception as e:
        logger.exception("department_period_comparison failed")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


def _generate_comparison_report(departments: list, period1: str, period2: str, total_change: float) -> str:
    """生成部门对比报告（Markdown格式）"""
    
    # 表格标题
    report = f"## 📊 {period1} vs {period2} 部门业绩对比\n\n"
    
    # 总体概况
    total_arrow = "📈" if total_change > 0 else ("📉" if total_change < 0 else "➡️")
    report += f"**总体变化**: {total_arrow} {total_change:+.2f}%\n\n"
    
    # 表格头
    report += "| 部门 | " + period1 + " | " + period2 + " | 变化 | 涨跌 |\n"
    report += "|------|--------|--------|------|------|\n"
    
    # 表格内容
    for dept in departments:
        if dept['p1_money'] == 0 and dept['p2_money'] == 0:
            continue  # 跳过没有数据的部门
            
        report += f"| {dept['name']} | ¥{dept['p1_money']:,.0f} | ¥{dept['p2_money']:,.0f} | {dept['change_pct']:+.1f}% | {dept['arrow']} |\n"
    
    # 分析摘要
    rising_depts = [d for d in departments if d['change_pct'] > 0 and d['p1_money'] > 0]
    falling_depts = [d for d in departments if d['change_pct'] < 0 and d['p1_money'] > 0]
    
    report += f"\n### 📈 表现分析\n"
    
    if rising_depts:
        rising_depts.sort(key=lambda x: x['change_pct'], reverse=True)
        top_riser = rising_depts[0]
        report += f"- **最强部门**: {top_riser['name']} (+{top_riser['change_pct']:.1f}%)\n"
    
    if falling_depts:
        falling_depts.sort(key=lambda x: x['change_pct'])
        top_faller = falling_depts[0]
        report += f"- **下滑最大**: {top_faller['name']} ({top_faller['change_pct']:.1f}%)\n"
    
    report += f"- **上涨部门**: {len(rising_depts)}个\n"
    report += f"- **下滑部门**: {len(falling_depts)}个\n"
    
    return report


# ---- 22. 部门细化下钻分析 ----

@mcp.tool()
def department_drill_down(department_name: str, days: int = 30) -> str:
    """部门细化下钻分析。先获取部门对应的渠道列表，然后按客服和来源维度分析成交分布。
    当用户问"XX部门怎么了"、"为什么掉了"、"下钻看看"、"具体分析一下"时使用。

    Args:
        department_name: 部门名称缩写（如 TEG/BCG/CDG/OMG/BSG/MMG/CMG/AMG）
        days: 统计天数，默认30天
    """
    try:
        # 1. 从 DEPARTMENT_MAP 获取 department_id
        if department_name not in DEPARTMENT_MAP:
            return json.dumps({
                "error": f"未知部门: {department_name}",
                "valid_departments": list(DEPARTMENT_MAP.keys())
            }, ensure_ascii=False)
        
        dept_id, dept_full_name = DEPARTMENT_MAP[department_name]
        
        # 2. 获取该部门所有渠道 channel_id 列表
        channel_ids = _get_department_channel_ids(dept_id)
        if not channel_ids:
            return json.dumps({
                "error": f"部门 {department_name} 没有关联的渠道",
                "department_id": dept_id,
                "department_full_name": dept_full_name
            }, ensure_ascii=False)
        
        # 3. 按客服分组统计成交数据
        channel_ids_str = ",".join(str(cid) for cid in channel_ids)
        kf_stats_sql = f"""
            SELECT 
                p.ManagerId,
                a.realname as kf_name,
                COUNT(*) as deal_count,
                SUM(p.true_number) as deal_amount
            FROM un_channel_paylist p
            LEFT JOIN un_admin a ON p.ManagerId = a.userid
            WHERE p.status = 1 
            AND p.from_type IN ({channel_ids_str})
            AND p.checktime >= UNIX_TIMESTAMP(DATE_SUB(CURDATE(), INTERVAL %s DAY))
            GROUP BY p.ManagerId, a.realname
            ORDER BY deal_amount DESC
        """
        
        kf_results = query_qudao_db(kf_stats_sql, (days,))
        
        # 4. 按来源分组统计成交分布
        source_stats_sql = f"""
            SELECT 
                p.from_type,
                COUNT(*) as deal_count,
                SUM(p.true_number) as deal_amount,
                CASE 
                    WHEN p.from_type = 1 THEN '百度SEM'
                    WHEN p.from_type = 2 THEN '百度SEO'
                    WHEN p.from_type = 3 THEN '360SEM'
                    WHEN p.from_type = 4 THEN '搜狗SEM'
                    WHEN p.from_type = 5 THEN '神马SEM'
                    WHEN p.from_type = 6 THEN '今日头条'
                    WHEN p.from_type = 7 THEN '抖音'
                    WHEN p.from_type = 8 THEN '快手'
                    WHEN p.from_type = 9 THEN '小红书'
                    WHEN p.from_type = 10 THEN '微信朋友圈'
                    WHEN p.from_type = 11 THEN '微博'
                    WHEN p.from_type = 12 THEN 'QQ空间'
                    WHEN p.from_type = 13 THEN '腾讯广点通'
                    WHEN p.from_type = 14 THEN '美团'
                    WHEN p.from_type = 15 THEN '大众点评'
                    WHEN p.from_type = 16 THEN '口碑'
                    WHEN p.from_type = 17 THEN '贝色'
                    WHEN p.from_type = 18 THEN '牙舒丽'
                    WHEN p.from_type = 19 THEN '自然流量'
                    WHEN p.from_type = 20 THEN '直接访问'
                    ELSE CONCAT('来源', p.from_type)
                END as source_name
            FROM un_channel_paylist p
            WHERE p.status = 1 
            AND p.from_type IN ({channel_ids_str})
            AND p.checktime >= UNIX_TIMESTAMP(DATE_SUB(CURDATE(), INTERVAL %s DAY))
            GROUP BY p.from_type
            ORDER BY deal_amount DESC
        """
        
        source_results = query_qudao_db(source_stats_sql, (days,))
        
        # 5. 处理客服数据
        by_kf = []
        for kf in kf_results:
            manager_id = kf.get('ManagerId')
            kf_name = kf.get('kf_name') or f"客服{manager_id}"
            deal_count = int(kf.get('deal_count', 0) or 0)
            deal_amount = float(kf.get('deal_amount', 0) or 0)
            
            by_kf.append({
                "kf_name": kf_name,
                "deal_count": deal_count,
                "deal_amount": deal_amount
            })
        
        # 6. 处理来源数据
        by_source = []
        for source in source_results:
            from_type = source.get('from_type')
            source_name = source.get('source_name', f'来源{from_type}')
            deal_count = int(source.get('deal_count', 0) or 0)
            deal_amount = float(source.get('deal_amount', 0) or 0)
            
            by_source.append({
                "source_name": source_name,
                "deal_count": deal_count,
                "deal_amount": deal_amount
            })
        
        # 7. 统计总计数据
        total_deals = sum(kf['deal_count'] for kf in by_kf)
        total_amount = sum(kf['deal_amount'] for kf in by_kf)
        
        # 8. 构造返回结果
        result = {
            "department": {
                "name": department_name,
                "full_name": dept_full_name,
                "department_id": dept_id,
                "channel_ids": channel_ids,
                "analysis_days": days
            },
            "summary": {
                "total_deals": total_deals,
                "total_amount": total_amount,
                "avg_deal_amount": round(total_amount / total_deals, 2) if total_deals > 0 else 0,
                "active_kf_count": len(by_kf),
                "active_source_count": len(by_source)
            },
            "by_kf": by_kf,
            "by_source": by_source,
            "analysis_summary": f"{department_name}部门最近{days}天共成交{total_deals}单，总金额{total_amount:,.2f}元，涉及{len(by_kf)}个客服、{len(by_source)}个来源渠道"
        }
        
        return json.dumps(_serialize(result), ensure_ascii=False, indent=2)
        
    except Exception as e:
        logger.exception("department_drill_down failed")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


# ---- 23. 渠道ROI排名 ----

@mcp.tool()
def source_roi_ranking(days: int = 30, client_region: int = 0) -> str:
    """查询各渠道来源的ROI排名。分析不同来源渠道的注册数、成交数、成交金额和ROI效果。
    当用户问"渠道ROI"、"哪个渠道最好"、"来源排名"、"哪个渠道效果最好"时使用。

    Args:
        days: 统计天数，默认30天
        client_region: 业务线过滤（0=全部，1=医美，2=口腔，4=韩国，8=眼科），默认0
    """
    try:
        # from_type 映射到中文名称
        from_type_map = {
            2: '400电话',
            3: '商务通',
            4: '微信',
            5: 'QQ',
            6: '其他',
            7: 'APP',
            8: '导入'
        }
        
        # 构建查询SQL
        sql = """
            SELECT 
                c.from_type,
                COUNT(DISTINCT c.Client_Id) as register_count,
                COUNT(DISTINCT p.Client_Id) as deal_count,
                COALESCE(SUM(p.true_number), 0) as deal_amount
            FROM un_channel_client c
            LEFT JOIN un_channel_paylist p ON c.Client_Id = p.Client_Id 
                AND p.status = 1 
                AND p.checktime >= UNIX_TIMESTAMP(DATE_SUB(CURDATE(), INTERVAL %s DAY))
            WHERE c.RegisterTime >= UNIX_TIMESTAMP(DATE_SUB(CURDATE(), INTERVAL %s DAY))
            AND (0 = %s OR c.client_region = %s)
            GROUP BY c.from_type
            HAVING register_count > 10
            ORDER BY deal_amount DESC
        """
        
        results = query_qudao_db(sql, (days, days, client_region, client_region))
        
        if not results:
            return json.dumps({
                "message": f"最近{days}天没有找到符合条件的渠道数据",
                "days": days,
                "client_region": client_region,
                "sources": []
            }, ensure_ascii=False, indent=2)
        
        # 处理结果数据
        source_rankings = []
        for result in results:
            from_type = int(result.get('from_type', 0) or 0)
            register_count = int(result.get('register_count', 0) or 0)
            deal_count = int(result.get('deal_count', 0) or 0)
            deal_amount = float(result.get('deal_amount', 0) or 0)
            
            # 计算 ROI 和转化率
            roi = round(deal_amount / register_count, 2) if register_count > 0 else 0
            conversion_rate = round((deal_count / register_count) * 100, 2) if register_count > 0 else 0
            avg_deal_amount = round(deal_amount / deal_count, 2) if deal_count > 0 else 0
            
            # 获取渠道中文名称
            source_name = from_type_map.get(from_type, f'来源{from_type}')
            
            source_data = {
                "from_type": from_type,
                "source_name": source_name,
                "register_count": register_count,
                "deal_count": deal_count,
                "deal_amount": deal_amount,
                "roi": roi,  # 每注册用户的平均成交金额
                "conversion_rate": conversion_rate,  # 注册到成交的转化率
                "avg_deal_amount": avg_deal_amount,  # 平均客单价
                "rank": 0  # 将在排序后设置
            }
            source_rankings.append(source_data)
        
        # 按成交金额排序（已经在SQL中排序了，但确保一致性）
        source_rankings.sort(key=lambda x: x['deal_amount'], reverse=True)
        
        # 设置排名
        for i, source in enumerate(source_rankings, 1):
            source['rank'] = i
        
        # 计算汇总数据
        total_registers = sum(s['register_count'] for s in source_rankings)
        total_deals = sum(s['deal_count'] for s in source_rankings)
        total_amount = sum(s['deal_amount'] for s in source_rankings)
        overall_conversion_rate = round((total_deals / total_registers) * 100, 2) if total_registers > 0 else 0
        overall_roi = round(total_amount / total_registers, 2) if total_registers > 0 else 0
        
        # 找出最佳渠道
        best_roi_source = max(source_rankings, key=lambda x: x['roi']) if source_rankings else None
        best_conversion_source = max(source_rankings, key=lambda x: x['conversion_rate']) if source_rankings else None
        best_volume_source = max(source_rankings, key=lambda x: x['deal_amount']) if source_rankings else None
        
        # 构建返回结果
        result = {
            "analysis_period": {
                "days": days,
                "client_region": client_region,
                "client_region_name": {
                    0: "全部业务线",
                    1: "医美",
                    2: "口腔", 
                    4: "韩国",
                    8: "眼科"
                }.get(client_region, f"业务线{client_region}")
            },
            "summary": {
                "total_sources": len(source_rankings),
                "total_registers": total_registers,
                "total_deals": total_deals,
                "total_amount": total_amount,
                "overall_conversion_rate": overall_conversion_rate,
                "overall_roi": overall_roi
            },
            "top_performers": {
                "best_roi": {
                    "source": best_roi_source['source_name'] if best_roi_source else None,
                    "roi": best_roi_source['roi'] if best_roi_source else 0
                },
                "best_conversion": {
                    "source": best_conversion_source['source_name'] if best_conversion_source else None,
                    "rate": best_conversion_source['conversion_rate'] if best_conversion_source else 0
                },
                "best_volume": {
                    "source": best_volume_source['source_name'] if best_volume_source else None,
                    "amount": best_volume_source['deal_amount'] if best_volume_source else 0
                }
            },
            "source_rankings": source_rankings,
            "analysis_note": f"ROI = 成交金额/注册数，转化率 = 成交数/注册数，仅显示注册数>10的渠道"
        }
        
        return json.dumps(_serialize(result), ensure_ascii=False, indent=2)
        
    except Exception as e:
        logger.exception("source_roi_ranking failed")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


# ---- 24. 客户流失分析 ----

@mcp.tool()
def client_drop_analysis(client_id: int) -> str:
    """客户流失原因分析。分析客户为什么不跟了，包括客户状态、无单原因、跟进情况、提醒处理等。
    当用户问"这个客户为什么不跟了"、"客户怎么了"、"这个客户什么情况"时使用。
    
    Args:
        client_id: 客户ID
    """
    try:
        # 1. 获取客户基本信息和状态
        client_sql = """
            SELECT 
                c.Client_Id,
                c.ClientName,
                c.client_status,
                c.no_order_type,
                c.KfId,
                c.RegisterTime,
                c.MobilePhone,
                c.PlasticsIntention,
                a.realname as kf_name,
                CASE
                    WHEN c.PlasticsIntention = 1 THEN '种植牙'
                    WHEN c.PlasticsIntention = 2 THEN '矫正'
                    WHEN c.PlasticsIntention = 3 THEN '美白'
                    WHEN c.PlasticsIntention = 4 THEN '洁牙'
                    WHEN c.PlasticsIntention = 5 THEN '补牙'
                    WHEN c.PlasticsIntention = 6 THEN '拔牙'
                    WHEN c.PlasticsIntention = 7 THEN '口腔检查'
                    WHEN c.PlasticsIntention = 8 THEN '牙周治疗'
                    WHEN c.PlasticsIntention = 9 THEN '根管治疗'
                    WHEN c.PlasticsIntention = 10 THEN '烤瓷牙'
                    ELSE '其他'
                END as intention_name,
                CASE
                    WHEN c.client_status = 0 THEN '待跟进'
                    WHEN c.client_status = 1 THEN '已联系'
                    WHEN c.client_status = 2 THEN '有意向'
                    WHEN c.client_status = 3 THEN '已预约'
                    WHEN c.client_status = 4 THEN '已到院'
                    WHEN c.client_status = 5 THEN '已成交'
                    WHEN c.client_status = 6 THEN '无效'
                    WHEN c.client_status = 7 THEN '流失'
                    ELSE '未知'
                END as status_text
            FROM un_channel_client c
            LEFT JOIN un_admin a ON c.KfId = a.userid
            WHERE c.Client_Id = %s
            LIMIT 1
        """
        
        client_result = query_qudao_db(client_sql, (client_id,))
        if not client_result:
            return json.dumps({"error": f"客户 {client_id} 不存在"}, ensure_ascii=False)
        
        client_info = client_result[0]
        
        # 2. 获取最后3条跟进记录
        follow_sql = """
            SELECT 
                l.addtime,
                l.content,
                FROM_UNIXTIME(l.addtime) as follow_time
            FROM un_channel_crm_log l
            WHERE l.client_id = %s 
            ORDER BY l.addtime DESC
            LIMIT 3
        """
        
        follow_records = query_qudao_db(follow_sql, (client_id,))
        
        # 3. 获取最近的CRM提醒是否已处理
        reminder_sql = """
            SELECT 
                crm.nexttime,
                crm.processed,
                FROM_UNIXTIME(crm.nexttime) as next_follow_time
            FROM un_channel_crm crm
            WHERE crm.Client_Id = %s
            ORDER BY crm.nexttime DESC
            LIMIT 1
        """
        
        reminder_result = query_qudao_db(reminder_sql, (client_id,))
        
        # 4. 处理客户基本信息
        client_name = client_info.get('ClientName', '未知')
        client_status = int(client_info.get('client_status', 0) or 0)
        status_text = client_info.get('status_text', '未知')
        no_order_type = int(client_info.get('no_order_type', 0) or 0)
        register_time = int(client_info.get('RegisterTime', 0) or 0)
        kf_name = client_info.get('kf_name', '未知')
        intention_name = client_info.get('intention_name', '其他')
        phone = _mask_phone(client_info.get('MobilePhone', ''))
        
        # 5. 处理跟进记录
        last_follow_time = ""
        last_follow_content = ""
        days_no_follow = 0
        
        if follow_records:
            last_addtime = int(follow_records[0].get('addtime', 0) or 0)
            if last_addtime > 0:
                last_follow_time = datetime.fromtimestamp(last_addtime).strftime('%Y-%m-%d %H:%M')
                days_no_follow = int((time.time() - last_addtime) / 86400)
            last_follow_content = follow_records[0].get('content', '')
        else:
            # 如果没有跟进记录，计算从注册时间到现在的天数
            if register_time > 0:
                days_no_follow = int((time.time() - register_time) / 86400)
                last_follow_time = "从未跟进"
        
        # 6. 处理提醒信息
        reminder_processed = True
        next_follow_time = ""
        if reminder_result:
            reminder_processed = bool(reminder_result[0].get('processed', 1))
            next_follow_time = reminder_result[0].get('next_follow_time', '')
        
        # 7. 分析逻辑
        drop_reasons = []
        recommended_action = ""
        
        # 客户状态分析
        if client_status == 5:
            drop_reasons.append("客户标记为无意向")
            recommended_action = "确认是否真的无意向，考虑重新激活或标记为无效"
        elif client_status == 6:
            drop_reasons.append("客户标记为无效")
            recommended_action = "已标记无效，无需继续跟进"
        elif client_status == 7:
            drop_reasons.append("客户标记为流失")
            recommended_action = "分析流失原因，考虑挽回策略"
        
        # 无法派单原因分析
        if no_order_type > 0:
            # 使用 get_linkage_name 查询无单原因中文名称
            try:
                no_order_reason = get_linkage_name(no_order_type)
                if no_order_reason:
                    drop_reasons.append(f"有明确的无法派单原因：{no_order_reason}")
                else:
                    drop_reasons.append(f"有无法派单原因（ID:{no_order_type}）")
            except:
                drop_reasons.append(f"有无法派单原因（ID:{no_order_type}）")
        
        # 跟进时间分析
        if days_no_follow > 14:
            drop_reasons.append(f"最后跟进超过{days_no_follow}天，可能被遗忘")
            if not recommended_action:
                recommended_action = "立即联系客户，重新建立沟通"
        elif days_no_follow > 7:
            drop_reasons.append(f"已{days_no_follow}天未跟进，需要关注")
            if not recommended_action:
                recommended_action = "尽快联系客户，了解当前状态"
        
        # 跟进内容情感分析
        negative_keywords = ["不想做", "考虑", "太贵", "害怕", "不考虑", "暂时不", "经济", "没钱", "贵了", "再说"]
        if last_follow_content:
            content_lower = last_follow_content.lower()
            found_keywords = [kw for kw in negative_keywords if kw in content_lower]
            if found_keywords:
                drop_reasons.append(f"跟进内容显示客户犹豫（提到：{', '.join(found_keywords)}）")
                if not recommended_action:
                    recommended_action = "针对客户顾虑进行专门解答和价格调整"
        
        # 提醒处理状态分析
        if not reminder_processed and next_follow_time:
            drop_reasons.append("有未处理的跟进提醒")
            if not recommended_action:
                recommended_action = "处理未完成的跟进提醒"
        
        # 默认建议
        if not recommended_action:
            if client_status in [0, 1, 2]:  # 正常状态
                recommended_action = "保持正常跟进节奏，关注客户需求变化"
            else:
                recommended_action = "评估客户价值，决定是否继续投入"
        
        # 如果没有发现明显问题
        if not drop_reasons:
            if days_no_follow <= 3:
                drop_reasons.append("客户状态正常，跟进及时")
                recommended_action = "保持当前跟进节奏"
            else:
                drop_reasons.append("未发现明显问题，但需要加强跟进")
                recommended_action = "增加跟进频率，深入了解客户需求"
        
        # 8. 构建返回结果
        result = {
            "client_id": client_id,
            "client_name": client_name,
            "phone": phone,
            "status_text": status_text,
            "intention_project": intention_name,
            "kf_name": kf_name,
            "register_days": int((time.time() - register_time) / 86400) if register_time > 0 else 0,
            "last_follow_time": last_follow_time,
            "last_follow_content": last_follow_content[:200] + "..." if len(last_follow_content) > 200 else last_follow_content,
            "days_no_follow": days_no_follow,
            "no_order_type": no_order_type,
            "reminder_processed": reminder_processed,
            "next_follow_time": next_follow_time,
            "drop_reasons": drop_reasons,
            "recommended_action": recommended_action,
            "analysis_summary": f"{client_name}（{intention_name}）已注册{int((time.time() - register_time) / 86400) if register_time > 0 else 0}天，{days_no_follow}天未跟进，当前状态：{status_text}",
            "follow_records": [
                {
                    "time": datetime.fromtimestamp(int(record.get('addtime', 0) or 0)).strftime('%Y-%m-%d %H:%M') if record.get('addtime') else '',
                    "content": record.get('content', '')[:100] + "..." if len(record.get('content', '')) > 100 else record.get('content', '')
                }
                for record in follow_records
            ]
        }
        
        return json.dumps(_serialize(result), ensure_ascii=False, indent=2)
        
    except Exception as e:
        logger.exception("client_drop_analysis failed")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


# ---- 25. 我录的单子审核了吗 ----

@mcp.tool()
def my_pending_deals(kf_id: int, days: int = 30) -> str:
    """查询客服最近录入的成交单审核状态。返回包括待审核、已核销、退款、坏账等状态的成交单列表。
    当用户说"我的单子"、"审核状态"、"成交单"、"录的单"时使用。

    Args:
        kf_id: 客服ID
        days: 查询天数，默认30天
    """
    try:
        # 状态映射
        status_map = {
            0: '待审核',
            1: '已核销',
            3: '退款',
            5: '坏账'
        }
        
        # 币种映射  
        currency_map = {
            0: '人民币',
            1: '韩币',
            3: '美元',
            4: '泰铢',
            5: '日元'
        }
        
        # 查询SQL - 客服录入的成交单
        sql = """
            SELECT 
                p.id,
                p.Client_Id,
                c.ClientName,
                p.number,
                p.true_number,
                p.status,
                p.addtime,
                p.checktime,
                p.currency_type,
                FROM_UNIXTIME(p.addtime) as add_time_str,
                FROM_UNIXTIME(p.checktime) as check_time_str
            FROM un_channel_paylist p
            LEFT JOIN un_channel_client c ON p.Client_Id = c.Client_Id
            WHERE p.KfId = %s 
            AND p.addtime >= UNIX_TIMESTAMP(DATE_SUB(CURDATE(), INTERVAL %s DAY))
            ORDER BY 
                CASE WHEN p.status = 0 THEN 0 ELSE 1 END,  -- 未审核的排在前面
                p.addtime DESC
        """
        
        deals = query_qudao_db(sql, (kf_id, days))
        
        if not deals:
            return json.dumps({
                "message": f"最近{days}天没有录入的成交单",
                "kf_id": kf_id,
                "days": days,
                "total_deals": 0,
                "deals": []
            }, ensure_ascii=False, indent=2)
        
        # 处理结果
        result_deals = []
        pending_count = 0
        approved_count = 0
        total_amount = 0.0
        pending_amount = 0.0
        
        for deal in deals:
            deal_id = deal.get('id')
            client_id = deal.get('Client_Id')
            client_name = deal.get('ClientName', '未知客户')
            number = float(deal.get('number', 0) or 0)
            true_number = float(deal.get('true_number', 0) or 0)
            status = int(deal.get('status', 0) or 0)
            addtime = int(deal.get('addtime', 0) or 0)
            checktime = int(deal.get('checktime', 0) or 0)
            currency_type = int(deal.get('currency_type', 0) or 0)
            
            # 状态和币种中文名
            status_name = status_map.get(status, f'状态{status}')
            currency_name = currency_map.get(currency_type, f'币种{currency_type}')
            
            # 格式化时间
            add_time_str = datetime.fromtimestamp(addtime).strftime('%Y-%m-%d %H:%M') if addtime > 0 else ''
            check_time_str = datetime.fromtimestamp(checktime).strftime('%Y-%m-%d %H:%M') if checktime > 0 else ''
            
            # 统计
            if status == 0:  # 待审核
                pending_count += 1
                pending_amount += true_number
            elif status == 1:  # 已核销
                approved_count += 1
            
            total_amount += true_number
            
            deal_info = {
                "id": deal_id,
                "client_id": client_id,
                "client_name": client_name,
                "number": number,
                "true_number": true_number,
                "status": status,
                "status_name": status_name,
                "addtime": addtime,
                "checktime": checktime,
                "add_time": add_time_str,
                "check_time": check_time_str,
                "currency_type": currency_type,
                "currency_name": currency_name,
                "days_waiting": int((time.time() - addtime) / 86400) if addtime > 0 else 0
            }
            result_deals.append(deal_info)
        
        # 获取客服姓名
        kf_info_sql = "SELECT realname, AdminName FROM un_admin WHERE userid = %s LIMIT 1"
        kf_info_result = query_qudao_db(kf_info_sql, (kf_id,))
        kf_name = "未知客服"
        if kf_info_result:
            kf_name = kf_info_result[0].get('realname') or kf_info_result[0].get('AdminName', '未知客服')
        
        # 构造结果
        result = {
            "kf_id": kf_id,
            "kf_name": kf_name,
            "period_days": days,
            "summary": {
                "total_deals": len(result_deals),
                "pending_count": pending_count,
                "approved_count": approved_count,
                "total_amount": total_amount,
                "pending_amount": pending_amount,
                "approval_rate": round(approved_count / len(result_deals) * 100, 2) if len(result_deals) > 0 else 0
            },
            "deals": result_deals,
            "message": f"{kf_name}最近{days}天共录入{len(result_deals)}个成交单，待审核{pending_count}个，已核销{approved_count}个"
        }
        
        return json.dumps(_serialize(result), ensure_ascii=False, indent=2)
        
    except Exception as e:
        logger.exception("my_pending_deals failed")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


# ---- 26. 退款和坏账查询 ----

@mcp.tool()
def refund_and_bad_debt(kf_id: int = 0, days: int = 30) -> str:
    """查询最近的退款和坏账记录。包括退款、坏账、异常单等情况，支持按客服筛选。
    当用户问"退款"、"坏账"、"异常单"、"最近有退款或坏账吗"时使用。

    Args:
        kf_id: 客服ID（0=全部客服）
        days: 统计天数，默认30天
    """
    try:
        # 状态和类型映射
        status_map = {
            3: '退款',
            5: '坏账'
        }
        
        split_order_status_map = {
            5: '异常单',
            7: '异常单'
        }
        
        # 构建查询条件
        conditions = []
        params = []
        
        # 时间条件 - 最近N天
        time_threshold = f"UNIX_TIMESTAMP(DATE_SUB(CURDATE(), INTERVAL %s DAY))"
        conditions.append(f"p.checktime >= {time_threshold}")
        params.append(days)
        
        # 客服条件
        if kf_id > 0:
            conditions.append("p.ManagerId = %s")
            params.append(kf_id)
        
        # 状态条件：退款(status=3)、坏账(status=5)、异常单(split_order_status IN (5,7))
        status_condition = "(p.status = 3 OR p.status = 5 OR p.split_order_status IN (5,7))"
        conditions.append(status_condition)
        
        where_clause = " AND ".join(conditions)
        
        # 查询SQL
        sql = f"""
            SELECT 
                p.id,
                p.Client_Id,
                c.ClientName,
                c.MobilePhone,
                p.number,
                p.true_number,
                p.status,
                p.split_order_status,
                p.checktime,
                p.ManagerId,
                a.realname as kf_name,
                hc.company_name as hospital_name,
                FROM_UNIXTIME(p.checktime) as check_time_str,
                CASE
                    WHEN p.status = 3 THEN '退款'
                    WHEN p.status = 5 THEN '坏账'
                    WHEN p.split_order_status IN (5,7) THEN '异常单'
                    ELSE '其他'
                END as record_type
            FROM un_channel_paylist p
            LEFT JOIN un_channel_client c ON p.Client_Id = c.Client_Id
            LEFT JOIN un_admin a ON p.ManagerId = a.userid
            LEFT JOIN un_hospital_company hc ON p.hospital_id = hc.id
            WHERE {where_clause}
            ORDER BY p.checktime DESC
            LIMIT 100
        """
        
        records = query_qudao_db(sql, params)
        
        if not records:
            kf_desc = f"客服ID {kf_id}" if kf_id > 0 else "全部客服"
            return json.dumps({
                "message": f"最近{days}天{kf_desc}没有退款、坏账或异常单记录",
                "kf_id": kf_id,
                "days": days,
                "total_records": 0,
                "records": []
            }, ensure_ascii=False, indent=2)
        
        # 处理结果
        result_records = []
        stats = {
            'total_records': 0,
            'refund_count': 0,
            'bad_debt_count': 0,
            'abnormal_count': 0,
            'total_amount': 0.0,
            'refund_amount': 0.0,
            'bad_debt_amount': 0.0,
            'abnormal_amount': 0.0
        }
        
        for record in records:
            client_id = record.get('Client_Id')
            client_name = record.get('ClientName', '未知客户')
            number = float(record.get('number', 0) or 0)
            true_number = float(record.get('true_number', 0) or 0)
            status = int(record.get('status', 0) or 0)
            split_order_status = int(record.get('split_order_status', 0) or 0)
            checktime = int(record.get('checktime', 0) or 0)
            manager_id = record.get('ManagerId')
            kf_name = record.get('kf_name', '未知客服')
            hospital_name = record.get('hospital_name', '未知医院')
            record_type = record.get('record_type', '其他')
            
            # 格式化时间
            check_time_str = datetime.fromtimestamp(checktime).strftime('%Y-%m-%d %H:%M') if checktime > 0 else ''
            
            # 统计
            stats['total_records'] += 1
            stats['total_amount'] += true_number
            
            if status == 3:  # 退款
                stats['refund_count'] += 1
                stats['refund_amount'] += true_number
            elif status == 5:  # 坏账
                stats['bad_debt_count'] += 1
                stats['bad_debt_amount'] += true_number
            elif split_order_status in [5, 7]:  # 异常单
                stats['abnormal_count'] += 1
                stats['abnormal_amount'] += true_number
            
            record_info = {
                "id": record.get('id'),
                "client_id": client_id,
                "client_name": client_name,
                "phone": _mask_phone(record.get('MobilePhone', '')),
                "number": number,
                "true_number": true_number,
                "status": status,
                "split_order_status": split_order_status,
                "record_type": record_type,
                "checktime": checktime,
                "check_time": check_time_str,
                "manager_id": manager_id,
                "kf_name": kf_name,
                "hospital_name": hospital_name,
                "days_ago": int((time.time() - checktime) / 86400) if checktime > 0 else 0
            }
            result_records.append(record_info)
        
        # 获取查询的客服信息
        query_kf_name = "全部客服"
        if kf_id > 0:
            kf_info_sql = "SELECT realname, AdminName FROM un_admin WHERE userid = %s LIMIT 1"
            kf_info_result = query_qudao_db(kf_info_sql, (kf_id,))
            if kf_info_result:
                query_kf_name = kf_info_result[0].get('realname') or kf_info_result[0].get('AdminName', f'客服{kf_id}')
        
        # 构造结果
        result = {
            "kf_id": kf_id,
            "kf_name": query_kf_name,
            "period_days": days,
            "statistics": stats,
            "summary": {
                "message": f"最近{days}天{query_kf_name}共有{stats['total_records']}条异常记录",
                "breakdown": f"退款{stats['refund_count']}单(¥{stats['refund_amount']:,.2f})，坏账{stats['bad_debt_count']}单(¥{stats['bad_debt_amount']:,.2f})，异常{stats['abnormal_count']}单(¥{stats['abnormal_amount']:,.2f})",
                "total_impact": f"总损失金额：¥{stats['total_amount']:,.2f}"
            },
            "records": result_records,
            "analysis": _generate_refund_analysis(stats, days)
        }
        
        return json.dumps(_serialize(result), ensure_ascii=False, indent=2)
        
    except Exception as e:
        logger.exception("refund_and_bad_debt failed")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


def _generate_refund_analysis(stats: dict, days: int) -> str:
    """生成退款坏账分析报告"""
    total_records = stats['total_records']
    total_amount = stats['total_amount']
    
    if total_records == 0:
        return f"最近{days}天财务状况良好，没有退款、坏账或异常单。"
    
    analysis_parts = []
    
    # 总体情况
    daily_avg = round(total_records / days, 1)
    amount_avg = round(total_amount / days, 2)
    analysis_parts.append(f"最近{days}天平均每日异常{daily_avg}单，日均损失¥{amount_avg:,.2f}")
    
    # 分类分析
    if stats['refund_count'] > 0:
        refund_pct = round(stats['refund_count'] / total_records * 100, 1)
        analysis_parts.append(f"退款占{refund_pct}%，可能原因：客户不满意、服务质量问题")
    
    if stats['bad_debt_count'] > 0:
        bad_debt_pct = round(stats['bad_debt_count'] / total_records * 100, 1)
        analysis_parts.append(f"坏账占{bad_debt_pct}%，需加强收款管理和风险控制")
    
    if stats['abnormal_count'] > 0:
        abnormal_pct = round(stats['abnormal_count'] / total_records * 100, 1)
        analysis_parts.append(f"异常单占{abnormal_pct}%，建议核查订单处理流程")
    
    # 风险提醒
    if total_amount > 50000:
        analysis_parts.append("⚠️ 损失金额较大，建议重点关注和处理")
    elif total_records > 10:
        analysis_parts.append("⚠️ 异常单量较多，建议分析原因并优化流程")
    
    return "；".join(analysis_parts) + "。"


# ---- 27. 我派出去的客户到院了吗 ----

@mcp.tool()
def my_dispatch_tracking(kf_id: int, days: int = 7) -> str:
    """查询客服派出去的客户到院跟踪情况。返回最近N天的派单列表，包括客户名、医院名、派单时间、医院查看状态、手术状态、消费金额等。
    当用户问"派出去的客户到院了吗"、"我的派单跟踪"、"医院看了吗"、"派单情况"时使用。

    Args:
        kf_id: 客服ID
        days: 查询天数，默认7天
    """
    try:
        # 查询SQL - 通过客服ID关联客户，再查询这些客户的派单记录
        sql = """
            SELECT 
                o.id as order_id,
                o.Client_Id,
                c.ClientName,
                h.company_name as hospital_name,
                o.send_order_time,
                o.view_status,
                o.surgery_status,
                o.consumption_money,
                FROM_UNIXTIME(o.send_order_time) as send_time_str,
                CASE 
                    WHEN o.view_status = 0 THEN '医院未看'
                    WHEN o.view_status = 1 THEN '医院已查看'
                    ELSE '未知状态'
                END as view_status_text,
                CASE 
                    WHEN o.surgery_status = 10 THEN '已完成手术'
                    WHEN o.surgery_status = 0 THEN '未手术'
                    ELSE '其他状态'
                END as surgery_status_text
            FROM un_hospital_order o
            JOIN un_channel_client c ON o.Client_Id = c.Client_Id
            JOIN un_hospital_company h ON o.hospital_id = h.id
            WHERE c.KfId = %s 
            AND o.send_order_time >= UNIX_TIMESTAMP(DATE_SUB(CURDATE(), INTERVAL %s DAY))
            ORDER BY o.send_order_time DESC
        """
        
        dispatch_records = query_qudao_db(sql, (kf_id, days))
        
        if not dispatch_records:
            # 获取客服姓名
            kf_info_sql = "SELECT realname, AdminName FROM un_admin WHERE userid = %s LIMIT 1"
            kf_info_result = query_qudao_db(kf_info_sql, (kf_id,))
            kf_name = "未知客服"
            if kf_info_result:
                kf_name = kf_info_result[0].get('realname') or kf_info_result[0].get('AdminName', '未知客服')
            
            return json.dumps({
                "message": f"最近{days}天没有派单记录",
                "kf_id": kf_id,
                "kf_name": kf_name,
                "days": days,
                "total_dispatches": 0,
                "dispatches": []
            }, ensure_ascii=False, indent=2)
        
        # 处理结果
        result_dispatches = []
        stats = {
            'total_dispatches': len(dispatch_records),
            'viewed_by_hospital': 0,
            'completed_surgery': 0,
            'total_consumption': 0.0,
            'avg_consumption': 0.0
        }
        
        for record in dispatch_records:
            order_id = record.get('order_id')
            client_id = record.get('Client_Id')
            client_name = record.get('ClientName', '未知客户')
            hospital_name = record.get('hospital_name', '未知医院')
            send_order_time = int(record.get('send_order_time', 0) or 0)
            view_status = int(record.get('view_status', 0) or 0)
            surgery_status = int(record.get('surgery_status', 0) or 0)
            consumption_money = float(record.get('consumption_money', 0) or 0)
            
            # 格式化派单时间
            send_time_str = datetime.fromtimestamp(send_order_time).strftime('%Y-%m-%d %H:%M') if send_order_time > 0 else ''
            days_ago = int((time.time() - send_order_time) / 86400) if send_order_time > 0 else 0
            
            # 统计
            if view_status == 1:
                stats['viewed_by_hospital'] += 1
            if surgery_status == 10:
                stats['completed_surgery'] += 1
            stats['total_consumption'] += consumption_money
            
            dispatch_info = {
                "order_id": order_id,
                "client_id": client_id,
                "client_name": client_name,
                "hospital_name": hospital_name,
                "send_order_time": send_order_time,
                "send_time": send_time_str,
                "days_ago": days_ago,
                "view_status": view_status,
                "view_status_text": record.get('view_status_text', '未知状态'),
                "surgery_status": surgery_status,
                "surgery_status_text": record.get('surgery_status_text', '其他状态'),
                "consumption_money": consumption_money
            }
            result_dispatches.append(dispatch_info)
        
        # 计算平均消费
        if stats['completed_surgery'] > 0:
            stats['avg_consumption'] = round(stats['total_consumption'] / stats['completed_surgery'], 2)
        
        # 获取客服姓名
        kf_info_sql = "SELECT realname, AdminName FROM un_admin WHERE userid = %s LIMIT 1"
        kf_info_result = query_qudao_db(kf_info_sql, (kf_id,))
        kf_name = "未知客服"
        if kf_info_result:
            kf_name = kf_info_result[0].get('realname') or kf_info_result[0].get('AdminName', '未知客服')
        
        # 计算查看率和完成率
        view_rate = round(stats['viewed_by_hospital'] / stats['total_dispatches'] * 100, 2) if stats['total_dispatches'] > 0 else 0
        completion_rate = round(stats['completed_surgery'] / stats['total_dispatches'] * 100, 2) if stats['total_dispatches'] > 0 else 0
        
        # 构造结果
        result = {
            "kf_id": kf_id,
            "kf_name": kf_name,
            "period_days": days,
            "summary": {
                "total_dispatches": stats['total_dispatches'],
                "viewed_by_hospital": stats['viewed_by_hospital'],
                "completed_surgery": stats['completed_surgery'],
                "view_rate": view_rate,
                "completion_rate": completion_rate,
                "total_consumption": stats['total_consumption'],
                "avg_consumption": stats['avg_consumption']
            },
            "dispatches": result_dispatches,
            "message": f"{kf_name}最近{days}天共派单{stats['total_dispatches']}个，医院查看{stats['viewed_by_hospital']}个({view_rate}%)，完成手术{stats['completed_surgery']}个({completion_rate}%)",
            "analysis": _generate_dispatch_analysis(stats, days)
        }
        
        return json.dumps(_serialize(result), ensure_ascii=False, indent=2)
        
    except Exception as e:
        logger.exception("my_dispatch_tracking failed")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


def _generate_dispatch_analysis(stats: dict, days: int) -> str:
    """生成派单跟踪分析报告"""
    total_dispatches = stats['total_dispatches']
    viewed_by_hospital = stats['viewed_by_hospital']
    completed_surgery = stats['completed_surgery']
    total_consumption = stats['total_consumption']
    
    if total_dispatches == 0:
        return f"最近{days}天没有派单记录，建议加强客户跟进和医院合作。"
    
    analysis_parts = []
    
    # 查看率分析
    view_rate = round(viewed_by_hospital / total_dispatches * 100, 2) if total_dispatches > 0 else 0
    if view_rate >= 80:
        analysis_parts.append(f"医院查看率{view_rate}%，响应良好")
    elif view_rate >= 60:
        analysis_parts.append(f"医院查看率{view_rate}%，响应一般")
    else:
        analysis_parts.append(f"医院查看率{view_rate}%，需提升医院响应度")
    
    # 转化率分析
    completion_rate = round(completed_surgery / total_dispatches * 100, 2) if total_dispatches > 0 else 0
    if completion_rate >= 50:
        analysis_parts.append(f"手术完成率{completion_rate}%，转化效果优秀")
    elif completion_rate >= 30:
        analysis_parts.append(f"手术完成率{completion_rate}%，转化效果良好")
    elif completion_rate >= 10:
        analysis_parts.append(f"手术完成率{completion_rate}%，转化效果一般")
    else:
        analysis_parts.append(f"手术完成率{completion_rate}%，需优化派单质量")
    
    # 消费分析
    if total_consumption > 0:
        avg_consumption = round(total_consumption / completed_surgery, 2) if completed_surgery > 0 else 0
        if avg_consumption >= 20000:
            analysis_parts.append(f"平均消费{avg_consumption:,.0f}元，客单价优秀")
        elif avg_consumption >= 10000:
            analysis_parts.append(f"平均消费{avg_consumption:,.0f}元，客单价良好")
        else:
            analysis_parts.append(f"平均消费{avg_consumption:,.0f}元，客单价一般")
    
    # 建议
    if view_rate < 60:
        analysis_parts.append("建议与医院沟通改善响应速度")
    if completion_rate < 20:
        analysis_parts.append("建议提升客户质量和医院匹配度")
    
    return "；".join(analysis_parts) + "。"


# ---- 28. 哪些客户派了但没到院 ----

@mcp.tool()
def dispatch_no_arrival(kf_id: int = 0, days: int = 14) -> str:
    """查询派单后超过3天但客户未到院的记录。这是高价值预警——派了但没到意味着可能流失。
    SQL查派单后超过3天但客户client_status未变为10(到院)或11(消费)的记录。
    当用户问"派了没到"、"没到院"、"派单流失"、"哪些客户派了但没来"时使用。

    Args:
        kf_id: 客服ID（0=全部客服）
        days: 统计范围天数，默认14天
    """
    try:
        # 构建查询条件
        conditions = []
        params = []
        
        # 时间条件 - 派单时间在统计范围内，且超过3天
        conditions.append("o.send_order_time >= UNIX_TIMESTAMP(DATE_SUB(CURDATE(), INTERVAL %s DAY))")
        params.append(days)
        conditions.append("o.send_order_time <= UNIX_TIMESTAMP(DATE_SUB(CURDATE(), INTERVAL 3 DAY))")
        
        # 客户状态条件 - 未到院且未成交且非无效客户
        conditions.append("c.client_status NOT IN (10, 11)")  # 10=到院, 11=消费
        conditions.append("c.client_status != 5")  # 排除无意向客户
        
        # 客服条件
        if kf_id > 0:
            conditions.append("c.KfId = %s")
            params.append(kf_id)
        
        where_clause = " AND ".join(conditions)
        
        # 查询SQL
        sql = f"""
            SELECT 
                o.Client_Id,
                c.ClientName,
                c.client_status,
                c.KfId,
                a.realname as kf_name,
                h.company_name as hospital_name,
                o.send_order_time,
                FROM_UNIXTIME(o.send_order_time) as send_time_str,
                DATEDIFF(CURDATE(), FROM_UNIXTIME(o.send_order_time)) as dispatch_days,
                CASE
                    WHEN c.client_status = 0 THEN '待跟进'
                    WHEN c.client_status = 1 THEN '已联系'
                    WHEN c.client_status = 2 THEN '有意向'
                    WHEN c.client_status = 3 THEN '已预约'
                    WHEN c.client_status = 4 THEN '已到院'
                    WHEN c.client_status = 5 THEN '已成交'
                    WHEN c.client_status = 6 THEN '无效'
                    WHEN c.client_status = 7 THEN '流失'
                    WHEN c.client_status = 8 THEN '待回访'
                    WHEN c.client_status = 9 THEN '已预约'
                    WHEN c.client_status = 10 THEN '已到院'
                    WHEN c.client_status = 11 THEN '已消费'
                    WHEN c.client_status = 12 THEN '未联系上'
                    ELSE '未知'
                END as status_name
            FROM un_hospital_order o
            JOIN un_channel_client c ON o.Client_Id = c.Client_Id
            LEFT JOIN un_admin a ON c.KfId = a.userid
            LEFT JOIN un_hospital_company h ON o.hospital_id = h.id
            WHERE {where_clause}
            ORDER BY o.send_order_time DESC
            LIMIT 100
        """
        
        no_arrival_records = query_qudao_db(sql, params)
        
        if not no_arrival_records:
            kf_desc = f"客服ID {kf_id}" if kf_id > 0 else "全部客服"
            return json.dumps({
                "message": f"最近{days}天{kf_desc}没有找到派了但未到院的客户",
                "kf_id": kf_id,
                "days": days,
                "total_records": 0,
                "no_arrival_clients": []
            }, ensure_ascii=False, indent=2)
        
        # 处理结果
        result_clients = []
        stats = {
            'total_no_arrival': len(no_arrival_records),
            'avg_dispatch_days': 0.0,
            'by_status': {},
            'by_hospital': {},
            'by_kf': {}
        }
        
        total_dispatch_days = 0
        
        for record in no_arrival_records:
            client_id = record.get('Client_Id')
            client_name = record.get('ClientName', '未知客户')
            client_status = int(record.get('client_status', 0) or 0)
            status_name = record.get('status_name', '未知')
            kf_id_record = record.get('KfId')
            kf_name = record.get('kf_name', '未知客服')
            hospital_name = record.get('hospital_name', '未知医院')
            send_order_time = int(record.get('send_order_time', 0) or 0)
            dispatch_days = int(record.get('dispatch_days', 0) or 0)
            
            # 格式化派单时间
            send_time_str = datetime.fromtimestamp(send_order_time).strftime('%Y-%m-%d %H:%M') if send_order_time > 0 else ''
            
            # 统计
            total_dispatch_days += dispatch_days
            
            # 按状态统计
            stats['by_status'][status_name] = stats['by_status'].get(status_name, 0) + 1
            
            # 按医院统计
            stats['by_hospital'][hospital_name] = stats['by_hospital'].get(hospital_name, 0) + 1
            
            # 按客服统计
            stats['by_kf'][kf_name] = stats['by_kf'].get(kf_name, 0) + 1
            
            # 判断风险等级
            if dispatch_days >= 10:
                risk_level = "极高风险"
                risk_color = "red"
            elif dispatch_days >= 7:
                risk_level = "高风险"
                risk_color = "orange"
            elif dispatch_days >= 5:
                risk_level = "中风险"
                risk_color = "yellow"
            else:
                risk_level = "低风险"
                risk_color = "blue"
            
            client_info = {
                "client_id": client_id,
                "client_name": client_name,
                "hospital_name": hospital_name,
                "dispatch_days": dispatch_days,
                "current_status": status_name,
                "kf_name": kf_name,
                "send_time": send_time_str,
                "risk_level": risk_level,
                "risk_color": risk_color,
                "dispatch_timestamp": send_order_time
            }
            result_clients.append(client_info)
        
        # 计算平均派单天数
        if stats['total_no_arrival'] > 0:
            stats['avg_dispatch_days'] = round(total_dispatch_days / stats['total_no_arrival'], 2)
        
        # 按派单天数排序（风险从高到低）
        result_clients.sort(key=lambda x: x['dispatch_days'], reverse=True)
        
        # 获取查询的客服信息
        query_kf_name = "全部客服"
        if kf_id > 0:
            kf_info_sql = "SELECT realname, AdminName FROM un_admin WHERE userid = %s LIMIT 1"
            kf_info_result = query_qudao_db(kf_info_sql, (kf_id,))
            if kf_info_result:
                query_kf_name = kf_info_result[0].get('realname') or kf_info_result[0].get('AdminName', f'客服{kf_id}')
        
        # 构造结果
        result = {
            "kf_id": kf_id,
            "kf_name": query_kf_name,
            "period_days": days,
            "summary": {
                "total_no_arrival": stats['total_no_arrival'],
                "avg_dispatch_days": stats['avg_dispatch_days'],
                "high_risk_count": len([c for c in result_clients if c['risk_level'] in ['极高风险', '高风险']]),
                "message": f"最近{days}天{query_kf_name}共有{stats['total_no_arrival']}个客户派了但未到院，平均派单{stats['avg_dispatch_days']:.1f}天"
            },
            "statistics": {
                "by_status": stats['by_status'],
                "by_hospital": dict(list(stats['by_hospital'].items())[:10]),  # 显示前10个医院
                "by_kf": dict(list(stats['by_kf'].items())[:10])  # 显示前10个客服
            },
            "no_arrival_clients": result_clients,
            "analysis": _generate_no_arrival_analysis(stats, days),
            "warning": "⚠️ 这些客户已派单但未到院，可能存在流失风险，建议立即跟进！"
        }
        
        return json.dumps(_serialize(result), ensure_ascii=False, indent=2)
        
    except Exception as e:
        logger.exception("dispatch_no_arrival failed")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


def _generate_no_arrival_analysis(stats: dict, days: int) -> str:
    """生成派了但没到院的分析报告"""
    total_no_arrival = stats['total_no_arrival']
    avg_dispatch_days = stats['avg_dispatch_days']
    
    if total_no_arrival == 0:
        return f"最近{days}天派单客户到院情况良好，没有发现派了但未到院的风险客户。"
    
    analysis_parts = []
    
    # 总体风险评估
    daily_avg = round(total_no_arrival / days, 1)
    analysis_parts.append(f"平均每日{daily_avg}个客户派了但未到院")
    
    # 时效性分析
    if avg_dispatch_days >= 8:
        analysis_parts.append(f"平均派单{avg_dispatch_days:.1f}天未到院，流失风险极高")
    elif avg_dispatch_days >= 6:
        analysis_parts.append(f"平均派单{avg_dispatch_days:.1f}天未到院，流失风险较高")
    else:
        analysis_parts.append(f"平均派单{avg_dispatch_days:.1f}天未到院，尚在正常范围")
    
    # 状态分析
    by_status = stats.get('by_status', {})
    if by_status:
        top_status = max(by_status.items(), key=lambda x: x[1])
        status_pct = round(top_status[1] / total_no_arrival * 100, 1)
        analysis_parts.append(f"主要状态：{top_status[0]}占{status_pct}%")
    
    # 医院分析
    by_hospital = stats.get('by_hospital', {})
    if len(by_hospital) >= 3:
        analysis_parts.append(f"涉及{len(by_hospital)}家医院，问题较分散")
    elif len(by_hospital) == 2:
        analysis_parts.append("问题集中在2家医院，建议重点关注")
    elif len(by_hospital) == 1:
        top_hospital = list(by_hospital.keys())[0]
        analysis_parts.append(f"问题集中在{top_hospital}，建议检查合作质量")
    
    # 客服分析
    by_kf = stats.get('by_kf', {})
    if len(by_kf) >= 5:
        analysis_parts.append("问题涉及多个客服，可能是系统性问题")
    elif len(by_kf) <= 2:
        analysis_parts.append("问题集中在少数客服，建议针对性培训")
    
    # 建议
    if avg_dispatch_days >= 7:
        analysis_parts.append("建议立即电话回访并重新激活客户")
    else:
        analysis_parts.append("建议加强跟进和到院引导")
    
    return "；".join(analysis_parts) + "。"


# ---- 29. 最近重单查询 ----

@mcp.tool()
def recent_duplicates(kf_id: int = 0, days: int = 7) -> str:
    """查询最近重单情况。包括客户重复注册和派单重复两种情况。
    当用户问"重单"、"重复"、"重复注册"、"重复派单"、"最近有重单吗"时使用。

    Args:
        kf_id: 客服ID，大于0时只查该客服的重单，0查全部
        days: 查询天数，默认7天
    """
    try:
        # 查询客户重复注册（un_channel_client_repetition表）
        client_repeat_conditions = []
        client_repeat_params = []
        
        # 时间条件
        client_repeat_conditions.append("r.addtime >= UNIX_TIMESTAMP(DATE_SUB(CURDATE(), INTERVAL %s DAY))")
        client_repeat_params.append(days)
        
        # 客服条件 - 根据关联的客户表的KfId或直接的manager_id
        if kf_id > 0:
            client_repeat_conditions.append("(c.KfId = %s OR r.manager_id = %s)")
            client_repeat_params.extend([kf_id, kf_id])
        
        client_repeat_where = " AND ".join(client_repeat_conditions)
        
        client_repeat_sql = f"""
            SELECT 
                r.id as repeat_id,
                r.client_id,
                c.ClientName,
                c.MobilePhone,
                c.from_type,
                r.repeat_type,
                r.addtime,
                r.is_read,
                r.manager_id,
                a.realname as manager_name,
                FROM_UNIXTIME(r.addtime) as repeat_time_str,
                CASE 
                    WHEN c.from_type = 2 THEN '400电话'
                    WHEN c.from_type = 3 THEN '商务通'
                    WHEN c.from_type = 4 THEN '微信'
                    WHEN c.from_type = 5 THEN 'QQ'
                    WHEN c.from_type = 6 THEN '其他'
                    WHEN c.from_type = 7 THEN 'APP'
                    WHEN c.from_type = 8 THEN '导入'
                    ELSE CONCAT('来源', c.from_type)
                END as source_name,
                CASE
                    WHEN r.repeat_type = 1 THEN '重复注册'
                    WHEN r.repeat_type = 2 THEN '重复留电'
                    WHEN r.repeat_type = 3 THEN '重复咨询'
                    ELSE '其他重复'
                END as repeat_type_name
            FROM un_channel_client_repetition r
            JOIN un_channel_client c ON r.client_id = c.Client_Id
            LEFT JOIN un_admin a ON r.manager_id = a.userid
            WHERE {client_repeat_where}
            ORDER BY r.addtime DESC
            LIMIT 50
        """
        
        client_repeats = query_qudao_db(client_repeat_sql, client_repeat_params)
        
        # 查询派单重复（un_hospital_contact_info_repeat表）
        dispatch_repeat_conditions = []
        dispatch_repeat_params = []
        
        # 时间条件
        dispatch_repeat_conditions.append("hr.addtime >= UNIX_TIMESTAMP(DATE_SUB(CURDATE(), INTERVAL %s DAY))")
        dispatch_repeat_params.append(days)
        
        # 客服条件 - 通过客户关联
        if kf_id > 0:
            dispatch_repeat_conditions.append("EXISTS (SELECT 1 FROM un_channel_client c WHERE c.Client_Id = hr.client_id AND c.KfId = %s)")
            dispatch_repeat_params.append(kf_id)
        
        dispatch_repeat_where = " AND ".join(dispatch_repeat_conditions)
        
        dispatch_repeat_sql = f"""
            SELECT 
                hr.id as repeat_id,
                hr.client_id,
                c.ClientName,
                c.MobilePhone,
                hr.hospital_id,
                h.company_name as hospital_name,
                hr.addtime,
                hr.is_read,
                FROM_UNIXTIME(hr.addtime) as repeat_time_str,
                '派单重复' as repeat_type_name
            FROM un_hospital_contact_info_repeat hr
            JOIN un_channel_client c ON hr.client_id = c.Client_Id
            LEFT JOIN un_hospital_company h ON hr.hospital_id = h.id
            WHERE {dispatch_repeat_where}
            ORDER BY hr.addtime DESC
            LIMIT 50
        """
        
        dispatch_repeats = query_qudao_db(dispatch_repeat_sql, dispatch_repeat_params)
        
        # 处理客户重复注册结果
        client_repeat_list = []
        client_unread_count = 0
        
        for repeat in client_repeats:
            client_name = repeat.get('ClientName', '未知客户')
            phone = _mask_phone(repeat.get('MobilePhone', ''))
            source_name = repeat.get('source_name', '未知来源')
            repeat_type_name = repeat.get('repeat_type_name', '其他重复')
            repeat_time_str = repeat.get('repeat_time_str', '')
            is_read = int(repeat.get('is_read', 0) or 0)
            manager_name = repeat.get('manager_name', '未知')
            
            if is_read == 0:
                client_unread_count += 1
                
            client_repeat_info = {
                "repeat_id": repeat.get('repeat_id'),
                "client_id": repeat.get('client_id'),
                "client_name": client_name,
                "phone": phone,
                "source": source_name,
                "repeat_type": repeat_type_name,
                "repeat_time": repeat_time_str,
                "is_read": bool(is_read),
                "manager_name": manager_name,
                "days_ago": int((time.time() - int(repeat.get('addtime', 0) or 0)) / 86400)
            }
            client_repeat_list.append(client_repeat_info)
        
        # 处理派单重复结果
        dispatch_repeat_list = []
        dispatch_unread_count = 0
        
        for repeat in dispatch_repeats:
            client_name = repeat.get('ClientName', '未知客户')
            phone = _mask_phone(repeat.get('MobilePhone', ''))
            hospital_name = repeat.get('hospital_name', '未知医院')
            repeat_time_str = repeat.get('repeat_time_str', '')
            is_read = int(repeat.get('is_read', 0) or 0)
            
            if is_read == 0:
                dispatch_unread_count += 1
                
            dispatch_repeat_info = {
                "repeat_id": repeat.get('repeat_id'),
                "client_id": repeat.get('client_id'),
                "client_name": client_name,
                "phone": phone,
                "hospital_name": hospital_name,
                "repeat_type": "派单重复",
                "repeat_time": repeat_time_str,
                "is_read": bool(is_read),
                "days_ago": int((time.time() - int(repeat.get('addtime', 0) or 0)) / 86400)
            }
            dispatch_repeat_list.append(dispatch_repeat_info)
        
        # 获取查询的客服信息
        query_kf_name = "全部客服"
        if kf_id > 0:
            kf_info_sql = "SELECT realname, AdminName FROM un_admin WHERE userid = %s LIMIT 1"
            kf_info_result = query_qudao_db(kf_info_sql, (kf_id,))
            if kf_info_result:
                query_kf_name = kf_info_result[0].get('realname') or kf_info_result[0].get('AdminName', f'客服{kf_id}')
        
        # 汇总统计
        total_client_repeats = len(client_repeat_list)
        total_dispatch_repeats = len(dispatch_repeat_list)
        total_repeats = total_client_repeats + total_dispatch_repeats
        total_unread = client_unread_count + dispatch_unread_count
        
        # 构造结果
        result = {
            "kf_id": kf_id,
            "kf_name": query_kf_name,
            "period_days": days,
            "summary": {
                "total_repeats": total_repeats,
                "client_repeats": total_client_repeats,
                "dispatch_repeats": total_dispatch_repeats,
                "total_unread": total_unread,
                "message": f"最近{days}天{query_kf_name}共有{total_repeats}个重单记录，其中{total_unread}个未处理"
            },
            "client_repetitions": client_repeat_list,
            "dispatch_repetitions": dispatch_repeat_list,
            "analysis": _generate_duplicate_analysis(total_repeats, client_unread_count, dispatch_unread_count, days),
            "warning": "⚠️ 请及时处理未读的重单记录，避免客户体验下降！" if total_unread > 0 else "✅ 所有重单记录已处理完成。"
        }
        
        return json.dumps(_serialize(result), ensure_ascii=False, indent=2)
        
    except Exception as e:
        logger.exception("recent_duplicates failed")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


def _generate_duplicate_analysis(total_repeats: int, client_unread: int, dispatch_unread: int, days: int) -> str:
    """生成重单分析报告"""
    if total_repeats == 0:
        return f"最近{days}天没有重单记录，客户管理质量良好。"
    
    analysis_parts = []
    
    # 总体情况
    daily_avg = round(total_repeats / days, 1)
    analysis_parts.append(f"平均每日{daily_avg}个重单")
    
    # 处理情况分析
    total_unread = client_unread + dispatch_unread
    if total_unread > 0:
        unread_pct = round(total_unread / total_repeats * 100, 1)
        analysis_parts.append(f"{unread_pct}%的重单未处理，需要及时跟进")
        
        if client_unread > 0:
            analysis_parts.append(f"客户重复注册{client_unread}个待处理")
        if dispatch_unread > 0:
            analysis_parts.append(f"派单重复{dispatch_unread}个待处理")
    else:
        analysis_parts.append("所有重单已处理，管理规范")
    
    # 建议
    if total_unread >= 3:
        analysis_parts.append("建议立即处理未读重单，优化客户体验")
    elif total_repeats > days * 2:  # 平均每天超过2个重单
        analysis_parts.append("重单频率较高，建议检查客户录入流程")
    else:
        analysis_parts.append("重单控制在合理范围内")
    
    return "；".join(analysis_parts) + "。"


# ---- 30. 今天有哪些韩国客户要接 ----

@mcp.tool()
def korean_today_schedule(staff_id: int = 0) -> str:
    """查询今天需要接待的韩国客户行程。包括今日预约行程和今日到达航班的客户。
    当用户问"今天接谁"、"今天行程"、"今天手术"、"今天有哪些韩国客户"时使用。

    Args:
        staff_id: 员工ID，0表示查询所有员工的行程
    """
    try:
        # 构建查询条件
        conditions = []
        params = []
        
        # 今日行程条件：schedule_date=CURDATE()
        date_condition = "s.schedule_date = CURDATE()"
        
        # 今日航班条件：jobtype=1 AND flight_landingtime BETWEEN 今天开始和结束
        flight_condition = "(s.jobtype = 1 AND s.flight_landingtime BETWEEN UNIX_TIMESTAMP(CURDATE()) AND UNIX_TIMESTAMP(CURDATE() + INTERVAL 1 DAY))"
        
        # 组合条件
        time_condition = f"({date_condition} OR {flight_condition})"
        conditions.append(time_condition)
        
        # 员工条件
        if staff_id > 0:
            conditions.append("s.staff_id = %s")
            params.append(staff_id)
        
        where_clause = " AND ".join(conditions)
        
        # 查询SQL
        sql = f"""
            SELECT 
                s.id as schedule_id,
                s.client_id,
                c.ClientName as client_name,
                c.MobilePhone,
                s.schedule_date,
                s.schedule_time,
                s.jobtype,
                s.hospital_id,
                h.company_name as hospital_name,
                s.flight_number,
                s.flight_landingtime,
                s.staff_id,
                a.realname as staff_name,
                s.remark,
                DATE(s.schedule_date) as schedule_date_str,
                TIME(s.schedule_time) as schedule_time_str,
                FROM_UNIXTIME(s.flight_landingtime) as landing_time_str,
                CASE 
                    WHEN s.jobtype = 1 THEN '接机'
                    WHEN s.jobtype = 2 THEN '送机'  
                    WHEN s.jobtype = 3 THEN '医院陪诊'
                    WHEN s.jobtype = 4 THEN '手术'
                    WHEN s.jobtype = 5 THEN '复查'
                    WHEN s.jobtype = 6 THEN '咨询'
                    WHEN s.jobtype = 7 THEN '其他'
                    ELSE '未知类型'
                END as job_type_name,
                CASE
                    WHEN s.schedule_date = CURDATE() AND s.jobtype != 1 THEN '今日预约'
                    WHEN s.jobtype = 1 AND s.flight_landingtime BETWEEN UNIX_TIMESTAMP(CURDATE()) AND UNIX_TIMESTAMP(CURDATE() + INTERVAL 1 DAY) THEN '今日到达'
                    ELSE '其他'
                END as schedule_type
            FROM un_schedule s
            LEFT JOIN un_channel_client c ON s.client_id = c.Client_Id
            LEFT JOIN un_hospital_company h ON s.hospital_id = h.id
            LEFT JOIN un_admin a ON s.staff_id = a.userid
            WHERE {where_clause}
            ORDER BY 
                CASE WHEN s.jobtype = 1 THEN s.flight_landingtime ELSE UNIX_TIMESTAMP(CONCAT(s.schedule_date, ' ', s.schedule_time)) END ASC
        """
        
        schedules = query_qudao_db(sql, params)
        
        if not schedules:
            staff_desc = f"员工ID {staff_id}" if staff_id > 0 else "所有员工"
            return json.dumps({
                "message": f"今天没有找到{staff_desc}的韩国客户接待行程",
                "staff_id": staff_id,
                "date": datetime.now().strftime('%Y-%m-%d'),
                "total_schedules": 0,
                "schedules": []
            }, ensure_ascii=False, indent=2)
        
        # 处理结果
        result_schedules = []
        stats = {
            'total_schedules': len(schedules),
            'airport_pickups': 0,
            'hospital_visits': 0,
            'surgeries': 0,
            'consultations': 0,
            'other_activities': 0
        }
        
        for schedule in schedules:
            schedule_id = schedule.get('schedule_id')
            client_id = schedule.get('client_id')
            client_name = schedule.get('client_name', '未知客户')
            phone = _mask_phone(schedule.get('MobilePhone', ''))
            jobtype = int(schedule.get('jobtype', 0) or 0)
            job_type_name = schedule.get('job_type_name', '未知类型')
            hospital_name = schedule.get('hospital_name', '未指定医院')
            schedule_date_str = schedule.get('schedule_date_str', '')
            schedule_time_str = schedule.get('schedule_time_str', '')
            flight_number = schedule.get('flight_number', '')
            landing_time_str = schedule.get('landing_time_str', '')
            staff_id_record = schedule.get('staff_id')
            staff_name = schedule.get('staff_name', '未指定员工')
            remark = schedule.get('remark', '')
            schedule_type = schedule.get('schedule_type', '其他')
            
            # 统计
            if jobtype == 1:  # 接机
                stats['airport_pickups'] += 1
            elif jobtype == 3:  # 医院陪诊
                stats['hospital_visits'] += 1
            elif jobtype == 4:  # 手术
                stats['surgeries'] += 1
            elif jobtype == 6:  # 咨询
                stats['consultations'] += 1
            else:
                stats['other_activities'] += 1
            
            # 格式化时间信息
            time_info = ""
            if jobtype == 1 and landing_time_str:  # 接机显示航班时间
                time_info = f"{landing_time_str} ({flight_number})" if flight_number else landing_time_str
            elif schedule_time_str:  # 其他活动显示预约时间
                time_info = f"{schedule_date_str} {schedule_time_str}"
            else:
                time_info = schedule_date_str
            
            schedule_info = {
                "schedule_id": schedule_id,
                "client_id": client_id,
                "client_name": client_name,
                "phone": phone,
                "job_type": job_type_name,
                "hospital_name": hospital_name,
                "time_info": time_info,
                "schedule_type": schedule_type,
                "staff_name": staff_name,
                "flight_info": f"{flight_number} {landing_time_str}" if flight_number else "",
                "remark": remark
            }
            result_schedules.append(schedule_info)
        
        # 获取查询的员工信息
        query_staff_name = "所有员工"
        if staff_id > 0:
            staff_info_sql = "SELECT realname, AdminName FROM un_admin WHERE userid = %s LIMIT 1"
            staff_info_result = query_qudao_db(staff_info_sql, (staff_id,))
            if staff_info_result:
                query_staff_name = staff_info_result[0].get('realname') or staff_info_result[0].get('AdminName', f'员工{staff_id}')
        
        # 构造结果
        result = {
            "staff_id": staff_id,
            "staff_name": query_staff_name,
            "date": datetime.now().strftime('%Y-%m-%d'),
            "summary": {
                "total_schedules": stats['total_schedules'],
                "airport_pickups": stats['airport_pickups'],
                "hospital_visits": stats['hospital_visits'],
                "surgeries": stats['surgeries'],
                "consultations": stats['consultations'],
                "other_activities": stats['other_activities'],
                "message": f"今天{query_staff_name}共有{stats['total_schedules']}项韩国客户接待安排"
            },
            "schedules": result_schedules,
            "analysis": _generate_korean_schedule_analysis(stats),
            "note": "包含今日预约行程和今日到达航班的韩国客户"
        }
        
        return json.dumps(_serialize(result), ensure_ascii=False, indent=2)
        
    except Exception as e:
        logger.exception("korean_today_schedule failed")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


def _generate_korean_schedule_analysis(stats: dict) -> str:
    """生成韩国客户今日行程分析报告"""
    total_schedules = stats['total_schedules']
    
    if total_schedules == 0:
        return "今天没有韩国客户接待安排，可以专注于其他工作。"
    
    analysis_parts = []
    
    # 工作量分析
    if total_schedules >= 5:
        analysis_parts.append(f"今日安排较满({total_schedules}项)，需要合理安排时间")
    elif total_schedules >= 3:
        analysis_parts.append(f"今日工作量适中({total_schedules}项)")
    else:
        analysis_parts.append(f"今日安排相对较少({total_schedules}项)")
    
    # 活动类型分析
    activity_summary = []
    if stats['airport_pickups'] > 0:
        activity_summary.append(f"接机{stats['airport_pickups']}次")
    if stats['surgeries'] > 0:
        activity_summary.append(f"手术{stats['surgeries']}台")
    if stats['hospital_visits'] > 0:
        activity_summary.append(f"医院陪诊{stats['hospital_visits']}次")
    if stats['consultations'] > 0:
        activity_summary.append(f"咨询{stats['consultations']}次")
    if stats['other_activities'] > 0:
        activity_summary.append(f"其他{stats['other_activities']}项")
    
    if activity_summary:
        analysis_parts.append("，".join(activity_summary))
    
    # 重点提醒
    if stats['airport_pickups'] > 0:
        analysis_parts.append("注意航班时间，提前到达机场")
    if stats['surgeries'] > 0:
        analysis_parts.append("手术安排需确认医院和医生")
    
    return "；".join(analysis_parts) + "。"


# ---- 31. 客服业绩排名 ----

@mcp.tool()
def kf_performance_ranking(days: int = 30, department_name: str = "", metric: str = "deal_amount") -> str:
    """查询客服业绩排名。按成交金额、成交数量或平均客单价等指标排序。
    当用户问"客服排名"、"谁做得好"、"业绩排行"、"TOP客服"时使用。

    Args:
        days: 统计天数，默认30天
        department_name: 部门名称（可选），如 TEG/BCG/CDG 等，留空表示全部部门
        metric: 排序指标，可选值：deal_amount(成交金额)/deal_count(成交数量)/avg_amount(平均客单价)，默认deal_amount
    """
    try:
        # 验证metric参数
        valid_metrics = ["deal_amount", "deal_count", "avg_amount"]
        if metric not in valid_metrics:
            return json.dumps({
                "error": f"无效的排序指标: {metric}，支持: {', '.join(valid_metrics)}"
            }, ensure_ascii=False)

        # 构建查询条件
        conditions = []
        params = []
        
        # 时间条件
        conditions.append("p.checktime >= UNIX_TIMESTAMP(DATE_SUB(CURDATE(), INTERVAL %s DAY))")
        params.append(days)
        
        # 成交状态条件
        conditions.append("p.status = 1")
        
        # 部门条件
        if department_name:
            # 从 DEPARTMENT_MAP 导入并获取部门信息
            if department_name not in DEPARTMENT_MAP:
                return json.dumps({
                    "error": f"未知部门: {department_name}，支持: {', '.join(DEPARTMENT_MAP.keys())}"
                }, ensure_ascii=False)
            
            dept_id, dept_full_name = DEPARTMENT_MAP[department_name]
            
            # 获取该部门的渠道channel_ids
            channel_ids = _get_department_channel_ids(dept_id)
            if not channel_ids:
                return json.dumps({
                    "error": f"部门 {department_name} 没有关联的渠道"
                }, ensure_ascii=False)
            
            # 添加渠道过滤条件
            channel_ids_str = ",".join(str(cid) for cid in channel_ids)
            conditions.append(f"p.from_type IN ({channel_ids_str})")
        
        where_clause = " AND ".join(conditions)

        # 构建排序字段
        order_field_map = {
            "deal_amount": "deal_amount DESC",
            "deal_count": "deal_count DESC", 
            "avg_amount": "avg_amount DESC"
        }
        order_clause = order_field_map[metric]

        # 主查询SQL
        sql = f"""
            SELECT 
                p.KfId,
                a.realname as kf_name,
                a.AdminName as kf_username,
                COUNT(*) as deal_count,
                SUM(p.true_number) as deal_amount,
                AVG(p.true_number) as avg_amount
            FROM un_channel_paylist p
            LEFT JOIN un_admin a ON p.KfId = a.userid
            WHERE {where_clause}
            GROUP BY p.KfId, a.realname, a.AdminName
            HAVING deal_count > 0
            ORDER BY {order_clause}
            LIMIT 50
        """
        
        results = query_qudao_db(sql, params)
        
        if not results:
            dept_desc = f"部门 {department_name}" if department_name else "全公司"
            return json.dumps({
                "message": f"最近{days}天{dept_desc}没有客服成交记录",
                "department_name": department_name,
                "days": days,
                "metric": metric,
                "total_kf": 0,
                "rankings": []
            }, ensure_ascii=False, indent=2)

        # 处理结果，生成排名列表
        rankings = []
        for i, result in enumerate(results, 1):
            kf_id = result.get('KfId')
            kf_name = result.get('kf_name') or result.get('kf_username') or f'客服{kf_id}'
            deal_count = int(result.get('deal_count', 0) or 0)
            deal_amount = float(result.get('deal_amount', 0) or 0)
            avg_amount = float(result.get('avg_amount', 0) or 0)
            
            ranking_info = {
                "rank": i,
                "kf_id": kf_id,
                "kf_name": kf_name,
                "deal_count": deal_count,
                "deal_amount": deal_amount,
                "avg_amount": round(avg_amount, 2)
            }
            rankings.append(ranking_info)

        # 统计汇总信息
        total_kf = len(rankings)
        total_deals = sum(r['deal_count'] for r in rankings)
        total_amount = sum(r['deal_amount'] for r in rankings)
        overall_avg = round(total_amount / total_deals, 2) if total_deals > 0 else 0

        # 找出表现最好的客服
        top_performer = rankings[0] if rankings else None
        
        # 生成排名指标说明
        metric_desc_map = {
            "deal_amount": "成交金额",
            "deal_count": "成交数量",
            "avg_amount": "平均客单价"
        }
        metric_desc = metric_desc_map.get(metric, metric)

        # 构建返回结果
        result = {
            "department_name": department_name,
            "department_desc": f"部门 {department_name}" if department_name else "全公司",
            "period_days": days,
            "ranking_metric": metric,
            "ranking_metric_desc": metric_desc,
            "summary": {
                "total_kf": total_kf,
                "total_deals": total_deals,
                "total_amount": total_amount,
                "overall_avg_amount": overall_avg,
                "top_performer": {
                    "name": top_performer['kf_name'] if top_performer else None,
                    "value": top_performer.get(metric, 0) if top_performer else 0
                },
                "message": f"最近{days}天{f'部门{department_name}' if department_name else '全公司'}共{total_kf}个客服参与排名，按{metric_desc}排序"
            },
            "rankings": rankings,
            "analysis": _generate_kf_ranking_analysis(rankings, metric, days, department_name or "全公司")
        }
        
        return json.dumps(_serialize(result), ensure_ascii=False, indent=2)
        
    except Exception as e:
        logger.exception("kf_performance_ranking failed")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


def _generate_kf_ranking_analysis(rankings: list, metric: str, days: int, scope: str) -> str:
    """生成客服业绩排名分析报告"""
    if not rankings:
        return f"{scope}最近{days}天没有客服成交记录。"
    
    total_kf = len(rankings)
    metric_desc_map = {
        "deal_amount": "成交金额", 
        "deal_count": "成交数量",
        "avg_amount": "平均客单价"
    }
    metric_desc = metric_desc_map.get(metric, metric)
    
    analysis_parts = []
    
    # 总体概况
    analysis_parts.append(f"{scope}参与排名客服{total_kf}人，按{metric_desc}排序")
    
    # 冠军表现
    top_performer = rankings[0]
    top_value = top_performer.get(metric, 0)
    if metric == "deal_amount":
        analysis_parts.append(f"冠军{top_performer['kf_name']}成交金额¥{top_value:,.2f}")
    elif metric == "deal_count": 
        analysis_parts.append(f"冠军{top_performer['kf_name']}成交{top_value}单")
    else:  # avg_amount
        analysis_parts.append(f"冠军{top_performer['kf_name']}平均客单价¥{top_value:,.2f}")
    
    # 前三名表现
    if total_kf >= 3:
        top3_names = [r['kf_name'] for r in rankings[:3]]
        analysis_parts.append(f"前三名：{', '.join(top3_names)}")
    
    # 分层分析
    if total_kf >= 10:
        # 计算分位数
        values = [r.get(metric, 0) for r in rankings]
        avg_value = sum(values) / len(values)
        
        above_avg = len([v for v in values if v > avg_value])
        below_avg = total_kf - above_avg
        
        analysis_parts.append(f"超过平均水平{above_avg}人，低于平均水平{below_avg}人")
        
        # 差距分析
        if len(values) >= 2:
            gap_ratio = values[0] / values[-1] if values[-1] > 0 else float('inf')
            if gap_ratio >= 5:
                analysis_parts.append("第1名和最后1名差距较大，建议关注后进客服")
            elif gap_ratio >= 3:
                analysis_parts.append("客服间存在一定差距")
            else:
                analysis_parts.append("客服间表现相对均衡")
    elif total_kf >= 5:
        analysis_parts.append("团队规模适中，建议加强经验分享")
    else:
        analysis_parts.append("团队规模较小，可关注个体表现")
    
    # 建议
    if metric == "deal_amount":
        analysis_parts.append("建议优秀客服分享成交技巧")
    elif metric == "deal_count":
        analysis_parts.append("建议关注跟进效率和客户质量")
    else:  # avg_amount
        analysis_parts.append("建议学习客单价提升方法")
    
    return "；".join(analysis_parts) + "。"


# ---- 32. 客户重单检查 ----

@mcp.tool()
def check_client_duplicate(client_id: int) -> str:
    """客户重单检查。检查客户是否为重单，包括：
    1. un_channel_client_repetition 表中的重单记录
    2. un_channel_client 表中相同手机号的其他客户
    3. un_hospital_contact_info_repeat 表中的派单重复
    当用户问"是不是重单"、"重复了吗"、"有没有重复"时使用。

    Args:
        client_id: 客户ID
    """
    try:
        result = {
            "client_id": client_id,
            "is_duplicate": False,
            "duplicate_records": [],
            "same_phone_clients": [],
            "dispatch_duplicates": []
        }

        # 1. 检查 un_channel_client_repetition 表中的重单记录
        repetition_sql = """
            SELECT 
                r.id,
                r.client_id,
                r.repetition_client_id,
                r.create_time,
                c1.ClientName as original_name,
                c1.MobilePhone as original_phone,
                c2.ClientName as repeat_name,
                c2.MobilePhone as repeat_phone,
                FROM_UNIXTIME(r.create_time) as create_time_str
            FROM un_channel_client_repetition r
            LEFT JOIN un_channel_client c1 ON r.client_id = c1.Client_Id
            LEFT JOIN un_channel_client c2 ON r.repetition_client_id = c2.Client_Id
            WHERE r.client_id = %s OR r.repetition_client_id = %s
        """
        
        repetition_records = query_qudao_db(repetition_sql, (client_id, client_id))
        
        if repetition_records:
            result["is_duplicate"] = True
            for record in repetition_records:
                duplicate_record = {
                    "id": record.get('id'),
                    "original_client_id": record.get('client_id'),
                    "repeat_client_id": record.get('repetition_client_id'),
                    "original_name": record.get('original_name', ''),
                    "repeat_name": record.get('repeat_name', ''),
                    "original_phone": _mask_phone(record.get('original_phone', '')),
                    "repeat_phone": _mask_phone(record.get('repeat_phone', '')),
                    "create_time": record.get('create_time_str', ''),
                    "type": "系统重单记录"
                }
                result["duplicate_records"].append(duplicate_record)

        # 2. 检查 un_channel_client 表中相同手机号的其他客户
        # 先获取当前客户的手机号
        client_info_sql = """
            SELECT ClientName, MobilePhone
            FROM un_channel_client 
            WHERE Client_Id = %s
        """
        client_info_result = query_qudao_db(client_info_sql, (client_id,))
        
        if client_info_result:
            current_phone = client_info_result[0].get('MobilePhone', '')
            current_name = client_info_result[0].get('ClientName', '')
            
            if current_phone:
                same_phone_sql = """
                    SELECT 
                        Client_Id,
                        ClientName,
                        MobilePhone,
                        client_status,
                        RegisterTime,
                        KfId,
                        a.realname as kf_name,
                        CASE
                            WHEN client_status = 0 THEN '待跟进'
                            WHEN client_status = 1 THEN '已联系'
                            WHEN client_status = 2 THEN '有意向'
                            WHEN client_status = 3 THEN '已预约'
                            WHEN client_status = 4 THEN '已到院'
                            WHEN client_status = 5 THEN '已成交'
                            WHEN client_status = 6 THEN '无效'
                            WHEN client_status = 7 THEN '流失'
                            ELSE '未知'
                        END as status_name
                    FROM un_channel_client c
                    LEFT JOIN un_admin a ON c.KfId = a.userid
                    WHERE c.MobilePhone = %s AND c.Client_Id != %s
                    ORDER BY c.RegisterTime DESC
                """
                
                same_phone_results = query_qudao_db(same_phone_sql, (current_phone, client_id))
                
                if same_phone_results:
                    result["is_duplicate"] = True
                    for record in same_phone_results:
                        same_phone_client = {
                            "client_id": record.get('Client_Id'),
                            "name": record.get('ClientName', ''),
                            "phone": _mask_phone(record.get('MobilePhone', '')),
                            "status": record.get('status_name', ''),
                            "kf_name": record.get('kf_name', ''),
                            "register_time": datetime.fromtimestamp(record.get('RegisterTime', 0)).strftime('%Y-%m-%d %H:%M') if record.get('RegisterTime') else '',
                            "type": "相同手机号客户"
                        }
                        result["same_phone_clients"].append(same_phone_client)

        # 3. 检查 un_hospital_contact_info_repeat 表中的派单重复
        dispatch_repeat_sql = """
            SELECT 
                r.id,
                r.client_id,
                r.hospital_id,
                r.contact_info,
                r.create_time,
                h.company_name as hospital_name,
                c.ClientName,
                FROM_UNIXTIME(r.create_time) as create_time_str
            FROM un_hospital_contact_info_repeat r
            LEFT JOIN un_hospital_company h ON r.hospital_id = h.id
            LEFT JOIN un_channel_client c ON r.client_id = c.Client_Id
            WHERE r.client_id = %s
        """
        
        dispatch_repeat_results = query_qudao_db(dispatch_repeat_sql, (client_id,))
        
        if dispatch_repeat_results:
            result["is_duplicate"] = True
            for record in dispatch_repeat_results:
                dispatch_duplicate = {
                    "id": record.get('id'),
                    "client_id": record.get('client_id'),
                    "client_name": record.get('ClientName', ''),
                    "hospital_id": record.get('hospital_id'),
                    "hospital_name": record.get('hospital_name', ''),
                    "contact_info": _mask_phone(record.get('contact_info', '')),
                    "create_time": record.get('create_time_str', ''),
                    "type": "派单重复记录"
                }
                result["dispatch_duplicates"].append(dispatch_duplicate)

        # 构建分析摘要
        total_duplicates = len(result["duplicate_records"]) + len(result["same_phone_clients"]) + len(result["dispatch_duplicates"])
        
        if result["is_duplicate"]:
            summary = f"检测到重单！共发现{total_duplicates}个重复记录"
            if result["duplicate_records"]:
                summary += f"，包含{len(result['duplicate_records'])}个系统重单记录"
            if result["same_phone_clients"]:
                summary += f"，{len(result['same_phone_clients'])}个相同手机号客户"
            if result["dispatch_duplicates"]:
                summary += f"，{len(result['dispatch_duplicates'])}个派单重复记录"
        else:
            summary = "未检测到重单，该客户是首次录入"

        result["summary"] = summary
        result["total_duplicate_count"] = total_duplicates
        result["check_time"] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        return json.dumps(_serialize(result), ensure_ascii=False, indent=2)
        
    except Exception as e:
        logger.exception("check_client_duplicate failed")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


# ---- 31. 韩国行程查询 ----

@mcp.tool()
def korean_schedule(client_id: int) -> str:
    """查询客户韩国行程安排。仅对 client_region=4 的韩国客户有效。
    返回行程时间线，包括接机、术前咨询、手术、术后恢复、送机等所有环节。
    对于接机和送机环节还会返回航班起降时间。
    当用户问"韩国行程"、"行程安排"、"接机"、"手术安排"时使用。

    Args:
        client_id: 客户ID
    """
    try:
        # 1. 首先检查客户是否为韩国业务线客户
        customer = query_customer_info(client_id)
        if not customer:
            return json.dumps({"error": f"客户 {client_id} 不存在"}, ensure_ascii=False)

        client_region = int(customer.get("client_region", 0) or 0)
        if client_region != 4:
            return json.dumps({
                "error": f"客户 {client_id} 不是韩国业务线客户（client_region={client_region}），无法查询韩国行程",
                "client_name": customer.get('ClientName', ''),
                "client_region": client_region
            }, ensure_ascii=False)

        # 2. 查询行程安排
        schedule_sql = """
            SELECT 
                s.id,
                s.custom_id,
                s.schedule_date,
                s.jobtype,
                s.hospital_name,
                s.appointment_time,
                s.responsible_person,
                s.need_car,
                s.flight_takeofftime,
                s.flight_landingtime,
                s.remarks,
                CASE 
                    WHEN s.jobtype = 1 THEN '接机'
                    WHEN s.jobtype = 2 THEN '术前咨询'
                    WHEN s.jobtype = 3 THEN '手术'
                    WHEN s.jobtype = 4 THEN '术后恢复'
                    WHEN s.jobtype = 5 THEN '送机'
                    ELSE CONCAT('类型', s.jobtype)
                END as jobtype_name,
                CASE 
                    WHEN s.need_car = 1 THEN '需要用车'
                    WHEN s.need_car = 0 THEN '不需要用车'
                    ELSE '未知'
                END as need_car_text
            FROM un_schedule s
            WHERE s.custom_id = %s
            ORDER BY s.schedule_date ASC, s.jobtype ASC
        """
        
        schedule_records = query_qudao_db(schedule_sql, (client_id,))
        
        # 3. 查询护照信息
        passport_sql = """
            SELECT 
                passport_number,
                passport_expiry,
                nationality,
                visa_status,
                visa_expiry
            FROM un_custom_archives
            WHERE custom_id = %s
            LIMIT 1
        """
        
        passport_result = query_qudao_db(passport_sql, (client_id,))
        passport_info = passport_result[0] if passport_result else {}

        # 4. 处理行程数据
        schedule_timeline = []
        stats = {
            'total_items': len(schedule_records),
            'pickup_count': 0,
            'consultation_count': 0,
            'surgery_count': 0,
            'recovery_count': 0,
            'dropoff_count': 0,
            'need_car_count': 0,
            'has_flight_info': 0
        }
        
        for record in schedule_records:
            schedule_date = record.get('schedule_date', '')
            jobtype = int(record.get('jobtype', 0) or 0)
            jobtype_name = record.get('jobtype_name', '未知类型')
            hospital_name = record.get('hospital_name', '')
            appointment_time = record.get('appointment_time', '')
            responsible_person = record.get('responsible_person', '')
            need_car = int(record.get('need_car', 0) or 0)
            need_car_text = record.get('need_car_text', '未知')
            flight_takeofftime = record.get('flight_takeofftime', '')
            flight_landingtime = record.get('flight_landingtime', '')
            remarks = record.get('remarks', '')
            
            # 统计
            if jobtype == 1:
                stats['pickup_count'] += 1
            elif jobtype == 2:
                stats['consultation_count'] += 1
            elif jobtype == 3:
                stats['surgery_count'] += 1
            elif jobtype == 4:
                stats['recovery_count'] += 1
            elif jobtype == 5:
                stats['dropoff_count'] += 1
                
            if need_car == 1:
                stats['need_car_count'] += 1
                
            if flight_takeofftime or flight_landingtime:
                stats['has_flight_info'] += 1
            
            # 构建行程节点
            schedule_item = {
                "id": record.get('id'),
                "date": schedule_date,
                "jobtype": jobtype,
                "jobtype_name": jobtype_name,
                "hospital_name": hospital_name,
                "appointment_time": appointment_time,
                "responsible_person": responsible_person,
                "need_car": bool(need_car),
                "need_car_text": need_car_text,
                "remarks": remarks
            }
            
            # 对于接机(1)和送机(5)，额外返回航班信息
            if jobtype in [1, 5]:
                schedule_item.update({
                    "flight_takeofftime": flight_takeofftime,
                    "flight_landingtime": flight_landingtime,
                    "flight_info": f"起飞:{flight_takeofftime}, 降落:{flight_landingtime}" if flight_takeofftime or flight_landingtime else "无航班信息"
                })
            
            schedule_timeline.append(schedule_item)
        
        # 5. 判断行程完整性
        completeness_score = 0
        missing_items = []
        
        if stats['pickup_count'] > 0:
            completeness_score += 20
        else:
            missing_items.append("接机安排")
            
        if stats['consultation_count'] > 0:
            completeness_score += 20
        else:
            missing_items.append("术前咨询")
            
        if stats['surgery_count'] > 0:
            completeness_score += 30
        else:
            missing_items.append("手术安排")
            
        if stats['recovery_count'] > 0:
            completeness_score += 15
        else:
            missing_items.append("术后恢复")
            
        if stats['dropoff_count'] > 0:
            completeness_score += 15
        else:
            missing_items.append("送机安排")
        
        # 6. 构造返回结果
        result = {
            "client_id": client_id,
            "client_info": {
                "name": customer.get('ClientName', ''),
                "phone": _mask_phone(customer.get('MobilePhone', '')),
                "client_region": client_region,
                "intention_project": {
                    1: '种植牙', 2: '矫正', 3: '美白', 4: '洁牙', 5: '补牙',
                    6: '拔牙', 7: '口腔检查', 8: '牙周治疗', 9: '根管治疗', 10: '烤瓷牙'
                }.get(int(customer.get('PlasticsIntention', 0) or 0), '其他')
            },
            "passport_info": {
                "passport_number": _mask_phone(passport_info.get('passport_number', '')) if passport_info.get('passport_number') else '',
                "passport_expiry": passport_info.get('passport_expiry', ''),
                "nationality": passport_info.get('nationality', ''),
                "visa_status": passport_info.get('visa_status', ''),
                "visa_expiry": passport_info.get('visa_expiry', ''),
                "has_passport_info": bool(passport_info.get('passport_number'))
            },
            "schedule_timeline": schedule_timeline,
            "statistics": stats,
            "completeness": {
                "score": completeness_score,
                "is_complete": completeness_score >= 90,
                "missing_items": missing_items,
                "completeness_desc": f"{completeness_score}%完整度" + (f"，缺少：{', '.join(missing_items)}" if missing_items else "，行程完整")
            },
            "summary": {
                "total_schedule_items": stats['total_items'],
                "schedule_span": f"共{stats['total_items']}个行程节点" if stats['total_items'] > 0 else "暂无行程安排",
                "key_milestones": f"接机{stats['pickup_count']}次，手术{stats['surgery_count']}次，送机{stats['dropoff_count']}次",
                "logistics": f"需要用车{stats['need_car_count']}次，有航班信息{stats['has_flight_info']}项",
                "message": f"{customer.get('ClientName', '')}的韩国行程安排" + (f"({completeness_score}%完整)" if stats['total_items'] > 0 else "尚未制定")
            }
        }
        
        if stats['total_items'] == 0:
            result["warning"] = "⚠️ 该客户尚未安排韩国行程，请联系相关部门制定行程计划"
        elif completeness_score < 80:
            result["warning"] = f"⚠️ 行程完整度{completeness_score}%，建议完善：{', '.join(missing_items)}"
        
        return json.dumps(_serialize(result), ensure_ascii=False, indent=2)
        
    except Exception as e:
        logger.exception("korean_schedule failed")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


# ---- 32. 推荐医院 ----

@mcp.tool()
def recommend_hospital_for_client(client_id: int) -> str:
    """为客户推荐合适的医院。基于客户意向项目和地区，结合医院合作状态、成交数据和转化率进行推荐。
    当用户问"派什么医院"、"推荐医院"、"哪个医院合适"、"应该派哪里"并且有客户上下文时使用。

    Args:
        client_id: 客户ID
    """
    try:
        # 1. 获取客户基本信息
        customer = query_customer_info(client_id)
        if not customer:
            return json.dumps({"error": f"客户 {client_id} 不存在"}, ensure_ascii=False)

        plastics_intention = int(customer.get("PlasticsIntention", 0) or 0)
        zx_district = int(customer.get("zx_District", 0) or 0)
        client_region = int(customer.get("client_region", 0) or 0)
        
        # 2. 用 get_linkage_name 把 ID 转中文名
        district_name = get_linkage_name(zx_district) if zx_district > 0 else ""
        
        if not district_name:
            return json.dumps({
                "error": "客户地区信息缺失，无法推荐医院",
                "client_id": client_id,
                "customer_name": customer.get('ClientName', ''),
            }, ensure_ascii=False)
        
        # 3. 用中文地区名查 robot_kb_hospitals（hospital_db）
        hospital_sql = """
            SELECT hospital_name, district_name, detailed_address, phone, 
                   cooperation_status, main_projects_list
            FROM robot_kb_hospitals 
            WHERE cooperation_status='合作中' 
            AND (city_name LIKE %s OR district_name LIKE %s) 
            ORDER BY qudao_id DESC 
            LIMIT 10
        """
        like_district = f"%{district_name}%"
        hospitals = query_db(hospital_sql, (like_district, like_district))
        
        # 4. 调用 query_district_hospital_performance 获取成交数据
        performance_data = query_district_hospital_performance(zx_district, client_region)
        
        # 5. 创建医院成交数据映射（按医院名称匹配）
        performance_map = {}
        if performance_data and performance_data.get('hospitals'):
            for perf_hospital in performance_data['hospitals']:
                hospital_name = perf_hospital.get('hospital_name', '')
                performance_map[hospital_name] = {
                    'deal_count': perf_hospital.get('deal_count', 0),
                    'unique_clients': perf_hospital.get('unique_clients', 0),
                    'total_amount': perf_hospital.get('total_amount', 0),
                    'avg_amount': perf_hospital.get('avg_amount', 0),
                    'repeat_clients': perf_hospital.get('repeat_clients', 0),
                    'has_repeat': perf_hospital.get('has_repeat', False),
                    'conversion_rate': round(perf_hospital.get('deal_count', 0) * 100.0 / max(perf_hospital.get('unique_clients', 1), 1), 2)
                }
        
        # 6. 合并医院信息和成交数据
        recommended_hospitals = []
        for hospital in hospitals:
            hospital_name = hospital.get('hospital_name', '')
            
            # 从成交数据中查找匹配的医院（模糊匹配）
            matched_performance = None
            for perf_name, perf_data in performance_map.items():
                # 简单的名称匹配逻辑：包含关键词
                if hospital_name in perf_name or perf_name in hospital_name:
                    matched_performance = perf_data
                    break
            
            # 如果没有找到匹配的成交数据，创建默认值
            if not matched_performance:
                matched_performance = {
                    'deal_count': 0,
                    'unique_clients': 0,
                    'total_amount': 0,
                    'avg_amount': 0,
                    'repeat_clients': 0,
                    'has_repeat': False,
                    'conversion_rate': 0.0
                }
            
            recommended_hospital = {
                'hospital_name': hospital_name,
                'district_name': hospital.get('district_name', ''),
                'detailed_address': hospital.get('detailed_address', ''),
                'phone': hospital.get('phone', ''),
                'main_projects': hospital.get('main_projects_list', ''),
                'deal_count': matched_performance['deal_count'],
                'unique_clients': matched_performance['unique_clients'],
                'total_amount': matched_performance['total_amount'],
                'avg_deal_amount': matched_performance['avg_amount'],
                'repeat_clients': matched_performance['repeat_clients'],
                'has_repeat_business': matched_performance['has_repeat'],
                'conversion_rate': matched_performance['conversion_rate'],
                'recommendation_score': 0  # 将在下面计算
            }
            
            # 计算推荐评分（0-100分）
            score = 0
            
            # 成交数量权重 (40分)
            deal_count = matched_performance['deal_count']
            if deal_count >= 10:
                score += 40
            elif deal_count >= 5:
                score += 30
            elif deal_count >= 1:
                score += 20
            else:
                score += 10  # 合作中医院基础分
            
            # 转化率权重 (30分)
            conv_rate = matched_performance['conversion_rate']
            if conv_rate >= 80:
                score += 30
            elif conv_rate >= 60:
                score += 25
            elif conv_rate >= 40:
                score += 20
            elif conv_rate >= 20:
                score += 15
            else:
                score += 5
            
            # 二开能力权重 (20分)
            if matched_performance['has_repeat']:
                score += 20
            
            # 客单价权重 (10分)
            avg_amount = matched_performance['avg_amount']
            if avg_amount >= 30000:
                score += 10
            elif avg_amount >= 20000:
                score += 8
            elif avg_amount >= 10000:
                score += 5
            else:
                score += 2
            
            recommended_hospital['recommendation_score'] = score
            recommended_hospitals.append(recommended_hospital)
        
        # 7. 按成交数降序排序，然后按推荐评分排序
        recommended_hospitals.sort(key=lambda x: (x['deal_count'], x['recommendation_score']), reverse=True)
        
        # 意向项目映射
        intention_map = {
            1: '种植牙', 2: '矫正', 3: '美白', 4: '洁牙', 5: '补牙',
            6: '拔牙', 7: '口腔检查', 8: '牙周治疗', 9: '根管治疗', 10: '烤瓷牙'
        }
        intention_name = intention_map.get(plastics_intention, '其他')
        
        # 构造返回结果
        result = {
            "client_id": client_id,
            "customer_info": {
                "name": customer.get('ClientName', ''),
                "intention_project": intention_name,
                "target_district": district_name,
            },
            "district_performance": {
                "district_name": district_name,
                "total_hospitals_in_kb": len(hospitals),
                "hospitals_with_deals": len([h for h in recommended_hospitals if h['deal_count'] > 0]),
                "district_score": performance_data.get('district_score', 0) if performance_data else 0,
            },
            "recommended_hospitals": recommended_hospitals[:5],  # 返回前5个推荐
            "summary": {
                "total_recommendations": len(recommended_hospitals),
                "top_hospital": recommended_hospitals[0]['hospital_name'] if recommended_hospitals else "暂无推荐",
                "avg_conversion_rate": round(sum(h['conversion_rate'] for h in recommended_hospitals) / len(recommended_hospitals), 2) if recommended_hospitals else 0,
                "recommendation_basis": f"基于客户意向项目({intention_name})和目标地区({district_name})的医院合作状态及成交表现"
            }
        }
        
        return json.dumps(_serialize(result), ensure_ascii=False, indent=2)
        
    except Exception as e:
        logger.exception("recommend_hospital_for_client failed")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


# ---- 23. 客户没派单原因分析 ----

@mcp.tool()
def no_dispatch_analysis(days: int = 30, kf_id: int = 0) -> str:
    """分析为什么这些客户没有派单。统计指定时间范围内注册但未派单的客户，按 no_order_type 分组统计各原因。
    当用户问"为什么没派单"、"哪些客户没派单"、"无法派单"、"派单率低"时使用。

    Args:
        days: 统计天数，默认30天
        kf_id: 客服ID，0表示全部客服
    """
    try:
        # 计算时间范围
        from_timestamp = int(time.time()) - (days * 24 * 3600)
        
        # 构建SQL条件
        kf_condition = ""
        kf_params = []
        if kf_id > 0:
            kf_condition = " AND KfId = %s"
            kf_params = [kf_id]
        
        # 查询注册但未派单的客户及其no_order_type
        sql = f"""
            SELECT 
                c.Client_Id,
                c.ClientName,
                c.no_order_type,
                c.RegisterTime,
                c.KfId,
                a.realname as kf_name
            FROM un_channel_client c
            LEFT JOIN un_admin a ON c.KfId = a.userid
            WHERE c.RegisterTime >= %s
            {kf_condition}
            AND c.Client_Id NOT IN (
                SELECT DISTINCT Client_Id 
                FROM un_hospital_order 
                WHERE send_order_time > 0
            )
            ORDER BY c.RegisterTime DESC
        """
        
        params = [from_timestamp] + kf_params
        no_dispatch_customers = query_qudao_db(sql, tuple(params))
        
        if not no_dispatch_customers:
            return json.dumps({
                "message": f"最近{days}天内没有未派单的客户",
                "analysis_period": f"{days}天",
                "total_no_dispatch": 0,
                "by_reason": []
            }, ensure_ascii=False, indent=2)
        
        # 统计各原因的客户数
        reason_stats = {}
        total_no_dispatch = len(no_dispatch_customers)
        
        for customer in no_dispatch_customers:
            no_order_type = customer.get('no_order_type', 0) or 0
            
            if no_order_type not in reason_stats:
                reason_stats[no_order_type] = {
                    'reason_id': no_order_type,
                    'reason_name': get_linkage_name(no_order_type) if no_order_type > 0 else '未设置原因',
                    'count': 0,
                    'customers': []
                }
            
            reason_stats[no_order_type]['count'] += 1
            reason_stats[no_order_type]['customers'].append({
                'client_id': customer.get('Client_Id'),
                'client_name': customer.get('ClientName', ''),
                'kf_name': customer.get('kf_name', ''),
                'register_time': datetime.fromtimestamp(customer.get('RegisterTime', 0)).strftime('%Y-%m-%d %H:%M') if customer.get('RegisterTime') else ''
            })
        
        # 按客户数排序，计算百分比
        by_reason = []
        for reason_id, stats in reason_stats.items():
            percentage = round(stats['count'] / total_no_dispatch * 100, 2) if total_no_dispatch > 0 else 0
            by_reason.append({
                'reason_id': reason_id,
                'reason_name': stats['reason_name'],
                'count': stats['count'],
                'percentage': percentage,
                'sample_customers': stats['customers'][:5]  # 只返回前5个样本客户
            })
        
        # 按数量倒序排列
        by_reason.sort(key=lambda x: x['count'], reverse=True)
        
        # 获取客服信息（如果指定了客服）
        kf_info = ""
        if kf_id > 0:
            kf_sql = "SELECT realname FROM un_admin WHERE userid = %s LIMIT 1"
            kf_result = query_qudao_db(kf_sql, (kf_id,))
            if kf_result:
                kf_info = f"（客服：{kf_result[0].get('realname', '')}）"
        
        result = {
            "message": f"最近{days}天共有 {total_no_dispatch} 个客户未派单{kf_info}",
            "analysis_period": f"{days}天",
            "kf_id": kf_id,
            "kf_info": kf_info.replace('（', '').replace('）', '') if kf_info else "全部客服",
            "total_no_dispatch": total_no_dispatch,
            "by_reason": by_reason,
            "summary": {
                "top_reason": by_reason[0]['reason_name'] if by_reason else "无数据",
                "top_reason_count": by_reason[0]['count'] if by_reason else 0,
                "top_reason_percentage": by_reason[0]['percentage'] if by_reason else 0,
                "reasons_count": len(by_reason)
            }
        }
        
        return json.dumps(_serialize(result), ensure_ascii=False, indent=2)
        
    except Exception as e:
        logger.exception("no_dispatch_analysis failed")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


# ============================================================
# 启动入口
# ============================================================

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="口腔CRM MCP Server")
    parser.add_argument("--transport", choices=["sse", "stdio"], default="sse",
                        help="传输协议 (默认: sse)")
    parser.add_argument("--port", type=int, default=8091,
                        help="HTTP 端口 (默认: 8091)")
    parser.add_argument("--host", default="0.0.0.0",
                        help="HTTP 绑定地址 (默认: 0.0.0.0)")
    args = parser.parse_args()

    logger.info(f"启动 MCP Server ({args.transport} 模式, {MCP_HOST}:{MCP_PORT})")
    mcp.run(transport=args.transport)
