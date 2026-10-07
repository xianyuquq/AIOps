"""查看已登记的服务器配置（不涉及解密，只看是否填了凭据）。

用法：python tests/inspect_servers.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import SessionLocal
from app.models import DatabaseInstance, Server

db = SessionLocal()
try:
    servers = db.query(Server).order_by(Server.id).all()
    print(f"共 {len(servers)} 台服务器\n")
    print(f"{'ID':<4}{'名称':<16}{'地址':<24}{'用户':<12}{'协议':<7}{'认证':<10}{'凭据':<8}{'状态':<9}错误")
    print("-" * 118)
    for s in servers:
        has_secret = "已填" if (s.secret_ciphertext and s.secret_nonce) else "★空★"
        err = (s.last_error or "")[:40]
        print(
            f"{s.id:<4}{s.name[:14]:<16}{(s.host + ':' + str(s.port))[:22]:<24}"
            f"{s.username[:10]:<12}{s.protocol:<7}{s.auth_type:<10}{has_secret:<8}"
            f"{s.status:<9}{err}"
        )

    # 重点提示可能导致连接失败的问题
    print()
    problems = []
    for s in servers:
        if s.auth_type == "password" and not (s.secret_ciphertext and s.secret_nonce):
            problems.append(f"  · [{s.name}] 认证方式为「密码」但没有保存任何密码 → 必然认证失败")
        if s.auth_type == "private_key" and not (s.secret_ciphertext and s.secret_nonce):
            problems.append(f"  · [{s.name}] 认证方式为「私钥」但没有保存私钥内容 → 必然认证失败")
        if s.protocol == "winrm":
            problems.append(f"  · [{s.name}] 协议为 WinRM，但 WinRM 执行器尚未实现 → 无法连接")

    if problems:
        print("发现可能导致连接失败的问题：")
        for p in problems:
            print(p)
    else:
        print("未发现「凭据缺失」类问题。")

    dbs = db.query(DatabaseInstance).all()
    if dbs:
        print(f"\n另有 {len(dbs)} 个数据库实例")
finally:
    db.close()
