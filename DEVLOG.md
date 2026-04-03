# DB-GPT 口腔客服系统 — 开发文档

> 最后更新: 2026-03-12

---

## 一、项目概述

**神舟商通·小通** — 基于 DB-GPT 的口腔/牙科医疗智能客服系统，通过钉钉机器人为客服人员和管理者提供：

- 医院/医生推荐、价格查询、排班信息
- 客户跟进管理（待跟进、逾期提醒、今日统计）
- 客户转化概率分析（AI 评分 A/B/C/D）
- 渠道来源分析、多渠道对比
- 多轮对话，带上下文记忆

---

## 二、系统架构

```
┌─────────────┐     WebSocket      ┌──────────────────┐     HTTP       ┌──────────────────┐
│   钉钉用户   │ ◄──── Stream ────► │  dingtalk_bot.py  │ ────────────► │ dental_chat_api  │
│  (手机/PC)   │     (无需公网IP)    │    (消息中间层)     │   localhost    │   (Flask:8090)   │
└─────────────┘                    └──────────────────┘    :8090        └────────┬─────────┘
                                     ▲ 身份识别                                  │
                                     │ session 管理                              ▼
                                   ┌────────────┐                    ┌───────────────────┐
                                   │ kf_users    │                   │ LLM (qwen3-omni)  │
                                   │  .json      │                   │ 182.114.59.224    │
                                   │ (身份缓存)   │                   │ :60329            │
                                   └────────────┘                    └───────────────────┘
                                                                              ▲
                                                                              │
                                                              ┌───────────────┴───────────────┐
                                                              │                               │
                                                     ┌────────────────┐              ┌────────────────┐
                                                     │  hospital_db   │              │   kfsyscb      │
                                                     │  (本地MySQL)    │              │  (OceanBase)   │
                                                     │  医院知识库      │              │  客服渠道系统    │
                                                     └────────────────┘              └────────────────┘
```

---

## 三、核心文件说明

| 文件 | 作用 | 备注 |
|------|------|------|
| `dental_chat_api.py` | 主服务（Flask, 端口 8090） | 所有 API 端点、智能查询、LLM 调用、会话存储 |
| `dingtalk_bot.py` | 钉钉机器人 | Stream 模式接入，身份识别，意图路由，调用 API |
| `.env` | 环境变量 | 数据库、LLM、钉钉凭证等配置 |
| `kf_users.json` | 身份缓存文件 | 钉钉用户 → 客服身份映射（自动生成） |
| `hospital_data_sync.py` | 医院数据同步 | 从渠道系统同步到本地知识库 |
| `doctor_data_sync.py` | 医生数据同步 | 同上 |
| `precompute_stats_sync.py` | 统计数据预计算 | 定期刷新缓存统计 |
| `sync_all.py` | 一键全量同步 | 调上面三个同步脚本 |

---

## 四、已完成的工作

### 4.1 钉钉机器人直连 DB-GPT（dingtalk_bot.py — 从零创建）

**背景**：最初考虑通过 OpenClaw 平台中转，但评估后发现直连更简单、无额外依赖、延迟更低。

**实现内容**：

1. **Stream 模式接入**
   - 使用 `dingtalk-stream` SDK，WebSocket 长连接
   - 无需公网 IP，无需 HTTPS 证书
   - 断线自动重连

2. **自动身份识别**（4 步链路，无需手动绑定）
   ```
   本地缓存(kf_users.json)
     → 数据库 un_admin 表（按钉钉昵称匹配）
       → 钉钉 API 获取真名 → 再用真名查数据库
         → 钉钉管理员自动赋予管理员角色
   ```

3. **权限控制**
   - 客服：只能看自己的客户数据（查询必带 `kf_id`）
   - 主管/管理员：可查看全量数据
   - 未识别用户：必须先绑定身份

4. **意图路由**
   - `today_stats`：今日跟进统计
   - `pending_followup`：待跟进客户列表
   - `overdue`：逾期客户
   - `chat`：通用对话（医院、医生、价格等）
   - `new_session` / `bind_identity`：会话和身份管理

5. **会话管理**
   - `SessionManager` 维护钉钉用户 → DB-GPT session_id 映射
   - 30 分钟超时自动清理
   - 支持 `/new` 手动开启新会话

### 4.2 多轮对话上下文修复（dental_chat_api.py 修改）

**问题**：用户反馈第 3-4 个问题丢失上下文。

**根因**：
- `call_llm()` 把系统指令 + 数据库结果 + 对话历史 + 当前问题全部拼成一个字符串，作为单条 user message 发给 LLM
- LLM 很难从混杂的长文本中识别嵌入的"对话历史"
- 部分专门处理分支（customer_analysis、source_list 等）直接返回结果，完全跳过历史上下文

**修复方案**：
1. **新增 `call_llm_messages()` 函数** — 支持传入标准的多轮 messages 数组
2. **重写 `generate_response()` 通用 chat 路径** — 改为多轮 messages 格式：
   ```
   [system]  你是口腔客服AI助手… 数据库查询结果: {…}
   [user]    北京种植牙推荐
   [assistant] 为您推荐以下3家…
   [user]    第一家价格怎么样
   [assistant] XX口腔的种植牙参考价…
   [user]    有什么优惠活动吗     ← 当前问题
   ```
3. **助手历史回复自动截断**（>600 字截断），防止 prompt 膨胀
4. **历史窗口从 3 轮扩展到 4 轮**（`history[-8:]`）
5. **添加口腔/非口腔区分规则** — 医美、眼科等非口腔问题礼貌说明不在服务范围

### 4.3 钉钉 API 权限配置

- 开通了 `qyapi_get_member` 权限（通讯录-用户信息读权限）
- 用于根据 `sender_staff_id` 获取用户真名和管理员状态

---

## 五、环境变量说明（.env）

```bash
# 医院知识库数据库（本地 MySQL）
DB_HOST=localhost
DB_PORT=3306
DB_USER=root
DB_PASSWORD=ServBay.dev
DB_NAME=hospital_db

# 客服系统数据库（渠道系统，OceanBase）
QUDAO_DB_HOST=192.168.103.227
QUDAO_DB_PORT=2883
QUDAO_DB_USER=oaxz@kfsys_tnt#szst_oceanbase_sec
QUDAO_DB_PASSWORD=yq4(Rv-ZMWvo4Sg%5q2!
QUDAO_DB_NAME=kfsyscb

# LLM
LLM_API_BASE=http://182.114.59.224:60329/v1
LLM_API_KEY=dummy
LLM_MODEL=qwen3-omni-30b

# API 安全
API_SECRET_KEY=dental_api_secret_2026

# 钉钉机器人
DINGTALK_CLIENT_ID=dingjubchal0g3m6k8lz
DINGTALK_CLIENT_SECRET=W0Hkh9ObAuGaJ0yQ9Dp6HD2p9buGmZrkCuXxRVldzVd_e6pre0esa1YMiXf4FEjs
DENTAL_API_BASE=http://localhost:8090
```

---

## 六、启动方式

```bash
cd /Users/wangbaiwei/工作/zsk/DB-GPT-main

# 1. 先启动主 API 服务
nohup python dental_chat_api.py > dental_chat_api.log 2>&1 &

# 2. 再启动钉钉机器人
nohup python dingtalk_bot.py > dingtalk_bot.log 2>&1 &

# 查看日志
tail -f dental_chat_api.log
tail -f dingtalk_bot.log

# 健康检查
curl http://localhost:8090/health
```

**启动顺序很重要**：dingtalk_bot.py 启动时会检查 dental_chat_api.py 是否在线，如果不在会有警告（但不会退出，会持续重试）。

---

## 七、API 端点一览

| 端点 | 方法 | 用途 | 认证 |
|------|------|------|------|
| `/health` | GET | 健康检查 | 无 |
| `/api/dental/chat` | POST | 智能对话（主接口） | 无 |
| `/api/dental/chat/stream` | POST | 流式对话（SSE） | 无 |
| `/api/dental/kf/today_stats` | POST | 今日跟进统计 | 无 |
| `/api/dental/kf/warning_stats` | POST | 客户跟进预警列表 | 无 |
| `/api/dental/customer/<id>` | GET | 客户详情+转化分析 | 无 |
| `/api/dental/source/list` | GET | 渠道来源列表 | 无 |
| `/api/dental/source/analyze` | POST | 单渠道分析 | 无 |
| `/api/dental/source/compare` | POST | 多渠道对比 | 无 |
| `/api/dental/query` | POST | 直接 SQL 查询（调试用） | Bearer Token |

---

## 八、数据库说明

### 8.1 hospital_db（本地 MySQL, localhost:3306）

医院知识库，由同步脚本定期从渠道系统同步。

| 核心表 | 作用 |
|--------|------|
| `robot_kb_hospitals` | 医院信息（名称、地址、评分、合作状态、价格列表） |
| `robot_kb_doctors` | 医生信息（姓名、职称、擅长、排班） |
| `robot_kb_promotions` | 活动优惠信息 |

### 8.2 kfsyscb（OceanBase, 192.168.103.227:2883）

客服渠道系统（线上生产库，只读查询）。

| 核心表 | 作用 |
|--------|------|
| `un_admin` | 客服人员信息（userid, username, realname, roleid） |
| `un_channel_client` | 客户信息（姓名、手机、意向项目、来源、状态） |
| `un_channel_follow` | 跟进记录 |
| `un_channel_order` | 成交订单 |
| `un_linkage` | 渠道来源分类 |
| `un_hospital` | 医院基础信息 |

---

## 九、身份识别与权限控制

### 识别流程

```
用户在钉钉发消息
  │
  ├─ 1. 查本地缓存 kf_users.json（按 sender_id）
  │     ↓ 命中 → 直接返回身份
  │
  ├─ 2. 查本地缓存（按钉钉昵称）
  │     ↓ 命中 → 返回 + 缓存 sender_id
  │
  ├─ 3. 查数据库 un_admin（按昵称匹配 realname/username）
  │     ↓ 命中 → 返回 + 缓存到本地
  │
  ├─ 4. 调钉钉 API 获取真名
  │     ├─ 4a. 用真名再查数据库 → 命中则返回
  │     └─ 4b. 如果是钉钉管理员 → 自动赋予"管理员"角色
  │
  └─ 5. 全部未命中 → 提示用户手动绑定
```

### 权限矩阵

| 角色 | 客户数据 | 渠道分析 | 查询限制 |
|------|---------|---------|---------|
| 客服 | 仅自己 | 不可用 | 必须带 kf_id |
| 主管/经理 | 全部 | 可用 | 可选带 kf_id |
| 管理员 | 全部 | 可用 | 无限制 |
| 未识别 | 禁止 | 禁止 | 必须先绑定 |

---

## 十、钉钉机器人命令

| 命令 | 作用 |
|------|------|
| 任何口腔相关问题 | 智能对话（医院推荐、价格、医生排班等） |
| `今天客户量如何` / `今日统计` | 查看今日跟进统计 |
| `待跟进` / `要跟进谁` | 查看待跟进客户列表 |
| `逾期` / `过期客户` | 查看逾期未跟进客户 |
| `新会话` / `/new` | 清除当前对话上下文，开启新会话 |
| `绑定 张三 5` | 手动绑定身份（姓名+工号） |

---

## 十一、待办 / 后续优化

### 高优先级

- [ ] **钉钉机器人无响应排查** — 当前 WebSocket 连接有周期性断线重连（约 16 分钟一次），需确认是网络环境还是 SDK 版本问题。可考虑：
  - 升级 `dingtalk-stream` SDK 版本
  - 添加心跳保活机制
  - 添加消息接收日志，区分"没收到消息"和"收到但处理失败"
- [ ] **多轮上下文验证** — 已改为多轮 messages 格式发给 LLM，需实测验证 3-5 轮对话的上下文连贯性
- [ ] **非口腔问题过滤** — 已在 system prompt 中添加规则，但效果取决于 LLM。可考虑在 `smart_query` 前增加意图分类器

### 中优先级

- [ ] **会话持久化** — 当前 `ConversationStore` 是内存存储，服务重启后丢失。可改为 Redis 或 SQLite
- [ ] **客户 ID 快捷查询** — 钉钉中直接发 `客户 4333303` 查转化分析
- [ ] **Markdown 消息卡片** — 钉钉支持 Markdown 格式消息，可以让回复更美观（表格、列表等）
- [ ] **流式回复** — 当前钉钉回复是等 LLM 全部生成完才发送，长回复可能等待较久。可考虑分段发送
- [ ] **数据同步定时任务** — 将 `sync_all.py` 配置为 cron 定时执行，保持知识库数据新鲜

### 低优先级

- [ ] **群聊支持优化** — 当前群聊中 @机器人 可用，但未做群级别的权限隔离
- [ ] **消息审计日志** — 记录所有对话到数据库，用于质量分析和合规审计
- [ ] **Web 管理后台** — 可视化查看机器人使用统计、对话记录、身份映射管理
- [ ] **多 LLM 支持** — 支持切换不同模型（如 deepseek、qwen-72b 等），做 A/B 测试

---

## 十二、常见问题排查

### Q: 钉钉发消息没反应

1. 检查 `dingtalk_bot.py` 进程是否在运行：`ps aux | grep dingtalk_bot`
2. 查看日志：`tail -f dingtalk_bot.log`
3. 如果日志里有 `open connection` 但没有 `收到消息`，说明 WebSocket 连接正常但消息未到达，检查：
   - 钉钉后台机器人是否已发布上线
   - Client ID / Secret 是否正确
   - 是否在正确的机器人对话窗口发送
4. 重启：`kill <pid> && nohup python dingtalk_bot.py > dingtalk_bot.log 2>&1 &`

### Q: 回复说"系统繁忙"

- LLM 服务不可达。检查 `http://182.114.59.224:60329/v1` 是否能访问
- `curl http://182.114.59.224:60329/v1/models` 测试连通性

### Q: 回复没有上下文

- 确认 `dental_chat_api.py` 已更新到多轮 messages 版本
- 查看 API 日志中是否有 `多轮对话: X条messages, 历史Y条`
- 发 `新会话` 清除旧 session，重新开始测试

### Q: 身份识别失败

- 检查 `kf_users.json` 中是否有映射
- 钉钉昵称和客服系统 `un_admin.realname` 是否一致
- 手动绑定：发送 `绑定 张三 5`

### Q: 服务重启后会话丢失

- 这是正常的。`ConversationStore` 是内存存储，重启后所有会话历史清空
- 用户发新消息时会自动创建新 session

---

## 十三、改代码指南

### 修改 dental_chat_api.py

- **添加新的查询意图**：在 `extract_keywords()` 中添加关键词匹配，在 `smart_query()` 中添加查询逻辑，在 `generate_response()` 中添加结果格式化
- **调整 LLM prompt**：在 `generate_response()` 末尾的 system message 中修改
- **修改会话超时**：`ConversationStore.__init__` 的 `ttl_seconds` 参数（默认 1800 秒 = 30 分钟）
- **修改历史轮数**：`generate_response()` 中的 `history[-8:]` 数字（8 = 4 轮对话）

### 修改 dingtalk_bot.py

- **添加新的钉钉命令**：在 `detect_intent()` 中添加关键词，在 `DentalBotHandler.process()` 中添加处理分支
- **修改权限规则**：在 `process()` 方法中修改 `is_manager` 判断逻辑
- **修改身份匹配**：在 `lookup_kf()` 中调整 4 步匹配链
- **修改会话超时**：`SessionManager.__init__` 的 `session_timeout` 参数（默认 1800 秒）

### 修改环境配置

- 修改 `.env` 文件后需重启对应服务才生效
- 钉钉凭证变更后需到钉钉开放平台重新发布应用

---

## 十四、依赖安装

```bash
pip install flask flask-cors flask-limiter
pip install pymysql
pip install dingtalk-stream
pip install python-dotenv
pip install requests
```
