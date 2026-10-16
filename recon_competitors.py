#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
recon_competitors.py —— 第三方电话卡 / eSIM 推广分销平台 竞品侦察器

背景：ghunter 用 "esim reseller platform" 等意图词在 GitHub 只命中 3 条（B/C 级），
说明"电话卡推广"这一细分在 GitHub 上几乎没有开源成品。因此本脚本改走
GitHub Search REST API 做宽域扇出，分四组关键词，抓真实 star / 语言 / 最近提交，
产出竞品功能对比所需的一手数据。

用法：
    python recon_competitors.py            # 跑全部关键词组，输出表 + 写 competitors.json
    python recon_competitors.py --json     # 只输出 JSON

只依赖标准库 + 本机代理 127.0.0.1:7897。Token 从 gh_token.txt 读（可选，有则限速更高）。
"""
import os
import sys
import json
import time
import urllib.request
import urllib.parse

PROXY = "http://127.0.0.1:7897"
TOKEN_FILE = "C:/Users/qq/.workbuddy/skills/github-upload-via-api/pw/gh_token.txt"
UA = "osint-recon/1.0"

# 四组关键词：① eSIM/电话卡供给侧 ② 联盟/分销平台 ③ 商业开源替代品 ④ 电信计费/转售
QUERY_GROUPS = {
    "eSIM_转售_供给侧": [
        "esim reseller platform",
        "esim api reseller white label",
        "esim store open source",
        "virtual phone number reseller",
        "sim card distribution system",
        "mvno billing open source",
        "telecom reseller platform",
        "travel esim shop",
    ],
    "联盟分销_平台层": [
        "affiliate platform self hosted",
        "affiliate tracking software open source",
        "referral marketing platform",
        "affiliate network software",
        "affiliate program laravel",
        "affiliate system django",
        "cps commission platform",
        "multi level marketing software open source",
    ],
    "商业产品_开源替代": [
        "rewardful alternative open source",
        "tapfiliate alternative",
        "post affiliate pro alternative open source",
        "firstpromoter alternative self hosted",
        "refersion alternative open source",
        "affiliatewp alternative plugin",
        "partnerstack alternative open source",
        "affiliate tracking python",
    ],
    "电信_号码_计费": [
        "telecom billing system open source",
        "voip reseller platform",
        "sms gateway reseller billing",
        "reseller panel open source",
        "whmcs reseller module",
        "subscription referral saas starter",
    ],
}


def gh_get(path, extra_headers=None, timeout=30):
    url = "https://api.github.com" + path
    hdr = {"User-Agent": UA, "Accept": "application/vnd.github+json"}
    if os.path.exists(TOKEN_FILE):
        hdr["Authorization"] = "Bearer " + open(TOKEN_FILE, encoding="utf-8").read().strip()
    if extra_headers:
        hdr.update(extra_headers)
    req = urllib.request.Request(url, headers=hdr)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}))
    return json.loads(opener.open(req, timeout=timeout).read().decode("utf-8"))


def search(q, per=10):
    path = "/search/repositories?sort=stars&order=desc&per_page=%d&q=%s" % (
        per, urllib.parse.quote(q))
    try:
        d = gh_get(path)
    except Exception as e:
        return {"error": "%s: %s" % (type(e).__name__, str(e)[:120]), "items": []}
    out = []
    for it in d.get("items", []):
        out.append({
            "full_name": it["full_name"],
            "stars": it["stargazers_count"],
            "lang": it.get("language"),
            "pushed": (it.get("pushed_at") or "")[:10],
            "desc": (it.get("description") or "")[:160],
            "url": it["html_url"],
            "topics": it.get("topics", [])[:6],
            "license": ((it.get("license") or {}) or {}).get("spdx_id"),
        })
    return {"items": out}


def main():
    as_json = "--json" in sys.argv
    result = {}
    all_repos = {}
    for group, queries in QUERY_GROUPS.items():
        if not as_json:
            print("\n" + "=" * 72)
            print("组：%s" % group)
            print("=" * 72)
        result[group] = {}
        for q in queries:
            r = search(q)
            result[group][q] = r
            if not as_json:
                print("\n  q = %r  -> %d 条" % (q, len(r.get("items", []))))
                if r.get("error"):
                    print("     ! %s" % r["error"])
            for it in r.get("items", []):
                all_repos.setdefault(it["full_name"], it)
            time.sleep(1.2)  # 尊重 GitHub search 限速

    ranked = sorted(all_repos.values(), key=lambda x: -x["stars"])
    if not as_json:
        print("\n" + "=" * 72)
        print("全部去重仓库按 star 排序（前 30）")
        print("=" * 72)
        for it in ranked[:30]:
            print("  %-8d %-46s %-12s push:%s  %s" % (
                it["stars"], it["full_name"][:46], str(it["lang"])[:12], it["pushed"],
                str(it["license"])))

    out = {"groups": result, "ranked": ranked, "total_unique": len(all_repos)}
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "competitors.json"),
              "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    if not as_json:
        print("\n去重后唯一仓库数：%d  -> competitors.json 已写出" % len(all_repos))
    return out


if __name__ == "__main__":
    main()
