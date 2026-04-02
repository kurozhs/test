#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
测试 customer_search 工具
"""

import json
from dental_mcp_server import customer_search

def test_customer_search():
    """测试客户搜索功能"""
    print("=== 测试客户搜索功能 ===\n")
    
    # 测试1: 搜索姓名
    print("1. 测试姓名搜索 (keyword='张三'):")
    result = customer_search("张三")
    print(result[:200] + "..." if len(result) > 200 else result)
    print()
    
    # 测试2: 搜索手机号后四位
    print("2. 测试手机号后四位搜索 (keyword='1234'):")
    result = customer_search("1234")
    print(result[:200] + "..." if len(result) > 200 else result)
    print()
    
    # 测试3: 空关键词
    print("3. 测试空关键词:")
    result = customer_search("")
    print(result)
    print()
    
    # 测试4: 太短的关键词
    print("4. 测试太短的关键词 (keyword='a'):")
    result = customer_search("a")
    print(result)
    print()

if __name__ == "__main__":
    test_customer_search()