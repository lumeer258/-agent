"""
storage.py - 数据库存储模块
用于连接 MySQL 并保存/查询 AI 分析结果
"""
import json
import pymysql
from dotenv import load_dotenv
import os
from datetime import datetime

# 1. 加载环境变量（从 .env 文件中读取密码）
load_dotenv()

def get_db_connection():
    """建立并返回一个数据库连接"""
    return pymysql.connect(
        host='localhost',
        port=3306,
        user='root',
        password=os.getenv('DB_PASSWORD'),
        database='finance_agent_db',
        charset='utf8mb4',
        cursorclass=pymysql.cursors.DictCursor # 让返回结果以字典形式返回
    )

def save_analysis_result(data):
    """
    将AI的分析结果保存到数据库
    :param data: 包含各种字段的字典
    :return: 是否保存成功
    """
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            sql = """INSERT INTO ai_analysis_results 
                     (query_text, stock_code, time_range, core_conclusion, 
                      confidence_score, impact_level, final_judgement, related_tags) 
                     VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"""
            
            # 确保 tags 是 JSON 字符串格式
            tags_str = json.dumps(data.get('related_tags', []), ensure_ascii=False)
            
            cursor.execute(sql, (
                data.get('query_text'),
                data.get('stock_code'),
                data.get('time_range'),
                data.get('core_conclusion'),
                data.get('confidence_score'),
                data.get('impact_level'),
                data.get('final_judgement'),
                tags_str
            ))
            conn.commit()
        return True
    except Exception as e:
        print(f"[数据库错误] 保存数据失败: {e}")
        if conn: conn.rollback()
        return False
    finally:
        if conn: conn.close()

# 简单测试一下连接是否通畅
if __name__ == '__main__':
    try:
        conn = get_db_connection()
        print(">>> 数据库连接测试成功！")
        conn.close()
    except Exception as e:
        print(f">>> 数据库连接失败: {e}")