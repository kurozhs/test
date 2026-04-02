#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
一键同步脚本 - 同步医院和医生数据
"""

import subprocess
import sys
from datetime import datetime

def run_sync(script_name, description):
    """运行同步脚本"""
    print(f"\n{'='*50}")
    print(f"  {description}")
    print(f"{'='*50}")

    result = subprocess.run(
        [sys.executable, script_name],
        capture_output=False
    )
    return result.returncode == 0

def main():
    print("\n" + "="*60)
    print("        知识库数据一键同步")
    print(f"        {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("="*60)

    # 1. 同步医院
    if not run_sync("hospital_data_sync.py", "同步医院数据"):
        print("❌ 医院同步失败")
        return

    # 2. 同步医生
    if not run_sync("doctor_data_sync.py", "同步医生数据"):
        print("❌ 医生同步失败")
        return

    # 3. 预计算客户分析统计
    if not run_sync("precompute_stats_sync.py", "预计算客户分析统计"):
        print("❌ 预计算同步失败")
        return

    print("\n" + "="*60)
    print("  ✅ 全部同步完成!")
    print("="*60 + "\n")

if __name__ == "__main__":
    main()
