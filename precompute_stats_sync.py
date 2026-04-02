#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
预计算统计数据同步服务
将渠道系统的聚合统计数据预计算后存入本地 hospital_db，加速客户分析查询。

预计算内容：
1. 留电时段转化率 (precomputed_hourly_conversion)
2. 同类客户转化率 (precomputed_conversion_rate)
3. 成交周期统计 (precomputed_conversion_cycle)
4. 地区医院实力 (precomputed_district_hospital)

使用方式:
    python precompute_stats_sync.py --full     # 全量预计算
    python precompute_stats_sync.py --hourly   # 只跑留电时段
    python precompute_stats_sync.py --rates    # 只跑转化率
    python precompute_stats_sync.py --cycles   # 只跑成交周期
    python precompute_stats_sync.py --district # 只跑地区医院
    python precompute_stats_sync.py --status   # 查看任务状态

日期: 2026-03-02
"""

import json
import logging
import os
import time
from datetime import datetime
from typing import Any, Dict, List, Tuple

import pymysql
from pymysql.cursors import DictCursor
from dbutils.pooled_db import PooledDB
from dotenv import load_dotenv

load_dotenv()

# ============================================================
# 配置
# ============================================================

QUDAO_DB_CONFIG = {
    "host": os.getenv("QUDAO_DB_HOST", ""),
    "port": int(os.getenv("QUDAO_DB_PORT", "3306")),
    "user": os.getenv("QUDAO_DB_USER", ""),
    "password": os.getenv("QUDAO_DB_PASSWORD", ""),
    "database": os.getenv("QUDAO_DB_NAME", ""),
}

HOSPITAL_DB_CONFIG = {
    "host": os.getenv("DB_HOST", "localhost"),
    "port": int(os.getenv("DB_PORT", "3306")),
    "user": os.getenv("DB_USER", "root"),
    "password": os.getenv("DB_PASSWORD", ""),
    "database": os.getenv("DB_NAME", "hospital_db"),
}

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('precompute_sync.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ============================================================
# 数据库连接池
# ============================================================

_qudao_pool = None
_hospital_pool = None


def get_qudao_pool():
    global _qudao_pool
    if _qudao_pool is None:
        _qudao_pool = PooledDB(
            creator=pymysql, maxconnections=5, mincached=1, maxcached=3,
            blocking=True, **QUDAO_DB_CONFIG, charset='utf8mb4'
        )
    return _qudao_pool


def get_hospital_pool():
    global _hospital_pool
    if _hospital_pool is None:
        _hospital_pool = PooledDB(
            creator=pymysql, maxconnections=5, mincached=1, maxcached=3,
            blocking=True, **HOSPITAL_DB_CONFIG, charset='utf8mb4'
        )
    return _hospital_pool


def query_qudao(sql: str, params: tuple = None) -> List[Dict]:
    pool = get_qudao_pool()
    conn = pool.connection()
    try:
        with conn.cursor(DictCursor) as cursor:
            cursor.execute(sql, params)
            return list(cursor.fetchall())
    finally:
        conn.close()


def query_hospital(sql: str, params: tuple = None) -> List[Dict]:
    pool = get_hospital_pool()
    conn = pool.connection()
    try:
        with conn.cursor(DictCursor) as cursor:
            cursor.execute(sql, params)
            return list(cursor.fetchall())
    finally:
        conn.close()


def execute_hospital(sql: str, params: tuple = None) -> int:
    pool = get_hospital_pool()
    conn = pool.connection()
    try:
        with conn.cursor() as cursor:
            result = cursor.execute(sql, params)
            conn.commit()
            return result
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()


def execute_hospital_many(sql: str, params_list: List[tuple]) -> int:
    """批量执行写入"""
    pool = get_hospital_pool()
    conn = pool.connection()
    try:
        with conn.cursor() as cursor:
            result = cursor.executemany(sql, params_list)
            conn.commit()
            return result
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()


# ============================================================
# 任务日志
# ============================================================

def log_job_start(job_type: str) -> int:
    execute_hospital(
        "INSERT INTO precompute_job_log (job_type, start_time, status) VALUES (%s, NOW(), 'running')",
        (job_type,)
    )
    rows = query_hospital("SELECT LAST_INSERT_ID() as id")
    return int(rows[0]['id']) if rows else 0


def log_job_end(job_id: int, rows_affected: int, duration: float, status: str = 'success', error: str = None):
    execute_hospital(
        """UPDATE precompute_job_log
           SET end_time=NOW(), rows_affected=%s, duration_seconds=%s, status=%s, error_message=%s
           WHERE id=%s""",
        (rows_affected, round(duration, 2), status, error, job_id)
    )


# ============================================================
# 维度发现
# ============================================================

def discover_active_intentions() -> List[Dict]:
    """查询所有有客户数据的 (intention_id, region_id, client_region) 组合"""
    sql = """
        SELECT PlasticsIntention as intention_id,
               zx_District as region_id,
               client_region,
               COUNT(*) as cnt
        FROM un_channel_client
        WHERE PlasticsIntention > 0
          AND RegisterTime >= UNIX_TIMESTAMP(DATE_SUB(NOW(), INTERVAL 365 DAY))
        GROUP BY PlasticsIntention, zx_District, client_region
        HAVING COUNT(*) >= 1
    """
    return query_qudao(sql)


def discover_parent_siblings() -> Dict[int, Dict]:
    """查询 un_linkage 父子关系，返回 {intention_id: {parent_id, parent_name, sibling_ids}}"""
    sql = """
        SELECT child.linkageid as child_id,
               child.parentid as parent_id,
               parent.name as parent_name,
               parent.arrchildid
        FROM un_linkage child
        JOIN un_linkage parent ON child.parentid = parent.linkageid
        WHERE child.parentid > 0
    """
    rows = query_qudao(sql)
    result = {}
    for r in rows:
        child_id = int(r['child_id'])
        parent_id = int(r['parent_id'])
        arrchildid = r.get('arrchildid', '') or ''
        sibling_ids = []
        if arrchildid:
            try:
                sibling_ids = [int(x.strip()) for x in arrchildid.split(',') if x.strip().isdigit()]
            except (ValueError, AttributeError):
                pass
        result[child_id] = {
            'parent_id': parent_id,
            'parent_name': r.get('parent_name', ''),
            'sibling_ids': sibling_ids,
        }
    return result


def discover_active_districts() -> List[Dict]:
    """查询有客户数据的 (zx_district, client_region) 组合"""
    sql = """
        SELECT zx_District as zx_district, client_region, COUNT(*) as cnt
        FROM un_channel_client
        WHERE zx_District > 0
          AND RegisterTime >= UNIX_TIMESTAMP(DATE_SUB(NOW(), INTERVAL 365 DAY))
        GROUP BY zx_District, client_region
        HAVING COUNT(*) >= 1
    """
    return query_qudao(sql)


def get_linkage_names(ids: List[int]) -> Dict[int, str]:
    """批量查询 linkage 名称"""
    if not ids:
        return {}
    placeholders = ','.join(['%s'] * len(ids))
    sql = f"SELECT linkageid, name FROM un_linkage WHERE linkageid IN ({placeholders})"
    rows = query_qudao(sql, tuple(ids))
    return {int(r['linkageid']): r['name'] for r in rows}


# ============================================================
# 1. 留电时段转化率
# ============================================================

def sync_hourly_conversion():
    """预计算各业务线 + 全业务线的24小时转化率"""
    logger.info("=" * 50)
    logger.info("开始预计算: 留电时段转化率")
    job_id = log_job_start('hourly')
    t0 = time.time()
    total_rows = 0

    try:
        # 各业务线
        client_regions = [0, 1, 2, 4, 8]  # 0=全业务线

        for cr in client_regions:
            where = "RegisterTime >= UNIX_TIMESTAMP(DATE_SUB(NOW(), INTERVAL 365 DAY)) AND RegisterTime > 0"
            params = ()
            if cr > 0:
                where += " AND client_region = %s"
                params = (cr,)

            sql = f"""
                SELECT HOUR(FROM_UNIXTIME(RegisterTime)) as reg_hour,
                       COUNT(*) as total,
                       SUM(CASE WHEN Status = 1 THEN 1 ELSE 0 END) as converted
                FROM un_channel_client
                WHERE {where}
                GROUP BY reg_hour
                ORDER BY reg_hour
            """
            data = query_qudao(sql, params)

            if not data:
                continue

            upsert_sql = """
                INSERT INTO precomputed_hourly_conversion
                    (client_region, register_hour, total, converted, rate, updated_at)
                VALUES (%s, %s, %s, %s, %s, NOW())
                ON DUPLICATE KEY UPDATE
                    total=VALUES(total), converted=VALUES(converted),
                    rate=VALUES(rate), updated_at=NOW()
            """
            batch = []
            for row in data:
                h = int(row.get('reg_hour', 0) or 0)
                t = int(row.get('total', 0) or 0)
                c = int(row.get('converted', 0) or 0)
                rate = c / t if t > 0 else 0
                batch.append((cr, h, t, c, round(rate, 6)))

            if batch:
                execute_hospital_many(upsert_sql, batch)
                total_rows += len(batch)

            logger.info(f"  client_region={cr}: {len(batch)} 条时段数据")

        duration = time.time() - t0
        log_job_end(job_id, total_rows, duration)
        logger.info(f"留电时段转化率完成: {total_rows}行, {duration:.1f}秒")
        return total_rows

    except Exception as e:
        log_job_end(job_id, total_rows, time.time() - t0, 'error', str(e))
        logger.error(f"留电时段转化率失败: {e}")
        raise


# ============================================================
# 2. 同类转化率
# ============================================================

def _bucketed_time_query_sync(where_base: str, params: tuple) -> Dict:
    """分桶查询: 一条SQL算出 d90/d180/d365/all 的转化计数"""
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
    data = query_qudao(sql, params)
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


def sync_conversion_rates():
    """预计算所有活跃维度组合的转化率"""
    logger.info("=" * 50)
    logger.info("开始预计算: 同类转化率")
    job_id = log_job_start('rates')
    t0 = time.time()
    total_rows = 0

    try:
        combos = discover_active_intentions()
        parent_map = discover_parent_siblings()
        logger.info(f"  发现 {len(combos)} 个活跃(intention, region, cr)组合")

        # 收集需要计算的维度
        upsert_sql = """
            INSERT INTO precomputed_conversion_rate
                (dimension_type, intention_id, region_id, client_region, parent_id, time_window,
                 total, converted, rate, sample_sufficient, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
            ON DUPLICATE KEY UPDATE
                total=VALUES(total), converted=VALUES(converted), rate=VALUES(rate),
                sample_sufficient=VALUES(sample_sufficient), updated_at=NOW()
        """

        batch = []
        seen_precise = set()
        seen_overall = set()
        seen_sibling = set()
        seen_business = set()

        for combo in combos:
            intention = int(combo.get('intention_id', 0) or 0)
            region = int(combo.get('region_id', 0) or 0)
            cr = int(combo.get('client_region', 0) or 0)

            if intention <= 0:
                continue

            # Precise: same intention + same region
            pkey = (intention, region, cr)
            if pkey not in seen_precise and region > 0:
                seen_precise.add(pkey)
                buckets = _bucketed_time_query_sync(
                    "PlasticsIntention = %s AND (zx_District = %s OR client_region = %s)",
                    (intention, region, region)
                )
                for tw, data in buckets.items():
                    batch.append(('precise', intention, region, cr, 0, tw,
                                  data['total'], data['converted'],
                                  round(data['rate'], 6), 1 if data['total'] >= 10 else 0))

            # Overall: same intention only
            okey = (intention,)
            if okey not in seen_overall:
                seen_overall.add(okey)
                buckets = _bucketed_time_query_sync(
                    "PlasticsIntention = %s",
                    (intention,)
                )
                for tw, data in buckets.items():
                    batch.append(('overall', intention, 0, 0, 0, tw,
                                  data['total'], data['converted'],
                                  round(data['rate'], 6), 1 if data['total'] >= 10 else 0))

            # Sibling: parent category
            pinfo = parent_map.get(intention)
            if pinfo and pinfo['sibling_ids'] and len(pinfo['sibling_ids']) > 1:
                parent_id = pinfo['parent_id']
                skey = (parent_id,)
                if skey not in seen_sibling:
                    seen_sibling.add(skey)
                    placeholders = ','.join(['%s'] * len(pinfo['sibling_ids']))
                    buckets = _bucketed_time_query_sync(
                        f"PlasticsIntention IN ({placeholders})",
                        tuple(pinfo['sibling_ids'])
                    )
                    for tw, data in buckets.items():
                        batch.append(('sibling', 0, 0, 0, parent_id, tw,
                                      data['total'], data['converted'],
                                      round(data['rate'], 6), 1 if data['total'] >= 10 else 0))

            # Business line
            if cr > 0:
                bkey = (cr,)
                if bkey not in seen_business:
                    seen_business.add(bkey)
                    buckets = _bucketed_time_query_sync(
                        "client_region = %s",
                        (cr,)
                    )
                    for tw, data in buckets.items():
                        batch.append(('business_line', 0, 0, cr, 0, tw,
                                      data['total'], data['converted'],
                                      round(data['rate'], 6), 1 if data['total'] >= 10 else 0))

            # 每500行写一批
            if len(batch) >= 500:
                execute_hospital_many(upsert_sql, batch)
                total_rows += len(batch)
                batch = []

        # 写入剩余
        if batch:
            execute_hospital_many(upsert_sql, batch)
            total_rows += len(batch)

        duration = time.time() - t0
        log_job_end(job_id, total_rows, duration)
        logger.info(f"同类转化率完成: {total_rows}行, {duration:.1f}秒")
        logger.info(f"  precise={len(seen_precise)}, overall={len(seen_overall)}, "
                     f"sibling={len(seen_sibling)}, business_line={len(seen_business)}")
        return total_rows

    except Exception as e:
        log_job_end(job_id, total_rows, time.time() - t0, 'error', str(e))
        logger.error(f"同类转化率失败: {e}")
        raise


# ============================================================
# 3. 成交周期
# ============================================================

def _calculate_cycle_stats(days_list: list) -> Dict:
    """纯Python计算成交周期统计（同 dental_chat_api.py 中的逻辑）"""
    if not days_list:
        return {
            'sample_size': 0,
            'distribution': {'0-7': 0, '8-14': 0, '15-30': 0, '31-60': 0, '61-90': 0, '90+': 0},
            'avg': 0, 'median': 0, 'p25': 0, 'p75': 0, 'min': 0, 'max': 0
        }

    sorted_days = sorted(days_list)
    n = len(sorted_days)

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

    def percentile(data, p):
        k = (len(data) - 1) * (p / 100.0)
        f = int(k)
        c = f + 1
        if c >= len(data):
            return data[-1]
        return data[f] + (k - f) * (data[c] - data[f])

    return {
        'sample_size': n,
        'distribution': distribution,
        'avg': round(sum(sorted_days) / n, 1),
        'median': round(percentile(sorted_days, 50), 1),
        'p25': round(percentile(sorted_days, 25), 1),
        'p75': round(percentile(sorted_days, 75), 1),
        'min': sorted_days[0],
        'max': sorted_days[-1]
    }


def _bucketed_cycle_query_sync(where_base: str, params: tuple) -> Dict:
    """一条SQL查出成交周期天数，Python端按注册时间分桶"""
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
    data = query_qudao(sql, params)
    if not data:
        empty = _calculate_cycle_stats([])
        return {'d90': empty.copy(), 'd180': empty.copy(), 'd365': empty.copy(), 'all': empty.copy()}

    now_ts = int(time.time())
    cutoffs = {
        'd90': now_ts - 90 * 86400,
        'd180': now_ts - 180 * 86400,
        'd365': now_ts - 365 * 86400,
    }

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


def sync_conversion_cycles():
    """预计算所有活跃维度组合的成交周期"""
    logger.info("=" * 50)
    logger.info("开始预计算: 成交周期")
    job_id = log_job_start('cycles')
    t0 = time.time()
    total_rows = 0

    try:
        combos = discover_active_intentions()
        parent_map = discover_parent_siblings()
        logger.info(f"  发现 {len(combos)} 个活跃组合")

        upsert_sql = """
            INSERT INTO precomputed_conversion_cycle
                (dimension_type, intention_id, region_id, client_region, parent_id, time_window,
                 sample_size, avg_days, median_days, p25_days, p75_days, min_days, max_days,
                 distribution_json, sample_sufficient, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
            ON DUPLICATE KEY UPDATE
                sample_size=VALUES(sample_size), avg_days=VALUES(avg_days),
                median_days=VALUES(median_days), p25_days=VALUES(p25_days), p75_days=VALUES(p75_days),
                min_days=VALUES(min_days), max_days=VALUES(max_days),
                distribution_json=VALUES(distribution_json),
                sample_sufficient=VALUES(sample_sufficient), updated_at=NOW()
        """

        batch = []
        seen_precise = set()
        seen_overall = set()
        seen_sibling = set()
        seen_business = set()

        for combo in combos:
            intention = int(combo.get('intention_id', 0) or 0)
            region = int(combo.get('region_id', 0) or 0)
            cr = int(combo.get('client_region', 0) or 0)

            if intention <= 0:
                continue

            # Precise
            pkey = (intention, region, cr)
            if pkey not in seen_precise and region > 0:
                seen_precise.add(pkey)
                buckets = _bucketed_cycle_query_sync(
                    "c.PlasticsIntention = %s AND (c.zx_District = %s OR c.client_region = %s)",
                    (intention, region, region)
                )
                for tw, stats in buckets.items():
                    dist_json = json.dumps(stats['distribution'], ensure_ascii=False)
                    batch.append(('precise', intention, region, cr, 0, tw,
                                  stats['sample_size'], stats['avg'], stats['median'],
                                  stats['p25'], stats['p75'], stats['min'], stats['max'],
                                  dist_json, 1 if stats['sample_size'] >= 10 else 0))

            # Overall
            okey = (intention,)
            if okey not in seen_overall:
                seen_overall.add(okey)
                buckets = _bucketed_cycle_query_sync(
                    "c.PlasticsIntention = %s",
                    (intention,)
                )
                for tw, stats in buckets.items():
                    dist_json = json.dumps(stats['distribution'], ensure_ascii=False)
                    batch.append(('overall', intention, 0, 0, 0, tw,
                                  stats['sample_size'], stats['avg'], stats['median'],
                                  stats['p25'], stats['p75'], stats['min'], stats['max'],
                                  dist_json, 1 if stats['sample_size'] >= 10 else 0))

            # Sibling
            pinfo = parent_map.get(intention)
            if pinfo and pinfo['sibling_ids'] and len(pinfo['sibling_ids']) > 1:
                parent_id = pinfo['parent_id']
                skey = (parent_id,)
                if skey not in seen_sibling:
                    seen_sibling.add(skey)
                    placeholders = ','.join(['%s'] * len(pinfo['sibling_ids']))
                    buckets = _bucketed_cycle_query_sync(
                        f"c.PlasticsIntention IN ({placeholders})",
                        tuple(pinfo['sibling_ids'])
                    )
                    for tw, stats in buckets.items():
                        dist_json = json.dumps(stats['distribution'], ensure_ascii=False)
                        batch.append(('sibling', 0, 0, 0, parent_id, tw,
                                      stats['sample_size'], stats['avg'], stats['median'],
                                      stats['p25'], stats['p75'], stats['min'], stats['max'],
                                      dist_json, 1 if stats['sample_size'] >= 10 else 0))

            # Business line
            if cr > 0:
                bkey = (cr,)
                if bkey not in seen_business:
                    seen_business.add(bkey)
                    buckets = _bucketed_cycle_query_sync(
                        "c.client_region = %s",
                        (cr,)
                    )
                    for tw, stats in buckets.items():
                        dist_json = json.dumps(stats['distribution'], ensure_ascii=False)
                        batch.append(('business_line', 0, 0, cr, 0, tw,
                                      stats['sample_size'], stats['avg'], stats['median'],
                                      stats['p25'], stats['p75'], stats['min'], stats['max'],
                                      dist_json, 1 if stats['sample_size'] >= 10 else 0))

            if len(batch) >= 500:
                execute_hospital_many(upsert_sql, batch)
                total_rows += len(batch)
                batch = []

        if batch:
            execute_hospital_many(upsert_sql, batch)
            total_rows += len(batch)

        duration = time.time() - t0
        log_job_end(job_id, total_rows, duration)
        logger.info(f"成交周期完成: {total_rows}行, {duration:.1f}秒")
        return total_rows

    except Exception as e:
        log_job_end(job_id, total_rows, time.time() - t0, 'error', str(e))
        logger.error(f"成交周期失败: {e}")
        raise


# ============================================================
# 4. 地区医院实力
# ============================================================

def sync_district_hospitals():
    """预计算各地区的医院成交能力"""
    logger.info("=" * 50)
    logger.info("开始预计算: 地区医院实力")
    job_id = log_job_start('district')
    t0 = time.time()
    total_rows = 0

    try:
        districts = discover_active_districts()
        logger.info(f"  发现 {len(districts)} 个活跃(district, cr)组合")

        # 批量获取地区名称
        district_ids = list(set(int(d.get('zx_district', 0) or 0) for d in districts))
        district_ids = [d for d in district_ids if d > 0]
        name_map = get_linkage_names(district_ids) if district_ids else {}

        upsert_sql = """
            INSERT INTO precomputed_district_hospital
                (zx_district, client_region, district_name, total_hospitals, total_deals,
                 total_amount, hospitals_with_repeat, district_score, hospitals_json, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
            ON DUPLICATE KEY UPDATE
                district_name=VALUES(district_name), total_hospitals=VALUES(total_hospitals),
                total_deals=VALUES(total_deals), total_amount=VALUES(total_amount),
                hospitals_with_repeat=VALUES(hospitals_with_repeat),
                district_score=VALUES(district_score), hospitals_json=VALUES(hospitals_json),
                updated_at=NOW()
        """

        batch = []
        seen = set()

        for combo in districts:
            zx_district = int(combo.get('zx_district', 0) or 0)
            cr = int(combo.get('client_region', 0) or 0)

            if zx_district <= 0:
                continue

            dkey = (zx_district, cr)
            if dkey in seen:
                continue
            seen.add(dkey)

            district_name = name_map.get(zx_district, '未知')

            # 成交统计
            filter_col = 'zx_District'
            filter_val = zx_district
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
                  AND p.addtime >= UNIX_TIMESTAMP(DATE_SUB(NOW(), INTERVAL 90 DAY))
                  AND hc.companyname NOT LIKE '%%韩国业务部%%'
                GROUP BY p.hospital_id
                ORDER BY deal_count DESC
                LIMIT 10
            """
            deal_data = query_qudao(sql_deals, (filter_val,))

            # 二开统计
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
            repeat_data = query_qudao(sql_repeat, (filter_val,))

            # 合并
            repeat_map = {}
            for r in (repeat_data or []):
                hid = int(r.get('hospital_id', 0) or 0)
                repeat_map[hid] = int(r.get('repeat_client_count', 0) or 0)

            hospitals = []
            total_deals = 0
            total_amount = 0
            hospitals_with_repeat = 0

            for h in (deal_data or []):
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

            # district_score
            score = 0
            if len(hospitals) > 0:
                score += min(len(hospitals) * 10, 30)
            if total_deals >= 20:
                score += 40
            elif total_deals >= 10:
                score += 30
            elif total_deals >= 5:
                score += 20
            elif total_deals >= 1:
                score += 10
            if hospitals_with_repeat >= 3:
                score += 30
            elif hospitals_with_repeat >= 1:
                score += 20

            hospitals_json = json.dumps(hospitals, ensure_ascii=False)
            batch.append((zx_district, cr, district_name, len(hospitals), total_deals,
                         total_amount, hospitals_with_repeat, score, hospitals_json))

            if len(batch) >= 200:
                execute_hospital_many(upsert_sql, batch)
                total_rows += len(batch)
                batch = []

        if batch:
            execute_hospital_many(upsert_sql, batch)
            total_rows += len(batch)

        duration = time.time() - t0
        log_job_end(job_id, total_rows, duration)
        logger.info(f"地区医院实力完成: {total_rows}行, {duration:.1f}秒")
        return total_rows

    except Exception as e:
        log_job_end(job_id, total_rows, time.time() - t0, 'error', str(e))
        logger.error(f"地区医院实力失败: {e}")
        raise


# ============================================================
# 全量预计算
# ============================================================

def run_full():
    """运行全量预计算"""
    logger.info("=" * 60)
    logger.info("       开始全量预计算")
    logger.info("=" * 60)
    t0 = time.time()
    job_id = log_job_start('full')
    total = 0
    errors = []

    steps = [
        ('留电时段', sync_hourly_conversion),
        ('同类转化率', sync_conversion_rates),
        ('成交周期', sync_conversion_cycles),
        ('地区医院', sync_district_hospitals),
    ]

    for name, func in steps:
        try:
            rows = func()
            total += rows
        except Exception as e:
            errors.append(f"{name}: {e}")
            logger.error(f"{name} 失败: {e}")

    duration = time.time() - t0
    status = 'success' if not errors else 'error'
    error_msg = '; '.join(errors) if errors else None
    log_job_end(job_id, total, duration, status, error_msg)

    logger.info("=" * 60)
    logger.info(f"全量预计算完成: {total}行, {duration:.1f}秒, 错误{len(errors)}个")
    if errors:
        for e in errors:
            logger.error(f"  {e}")
    logger.info("=" * 60)
    return total


# ============================================================
# 状态查看
# ============================================================

def show_status():
    """显示最近预计算任务状态"""
    rows = query_hospital("""
        SELECT job_type, start_time, end_time, rows_affected,
               duration_seconds, status, error_message
        FROM precompute_job_log
        ORDER BY id DESC LIMIT 20
    """)

    if not rows:
        print("暂无预计算记录")
        return

    print(f"\n{'='*70}")
    print("  最近预计算任务")
    print(f"{'='*70}")

    for r in rows:
        status_icon = '✅' if r['status'] == 'success' else '❌' if r['status'] == 'error' else '⏳'
        print(f"\n  {status_icon} [{r['start_time']}] {r['job_type']} - {r['status']}")
        print(f"     行数: {r['rows_affected']}  耗时: {r['duration_seconds']}秒")
        if r.get('error_message'):
            print(f"     错误: {r['error_message'][:100]}")

    # 各表行数
    print(f"\n{'='*70}")
    print("  预计算表数据量")
    print(f"{'='*70}")

    tables = [
        ('precomputed_hourly_conversion', '留电时段'),
        ('precomputed_conversion_rate', '同类转化率'),
        ('precomputed_conversion_cycle', '成交周期'),
        ('precomputed_district_hospital', '地区医院'),
    ]
    for tname, desc in tables:
        try:
            cnt = query_hospital(f"SELECT COUNT(*) as cnt FROM {tname}")
            c = cnt[0]['cnt'] if cnt else 0
            # 最后更新时间
            latest = query_hospital(f"SELECT MAX(updated_at) as t FROM {tname}")
            t = latest[0]['t'] if latest and latest[0]['t'] else '无'
            print(f"  {desc} ({tname}): {c}行, 最后更新: {t}")
        except Exception as e:
            print(f"  {desc} ({tname}): 查询失败 - {e}")


# ============================================================
# CLI
# ============================================================

def main():
    import argparse

    parser = argparse.ArgumentParser(description='预计算统计数据同步服务')
    parser.add_argument('--full', action='store_true', help='全量预计算')
    parser.add_argument('--hourly', action='store_true', help='只跑留电时段')
    parser.add_argument('--rates', action='store_true', help='只跑同类转化率')
    parser.add_argument('--cycles', action='store_true', help='只跑成交周期')
    parser.add_argument('--district', action='store_true', help='只跑地区医院')
    parser.add_argument('--status', action='store_true', help='查看任务状态')

    args = parser.parse_args()

    print("\n" + "=" * 60)
    print("        预计算统计数据同步服务")
    print(f"        {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    if args.status:
        show_status()
        return

    if args.hourly:
        sync_hourly_conversion()
    elif args.rates:
        sync_conversion_rates()
    elif args.cycles:
        sync_conversion_cycles()
    elif args.district:
        sync_district_hospitals()
    elif args.full:
        run_full()
    else:
        # 默认全量
        run_full()


if __name__ == '__main__':
    main()
