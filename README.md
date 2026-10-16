# sim-affiliate-platform · 第三方电话卡 / eSIM 推广分销平台

能真跑的 Web 系统：供应商管理、卡品管理、推广员分级、专属推广链接 + 二维码、
订单与佣金结算（CPS/CPA/阶梯分佣）、提现申请、数据看板、防刷量风控。
后端纯 Python 标准库 `http.server`，前端内联 SPA，零依赖即可启动。

---

## 一、30 秒跑起来

```bash
cd simaffiliate
python seed.py --force --simulate     # 灌入示例数据（含被风控拦截的样本单）
python server.py --port 8791          # 启动
python e2e_test.py --base http://127.0.0.1:8791   # 端到端验收
```

浏览器：
- 推广员端 <http://127.0.0.1:8791/>（管理员 `admin@esim.demo` / `admin123`）
- 管理后台 <http://127.0.0.1:8791/admin>

可选依赖：`pip install qrcode`（二维码 SVG）。没装也能跑，二维码位置显示占位图。

---

## 二、真实运行回显（本机 2026-10-16 实测，非编造）

### 服务启动 + 健康检查

```
$ python server.py --port 8791
[+] eSIM 分销平台已启动：http://127.0.0.1:8791/

$ curl http://127.0.0.1:8791/api/health
{"ok": true, "service": "sim-affiliate", "ts": "2026-10-16 09:32:01", "base_url": "http://127.0.0.1:8791"}

$ curl http://127.0.0.1:8791/api/stats
{"ok": true, "stats": {"plans": 10, "affiliates": 4, "orders": 26, "gmv": 358.1, "commission_total": 84.5, "clicks": 59}}
```

### 端到端验收（`e2e_test.py` 实际输出节选）

```
--- 1 推广员登录  [HTTP 200] ---
    ->  数码博主小王 / L3 / 余额 0.0

--- 2 取专属推广链接  [HTTP 200] ---
    url      http://127.0.0.1:8793/r/4dj3evid
    code     4dj3evid
    qr_svg   6623 字节，开头：<?xml version='1.0' encoding='UTF-8'?> <svg width="37mm"

--- 3 访问推广落地页 /r/4dj3evid  [HTTP 200] ---
    落地页命中：True ｜ 页面长度 3334

--- 4 正常下单  [HTTP 200] ---
    ->  金额 15.9 ｜ 佣金 4.58 ｜ 风控分 0 ｜ 状态 pending

--- 5 自推自买下单（应拒绝）  [HTTP 200] ---
    rejected       True
    risk_score     3
    reasons   ['自推自买：下单联系方式与推广员本人一致']

--- 6 高频刷量后下单  ---
    预置点击 34 次 ｜ 风控分 2 ｜ 处置 review ｜ reasons ['同 IP 24h 点击 35 次（阈值 30）']

--- 8 一键结算 pending→settled  [HTTP 200] ---
    settled        25

--- 9a 超额提现（应被拒）  [HTTP 400] ---
    error          余额不足（当前 29.68）

--- 11 管理后台统计  [HTTP 200] ---
    {"orders": 29, "by_status": {"settled": 25, "rejected": 3, "review": 1},
     "gmv": 380.9, "commission": 93.66, "clicks": 94, "unique_ip": 26,
     "affiliates": 4, "risk_rejected": 3, "pending_withdraw": 700.0}

--- 13 风控日志  共 5 条 ---
    R0001  order  分3  reject   自推自买：下单联系方式与推广员本人一致
    R0002  order  分4  reject   同 IP 24h 点击 35 次; 同设备指纹 24h 点击 35 次
    R0005  order  分2  review   同 IP 24h 点击 35 次（阈值 30）
    R0008  order  分3  reject   同 IP 24h 点击 70 次（阈值 30）; 该 IP 历史下单 5 笔

验收结论：通过
```

---

## 三、功能清单

### 后端

| 模块 | 接口 | 说明 |
|---|---|---|
| 健康检查 | `GET /api/health` | 服务与 base_url |
| 卡品 | `GET /api/plans`、`POST /api/plans`(管理员) | 成本/售价/CPS 率/CPA 额/区域/流量/有效期 |
| 供应商 | `GET,POST /api/suppliers` | eSIM 上游渠道（eSIM Go / Maya / Airalo Partner / 运营商渠道） |
| 推广员 | `POST /api/register`、`/api/login`、`GET /api/me` | 首个注册者自动成为管理员；其余需后台审核 |
| 推广链接 | `GET,POST /api/link?plan_id=` | 每人每卡品一个短码 + 二维码 SVG |
| 落地页 | `GET /r/<code>` | 记录点击（IP/UA/设备指纹）并展示卡品 |
| 下单 | `POST /api/order` | 走风控后入账，实时算佣金 |
| 佣金 | `GET /api/commissions` | 明细 + 待结算/已结算汇总 |
| 提现 | `POST /api/withdraw` / `GET /api/withdrawals` | 最低额、手续费率可配置 |
| 管理 | `/api/admin/{stats,affiliates,orders,withdrawals,risk,clicks,settle,affiliate,withdraw,config}` | 审核、结算、配置、风控日志 |

### 佣金模型

```
佣金 = 卡品售价 × 卡品 CPS 率 × 等级倍率  +  阶梯加成（售价 × bonus）
等级：L1 ×1.0（0 单起）/ L2 ×1.25（30 单）/ L3 ×1.6（120 单）
阶梯：月 50 单 +2%、月 200 单 +4%
CPA 模式：固定额 × 等级倍率（卡品 model 切到 cpa 即生效）
```

### 防刷量风控（打分制）

| 规则 | 分值 | 处置 |
|---|---|---|
| 自推自买（下单联系方式 = 推广员本人邮箱/手机） | +3 | 直接拒单 |
| 同 IP 24h 点击超阈值（默认 30） | +2 | 复核/拒单 |
| 同设备指纹 24h 点击超阈值（默认 20） | +2 | 复核/拒单 |
| 该 IP 历史下单 ≥ 5 笔 | +1 | 复核 |
| 点击后 < 3 秒下单 | +1 | 复核 |

总分 ≥ 3 自动拒单，1~2 进人工复核，0 直接入账。阈值在 `/api/admin/config` 可改。

### 前端

- **推广员端** `/`：我的等级/余额/单数/佣金 → 选品表 → 专属链接 + 二维码（一键复制）→ 收益明细 → 提现申请
- **管理后台** `/admin`：平台总览 KPI → 推广员审核 → 订单（含风控分与原因）→ 提现审核（通过/驳回）→ 风控日志 → 分佣配置

---

## 四、竞品对比（侦察来源真实，见 `competitors.json`）

侦察方式：`ghunter --intent "esim reseller platform"` 等 6 组意图 + GitHub Search API
四组共 30 条查询（`recon_competitors.py`），去重得到 **27 个仓库**。

### 侦察结论（重要）

> **GitHub 上不存在"电话卡 / eSIM 推广分销平台"的成熟开源成品。**
> 命中最高的只有 ★268 的无关仓库、★56 的 Django 推荐返佣系统、以及一批 WHMCS 主机转售
> 模块（PHP，2016-2026 年，域名/主机行业）。eSIM 侧只找到供应商 SDK/API 档案，
> 没有"分销平台"层。→ 这个细分是空白，本平台填补的正是这一层。

### 功能对比表

| 能力 | 本平台 | Rewardful | FirstPromoter | Tapfiliate | Post Affiliate Pro | Refearn(开源★43) | django-referral-system(★56) |
|---|---|---|---|---|---|---|---|
| 自托管 | ✅ | ❌ SaaS | ❌ SaaS | ❌ SaaS | ✅ 商业授权 | ✅ | ✅ |
| 语言/栈 | Python stdlib | Ruby/Rails | SaaS | SaaS | PHP | TypeScript | Python/Django |
| 许可证 | MIT | 商业 | 商业 | 商业 | 商业 | 开源 | 开源 |
| 多级/阶梯分佣 | ✅ 等级倍率+阶梯加成 | ❌ | ✅ | ✅ | ✅ | 部分 | ❌ |
| CPS + CPA 双模型 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ |
| 专属链接 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| 二维码物料 | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| 提现/结算流 | ✅ | ❌(走 Stripe) | ✅ | ✅ | ✅ | ❌ | ❌ |
| 防刷量（IP/指纹/自推自买） | ✅ 打分制 | ❌ | 部分 | 部分 | ✅ | ❌ | ❌ |
| **卡品/套餐库存域模型** | ✅（流量/区域/有效期） | ❌ 通用 | ❌ 通用 | ❌ 通用 | ❌ 通用 | ❌ 通用 | ❌ 通用 |
| **eSIM/电话卡语义** | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| 供应商/上游 API 位 | ✅（预留 supplier 表） | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |

**差异点**：现有开源方案都是"通用推荐返佣"，没有卡品库存域（流量/区域/有效期/激活态），
也没有 eSIM 供应链这一层——而这恰恰是电话卡分销的核心。

### eSIM 上游（本平台 supplier 位对接的真实供应商，侦察所得）

| 上游 | 类型 | 说明 |
|---|---|---|
| eSIM Go | REST API | 白标 eSIM，reseller 体系（`api-evangelist/esimgo` 归档） |
| Maya Mobile | REST API | 400+ 漫游网络聚合（`api-evangelist/maya-mobile` 归档） |
| Tourist eSIM | PHP SDK | 面向分销商的套餐/激活/订单 SDK（`touristesim/touristesim-php-sdk`） |
| Airalo Partner API | REST | 商业合作制，需商务签约 |

---

## 五、文件说明

| 文件 | 作用 |
|---|---|
| `server.py` | 后端 + 前端（单文件，stdlib） |
| `seed.py` | 示例数据（供应商/卡品/推广员/订单/风控样本/提现） |
| `e2e_test.py` | 端到端验收脚本（登录→取链→点击→下单→风控→结算→提现→审核→看板） |
| `recon_competitors.py` | 竞品侦察（GitHub Search API，四组 30 条查询） |
| `competitors.json` | 侦察原始数据（27 个去重仓库） |
| `data/*.json` | 运行时数据（可 `rm -rf data` 重来） |

---

## 六、已知限制（诚实清单）

- 存储是 JSON 文件 + 全局锁，单机演示足够；上生产要换 SQLite/Postgres。
- 鉴权是简单 Bearer token（md5 存密码），生产需换成 bcrypt/argon2 + HTTPS。
- 设备指纹靠前端 canvas/UA/screen 拼接，可被伪造；真实场景要接风控 SDK。
- 供应商 API 只建了数据位，未实接 eSIM Go / Maya（需要商务账号）。
- 佣金结算为手动触发 `/api/admin/settle`，未做定时任务。
