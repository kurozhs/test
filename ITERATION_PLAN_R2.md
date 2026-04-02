# DeerFlow + Dental CRM 第二轮迭代计划

> 基于 qudao_server 深度分析，覆盖成交流程、重单预警、派单追踪、韩国行程等高级场景
> 第一轮完成 25 个工具 + 5 条链路，第二轮新增 6 条链路
> 数据库查询用 query_qudao_db(sql, params) 执行
> MCP 工具用 @mcp.tool() 装饰器定义在 dental_mcp_server.py
> SKILL 更新在 /Users/wangbaiwei/deer-flow/skills/custom/dental-crm/SKILL.md
> 验证：python -c "from dental_mcp_server import mcp; print(len(mcp._tool_manager._tools))"
> 注意：SKILL.md 中不能用花括号包变量名，用 <变量名> 代替

## 链路六：成交录入 → 审核追踪

- [x] 【6.1 我录的单子审核了吗】在 dental_mcp_server.py 新增 my_pending_deals(kf_id: int, days: int = 30) 工具。SQL 查 un_channel_paylist：WHERE KfId=%s AND addtime>=UNIX_TIMESTAMP(DATE_SUB(CURDATE(), INTERVAL %s DAY))，返回列表（id, Client_Id, 客户名（JOIN un_channel_client）, number金额, true_number实收, status状态, addtime录入时间, checktime审核时间, currency_type币种）。status 映射：0=待审核, 1=已核销, 3=退款, 5=坏账。currency_type 映射：0=人民币,1=韩币,3=美元,4=泰铢,5=日元。未审核的排在前面。更新 SKILL.md：当用户说"我的单子/审核状态/成交单/录的单"时触发。
- [x] 【6.2 最近有退款或坏账吗】在 dental_mcp_server.py 新增 refund_and_bad_debt(kf_id: int = 0, days: int = 30) 工具。SQL 查 un_channel_paylist WHERE (status=3 OR status=5 OR split_order_status IN (5,7)) AND checktime>=时间范围。如果 kf_id>0 只查该客服的。返回列表（客户名, 金额, status类型（退款/坏账）, 医院名（JOIN un_hospital_company）, 时间）。更新 SKILL.md："退款/坏账/异常单" 触发。

## 链路七：派单追踪 → 到院确认

- [x] 【7.1 我派出去的客户到院了吗】在 dental_mcp_server.py 新增 my_dispatch_tracking(kf_id: int, days: int = 7) 工具。SQL 查 un_hospital_order o JOIN un_channel_client c ON o.Client_Id=c.Client_Id JOIN un_hospital_company h ON o.hospital_id=h.id WHERE c.KfId=%s AND o.send_order_time>=时间范围。返回列表（客户名, 医院名, 派单时间, view_status(0=医院未看/1=已查看), surgery_status(10=已完成手术), consumption_money消费金额）。按 send_order_time DESC 排序。更新 SKILL.md："派出去的/到院了吗/医院看了吗/派单跟踪" 触发。
- [x] 【7.2 哪些客户派了但没到院】在 dental_mcp_server.py 新增 dispatch_no_arrival(kf_id: int = 0, days: int = 14) 工具。SQL 查派单后超过3天但客户 client_status 未变为 10(到院) 或 11(消费) 的记录：un_hospital_order o JOIN un_channel_client c ON o.Client_Id=c.Client_Id WHERE o.send_order_time>=时间范围 AND o.send_order_time<=UNIX_TIMESTAMP(DATE_SUB(CURDATE(), INTERVAL 3 DAY)) AND c.client_status NOT IN (10,11) AND c.client_status!=5。如果 kf_id>0 加 c.KfId=%s 条件。返回列表（客户名, 医院名, 派单天数, 当前状态, 客服名）。这是高价值预警——派了但没到意味着可能流失。更新 SKILL.md："派了没到/没到院/派单流失" 触发。

## 链路八：重单检测 → 协调处理

- [x] 【8.1 最近有重单吗】在 dental_mcp_server.py 新增 recent_duplicates(kf_id: int = 0, days: int = 7) 工具。SQL 查 un_channel_client_repetition r JOIN un_channel_client c ON r.client_id=c.Client_Id WHERE r.addtime>=时间范围。如果 kf_id>0 加条件 c.KfId=%s 或 r.manager_id 关联。返回（客户名, 客户手机（脱敏）, 来源, 重单类型, 时间, is_read 是否已处理）。同时查 un_hospital_contact_info_repeat 获取派单重复。更新 SKILL.md："重单/重复/重复注册/重复派单" 触发。
- [x] 【8.2 这个客户是重单吗】在 dental_mcp_server.py 新增 check_client_duplicate(client_id: int) 工具。查询：(1) un_channel_client_repetition 是否有该 client_id 的重单记录 (2) un_channel_client 中用 MobilePhone 搜索是否有同手机号的其他客户（返回时手机号脱敏）(3) un_hospital_contact_info_repeat 是否有派单重复。返回 {is_duplicate: bool, duplicate_records: [...], same_phone_clients: [...], dispatch_duplicates: [...]}。更新 SKILL.md："是不是重单/重复了吗/有没有重复" + 客户上下文触发。

## 链路九：韩国客户行程管理

- [x] 【9.1 这个客户的韩国行程】在 dental_mcp_server.py 新增 korean_schedule(client_id: int) 工具。查 un_schedule WHERE custom_id=%s ORDER BY schedule_date, jobtype。jobtype 映射：1=接机, 2=术前咨询, 3=手术, 4=术后恢复, 5=送机。返回行程时间线：每个节点（日期, 类型, 医院, 预约时间, 负责人, 是否需要用车, 航班起降时间）。对于 jobtype=1 和 5 额外返回 flight_takeofftime 和 flight_landingtime。同时查 un_custom_archives 获取护照信息。更新 SKILL.md："韩国行程/行程安排/接机/手术安排" 触发。仅对 client_region=4 的客户有效。
- [x] 【9.2 今天有哪些韩国客户要接】在 dental_mcp_server.py 新增 korean_today_schedule(staff_id: int = 0) 工具。查 un_schedule WHERE schedule_date=CURDATE() 或 (jobtype=1 AND flight_landingtime BETWEEN UNIX_TIMESTAMP(CURDATE()) AND UNIX_TIMESTAMP(CURDATE()+INTERVAL 1 DAY))。如果 staff_id>0 加 staff_id=%s 条件。返回今日行程列表（客户名, 类型, 医院, 时间, 航班信息）。更新 SKILL.md："今天接谁/今天行程/今天手术" 触发。

## 链路十：无法派单原因分析

- [x] 【10.1 为什么这些客户没派单】在 dental_mcp_server.py 新增 no_dispatch_analysis(days: int = 30, kf_id: int = 0) 工具。SQL 查 un_channel_client WHERE RegisterTime>=时间范围 AND Client_Id NOT IN (SELECT DISTINCT Client_Id FROM un_hospital_order WHERE send_order_time>0)。GROUP BY no_order_type 统计各原因的客户数。no_order_type 值从 un_linkage 表查对应中文名（用 get_linkage_name 或直接 JOIN un_linkage ON TypeID=no_order_type）。如果 kf_id>0 只查该客服。返回 {total_no_dispatch: int, by_reason: [{reason_id, reason_name, count, percentage}]}。更新 SKILL.md："没派单/为什么不派/无法派单/派单率低" 触发。

## 链路十一：业绩排名 → 团队管理

- [x] 【11.1 客服业绩排名】在 dental_mcp_server.py 新增 kf_performance_ranking(days: int = 30, department_name: str = "", metric: str = "deal_amount") 工具。SQL 查 un_channel_paylist p JOIN un_admin a ON p.KfId=a.userid WHERE p.status=1 AND p.checktime>=UNIX_TIMESTAMP(DATE_SUB(CURDATE(), INTERVAL %s DAY))。如果指定 department_name 则用 DEPARTMENT_MAP（从 dental_chat_api 导入）获取 dept_id，再用 _get_department_channel_ids 获取 channel_ids 过滤。GROUP BY p.KfId 统计 deal_count, SUM(true_number) as deal_amount, AVG(true_number) as avg_amount。按 metric 参数排序。返回排名列表（rank, kf_name, deal_count, deal_amount, avg_amount）。更新 SKILL.md："客服排名/谁做得好/业绩排行/TOP客服" 触发。
- [x] 【11.2 这个客服跟上个月比怎么样】在 dental_mcp_server.py 新增 kf_period_comparison(kf_name: str, period1: str = "本月", period2: str = "上月") 工具。先用 query_qudao_db 查 un_admin WHERE realname LIKE %s 获取 userid。复用已有的 _parse_period 时间解析函数（如果 department_period_comparison 中已实现则从同文件导入，否则重新实现）。分别查两个时间段的：新客数(un_channel_client WHERE KfId=userid AND RegisterTime between)、跟进数(un_channel_crm_log WHERE kfid=userid AND addtime between)、派单数(通过 un_hospital_order + un_channel_client 关联)、成交数和金额(un_channel_paylist WHERE KfId=userid AND status=1 AND checktime between)。返回 {kf_name, period1_label, period2_label, metrics: [{name, p1_value, p2_value, change_pct, direction}]}。更新 SKILL.md："XX上个月/XX对比/XX环比/XX趋势" 触发。
