#!/usr/bin/env python3
"""
客户成交概率查询工具
查询指定客户的详细信息和成交概率分析
"""

import pymysql
from datetime import datetime, timedelta
import json

# 渠道系统数据库配置
QUDAO_DB_CONFIG = {
    "host": "192.168.103.99",
    "port": 3306,
    "user": "kfsyscb",
    "password": "HfBFtvXkq5",
    "database": "kfsyscb",
    "charset": "utf8mb4",
}


def get_connection():
    """获取数据库连接"""
    return pymysql.connect(**QUDAO_DB_CONFIG)


def query_customer_info(client_id: int) -> dict:
    """查询客户基本信息"""
    conn = get_connection()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            sql = """
                SELECT
                    c.Client_Id,
                    c.ClientName,
                    c.Sex,
                    c.Age,
                    c.MobilePhone,
                    c.District,
                    c.zx_District,
                    c.PlasticsIntention,
                    c.client_region,
                    c.Status,
                    c.client_status,
                    c.RegisterTime,
                    c.EditTime,
                    c.KfId,
                    c.ManagerId,
                    c.from_type,
                    c.Remarks
                FROM un_channel_client c
                WHERE c.Client_Id = %s
            """
            cursor.execute(sql, (client_id,))
            return cursor.fetchone()
    finally:
        conn.close()


def query_follow_records(client_id: int) -> list:
    """查询客户跟进记录"""
    conn = get_connection()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            sql = """
                SELECT
                    CM_Id as id,
                    Content as CMContent,
                    CMPostTime,
                    Type as CMType,
                    KfId as CMPostPerson
                FROM un_channel_managermessage
                WHERE ClientId = %s
                ORDER BY CMPostTime DESC
                LIMIT 20
            """
            cursor.execute(sql, (client_id,))
            return cursor.fetchall()
    finally:
        conn.close()


def query_order_records(client_id: int) -> list:
    """查询客户成交记录"""
    conn = get_connection()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            sql = """
                SELECT
                    id,
                    hospital_id as Hospital_Id,
                    hospital as Hospital_Name,
                    true_number,
                    number,
                    time,
                    status,
                    op_type,
                    item
                FROM un_channel_paylist
                WHERE Client_Id = %s
                ORDER BY time DESC
            """
            cursor.execute(sql, (client_id,))
            return cursor.fetchall()
    finally:
        conn.close()


def query_similar_customers_conversion(intention: int, region: int) -> dict:
    """查询相似客户的成交率（同意向项目+同地区）"""
    conn = get_connection()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            # 查询相似客户的成交情况
            sql = """
                SELECT
                    COUNT(*) as total_count,
                    SUM(CASE WHEN Status = 1 THEN 1 ELSE 0 END) as converted_count,
                    AVG(CASE WHEN Status = 1 THEN 1 ELSE 0 END) as conversion_rate
                FROM un_channel_client
                WHERE PlasticsIntention = %s
                  AND (zx_District = %s OR client_region = %s)
                  AND RegisterTime >= UNIX_TIMESTAMP(DATE_SUB(NOW(), INTERVAL 90 DAY))
            """
            cursor.execute(sql, (intention, region, region))
            return cursor.fetchone()
    finally:
        conn.close()


def analyze_follow_content(records: list) -> dict:
    """分析跟进内容，提取意向信号"""
    positive_keywords = ['感兴趣', '想了解', '预约', '咨询', '想做', '考虑', '价格多少', '什么时候', '有时间']
    negative_keywords = ['不需要', '太贵', '再考虑', '暂时不', '没时间', '不想', '算了']

    positive_count = 0
    negative_count = 0
    total_records = len(records)

    recent_activity = False
    if records:
        latest_time = records[0].get('CMPostTime', 0)
        if latest_time:
            days_since_last = (datetime.now() - datetime.fromtimestamp(latest_time)).days
            recent_activity = days_since_last <= 7

    for record in records:
        content = record.get('CMContent', '') or ''
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
        'engagement_score': min(total_records * 10, 100)  # 跟进次数转换为参与度分数
    }


def calculate_conversion_probability(customer: dict, follow_analysis: dict, similar_rate: dict, orders: list) -> dict:
    """
    计算成交概率

    评分维度：
    1. 客户状态 (30%) - 已成交直接100分
    2. 跟进活跃度 (25%) - 跟进次数和最近活动
    3. 意向信号 (25%) - 正面/负面关键词
    4. 相似客户转化率 (20%) - 同类客户历史数据
    """

    # 如果已经成交
    if customer.get('Status') == 1 or orders:
        return {
            'probability': 100,
            'level': 'A',
            'status': '已成交',
            'reason': '该客户已有成交记录'
        }

    score = 0
    reasons = []

    # 1. 客户状态评分 (30%)
    status = customer.get('Status', 0)
    client_status = customer.get('client_status', 0)
    if status == 0:  # 未成交，进行中
        status_score = 50
        reasons.append("客户状态：跟进中")
    elif status == 2:  # 已取消
        status_score = 10
        reasons.append("客户状态：已取消（低概率）")
    else:
        status_score = 30
    score += status_score * 0.3

    # 2. 跟进活跃度评分 (25%)
    engagement = follow_analysis.get('engagement_score', 0)
    if follow_analysis.get('recent_activity'):
        engagement += 20
        reasons.append("近7天有跟进活动（+）")
    score += min(engagement, 100) * 0.25

    # 3. 意向信号评分 (25%)
    positive = follow_analysis.get('positive_signals', 0)
    negative = follow_analysis.get('negative_signals', 0)
    if positive > negative:
        signal_score = min(70 + positive * 10, 100)
        reasons.append(f"意向信号积极：{positive}个正面信号")
    elif negative > positive:
        signal_score = max(30 - negative * 10, 0)
        reasons.append(f"意向信号消极：{negative}个负面信号")
    else:
        signal_score = 50
        reasons.append("意向信号中性")
    score += signal_score * 0.25

    # 4. 相似客户转化率 (20%)
    if similar_rate and similar_rate.get('conversion_rate'):
        similar_conversion = float(similar_rate['conversion_rate']) * 100
        score += similar_conversion * 0.2
        reasons.append(f"相似客户转化率：{similar_conversion:.1f}%")
    else:
        score += 30 * 0.2  # 默认30%
        reasons.append("相似客户数据不足，使用默认值")

    # 确定等级
    if score >= 70:
        level = 'A'
        level_desc = '高意向'
    elif score >= 50:
        level = 'B'
        level_desc = '中等意向'
    elif score >= 30:
        level = 'C'
        level_desc = '低意向'
    else:
        level = 'D'
        level_desc = '极低意向'

    return {
        'probability': round(score, 1),
        'level': level,
        'level_desc': level_desc,
        'status': '跟进中',
        'reasons': reasons
    }


def format_timestamp(ts):
    """格式化时间戳"""
    if not ts:
        return '未知'
    try:
        return datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M')
    except:
        return '未知'


def query_customer_conversion(client_id: int):
    """主查询函数：查询客户成交概率"""

    print(f"\n{'='*60}")
    print(f"  客户成交概率分析报告 - ID: {client_id}")
    print(f"{'='*60}\n")

    # 1. 查询客户基本信息
    print("📋 正在查询客户信息...")
    customer = query_customer_info(client_id)

    if not customer:
        print(f"❌ 未找到客户 ID: {client_id}")
        return

    print(f"\n【客户基本信息】")
    print(f"  姓名: {customer.get('ClientName', '未知')}")
    print(f"  性别: {'男' if customer.get('Sex') == 1 else '女' if customer.get('Sex') == 2 else '未知'}")
    print(f"  年龄: {customer.get('Age', '未知')}")
    print(f"  手机: {customer.get('MobilePhone', '未知')}")
    print(f"  意向项目ID: {customer.get('PlasticsIntention', '未知')}")
    print(f"  意向地区ID: {customer.get('zx_District', '未知')}")
    print(f"  客户状态: {'已成交' if customer.get('Status') == 1 else '未成交' if customer.get('Status') == 0 else '已取消'}")
    print(f"  注册时间: {format_timestamp(customer.get('RegisterTime'))}")
    print(f"  最后编辑: {format_timestamp(customer.get('EditTime'))}")

    # 2. 查询跟进记录
    print(f"\n📝 正在查询跟进记录...")
    follow_records = query_follow_records(client_id)
    print(f"  共 {len(follow_records)} 条跟进记录")

    if follow_records:
        print(f"\n【最近跟进记录】(最多显示5条)")
        for i, record in enumerate(follow_records[:5]):
            time_str = format_timestamp(record.get('CMPostTime'))
            content = (record.get('CMContent', '') or '')[:100]
            content = content.replace('\n', ' ').replace('\r', '')
            print(f"  {i+1}. [{time_str}] {content}...")

    # 3. 查询成交记录
    print(f"\n💰 正在查询成交记录...")
    orders = query_order_records(client_id)

    if orders:
        print(f"  共 {len(orders)} 条成交记录")
        print(f"\n【成交记录】")
        for order in orders:
            time_str = format_timestamp(order.get('time'))
            amount = order.get('true_number', 0)
            hospital = order.get('Hospital_Name', '未知')
            status = '已确认' if order.get('status') == 1 else '待确认'
            print(f"  - [{time_str}] {hospital} | ¥{amount} | {status}")
    else:
        print(f"  暂无成交记录")

    # 4. 查询相似客户转化率
    print(f"\n📊 正在分析相似客户数据...")
    similar_rate = query_similar_customers_conversion(
        customer.get('PlasticsIntention', 0),
        customer.get('zx_District', 0) or customer.get('client_region', 0)
    )

    if similar_rate:
        total = similar_rate.get('total_count', 0)
        converted = similar_rate.get('converted_count', 0)
        rate = float(similar_rate.get('conversion_rate', 0) or 0) * 100
        print(f"  相似客户总数: {total}")
        print(f"  已成交数量: {converted}")
        print(f"  历史转化率: {rate:.1f}%")

    # 5. 分析跟进内容
    follow_analysis = analyze_follow_content(follow_records)

    # 6. 计算成交概率
    print(f"\n{'='*60}")
    print(f"  🎯 成交概率分析结果")
    print(f"{'='*60}")

    result = calculate_conversion_probability(customer, follow_analysis, similar_rate, orders)

    print(f"\n  成交概率: {result['probability']}%")
    print(f"  意向等级: {result['level']} ({result.get('level_desc', result['status'])})")
    print(f"\n  分析依据:")
    for reason in result.get('reasons', [result.get('reason', '')]):
        print(f"    • {reason}")

    # 7. 给出建议
    print(f"\n{'='*60}")
    print(f"  💡 跟进建议")
    print(f"{'='*60}")

    if result['level'] == 'A':
        print("  • 高意向客户，建议立即跟进")
        print("  • 可以主动推荐优质医院和优惠活动")
        print("  • 尽快安排预约或面诊")
    elif result['level'] == 'B':
        print("  • 中等意向客户，保持定期跟进")
        print("  • 了解客户顾虑点，针对性解答")
        print("  • 可推送相关案例和优惠信息")
    elif result['level'] == 'C':
        print("  • 低意向客户，建议轻度维护")
        print("  • 定期发送行业资讯保持联系")
        print("  • 等待客户主动咨询时再深入跟进")
    else:
        print("  • 极低意向客户，建议暂缓跟进")
        print("  • 可加入长期培育池")
        print("  • 重大活动时批量触达")

    print(f"\n{'='*60}\n")

    return {
        'customer': customer,
        'follow_records': follow_records,
        'orders': orders,
        'analysis': result
    }


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        client_id = int(sys.argv[1])
    else:
        client_id = 4333303  # 默认查询的客户ID

    try:
        query_customer_conversion(client_id)
    except Exception as e:
        print(f"❌ 查询出错: {e}")
        print("\n请检查:")
        print("  1. 数据库连接是否正常 (192.168.103.99)")
        print("  2. 客户ID是否存在")
        print("  3. 网络是否能访问内网")
