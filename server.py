#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
server.py —— 第三方电话卡 / eSIM 推广分销平台（后端 + 前端，纯标准库 http.server）

为什么不用 FastAPI：要求"能真跑起来"，本机没有 fastapi/uvicorn 之外的依赖，
用标准库起服务可以保证 `python server.py` 一条命令就能跑，零安装。
（uvicorn 在本机是有的，但 stdlib 版本对部署环境更友好。）

功能
  供应商管理   /api/suppliers
  卡品管理     /api/plans            （CPS 比例 / CPA 固定 / 阶梯分佣）
  推广员       /api/register /login /me /upgrade（L1-L3 分级，等级决定分佣倍率）
  推广链接     /api/link?plan_id=     -> 短链 + 二维码(SVG)
  落地跳转     /r/<code>              -> 记点击 + 落地页 + 下单
  订单与佣金   /api/order /api/commissions
  提现         /api/withdraw          /api/admin/withdraw?id=&action=
  风控         /api/admin/risk        同 IP / 设备指纹 / 自推自买 / 高频点击
  看板         /api/stats /api/admin/stats

前端
  /       推广员端：选品 → 取链（含二维码）→ 看收益 → 提现
  /admin  管理后台：审核推广员 / 结算佣金 / 配置分佣规则 / 风控拦截记录

数据落盘：./data/*.json（可删库重来：rm -rf data && python seed.py）

启动：
    python server.py --port 8788
    curl http://127.0.0.1:8788/api/stats
"""
import os
import re
import io
import json
import time
import uuid
import random
import hashlib
import argparse
import datetime
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
BASE_URL = os.environ.get("SIM_BASE_URL", "http://127.0.0.1:8788")
LOCK = threading.RLock()

for d in (DATA,):
    os.makedirs(d, exist_ok=True)

# ============================================================================
# 存储：每个集合一个 JSON 文件
# ============================================================================
FILES = {
    "suppliers": "suppliers.json",
    "plans": "plans.json",
    "affiliates": "affiliates.json",
    "orders": "orders.json",
    "clicks": "clicks.json",
    "withdrawals": "withdrawals.json",
    "sessions": "sessions.json",
    "risk": "risk.json",
    "config": "config.json",
}

DEFAULT_CONFIG = {
    "site_name": "eSIM 分销平台",
    # 分佣模型：cps=按销售额比例，cpa=按激活固定额
    "commission_model": "cps",
    # 等级 -> 分佣倍率（作用在卡品基础佣金率上）
    "tiers": {"L1": {"mult": 1.0, "min_sales": 0, "name": "青铜"},
              "L2": {"mult": 1.25, "min_sales": 30, "name": "白银"},
              "L3": {"mult": 1.6, "min_sales": 120, "name": "黄金"}},
    # 阶梯分佣：月销量达到阈值，整单额外加成
    "volume_bonus": [{"orders": 50, "bonus_rate": 0.02},
                     {"orders": 200, "bonus_rate": 0.04}],
    "withdraw_min": 100.0,
    "withdraw_fee_rate": 0.01,
    # 风控阈值
    "risk": {"same_ip_click_max": 30,      # 同 IP 24h 点击上限
             "same_fp_click_max": 20,      # 同设备指纹 24h 点击上限
             "order_min_seconds_after_click": 3,   # 点击后 N 秒内下单判可疑
             "block_self_buy": True,       # 自推自买拦截
             "auto_reject_score": 3,       # 风控分 >= 该值自动拒单
             "ip_blacklist": [],           # IP 黑名单（后台可加）
             "ua_block_keywords": ["bot", "spider", "crawl", "scrapy",
                                   "python-requests", "curl", "wget", "httpclient"],
             "min_orders_for_cvr": 8,      # 订单数达到该值才检测转化率
             "cvr_max": 0.25,              # 推广员转化率上限（25%，正常 1-8%）
             "same_day_order_max": 3},     # 单个推广员 24h 成交上限
}


def _path(k):
    return os.path.join(DATA, FILES[k])


def load(k):
    p = _path(k)
    if not os.path.exists(p):
        if k == "config":
            save(k, DEFAULT_CONFIG)
            return dict(DEFAULT_CONFIG)
        return []
    with LOCK:
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return [] if k != "config" else dict(DEFAULT_CONFIG)


def save(k, v):
    with LOCK:
        tmp = _path(k) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(v, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _path(k))


def next_id(k, prefix):
    rows = load(k)
    n = len(rows) + 1
    return "%s%04d" % (prefix, n)


# ============================================================================
# 工具
# ============================================================================
def now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def ts():
    return int(time.time())


def md5(s):
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def money(x):
    return round(float(x or 0), 2)


def new_code(n=8):
    alphabet = "abcdefghijkmnpqrstuvwxyz23456789"
    return "".join(random.choice(alphabet) for _ in range(n))


def qr_svg(text):
    """生成二维码 SVG。qrcode 缺失时降级为占位 SVG（不崩）。"""
    try:
        import qrcode
        import qrcode.image.svg as svgmod
        img = qrcode.make(text, image_factory=svgmod.SvgPathImage)
        buf = io.BytesIO()
        img.save(buf)
        return buf.getvalue().decode("utf-8")
    except Exception:
        return ('<svg xmlns="http://www.w3.org/2000/svg" width="160" height="160">'
                '<rect width="160" height="160" fill="#f3f4f6"/>'
                '<text x="80" y="76" font-size="11" text-anchor="middle" fill="#6b7280">'
                'pip install qrcode</text>'
                '<text x="80" y="94" font-size="10" text-anchor="middle" fill="#9ca3af">'
                '%s</text></svg>' % text[:28])


# ============================================================================
# 佣金引擎：CPS / CPA / 等级倍率 / 阶梯加成
# ============================================================================
def calc_commission(plan, affiliate, month_order_count):
    cfg = load("config")
    model = plan.get("model") or cfg.get("commission_model", "cps")
    base_rate = float(plan.get("cps_rate", 0))       # 卡品基础佣金率
    cpa = float(plan.get("cpa_amount", 0))           # CPA 固定额
    price = float(plan.get("price", 0))
    tier = affiliate.get("tier", "L1")
    mult = float((cfg.get("tiers") or {}).get(tier, {}).get("mult", 1.0))
    if model == "cpa":
        amount = cpa * mult
        detail = {"model": "CPA", "base": money(cpa), "tier": tier, "mult": mult}
    else:
        amount = price * base_rate * mult
        detail = {"model": "CPS", "base_rate": base_rate, "price": price,
                  "tier": tier, "mult": mult}
    bonus = 0.0
    for b in (cfg.get("volume_bonus") or []):
        if month_order_count >= b["orders"]:
            bonus = float(b["bonus_rate"])
    if bonus:
        amount += price * bonus
        detail["volume_bonus"] = bonus
        detail["month_orders"] = month_order_count
    return money(amount), detail


def tier_of(affiliate):
    cfg = load("config")
    sales = affiliate.get("total_sales", 0)
    cur = "L1"
    for t, v in sorted((cfg.get("tiers") or {}).items(), key=lambda x: x[1]["min_sales"]):
        if sales >= v["min_sales"]:
            cur = t
    return cur


# ============================================================================
# 风控：同 IP / 设备指纹 / 自推自买 / 高频点击
# ============================================================================
def log_risk(kind, detail, score, action, affiliate_id=None):
    rows = load("risk")
    rows.append({"id": next_id("risk", "R"), "ts": now(), "kind": kind,
                 "detail": detail, "score": score, "action": action,
                 "affiliate_id": affiliate_id})
    save("risk", rows[-2000:])


def risk_check(affiliate, order_payload, ip, fp, click_id=None, ua=""):
    """返回 (score, reasons, action)。action: allow / review / reject

    规则分四组，按分定动作：>=auto_reject_score 拒绝，>=1 人工复核，否则放行。
    1) 身份类：自推自买、IP 黑名单、UA 爬虫特征
    2) 频次类：同 IP/指纹 24h 点击、同 IP 历史反复下单、点击后秒下单
    3) 行为类：单个推广员转化率畸高（>=8 单且 >25%）、24h 内集中下单 >=3 笔
    4) 点击-下单链路：click_id 对不上 / 超时未转化（>72h）不计转化但也不加分
    """
    cfg = (load("config") or {}).get("risk") or DEFAULT_CONFIG["risk"]
    score, reasons = 0, []
    # ---- 1) 身份类 ----
    if cfg.get("block_self_buy", True):
        contact = (order_payload.get("contact") or "").strip().lower()
        if contact and (contact == (affiliate.get("email") or "").lower()
                        or contact == (affiliate.get("phone") or "").lower()):
            score += 3
            reasons.append("自推自买：下单联系方式与推广员本人一致")
    bl = [x.strip() for x in (cfg.get("ip_blacklist") or []) if x.strip()]
    if ip and ip in bl:
        score += 3
        reasons.append("IP 在黑名单中：%s" % ip)
    ua_l = (ua or "").lower()
    if not ua_l:
        score += 2
        reasons.append("空 User-Agent（正常浏览器必带）")
    else:
        for kw in cfg.get("ua_block_keywords") or ["bot", "spider", "crawl", "scrapy",
                                                   "python-requests", "curl", "wget", "httpclient"]:
            if kw in ua_l:
                score += 2
                reasons.append("User-Agent 含爬虫特征：%s" % kw)
                break
    # ---- 2) 频次类 ----
    clicks = load("clicks")
    day = ts() - 86400
    def _c(rows):
        n = 0
        for c in rows:
            try:
                t = int(c.get("t") or 0)
            except Exception:
                t = 0
            if t >= day:
                n += 1
        return n
    ip_hits = _c([c for c in clicks if c.get("ip") == ip])
    fp_hits = _c([c for c in clicks if c.get("fp") == fp and fp])
    if ip_hits > cfg.get("same_ip_click_max", 30):
        score += 2
        reasons.append("同 IP 24h 点击 %d 次（阈值 %d）" % (ip_hits, cfg["same_ip_click_max"]))
    if fp and fp_hits > cfg.get("same_fp_click_max", 20):
        score += 2
        reasons.append("同设备指纹 24h 点击 %d 次（阈值 %d）" % (fp_hits, cfg["same_fp_click_max"]))
    orders = load("orders")
    same_ip_orders = len([o for o in orders if o.get("ip") == ip])
    if same_ip_orders >= 5:
        score += 1
        reasons.append("该 IP 历史下单 %d 笔" % same_ip_orders)
    # ---- 3) 行为类：推广员维度 ----
    aff_orders = [o for o in orders if o.get("affiliate_id") == affiliate["id"]
                  and o.get("status") != "rejected"]
    aff_clicks = [c for c in clicks if c.get("affiliate_id") == affiliate["id"]]
    if len(aff_orders) >= cfg.get("min_orders_for_cvr", 8):
        cvr = len(aff_orders) / float(max(len(aff_clicks), 1))
        if cvr > cfg.get("cvr_max", 0.25):
            score += 2
            reasons.append("推广员转化率 %.0f%% 畸高（%d 单 / %d 点击，阈值 %.0f%%）" % (
                cvr * 100, len(aff_orders), len(aff_clicks), cfg["cvr_max"] * 100))
    day_orders = [o for o in orders
                  if o.get("affiliate_id") == affiliate["id"]
                  and o.get("status") != "rejected"
                  and (o.get("t") or 0) >= day]
    if len(day_orders) >= cfg.get("same_day_order_max", 3):
        score += 1
        reasons.append("该推广员 24h 内成交 %d 笔（阈值 %d）" % (
            len(day_orders), cfg["same_day_order_max"]))
    # ---- 4) 点击后秒下单 ----
    if click_id:
        cl = [c for c in clicks if c.get("id") == click_id]
        if cl:
            try:
                delta = ts() - int(cl[0].get("t") or ts())
            except Exception:
                delta = 999
            if delta < cfg.get("order_min_seconds_after_click", 3):
                score += 1
                reasons.append("点击后 %d 秒即下单（疑似脚本）" % delta)
    action = "reject" if score >= cfg.get("auto_reject_score", 3) else (
        "review" if score >= 1 else "allow")
    return score, reasons, action


# ============================================================================
# 鉴权
# ============================================================================
def make_session(user):
    s = load("sessions")
    tok = uuid.uuid4().hex
    s.append({"token": tok, "id": user["id"], "role": user.get("role", "affiliate"),
              "t": ts()})
    save("sessions", s)
    return tok


def user_by_token(tok):
    if not tok:
        return None
    for s in load("sessions"):
        if s["token"] == tok:
            if ts() - int(s.get("t", 0)) > 86400 * 7:
                continue
            for a in load("affiliates"):
                if a["id"] == s["id"]:
                    return a
    return None


def public_user(a):
    return {"id": a["id"], "name": a.get("name"), "email": a.get("email"),
            "phone": a.get("phone"), "tier": a.get("tier"),
            "role": a.get("role"), "status": a.get("status"),
            "balance": money(a.get("balance", 0)),
            "total_sales": a.get("total_sales", 0),
            "total_commission": money(a.get("total_commission", 0)),
            "created": a.get("created")}


# ============================================================================
# HTTP
# ============================================================================
class Handler(BaseHTTPRequestHandler):
    server_version = "simaffiliate/1.0"

    # ---------- helpers ----------
    def _q(self):
        u = urllib.parse.urlparse(self.path)
        return urllib.parse.parse_qs(u.query)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            try:
                return {k: v[0] for k, v in urllib.parse.parse_qs(raw.decode()).items()}
            except Exception:
                return {}

    def _send(self, code, obj=None, ctype="application/json; charset=utf-8"):
        if obj is None:
            obj = {}
        if isinstance(obj, (dict, list)):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        elif isinstance(obj, bytes):
            body = obj
        else:
            body = str(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type,Authorization")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,PUT,OPTIONS")
        self.end_headers()
        self.wfile.write(body)

    def _html(self, s, code=200):
        self._send(code, s.encode("utf-8"), "text/html; charset=utf-8")

    def _ip(self):
        return self.headers.get("X-Forwarded-For") or self.client_address[0]

    def _auth(self):
        h = self.headers.get("Authorization") or ""
        tok = h.replace("Bearer", "").strip()
        return user_by_token(tok)

    def log_message(self, fmt, *a):
        pass  # 静音，避免污染 curl 输出

    def do_OPTIONS(self):
        self._send(204, {})

    # ---------- GET ----------
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        p, q = u.path, self._q()

        if p in ("/", "/index.html"):
            return self._html(PAGE_AFFILIATE)
        if p in ("/admin", "/admin/"):
            return self._html(PAGE_ADMIN)
        if p.startswith("/r/"):
            return self._redirect(p[3:], q)
        if p == "/api/health":
            return self._send(200, {"ok": True, "service": "sim-affiliate",
                                    "ts": now(), "base_url": BASE_URL})
        if p == "/api/plans":
            plans = [x for x in load("plans") if x.get("active", True)]
            return self._send(200, {"ok": True, "plans": plans, "count": len(plans)})
        if p == "/api/suppliers":
            return self._send(200, {"ok": True, "suppliers": load("suppliers")})
        if p == "/api/config":
            return self._send(200, {"ok": True, "config": load("config")})
        if p == "/api/qr":
            data = (q.get("data") or [""])[0]
            return self._send(200, qr_svg(data), "image/svg+xml")
        if p == "/api/stats":
            return self._send(200, {"ok": True, "stats": public_stats()})
        if p == "/api/me":
            a = self._auth()
            if not a:
                return self._send(401, {"ok": False, "error": "未登录"})
            return self._send(200, {"ok": True, "user": public_user(a)})
        if p == "/api/link":
            a = self._auth()
            if not a:
                return self._send(401, {"ok": False, "error": "未登录"})
            pid = (q.get("plan_id") or [""])[0]
            return self._send(200, {"ok": True, "links": make_links(a, pid)})
        if p == "/api/commissions":
            a = self._auth()
            if not a:
                return self._send(401, {"ok": False, "error": "未登录"})
            orders = [o for o in load("orders") if o.get("affiliate_id") == a["id"]]
            return self._send(200, {"ok": True, "user": public_user(a),
                                    "orders": orders[-50:],
                                    "summary": {"count": len(orders),
                                                "pending": money(sum(
                                                    o.get("commission", 0) for o in orders
                                                    if o.get("status") == "pending")),
                                                "settled": money(sum(
                                                    o.get("commission", 0) for o in orders
                                                    if o.get("status") == "settled"))}})
        if p == "/api/me-stats":
            a = self._auth()
            if not a:
                return self._send(401, {"ok": False, "error": "未登录"})
            return self._send(200, {"ok": True,
                                    "stats": affiliate_stats(a),
                                    "trend": affiliate_trend(a, 7)})
        if p == "/api/materials":
            a = self._auth()
            if not a:
                return self._send(401, {"ok": False, "error": "未登录"})
            return self._send(200, {"ok": True, "items": materials(a)})
        if p == "/api/withdrawals":
            a = self._auth()
            if not a:
                return self._send(401, {"ok": False, "error": "未登录"})
            w = [x for x in load("withdrawals") if x.get("affiliate_id") == a["id"]]
            return self._send(200, {"ok": True, "items": w})
        # 管理后台接口
        if p.startswith("/api/admin/"):
            a = self._auth()
            if not a or a.get("role") != "admin":
                return self._send(403, {"ok": False, "error": "需要管理员权限"})
            sub = p[len("/api/admin/"):]
            if sub == "stats":
                return self._send(200, {"ok": True, "stats": admin_stats()})
            if sub == "affiliates":
                return self._send(200, {"ok": True, "items":
                                        [public_user(x) for x in load("affiliates")]})
            if sub == "orders":
                return self._send(200, {"ok": True, "items": load("orders")[-200:]})
            if sub == "withdrawals":
                return self._send(200, {"ok": True, "items": load("withdrawals")})
            if sub == "risk":
                return self._send(200, {"ok": True, "items": load("risk")[-200:]})
            if sub == "clicks":
                return self._send(200, {"ok": True, "items": load("clicks")[-200:]})
            if sub == "withdraw":
                wid = (q.get("id") or [""])[0]
                action = (q.get("action") or ["approve"])[0]
                rows = load("withdrawals")
                hit = None
                for w in rows:
                    if w["id"] == wid:
                        hit = w
                        break
                if not hit:
                    return self._send(404, {"ok": False, "error": "提现单不存在"})
                if hit.get("status") != "pending":
                    return self._send(400, {"ok": False, "error": "该单已处理"})
                affs = load("affiliates")
                target = None
                for af in affs:
                    if af["id"] == hit["affiliate_id"]:
                        target = af
                        break
                if action == "approve":
                    hit["status"] = "approved"
                    hit["processed"] = now()
                    if target:
                        target["balance"] = money(target.get("balance", 0) - hit["amount"])
                else:
                    hit["status"] = "rejected"
                    hit["processed"] = now()
                save("withdrawals", rows)
                save("affiliates", affs)
                return self._send(200, {"ok": True, "item": hit})
            if sub == "affiliate":
                aid = (q.get("id") or [""])[0]
                action = (q.get("action") or ["approve"])[0]
                affs = load("affiliates")
                hit = None
                for af in affs:
                    if af["id"] == aid:
                        hit = af
                        break
                if not hit:
                    return self._send(404, {"ok": False, "error": "推广员不存在"})
                hit["status"] = "approved" if action == "approve" else "rejected"
                save("affiliates", affs)
                return self._send(200, {"ok": True, "item": public_user(hit)})
            if sub == "settle":
                # 结算：把所有 pending 且已过审核期的订单置为 settled
                orders = load("orders")
                n = 0
                for o in orders:
                    if o.get("status") == "pending" and o.get("risk_action") in (None, "allow"):
                        o["status"] = "settled"
                        o["settled_at"] = now()
                        n += 1
                save("orders", orders)
                affs = load("affiliates")
                for af in affs:
                    got = sum(o.get("commission", 0) for o in orders
                              if o.get("affiliate_id") == af["id"]
                              and o.get("status") == "settled")
                    paid = sum(w.get("amount", 0) for w in load("withdrawals")
                               if w.get("affiliate_id") == af["id"]
                               and w.get("status") == "approved")
                    af["total_commission"] = money(got)
                    af["balance"] = money(got - paid)
                    af["tier"] = tier_of(af)
                save("affiliates", affs)
                return self._send(200, {"ok": True, "settled": n})
            return self._send(404, {"ok": False, "error": "unknown admin api"})
        return self._send(404, {"ok": False, "error": "not found: " + p})

    # ---------- 推广落地页 /r/<code> ----------
    def _redirect(self, code, q):
        plans = load("plans")
        affs = load("affiliates")
        aff = None
        plan = None
        for a in affs:
            for l in (a.get("links") or []):
                if l.get("code") == code:
                    aff, plan = a, next((x for x in plans if x["id"] == l.get("plan_id")), None)
                    break
            if aff:
                break
        ip = self._ip()
        fp = (q.get("fp") or [""])[0]
        ua = self.headers.get("User-Agent", "")
        clicks = load("clicks")
        cid = "C" + uuid.uuid4().hex[:8]
        clicks.append({"id": cid, "code": code, "ts": now(), "t": ts(),
                       "ip": ip, "fp": fp, "ua": ua[:120],
                       "affiliate_id": aff["id"] if aff else None})
        save("clicks", clicks[-5000:])
        if not aff or not plan:
            return self._html("<h3>链接无效</h3><p>推广码 %s 不存在</p>" % code, 404)
        # 落地页：展示卡品 + 立即购买（下单打到 /api/order）
        # 用 replace 而不是 % 格式化：模板内嵌的 JS 里有裸 %（如 length%9999），
        # 用 %-format 会直接 TypeError 让整个请求 500。
        html = PAGE_LANDING
        for k, v in (("css", CSS), ("title", plan.get("title")),
                     ("price", plan.get("price")), ("data", plan.get("data_gb")),
                     ("days", plan.get("validity_days")), ("region", plan.get("region")),
                     ("code", code), ("cid", cid), ("plan_id", plan["id"]),
                     ("aff", aff.get("name")), ("desc", plan.get("desc") or "")):
            html = html.replace("@@%s@@" % k, str(v))
        return self._html(html)

    # ---------- POST ----------
    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        p = u.path
        b = self._body()

        if p == "/api/register":
            affs = load("affiliates")
            email = (b.get("email") or "").strip().lower()
            if not email or not b.get("password"):
                return self._send(400, {"ok": False, "error": "邮箱与密码必填"})
            if any(a.get("email") == email for a in affs):
                return self._send(409, {"ok": False, "error": "该邮箱已注册"})
            a = {"id": next_id("affiliates", "A"), "name": b.get("name") or email.split("@")[0],
                 "email": email, "phone": (b.get("phone") or "").strip(),
                 "pwd": md5(b.get("password")), "role": "affiliate",
                 "status": "pending" if len(affs) else "approved",
                 "tier": "L1", "balance": 0.0, "total_commission": 0.0,
                 "total_sales": 0, "links": [], "created": now()}
            if b.get("role") == "admin" or not len(affs):
                a["role"] = "admin"
                a["status"] = "approved"
            affs.append(a)
            save("affiliates", affs)
            return self._send(200, {"ok": True, "user": public_user(a),
                                    "token": make_session(a),
                                    "note": "首个注册账号自动成为管理员；其余需后台审核"})

        if p == "/api/login":
            email = (b.get("email") or "").strip().lower()
            for a in load("affiliates"):
                if a.get("email") == email and a.get("pwd") == md5(b.get("password") or ""):
                    if a.get("status") != "approved":
                        return self._send(403, {"ok": False, "error": "账号待审核"})
                    return self._send(200, {"ok": True, "user": public_user(a),
                                            "token": make_session(a)})
            return self._send(401, {"ok": False, "error": "邮箱或密码错误"})

        if p == "/api/plans":      # 管理员建卡品
            a = self._auth()
            if not a or a.get("role") != "admin":
                return self._send(403, {"ok": False, "error": "需要管理员权限"})
            plans = load("plans")
            pid = "P%04d" % (len(plans) + 1)
            plans.append({
                "id": pid, "title": b.get("title") or "未命名卡品",
                "supplier_id": b.get("supplier_id"), "region": b.get("region") or "全球",
                "data_gb": b.get("data_gb"), "validity_days": b.get("validity_days"),
                "price": float(b.get("price") or 0),
                "cost": float(b.get("cost") or 0),
                "model": b.get("model") or "cps",
                "cps_rate": float(b.get("cps_rate") or 0.15),
                "cpa_amount": float(b.get("cpa_amount") or 0),
                "desc": b.get("desc") or "", "active": True, "created": now(),
            })
            save("plans", plans)
            return self._send(200, {"ok": True, "plan": plans[-1]})

        if p == "/api/suppliers":
            a = self._auth()
            if not a or a.get("role") != "admin":
                return self._send(403, {"ok": False, "error": "需要管理员权限"})
            s = load("suppliers")
            s.append({"id": "S%04d" % (len(s) + 1), "name": b.get("name"),
                      "api_type": b.get("api_type") or "manual",
                      "contact": b.get("contact") or "", "created": now()})
            save("suppliers", s)
            return self._send(200, {"ok": True, "supplier": s[-1]})

        if p == "/api/link":
            a = self._auth()
            if not a:
                return self._send(401, {"ok": False, "error": "未登录"})
            return self._send(200, {"ok": True, "links": make_links(a, b.get("plan_id"))})

        if p == "/api/order":
            code = b.get("code") or ""
            plans = load("plans")
            affs = load("affiliates")
            aff = plan = None
            for a in affs:
                for l in (a.get("links") or []):
                    if l.get("code") == code:
                        aff, plan = a, next((x for x in plans if x["id"] == l.get("plan_id")), None)
            if not aff or not plan:
                return self._send(404, {"ok": False, "error": "推广码无效"})
            ip = self._ip()
            fp = b.get("fp") or ""
            score, reasons, action = risk_check(aff, b, ip, fp, b.get("click_id"),
                                                self.headers.get("User-Agent", ""))
            month_orders = len([o for o in load("orders")
                                if o.get("affiliate_id") == aff["id"]])
            commission, detail = calc_commission(plan, aff, month_orders)
            orders = load("orders")
            o = {"id": "O" + uuid.uuid4().hex[:8], "ts": now(), "t": ts(),
                 "affiliate_id": aff["id"], "affiliate_name": aff.get("name"),
                 "plan_id": plan["id"], "plan_title": plan.get("title"),
                 "amount": money(plan.get("price")), "commission": commission,
                 "commission_detail": detail, "contact": b.get("contact") or "",
                 "ip": ip, "fp": fp, "risk_score": score, "risk_reasons": reasons,
                 "risk_action": action,
                 "status": "rejected" if action == "reject" else (
                     "review" if action == "review" else "pending")}
            orders.append(o)
            save("orders", orders)
            log_risk("order", "; ".join(reasons) or "正常", score, action, aff["id"])
            if action == "reject":
                return self._send(200, {"ok": False, "rejected": True,
                                        "order": o, "risk_score": score,
                                        "reasons": reasons})
            aff["total_sales"] = aff.get("total_sales", 0) + 1
            save("affiliates", affs)
            return self._send(200, {"ok": True, "order": o,
                                    "note": "需人工复核" if action == "review" else "已入账待结算"})

        if p == "/api/withdraw":
            a = self._auth()
            if not a:
                return self._send(401, {"ok": False, "error": "未登录"})
            cfg = load("config")
            amount = float(b.get("amount") or 0)
            bal = money(a.get("balance", 0))
            if amount < float(cfg.get("withdraw_min", 100)):
                return self._send(400, {"ok": False,
                                        "error": "低于最低提现额 %s" % cfg["withdraw_min"]})
            if amount > bal:
                return self._send(400, {"ok": False, "error": "余额不足（当前 %s）" % bal})
            fee = money(amount * float(cfg.get("withdraw_fee_rate", 0.01)))
            w = load("withdrawals")
            item = {"id": "W" + uuid.uuid4().hex[:8], "ts": now(),
                    "affiliate_id": a["id"], "affiliate_name": a.get("name"),
                    "amount": amount, "fee": fee, "actual": money(amount - fee),
                    "channel": b.get("channel") or "支付宝",
                    "account": b.get("account") or "", "status": "pending"}
            w.append(item)
            save("withdrawals", w)
            return self._send(200, {"ok": True, "item": item})

        if p == "/api/admin/config":
            a = self._auth()
            if not a or a.get("role") != "admin":
                return self._send(403, {"ok": False, "error": "需要管理员权限"})
            cfg = load("config")
            cfg.update(b or {})
            save("config", cfg)
            return self._send(200, {"ok": True, "config": cfg})

        return self._send(404, {"ok": False, "error": "not found: " + p})


# ============================================================================
# 业务函数
# ============================================================================
def make_links(affiliate, plan_id=None):
    """生成专属推广链接（每个卡品一个短码）+ 二维码 SVG。"""
    plans = load("plans")
    if plan_id:
        plans = [p for p in plans if p["id"] == plan_id]
    affs = load("affiliates")
    # 传入的 affiliate 可能来自另一份 load 的列表（seed.py 就是这样），
    # 直接 append 到它身上再 save(affs) 会把新链接丢掉 —— 必须先按 id 回挂到
    # 磁盘列表里的同一条记录上再保存。
    target = None
    for x in affs:
        if x["id"] == affiliate["id"]:
            target = x
            break
    if target is None:
        target = affiliate
        affs.append(target)
    out, changed = [], False
    for p in plans:
        exist = None
        for l in (target.get("links") or []):
            if l.get("plan_id") == p["id"]:
                exist = l
                break
        if not exist:
            exist = {"code": new_code(), "plan_id": p["id"]}
            target.setdefault("links", []).append(exist)
            changed = True
        if changed is False and not any(
                l.get("plan_id") == p["id"] for l in (affiliate.get("links") or [])):
            target.setdefault("links", []).append(exist)
            changed = True
        url = "%s/r/%s" % (BASE_URL, exist["code"])
        out.append({"plan_id": p["id"], "title": p.get("title"), "code": exist["code"],
                    "url": url, "price": p.get("price"),
                    "cps_rate": p.get("cps_rate"), "cpa_amount": p.get("cpa_amount"),
                    "qr": qr_svg(url)})
    if changed:
        save("affiliates", affs)
    # 同步回传入对象，调用方（如 seed.py）后续读 affiliate["links"] 才是完整的
    affiliate["links"] = target.get("links", [])
    return out


def public_stats():
    plans = [p for p in load("plans") if p.get("active", True)]
    affs = load("affiliates")
    orders = load("orders")
    return {"plans": len(plans), "affiliates": len(affs),
            "orders": len(orders),
            "gmv": money(sum(o.get("amount", 0) for o in orders)),
            "commission_total": money(sum(o.get("commission", 0) for o in orders
                                          if o.get("status") != "rejected")),
            "clicks": len(load("clicks"))}


def admin_stats():
    orders = load("orders")
    clicks = load("clicks")
    affs = load("affiliates")
    by_status = {}
    for o in orders:
        by_status[o.get("status")] = by_status.get(o.get("status"), 0) + 1
    rejected = [o for o in orders if o.get("risk_action") == "reject"]
    return {"orders": len(orders), "by_status": by_status,
            "gmv": money(sum(o.get("amount", 0) for o in orders if o["status"] != "rejected")),
            "commission": money(sum(o.get("commission", 0) for o in orders
                                    if o["status"] != "rejected")),
            "clicks": len(clicks), "unique_ip": len({c.get("ip") for c in clicks}),
            "affiliates": len(affs),
            "pending_affiliates": len([a for a in affs if a.get("status") == "pending"]),
            "risk_rejected": len(rejected),
            "pending_withdraw": money(sum(w.get("amount", 0) for w in load("withdrawals")
                                          if w.get("status") == "pending"))}


def affiliate_stats(aff):
    """推广员自己的数据：点击/UV/订单/转化率/风控拦截数。"""
    clicks = [c for c in load("clicks") if c.get("affiliate_id") == aff["id"]]
    orders = [o for o in load("orders") if o.get("affiliate_id") == aff["id"]]
    valid = [o for o in orders if o.get("status") != "rejected"]
    uv = len({c.get("fp") for c in clicks if c.get("fp")}) or len({c.get("ip") for c in clicks})
    return {"clicks": len(clicks), "uv": uv,
            "orders": len(valid), "rejected": len(orders) - len(valid),
            "cvr": round(len(valid) / float(max(uv, 1)), 4),
            "today_clicks": len([c for c in clicks if (c.get("t") or 0) >= ts() - 86400]),
            "today_orders": len([o for o in valid if (o.get("t") or 0) >= ts() - 86400])}


def affiliate_trend(aff, days=7):
    """近 N 日点击/成交趋势，按本地日期分桶。"""
    clicks = [c for c in load("clicks") if c.get("affiliate_id") == aff["id"]]
    orders = [o for o in load("orders")
              if o.get("affiliate_id") == aff["id"] and o.get("status") != "rejected"]
    buckets = {}
    for i in range(days):
        d = (datetime.date.today() - datetime.timedelta(days=days - 1 - i)).isoformat()
        buckets[d] = {"date": d, "clicks": 0, "orders": 0}
    for c in clicks:
        try:
            t = int(c.get("t") or 0)
        except Exception:
            continue
        if not t:
            continue
        d = datetime.date.fromtimestamp(t).isoformat()
        if d in buckets:
            buckets[d]["clicks"] += 1
    for o in orders:
        try:
            t = int(o.get("t") or 0)
        except Exception:
            t = 0
        if not t:
            continue
        d = datetime.date.fromtimestamp(t).isoformat()
        if d in buckets:
            buckets[d]["orders"] += 1
    return list(buckets.values())


def materials(aff):
    """推广素材中心：每个卡品生成可直接复制的话术（朋友圈/社群/Twitter）。"""
    plans = [p for p in load("plans") if p.get("active", True)]
    links = {l.get("plan_id"): l for l in (aff.get("links") or [])}
    out = []
    for p in plans:
        l = links.get(p["id"])
        if not l:
            continue
        url = "%s/r/%s" % (BASE_URL, l["code"])
        out.append({
            "plan_id": p["id"], "title": p.get("title"), "url": url,
            "texts": [
                {"scene": "朋友圈",
                 "text": "出国旅游/留学必备！%s，%s %s，%d天有效，落地即用不用换卡。"
                         "我实测比机场买卡便宜一半，链接在这 👉 %s" % (
                             p.get("title"), p.get("data_gb"), "GB",
                             p.get("validity_days") or 0, url)},
                {"scene": "社群",
                 "text": "【实测推荐】%s（%s）\n· 区域：%s\n· 流量：%sGB\n· 有效期：%s 天\n"
                         "· 价格：%.2f\n需要直接点：%s" % (
                             p.get("title"), p.get("region"), p.get("region"),
                             p.get("data_gb"), p.get("validity_days"),
                             float(p.get("price") or 0), url)},
                {"scene": "Twitter/X",
                 "text": "Traveling? %s — %sGB, %s days, activate on arrival. "
                         "Cheaper than airport kiosks. %s" % (
                             p.get("title"), p.get("data_gb"),
                             p.get("validity_days"), url)},
            ]})
    return out


# ============================================================================
# 前端页面（内联，不引任何 CDN，离线可用）
# ============================================================================
CSS = """
*{box-sizing:border-box}body{margin:0;background:#0b1020;color:#e8ecf4;
font-family:-apple-system,"Microsoft YaHei",sans-serif;font-size:14px}
header{padding:14px 20px;border-bottom:1px solid #1e2740;display:flex;gap:14px;align-items:center}
header b{font-size:16px}.sp{flex:1}
.wrap{max-width:1180px;margin:0 auto;padding:16px}
.card{background:#121a2e;border:1px solid #1e2740;border-radius:10px;padding:14px 16px;margin-bottom:14px}
.card h3{margin:0 0 10px;font-size:14px;color:#6ea8fe}
table{width:100%;border-collapse:collapse;font-size:13px}
td,th{border-bottom:1px solid #1e2740;padding:6px 8px;text-align:left}
th{color:#8b96ad;font-weight:500}.n{text-align:right;font-variant-numeric:tabular-nums}
button,input,select{background:#0d1424;color:#e8ecf4;border:1px solid #26314d;
border-radius:6px;padding:7px 10px;font-size:13px}
button{background:#2563eb;border-color:#2563eb;cursor:pointer}
button.g{background:#0d1424;border:1px solid #26314d}
button.r{background:#b91c1c;border-color:#b91c1c}
.kpi{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:10px}
.kpi div{background:#0d1424;border:1px solid #1e2740;border-radius:8px;padding:8px 14px;min-width:110px}
.kpi b{display:block;font-size:19px}.kpi span{font-size:11px;color:#8b96ad}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:6px 0}
.qr{background:#fff;border-radius:8px;padding:8px;display:inline-block}
.qr svg{width:150px;height:150px;display:block}
.tag{display:inline-block;padding:2px 8px;border-radius:20px;font-size:11px;border:1px solid #26314d}
.t-ok{color:#4ade80;border-color:#14532d}.t-warn{color:#fbbf24;border-color:#713f12}
.t-bad{color:#f87171;border-color:#7f1d1d}.muted{color:#8b96ad;font-size:12px}
a{color:#6ea8fe}pre{background:#0d1424;padding:10px;border-radius:6px;overflow:auto;font-size:12px}
.chart{display:flex;gap:10px;align-items:flex-end;height:120px;padding-top:6px}
.chart .col{flex:1;display:flex;flex-direction:column;align-items:center;gap:4px;height:100%;justify-content:flex-end}
.chart .bar{width:100%;max-width:34px;background:#2563eb;border-radius:4px 4px 0 0;min-height:2px;position:relative}
.chart .bar.o{background:#4ade80}
.chart .lab{font-size:11px;color:#8b96ad}
.chart .val{font-size:10px;color:#8b96ad}
.legend{display:flex;gap:14px;font-size:12px;color:#8b96ad;margin-bottom:8px}
.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:5px}
.mat{border:1px solid #1e2740;border-radius:8px;padding:10px 12px;margin-bottom:10px;background:#0d1424}
.mat .scene{font-size:11px;color:#fbbf24;border:1px solid #713f12;border-radius:20px;padding:1px 8px;margin-right:8px}
.mat pre{margin:8px 0 0;white-space:pre-wrap;word-break:break-all}
#toast{position:fixed;left:50%;bottom:32px;transform:translateX(-50%);background:#14532d;color:#4ade80;
border:1px solid #14532d;border-radius:8px;padding:9px 18px;font-size:13px;display:none;z-index:99}
.two{display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media(max-width:760px){.two{grid-template-columns:1fr}}
"""

JS_AFF = """
const API='/api';let TOKEN=localStorage.getItem('sim_token')||'';
async function api(p,o={}){o.headers=Object.assign({'Content-Type':'application/json'},o.headers||{});
 if(TOKEN)o.headers.Authorization='Bearer '+TOKEN;
 const r=await fetch(API+p,o);return r.json();}
function el(id){return document.getElementById(id)}
function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function toast(m){const t=el('toast');if(!t)return;t.textContent=m;t.style.display='block';
 clearTimeout(t._h);t._h=setTimeout(()=>t.style.display='none',1800);}
function copy(t){if(navigator.clipboard&&navigator.clipboard.writeText){
 navigator.clipboard.writeText(t).then(()=>toast('已复制'));}else{
 const a=document.createElement('textarea');a.value=t;document.body.appendChild(a);
 a.select();document.execCommand('copy');a.remove();toast('已复制');}}
async function login(){const r=await api('/login',{method:'POST',body:JSON.stringify(
 {email:el('le').value,password:el('lp').value})});
 if(r.ok){TOKEN=r.token;localStorage.setItem('sim_token',TOKEN);boot();}else toast(r.error||'失败');}
async function reg(){const r=await api('/register',{method:'POST',body:JSON.stringify(
 {name:el('rn').value,email:el('re').value,password:el('rp').value,phone:el('rph').value})});
 if(r.ok){TOKEN=r.token;localStorage.setItem('sim_token',TOKEN);boot();toast('注册成功：'+(r.note||''));}
 else toast(r.error||'失败');}
function logout(){TOKEN='';localStorage.removeItem('sim_token');boot();}
function chart(trend){
 const mx=Math.max(1,...trend.map(d=>d.clicks),...trend.map(d=>d.orders));
 const cols=trend.map(d=>{const hc=Math.round(d.clicks/mx*100),ho=Math.round(d.orders/mx*100);
  return `<div class="col"><span class="val">${d.clicks}/${d.orders}</span>
   <div class="bar" style="height:${hc}%"></div><div class="bar o" style="height:${ho}%"></div>
   <span class="lab">${d.date.slice(5)}</span></div>`}).join('');
 return `<div class="legend"><span><i style="background:#2563eb"></i>点击</span>
  <span><i style="background:#4ade80"></i>成交</span><span class="sp"></span>
  <span>格式：点击/成交</span></div><div class="chart">${cols}</div>`;}
async function boot(){
 if(!TOKEN){el('app').innerHTML=`
  <div class="card"><h3>登录 / 注册</h3>
  <div class="row"><input id="le" placeholder="邮箱" value="admin@esim.demo">
   <input id="lp" placeholder="密码" type="password" value="admin123">
   <button onclick="login()">登录</button><button class="g" onclick="reg()">注册</button></div>
  <div class="row muted">演示管理员：admin@esim.demo / admin123（seed.py 灌入）</div>
  <div class="row"><input id="rn" placeholder="昵称"><input id="re" placeholder="新邮箱">
   <input id="rp" type="password" placeholder="新密码"><input id="rph" placeholder="手机号"></div></div>`;
  return;}
 const me=await api('/me');
 if(!me.ok){TOKEN='';localStorage.removeItem('sim_token');boot();return}
 const u=me.user;
 const plans=(await api('/plans')).plans||[];
 const links=(await api('/link')).links||[];
 const com=await api('/commissions');
 const ms=await api('/me-stats');
 const mats=await api('/materials');
 const wds=(await api('/withdrawals')).items||[];
 const S=ms.stats||{},T=ms.trend||[];
 el('app').innerHTML=`
 <div class="card"><h3>我的推广员信息</h3>
  <div class="kpi"><div><b>${u.tier}</b><span>等级</span></div>
  <div><b>${u.balance}</b><span>可提现余额</span></div>
  <div><b>${u.total_sales}</b><span>累计推广单数</span></div>
  <div><b>${u.total_commission}</b><span>累计佣金</span></div>
  <div><b>${S.today_clicks||0}</b><span>今日点击</span></div>
  <div><b>${S.uv||0}</b><span>独立访客 UV</span></div>
  <div><b>${(S.cvr*100||0).toFixed(1)}%</b><span>转化率</span></div></div>
  <div class="row muted">${u.name} · ${u.email} · ${u.status} · <button class="g" onclick="logout()">退出</button></div></div>
 <div class="card"><h3>数据报表（近 7 日）</h3>${chart(T)}</div>
 <div class="two">
 <div class="card"><h3>① 选品（可推广卡品）</h3><table>
  <tr><th>卡品</th><th>区域</th><th class="n">流量</th><th class="n">售价</th><th class="n">佣金率</th></tr>
  ${plans.map(p=>`<tr><td>${esc(p.title)}</td><td>${esc(p.region)}</td><td class="n">${p.data_gb||'-'}GB</td>
   <td class="n">${p.price}</td><td class="n">${(p.cps_rate*100).toFixed(0)}%</td></tr>`).join('')}
 </table></div>
 <div class="card"><h3>② 我的专属推广链接</h3>
  ${links.map(l=>`<div class="row" style="border-bottom:1px solid #1e2740;padding-bottom:10px">
   <div class="qr">${l.qr}</div>
   <div style="flex:1;min-width:240px"><b>${esc(l.title)}</b><br>
   <a href="${l.url}" target="_blank">${esc(l.url)}</a><br>
   <span class="muted">推广码 ${l.code} ｜ 售价 ${l.price} ｜ 佣金率 ${(l.cps_rate*100).toFixed(0)}%</span><br>
   <button class="g" onclick="copy('${l.url}')">复制链接</button></div></div>`).join('')}
 </div></div>
 <div class="card"><h3>③ 推广素材中心（一键复制话术）</h3>
  ${(mats.items||[]).map(m=>`<div class="mat"><b>${esc(m.title)}</b>
   ${m.texts.map(t=>`<div style="margin-top:10px"><span class="scene">${esc(t.scene)}</span>
    <button class="g" onclick="copy(\`${t.text.replace(/`/g,'\\`')}\`)">复制</button>
    <pre>${esc(t.text)}</pre></div>`).join('')}</div>`).join('')||'<div class="muted">暂无素材（先在上方生成推广链接）</div>'}
 </div>
 <div class="card"><h3>④ 我的收益</h3>
  <div class="kpi"><div><b>${com.summary?com.summary.count:0}</b><span>订单数</span></div>
  <div><b>${com.summary?com.summary.pending:0}</b><span>待结算</span></div>
  <div><b>${com.summary?com.summary.settled:0}</b><span>已结算</span></div>
  <div><b>${S.rejected||0}</b><span>风控拦截</span></div></div>
  <table><tr><th>时间</th><th>卡品</th><th class="n">金额</th><th class="n">佣金</th><th>状态</th><th>风控</th></tr>
  ${(com.orders||[]).slice().reverse().map(o=>`<tr><td>${o.ts.slice(5)}</td><td>${esc(o.plan_title)}</td>
   <td class="n">${o.amount}</td><td class="n">${o.commission}</td>
   <td><span class="tag ${o.status==='rejected'?'t-bad':(o.status==='pending'?'t-warn':'t-ok')}">${o.status}</span></td>
   <td class="muted">${esc((o.risk_reasons||[]).join('；')||'正常')}</td></tr>`).join('')}</table></div>
 <div class="card"><h3>⑤ 申请提现</h3>
  <div class="row"><input id="wa" placeholder="金额" style="width:120px">
   <input id="wc" placeholder="渠道" value="支付宝" style="width:120px">
   <input id="wacc" placeholder="收款账号"><button onclick="wd()">提交申请</button></div>
  <div class="muted">起提金额与手续费以平台配置为准；到账 = 金额 - 手续费。</div>
  <div class="muted" id="wmsg"></div>
  ${wds.length?`<table style="margin-top:10px"><tr><th>时间</th><th class="n">金额</th><th class="n">手续费</th>
   <th class="n">实付</th><th>渠道</th><th>状态</th></tr>
   ${wds.slice().reverse().map(w=>`<tr><td>${w.ts.slice(5)}</td><td class="n">${w.amount}</td>
   <td class="n">${w.fee}</td><td class="n">${w.actual}</td><td>${esc(w.channel)}</td>
   <td><span class="tag ${w.status==='approved'?'t-ok':(w.status==='rejected'?'t-bad':'t-warn')}">${w.status}</span></td>
   </tr>`).join('')}</table>`:'<div class="muted" style="margin-top:8px">暂无提现记录</div>'}</div>`;}
async function wd(){const r=await api('/withdraw',{method:'POST',body:JSON.stringify(
 {amount:parseFloat(el('wa').value),channel:el('wc').value,account:el('wacc').value})});
 if(r.ok){toast('提现申请已提交，实付 '+(r.item?r.item.actual:''));boot();}
 else el('wmsg').textContent=r.error||'失败';}
boot();
"""

JS_ADMIN = """
const API='/api';let TOKEN=localStorage.getItem('sim_token')||'';
async function api(p,o={}){o.headers=Object.assign({'Content-Type':'application/json'},o.headers||{});
 if(TOKEN)o.headers.Authorization='Bearer '+TOKEN;const r=await fetch(API+p,o);return r.json();}
function el(id){return document.getElementById(id)}
function toast(m){const t=el('toast');if(!t){alert(m);return;}t.textContent=m;t.style.display='block';
 clearTimeout(t._h);t._h=setTimeout(()=>t.style.display='none',1800);}
async function login(){const r=await api('/login',{method:'POST',body:JSON.stringify(
 {email:el('le').value,password:el('lp').value})});
 if(r.ok){TOKEN=r.token;localStorage.setItem('sim_token',TOKEN);boot();}else alert(r.error||'失败');}
async function boot(){
 if(!TOKEN){el('app').innerHTML=`<div class="card"><h3>管理员登录</h3>
  <div class="row"><input id="le" placeholder="邮箱" value="admin@esim.demo">
  <input id="lp" type="password" placeholder="密码" value="admin123">
  <button onclick="login()">登录</button></div></div>`;return;}
 const me=await api('/me');
 if(!me.ok||me.user.role!=='admin'){alert('需要管理员账号');return;}
 const s=(await api('/admin/stats')).stats;
 const af=(await api('/admin/affiliates')).items||[];
 const od=(await api('/admin/orders')).items||[];
 const wd=(await api('/admin/withdrawals')).items||[];
 const rk=(await api('/admin/risk')).items||[];
 const cfg=(await api('/config')).config||{};
 el('app').innerHTML=`
 <div class="card"><h3>平台总览</h3><div class="kpi">
  <div><b>${s.orders}</b><span>订单</span></div><div><b>${s.gmv}</b><span>GMV</span></div>
  <div><b>${s.commission}</b><span>佣金总额</span></div>
  <div><b>${s.clicks}</b><span>点击</span></div><div><b>${s.unique_ip}</b><span>独立IP</span></div>
  <div><b>${s.risk_rejected}</b><span>风控拒单</span></div>
  <div><b>${s.pending_withdraw}</b><span>待审提现</span></div></div>
  <div class="row"><button onclick="settle()">一键结算（pending→settled）</button>
  <span class="muted" id="smsg"></span></div></div>
 <div class="card"><h3>推广员（${af.length}）</h3><table>
  <tr><th>ID</th><th>名称</th><th>等级</th><th class="n">单数</th><th class="n">佣金</th><th class="n">余额</th><th>状态</th><th>操作</th></tr>
  ${af.map(a=>`<tr><td>${a.id}</td><td>${a.name}</td><td>${a.tier}</td><td class="n">${a.total_sales}</td>
   <td class="n">${a.total_commission}</td><td class="n">${a.balance}</td><td>${a.status}</td>
   <td>${a.status!=='approved'?`<button class="g" onclick="appr('${a.id}')">通过</button>`:''}</td></tr>`).join('')}
 </table></div>
 <div class="card"><h3>订单（${od.length}）</h3><table>
  <tr><th>时间</th><th>推广员</th><th>卡品</th><th class="n">金额</th><th class="n">佣金</th>
   <th>状态</th><th>风控分</th><th>原因</th></tr>
  ${od.slice().reverse().slice(0,60).map(o=>`<tr><td>${o.ts.slice(5)}</td><td>${o.affiliate_name}</td>
   <td>${o.plan_title}</td><td class="n">${o.amount}</td><td class="n">${o.commission}</td>
   <td><span class="tag ${o.status==='rejected'?'t-bad':(o.status==='review'?'t-warn':'t-ok')}">${o.status}</span></td>
   <td class="n">${o.risk_score||0}</td><td class="muted">${(o.risk_reasons||[]).join('；')||'-'}</td></tr>`).join('')}
 </table></div>
 <div class="card"><h3>提现审核（${wd.length}）</h3><table>
  <tr><th>时间</th><th>推广员</th><th class="n">金额</th><th class="n">手续费</th><th class="n">实付</th>
   <th>渠道</th><th>状态</th><th>操作</th></tr>
  ${wd.slice().reverse().map(w=>`<tr><td>${w.ts.slice(5)}</td><td>${w.affiliate_name}</td>
   <td class="n">${w.amount}</td><td class="n">${w.fee}</td><td class="n">${w.actual}</td>
   <td>${w.channel}</td><td>${w.status}</td>
   <td>${w.status==='pending'?`<button class="g" onclick="wdo('${w.id}','approve')">通过</button>
    <button class="r" onclick="wdo('${w.id}','reject')">驳回</button>`:''}</td></tr>`).join('')}

 </table></div>
 <div class="card"><h3>风控日志（${rk.length}）</h3><table>
  <tr><th>时间</th><th>类型</th><th>详情</th><th class="n">分</th><th>处置</th></tr>
  ${rk.slice().reverse().slice(0,40).map(r=>`<tr><td>${r.ts.slice(5)}</td><td>${r.kind}</td>
   <td>${r.detail||''}</td><td class="n">${r.score}</td>
   <td><span class="tag ${r.action==='reject'?'t-bad':(r.action==='review'?'t-warn':'t-ok')}">${r.action}</span></td></tr>`).join('')}
 </table></div>
 <div class="card"><h3>分佣与风控配置</h3>
  <div class="row"><span>最低提现额</span><input id="cmin" value="${cfg.withdraw_min}" style="width:100px">
   <span>提现手续费率</span><input id="cfee" value="${cfg.withdraw_fee_rate}" style="width:100px">
   <span>同IP点击上限</span><input id="cip" value="${(cfg.risk||{}).same_ip_click_max}" style="width:100px">
   <span>同指纹点击上限</span><input id="cfp" value="${(cfg.risk||{}).same_fp_click_max}" style="width:100px"></div>
  <div class="row"><span>转化率上限%</span><input id="ccvr" value="${Math.round(((cfg.risk||{}).cvr_max||0.25)*100)}" style="width:100px">
   <span>推广员24h成交上限</span><input id="cday" value="${(cfg.risk||{}).same_day_order_max}" style="width:100px">
   <span>自动拒单风控分</span><input id="crej" value="${(cfg.risk||{}).auto_reject_score}" style="width:100px"></div>
  <div class="row"><span>IP黑名单（逗号分隔）</span>
   <input id="cbl" value="${((cfg.risk||{}).ip_blacklist||[]).join(',')}" style="width:340px" placeholder="留空=不启用">
   <button onclick="savecfg()">保存</button></div>
  <div class="muted">正常推广转化率 1%–8%；>25% 且订单≥8 笔判畸高。留空黑名单即关闭。</div>
  <pre>${JSON.stringify(cfg.tiers,null,1)}</pre></div>`;}
async function settle(){const r=await api('/admin/settle',{method:'GET'});
 el('smsg').textContent=r.ok?('已结算 '+r.settled+' 单'):(r.error||'失败');boot();}
async function appr(id){await fetch('/api/admin/affiliate?id='+id+'&action=approve',
 {headers:{Authorization:'Bearer '+TOKEN}});boot();}
async function wdo(id,act){await fetch('/api/admin/withdraw?id='+id+'&action='+act,
 {headers:{Authorization:'Bearer '+TOKEN}});boot();}
async function savecfg(){const risk=(await api('/config')).config.risk||{};
 risk.same_ip_click_max=parseInt(el('cip').value);
 risk.same_fp_click_max=parseInt(el('cfp').value);
 risk.cvr_max=parseFloat(el('ccvr').value)/100;
 risk.same_day_order_max=parseInt(el('cday').value);
 risk.auto_reject_score=parseInt(el('crej').value);
 risk.ip_blacklist=(el('cbl').value||'').split(',').map(s=>s.trim()).filter(s=>s);
 await api('/admin/config',{method:'POST',body:JSON.stringify(
  {withdraw_min:parseFloat(el('cmin').value),withdraw_fee_rate:parseFloat(el('cfee').value),risk})});
 toast('已保存');}
boot();
"""


def page(title, js, sub):
    return """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>%s</title><style>%s</style></head><body>
<header><b>%s</b><span class="muted">%s</span><span class="sp"></span>
<a href="/">推广员端</a> · <a href="/admin">管理后台</a></header>
<div class="wrap"><div id="app"></div></div>
<div id="toast"></div>
<script>%s</script></body></html>""" % (title, CSS, title, sub, js)


PAGE_AFFILIATE = page("推广员端 · eSIM 分销", JS_AFF,
                      "选品 → 取链 → 看收益 → 提现")
PAGE_ADMIN = page("管理后台 · eSIM 分销", JS_ADMIN,
                  "审核推广员 / 结算佣金 / 配置分佣 / 风控拦截")

PAGE_LANDING = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>@@title@@</title><style>@@css@@
.hero{max-width:520px;margin:40px auto}.price{font-size:32px;color:#6ea8fe;font-weight:700}
</style></head><body><div class="wrap"><div class="card hero">
<h3>@@title@@</h3>
<div class="price">@@price@@ USD</div>
<div class="muted">@@desc@@</div>
<table>
<tr><td>覆盖区域</td><td>@@region@@</td></tr>
<tr><td>流量</td><td>@@data@@ GB</td></tr>
<tr><td>有效期</td><td>@@days@@ 天</td></tr>
<tr><td>推荐人</td><td>@@aff@@</td></tr>
</table>
<div class="row"><input id="ct" placeholder="邮箱或手机号（用于接收 eSIM 二维码）">
<button onclick="buy()">立即购买</button></div>
<div class="muted" id="msg"></div>
<div class="muted">推广码 @@code@@ ｜ 点击已记录（用于反刷量判定）</div>
</div></div>
<script>
function fp(){try{const c=document.createElement('canvas');const g=c.getContext('2d');
 g.textBaseline='top';g.font='14px Arial';g.fillText('fp',2,2);
 return btoa(navigator.userAgent+screen.width+screen.height+(c.toDataURL().length%9999)).slice(0,24);
}catch(e){return 'nofa'}}
async function buy(){const r=await fetch('/api/order',{method:'POST',headers:{'Content-Type':'application/json'},
 body:JSON.stringify({code:'@@code@@',click_id:'@@cid@@',fp:fp(),contact:document.getElementById('ct').value})});
 const j=await r.json();
 document.getElementById('msg').innerHTML=j.ok?('下单成功，佣金 '+(j.order?j.order.commission:'')+
  ' ｜ '+(j.note||'')):('下单被拦截：'+(j.reasons||[]).join('；')||j.error);}
</script></body></html>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8788)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--base-url", default=None)
    a = ap.parse_args()
    global BASE_URL
    if a.base_url:
        BASE_URL = a.base_url.rstrip("/")
    else:
        BASE_URL = "http://127.0.0.1:%d" % a.port
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    print("[+] eSIM 分销平台已启动：http://%s:%d/" % (a.host, a.port))
    print("    推广员端 /   管理后台 /admin   健康检查 /api/health")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[+] 已停止")


if __name__ == "__main__":
    main()
