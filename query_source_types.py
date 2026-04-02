#!/usr/bin/env python3
"""
查询渠道系统中的来源类型
分析 un_linkage 表和 un_channel_client 表的 from_type 字段
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
    """获取数据库连接"""
    return pymysql.connect(**QUDAO_DB_CONFIG)


def query_linkage_categories():
    """查询 un_linkage 表的分类结构"""
    conn = get_connection()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            # 1. 查询所有 keyid 类型（分类）
            print("=" * 60)
            print("  1. un_linkage 表分类结构 (keyid)")
            print("=" * 60)

            sql = """
                SELECT keyid, COUNT(*) as count
                FROM un_linkage
                GROUP BY keyid
                ORDER BY keyid
            """
            cursor.execute(sql)
            categories = cursor.fetchall()

            for cat in categories:
                print(f"  keyid={cat['keyid']}: {cat['count']} 条记录")

            # 2. 查询来源相关的分类（通常 keyid 表示不同类型）
            print("\n" + "=" * 60)
            print("  2. 查找可能的来源分类")
            print("=" * 60)

            # 查询包含"来源"、"渠道"、"网站"等关键词的记录
            sql = """
                SELECT linkageid, keyid, parentid, name, arrchildid
                FROM un_linkage
                WHERE name LIKE '%来源%'
                   OR name LIKE '%渠道%'
                   OR name LIKE '%网站%'
                   OR name LIKE '%牙舒丽%'
                   OR name LIKE '%站点%'
                LIMIT 50
            """
            cursor.execute(sql)
            sources = cursor.fetchall()

            if sources:
                for s in sources:
                    print(f"  ID:{s['linkageid']} | keyid:{s['keyid']} | parent:{s['parentid']} | {s['name']}")
            else:
                print("  未找到明确的来源分类记录")

            return categories
    finally:
        conn.close()


def query_from_type_distribution():
    """查询客户表中 from_type 的分布"""
    conn = get_connection()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            print("\n" + "=" * 60)
            print("  3. un_channel_client 表 from_type 分布 (TOP 50)")
            print("=" * 60)

            sql = """
                SELECT
                    c.from_type,
                    l.name as source_name,
                    COUNT(*) as customer_count,
                    SUM(CASE WHEN c.Status = 1 THEN 1 ELSE 0 END) as converted_count
                FROM un_channel_client c
                LEFT JOIN un_linkage l ON c.from_type = l.linkageid
                WHERE c.from_type > 0
                GROUP BY c.from_type, l.name
                ORDER BY customer_count DESC
                LIMIT 50
            """
            cursor.execute(sql)
            results = cursor.fetchall()

            print(f"\n  {'ID':<8} {'来源名称':<30} {'客户数':<10} {'成交数':<10} {'转化率'}")
            print("  " + "-" * 75)

            total_customers = 0
            total_converted = 0

            for r in results:
                source_name = r['source_name'] or f"未知({r['from_type']})"
                count = r['customer_count']
                converted = r['converted_count'] or 0
                rate = (converted / count * 100) if count > 0 else 0
                total_customers += count
                total_converted += converted

                print(f"  {r['from_type']:<8} {source_name:<30} {count:<10} {converted:<10} {rate:.1f}%")

            print("  " + "-" * 75)
            total_rate = (total_converted / total_customers * 100) if total_customers > 0 else 0
            print(f"  {'合计':<8} {'':<30} {total_customers:<10} {total_converted:<10} {total_rate:.1f}%")

            return results
    finally:
        conn.close()


def query_linkage_by_keyid(keyid: int, limit: int = 30):
    """查询指定 keyid 下的所有选项"""
    conn = get_connection()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            sql = """
                SELECT linkageid, parentid, name, arrchildid
                FROM un_linkage
                WHERE keyid = %s
                ORDER BY parentid, linkageid
                LIMIT %s
            """
            cursor.execute(sql, (keyid, limit))
            return cursor.fetchall()
    finally:
        conn.close()


def query_top_level_linkages():
    """查询顶级分类（parentid=0）"""
    conn = get_connection()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            print("\n" + "=" * 60)
            print("  4. 顶级分类 (parentid=0)")
            print("=" * 60)

            sql = """
                SELECT keyid, linkageid, name, arrchildid
                FROM un_linkage
                WHERE parentid = 0
                ORDER BY keyid, linkageid
                LIMIT 100
            """
            cursor.execute(sql)
            results = cursor.fetchall()

            # 按 keyid 分组显示
            grouped = defaultdict(list)
            for r in results:
                grouped[r['keyid']].append(r)

            for keyid, items in sorted(grouped.items()):
                print(f"\n  [keyid={keyid}]")
                for item in items[:10]:  # 每组最多显示10个
                    has_children = "有子项" if item['arrchildid'] else ""
                    print(f"    - ID:{item['linkageid']} {item['name']} {has_children}")
                if len(items) > 10:
                    print(f"    ... 还有 {len(items) - 10} 项")

            return results
    finally:
        conn.close()


def query_source_hierarchy():
    """尝试查找来源的层级结构"""
    conn = get_connection()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            print("\n" + "=" * 60)
            print("  5. 尝试分析来源层级结构")
            print("=" * 60)

            # 先获取使用最多的 from_type
            sql = """
                SELECT from_type, COUNT(*) as cnt
                FROM un_channel_client
                WHERE from_type > 0
                GROUP BY from_type
                ORDER BY cnt DESC
                LIMIT 10
            """
            cursor.execute(sql)
            top_types = cursor.fetchall()

            if top_types:
                type_ids = [t['from_type'] for t in top_types]
                placeholders = ','.join(['%s'] * len(type_ids))

                # 查询这些 from_type 对应的 linkage 信息
                sql = f"""
                    SELECT linkageid, keyid, parentid, name
                    FROM un_linkage
                    WHERE linkageid IN ({placeholders})
                """
                cursor.execute(sql, type_ids)
                linkages = cursor.fetchall()

                print("\n  TOP10 来源对应的 linkage 信息:")
                for l in linkages:
                    print(f"    ID:{l['linkageid']} keyid:{l['keyid']} parent:{l['parentid']} -> {l['name']}")

                # 获取这些记录的 keyid，查看同类下有哪些其他选项
                if linkages:
                    keyids = list(set(l['keyid'] for l in linkages))
                    print(f"\n  这些来源属于 keyid: {keyids}")

                    for kid in keyids[:3]:  # 最多查3个 keyid
                        print(f"\n  keyid={kid} 下的所有来源:")
                        items = query_linkage_by_keyid(kid, 50)
                        for item in items:
                            indent = "    " if item['parentid'] == 0 else "      "
                            print(f"{indent}- ID:{item['linkageid']} {item['name']}")

    finally:
        conn.close()


def main():
    print("\n" + "=" * 60)
    print("  渠道来源类型分析报告")
    print("=" * 60)

    try:
        # 1. 查询分类结构
        query_linkage_categories()

        # 2. 查询 from_type 分布
        query_from_type_distribution()

        # 3. 查询顶级分类
        query_top_level_linkages()

        # 4. 分析来源层级
        query_source_hierarchy()

        print("\n" + "=" * 60)
        print("  分析完成")
        print("=" * 60 + "\n")

    except Exception as e:
        print(f"\n❌ 查询出错: {e}")
        print("\n请检查:")
        print("  1. 数据库连接是否正常")
        print("  2. 网络是否能访问内网 192.168.103.227")


if __name__ == "__main__":
    main()
