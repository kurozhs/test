# DeerFlow + Dental CRM 产品迭代计划

> 基于渠道客服系统(qudao_server)真实业务流程设计
> 每个任务 = 一句用户自然语言 → Agent 端到端跑通
> 数据库查询用 query_qudao_db(sql, params) 执行
> MCP 工具用 @mcp.tool() 装饰器定义在 dental_mcp_server.py
> SKILL 更新在 /Users/wangbaiwei/deer-flow/skills/custom/dental-crm/SKILL.md
> 验证：python -c "from dental_mcp_server import mcp; print(len(mcp._tool_manager._tools))"
> 注意：SKILL.md 中不能用花括号包变量名（会被 str.format 误解析），用 <变量名> 代替

## 链路一：客服早间开工

- [x] 【1.1 我今天要跟进哪些客户】在 dental_mcp_server.py 新增 my_crm_reminders(kf_id: int) 工具。SQL 查 un_channel_crm 表：条件 kfid=%s AND nexttime<=UNIX_TIMESTAMP(CURDATE()+INTERVAL 1 DAY) AND processed=0，JOIN un_channel_client 获取 Client_Id,ClientName,client_status。按 nexttime ASC 排序。client_status 用数字转中文映射（0=暂无,1=极好,2=好,3=较好,4=一般,5=无意向,6=确定赴韩,9=已预约,10=已到院,11=已消费,12=未联系上）。返回列表。然后更新 SKILL.md 的 Query Strategy 部分：增加规则——当用户说"今天跟进/跟进提醒/CRM/要跟谁"时，先 resolve_staff_identity 获取 kf_id，再调 my_crm_reminders(kf_id)。验证：工具数 +1，import 无报错。
- [x] 【1.2 我最近三天跟了什么客户】在 dental_mcp_server.py 新增 my_recent_followups(kf_id: int, days: int = 3) 工具。SQL：SELECT l.addtime, l.content, c.Client_Id, c.ClientName, c.client_status FROM un_channel_crm_log l JOIN un_channel_client c ON l.client_id=c.Client_Id WHERE l.kfid=%s AND l.addtime>=UNIX_TIMESTAMP(DATE_SUB(CURDATE(), INTERVAL %s DAY)) ORDER BY l.addtime DESC LIMIT 50。addtime 是 unix 时间戳，返回时转为 datetime 字符串。content 截取前100字符。client_status 转中文。然后更新 SKILL.md：当用户说"最近跟了/跟进记录/最近联系了谁"时触发。验证同上。
- [x] 【1.3 我今天的数据怎么样】在 dental_mcp_server.py 新增 my_daily_stats(kf_id: int, days: int = 1) 工具。执行4条SQL：(1) SELECT COUNT(*) as new_clients FROM un_channel_client WHERE KfId=%s AND RegisterTime>=UNIX_TIMESTAMP(DATE_SUB(CURDATE(), INTERVAL %s DAY)) (2) SELECT COUNT(*) as follow_count FROM un_channel_crm_log WHERE kfid=%s AND addtime>=同上 (3) SELECT COUNT(*) as dispatch_count FROM un_hospital_order WHERE send_order_time>=同上 AND Client_Id IN (SELECT Client_Id FROM un_channel_client WHERE KfId=%s) (4) SELECT COUNT(*) as deal_count, COALESCE(SUM(true_number),0) as deal_amount FROM un_channel_paylist WHERE ManagerId=%s AND status=1 AND checktime>=同上。合并返回 dict。更新 SKILL.md："我的数据/今天数据/业绩/我今天怎么样" 触发。

## 链路二：客户分析 → 派单决策

- [x] 【2.1 优化客户分析回复格式】更新 SKILL.md 的 Response Formatting Guidelines：customer_full_analysis 的回复改为分层结构——第一行：一句话概括（"张三，深圳，种植牙意向，当前已派单状态，注册15天"）。第二段：成交概率 XX%（等级X）+ 最关键的3个原因。第三段：建议下一步动作。第四段：如果用户追问"详细看看"再展开完整数据。不要一次性输出所有维度。注意不要在 SKILL.md 中使用花括号。
- [x] 【2.2 这个客户派什么医院合适】在 dental_mcp_server.py 新增 recommend_hospital_for_client(client_id: int) 工具。逻辑：(1) 调 query_customer_info(client_id) 获取 PlasticsIntention 和 zx_District (2) 用 get_linkage_name 把 ID 转中文名 (3) 用中文地区名查 robot_kb_hospitals（query_db 查 hospital_db）：SELECT hospital_name,district_name,detailed_address,phone,cooperation_status,main_projects_list FROM robot_kb_hospitals WHERE cooperation_status='合作中' AND (city_name LIKE %s OR district_name LIKE %s) ORDER BY qudao_id DESC LIMIT 10 (4) 同时调 query_district_hospital_performance(zx_district_id, client_region) 获取成交数据 (5) 合并：每个医院附加成交数和转化率，按成交数降序排。返回推荐列表。更新 SKILL.md："派什么医院/推荐医院/哪个医院合适/应该派哪里" + 有客户上下文时触发。
- [x] 【2.3-2.5 医院详情串联查询】只改 SKILL.md，不改代码。在 Conversation Context Awareness 部分增加"医院上下文串联"规则：(1) 当 recommend_hospital_for_client 返回了推荐列表后，用户说"这个医院怎么样/第一个怎么样/XX医院如何"时，自动提取医院名，同时调 knowledge_base_query(type=hospital_info, hospital=名) + hospital_analysis(hospital_name=名) + hospital_deals(hospital_name=名, days=30)，合并输出：合作状态、主营项目、价格区间、近30天派单N/到院N/成交N（转化率X%）、地址电话 (2) 用户继续问"哪个医生有空"→ 自动用同一医院名调 knowledge_base_query(type=schedule, hospital=名) (3) "这个项目多少钱"→ 自动用客户意向项目+当前医院调 knowledge_base_query(type=price, hospital=名, project=项目名)。注意不要使用花括号。

## 链路三：领导分析 → 下钻对比

- [x] 【3.1 这个月各部门对比上个月】在 dental_mcp_server.py 新增 department_period_comparison(period1: str = "本月", period2: str = "上月") 工具。内部实现时间解析函数 _parse_period(text)：本月→当月1日0点到现在、上月→上月1日到上月最后一天、本周→本周一到现在、近7天/近30天→对应天数前到现在。返回 (start_ts, end_ts) 元组。然后分别用这两个时间段调 query_company_stats(start_ts1, end_ts1, period1, 'checktime') 和 query_company_stats(start_ts2, end_ts2, period2, 'checktime')。对比每个部门的 total_money 计算 change = (p1-p2)/p2*100，标记 direction（涨/跌/平）。返回 {period1_label, period2_label, departments: [{name, p1_money, p2_money, change_pct, direction}], summary: {total_p1, total_p2, total_change_pct}}。更新 SKILL.md："各部门/公司成交/对比上个月/环比" 触发。输出格式：markdown 表格 + 涨跌箭头。
- [x] 【3.2 BCG怎么掉了这么多】在 dental_mcp_server.py 新增 department_drill_down(department_name: str, days: int = 30) 工具。先用 DEPARTMENT_MAP（从 dental_chat_api 导入）获取 department_id，然后用 _get_department_channel_ids(department_id)（从 dental_chat_api 导入）获取该部门所有渠道 channel_id 列表。SQL 查 un_channel_paylist：按 ManagerId 分组统计每个客服的成交数和金额（JOIN un_admin 获取 realname），按金额降序。再按 from_type 分组统计各来源的成交分布。返回 {department, by_kf: [{kf_name, deal_count, deal_amount}], by_source: [{source_name, deal_count, deal_amount}]}。更新 SKILL.md："XX部门怎么了/为什么掉了/下钻看看/具体分析一下" 触发。
- [x] 【3.3 生成报告触发规则】只改 SKILL.md。在末尾增加"报告生成"部分：当用户角色是 manager/director，且当前对话已经查询了 2 个以上分析数据时，Agent 在回复末尾主动加一句"需要我生成可视化报告吗？"。当用户说"生成报告/导出/汇总给我"时，Agent 收集当前对话中所有工具返回的数据，用 dental-report Skill 的模板生成 HTML（Chart.js 图表），write_file 到 /mnt/user-data/outputs/report_时间戳.html，present_files 展示。注意不要使用花括号。

## 链路四：渠道来源 → ROI

- [x] 【4.1-4.2 优化来源分析回复格式】只改 SKILL.md。在 Response Formatting Guidelines 增加来源分析格式：source_analysis 回复结构改为——开头一句结论（"贝色本月注册XX人，成交XX单，转化率X%"），然后关键指标表格，最后详细维度（按需）。source_compare 回复必须用对比表格，每个指标标注谁更优（用 ↑↓ 标记）。注意不要使用花括号。
- [x] 【4.3 哪个渠道ROI最高】在 dental_mcp_server.py 新增 source_roi_ranking(days: int = 30, client_region: int = 0) 工具。SQL：SELECT c.from_type, COUNT(DISTINCT c.Client_Id) as register_count, COUNT(DISTINCT p.Client_Id) as deal_count, COALESCE(SUM(p.true_number),0) as deal_amount FROM un_channel_client c LEFT JOIN un_channel_paylist p ON c.Client_Id=p.Client_Id AND p.status=1 AND p.checktime>=UNIX_TIMESTAMP(DATE_SUB(CURDATE(), INTERVAL %s DAY)) WHERE c.RegisterTime>=同上 条件 AND (0=%s OR c.client_region=%s) GROUP BY c.from_type HAVING register_count>10 ORDER BY deal_amount DESC。from_type 用映射转中文（2=400电话,3=商务通,4=微信,5=QQ,6=其他,7=APP,8=导入）。计算 ROI=deal_amount/register_count，conversion_rate=deal_count/register_count。更新 SKILL.md："渠道ROI/哪个渠道最好/来源排名" 触发。

## 链路五：流失预警

- [x] 【5.1 优化流失预警展示】只改 SKILL.md。churn_warning 返回结果的展示格式改为：先显示统计摘要（🔴紧急X个 🟠高风险X个 🟡中风险X个 🟢低风险X个），然后按紧急程度列出客户：每行格式"🔴 张三 | 种植牙 | XX医院 | 18天未跟进 | 建议：今天立即联系"。建议内容根据 risk_level 生成（critical→立即联系、high→今天内联系、medium→本周联系、low→按计划跟进）。注意不要使用花括号。
- [x] 【5.2 这个客户为什么不跟了】在 dental_mcp_server.py 新增 client_drop_analysis(client_id: int) 工具。查询：(1) un_channel_client 获取 client_status, no_order_type, KfId, RegisterTime (2) un_channel_crm_log 获取最后3条跟进记录（ORDER BY addtime DESC LIMIT 3）(3) un_channel_crm 获取最近的提醒是否已处理。分析逻辑：如果 client_status=5 → 客户标记为无意向；如果 no_order_type>0 → 有明确的无法派单原因（查 un_linkage 转中文）；如果最后跟进>14天 → 可能被遗忘；如果跟进内容含"不想做/考虑/太贵/害怕" → 客户犹豫。返回 {client_name, status_text, last_follow_time, last_follow_content, drop_reason, recommended_action}。更新 SKILL.md："为什么不跟了/客户怎么了/这个客户什么情况" + 流失预警上下文触发。
