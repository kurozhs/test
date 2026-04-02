# DeerFlow + Dental CRM 智能客服系统 — 完整交接文档

> 日期: 2026-03-31
> 项目: 把口腔 CRM 数据系统接入 DeerFlow AI Agent，实现钉钉自然语言对话查询
> 状态: **两轮迭代全部完成，11 条业务链路、36 个 MCP 工具**

---

## 一、做了什么（一句话）

把原来 7700 行的 `dental_chat_api.py` 单体 Flask 服务拆分，**数据查询层**通过 MCP 协议暴露给 DeerFlow Agent，**语义理解和回复生成**交给 DeerFlow 的 LLM（Qwen3/Claude）。用户在钉钉里用自然语言说话，Agent 自动识别身份、理解意图、调用对应数据工具、格式化回复。

---

## 二、系统架构

```
钉钉用户 1v1 私聊
       ↓ (WebSocket Stream)
DeerFlow DingTalk Channel
  - 接收消息，注入 [channel=dingtalk, user_id=staff_id]
       ↓
DeerFlow Lead Agent (LangGraph + LLM)
  - 系统提示含 dental-crm SKILL（306行业务知识）
  - 自动身份识别（首条消息调 resolve_staff_identity）
  - 角色权限控制（kf/manager/director）
  - 上下文串联（客户/医院/来源跨消息记忆）
  - 图表生成规则（漏斗/折线/柱状/饼图映射）
       ↓ MCP over SSE
dental-crm MCP Server (:8091)
  - 36 个数据查询工具
  - 手机号自动脱敏
  - 角色+团队判断
       ↓ PyMySQL
MySQL (hospital_db + qudao_db)
```

---

## 三、文件清单

### 核心文件（DB-GPT-main 目录）

| 文件 | 行数 | 作用 |
|------|------|------|
| `dental_mcp_server.py` | 4275 | MCP Server 主文件，36 个工具 |
| `dental_kb_queries.py` | ~200 | 8 个知识库查询函数（从 smart_query 提取） |
| `dental_chat_api.py` | 7725 | 原始 Flask API（数据查询函数被 MCP 复用，LLM 编排层已由 DeerFlow 替代） |
| `auto_iterate.py` | ~370 | 自驱迭代器（Claude API 驱动，本地工具执行） |
| `ITERATION_PLAN.md` | - | 第一轮迭代计划（13/13 完成） |
| `ITERATION_PLAN_R2.md` | - | 第二轮迭代计划（11/11 完成） |
| `iteration_log.md` | - | 自动迭代执行日志 |
| `precompute_stats_sync.py` | - | 预计算统计脚本（保持不变） |

### DeerFlow 侧文件

| 文件 | 作用 |
|------|------|
| `deer-flow/extensions_config.json` | MCP 连接配置（dental-crm SSE :8091） |
| `deer-flow/skills/custom/dental-crm/SKILL.md` | 306 行领域知识 Skill |
| `deer-flow/skills/custom/dental-report/SKILL.md` | HTML 报告模板 Skill |
| `deer-flow/backend/app/channels/manager.py` | 改动：注入 channel_name + user_id 到消息前缀 |
| `deer-flow/backend/.../prompt.py:256` | 修复：{project_name} → <project_name> |

---

## 四、36 个 MCP 工具一览

### 客服日常（6 个）
| 工具 | 场景 | 来源 |
|------|------|------|
| `my_crm_reminders` | "我今天要跟进谁" | un_channel_crm |
| `my_recent_followups` | "最近三天跟了什么客户" | un_channel_crm_log |
| `my_daily_stats` | "我今天的数据" | 多表聚合 |
| `customer_search` | "帮我找一下张三" | un_channel_client |
| `batch_customer_overview` | "我名下所有客户" | un_channel_client |
| `my_pending_deals` | "我的单子审核了吗" | un_channel_paylist |

### 客户分析（6 个）
| 工具 | 场景 |
|------|------|
| `customer_info` | 客户基本信息（轻量） |
| `customer_full_analysis` | 客户全维度深度分析（含7维成交概率） |
| `customer_follow_records` | 跟进记录列表 |
| `customer_orders` | 成交+派单记录 |
| `conversion_analysis` | 转化率+成交周期分析 |
| `client_drop_analysis` | "这个客户为什么不跟了" |

### 医院相关（5 个）
| 工具 | 场景 |
|------|------|
| `recommend_hospital_for_client` | "这个客户派什么医院合适" |
| `hospital_ranking` | 医院排名（按派单量） |
| `hospital_deals` | 医院成交数据 |
| `hospital_analysis` | 医院转化漏斗+地区实力 |
| `knowledge_base_query` | 推荐医院/排班/价格/活动（8种子类型） |

### 派单追踪（3 个）
| 工具 | 场景 |
|------|------|
| `my_dispatch_tracking` | "派出去的客户到院了吗" |
| `dispatch_no_arrival` | "哪些派了但没到院"（高价值预警） |
| `check_client_duplicate` | "这个客户是重单吗" |

### 领导分析（5 个）
| 工具 | 场景 |
|------|------|
| `operations_overview` | 漏斗/时间趋势/部门/公司数据 |
| `department_period_comparison` | "这个月各部门对比上个月" |
| `department_drill_down` | "BCG怎么掉了这么多"（下钻） |
| `kf_performance_ranking` | "客服业绩排名" |
| `kf_period_comparison` | "这个客服跟上个月比怎么样" |

### 渠道分析（3 个）
| 工具 | 场景 |
|------|------|
| `source_analysis` | 单个渠道效果分析 |
| `source_compare` | 多渠道对比 |
| `source_roi_ranking` | "哪个渠道ROI最高" |

### 预警与风控（4 个）
| 工具 | 场景 |
|------|------|
| `churn_warning` | 流失预警客户列表 |
| `recent_duplicates` | "最近有重单吗" |
| `refund_and_bad_debt` | "有退款或坏账吗" |
| `no_dispatch_analysis` | "为什么这些客户没派单" |

### 韩国业务（2 个）
| 工具 | 场景 |
|------|------|
| `korean_schedule` | 客户韩国行程时间线 |
| `korean_today_schedule` | "今天有哪些韩国客户要接" |

### 身份与权限（2 个）
| 工具 | 场景 |
|------|------|
| `resolve_staff_identity` | 钉钉身份→CRM身份（含角色+团队） |
| `sales_rep_stats` | 客服个人业绩数据 |

---

## 五、SKILL.md 核心规则（306 行）

| 规则板块 | 内容 |
|---------|------|
| 自动身份识别 | 首条消息从 `[channel=dingtalk, user_id=xxx]` 提取 staff_id，调 resolve_staff_identity |
| 角色权限控制 | kf 只看自己数据，manager 看团队，director 看全公司 |
| 上下文串联 | 客户ID/医院名/来源名跨消息记忆，"他/这个/对比一下"自动关联上文 |
| 医院串联查询 | 推荐医院→问详情→问排班→问价格，自动传递医院名 |
| 图表映射 | 11种数据→对应图表类型（漏斗/折线/柱状/饼图） |
| 钉钉格式化 | emoji风险标记、markdown表格、进度条、涨跌箭头 |
| 空数据兜底 | 结果为空时自动扩大查询范围 |
| 报告触发 | 领导查3+分析后主动提议生成HTML报告 |

---

## 六、数据库依赖

### hospital_db（知识库，query_db）
| 表 | 用途 |
|----|------|
| robot_kb_hospitals | 合作医院信息 |
| robot_kb_doctors | 医生信息 |
| robot_kb_hospital_doctors | 医院-医生关联 |
| doctor_schedules | 医生排班 |
| appointment_slots | 预约号源 |
| hospital_promotions | 优惠活动 |
| robot_kb_prices | 项目价格 |
| doctor_career_history | 医生变动 |
| precomputed_* | 预计算缓存表（4张） |

### qudao_db（渠道CRM，query_qudao_db）
| 表 | 用途 |
|----|------|
| un_channel_client | 客户主表 |
| un_channel_crm | CRM跟进提醒 |
| un_channel_crm_log | 跟进记录日志 |
| un_channel_paylist | 成交/支付记录 |
| un_hospital_order | 派单记录 |
| un_admin | 员工/客服表 |
| un_linkage | 数据字典（意向项目/地区/来源等） |
| un_channel_client_repetition | 注册重单 |
| un_hospital_contact_info_repeat | 派单重单 |
| un_hospital_company | 医院公司表 |
| un_schedule | 韩国行程 |
| un_custom_archives | 海外客户档案 |

---

## 七、启动方式

```bash
# 1. 启动 MCP Server（DB-GPT-main 目录）
cd ~/工作/zsk/DB-GPT-main
source .venv/bin/activate
python dental_mcp_server.py
# → SSE 模式, 端口 8091, 36 个工具

# 2. 启动 DeerFlow（deer-flow 目录）
cd ~/deer-flow
make dev
# → LangGraph :2024 + Gateway :8001 + Frontend :3000 + Nginx :2026

# 3. 验证连接
curl -s http://localhost:8091/sse  # MCP Server SSE endpoint
curl -s http://localhost:2024/ok   # LangGraph health
curl -s http://localhost:8001/api/models  # Gateway models
```

---

## 八、自动迭代器（auto_iterate.py）

当需要批量迭代时使用，无需人工干预：

```bash
# 单次执行下一个任务
python auto_iterate.py

# 循环模式（每3分钟一个任务，最多N轮）
python auto_iterate.py --loop --interval 3 --max-rounds 15

# 后台运行
nohup python auto_iterate.py --loop --interval 3 --max-rounds 15 > /tmp/auto_iterate.log 2>&1 &
```

**原理**：读 ITERATION_PLAN*.md → 找下一个 `- [ ]` → 调 Claude Sonnet API → Claude 用 read_file/write_file/str_replace/bash 工具自己改代码+验证 → 标记 `[x]`

**配置**：
- `PLAN_FILE`：指向当前迭代计划文件
- `MODEL`：claude-sonnet-4-20250514（通过公司代理）
- `MAX_TOOL_ROUNDS`：50（每个任务最多50轮工具调用）
- 529 Overloaded 自动重试（5次，递增等待）

---

## 九、已知问题与注意事项

| 问题 | 说明 |
|------|------|
| SKILL.md 花括号 | 不能用 `{变量}` 格式，会被 Python str.format() 误解析。用 `<变量>` 代替 |
| MCP SDK 版本 | 当前 mcp 1.6.0 只支持 stdio/sse，不支持 streamable-http |
| Qwen3 tool calling | Qwen3-Omni-30B 通过 LangGraph API 调用时 tool calling 不稳定（输出 XML 而非结构化 function call），自动迭代改用 Claude Sonnet |
| DB 字符串ID | 渠道DB返回的数字ID是字符串类型，比较前必须 int() 转换 |
| 预计算缓存 | USE_PRECOMPUTED_STATS=0\|1 控制，48小时新鲜度自动回退实时查询 |
| dental_mcp_server.py 体积 | 已膨胀到 4275 行（自动迭代生成），需要人工 review 代码质量 |
| DingTalk 不能发图片 | webhook API 限制，图表只能通过 Web UI 链接查看 |
| un_admin 角色判断 | 通过 parentId 有无下属推断角色，不一定准确，可能需要和实际权限体系对齐 |

---

## 十、下一步方向

### 已完成（Phase 1+2）
- [x] 智能客服助手：11条业务链路，自然语言对话
- [x] 权限控制：角色矩阵 + 数据脱敏
- [x] 图表规则：SKILL 映射 + HTML 报告模板

### 待做（Phase 3+4）
- [ ] **Web 客服工作台**：DeerFlow Web UI 定制，个人看板+客户列表+AI建议
- [ ] **管理驾驶舱**：部门对比图表+转化漏斗+预警大盘
- [ ] **每日自动推送**：cron 驱动，早间跟进提醒+晚间业绩小结
- [ ] **dental_mcp_server.py 代码审查**：自动生成的 4275 行需要人工 review 质量
- [ ] **真实用户测试**：让客服在钉钉上跑一周，收集反馈迭代

---

## 十一、关键决策记录

| 决策 | 原因 |
|------|------|
| MCP over SSE（不是 Custom Tools） | 隔离部署，DB 连接池独立，Flask 可退役 |
| 36 个分组工具（不是 60+原子函数） | 减少 LLM 工具选择压力，按业务场景分组 |
| SKILL.md 指引上下文串联（不是代码实现） | 让 LLM 自己做上下文理解，比硬编码规则更灵活 |
| Claude Sonnet 驱动自动迭代（不是 Qwen3） | Qwen3 的 tool calling 在 API 模式下不稳定 |
| 角色通过 parentId 推断（不是 RoleId） | un_admin 表没有明确的 RoleId 字段 |
