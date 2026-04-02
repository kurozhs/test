#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
医生数据同步服务
将客服系统(qudao)的医生数据同步到本地知识库(hospital_db)

数据源: qudao.un_knowledge_doctor_team
目标: hospital_db.robot_kb_doctors + robot_kb_hospital_doctors

使用方式:
    python doctor_data_sync.py --full     # 全量同步
    python doctor_data_sync.py            # 增量同步
    python doctor_data_sync.py --check    # 检查模式

作者: 数据同步服务
日期: 2026-01-23
"""

import json
import logging
import os
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import pymysql
from pymysql.cursors import DictCursor
from dbutils.pooled_db import PooledDB
from dotenv import load_dotenv

load_dotenv()

# 配置
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
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('doctor_sync.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# 连接池
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


def init_sync_fields():
    """给医生表添加同步字段"""
    fields = [
        ("qudao_id", "INT COMMENT '客服系统医生ID'"),
        ("qudao_hospital_id", "INT COMMENT '客服系统医院ID'"),
        ("sync_source", "VARCHAR(50) DEFAULT 'manual' COMMENT '数据来源'"),
        ("last_sync_time", "DATETIME COMMENT '最后同步时间'"),
    ]

    for col_name, col_def in fields:
        try:
            execute_hospital_db(f"ALTER TABLE robot_kb_doctors ADD COLUMN {col_name} {col_def}")
            logger.info(f"添加字段: {col_name}")
        except Exception as e:
            if "Duplicate column" not in str(e):
                logger.debug(f"字段 {col_name}: {e}")

    # 添加索引
    try:
        execute_hospital_db("ALTER TABLE robot_kb_doctors ADD INDEX idx_qudao_id (qudao_id)")
    except:
        pass

    # 确保id是自增的
    try:
        execute_hospital_db("ALTER TABLE robot_kb_doctors MODIFY COLUMN id INT NOT NULL AUTO_INCREMENT")
    except:
        pass

    try:
        execute_hospital_db("ALTER TABLE robot_kb_hospital_doctors MODIFY COLUMN id INT NOT NULL AUTO_INCREMENT")
    except:
        pass


def get_hospital_id_mapping() -> Dict[int, int]:
    """获取 qudao_id -> knowledge_base_id 的映射"""
    rows = query_hospital_db(
        "SELECT id, qudao_id FROM robot_kb_hospitals WHERE qudao_id IS NOT NULL"
    )
    return {row['qudao_id']: row['id'] for row in rows}


def extract_doctors_from_qudao() -> List[Dict]:
    """从客服系统提取医生数据"""
    sql = """
        SELECT
            d.id as qudao_id,
            d.doctor_name,
            d.hospital_id as qudao_hospital_id,
            d.doctor_photo,
            d.doctor_title,
            d.department,
            d.education_info,
            d.docker_graduate_school as graduate_school,
            d.personal_profile,
            d.status,
            d.created_at,
            d.updated_at,
            h.companyname as hospital_name
        FROM un_knowledge_doctor_team d
        JOIN un_hospital_company h ON d.hospital_id = h.userid
        WHERE d.doctor_name IS NOT NULL AND d.doctor_name != ''
        ORDER BY d.id
    """
    return query_qudao(sql)


def transform_doctor(doctor: Dict, kb_hospital_id: int) -> Dict:
    """转换医生数据为知识库格式"""

    # 提取职称
    title = doctor.get('doctor_title', '') or ''

    # 提取专长（从个人简介中）
    profile = doctor.get('personal_profile', '') or ''
    specialties = ''
    if '擅长' in profile:
        idx = profile.find('擅长')
        specialties = profile[idx:idx+100]

    return {
        'qudao_id': doctor.get('qudao_id'),
        'qudao_hospital_id': doctor.get('qudao_hospital_id'),
        'doctor_name': doctor.get('doctor_name', ''),
        'hospital_id': kb_hospital_id,
        'title': title[:100] if title else None,
        'position': title[:255] if title else None,
        'specialty': specialties[:255] if specialties else None,
        'specialties': specialties if specialties else None,
        'introduction': profile,
        'education': doctor.get('education_info') or doctor.get('graduate_school'),
        'avatar_url': doctor.get('doctor_photo'),
        'status': 1 if doctor.get('status', 1) == 1 else 0,
        'sync_source': 'qudao',
        'last_sync_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }


def get_existing_doctors() -> Dict[int, Dict]:
    """获取已存在的医生（按qudao_id）"""
    rows = query_hospital_db(
        "SELECT id, qudao_id, doctor_name FROM robot_kb_doctors WHERE qudao_id IS NOT NULL"
    )
    return {row['qudao_id']: row for row in rows}


def upsert_doctor(data: Dict, existing: Dict = None) -> Tuple[str, int, Optional[str]]:
    """插入或更新医生"""
    try:
        if existing:
            # 更新
            update_fields = []
            update_values = []
            for key, value in data.items():
                if key in ('qudao_id',):
                    continue
                update_fields.append(f"{key} = %s")
                update_values.append(value)

            update_values.append(existing['id'])
            sql = f"UPDATE robot_kb_doctors SET {', '.join(update_fields)} WHERE id = %s"
            execute_hospital_db(sql, tuple(update_values))
            return 'update', existing['id'], None
        else:
            # 插入
            fields = list(data.keys())
            values = list(data.values())
            placeholders = ', '.join(['%s'] * len(fields))

            sql = f"INSERT INTO robot_kb_doctors ({', '.join(fields)}) VALUES ({placeholders})"
            execute_hospital_db(sql, tuple(values))

            rows = query_hospital_db("SELECT LAST_INSERT_ID() as id")
            return 'insert', rows[0]['id'], None
    except Exception as e:
        return 'error', 0, str(e)


def create_hospital_doctor_relation(hospital_id: int, doctor_id: int):
    """创建医生-医院关联"""
    try:
        # 检查是否已存在
        existing = query_hospital_db(
            "SELECT id FROM robot_kb_hospital_doctors WHERE hospital_id = %s AND doctor_id = %s",
            (hospital_id, doctor_id)
        )
        if existing:
            return

        execute_hospital_db("""
            INSERT INTO robot_kb_hospital_doctors
            (hospital_id, doctor_id, status, created_time)
            VALUES (%s, %s, 1, %s)
        """, (hospital_id, doctor_id, datetime.now()))
    except Exception as e:
        logger.warning(f"创建关联失败: {e}")


def sync_doctors(dry_run: bool = False) -> Dict:
    """执行医生数据同步"""
    logger.info(f"{'[检查模式] ' if dry_run else ''}开始医生数据同步")

    if not dry_run:
        init_sync_fields()

    stats = {
        'source_count': 0,
        'insert_count': 0,
        'update_count': 0,
        'skip_count': 0,
        'error_count': 0,
        'relation_count': 0,
        'errors': []
    }

    # 获取医院ID映射
    hospital_mapping = get_hospital_id_mapping()
    logger.info(f"获取到 {len(hospital_mapping)} 个医院ID映射")

    # 提取医生数据
    doctors = extract_doctors_from_qudao()
    stats['source_count'] = len(doctors)
    logger.info(f"从客服系统提取 {len(doctors)} 个医生")

    if not doctors:
        return stats

    # 获取已存在的医生
    existing_doctors = get_existing_doctors() if not dry_run else {}

    # 同步
    for i, doctor in enumerate(doctors):
        qudao_id = doctor['qudao_id']
        qudao_hospital_id = doctor['qudao_hospital_id']
        name = doctor['doctor_name']

        # 检查医院是否在知识库中
        kb_hospital_id = hospital_mapping.get(qudao_hospital_id)
        if not kb_hospital_id:
            stats['skip_count'] += 1
            continue

        # 转换数据
        transformed = transform_doctor(doctor, kb_hospital_id)

        if dry_run:
            existing = existing_doctors.get(qudao_id)
            action = 'update' if existing else 'insert'
        else:
            existing = existing_doctors.get(qudao_id)
            action, doctor_id, error = upsert_doctor(transformed, existing)

            if error:
                stats['error_count'] += 1
                if len(stats['errors']) < 10:
                    stats['errors'].append({'doctor': name, 'error': error})
                continue

            # 创建医生-医院关联
            if action in ('insert', 'update') and doctor_id:
                create_hospital_doctor_relation(kb_hospital_id, doctor_id)
                stats['relation_count'] += 1

        if action == 'insert':
            stats['insert_count'] += 1
        elif action == 'update':
            stats['update_count'] += 1

        if (i + 1) % 500 == 0:
            logger.info(f"已处理 {i + 1}/{len(doctors)}")

    logger.info(f"同步完成 - 新增:{stats['insert_count']} 更新:{stats['update_count']} "
                f"跳过:{stats['skip_count']} 关联:{stats['relation_count']}")

    return stats


def main():
    import argparse

    parser = argparse.ArgumentParser(description='医生数据同步服务')
    parser.add_argument('--full', action='store_true', help='全量同步')
    parser.add_argument('--check', action='store_true', help='检查模式')

    args = parser.parse_args()

    print("\n" + "="*60)
    print("           医生数据同步服务")
    print("="*60)

    try:
        result = sync_doctors(dry_run=args.check)

        print(f"\n【{'检查' if args.check else '同步'}结果】")
        print(f"  源数据:   {result['source_count']}")
        print(f"  新增:     {result['insert_count']}")
        print(f"  更新:     {result['update_count']}")
        print(f"  跳过:     {result['skip_count']}")
        print(f"  关联创建: {result['relation_count']}")
        print(f"  错误:     {result['error_count']}")

        if result['errors']:
            print("\n【错误详情】")
            for err in result['errors'][:5]:
                print(f"  - {err['doctor']}: {err['error']}")

    except Exception as e:
        print(f"\n同步失败: {e}")
        import traceback
        traceback.print_exc()
        exit(1)


if __name__ == '__main__':
    main()
