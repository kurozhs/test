#!/usr/bin/env python3
"""
分析来源类型：区分自然来源 vs 竞价来源
"""

import pymysql
from collections import defaultdict

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
    return pymysql.connect(**QUDAO_DB_CONFIG)


def query_all_sources_with_parent():
    """查询所有来源及其父级分类"""
    conn = get_connection()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            # 查询所有来源（keyid=5594）及其层级关系
            sql = """
                SELECT
                    l.linkageid,
                    l.name,
                    l.parentid,
                    p.name as parent_name,
                    p.parentid as grandparent_id
                FROM un_linkage l
                LEFT JOIN un_linkage p ON l.parentid = p.linkageid
                WHERE l.keyid = 5594
                ORDER BY l.parentid, l.linkageid
            """
            cursor.execute(sql)
            return cursor.fetchall()
    finally:
        conn.close()


def query_source_stats():
    """查询各来源的客户统计"""
    conn = get_connection()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            sql = """
                SELECT
                    c.from_type,
                    l.name as source_name,
                    l.parentid,
                    p.name as parent_name,
                    COUNT(*) as total_count,
                    SUM(CASE WHEN c.Status = 1 THEN 1 ELSE 0 END) as converted_count
                FROM un_channel_client c
                LEFT JOIN un_linkage l ON c.from_type = l.linkageid
                LEFT JOIN un_linkage p ON l.parentid = p.linkageid
                WHERE c.from_type > 0
                GROUP BY c.from_type, l.name, l.parentid, p.name
                HAVING total_count >= 100
                ORDER BY total_count DESC
            """
            cursor.execute(sql)
            return cursor.fetchall()
    finally:
        conn.close()


def classify_source(source_name: str, parent_name: str) -> tuple:
    """
    根据来源名称和父级名称判断是自然来源还是竞价来源

    Returns:
        (source_type, confidence, reason)
        source_type: 'paid' 竞价/付费 | 'organic' 自然 | 'unknown' 未知
    """
    if not source_name:
        return ('unknown', 'low', '来源名称为空')

    name = source_name.lower()
    parent = (parent_name or '').lower()

    # ============ 竞价/付费来源关键词 ============
    paid_keywords = [
        # 信息流广告平台
        '头条', '抖音', '快手', 'tiktok', '信息流',
        # 搜索竞价
        '竞价', '百度', '360', '搜狗', '神马', 'sem', 'ppc',
        # 社交广告
        '广点通', '腾讯广告', '朋友圈广告',
        # 明确标识
        '投放', '广告', '付费', 'ads', 'ad',
        # 特定渠道标识
        'bsg头条', 'bcg头条', 'omg头条', 'teg头条', 'cdg头条',
        'bsg快手', 'bcg快手', 'omg快手', 'teg快手',
        '小红书',  # 小红书投放
    ]

    # ============ 自然来源关键词 ============
    organic_keywords = [
        # 在线客服（用户主动咨询）
        'swt', '商务通', '在线咨询', '主站',
        # 电话咨询
        '400', '电话',
        # 微信自然流量
        '微信', '公众号', '小程序',
        # 口碑/转介绍
        '转介绍', '老带新', '口碑', '推荐',
        # 自然搜索/SEO
        'seo', '自然', '免费',
        # APP自然注册
        'app', '移动',
        # 数据导入（历史客户）
        '导入', '无忧导入',
        # 第三方平台自然流量
        '新氧', '柠檬', '美团', '大众点评',
    ]

    # ============ 混合/渠道来源 ============
    channel_keywords = [
        '渠道', '合作', '代理', '分销', '外部',
    ]

    # 优先检查竞价关键词
    for kw in paid_keywords:
        if kw in name or kw in parent:
            return ('paid', 'high', f'包含付费关键词: {kw}')

    # 检查自然流量关键词
    for kw in organic_keywords:
        if kw in name or kw in parent:
            # 特殊处理：某些看起来像自然但实际是付费的
            # 比如"微信广告"、"APP投放"等
            if any(pk in name for pk in ['广告', '投放', '付费']):
                return ('paid', 'medium', f'虽然包含{kw}但有付费标识')
            return ('organic', 'high', f'包含自然关键词: {kw}')

    # 检查渠道合作
    for kw in channel_keywords:
        if kw in name or kw in parent:
            return ('channel', 'medium', f'渠道合作: {kw}')

    # 根据父级分类判断
    parent_paid = ['头条抖音', '快手投放', '信息流广告', '竞价投放']
    parent_organic = ['商务通', '400电话', '微信', '主站引导', '系统注册', '无忧导入']

    if parent_name:
        for pp in parent_paid:
            if pp in parent_name:
                return ('paid', 'medium', f'父级为付费分类: {parent_name}')
        for po in parent_organic:
            if po in parent_name:
                return ('organic', 'medium', f'父级为自然分类: {parent_name}')

    return ('unknown', 'low', '无法确定')


def main():
    print("\n" + "=" * 80)
    print("  来源类型分析：自然来源 vs 竞价来源")
    print("=" * 80)

    # 获取所有来源统计
    sources = query_source_stats()

    # 分类统计
    paid_sources = []
    organic_sources = []
    channel_sources = []
    unknown_sources = []

    paid_total = 0
    paid_converted = 0
    organic_total = 0
    organic_converted = 0
    channel_total = 0
    channel_converted = 0

    for s in sources:
        source_type, confidence, reason = classify_source(s['source_name'], s['parent_name'])
        s['source_type'] = source_type
        s['confidence'] = confidence
        s['reason'] = reason

        total = s['total_count']
        converted = s['converted_count'] or 0

        if source_type == 'paid':
            paid_sources.append(s)
            paid_total += total
            paid_converted += converted
        elif source_type == 'organic':
            organic_sources.append(s)
            organic_total += total
            organic_converted += converted
        elif source_type == 'channel':
            channel_sources.append(s)
            channel_total += total
            channel_converted += converted
        else:
            unknown_sources.append(s)

    # 输出竞价来源
    print("\n" + "=" * 80)
    print(f"  💰 竞价/付费来源 (共 {len(paid_sources)} 个，{paid_total:,} 客户)")
    print("=" * 80)
    print(f"  {'ID':<8} {'来源名称':<25} {'父级分类':<15} {'客户数':<12} {'转化率':<8} {'判断依据'}")
    print("  " + "-" * 78)

    for s in paid_sources[:30]:
        name = (s['source_name'] or '')[:24]
        parent = (s['parent_name'] or '')[:14]
        rate = (s['converted_count'] or 0) / s['total_count'] * 100 if s['total_count'] > 0 else 0
        reason = s['reason'][:20] if len(s['reason']) > 20 else s['reason']
        print(f"  {s['from_type']:<8} {name:<25} {parent:<15} {s['total_count']:<12,} {rate:<8.1f}% {reason}")

    if len(paid_sources) > 30:
        print(f"  ... 还有 {len(paid_sources) - 30} 个来源")

    paid_rate = paid_converted / paid_total * 100 if paid_total > 0 else 0
    print("  " + "-" * 78)
    print(f"  {'小计':<8} {'':<25} {'':<15} {paid_total:<12,} {paid_rate:<8.1f}%")

    # 输出自然来源
    print("\n" + "=" * 80)
    print(f"  🌱 自然来源 (共 {len(organic_sources)} 个，{organic_total:,} 客户)")
    print("=" * 80)
    print(f"  {'ID':<8} {'来源名称':<25} {'父级分类':<15} {'客户数':<12} {'转化率':<8} {'判断依据'}")
    print("  " + "-" * 78)

    for s in organic_sources[:30]:
        name = (s['source_name'] or '')[:24]
        parent = (s['parent_name'] or '')[:14]
        rate = (s['converted_count'] or 0) / s['total_count'] * 100 if s['total_count'] > 0 else 0
        reason = s['reason'][:20] if len(s['reason']) > 20 else s['reason']
        print(f"  {s['from_type']:<8} {name:<25} {parent:<15} {s['total_count']:<12,} {rate:<8.1f}% {reason}")

    if len(organic_sources) > 30:
        print(f"  ... 还有 {len(organic_sources) - 30} 个来源")

    organic_rate = organic_converted / organic_total * 100 if organic_total > 0 else 0
    print("  " + "-" * 78)
    print(f"  {'小计':<8} {'':<25} {'':<15} {organic_total:<12,} {organic_rate:<8.1f}%")

    # 输出渠道来源
    print("\n" + "=" * 80)
    print(f"  🤝 渠道合作 (共 {len(channel_sources)} 个，{channel_total:,} 客户)")
    print("=" * 80)
    print(f"  {'ID':<8} {'来源名称':<25} {'父级分类':<15} {'客户数':<12} {'转化率':<8}")
    print("  " + "-" * 78)

    for s in channel_sources[:20]:
        name = (s['source_name'] or '')[:24]
        parent = (s['parent_name'] or '')[:14]
        rate = (s['converted_count'] or 0) / s['total_count'] * 100 if s['total_count'] > 0 else 0
        print(f"  {s['from_type']:<8} {name:<25} {parent:<15} {s['total_count']:<12,} {rate:<8.1f}%")

    channel_rate = channel_converted / channel_total * 100 if channel_total > 0 else 0
    print("  " + "-" * 78)
    print(f"  {'小计':<8} {'':<25} {'':<15} {channel_total:<12,} {channel_rate:<8.1f}%")

    # 输出未知来源
    if unknown_sources:
        print("\n" + "=" * 80)
        print(f"  ❓ 未分类来源 (共 {len(unknown_sources)} 个)")
        print("=" * 80)
        for s in unknown_sources[:15]:
            name = (s['source_name'] or '')[:24]
            parent = (s['parent_name'] or '')[:14]
            rate = (s['converted_count'] or 0) / s['total_count'] * 100 if s['total_count'] > 0 else 0
            print(f"  {s['from_type']:<8} {name:<25} {parent:<15} {s['total_count']:<12,} {rate:<8.1f}%")

    # 汇总对比
    print("\n" + "=" * 80)
    print("  📊 汇总对比")
    print("=" * 80)

    total_all = paid_total + organic_total + channel_total
    print(f"\n  {'来源类型':<15} {'来源数':<10} {'客户数':<15} {'占比':<10} {'成交数':<12} {'转化率'}")
    print("  " + "-" * 70)
    print(f"  {'💰 竞价/付费':<13} {len(paid_sources):<10} {paid_total:<15,} {paid_total/total_all*100:<10.1f}% {paid_converted:<12,} {paid_rate:.2f}%")
    print(f"  {'🌱 自然来源':<13} {len(organic_sources):<10} {organic_total:<15,} {organic_total/total_all*100:<10.1f}% {organic_converted:<12,} {organic_rate:.2f}%")
    print(f"  {'🤝 渠道合作':<13} {len(channel_sources):<10} {channel_total:<15,} {channel_total/total_all*100:<10.1f}% {channel_converted:<12,} {channel_rate:.2f}%")
    print("  " + "-" * 70)
    total_converted = paid_converted + organic_converted + channel_converted
    total_rate = total_converted / total_all * 100 if total_all > 0 else 0
    print(f"  {'合计':<15} {len(paid_sources)+len(organic_sources)+len(channel_sources):<10} {total_all:<15,} {'100%':<10} {total_converted:<12,} {total_rate:.2f}%")

    # 牙舒丽专项分析
    print("\n" + "=" * 80)
    print("  🦷 牙舒丽来源分析")
    print("=" * 80)

    ysl_sources = [s for s in sources if '牙舒丽' in (s['source_name'] or '')]
    for s in ysl_sources:
        source_type, confidence, reason = classify_source(s['source_name'], s['parent_name'])
        rate = (s['converted_count'] or 0) / s['total_count'] * 100 if s['total_count'] > 0 else 0
        type_label = {'paid': '💰竞价', 'organic': '🌱自然', 'channel': '🤝渠道', 'unknown': '❓未知'}
        print(f"  {s['from_type']:<8} {s['source_name']:<20} {type_label[source_type]:<8} {s['total_count']:>10,}客户 {rate:.1f}%转化")
        print(f"           父级: {s['parent_name'] or '无'} | 判断: {reason}")

    print("\n" + "=" * 80)
    print("  分析完成")
    print("=" * 80 + "\n")


if __name__ == "__main__":
    main()
