# Web Builder 模板方案 — 交接文档 v2

> 日期: 2026-04-03 (v4 更新)
> 状态: 双向同步验证通过 ✅
> 上次会话完成: 双向同步脚本验证 (2026-04-03 两个方向均正常)
> 下次会话目标: 端到端钉钉实测验证

---

## 一、已完成

### 1. Astro 模板 ✅
- 位置: `deer-flow/skills/custom/web-builder/templates/astro-medical/`
- 结构: Layout + Navbar/Hero/ServiceCard/DoctorCard/CTA/ContactForm/Footer
- 所有组件从 `src/content/site.json` 读内容
- CSS 变量控制主题色 (`--color-primary/secondary/accent`)
- `pnpm build` 验证通过，5 页面 <1s

### 2. SKILL.md 重写 ✅
- 从 9-Agent 流水线改为 "Claude 当数字员工写代码" 模式
- Step 1: cp 模板 → Step 2: 改文件 → Step 3: build + register + preview
- `<SKILL_DIR>` 路径占位符
- 位置: `deer-flow/skills/custom/web-builder/SKILL.md`

### 3. 钉钉 richText 修复 ✅
- 文件: `deer-flow/backend/app/channels/dingtalk.py`
- 改动: 把 `richText` 从文件分支移出，提取文本内容走文本分支
- Gateway 需要重启才生效（kill uvicorn + 重新启动）

### 4. build_site.py register action ✅
- `--action register --site {slug} --output ~/工作/my_country/sites`
- 创建: Site + Pages + Landing Pages + Sections（同时挂 Page 和 LandingPage）
- Sections data 格式对齐 section_style.default_data schema
- 自动启动 preview server + 同步 preview_url 到 Laravel
- 处理 slug 唯一约束冲突（软删除残留 → 加时间戳后缀）

### 5. Laravel pages/{id}/sections API ✅
- 新增: `PageSectionController.php` (CRUD)
- 路由: `routes/api.php` 加了 `pages/{page}/sections` 4 个路由
- 同时加了 `backend_client.py` 的 `create_page_section()` 方法

### 6. SectionRenderer 500 修复 ✅
- 文件: `site_management/app/Services/SectionRenderer.php:87`
- 问题: `array_merge()` 收到 string（default_data 是 JSON string 没被 decode）
- 修复: 加了 `is_string()` 判断 + `json_decode()` 防御

---

## 二、已知问题

### A. DeerFlow 沙箱路径映射 ✅ 已修复
- SKILL.md 所有路径改为 `/mnt/user-data/workspace/sites/$SLUG`（沙箱虚拟路径）
- `build_site.py` register action 加了自动同步: 检测到沙箱路径时，自动 `shutil.copytree` 到 `~/工作/my_country/sites/`
- preview server 从真实路径启动，确保外部可访问
- Laravel `local_path` 也更新为真实路径

### B. Laravel SoftDeletes slug 唯一约束
- delete 后 create 同 slug 报 422
- 当前方案: fallback 加时间戳后缀
- 正确方案: Laravel migration 改为 unique 约束排除 soft deleted，或 forceDelete

---

## 三、双向同步 ✅ 已完成

完整链路:
```
钉钉建站 ──→ 模板build ──→ Laravel后台有内容 ──→ 预览链接  ✅ 已通
Laravel编辑 ──→ sync-from-laravel ──→ rebuild ──→ 预览刷新  ✅ 已通
钉钉修改 site.json ──→ sync-to-laravel ──→ Laravel同步     ✅ 已通
```

### 链路 3: Laravel后台编辑 → 预览同步 ✅
- 脚本: `~/工作/my_country/src/webbuilder/sync_site.py`
- 用法: `python3 -m webbuilder.sync_site sync-from-laravel --slug guangkun-dental`
- 逻辑: 从 Laravel pages/{id}/sections API 拉数据 → 映射到 site.json 格式（保留 brand/theme/seo 等全局字段和 _en 双语） → 写入 site.json → pnpm build
- 已测试: 4页面5个section全部映射正确

### 链路 4: 钉钉修改 → Laravel 同步 ✅
- 用法: `python3 -m webbuilder.sync_site sync-to-laravel --slug guangkun-dental`
- 逻辑: 读 site.json → 映射成 Laravel section data → PUT /pages/{id}/sections/{id} 更新
- 已测试: 5个section全部 PUT 200，双向往返数据一致

### 新增/修改文件
| 文件 | 改动 |
|------|------|
| `~/工作/my_country/src/webbuilder/sync_site.py` | 新建，sync-from-laravel + sync-to-laravel |
| `~/工作/my_country/src/webbuilder/backend_client.py` | +list_page_sections() +update_page_section() |
| `deer-flow/skills/custom/web-builder/SKILL.md` | 所有路径改为 /mnt/user-data/workspace/sites/ |
| `deer-flow/skills/custom/web-builder/scripts/build_site.py` | register action 加沙箱→真实路径自动同步 |

### 待做: 钉钉端到端实测
1. 在钉钉发"帮我建个网站"，走完 4 轮需求收集
2. 验证 Agent 是否在 /mnt/user-data/workspace/sites/ 下建站
3. 验证 register 后是否自动同步到 ~/工作/my_country/sites/
4. 验证预览链接是否可访问
5. 验证 Laravel 后台是否有数据

---

## 四、环境信息

- DeerFlow: 进程在跑，gateway 端口 8001，langgraph 端口 2024
- Laravel: https://site-mgmt.servbay.host/api, token: `2|4Z8LTFNxy4inVvGS3KeunLHPcUicVrCrcGfoUKOW9ca44632`
- 当前测试站点: site_id=16, slug=guangkun-dental (带时间戳后缀)
- 预览: http://10.39.14.49:4042
- DeerFlow 本地: `~/deer-flow/` (注意: 不是 ~/工作/zsk/deer-flow/)
- 模板: `~/deer-flow/skills/custom/web-builder/templates/astro-medical/`
- 站点输出: `~/工作/my_country/sites/guangkun-dental/`
- DeerFlow 清理: 删 `checkpoints.db` + `channels/store.json` 后重启

---

## 五、改动文件清单

| 文件 | 改动 |
|------|------|
| `deer-flow/skills/custom/web-builder/templates/astro-medical/*` | 新建，完整 Astro 模板 |
| `deer-flow/skills/custom/web-builder/SKILL.md` | 重写为模板模式 + 路径改为沙箱虚拟路径 |
| `deer-flow/skills/custom/web-builder/scripts/build_site.py` | 加 register action + 沙箱→真实路径同步 |
| `deer-flow/backend/app/channels/dingtalk.py` | richText 修复 |
| `工作/my_country/src/webbuilder/backend_client.py` | +create/list/update_page_section() |
| `工作/my_country/src/webbuilder/sync_site.py` | **新建** 双向同步脚本 |
| `工作/site_management/app/Http/Controllers/Api/PageSectionController.php` | 新建 |
| `工作/site_management/routes/api.php` | 加 pages sections 路由 |
| `工作/site_management/app/Services/SectionRenderer.php` | array_merge 500 修复 |

---

## 六、下次会话启动指令

"读 HANDOFF-WEBBUILDER.md，做钉钉端到端实测。在钉钉发'帮我建个网站'，验证完整链路是否跑通。"
