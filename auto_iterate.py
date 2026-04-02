#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
自驱迭代器 — Claude API 直接驱动，本地执行工具

用户给方向（ITERATION_PLAN.md），脚本自动逐个任务执行。
Claude 通过 tool_use 调用本地 read_file / write_file / bash 工具，
自己看代码、改代码、验证结果。

使用方式：
    python auto_iterate.py                             # 执行下一个任务
    python auto_iterate.py --loop --interval 5 --max-rounds 16  # 循环模式
    nohup python auto_iterate.py --loop --interval 5 --max-rounds 16 > /tmp/auto_iterate.log 2>&1 &
"""

import argparse
import json
import logging
import os
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / "deer-flow" / ".env", override=False)
load_dotenv(override=False)

import anthropic

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("auto_iterate")

PLAN_FILE = Path(__file__).parent / "ITERATION_PLAN_R2.md"
LOG_FILE = Path(__file__).parent / "iteration_log.md"
PROJECT_ROOT = Path(__file__).parent  # DB-GPT-main
DEERFLOW_ROOT = Path(__file__).parent.parent / "deer-flow"

MODEL = "claude-sonnet-4-20250514"
MAX_TOKENS = 8192
MAX_TOOL_ROUNDS = 50  # 最多 50 轮工具调用

# ============================================================
# 工具定义
# ============================================================

TOOLS = [
    {
        "name": "read_file",
        "description": "Read a file from the local filesystem. Returns file content.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute file path to read"},
                "offset": {"type": "integer", "description": "Line number to start reading from (1-based). Optional."},
                "limit": {"type": "integer", "description": "Max number of lines to read. Optional, default 500."},
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write content to a file. Creates or overwrites the file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute file path to write"},
                "content": {"type": "string", "description": "Full file content to write"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "str_replace",
        "description": "Replace a specific string in a file. The old_string must be unique in the file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute file path"},
                "old_string": {"type": "string", "description": "Exact string to find and replace"},
                "new_string": {"type": "string", "description": "Replacement string"},
            },
            "required": ["path", "old_string", "new_string"],
        },
    },
    {
        "name": "bash",
        "description": "Execute a bash command. Returns stdout and stderr. Timeout: 60 seconds.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Bash command to execute"},
            },
            "required": ["command"],
        },
    },
    {
        "name": "task_complete",
        "description": "Signal that the current task is complete. Provide a summary of what was done.",
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "Brief summary of what was accomplished and verified"},
                "success": {"type": "boolean", "description": "Whether the task was successfully completed"},
            },
            "required": ["summary", "success"],
        },
    },
]

# ============================================================
# 工具执行
# ============================================================

ALLOWED_PATHS = [str(PROJECT_ROOT), str(DEERFLOW_ROOT)]


def _check_path(path: str) -> str:
    """安全检查：只允许访问项目目录"""
    resolved = str(Path(path).resolve())
    if not any(resolved.startswith(ap) for ap in ALLOWED_PATHS):
        raise ValueError(f"Access denied: {path} is outside allowed directories")
    return resolved


def execute_tool(name: str, input_data: dict) -> str:
    """执行工具并返回结果"""
    try:
        if name == "read_file":
            path = _check_path(input_data["path"])
            p = Path(path)
            if not p.exists():
                return f"Error: File not found: {path}"
            text = p.read_text(encoding="utf-8", errors="replace")
            lines = text.splitlines(keepends=True)
            offset = input_data.get("offset", 1) - 1
            limit = input_data.get("limit", 500)
            selected = lines[max(0, offset):offset + limit]
            numbered = "".join(f"{offset + i + 1:5d}\t{line}" for i, line in enumerate(selected))
            return numbered[:50000]  # cap output

        elif name == "write_file":
            path = _check_path(input_data["path"])
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_text(input_data["content"], encoding="utf-8")
            return f"File written: {path} ({len(input_data['content'])} chars)"

        elif name == "str_replace":
            path = _check_path(input_data["path"])
            content = Path(path).read_text(encoding="utf-8")
            old = input_data["old_string"]
            new = input_data["new_string"]
            count = content.count(old)
            if count == 0:
                return f"Error: old_string not found in {path}"
            if count > 1:
                return f"Error: old_string found {count} times in {path}, must be unique"
            updated = content.replace(old, new, 1)
            Path(path).write_text(updated, encoding="utf-8")
            return f"Replaced in {path} (1 occurrence)"

        elif name == "bash":
            cmd = input_data["command"]
            result = subprocess.run(
                cmd, shell=True, capture_output=True, text=True, timeout=60,
                cwd=str(PROJECT_ROOT),
                env={**os.environ, "PATH": f"{PROJECT_ROOT}/.venv/bin:{os.environ.get('PATH', '')}"},
            )
            output = ""
            if result.stdout:
                output += result.stdout[:30000]
            if result.stderr:
                output += f"\n[stderr]\n{result.stderr[:10000]}"
            output += f"\n[exit_code: {result.returncode}]"
            return output.strip()

        elif name == "task_complete":
            return json.dumps(input_data)

        else:
            return f"Unknown tool: {name}"

    except subprocess.TimeoutExpired:
        return "Error: Command timed out (60s limit)"
    except Exception as e:
        return f"Error: {type(e).__name__}: {e}"


# ============================================================
# Claude 对话循环
# ============================================================

def run_agent(task_text: str) -> tuple[bool, str]:
    """让 Claude 执行一个任务，支持多轮工具调用"""
    client = anthropic.Anthropic(
        api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
        base_url=os.environ.get("ANTHROPIC_BASE_URL"),
    )

    system_prompt = f"""你是一个自动迭代 Agent，负责逐步优化 DeerFlow + Dental CRM 集成项目。

## 项目结构
- MCP Server: {PROJECT_ROOT}/dental_mcp_server.py (FastMCP, 15+ tools)
- KB Queries: {PROJECT_ROOT}/dental_kb_queries.py
- Main API (read-only reference): {PROJECT_ROOT}/dental_chat_api.py
- DeerFlow Skill: {DEERFLOW_ROOT}/skills/custom/dental-crm/SKILL.md
- DeerFlow Report Skill: {DEERFLOW_ROOT}/skills/custom/dental-report/SKILL.md

## 规则
1. 先 read_file 看现有代码，理解结构后再改
2. 用 str_replace 做精确修改（推荐），或 write_file 重写整个文件
3. 改完必须用 bash 验证（python -c "import ..." 检查语法）
4. 完成后调用 task_complete 工具报告结果
5. 如果任务涉及 SKILL.md 修改，不要使用花括号 {{}} 包裹变量名（会被 str.format 误解析），用 <变量名> 代替

现在时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}"""

    messages = [{"role": "user", "content": f"请执行以下任务：\n\n**{task_text}**\n\n开始。"}]

    for round_num in range(MAX_TOOL_ROUNDS):
        logger.info(f"  Round {round_num + 1}/{MAX_TOOL_ROUNDS}")

        # 带重试的 API 调用（处理 529 Overloaded）
        response = None
        for attempt in range(5):
            try:
                response = client.messages.create(
                    model=MODEL,
                    max_tokens=MAX_TOKENS,
                    system=system_prompt,
                    tools=TOOLS,
                    messages=messages,
                )
                break
            except anthropic.APIStatusError as e:
                if e.status_code == 529:
                    wait = 30 * (attempt + 1)
                    logger.warning(f"  API overloaded, waiting {wait}s (attempt {attempt + 1}/5)")
                    time.sleep(wait)
                else:
                    raise
            except anthropic.RateLimitError:
                wait = 60 * (attempt + 1)
                logger.warning(f"  Rate limited, waiting {wait}s (attempt {attempt + 1}/5)")
                time.sleep(wait)
        if response is None:
            return False, "API overloaded after 5 retries"

        # 收集响应
        assistant_content = response.content
        messages.append({"role": "assistant", "content": assistant_content})

        # 检查是否有 tool_use
        tool_uses = [b for b in assistant_content if b.type == "tool_use"]
        if not tool_uses:
            # 纯文本响应，任务结束
            text = "".join(b.text for b in assistant_content if hasattr(b, "text"))
            return True, text[:1000]

        # 执行所有工具
        tool_results = []
        task_done = False
        done_summary = ""
        done_success = False

        for tu in tool_uses:
            logger.info(f"    Tool: {tu.name}({json.dumps(tu.input, ensure_ascii=False)[:100]}...)")
            result = execute_tool(tu.name, tu.input)

            if tu.name == "task_complete":
                task_done = True
                info = json.loads(result)
                done_summary = info.get("summary", "")
                done_success = info.get("success", False)

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tu.id,
                "content": result[:30000],
            })

        messages.append({"role": "user", "content": tool_results})

        if task_done:
            return done_success, done_summary

        if response.stop_reason == "end_turn":
            text = "".join(b.text for b in assistant_content if hasattr(b, "text"))
            return True, text[:1000] or "Task completed (no explicit summary)"

    return False, "Reached max tool rounds without completion"


# ============================================================
# Plan 解析 + 主循环
# ============================================================

def parse_plan(plan_text: str) -> list[dict]:
    tasks = []
    for line in plan_text.splitlines():
        m = re.match(r'\s*-\s*\[([ xX])\]\s*(.*)', line)
        if m:
            done = m.group(1).lower() == 'x'
            tasks.append({"text": m.group(2).strip(), "done": done})
    return tasks


def find_next_task(tasks: list[dict]) -> tuple[int, dict] | None:
    for i, t in enumerate(tasks):
        if not t["done"]:
            return i, t
    return None


def mark_task_done(plan_text: str, task_text: str) -> str:
    escaped = re.escape(task_text)
    return re.sub(rf'(\s*-\s*)\[ \]\s*{escaped}', rf'\1[x] {task_text}', plan_text, count=1)


def append_log(task_text: str, status: str, summary: str):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = f"\n### [{timestamp}] {status}\n**Task:** {task_text[:200]}\n**Result:** {summary[:500]}\n"
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(entry)


def run_once() -> bool:
    if not PLAN_FILE.exists():
        logger.error(f"Plan file not found: {PLAN_FILE}")
        return False

    plan_text = PLAN_FILE.read_text(encoding="utf-8")
    tasks = parse_plan(plan_text)
    result = find_next_task(tasks)
    if result is None:
        logger.info("All tasks completed!")
        return False

    idx, task = result
    total = len(tasks)
    done_count = sum(1 for t in tasks if t["done"])
    logger.info(f"Progress: {done_count}/{total}. Next [{idx + 1}]: {task['text'][:80]}...")

    try:
        success, summary = run_agent(task["text"])
    except Exception as e:
        logger.exception(f"run_agent crashed: {e}")
        success, summary = False, f"Agent crashed: {type(e).__name__}: {e}"

    if success:
        updated = mark_task_done(plan_text, task["text"])
        PLAN_FILE.write_text(updated, encoding="utf-8")
        logger.info(f"Task COMPLETED: {task['text'][:60]}")
        append_log(task["text"], "COMPLETED", summary)
    else:
        logger.warning(f"Task FAILED: {summary[:200]}")
        append_log(task["text"], "FAILED", summary)

    return (total - done_count - (1 if success else 0)) > 0


def main():
    parser = argparse.ArgumentParser(description="自驱迭代器 (Claude API)")
    parser.add_argument("--loop", action="store_true", help="持续循环模式")
    parser.add_argument("--interval", type=int, default=5, help="循环间隔（分钟），默认 5")
    parser.add_argument("--max-rounds", type=int, default=16, help="最大轮数，默认 16")
    args = parser.parse_args()

    if not args.loop:
        run_once()
        return

    logger.info(f"Loop mode: interval={args.interval}min, max_rounds={args.max_rounds}")
    for rnd in range(1, args.max_rounds + 1):
        logger.info(f"=== Round {rnd}/{args.max_rounds} ===")
        has_more = run_once()
        if not has_more:
            break
        if rnd < args.max_rounds:
            logger.info(f"Sleeping {args.interval} minutes...")
            time.sleep(args.interval * 60)

    logger.info("Done.")


if __name__ == "__main__":
    main()
