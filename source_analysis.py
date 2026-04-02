#!/usr/bin/env python3
"""
来源分析模块 - 支持牙舒丽等站点的多维度数据分析

功能:
1. 地区分布分析
2. 意向项目分布
3. 访问医院TOP
4. 自然vs竞价对比
5. 时间趋势分析
6. 咨询内容热词分析
"""

import pymysql
from pymysql.cursors import DictCursor
from datetime import datetime, timedelta
from collections import defaultdict
from typing import Dict, List, Optional, Tuple
import re

# 渠道系统数据库配置
QUDAO_DB_CONFIG = {
    "host": "192.168.103.227",
    "port": 2883,
    "user": "oaxz@kfsys_tnt#szst_oceanbase_sec",
    "password": "yq4(Rv-ZMWvo4Sg%5q2!",
    "database": "kfsyscb",
    "charset": "utf8mb4",
}


def get_connection():
    """获取数据库连接"""
    return pymysql.connect(**QUDAO_DB_CONFIG)


def query_db(sql: str, params: tuple = None) -> List[Dict]:
    """执行SQL查询"""
    conn = get_connection()
    try:
        with conn.cursor(DictCursor) as cursor:
            cursor.execute(sql, params)
            return list(cursor.fetchall())
    finally:
        conn.close()


# ============================================================
# 时间范围工具
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


def normalize_days(days: int = None, max_days: int = 365) -> int:
    """
    标准化分析天数
    - 默认: 本月天数
    - 最大: 365天 (一年)
    """
    if days is None:
        days = get_current_month_days()
    return min(max(days, 1), max_days)


# ============================================================
# 来源分类工具
# ============================================================
def classify_source(source_name: str, parent_name: str = '') -> str:
    """
    分类来源类型
    Returns: 'paid' | 'organic' | 'channel' | 'unknown'
    """
    if not source_name:
        return 'unknown'

    name = source_name.lower()
    parent = (parent_name or '').lower()

    # 竞价/付费关键词
    paid_keywords = [
        '头条', '抖音', '快手', 'tiktok', '信息流', '竞价', '百度',
        '360', '搜狗', '投放', '广告', '小红书', 'sem', 'ppc'
    ]

    # 自然来源关键词
    organic_keywords = [
        'swt', '商务通', '400', '电话', '微信', '公众号', '小程序',
        'app', '移动', '导入', '主站', 'seo', '自然'
    ]

    # 渠道关键词
    channel_keywords = ['渠道', '合作', '代理', '分销', '外部']

    for kw in paid_keywords:
        if kw in name or kw in parent:
            return 'paid'

    for kw in organic_keywords:
        if kw in name or kw in parent:
            return 'organic'

    for kw in channel_keywords:
        if kw in name or kw in parent:
            return 'channel'

    return 'unknown'


# ============================================================
# 来源ID查询
# ============================================================
def get_source_ids(source_keyword: str) -> List[int]:
    """根据关键词查找来源ID列表"""
    sql = """
        SELECT linkageid, name, parentid
        FROM un_linkage
        WHERE keyid = 5594 AND name LIKE %s
    """
    results = query_db(sql, (f"%{source_keyword}%",))
    return [r['linkageid'] for r in results]


def get_all_source_info() -> Dict[int, Dict]:
    """获取所有来源的详细信息"""
    sql = """
        SELECT
            l.linkageid, l.name, l.parentid,
            p.name as parent_name
        FROM un_linkage l
        LEFT JOIN un_linkage p ON l.parentid = p.linkageid
        WHERE l.keyid = 5594
    """
    results = query_db(sql)
    return {r['linkageid']: r for r in results}


# ============================================================
# 1. 地区分布分析
# ============================================================
def analyze_region_distribution(source_ids: List[int], days: int = None) -> Dict:
    """
    分析指定来源的地区分布

    Args:
        source_ids: 来源ID列表
        days: 分析天数，默认本月，最大365天

    Returns:
        地区分布统计
    """
    if not source_ids:
        return {'error': '未找到来源ID'}

    days = normalize_days(days)
    placeholders = ','.join(['%s'] * len(source_ids))
    start_time = int((datetime.now() - timedelta(days=days)).timestamp())

    # 查询地区分布（按省份）- 成交从paylist表统计
    sql = f"""
        SELECT
            l.name as province,
            COUNT(DISTINCT c.Client_Id) as total_count,
            COUNT(DISTINCT CASE WHEN p.status IN (1,3,5) THEN c.Client_Id END) as converted_count,
            COALESCE(SUM(CASE WHEN p.status IN (1,3,5) THEN p.number ELSE 0 END), 0) as deal_amount
        FROM un_channel_client c
        LEFT JOIN un_linkage l ON c.zx_District = l.linkageid
        LEFT JOIN un_channel_paylist p ON c.Client_Id = p.Client_Id
        WHERE c.from_type IN ({placeholders})
          AND c.RegisterTime >= %s
        GROUP BY c.zx_District, l.name
        HAVING total_count >= 10
        ORDER BY total_count DESC
        LIMIT 30
    """
    params = tuple(source_ids) + (start_time,)
    results = query_db(sql, params)

    # 计算总量
    total = sum(r['total_count'] for r in results)
    total_converted = sum(r['converted_count'] or 0 for r in results)
    total_amount = sum(float(r['deal_amount'] or 0) for r in results)

    return {
        'total_customers': total,
        'total_converted': total_converted,
        'total_amount': round(total_amount, 2),
        'conversion_rate': round(total_converted / total * 100, 2) if total > 0 else 0,
        'regions': [
            {
                'province': r['province'] or '未知',
                'count': r['total_count'],
                'converted': r['converted_count'] or 0,
                'amount': round(float(r['deal_amount'] or 0), 2),
                'rate': round((r['converted_count'] or 0) / r['total_count'] * 100, 2) if r['total_count'] > 0 else 0,
                'percentage': round(r['total_count'] / total * 100, 2) if total > 0 else 0
            }
            for r in results
        ]
    }


# ============================================================
# 2. 意向项目分布分析
# ============================================================
def analyze_project_distribution(source_ids: List[int], days: int = None) -> Dict:
    """
    分析指定来源的意向项目分布

    Args:
        source_ids: 来源ID列表
        days: 分析天数，默认本月，最大365天

    Returns:
        项目分布统计
    """
    if not source_ids:
        return {'error': '未找到来源ID'}

    days = normalize_days(days)
    placeholders = ','.join(['%s'] * len(source_ids))
    start_time = int((datetime.now() - timedelta(days=days)).timestamp())

    sql = f"""
        SELECT
            l.name as project_name,
            COUNT(*) as total_count,
            SUM(CASE WHEN c.Status = 1 THEN 1 ELSE 0 END) as converted_count
        FROM un_channel_client c
        LEFT JOIN un_linkage l ON c.PlasticsIntention = l.linkageid
        WHERE c.from_type IN ({placeholders})
          AND c.RegisterTime >= %s
        GROUP BY c.PlasticsIntention, l.name
        HAVING total_count >= 5
        ORDER BY total_count DESC
        LIMIT 30
    """
    params = tuple(source_ids) + (start_time,)
    results = query_db(sql, params)

    total = sum(r['total_count'] for r in results)

    return {
        'total_customers': total,
        'projects': [
            {
                'project': r['project_name'] or '未填写',
                'count': r['total_count'],
                'converted': r['converted_count'] or 0,
                'rate': round((r['converted_count'] or 0) / r['total_count'] * 100, 2) if r['total_count'] > 0 else 0,
                'percentage': round(r['total_count'] / total * 100, 2) if total > 0 else 0
            }
            for r in results
        ]
    }


# ============================================================
# 3. 访问医院TOP分析
# ============================================================
def analyze_hospital_distribution(source_ids: List[int], days: int = None) -> Dict:
    """
    分析指定来源的访问医院分布（基于派单和成交数据）

    Args:
        source_ids: 来源ID列表
        days: 分析天数，默认本月，最大365天

    Returns:
        医院分布统计
    """
    if not source_ids:
        return {'error': '未找到来源ID'}

    days = normalize_days(days)
    placeholders = ','.join(['%s'] * len(source_ids))
    start_time = int((datetime.now() - timedelta(days=days)).timestamp())

    # 查询成交医院分布 - 使用number字段(订单金额), status IN (1,3,5)为有效成交
    sql = f"""
        SELECT
            p.hospital as hospital_name,
            COUNT(DISTINCT p.Client_Id) as customer_count,
            COUNT(*) as order_count,
            SUM(p.number) as total_amount,
            AVG(p.number) as avg_amount
        FROM un_channel_paylist p
        JOIN un_channel_client c ON p.Client_Id = c.Client_Id
        WHERE c.from_type IN ({placeholders})
          AND c.RegisterTime >= %s
          AND p.status IN (1, 3, 5)
          AND p.hospital IS NOT NULL
          AND p.hospital != ''
        GROUP BY p.hospital
        HAVING customer_count >= 1
        ORDER BY total_amount DESC
        LIMIT 30
    """
    params = tuple(source_ids) + (start_time,)
    results = query_db(sql, params)

    total_amount = sum(float(r['total_amount'] or 0) for r in results)
    total_orders = sum(r['order_count'] for r in results)

    return {
        'total_orders': total_orders,
        'total_amount': round(total_amount, 2),
        'hospitals': [
            {
                'hospital': r['hospital_name'],
                'customers': r['customer_count'],
                'orders': r['order_count'],
                'amount': round(float(r['total_amount'] or 0), 2),
                'avg_amount': round(float(r['avg_amount'] or 0), 2),
                'amount_percentage': round(float(r['total_amount'] or 0) / total_amount * 100, 2) if total_amount > 0 else 0
            }
            for r in results
        ]
    }


# ============================================================
# 4. 自然vs竞价对比分析
# ============================================================
def analyze_source_type_comparison(source_keyword: str = None, days: int = None) -> Dict:
    """
    分析自然来源vs竞价来源的对比

    Args:
        source_keyword: 可选，限定特定来源（如"牙舒丽"）
        days: 分析天数，默认本月，最大365天

    Returns:
        来源类型对比统计
    """
    days = normalize_days(days)
    start_time = int((datetime.now() - timedelta(days=days)).timestamp())

    # 获取所有来源信息
    source_info = get_all_source_info()

    # 构建来源分类
    source_classification = {}
    for sid, info in source_info.items():
        source_type = classify_source(info['name'], info.get('parent_name', ''))
        source_classification[sid] = {
            'name': info['name'],
            'type': source_type
        }

    # 查询客户数据 - 成交从paylist统计
    sql_base = """
        SELECT
            c.from_type,
            COUNT(DISTINCT c.Client_Id) as total_count,
            COUNT(DISTINCT CASE WHEN p.status IN (1,3,5) THEN c.Client_Id END) as converted_count,
            COALESCE(SUM(CASE WHEN p.status IN (1,3,5) THEN p.number ELSE 0 END), 0) as deal_amount
        FROM un_channel_client c
        LEFT JOIN un_channel_paylist p ON c.Client_Id = p.Client_Id
        WHERE c.RegisterTime >= %s
          AND c.from_type > 0
    """

    if source_keyword:
        # 限定特定来源
        source_ids = get_source_ids(source_keyword)
        if not source_ids:
            return {'error': f'未找到包含"{source_keyword}"的来源'}
        placeholders = ','.join(['%s'] * len(source_ids))
        sql = sql_base + f" AND c.from_type IN ({placeholders}) GROUP BY c.from_type"
        params = (start_time,) + tuple(source_ids)
    else:
        sql = sql_base + " GROUP BY c.from_type"
        params = (start_time,)

    results = query_db(sql, params)

    # 按类型汇总
    type_stats = {
        'paid': {'name': '竞价/付费', 'total': 0, 'converted': 0, 'amount': 0, 'sources': []},
        'organic': {'name': '自然来源', 'total': 0, 'converted': 0, 'amount': 0, 'sources': []},
        'channel': {'name': '渠道合作', 'total': 0, 'converted': 0, 'amount': 0, 'sources': []},
        'unknown': {'name': '未分类', 'total': 0, 'converted': 0, 'amount': 0, 'sources': []}
    }

    for r in results:
        from_type = r['from_type']
        total = r['total_count']
        converted = r['converted_count'] or 0
        amount = float(r['deal_amount'] or 0)

        if from_type in source_classification:
            stype = source_classification[from_type]['type']
            sname = source_classification[from_type]['name']
        else:
            stype = 'unknown'
            sname = f'未知({from_type})'

        type_stats[stype]['total'] += total
        type_stats[stype]['converted'] += converted
        type_stats[stype]['amount'] += amount
        type_stats[stype]['sources'].append({
            'id': from_type,
            'name': sname,
            'total': total,
            'converted': converted,
            'amount': amount
        })

    # 计算转化率和客单价
    for stype in type_stats:
        stats = type_stats[stype]
        stats['rate'] = round(stats['converted'] / stats['total'] * 100, 2) if stats['total'] > 0 else 0
        stats['avg_amount'] = round(stats['amount'] / stats['converted'], 2) if stats['converted'] > 0 else 0
        # 按客户数排序来源
        stats['sources'] = sorted(stats['sources'], key=lambda x: x['total'], reverse=True)[:10]

    grand_total = sum(s['total'] for s in type_stats.values())
    grand_amount = sum(s['amount'] for s in type_stats.values())

    return {
        'period_days': days,
        'source_filter': source_keyword,
        'grand_total': grand_total,
        'grand_amount': round(grand_amount, 2),
        'comparison': [
            {
                'type': stype,
                'type_name': stats['name'],
                'total': stats['total'],
                'converted': stats['converted'],
                'amount': round(stats['amount'], 2),
                'avg_amount': stats['avg_amount'],
                'rate': stats['rate'],
                'percentage': round(stats['total'] / grand_total * 100, 2) if grand_total > 0 else 0,
                'top_sources': stats['sources'][:5]
            }
            for stype, stats in type_stats.items()
            if stats['total'] > 0
        ]
    }


# ============================================================
# 5. 时间趋势分析
# ============================================================
def analyze_time_trend(source_ids: List[int], days: int = None, group_by: str = 'day') -> Dict:
    """
    分析指定来源的时间趋势

    Args:
        source_ids: 来源ID列表
        days: 分析天数，默认本月，最大365天
        group_by: 分组方式 'day' | 'week' | 'month'

    Returns:
        时间趋势统计
    """
    if not source_ids:
        return {'error': '未找到来源ID'}

    days = normalize_days(days)
    placeholders = ','.join(['%s'] * len(source_ids))
    start_time = int((datetime.now() - timedelta(days=days)).timestamp())

    # 根据分组方式选择SQL（使用%%转义避免与参数占位符冲突）
    if group_by == 'month':
        date_expr = "DATE_FORMAT(FROM_UNIXTIME(c.RegisterTime), '%%Y-%%m')"
    elif group_by == 'week':
        date_expr = "DATE_FORMAT(FROM_UNIXTIME(c.RegisterTime), '%%Y-W%%V')"
    else:  # day
        date_expr = "DATE_FORMAT(FROM_UNIXTIME(c.RegisterTime), '%%Y-%%m-%%d')"

    sql = f"""
        SELECT
            {date_expr} as period,
            COUNT(DISTINCT c.Client_Id) as total_count,
            COUNT(DISTINCT CASE WHEN p.status IN (1,3,5) THEN c.Client_Id END) as converted_count,
            COALESCE(SUM(CASE WHEN p.status IN (1,3,5) THEN p.number ELSE 0 END), 0) as deal_amount
        FROM un_channel_client c
        LEFT JOIN un_channel_paylist p ON c.Client_Id = p.Client_Id
        WHERE c.from_type IN ({placeholders})
          AND c.RegisterTime >= %s
        GROUP BY {date_expr}
        ORDER BY period ASC
    """
    params = tuple(source_ids) + (start_time,)
    results = query_db(sql, params)

    return {
        'period_days': days,
        'group_by': group_by,
        'trend': [
            {
                'period': r['period'],
                'registrations': r['total_count'],
                'conversions': r['converted_count'] or 0,
                'amount': round(float(r['deal_amount'] or 0), 2),
                'rate': round((r['converted_count'] or 0) / r['total_count'] * 100, 2) if r['total_count'] > 0 else 0
            }
            for r in results
        ]
    }


# ============================================================
# 6. 咨询内容热词分析
# ============================================================
def analyze_consultation_keywords(source_ids: List[int], days: int = None, limit: int = 50) -> Dict:
    """
    分析指定来源的咨询内容热词

    Args:
        source_ids: 来源ID列表
        days: 分析天数，默认本月，最大365天
        limit: 返回记录数

    Returns:
        热词统计
    """
    if not source_ids:
        return {'error': '未找到来源ID'}

    days = normalize_days(days)
    placeholders = ','.join(['%s'] * len(source_ids))
    start_time = int((datetime.now() - timedelta(days=days)).timestamp())

    # 查询最近的咨询内容
    sql = f"""
        SELECT m.Content
        FROM un_channel_managermessage m
        JOIN un_channel_client c ON m.ClientId = c.Client_Id
        WHERE c.from_type IN ({placeholders})
          AND m.CMPostTime >= %s
          AND m.Content IS NOT NULL
          AND m.Content != ''
        ORDER BY m.CMPostTime DESC
        LIMIT %s
    """
    params = tuple(source_ids) + (start_time, limit * 20)  # 多取一些用于分析
    results = query_db(sql, params)

    # 关键词提取（简单版本）
    keyword_count = defaultdict(int)

    # 预定义的业务关键词
    business_keywords = [
        # 项目类
        '种植', '矫正', '正畸', '美白', '洗牙', '补牙', '拔牙', '根管', '牙冠', '贴面',
        '隐形', '金属', '陶瓷', '全瓷', '烤瓷', '牙周', '牙龈', '智齿',
        # 咨询类
        '价格', '多少钱', '费用', '优惠', '活动', '打折', '分期', '医保',
        # 预约类
        '预约', '挂号', '时间', '周末', '上班',
        # 医院/医生
        '医院', '医生', '专家', '推荐', '哪家', '好不好', '怎么样',
        # 症状类
        '疼', '痛', '松动', '出血', '发炎', '肿',
    ]

    for r in results:
        content = r.get('Content', '') or ''
        content = content.lower()

        for kw in business_keywords:
            if kw in content:
                keyword_count[kw] += 1

    # 排序
    sorted_keywords = sorted(keyword_count.items(), key=lambda x: x[1], reverse=True)[:30]

    return {
        'period_days': days,
        'analyzed_records': len(results),
        'keywords': [
            {'keyword': kw, 'count': count}
            for kw, count in sorted_keywords
        ]
    }


# ============================================================
# 综合分析报告
# ============================================================
def generate_source_report(source_keyword: str, days: int = None) -> Dict:
    """
    生成指定来源的综合分析报告

    Args:
        source_keyword: 来源关键词（如"牙舒丽"）
        days: 分析天数，默认本月，最大365天

    Returns:
        综合分析报告
    """
    days = normalize_days(days)

    # 获取来源ID
    source_ids = get_source_ids(source_keyword)

    if not source_ids:
        return {'error': f'未找到包含"{source_keyword}"的来源'}

    # 获取来源详情
    source_info = get_all_source_info()
    source_details = [
        {
            'id': sid,
            'name': source_info.get(sid, {}).get('name', '未知'),
            'type': classify_source(
                source_info.get(sid, {}).get('name', ''),
                source_info.get(sid, {}).get('parent_name', '')
            )
        }
        for sid in source_ids
    ]

    # 生成各维度分析
    report = {
        'source_keyword': source_keyword,
        'source_ids': source_ids,
        'source_details': source_details,
        'analysis_period_days': days,
        'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),

        # 各维度分析
        'region_distribution': analyze_region_distribution(source_ids, days),
        'project_distribution': analyze_project_distribution(source_ids, days),
        'hospital_distribution': analyze_hospital_distribution(source_ids, days),
        'source_type_comparison': analyze_source_type_comparison(source_keyword, days),
        'time_trend': analyze_time_trend(source_ids, days, 'week'),
        'consultation_keywords': analyze_consultation_keywords(source_ids, days),
    }

    return report


# ============================================================
# 命令行运行
# ============================================================
def print_report(report: Dict):
    """打印分析报告"""

    if 'error' in report:
        print(f"\n❌ 错误: {report['error']}")
        return

    print("\n" + "=" * 80)
    print(f"  📊 {report['source_keyword']} 来源综合分析报告")
    print(f"  分析周期: 最近 {report['analysis_period_days']} 天")
    print(f"  生成时间: {report['generated_at']}")
    print("=" * 80)

    # 来源详情
    print("\n📍 来源详情:")
    for s in report['source_details']:
        type_label = {'paid': '💰竞价', 'organic': '🌱自然', 'channel': '🤝渠道', 'unknown': '❓未知'}
        print(f"   - ID:{s['id']} {s['name']} [{type_label.get(s['type'], '未知')}]")

    # 地区分布
    print("\n" + "-" * 80)
    print("📍 地区分布 TOP10:")
    region = report.get('region_distribution', {})
    if 'regions' in region:
        print(f"   总客户: {region['total_customers']:,} | 成交: {region['total_converted']:,} | 转化率: {region['conversion_rate']}%")
        print(f"   {'省份':<12} {'客户数':<10} {'成交数':<10} {'转化率':<10} {'占比'}")
        for r in region['regions'][:10]:
            print(f"   {r['province']:<12} {r['count']:<10,} {r['converted']:<10} {r['rate']:<10}% {r['percentage']}%")

    # 意向项目
    print("\n" + "-" * 80)
    print("🦷 意向项目分布 TOP10:")
    project = report.get('project_distribution', {})
    if 'projects' in project:
        print(f"   {'项目':<20} {'客户数':<10} {'成交数':<10} {'转化率':<10} {'占比'}")
        for p in project['projects'][:10]:
            print(f"   {p['project']:<20} {p['count']:<10,} {p['converted']:<10} {p['rate']:<10}% {p['percentage']}%")

    # 医院分布
    print("\n" + "-" * 80)
    print("🏥 成交医院 TOP10:")
    hospital = report.get('hospital_distribution', {})
    if 'hospitals' in hospital:
        print(f"   总成交: {hospital['total_orders']} 单 | 总金额: ¥{hospital['total_amount']:,.0f}")
        print(f"   {'医院':<25} {'客户数':<8} {'单数':<8} {'金额':<12} {'均价'}")
        for h in hospital['hospitals'][:10]:
            print(f"   {h['hospital'][:24]:<25} {h['customers']:<8} {h['orders']:<8} ¥{h['amount']:<11,.0f} ¥{h['avg_amount']:,.0f}")

    # 来源类型对比
    print("\n" + "-" * 80)
    print("⚖️ 自然vs竞价对比:")
    comparison = report.get('source_type_comparison', {})
    if 'comparison' in comparison:
        for c in comparison['comparison']:
            print(f"   {c['type_name']}: {c['total']:,}客户 | {c['converted']}成交 | {c['rate']}%转化 | 占比{c['percentage']}%")

    # 时间趋势
    print("\n" + "-" * 80)
    print("📈 注册趋势 (最近几周):")
    trend = report.get('time_trend', {})
    if 'trend' in trend:
        for t in trend['trend'][-8:]:  # 最近8周
            bar = '█' * min(int(t['registrations'] / 100), 30)
            print(f"   {t['period']}: {t['registrations']:>6} 注册 | {t['conversions']:>4} 成交 | {t['rate']}% | {bar}")

    # 咨询热词
    print("\n" + "-" * 80)
    print("🔥 咨询热词 TOP15:")
    keywords = report.get('consultation_keywords', {})
    if 'keywords' in keywords:
        kw_list = keywords['keywords'][:15]
        kw_str = ', '.join([f"{k['keyword']}({k['count']})" for k in kw_list])
        print(f"   {kw_str}")

    print("\n" + "=" * 80)
    print("  报告结束")
    print("=" * 80 + "\n")


def main():
    import sys

    # 默认分析牙舒丽，默认分析本月，最大一年
    source_keyword = sys.argv[1] if len(sys.argv) > 1 else "牙舒丽"
    days = int(sys.argv[2]) if len(sys.argv) > 2 else None  # 默认本月

    actual_days = normalize_days(days)
    print(f"\n正在分析 [{source_keyword}] 的来源数据 (分析期限: {actual_days}天)...")

    try:
        report = generate_source_report(source_keyword, days)
        print_report(report)

    except Exception as e:
        print(f"\n❌ 分析出错: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()
