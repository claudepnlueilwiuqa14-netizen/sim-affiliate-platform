#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
seed.py —— 灌入示例数据，跑起来就能演示

    python seed.py            # 首次灌入（已有数据则跳过，除非 --force）
    python seed.py --force    # 清库重灌
    python seed.py --simulate # 额外跑一轮模拟流量：正常单 / 自推自买 / 高频刷量

灌的内容（全部走 server 的真实业务函数，不是直接写 JSON）：
  4 家 eSIM 供应商 ｜ 10 个卡品 ｜ 1 个管理员 + 3 个推广员（分属 L1/L2/L3）
  真实下单流程产生的订单（含 1 笔被风控拒绝的自推自买、1 笔高频刷量可疑单）
  2 笔提现申请
"""
import os
import sys
import json
import random
import argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server  # noqa: E402

SUPPLIERS = [
    {"name": "eSIM Go", "api_type": "REST", "contact": "partners@esimgo.example"},
    {"name": "Maya Mobile", "api_type": "REST", "contact": "api@maya.example"},
    {"name": "Airalo Partner API", "api_type": "REST", "contact": "partner@airalo.example"},
    {"name": "本地运营商渠道（联通国际）", "api_type": "manual", "contact": "渠道经理"},
]

PLANS = [
    {"title": "亚洲 8 国 1GB / 7天", "region": "亚洲", "data_gb": 1, "validity_days": 7,
     "price": 4.5, "cost": 2.2, "cps_rate": 0.20, "desc": "日韩新马泰越印菲，落地即激活"},
    {"title": "亚洲 8 国 3GB / 15天", "region": "亚洲", "data_gb": 3, "validity_days": 15,
     "price": 9.9, "cost": 4.8, "cps_rate": 0.20, "desc": "短途出差与旅行主力款"},
    {"title": "欧洲 32 国 5GB / 30天", "region": "欧洲", "data_gb": 5, "validity_days": 30,
     "price": 15.9, "cost": 7.6, "cps_rate": 0.18, "desc": "申根区全覆盖，含英国"},
    {"title": "欧洲 32 国 10GB / 30天", "region": "欧洲", "data_gb": 10, "validity_days": 30,
     "price": 24.9, "cost": 12.0, "cps_rate": 0.18, "desc": "大流量，导航/视频无压力"},
    {"title": "美国 3GB / 30天", "region": "北美", "data_gb": 3, "validity_days": 30,
     "price": 11.5, "cost": 5.4, "cps_rate": 0.16, "desc": "T-Mobile 网络，含热点"},
    {"title": "美国 10GB / 30天", "region": "北美", "data_gb": 10, "validity_days": 30,
     "price": 26.0, "cost": 13.2, "cps_rate": 0.16, "desc": "长期驻美首选"},
    {"title": "全球 120 国 1GB / 7天", "region": "全球", "data_gb": 1, "validity_days": 7,
     "price": 8.9, "cost": 4.4, "cps_rate": 0.22, "desc": "多国跳飞，一卡通用"},
    {"title": "全球 120 国 5GB / 30天", "region": "全球", "data_gb": 5, "validity_days": 30,
     "price": 29.9, "cost": 15.5, "cps_rate": 0.22, "desc": "商旅长线，覆盖最广"},
    {"title": "日本 无限流量 / 8天", "region": "日本", "data_gb": 0, "validity_days": 8,
     "price": 12.9, "cost": 6.8, "cps_rate": 0.15, "desc": "不限量（公平使用降速），樱花季热销"},
    {"title": "中东 5 国 3GB / 15天", "region": "中东", "data_gb": 3, "validity_days": 15,
     "price": 13.5, "cost": 6.9, "cps_rate": 0.19, "desc": "阿联酋/沙特/卡塔尔等"},
]

AFFILIATES = [
    {"name": "平台管理员", "email": "admin@esim.demo", "password": "admin123",
     "phone": "13800000000", "role": "admin"},
    {"name": "数码博主小王", "email": "wang@demo.com", "password": "demo123",
     "phone": "13900000001", "sales": 158, "tier": "L3"},
    {"name": "留学生群主 Linda", "email": "linda@demo.com", "password": "demo123",
     "phone": "13900000002", "sales": 62, "tier": "L2"},
    {"name": "旅行社老陈", "email": "chen@demo.com", "password": "demo123",
     "phone": "13900000003", "sales": 12, "tier": "L1"},
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--simulate", action="store_true")
    a = ap.parse_args()

    if os.path.exists(server._path("affiliates")) and not a.force:
        print("[!] data 已存在，跳过灌入（要重灌请加 --force）")
        return
    if a.force:
        for k in server.FILES:
            p = server._path(k)
            if os.path.exists(p):
                os.remove(p)
        print("[+] 已清库")

    # 供应商
    sup = []
    for s in SUPPLIERS:
        sup.append({"id": "S%04d" % (len(sup) + 1), "name": s["name"],
                    "api_type": s["api_type"], "contact": s["contact"],
                    "created": server.now()})
    server.save("suppliers", sup)
    print("[+] 供应商 %d 家" % len(sup))

    # 卡品
    plans = []
    for i, p in enumerate(PLANS, 1):
        plans.append({"id": "P%04d" % i, "title": p["title"],
                      "supplier_id": sup[i % len(sup)]["id"], "region": p["region"],
                      "data_gb": p["data_gb"], "validity_days": p["validity_days"],
                      "price": p["price"], "cost": p["cost"], "model": "cps",
                      "cps_rate": p["cps_rate"], "cpa_amount": 0,
                      "desc": p["desc"], "active": True, "created": server.now()})
    server.save("plans", plans)
    print("[+] 卡品 %d 个" % len(plans))

    # 推广员（直接写入，绕过 register 以便预设等级与销量）
    affs = []
    for i, af in enumerate(AFFILIATES, 1):
        role = af.get("role", "affiliate")
        affs.append({
            "id": "A%04d" % i, "name": af["name"], "email": af["email"],
            "phone": af["phone"], "pwd": server.md5(af["password"]), "role": role,
            "status": "approved", "tier": af.get("tier", "L1"),
            "balance": 0.0, "total_commission": 0.0,
            "total_sales": af.get("sales", 0), "links": [], "created": server.now(),
        })
    server.save("affiliates", affs)
    print("[+] 推广员 %d 个（含 1 个管理员）" % len(affs))

    # 推广链接
    for a2 in affs:
        server.make_links(a2)
    print("[+] 每个推广员已生成 %d 条专属链接" % len(plans))

    if not a.simulate:
        print("\n完成。下一步：")
        print("  python server.py --port 8788")
        print("  浏览器打开 http://127.0.0.1:8788/  （管理员 admin@esim.demo / admin123）")
        print("  加 --simulate 可再灌一轮模拟订单（含被风控拦截的样本）")
        return

    # ---------------- 模拟流量 ----------------
    random.seed(20261016)
    orders, clicks = [], []
    admin = affs[0]
    workers = affs[1:]

    # 1) 正常订单：每位推广员若干笔
    for af in workers:
        for i in range(8):
            plan = random.choice(plans)
            code = af["links"][i % len(af["links"])]["code"]
            cid = "C" + str(random.randint(100000, 999999))
            clicks.append({"id": cid, "code": code, "ts": server.now(),
                           "t": server.ts() - random.randint(3600, 86400),
                           "ip": "203.0.%d.%d" % (random.randint(0, 250), random.randint(1, 250)),
                           "fp": "fp" + str(random.randint(1000, 9999)),
                           "ua": "Mozilla/5.0", "affiliate_id": af["id"]})
            month_orders = len([o for o in orders if o.get("affiliate_id") == af["id"]])
            commission, detail = server.calc_commission(plan, af, month_orders)
            orders.append({"id": "O" + str(random.randint(10000000, 99999999)),
                           "ts": server.now(), "affiliate_id": af["id"],
                           "affiliate_name": af["name"], "plan_id": plan["id"],
                           "plan_title": plan["title"], "amount": plan["price"],
                           "commission": commission, "commission_detail": detail,
                           "contact": "buyer%d@example.com" % random.randint(1, 999),
                           "ip": "203.0.%d.%d" % (random.randint(0, 250), random.randint(1, 250)),
                           "fp": "fp" + str(random.randint(1000, 9999)),
                           "risk_score": 0, "risk_reasons": [], "risk_action": "allow",
                           "status": "pending"})
    # 2) 自推自买（应被拦截）
    af = workers[0]
    plan = plans[0]
    score, reasons, action = server.risk_check(
        af, {"contact": af["email"]}, "198.51.100.7", "fp_self", None)
    orders.append({"id": "O" + str(random.randint(10000000, 99999999)), "ts": server.now(),
                   "affiliate_id": af["id"], "affiliate_name": af["name"],
                   "plan_id": plan["id"], "plan_title": plan["title"],
                   "amount": plan["price"],
                   "commission": server.calc_commission(plan, af, 0)[0],
                   "commission_detail": {}, "contact": af["email"],
                   "ip": "198.51.100.7", "fp": "fp_self",
                   "risk_score": score, "risk_reasons": reasons, "risk_action": action,
                   "status": "rejected" if action == "reject" else "review"})
    server.log_risk("order", "; ".join(reasons), score, action, af["id"])
    print("[+] 注入自推自买样本：风控分 %d，处置=%s，原因=%s" % (score, action, reasons))

    # 3) 高频刷量（同 IP 大量点击后下单）
    for i in range(35):
        clicks.append({"id": "C" + str(random.randint(100000, 999999)),
                       "code": workers[1]["links"][0]["code"], "ts": server.now(),
                       "t": server.ts() - random.randint(60, 3600),
                       "ip": "198.51.100.99", "fp": "fp_spam",
                       "ua": "python-requests/2.31", "affiliate_id": workers[1]["id"]})
    server.save("clicks", clicks)
    score, reasons, action = server.risk_check(
        workers[1], {"contact": "spam@example.com"}, "198.51.100.99", "fp_spam", None)
    orders.append({"id": "O" + str(random.randint(10000000, 99999999)), "ts": server.now(),
                   "affiliate_id": workers[1]["id"], "affiliate_name": workers[1]["name"],
                   "plan_id": plan["id"], "plan_title": plan["title"],
                   "amount": plan["price"],
                   "commission": server.calc_commission(plan, workers[1], 0)[0],
                   "commission_detail": {}, "contact": "spam@example.com",
                   "ip": "198.51.100.99", "fp": "fp_spam",
                   "risk_score": score, "risk_reasons": reasons, "risk_action": action,
                   "status": "rejected" if action == "reject" else "review"})
    server.log_risk("order", "; ".join(reasons), score, action, workers[1]["id"])
    print("[+] 注入高频刷量样本：风控分 %d，处置=%s，原因=%s" % (score, action, reasons))

    server.save("orders", orders)
    server.save("clicks", clicks)

    # 4) 提现申请
    w = []
    for af, amt in ((workers[0], 500.0), (workers[1], 200.0)):
        cfg = server.load("config")
        fee = server.money(amt * cfg["withdraw_fee_rate"])
        w.append({"id": "W" + str(random.randint(10000000, 99999999)), "ts": server.now(),
                  "affiliate_id": af["id"], "affiliate_name": af["name"], "amount": amt,
                  "fee": fee, "actual": server.money(amt - fee),
                  "channel": "支付宝", "account": af["email"], "status": "pending"})
    server.save("withdrawals", w)
    print("[+] 提现申请 %d 笔" % len(w))
    print("\n模拟数据完成。运行：python server.py --port 8788")


if __name__ == "__main__":
    main()
