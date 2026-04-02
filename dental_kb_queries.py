#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
知识库查询函数 — 从 dental_chat_api.py 的 smart_query() 中提取
供 dental_mcp_server.py 调用

所有函数查询 hospital_db 数据库（robot_kb_* 系列表）
"""

from typing import Dict, List

from dental_chat_api import query_db, sanitize_like_param


def query_hospital_recommend(city: str = "", district: str = "", project: str = "") -> Dict:
    """推荐合作中的医院，按地区/项目筛选"""
    city = sanitize_like_param(city)

    if city:
        sql = """
            SELECT h.id, h.hospital_name, h.district_name, h.detailed_address,
                   h.phone, h.features, h.description, h.business_hours,
                   h.cooperation_status, h.main_projects_list
            FROM robot_kb_hospitals h
            WHERE h.cooperation_status = '合作中'
              AND (h.city_name LIKE %s OR h.hospital_name LIKE %s OR h.detailed_address LIKE %s)
            ORDER BY
              CASE WHEN h.hospital_name LIKE '%%口腔%%' THEN 0 ELSE 1 END,
              h.qudao_id DESC
            LIMIT 5
        """
        like_city = f"%{city}%"
        params = (like_city, like_city, like_city)
    else:
        sql = """
            SELECT h.id, h.hospital_name, h.district_name, h.detailed_address,
                   h.phone, h.features, h.description, h.business_hours,
                   h.cooperation_status, h.main_projects_list
            FROM robot_kb_hospitals h
            WHERE h.cooperation_status = '合作中'
              AND h.hospital_name LIKE '%%口腔%%'
            ORDER BY h.qudao_id DESC LIMIT 5
        """
        params = ()

    return {"hospitals": query_db(sql, params)}


def query_doctor_recommend(hospital: str = "", city: str = "", doctor: str = "") -> Dict:
    """推荐医生并返回排班信息"""
    hospital = sanitize_like_param(hospital)
    city = sanitize_like_param(city)
    doctor = sanitize_like_param(doctor)

    sql = """
        SELECT d.doctor_name, d.position, d.specialties,
               h.hospital_name,
               GROUP_CONCAT(DISTINCT
                   CONCAT(CASE ds.day_of_week
                       WHEN 1 THEN '周一' WHEN 2 THEN '周二' WHEN 3 THEN '周三'
                       WHEN 4 THEN '周四' WHEN 5 THEN '周五' WHEN 6 THEN '周六' WHEN 7 THEN '周日'
                   END, ds.time_slot) SEPARATOR '、') as schedule
        FROM robot_kb_doctors d
        JOIN robot_kb_hospital_doctors hd ON d.id = hd.doctor_id
        JOIN robot_kb_hospitals h ON hd.hospital_id = h.id
        LEFT JOIN doctor_schedules ds ON d.id = ds.doctor_id AND h.id = ds.hospital_id
        WHERE d.status = 1
          AND (%s = '' OR h.hospital_name LIKE %s)
          AND (%s = '' OR h.city_name LIKE %s)
          AND (%s = '' OR d.doctor_name LIKE %s)
        GROUP BY d.id, d.doctor_name, d.position, d.specialties, h.hospital_name
        LIMIT 10
    """
    params = (
        hospital, f"%{hospital}%" if hospital else "",
        city, f"%{city}%" if city else "",
        doctor, f"%{doctor}%" if doctor else "",
    )
    return {"doctors": query_db(sql, params)}


def query_schedule(hospital: str = "", city: str = "", doctor: str = "") -> Dict:
    """查询医生排班/坐诊信息（与 doctor_recommend 共享查询逻辑）"""
    return query_doctor_recommend(hospital=hospital, city=city, doctor=doctor)


def query_appointment(hospital: str = "", city: str = "", date_hint: str = "") -> Dict:
    """查询预约号源。date_hint 支持 '明天'/'后天'，否则查未来7天"""
    hospital = sanitize_like_param(hospital)
    city = sanitize_like_param(city)

    if date_hint == "明天":
        date_condition = "a.slot_date = DATE_ADD(CURDATE(), INTERVAL 1 DAY)"
    elif date_hint == "后天":
        date_condition = "a.slot_date = DATE_ADD(CURDATE(), INTERVAL 2 DAY)"
    else:
        date_condition = "a.slot_date >= CURDATE() AND a.slot_date <= DATE_ADD(CURDATE(), INTERVAL 7 DAY)"

    sql = f"""
        SELECT d.doctor_name, h.hospital_name,
               a.slot_date, a.time_slot, a.available_slots,
               CASE WHEN a.available_slots > 5 THEN '充足'
                    WHEN a.available_slots > 0 THEN '紧张' ELSE '已满' END as status
        FROM appointment_slots a
        JOIN robot_kb_doctors d ON a.doctor_id = d.id
        JOIN robot_kb_hospitals h ON a.hospital_id = h.id
        WHERE a.is_open = 1 AND a.available_slots > 0
          AND {date_condition}
          AND (%s = '' OR h.hospital_name LIKE %s)
          AND (%s = '' OR h.city_name LIKE %s)
        ORDER BY a.slot_date, a.available_slots DESC LIMIT 10
    """
    params = (
        hospital, f"%{hospital}%" if hospital else "",
        city, f"%{city}%" if city else "",
    )
    return {"appointments": query_db(sql, params)}


def query_promotion(hospital: str = "", city: str = "") -> Dict:
    """查询优惠活动，先查 hospital_promotions 表，无结果则从医院 current_activities 字段获取"""
    hospital = sanitize_like_param(hospital)
    city = sanitize_like_param(city)

    sql = """
        SELECT h.hospital_name, p.title, p.promotion_type,
               p.original_price, p.promotion_price, p.discount_rate, p.end_date
        FROM hospital_promotions p
        JOIN robot_kb_hospitals h ON p.hospital_id = h.id
        WHERE p.status = 1 AND p.end_date >= CURDATE()
          AND (%s = '' OR h.hospital_name LIKE %s)
          AND (%s = '' OR h.city_name LIKE %s)
        ORDER BY p.discount_rate ASC LIMIT 10
    """
    params = (
        hospital, f"%{hospital}%" if hospital else "",
        city, f"%{city}%" if city else "",
    )
    promotions = query_db(sql, params)

    if not promotions:
        sql = """
            SELECT hospital_name, current_activities as title
            FROM robot_kb_hospitals
            WHERE current_activities IS NOT NULL
              AND current_activities != ''
              AND (%s = '' OR hospital_name LIKE %s)
              AND (%s = '' OR city_name LIKE %s)
            LIMIT 10
        """
        promotions = query_db(sql, params)

    return {"promotions": promotions}


def query_price(hospital: str = "", city: str = "", project: str = "") -> Dict:
    """查询项目价格。优先从医院 price_list 字段获取，否则查 robot_kb_prices 表"""
    hospital = sanitize_like_param(hospital)
    city = sanitize_like_param(city)
    project = sanitize_like_param(project) if project else "种植"

    prices = []

    if hospital:
        sql = """
            SELECT hospital_name, city_name, price_list
            FROM robot_kb_hospitals
            WHERE price_list IS NOT NULL
              AND LENGTH(price_list) > 2
              AND hospital_name LIKE %s
            LIMIT 5
        """
        prices = query_db(sql, (f"%{hospital}%",))

    if not prices:
        sql = """
            SELECT h.hospital_name, h.city_name,
                   p.project_name, p.price_min, p.price_max, p.price_unit
            FROM robot_kb_prices p
            JOIN robot_kb_hospitals h ON p.hospital_id = h.id
            WHERE p.status = 1
              AND p.project_name LIKE %s
              AND (%s = '' OR h.city_name LIKE %s)
            ORDER BY p.price_min LIMIT 10
        """
        params = (
            f"%{project}%",
            city, f"%{city}%" if city else "",
        )
        prices = query_db(sql, params)

    return {"prices": prices}


def query_career() -> Dict:
    """查询医生变动记录（调动/入职等公开信息）"""
    sql = """
        SELECT doctor_name,
               CASE event_type WHEN 'transfer' THEN '调动' WHEN 'join' THEN '入职'
                    WHEN 'leave' THEN '离职' WHEN 'promote' THEN '晋升' END as event_type_cn,
               from_hospital_name, to_hospital_name,
               from_position, to_position, event_date, reason
        FROM doctor_career_history
        WHERE is_public = 1 AND event_type IN ('transfer', 'join')
        ORDER BY event_date DESC LIMIT 10
    """
    return {"career": query_db(sql)}


def query_hospital_info(hospital: str, city: str = "") -> Dict:
    """查询医院详细信息"""
    hospital = sanitize_like_param(hospital)
    city = sanitize_like_param(city)

    if not hospital:
        return {"hospital_detail": []}

    sql = """
        SELECT hospital_name, hospital_type, description,
               main_projects_list, price_list, current_activities,
               phone, business_hours, detailed_address, city_name,
               cooperation_status, channel_cooperation
        FROM robot_kb_hospitals
        WHERE hospital_name LIKE %s
          AND (%s = '' OR city_name LIKE %s)
        ORDER BY cooperation_status = '合作中' DESC, id DESC
        LIMIT 15
    """
    params = (
        f"%{hospital}%",
        city, f"%{city}%" if city else "",
    )
    return {"hospital_detail": query_db(sql, params)}
