#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
医院数据同步服务 v2.0
将客服系统(qudao)的医院数据同步到本地知识库(hospital_db)

核心功能：
1. 增量同步 - 新增医院自动同步
2. 减量同步 - 删除/停用医院自动标记
3. 修改同步 - 变更数据自动更新
4. 冲突保护 - 手动编辑的数据可锁定保护
5. 变更追溯 - 记录所有变更历史

数据源: qudao数据库 (un_hospital_company + 关联表)
目标: hospital_db.robot_kb_hospitals

使用方式:
    python hospital_data_sync.py --full     # 全量同步
    python hospital_data_sync.py            # 增量同步
    python hospital_data_sync.py --status   # 查看同步状态
    python hospital_data_sync.py --check    # 检查待同步数据（不执行）

作者: 数据同步服务
日期: 2026-01-23
"""

import hashlib
import json
import logging
import os
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from decimal import Decimal

import pymysql
from pymysql.cursors import DictCursor
from dbutils.pooled_db import PooledDB
from dotenv import load_dotenv

# 加载环境变量
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

# 日志配置
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('hospital_sync.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


# ============================================================
# 映射配置
# ============================================================

HOSPITAL_TYPE_MAP = {
    1: "综合口腔", 2: "专科口腔", 3: "口腔诊所",
    4: "口腔医院", 5: "综合医院口腔科", 0: "其他",
}

COOPERATION_STATUS_MAP = {
    1: "合作中", 2: "暂停合作", 3: "终止合作", 0: "未合作",
}


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
            return cursor.fetchall()
    finally:
        conn.close()


def query_hospital_db(sql: str, params: tuple = None) -> List[Dict]:
    pool = get_hospital_pool()
    conn = pool.connection()
    try:
        with conn.cursor(DictCursor) as cursor:
            cursor.execute(sql, params)
            return cursor.fetchall()
    finally:
        conn.close()


def execute_hospital_db(sql: str, params: tuple = None) -> int:
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


# ============================================================
# 表结构初始化
# ============================================================

def init_sync_tables():
    """初始化同步所需的表和字段"""

    # 1. 同步日志表
    execute_hospital_db("""
        CREATE TABLE IF NOT EXISTS hospital_sync_log (
            id INT AUTO_INCREMENT PRIMARY KEY,
            sync_type VARCHAR(50) NOT NULL COMMENT '同步类型: full/incremental',
            start_time DATETIME NOT NULL,
            end_time DATETIME,
            source_count INT DEFAULT 0,
            insert_count INT DEFAULT 0,
            update_count INT DEFAULT 0,
            delete_count INT DEFAULT 0,
            skip_count INT DEFAULT 0,
            error_count INT DEFAULT 0,
            status VARCHAR(20) DEFAULT 'running',
            error_message TEXT,
            INDEX idx_sync_time (start_time)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)

    # 2. 变更历史表
    execute_hospital_db("""
        CREATE TABLE IF NOT EXISTS hospital_change_log (
            id INT AUTO_INCREMENT PRIMARY KEY,
            hospital_id INT NOT NULL COMMENT '知识库医院ID',
            qudao_id INT COMMENT '客服系统医院ID',
            change_type VARCHAR(20) NOT NULL COMMENT 'insert/update/delete',
            change_fields TEXT COMMENT '变更字段JSON',
            old_values TEXT COMMENT '旧值JSON',
            new_values TEXT COMMENT '新值JSON',
            sync_log_id INT COMMENT '关联同步日志',
            created_time DATETIME DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_hospital (hospital_id),
            INDEX idx_time (created_time)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)

    # 3. 给robot_kb_hospitals添加同步相关字段
    sync_columns = [
        ("qudao_id", "INT COMMENT '客服系统医院ID'"),
        ("sync_source", "VARCHAR(50) DEFAULT 'manual' COMMENT '数据来源: qudao/manual'"),
        ("sync_locked", "TINYINT DEFAULT 0 COMMENT '同步锁定: 1=锁定不覆盖'"),
        ("last_sync_time", "DATETIME COMMENT '最后同步时间'"),
        ("data_hash", "VARCHAR(64) COMMENT '数据hash用于变更检测'"),
        ("cooperation_status", "VARCHAR(50) COMMENT '合作状态'"),
        ("commission_percent", "DECIMAL(5,2) COMMENT '佣金比例'"),
        ("channel_cooperation", "TEXT COMMENT '渠道合作信息JSON'"),
        ("current_activities", "TEXT COMMENT '当前活动JSON'"),
        ("price_list", "TEXT COMMENT '价格列表JSON'"),
        ("monthly_stats", "TEXT COMMENT '月度统计JSON'"),
    ]

    for col_name, col_def in sync_columns:
        try:
            execute_hospital_db(f"ALTER TABLE robot_kb_hospitals ADD COLUMN {col_name} {col_def}")
            logger.info(f"添加字段: {col_name}")
        except Exception as e:
            if "Duplicate column" not in str(e):
                logger.debug(f"字段 {col_name}: {e}")

    # 4. 添加索引
    try:
        execute_hospital_db("ALTER TABLE robot_kb_hospitals ADD INDEX idx_qudao_id (qudao_id)")
    except:
        pass

    logger.info("同步表结构初始化完成")


# ============================================================
# 数据提取 - 从客服系统
# ============================================================

def extract_hospitals_from_qudao(modified_after: datetime = None) -> List[Dict]:
    """从客服系统提取医院数据"""

    sql = """
    SELECT
        h.userid AS qudao_id,
        h.companyname AS hospital_name,
        h.hospital_type,
        h.status AS qudao_status,
        h.islock,
        h.is_suspend,
        h.percent,
        FROM_UNIXTIME(h.regtime) AS createtime,
        FROM_UNIXTIME(h.edit_time) AS updatetime,
        k.cooperation_status,
        k.main_projects,
        k.star_num,
        k.hospital_address AS address,
        k.hospital_phone AS contact_phone,
        k.business_hours,
        k.hospital_introduction AS description
    FROM un_hospital_company h
    LEFT JOIN un_knowledge_hospital_info k ON h.userid = k.userid
    WHERE h.companyname IS NOT NULL AND h.companyname != ''
    """
    params = ()

    if modified_after:
        # edit_time 和 regtime 是 Unix 时间戳 (INT)
        timestamp = int(modified_after.timestamp())
        sql += " AND (h.edit_time >= %s OR h.regtime >= %s)"
        params = (timestamp, timestamp)

    sql += " ORDER BY h.userid"
    return query_qudao(sql, params if params else None)


def extract_hospital_prices(hospital_ids: List[int]) -> Dict[int, List[Dict]]:
    """提取价格数据"""
    if not hospital_ids:
        return {}

    placeholders = ','.join(['%s'] * len(hospital_ids))
    sql = f"""
        SELECT hospital_id, project, price, category
        FROM un_sanitized_hospital_price
        WHERE hospital_id IN ({placeholders})
        ORDER BY hospital_id, category
    """
    rows = query_qudao(sql, tuple(hospital_ids))

    result = {}
    for row in rows:
        hid = row['hospital_id']
        if hid not in result:
            result[hid] = []
        result[hid].append(row)
    return result


def extract_hospital_activities(hospital_ids: List[int]) -> Dict[int, List[Dict]]:
    """提取活动数据"""
    if not hospital_ids:
        return {}

    placeholders = ','.join(['%s'] * len(hospital_ids))
    sql = f"""
        SELECT hospital_id, title, start_date, end_date, activity_type, status
        FROM un_hospital_activities
        WHERE hospital_id IN ({placeholders})
          AND status = 1 AND (end_date IS NULL OR end_date >= CURDATE())
        ORDER BY hospital_id, start_date DESC
    """
    rows = query_qudao(sql, tuple(hospital_ids))

    result = {}
    for row in rows:
        hid = row['hospital_id']
        if hid not in result:
            result[hid] = []
        result[hid].append(row)
    return result


def extract_hospital_cooperation(hospital_ids: List[int]) -> Dict[int, Dict]:
    """提取渠道合作数据"""
    if not hospital_ids:
        return {}

    placeholders = ','.join(['%s'] * len(hospital_ids))
    sql = f"""
        SELECT hospital_id,
               beise_percent, kelete_percent, meituan_percent,
               alipay_percent, jingdong_percent, gaode_percent
        FROM un_hospital_cooperation
        WHERE hospital_id IN ({placeholders})
    """
    rows = query_qudao(sql, tuple(hospital_ids))
    return {row['hospital_id']: row for row in rows}


# ============================================================
# 数据转换
# ============================================================

def safe_float(val, default=0.0):
    """安全转换为float"""
    if val is None:
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def parse_city_from_name(hospital_name: str) -> Tuple[str, str]:
    """从医院名称解析城市和区县"""

    cities = [
        "北京", "上海", "广州", "深圳", "成都", "杭州", "武汉", "西安",
        "南京", "重庆", "天津", "苏州", "郑州", "长沙", "东莞", "沈阳",
        "青岛", "合肥", "佛山", "无锡", "宁波", "昆明", "大连", "厦门",
        "济南", "福州", "哈尔滨", "温州", "南宁", "长春", "泉州", "石家庄",
    ]

    districts = [
        "朝阳", "海淀", "东城", "西城", "丰台", "通州", "顺义", "昌平",
        "浦东", "徐汇", "长宁", "静安", "普陀", "虹口", "杨浦", "闵行",
        "天河", "越秀", "荔湾", "白云", "黄埔", "番禺", "南沙",
        "福田", "罗湖", "南山", "宝安", "龙岗", "龙华",
        "武侯", "锦江", "青羊", "金牛", "成华", "高新",
    ]

    city, district = "", ""
    for c in cities:
        if c in hospital_name:
            city = c
            break
    for d in districts:
        if d in hospital_name:
            district = d + "区" if not d.endswith("区") else d
            break

    return city, district


def compute_data_hash(data: Dict) -> str:
    """计算数据hash用于变更检测"""
    # 只对关键字段计算hash
    key_fields = ['hospital_name', 'qudao_status', 'cooperation_status',
                  'main_projects', 'address', 'contact_phone', 'percent']
    hash_data = {k: str(data.get(k, '')) for k in key_fields}
    hash_str = json.dumps(hash_data, sort_keys=True, ensure_ascii=False)
    return hashlib.md5(hash_str.encode()).hexdigest()


def transform_to_kb_format(
    hospital: Dict,
    prices: List[Dict] = None,
    activities: List[Dict] = None,
    cooperation: Dict = None
) -> Dict:
    """转换为知识库格式"""

    city, district = parse_city_from_name(hospital.get('hospital_name', ''))

    # 处理主营项目
    main_projects = hospital.get('main_projects', '') or ''
    projects_list = [p.strip() for p in main_projects.split(',') if p.strip()]

    # 处理价格
    price_list = []
    if prices:
        for p in prices:
            # 处理价格字段（可能包含中文单位如 "1000元"，或异常值如 "."）
            price_val = p.get('price', 0)
            try:
                if isinstance(price_val, str):
                    # 移除中文单位，只保留数字
                    import re
                    price_match = re.search(r'\d+\.?\d*', price_val)
                    price_val = float(price_match.group()) if price_match else 0
                elif price_val is not None:
                    price_val = float(price_val)
                else:
                    price_val = 0
            except (ValueError, TypeError):
                price_val = 0

            price_list.append({
                "project": p.get('project', ''),
                "price": price_val,
                "category": p.get('category', '')
            })

    # 处理活动
    activities_list = []
    if activities:
        for a in activities[:5]:
            activities_list.append({
                "title": a.get('title', ''),
                "type": a.get('activity_type', ''),
                "start": str(a['start_date']) if a.get('start_date') else '',
                "end": str(a['end_date']) if a.get('end_date') else ''
            })

    # 处理渠道合作
    channel_info = {}
    if cooperation:
        channel_map = {
            'beise_percent': '贝壳', 'kelete_percent': '可乐',
            'meituan_percent': '美团', 'alipay_percent': '支付宝',
            'jingdong_percent': '京东', 'gaode_percent': '高德'
        }
        for field, name in channel_map.items():
            val = cooperation.get(field)
            if val:
                try:
                    channel_info[name] = float(val)
                except (ValueError, TypeError):
                    pass

    result = {
        # 基础信息
        "qudao_id": hospital.get('qudao_id'),
        "hospital_name": hospital.get('hospital_name', ''),
        "hospital_type": HOSPITAL_TYPE_MAP.get(hospital.get('hospital_type', 0), '其他'),
        "city_name": city,
        "district_name": district,
        "detailed_address": hospital.get('address', ''),
        "phone": hospital.get('contact_phone', ''),
        "business_hours": hospital.get('business_hours', ''),
        "description": hospital.get('description', ''),

        # 合作信息
        "cooperation_status": COOPERATION_STATUS_MAP.get(
            hospital.get('cooperation_status', 0), '未知'
        ),
        "commission_percent": safe_float(hospital.get('percent', 0)),

        # 项目和价格
        "main_projects_list": json.dumps(projects_list, ensure_ascii=False),
        "price_list": json.dumps(price_list, ensure_ascii=False),

        # 活动
        "current_activities": json.dumps(activities_list, ensure_ascii=False),

        # 渠道
        "channel_cooperation": json.dumps(channel_info, ensure_ascii=False),

        # 状态（根据客服系统状态判断）
        "status": 1 if hospital.get('qudao_status', 0) == 0 else 0,

        # 同步信息
        "sync_source": "qudao",
        "last_sync_time": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        "data_hash": compute_data_hash(hospital),
    }

    return result


# ============================================================
# 同步执行
# ============================================================

def get_existing_hospitals() -> Dict[int, Dict]:
    """获取知识库中已有的医院（按qudao_id索引）"""
    rows = query_hospital_db("""
        SELECT id, qudao_id, hospital_name, data_hash, sync_locked, status
        FROM robot_kb_hospitals
        WHERE qudao_id IS NOT NULL
    """)
    return {row['qudao_id']: row for row in rows}


def get_hospitals_by_name() -> Dict[str, Dict]:
    """获取知识库中的医院（按名称索引，用于首次匹配）"""
    rows = query_hospital_db("""
        SELECT id, qudao_id, hospital_name, data_hash, sync_locked, status
        FROM robot_kb_hospitals
    """)
    return {row['hospital_name']: row for row in rows}


def upsert_hospital(data: Dict, existing: Dict = None, sync_log_id: int = None) -> Tuple[str, Optional[str]]:
    """
    插入或更新医院

    Returns:
        (action, error) - action: insert/update/skip
    """
    try:
        qudao_id = data.get('qudao_id')
        hospital_name = data.get('hospital_name')

        if not hospital_name:
            return 'skip', '医院名称为空'

        if existing:
            # 检查是否锁定
            if existing.get('sync_locked'):
                return 'skip', '数据已锁定'

            # 检查是否有变更
            if existing.get('data_hash') == data.get('data_hash'):
                return 'skip', None  # 无变更，跳过

            # 记录变更
            log_change(existing['id'], qudao_id, 'update', data, sync_log_id)

            # 更新
            update_fields = []
            update_values = []

            for key, value in data.items():
                if key == 'qudao_id':
                    continue
                update_fields.append(f"{key} = %s")
                update_values.append(value)

            update_values.append(existing['id'])
            sql = f"UPDATE robot_kb_hospitals SET {', '.join(update_fields)} WHERE id = %s"
            execute_hospital_db(sql, tuple(update_values))

            return 'update', None
        else:
            # 新增
            fields = list(data.keys())
            values = list(data.values())
            placeholders = ', '.join(['%s'] * len(fields))

            sql = f"INSERT INTO robot_kb_hospitals ({', '.join(fields)}) VALUES ({placeholders})"
            try:
                execute_hospital_db(sql, tuple(values))
            except Exception as insert_err:
                return 'skip', f"INSERT失败: {insert_err}"

            # 获取新插入的ID并记录
            rows = query_hospital_db("SELECT LAST_INSERT_ID() as id")
            new_id = rows[0]['id']
            log_change(new_id, qudao_id, 'insert', data, sync_log_id)

            return 'insert', None

    except Exception as e:
        return 'skip', f"处理异常: {e}"


def mark_deleted_hospitals(active_qudao_ids: set, sync_log_id: int) -> int:
    """标记已删除的医院（软删除）"""

    # 获取知识库中来自qudao但不在活跃列表中的医院
    rows = query_hospital_db("""
        SELECT id, qudao_id, hospital_name
        FROM robot_kb_hospitals
        WHERE sync_source = 'qudao'
          AND qudao_id IS NOT NULL
          AND status = 1
          AND sync_locked = 0
    """)

    delete_count = 0
    for row in rows:
        if row['qudao_id'] not in active_qudao_ids:
            # 软删除
            execute_hospital_db(
                "UPDATE robot_kb_hospitals SET status = 0, last_sync_time = %s WHERE id = %s",
                (datetime.now(), row['id'])
            )
            log_change(row['id'], row['qudao_id'], 'delete', {'status': 0}, sync_log_id)
            delete_count += 1
            logger.info(f"软删除医院: {row['hospital_name']} (qudao_id={row['qudao_id']})")

    return delete_count


def log_change(hospital_id: int, qudao_id: int, change_type: str,
               new_values: Dict, sync_log_id: int = None):
    """记录变更历史"""
    try:
        execute_hospital_db("""
            INSERT INTO hospital_change_log
            (hospital_id, qudao_id, change_type, new_values, sync_log_id)
            VALUES (%s, %s, %s, %s, %s)
        """, (hospital_id, qudao_id, change_type,
              json.dumps(new_values, ensure_ascii=False, default=str), sync_log_id))
    except Exception as e:
        logger.warning(f"记录变更失败: {e}")


def sync_hospitals(full_sync: bool = False, dry_run: bool = False) -> Dict:
    """
    执行医院数据同步

    Args:
        full_sync: 全量同步
        dry_run: 只检查不执行

    Returns:
        同步统计
    """
    logger.info(f"{'[检查模式] ' if dry_run else ''}开始医院数据同步 - {'全量' if full_sync else '增量'}")

    # 初始化表结构（检查模式也需要读取同步历史）
    init_sync_tables()

    stats = {
        'source_count': 0, 'insert_count': 0, 'update_count': 0,
        'delete_count': 0, 'skip_count': 0, 'error_count': 0, 'errors': []
    }

    # 记录同步开始
    sync_log_id = None
    if not dry_run:
        execute_hospital_db(
            "INSERT INTO hospital_sync_log (sync_type, start_time, status) VALUES (%s, %s, 'running')",
            ('full' if full_sync else 'incremental', datetime.now())
        )
        rows = query_hospital_db("SELECT LAST_INSERT_ID() as id")
        sync_log_id = rows[0]['id']

    try:
        # 确定增量时间点
        modified_after = None
        if not full_sync:
            rows = query_hospital_db("""
                SELECT MAX(end_time) as last_sync FROM hospital_sync_log WHERE status = 'success'
            """)
            if rows and rows[0]['last_sync']:
                modified_after = rows[0]['last_sync']
            else:
                modified_after = datetime.now() - timedelta(days=7)
            logger.info(f"增量同步时间点: {modified_after}")

        # 1. 提取数据
        hospitals = extract_hospitals_from_qudao(modified_after)
        stats['source_count'] = len(hospitals)
        logger.info(f"从客服系统提取 {len(hospitals)} 条医院数据")

        if not hospitals:
            logger.info("没有需要同步的数据")
            if not dry_run:
                execute_hospital_db(
                    "UPDATE hospital_sync_log SET end_time=%s, status='success' WHERE id=%s",
                    (datetime.now(), sync_log_id)
                )
            return stats

        # 2. 提取关联数据
        hospital_ids = [h['qudao_id'] for h in hospitals if h.get('qudao_id')]
        prices_map = extract_hospital_prices(hospital_ids)
        activities_map = extract_hospital_activities(hospital_ids)
        cooperation_map = extract_hospital_cooperation(hospital_ids)

        # 3. 获取现有数据
        existing_by_id = get_existing_hospitals()
        existing_by_name = get_hospitals_by_name()

        # 4. 同步数据
        active_qudao_ids = set()

        for hospital in hospitals:
            qudao_id = hospital.get('qudao_id')
            name = hospital.get('hospital_name')
            active_qudao_ids.add(qudao_id)

            # 获取关联数据
            prices = prices_map.get(qudao_id, [])
            activities = activities_map.get(qudao_id, [])
            cooperation = cooperation_map.get(qudao_id)

            # 转换格式
            transformed = transform_to_kb_format(hospital, prices, activities, cooperation)

            # 查找现有记录（优先用qudao_id，其次用名称）
            existing = existing_by_id.get(qudao_id)
            if not existing and name in existing_by_name:
                existing = existing_by_name[name]
                # 如果通过名称匹配到，更新qudao_id
                if not dry_run and existing and not existing.get('qudao_id'):
                    execute_hospital_db(
                        "UPDATE robot_kb_hospitals SET qudao_id = %s WHERE id = %s",
                        (qudao_id, existing['id'])
                    )

            if dry_run:
                action = 'update' if existing else 'insert'
                if existing and existing.get('sync_locked'):
                    action = 'skip'
                elif existing and existing.get('data_hash') == transformed.get('data_hash'):
                    action = 'skip'
            else:
                action, error = upsert_hospital(transformed, existing, sync_log_id)
                if error:
                    stats['error_count'] += 1
                    if len(stats['errors']) < 20:  # 只记录前20个错误
                        stats['errors'].append({'hospital': name, 'error': error})

            if action == 'insert':
                stats['insert_count'] += 1
            elif action == 'update':
                stats['update_count'] += 1
            else:
                stats['skip_count'] += 1

        # 5. 处理删除（全量同步时）
        if full_sync and not dry_run:
            stats['delete_count'] = mark_deleted_hospitals(active_qudao_ids, sync_log_id)

        # 6. 更新同步日志
        if not dry_run:
            execute_hospital_db("""
                UPDATE hospital_sync_log
                SET end_time=%s, source_count=%s, insert_count=%s, update_count=%s,
                    delete_count=%s, skip_count=%s, error_count=%s, status='success'
                WHERE id=%s
            """, (datetime.now(), stats['source_count'], stats['insert_count'],
                  stats['update_count'], stats['delete_count'], stats['skip_count'],
                  stats['error_count'], sync_log_id))

        logger.info(f"同步完成 - 新增:{stats['insert_count']} 更新:{stats['update_count']} "
                    f"删除:{stats['delete_count']} 跳过:{stats['skip_count']}")

        return stats

    except Exception as e:
        logger.error(f"同步失败: {e}")
        if sync_log_id:
            execute_hospital_db(
                "UPDATE hospital_sync_log SET end_time=%s, status='failed', error_message=%s WHERE id=%s",
                (datetime.now(), str(e), sync_log_id)
            )
        raise


def get_sync_status() -> List[Dict]:
    """获取同步历史"""
    try:
        init_sync_tables()
        return query_hospital_db("""
            SELECT * FROM hospital_sync_log ORDER BY start_time DESC LIMIT 10
        """)
    except:
        return []


# ============================================================
# CLI
# ============================================================

def main():
    import argparse

    parser = argparse.ArgumentParser(description='医院数据同步服务')
    parser.add_argument('--full', action='store_true', help='全量同步')
    parser.add_argument('--check', action='store_true', help='只检查不执行')
    parser.add_argument('--status', action='store_true', help='查看同步状态')

    args = parser.parse_args()

    print("\n" + "="*60)
    print("           医院数据同步服务 v2.0")
    print("="*60)

    if args.status:
        history = get_sync_status()
        print("\n【同步历史】")
        if not history:
            print("  暂无同步记录")
        for h in history:
            print(f"\n  [{h['start_time']}] {h['sync_type']} - {h['status']}")
            print(f"    源数据: {h['source_count']} | 新增: {h['insert_count']} | "
                  f"更新: {h['update_count']} | 删除: {h.get('delete_count', 0)} | "
                  f"跳过: {h.get('skip_count', 0)}")
        return

    try:
        result = sync_hospitals(full_sync=args.full, dry_run=args.check)

        print(f"\n【{'检查' if args.check else '同步'}结果】")
        print(f"  源数据: {result['source_count']}")
        print(f"  新增:   {result['insert_count']}")
        print(f"  更新:   {result['update_count']}")
        print(f"  删除:   {result['delete_count']}")
        print(f"  跳过:   {result['skip_count']}")
        print(f"  错误:   {result['error_count']}")

        if result['errors']:
            print("\n【错误详情】")
            for err in result['errors'][:5]:
                print(f"  - {err['hospital']}: {err['error']}")

    except Exception as e:
        print(f"\n同步失败: {e}")
        exit(1)


if __name__ == '__main__':
    main()
