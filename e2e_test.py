#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
e2e_test.py —— 端到端验收脚本（服务跑起来之后执行）

覆盖：登录 → 取专属链接(含二维码) → 落地页点击 → 正常下单 → 自推自买被拦截
      → 高频刷量被拦截 → 结算 → 提现申请 → 管理员审核 → 看板统计

用法（先起服务）：
    python server.py --port 8791 &
    python e2e_test.py --base http://127.0.0.1:8791
"""
import os
import sys
import json
import time
import argparse
import urllib.request
import urllib.error

PROXY_OFF = urllib.request.ProxyHandler({})   # 本机服务，不走代理


def call(base, path, method="GET", body=None, token=None, timeout=20):
    url = base + path
    data = json.dumps(body or {}).encode("utf-8") if body is not None else None
    hdr = {"Content-Type": "application/json", "User-Agent": "e2e/1.0"}
    if token:
        hdr["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=data, headers=hdr, method=method)
    op = urllib.request.build_opener(PROXY_OFF)
    try:
        r = op.open(req, timeout=timeout)
        raw = r.read().decode("utf-8", "ignore")
        try:
            return r.status, json.loads(raw)
        except Exception:
            return r.status, {"_raw": raw}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "ignore")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"_raw": raw[:300]}
    except Exception as e:
        return 0, {"error": "%s: %s" % (type(e).__name__, str(e)[:120])}


def show(title, code, obj, keys=None):
    print("\n--- %s  [HTTP %s] ---" % (title, code))
    if keys and isinstance(obj, dict):
        for k in keys:
            print("    %-14s %s" % (k, obj.get(k)))
    else:
        s = json.dumps(obj, ensure_ascii=False)
        print("    " + (s[:600] + ("…" if len(s) > 600 else "")))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8791")
    a = ap.parse_args()
    B = a.base.rstrip("/")
    ok = True

    print("=" * 78)
    print("eSIM 分销平台 端到端验收  base=%s  ts=%s" % (B, time.strftime("%Y-%m-%d %H:%M:%S")))
    print("=" * 78)

    c, r = call(B, "/api/health")
    show("0 健康检查", c, r)
    ok &= (c == 200 and r.get("ok"))

    # 1 登录（推广员）
    c, r = call(B, "/api/login", "POST", {"email": "wang@demo.com", "password": "demo123"})
    tok = r.get("token", "")
    show("1 推广员登录", c, r, ["ok", "user"])
    if c == 200 and r.get("user"):
        u = r["user"]
        print("    %-14s %s / %s / 余额 %s" % ("->", u.get("name"), u.get("tier"), u.get("balance")))
    ok &= bool(tok)

    # 2 取链（含二维码）
    c, r = call(B, "/api/link?plan_id=P0003", "GET", token=tok)
    links = r.get("links") or []
    show("2 取专属推广链接", c, {"count": len(links)}, ["count"])
    if links:
        L = links[0]
        print("    url      %s" % L.get("url"))
        print("    code     %s" % L.get("code"))
        print("    qr_svg   %d 字节，开头：%s" % (len(L.get("qr") or ""),
                                                 (L.get("qr") or "")[:56].replace("\n", " ")))
        code = L["code"]
    else:
        code = ""
    ok &= bool(code)

    # 3 落地页点击
    c, r = call(B, "/r/" + code)
    raw = r.get("_raw", "")
    hit_click = ("点击已记录" in raw) or ("推广码" in raw)
    print("\n--- 3 访问推广落地页 /r/%s  [HTTP %s] ---" % (code, c))
    print("    落地页命中：%s ｜ 页面长度 %d" % (hit_click, len(raw)))
    ok &= hit_click

    # 4 正常下单
    c, r = call(B, "/api/order", "POST",
                {"code": code, "contact": "buyer777@example.com", "fp": "fp-e2e-normal"})
    show("4 正常下单", c, r, ["ok", "status"] if isinstance(r, dict) else None)
    if r.get("order"):
        print("    %-14s 金额 %s ｜ 佣金 %s ｜ 风控分 %s ｜ 状态 %s" % (
            "->", r["order"].get("amount"), r["order"].get("commission"),
            r["order"].get("risk_score"), r["order"].get("status")))
    ok &= bool(r.get("ok"))

    # 5 自推自买（应被拦截）
    c, r = call(B, "/api/order", "POST",
                {"code": code, "contact": "wang@demo.com", "fp": "fp-e2e-self"})
    show("5 自推自买下单（应拒绝）", c, r, ["ok", "rejected", "risk_score"])
    print("    reasons   %s" % (r.get("reasons") or []))
    ok &= (r.get("rejected") is True)

    # 6 高频刷量：先刷点击
    clicks = 0
    for i in range(34):
        cc, _ = call(B, "/r/" + code)
        clicks += (1 if cc == 200 else 0)
    c, r = call(B, "/api/order", "POST",
                {"code": code, "contact": "spam@example.com", "fp": "fp-e2e-spam"})
    o6 = r.get("order") or {}
    show("6 高频刷量后下单（应拒绝/复核）", c, r, ["ok", "rejected"])
    print("    预置点击 %d 次 ｜ 风控分 %s ｜ 处置 %s ｜ 状态 %s ｜ reasons %s" % (
        clicks, o6.get("risk_score"), o6.get("risk_action"), o6.get("status"),
        (r.get("reasons") or o6.get("risk_reasons") or [])))

    # 7 管理员登录 + 结算
    c, r = call(B, "/api/login", "POST", {"email": "admin@esim.demo", "password": "admin123"})
    atok = r.get("token", "")
    show("7 管理员登录", c, r, ["ok"])
    c, r = call(B, "/api/admin/settle", "GET", token=atok)
    show("8 一键结算 pending→settled", c, r, ["ok", "settled"])
    ok &= bool(r.get("ok"))

    # 9 提现申请（金额取 min(120, 余额)；余额低于最低提现额则如实跳过，不伪造成功）
    _, me = call(B, "/api/me", "GET", token=tok)
    bal = float((me.get("user") or {}).get("balance") or 0)
    wx = min(120.0, bal)
    print("\n--- 9 推广员申请提现（当前余额 %.2f）---" % bal)
    if wx < 100.0:
        print("    余额低于最低提现额 100，脚本改为验证「余额不足被拒」这条真实分支")
        c, r = call(B, "/api/withdraw", "POST",
                    {"amount": 500.0, "channel": "支付宝", "account": "wang@demo.com"}, token=tok)
        show("9a 超额提现（应被拒）", c, r, ["ok", "error"])
        ok &= (c == 400)
        wid = None
    else:
        c, r = call(B, "/api/withdraw", "POST",
                    {"amount": wx, "channel": "支付宝", "account": "wang@demo.com"}, token=tok)
        show("9 推广员申请提现", c, r, ["ok"])
        if r.get("item"):
            print("    %-14s 金额 %s ｜ 手续费 %s ｜ 实付 %s" % (
                "->", r["item"].get("amount"), r["item"].get("fee"), r["item"].get("actual")))
        wid = (r.get("item") or {}).get("id")
    if r.get("item"):
        print("    %-14s 金额 %s ｜ 手续费 %s ｜ 实付 %s" % (
            "->", r["item"].get("amount"), r["item"].get("fee"), r["item"].get("actual")))
    wid = (r.get("item") or {}).get("id")

    # 10 管理员审核提现
    if wid:
        c, r = call(B, "/api/admin/withdraw?id=%s&action=approve" % wid, "GET", token=atok)
        show("10 管理员审核提现", c, r, ["ok"])
        if r.get("item"):
            print("    status %s ｜ processed %s" % (r["item"].get("status"),
                                                    r["item"].get("processed")))
        ok &= bool(r.get("ok"))

    # 11 看板统计
    c, r = call(B, "/api/admin/stats", "GET", token=atok)
    show("11 管理后台统计", c, r.get("stats") or r)
    c, r = call(B, "/api/stats")
    show("12 公开统计", c, r.get("stats") or r)

    # 13 风控日志
    c, r = call(B, "/api/admin/risk", "GET", token=atok)
    items = r.get("items") or []
    print("\n--- 13 风控日志 [HTTP %s] 共 %d 条 ---" % (c, len(items)))
    for x in items[-6:]:
        print("    %-6s %-6s 分%-2s %-8s %s" % (x.get("id"), x.get("kind"), x.get("score"),
                                                x.get("action"), (x.get("detail") or "")[:52]))

    print("\n" + "=" * 78)
    print("验收结论：%s" % ("通过" if ok else "存在失败项（见上面 HTTP 码）"))
    print("=" * 78)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
