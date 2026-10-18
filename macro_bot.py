# -*- coding: utf-8 -*-
"""
市场天气 Bot —— 宏观感知 + 操控雷达（学习用）

每次运行：
  1. 拉取免费公开数据（BTC/ETH、币市波动率 DVOL、资金费率、恐惧贪婪、VIX、MOVE、纳指）
  2. 判断现在是哪种"市场天气"（6 种状态）
  3. 状态变化 -> 推送 Telegram 提醒；每天早上 8 点 -> 推送一份早报

仅用于学习理解市场，不构成投资建议。
"""
import io
import json
import math
import re
import zipfile
import os
import sys
import time
import html
import argparse
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import requests

# ============================================================
# 设置区（新手只需要看这里；数字都可以改）
# ============================================================
TIMEZONE_OFFSET_HOURS = 8        # 马来西亚时间 UTC+8
# 除了你自己的私聊，还要同时收到通知的群 / 频道（bot 必须已经在群里，并且有发言权限）
EXTRA_CHAT_IDS = ["-1004438833552"]
MORNING_REPORT_HOUR = 8          # 每天几点发早报（本地时间）

T = {
    # --- 币市波动率 DVOL（类似币圈的 VIX）---
    "dvol_crash": 90,       # ≥ 这个值：系统性崩溃态
    "dvol_burst": 70,       # ≥ 这个值：震荡爆发期
    "dvol_chop": 55,        # ≥ 这个值且没有趋势：多空绞杀期
    "dvol_quiet": 50,       # < 这个值：才可能是静默期
    "dvol_jump_pct": 20,    # 24 小时内 DVOL 跳升超过这个 %：爆发
    # --- BTC 24 小时涨跌 ---
    "btc_crash_drop": -10,  # 24h 跌超 10%：崩溃态
    "btc_burst_move": 6,    # 24h 涨或跌超 6%：爆发期
    # --- 趋势强度 ADX ---
    "adx_no_trend": 20,     # < 20：基本没有趋势（来回震荡）
    "adx_quiet_max": 20,    # 静默期要求 ADX < 20
    # --- 成交量 ---
    "volume_quiet_ratio": 0.9,  # 近 5 天成交量 / 近 30 天平均 < 0.9：成交冷清
    # --- 美股恐慌指数 VIX ---
    "vix_quiet": 20,
    "vix_quiet_days": 10,   # 静默期要求 VIX 连续 10 个交易日都低于 20
    "vix_warn": 25,
    "vix_burst": 30,
    "vix_crash": 40,
    # --- 雷达阈值 ---
    "funding_hot": 0.0005,      # 8 小时资金费率 ≥ 0.05%：多头过挤
    "funding_cold": -0.0003,    # ≤ -0.03%：空头过挤
    "fng_greed": 80,
    "fng_fear": 20,
    "oi_jump_pct": 10,          # 24h 持仓量增加 ≥ 10% 但价格几乎没动
    "liq_big_usd": 50e6,        # BTC+ETH 24h 爆仓（只含 OKX、Gate 两家）≥ 5000 万美元：大规模清算
    "liq_one_side_pct": 75,     # 其中一边占 ≥ 75%：集中清算某一方
    "liq_big_usd_all": 300e6,   # 有 6 大交易所汇总（Coinalyze）时，BTC+ETH 24h 爆仓 ≥ 3 亿美元算大规模清算
    "stable_out_pct": -1.0,     # 稳定币总量 7 天减少超过 1%：资金在离场
    "stable_in_pct": 2.0,       # 7 天增加超过 2%：新资金在进场
    "dxy_jump_pct": 1.5,        # 美元指数 5 个交易日上涨 ≥ 1.5%：美元走强，压制风险资产
    "us10y_jump": 0.25,         # 10 年期美债收益率 5 个交易日上升 ≥ 0.25 个百分点
    "put_oi_jump_pct": 50,      # BTC 现价下方 2–8% 的看跌期权持仓 7 天增加 ≥ 50%：大资金在买下跌保护
    "hy_spread_jump": 0.5,      # 美国高收益债信用利差 5 个交易日走阔 ≥ 0.5 个百分点：信用市场开始紧张
    "epu_warn": 200,            # 美国政策不确定性指数（7 日均值）> 200：明显紧张
    "epu_crisis": 300,          # > 300：严重危机级
    "rv_scale_ratio": 1.5,      # BTC 7 天实现波动率 ≥ 30 天的 1.5 倍：波动率在放大，量化/CTA 可能被迫减仓
    "rv_scale_min": 50,         # 且 7 天实现波动率（年化）≥ 50%
    "flow_toxic_pct": 95,       # 24h 主动买卖失衡度处于近 7 天的 95% 分位以上：单边订单流异常集中
    "cme_am_chg_pct": 20,       # CME 资产管理机构净多仓一周变化 ≥ 20%：机构明显加仓 / 减仓
    "cliff_gamma_pct": 40,      # 下次月度到期会带走 ≥ 40% 的 BTC 期权 Gamma：到期后减震效果明显变弱
    "move_high_pct": 90,        # 债市恐慌 MOVE 处于 1 年 90% 分位以上
    "vix_low_pct": 50,          # 同时纳指恐慌 VXN（拿不到就用 VIX）在 1 年 50% 分位以下 → 股债"时差"
}

UA = {"User-Agent": "Mozilla/5.0 (market-weather-bot)"}
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")

# ============================================================
# 6 种市场天气（来自笔记本里的"5 种博弈态"，加一个"平稳行进期"作为常态）
# ============================================================
STATES = {
    "crash": {
        "emoji": "🌋", "name": "系统性崩溃态", "level": 5,
        "meaning": "恐慌蔓延、流动性消失，价格可能连续急跌或剧烈上下插针。",
        "do": "现金为王，先保住本金；已有仓位优先降杠杆。",
        "dont": "不要徒手接飞刀，不要用高杠杆抄底。",
    },
    "burst": {
        "emoji": "⛈", "name": "震荡爆发期", "level": 4,
        "meaning": "突发消息或大波动，高频算法在快速收割两边的流动性。",
        "do": "观望为主；要交易就减小仓位、降低杠杆。",
        "dont": "不追涨杀跌，不设很窄的固定止损（容易被瞬间扫掉）。",
    },
    "chop": {
        "emoji": "🌪", "name": "多空绞杀期", "level": 3,
        "meaning": "波动不小但没有方向，来回洗盘，专门扫两边的止损。",
        "do": "减少短线次数，等方向明确。",
        "dont": "不要频繁抄底摸顶，不要来回追单。",
    },
    "fading": {
        "emoji": "🌥", "name": "噪音衰退期", "level": 2,
        "meaning": "大波动之后正在降温，市场慢慢回到平静。",
        "do": "耐心等待，观察是否进入静默期。",
        "dont": "不急着提前重仓进场。",
    },
    "normal": {
        "emoji": "🌤", "name": "平稳行进期", "level": 1,
        "meaning": "波动正常，没有极端信号。",
        "do": "按自己的计划正常操作，顺着趋势方向做。",
        "dont": "不要因为一条新闻就临时改变计划。",
    },
    "quiet": {
        "emoji": "☀️", "name": "冷却静默期", "level": 0,
        "meaning": "市场很安静、成交冷清、没人关注——大资金常在这种时候悄悄布局。",
        "do": "最适合按计划小仓位、分批慢慢布局的时段。",
        "dont": "不要因为无聊就乱开高杠杆单。",
    },
}

# 每日一课（轮流发送）
LESSONS = [
    "<b>期望值</b>：期望值 =（胜率 × 平均赚）−（败率 × 平均亏）− 手续费、滑点和资金费。"
    "只有算出来是正数的交易方法才值得长期做。不求每次都赢，只求长期是对的。",
    "<b>止损簇</b>：很多人把止损放在同样的位置（前低下方、整数关口附近），"
    "这些止损堆在一起，就成了机器拉一下就能吃掉的“免费燃料”。",
    "<b>止损太窄怎么办</b>：如果你的止损距离比 BTC 一天的正常波动还小，"
    "就很容易被正常波动扫掉。这时该做的是<b>减小仓位/杠杆</b>，而不是把止损缩得更窄。",
    "<b>热点衰减</b>：一个新闻热点越往后追，剩下的肉越少。"
    "在群里或朋友圈看到“大家都在讲”的消息时，往往已经不是早期了。",
    "<b>资金费率</b>：永续合约里，做多的人多，多头就要付钱给空头（费率为正）。"
    "费率很高说明多头太拥挤，市场反而容易向下插针去扫多头。",
    "<b>DVOL 是什么</b>：DVOL 是币圈的“恐慌指数”，由 BTC 期权价格算出来，"
    "代表市场预期未来 30 天会有多大的波动。数字越高，市场越紧张。",
    "<b>时差</b>：风险通常先在债市（利率）出现，再传到股市，最后才到大盘。"
    "如果债市已经很紧张而股市还很平静，说明风险可能还没被完全反映。",
    "<b>假挂单</b>：盘口上突然出现的大单，很多会在几秒内撤掉，只是用来吓人或引人跟单。"
    "看盘时，真正成交的量比挂着的单更可信。",
    "<b>冰山订单</b>：真正的大资金很少一次挂出大单，而是用算法拆成成千上万笔小单慢慢成交。"
    "所以盘口上突然出现的“大墙”往往不是真的，真实的进出要看成交和持仓的变化。",
    "<b>对冲断崖</b>：月度期权到期后，做市商手上需要对冲的仓位一下子少了很多，"
    "平时“跌了买、涨了卖”的减震作用跟着变弱。到期后一周左右，一有坏消息波动就容易被放大。",
    "<b>0DTE 期权</b>：当天到期的期权 Gamma 极高，做市商要跟着价格不停调整对冲，"
    "会放大日内的来回波动。0DTE 占比越高，盘中越容易出现急拉急跌。",
    "<b>三级加速</b>：暴跌常分三步：先有人制造假突破引人跟风，再击穿大家的止损位引发集中卖出，"
    "最后波动率飙升触发量化基金的风控，被迫机械减仓——跌势就这样自我加速。",
    "<b>新新闻是坏新闻</b>：市场最怕“没见过的事”。出现前所未有的事件组合时，算法和资金会先撤退观望，"
    "这种不确定性可能压制市场很长时间。政策不确定性指数可以帮你感受这种紧张程度。",
    "<b>51% 的胜率</b>：量化不求每次都对，只要胜率略高于一半、盈亏比合理、成本够低，"
    "重复上万次就能稳定赚钱。普通人反过来：别押一次大的，要守住小优势和纪律。",
    "<b>状态比方向重要</b>：先判断现在是什么“天气”，再决定要不要出门。"
    "同一个策略，在静默期可能赚钱，在绞杀期可能被来回打止损。",
]

DISCLAIMER = "<i>仅用于学习理解市场，不构成投资建议。</i>"


# ============================================================
# 工具函数
# ============================================================
def now_local():
    return datetime.now(timezone.utc) + timedelta(hours=TIMEZONE_OFFSET_HOURS)


def get_json(url, params=None, timeout=20, tries=2, headers=None):
    last = None
    for i in range(tries):
        try:
            r = requests.get(url, params=params, headers=dict(UA, **(headers or {})), timeout=timeout)
            r.raise_for_status()
            return r.json()
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 429 and i + 1 < tries:
                time.sleep(min(float(e.response.headers.get("Retry-After") or 10), 65))  # 请求太频繁：等一下再试
                continue
            if e.response is not None and 400 <= e.response.status_code < 500:
                raise  # 地区限制 / 参数错误，重试也没用
            last = e
            time.sleep(2)
        except Exception as e:  # noqa
            last = e
            time.sleep(2)
    raise last


def esc(s):
    return html.escape(str(s), quote=False)


def fmt_money(x, digits=0):
    if x is None:
        return "—"
    return f"${x:,.{digits}f}"


def fmt_musd(x):
    if x is None:
        return "—"
    return f"${x / 1e6:,.1f}M" if x >= 1e6 else f"${x / 1e3:,.0f}K"


def fmt_busd(x):
    if x is None:
        return "—"
    return f"${x / 1e12:,.2f}T" if x >= 1e12 else f"${x / 1e9:,.1f}B"


def fmt_pct(x, digits=1, sign=True):
    if x is None:
        return "—"
    return f"{x:+.{digits}f}%" if sign else f"{x:.{digits}f}%"


def percentile_rank(values, v):
    vals = [x for x in values if x is not None]
    if not vals or v is None:
        return None
    return 100.0 * sum(1 for x in vals if x <= v) / len(vals)


# ============================================================
# 数据获取
# 原则：
#   - 价格、资金费率、持仓量、成交量都同时向多家交易所取，各家原始数字一起汇总，不依赖单一来源
#   - 每家交易所单独容错：某家拿不到就跳过，报告里注明用了几家、哪几家失败
#   - 拿不到的数据就显示“—”，绝不用别的数据去推算代替
# ============================================================
COINS = ("BTC", "ETH")
DERIBIT = "https://www.deribit.com/api/v2/public"


def num(x):
    return float(x) if x not in (None, "") else None


def median(xs):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    k = len(xs) // 2
    return xs[k] if len(xs) % 2 else (xs[k - 1] + xs[k]) / 2


# ---------- 现货：最新价 + 交易所自己公布的 24 小时涨跌 ----------
# 每个函数返回 {"price": 最新价, "chg24": 24h 涨跌 %（交易所不提供就是 None）}
def spot_coinbase(coin):
    d = get_json(f"https://api.exchange.coinbase.com/products/{coin}-USD/stats")
    last, op = num(d["last"]), num(d["open"])  # open = 24 小时前的价格
    return {"price": last, "chg24": (last / op - 1) * 100 if op else None}


def spot_kraken(coin):
    d = get_json("https://api.kraken.com/0/public/Ticker", {"pair": {"BTC": "XBTUSD"}.get(coin, coin + "USD")})
    if d.get("error"):
        raise RuntimeError(d["error"])
    r = next(iter(d["result"].values()))
    return {"price": num(r["c"][0]), "chg24": None}  # Kraken 不公布滚动 24h 涨跌，只取价格


def spot_bitstamp(coin):
    d = get_json(f"https://www.bitstamp.net/api/v2/ticker/{coin.lower()}usd/")
    return {"price": num(d["last"]), "chg24": num(d.get("percent_change_24"))}


def spot_okx(coin):
    d = get_json("https://www.okx.com/api/v5/market/ticker", {"instId": f"{coin}-USDT"})["data"][0]
    last, op = num(d["last"]), num(d["open24h"])
    return {"price": last, "chg24": (last / op - 1) * 100 if op else None}


def spot_binance(coin):
    d = get_json("https://data-api.binance.vision/api/v3/ticker/24hr", {"symbol": f"{coin}USDT"})
    return {"price": num(d["lastPrice"]), "chg24": num(d["priceChangePercent"])}


SPOT_SOURCES = {"Coinbase": spot_coinbase, "Kraken": spot_kraken, "Bitstamp": spot_bitstamp,
                "OKX": spot_okx, "Binance": spot_binance}


# ---------- 永续合约：资金费率（统一换算成每 8 小时）+ 持仓量（美元） ----------
# 每个函数返回 {"funding_8h": 小数, "oi_usd": 美元}；拿不到的字段为 None
def perp_deribit(coin):
    d = get_json(f"{DERIBIT}/ticker", {"instrument_name": f"{coin}-PERPETUAL"})["result"]
    return {"funding_8h": d.get("funding_8h"), "oi_usd": d.get("open_interest")}  # Deribit 永续持仓以美元计


def perp_okx(coin):
    inst = f"{coin}-USDT-SWAP"
    fr = get_json("https://www.okx.com/api/v5/public/funding-rate", {"instId": inst})["data"][0]
    hours = (int(fr["nextFundingTime"]) - int(fr["fundingTime"])) / 3600000 or 8
    oi = get_json("https://www.okx.com/api/v5/public/open-interest",
                  {"instType": "SWAP", "instId": inst})["data"][0]
    return {"funding_8h": num(fr["fundingRate"]) * 8 / hours, "oi_usd": num(oi.get("oiUsd"))}


def perp_bitget(coin):
    q = {"symbol": f"{coin}USDT", "productType": "usdt-futures"}
    t = get_json("https://api.bitget.com/api/v2/mix/market/ticker", q)["data"][0]
    fr = get_json("https://api.bitget.com/api/v2/mix/market/current-fund-rate", q)["data"][0]
    hours = int(fr.get("fundingRateInterval") or 8)
    # 持仓量以币计，按标记价格换算成美元
    return {"funding_8h": num(t["fundingRate"]) * 8 / hours,
            "oi_usd": num(t["holdingAmount"]) * num(t["markPrice"])}


def perp_gate(coin):
    c = get_json(f"https://api.gateio.ws/api/v4/futures/usdt/contracts/{coin}_USDT")
    st = get_json("https://api.gateio.ws/api/v4/futures/usdt/contract_stats",
                  {"contract": f"{coin}_USDT", "interval": "5m", "limit": 1})
    hours = int(c.get("funding_interval") or 28800) / 3600
    return {"funding_8h": num(c["funding_rate"]) * 8 / hours,
            "oi_usd": num(st[-1]["open_interest_usd"]) if st else None}


def perp_hyperliquid(coin):
    r = requests.post("https://api.hyperliquid.xyz/info", json={"type": "metaAndAssetCtxs"},
                      headers=UA, timeout=15)
    r.raise_for_status()
    meta, ctxs = r.json()
    for a, c in zip(meta["universe"], ctxs):
        if a["name"] == coin:
            # Hyperliquid 每小时结算一次资金费；持仓量以币计，按标记价格换算成美元
            return {"funding_8h": num(c["funding"]) * 8, "oi_usd": num(c["openInterest"]) * num(c["markPx"])}
    raise KeyError(coin)


# Binance 合约、Bybit 会拒绝来自 GitHub 美国服务器的请求，所以这里不用它们
PERP_SOURCES = {"OKX": perp_okx, "Bitget": perp_bitget, "Gate": perp_gate,
                "Deribit": perp_deribit, "Hyperliquid": perp_hyperliquid}


# ---------- BTC 现货日 K 线（UTC 日），用来算 ADX、日均波动和成交量 ----------
# 每个函数返回 {当天 0 点的 UTC 时间戳(秒): (最高, 最低, 收盘, 成交量[BTC])}
def daily_coinbase():
    rows = get_json("https://api.exchange.coinbase.com/products/BTC-USD/candles", {"granularity": 86400})
    return {int(r[0]): (r[2], r[1], r[4], r[5]) for r in rows}


def daily_kraken():
    d = get_json("https://api.kraken.com/0/public/OHLC", {"pair": "XBTUSD", "interval": 1440})
    if d.get("error"):
        raise RuntimeError(d["error"])
    rows = next(v for k, v in d["result"].items() if k != "last")
    return {int(r[0]): (num(r[2]), num(r[3]), num(r[4]), num(r[6])) for r in rows}


def daily_bitstamp():
    rows = get_json("https://www.bitstamp.net/api/v2/ohlc/btcusd/",
                    {"step": 86400, "limit": 120})["data"]["ohlc"]
    return {int(r["timestamp"]): (num(r["high"]), num(r["low"]), num(r["close"]), num(r["volume"])) for r in rows}


def daily_okx():
    rows = get_json("https://www.okx.com/api/v5/market/candles",
                    {"instId": "BTC-USDT", "bar": "1Dutc", "limit": 120})["data"]
    return {int(r[0]) // 1000: (num(r[2]), num(r[3]), num(r[4]), num(r[5])) for r in rows}


def daily_binance():
    rows = get_json("https://data-api.binance.vision/api/v3/klines",
                    {"symbol": "BTCUSDT", "interval": "1d", "limit": 120})
    return {int(r[0]) // 1000: (num(r[2]), num(r[3]), num(r[4]), num(r[5])) for r in rows}


DAILY_SOURCES = {"Coinbase": daily_coinbase, "Kraken": daily_kraken, "Bitstamp": daily_bitstamp,
                 "OKX": daily_okx, "Binance": daily_binance}


def combine_daily(per_ex):
    """只保留所有成功来源都有数据、且已经走完的日子：
    高 / 低 / 收盘取各家中位数，成交量把各家相加（单位都是 BTC）。"""
    today0 = int(time.time()) // 86400 * 86400
    days = None
    for rows in per_ex.values():
        ks = {k for k in rows if k < today0}
        days = ks if days is None else days & ks
    days = sorted(days or [])[-120:]
    if not days:
        return None
    out = {"high": [], "low": [], "close": [], "volume": [], "ticks": days, "sources": list(per_ex)}
    for d in days:
        rows = [per_ex[ex][d] for ex in per_ex]
        out["high"].append(median(r[0] for r in rows))
        out["low"].append(median(r[1] for r in rows))
        out["close"].append(median(r[2] for r in rows))
        out["volume"].append(sum(r[3] for r in rows))
    return out


# ---------- 只有单一发布方的指数 ----------
def fetch_dvol(hours=8 * 24):
    # DVOL 是 Deribit 编制并发布的指数（Deribit 占 BTC 期权成交的大头），没有第二个发布方
    end = int(time.time() * 1000)
    start = end - hours * 3600 * 1000
    rows = get_json(f"{DERIBIT}/get_volatility_index_data", {
        "currency": "BTC", "start_timestamp": start, "end_timestamp": end,
        "resolution": "3600"})["result"]["data"]
    rows = sorted(rows, key=lambda r: r[0])
    closes = [r[4] for r in rows]
    if not closes:
        return None

    def ago(h):
        idx = len(closes) - 1 - h
        return closes[idx] if idx >= 0 else closes[0]

    return {"now": closes[-1], "d1": ago(24), "d7": closes[0]}


def fetch_btc_options():
    """Deribit 全部 BTC 期权的持仓量（以 BTC 计）和隐含波动率。Deribit 占 BTC 期权的大头。"""
    rows = get_json(f"{DERIBIT}/get_book_summary_by_currency", {"currency": "BTC", "kind": "option"})["result"]
    out = []
    for r in rows:
        try:
            _, exp, strike, typ = r["instrument_name"].split("-")  # 例如 BTC-25OCT26-80000-P
            expiry = datetime.strptime(exp, "%d%b%y").replace(hour=8, tzinfo=timezone.utc)  # 到期时间 08:00 UTC
        except ValueError:
            continue
        out.append({"strike": float(strike), "put": typ == "P", "expiry": expiry.timestamp(),
                    "oi": r.get("open_interest") or 0.0, "iv": r.get("mark_iv"),
                    "underlying": r.get("underlying_price")})
    return out


def fetch_fng():
    d = get_json("https://api.alternative.me/fng/", {"limit": 1})["data"][0]
    return int(d["value"])


def fetch_yahoo_closes(symbol):
    for host in ("query1", "query2"):
        try:
            url = f"https://{host}.finance.yahoo.com/v8/finance/chart/{quote(symbol)}"
            d = get_json(url, {"range": "1y", "interval": "1d"})
            q = d["chart"]["result"][0]["indicators"]["quote"][0]["close"]
            closes = [x for x in q if x is not None]
            if closes:
                return closes
        except Exception:
            continue
    return None


def fetch_fred_dated(series):
    """美联储圣路易斯分行 FRED 的官方数据，返回 [(日期, 数值)]（空值的日子跳过），只取最近约 14 个月。"""
    start = (datetime.now(timezone.utc) - timedelta(days=420)).strftime("%Y-%m-%d")
    r = requests.get("https://fred.stlouisfed.org/graph/fredgraph.csv",
                     params={"id": series, "cosd": start}, headers=UA, timeout=30)
    r.raise_for_status()
    rows = []
    for line in r.text.strip().splitlines()[1:]:
        parts = line.split(",")
        try:
            rows.append((parts[0], float(parts[1])))
        except (ValueError, IndexError):
            pass
    return rows[-260:] if rows else None


def fetch_fred(series):
    # 作为 Yahoo 拿不到时的第二来源
    rows = fetch_fred_dated(series)
    return [v for _, v in rows] if rows else None


# ---------- 全市场汇总（第三方，免费 API） ----------
COINALYZE_KEY = os.environ.get("COINALYZE_API_KEY", "").strip()
COINGECKO_KEY = os.environ.get("COINGECKO_API_KEY", "").strip()
CZ = "https://api.coinalyze.net/v1"
# Coinalyze 的交易所代码 → 名字
CZ_EXCHANGES = {"A": "Binance", "6": "Bybit", "3": "OKX", "2": "Deribit", "H": "Hyperliquid", "Y": "Gate"}
# 只取这 6 家大交易所的 BTC / ETH 主力永续合约。免费额度按合约个数计，
# 每次运行约 30 个，不超过每分钟 40 个的上限
CZ_SYMBOLS = {
    "BTC": ["BTCUSDT_PERP.A", "BTCUSD_PERP.A", "BTCUSDT.6", "BTCUSDT_PERP.3", "BTC-PERPETUAL.2", "BTC.H",
            "BTC_USDT.Y"],
    "ETH": ["ETHUSDT_PERP.A", "ETHUSD_PERP.A", "ETHUSDT.6", "ETHUSDT_PERP.3", "ETH-PERPETUAL.2", "ETH.H",
            "ETH_USDT.Y"],
}


def cz_get(path, params=None):
    return get_json(f"{CZ}/{path}", params, headers={"api_key": COINALYZE_KEY})


def fetch_coinalyze():
    """6 家大交易所（Coinalyze 汇总）：各合约持仓量、24h 多空爆仓，以及 Binance / Bybit 的资金费率。"""
    if not COINALYZE_KEY:
        raise RuntimeError("没有设置 COINALYZE_API_KEY")
    coin_of = {sym: c for c, syms in CZ_SYMBOLS.items() for sym in syms}
    syms = ",".join(coin_of)
    now = int(time.time())
    out = {c: {"oi_by_sym": {}, "liq_long": 0.0, "liq_short": 0.0, "exchanges": set()} for c in COINS}

    oi_now = {r["symbol"]: r["value"] for r in cz_get("open-interest", {"symbols": syms, "convert_to_usd": "true"})}
    for sym, v in oi_now.items():
        c = coin_of[sym]
        out[c]["oi_by_sym"][sym] = v
        out[c]["exchanges"].add(CZ_EXCHANGES[sym.rsplit(".", 1)[1]])

    for r in cz_get("liquidation-history", {"symbols": syms, "interval": "1hour", "from": now - 24 * 3600,
                                            "to": now, "convert_to_usd": "true"}):
        c = coin_of[r["symbol"]]
        for x in r.get("history", []):
            out[c]["liq_long"] += x["l"]
            out[c]["liq_short"] += x["s"]

    # Binance、Bybit 的资金费率（GitHub 服务器直连不了这两家，经 Coinalyze 取）
    # Coinalyze 给的数字单位是 %，这两家的 BTC / ETH USDT 永续都是每 8 小时结算
    perp = {}
    fr_syms = {"BTCUSDT_PERP.A": ("BTC", "Binance"), "ETHUSDT_PERP.A": ("ETH", "Binance"),
               "BTCUSDT.6": ("BTC", "Bybit"), "ETHUSDT.6": ("ETH", "Bybit")}
    for r in cz_get("funding-rate", {"symbols": ",".join(fr_syms)}):
        c, ex = fr_syms[r["symbol"]]
        perp.setdefault(c, {})[ex] = {"funding_8h": r["value"] / 100, "oi_usd": oi_now.get(r["symbol"])}
    for c in COINS:
        out[c]["exchanges"] = sorted(out[c]["exchanges"])
    out["perp"] = perp
    return out


def fetch_flow_imbalance():
    """Binance BTC 永续最近 7 天每小时的成交量和主动买入量（经 Coinalyze），用来近似“毒性订单流”。"""
    now = int(time.time())
    r = cz_get("ohlcv-history", {"symbols": "BTCUSDT_PERP.A", "interval": "1hour",
                                 "from": now - 7 * 86400, "to": now})
    return [(x["t"], x["v"], x["bv"]) for x in r[0]["history"]] if r else None


# ---------- CME（芝商所）：受美国监管的机构市场 ----------
CFTC = "https://publicreporting.cftc.gov/resource"
# CFTC 每周公布的持仓报告（TFF 金融期货分类）；比特币标准合约 5 BTC / 张，微型合约 0.1 BTC / 张
CME_BTC_CODES = {"133741": 5.0, "133742": 0.1}


def fetch_cme_cot():
    """CFTC 持仓周报：CME 比特币期货 + 期权（Delta 折算）的持仓，以及机构、对冲基金、交易商的多空。"""
    def pull(dataset):
        rows = get_json(f"{CFTC}/{dataset}.json", {
            "$where": "cftc_contract_market_code in('133741','133742')",
            "$order": "report_date_as_yyyy_mm_dd DESC", "$limit": 8})
        by_date = {}
        for r in rows:
            k = CME_BTC_CODES[r["cftc_contract_market_code"]]
            d = by_date.setdefault(r["report_date_as_yyyy_mm_dd"][:10], {"oi": 0.0, "am": 0.0, "lev": 0.0, "dealer": 0.0})
            d["oi"] += num(r["open_interest_all"]) * k
            d["am"] += (num(r["asset_mgr_positions_long"]) - num(r["asset_mgr_positions_short"])) * k
            d["lev"] += (num(r["lev_money_positions_long"]) - num(r["lev_money_positions_short"])) * k
            d["dealer"] += (num(r["dealer_positions_long_all"]) - num(r["dealer_positions_short_all"])) * k
        return [(d, by_date[d]) for d in sorted(by_date, reverse=True)]
    combined = pull("yw9f-hn96")   # 期货 + 期权
    futures = dict(pull("gpe5-46if"))  # 只有期货
    if not combined:
        return None
    date, now = combined[0]
    prev = combined[1][1] if len(combined) > 1 else None
    opt_oi = now["oi"] - futures[date]["oi"] if date in futures else None
    return {"date": date, "now": now, "prev": prev, "options_oi": opt_oi}


def fetch_cme_basis():
    """CME 比特币近月期货的最新价（Yahoo），只在 1 小时内有成交时才用（CME 周末休市）。"""
    meta = get_json("https://query1.finance.yahoo.com/v8/finance/chart/BTC%3DF",
                    {"range": "1d", "interval": "1h"})["chart"]["result"][0]["meta"]
    return {"price": meta["regularMarketPrice"], "time": meta["regularMarketTime"],
            "name": meta.get("shortName", "")}


# ---------- 新闻与情绪（多个独立来源，左右对照） ----------
GDELT = "https://api.gdeltproject.org/api/v2/doc/doc"


def gdelt_timeline(query, mode, timespan):
    """GDELT 全球新闻数据库（免费，每 5 秒最多 1 次请求）：返回 [(时间戳, 数值)]。"""
    time.sleep(6)
    d = get_json(GDELT, {"query": query + " sourcelang:english", "mode": mode,
                         "timespan": timespan, "format": "json"}, timeout=30, tries=1)  # 被限流就算了，不干等
    out = []
    for x in d["timeline"][0]["data"]:
        t = datetime.strptime(x["date"], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).timestamp()
        out.append((t, x["value"]))
    return out


def fetch_news():
    now = time.time()

    def recent_vs_all(rows, hours=24):
        last = [v for t, v in rows if now - t <= hours * 3600] or [rows[-1][1]]
        return sum(last) / len(last), sum(v for _, v in rows) / len(rows)
    out, last_err = {}, None
    # GDELT 对共用 IP 限流很严：三个查询分开取，拿到几个算几个
    try:
        out["tone"], out["tone_base"] = recent_vs_all(
            gdelt_timeline('(economy OR "stock market" OR inflation OR "interest rates")', "timelinetone", "14d"))
    except Exception as e:  # noqa
        last_err = e
    try:
        a, b = recent_vs_all(gdelt_timeline(
            '(recession OR crisis OR crash OR default OR "bank run" OR war)', "timelinevol", "30d"), 36)
        out["crisis_ratio"] = a / b if b else None
    except Exception as e:  # noqa
        last_err = e
    try:
        out["btc_tone"] = recent_vs_all(gdelt_timeline("bitcoin", "timelinetone", "3d"))[0]
    except Exception as e:  # noqa
        last_err = e
    if not out:
        raise last_err
    return out


def fetch_cnn_fg():
    d = get_json("https://production.dataviz.cnn.io/index/fearandgreed/graphdata")["fear_and_greed"]
    return {"score": d["score"], "week": d.get("previous_1_week")}


def fetch_sf_news_sentiment():
    """旧金山联储每日新闻情绪指数（Excel）：取最后两个多月的数据。"""
    r = requests.get("https://www.frbsf.org/wp-content/uploads/news_sentiment_data.xlsx", headers=UA, timeout=30)
    r.raise_for_status()
    sheet = zipfile.ZipFile(io.BytesIO(r.content)).read("xl/worksheets/sheet2.xml").decode()
    rows = re.findall(r'<c r="A\d+"[^>]*><v>([\d.]+)</v></c><c r="B\d+"[^>]*><v>(-?[\d.Ee-]+)</v></c>', sheet)
    rows = [(float(a), float(b)) for a, b in rows[-70:]]
    day0 = datetime(1899, 12, 30)  # Excel 日期从这一天起算
    return [((day0 + timedelta(days=a)).strftime("%m-%d"), b) for a, b in rows]


def fetch_gpr():
    """地缘政治风险指数（美联储经济学家 Caldara & Iacoviello 每日更新的 Excel）。"""
    import xlrd
    r = requests.get("https://www.matteoiacoviello.com/gpr_files/data_gpr_daily_recent.xls", headers=UA, timeout=60)
    r.raise_for_status()
    sh = xlrd.open_workbook(file_contents=r.content).sheet_by_index(0)
    col = sh.row_values(0).index("GPRD_MA7")
    vals = [v for v in (sh.cell_value(i, col) for i in range(max(1, sh.nrows - 365), sh.nrows)) if isinstance(v, float)]
    return {"now": vals[-1], "pct": percentile_rank(vals, vals[-1])}


def cached(state, name, hours, fn, errors, label):
    """每天才更新一次的大文件：隔 hours 小时才重新下载，其余时间用上次的结果。"""
    c = state.setdefault("cache", {}).get(name)
    if c and time.time() - c["t"] < hours * 3600:
        return c["v"]
    v = safe(fn, label=label, errors=errors)
    if v is not None:
        state["cache"][name] = {"t": int(time.time()), "v": v}
        return v
    return c["v"] if c else None


def fetch_coingecko_global():
    if not COINGECKO_KEY:
        raise RuntimeError("没有设置 COINGECKO_API_KEY")
    d = get_json("https://api.coingecko.com/api/v3/global", headers={"x-cg-demo-api-key": COINGECKO_KEY})["data"]
    pct = d["market_cap_percentage"]
    return {"total_mcap": d["total_market_cap"]["usd"],
            "mcap_chg24": d.get("market_cap_change_percentage_24h_usd"),
            "btc_dom": pct.get("btc"), "eth_dom": pct.get("eth"),
            "stable_dom": (pct.get("usdt") or 0) + (pct.get("usdc") or 0)}


def fetch_stablecoins():
    # DefiLlama：所有美元稳定币的流通总量（每天一个数）
    rows = get_json("https://stablecoins.llama.fi/stablecoincharts/all")
    vals = [(int(r["date"]), r["totalCirculatingUSD"]["peggedUSD"]) for r in rows
            if r.get("totalCirculatingUSD", {}).get("peggedUSD")]
    return vals[-40:]


# ---------- 爆仓（强平） ----------
# Binance、Bybit 拒绝 GitHub 美国服务器，Bitget 没有公开爆仓接口，所以只用这两家：
#   Gate：交易所直接公布每小时的多单 / 空单爆仓金额（美元），取最近 24 小时
#   OKX：只公布最近一百多条爆仓单，bot 每小时运行时把新出现的爆仓单累计记下来
def liq_gate(coin):
    rows = get_json("https://api.gateio.ws/api/v4/futures/usdt/contract_stats",
                    {"contract": f"{coin}_USDT", "interval": "1h", "limit": 24})
    return {"long": sum(num(r["long_liq_usd"]) for r in rows),
            "short": sum(num(r["short_liq_usd"]) for r in rows), "hours": len(rows)}


def liq_okx_events(coin, pages=10):
    """返回 [(时间戳毫秒, "long"/"short", 美元)]，从新到旧。"""
    inst = get_json("https://www.okx.com/api/v5/public/instruments",
                    {"instType": "SWAP", "instId": f"{coin}-USDT-SWAP"})["data"][0]
    ct_val = num(inst["ctVal"])  # 每张合约是多少个币
    out, after = [], None
    for _ in range(pages):
        q = {"instType": "SWAP", "uly": f"{coin}-USDT", "state": "filled", "limit": 100}
        if after:
            q["after"] = after
        data = get_json("https://www.okx.com/api/v5/public/liquidation-orders", q)["data"]
        rows = data[0]["details"] if data else []
        for r in rows:
            long_ = r["posSide"] == "long" or (r["posSide"] == "net" and r["side"] == "sell")
            out.append((int(r["ts"]), "long" if long_ else "short", num(r["sz"]) * ct_val * num(r["bkPx"])))
        if len(rows) < 100:
            break
        after = rows[-1]["ts"]
    return out


def safe(fn, *a, label="", errors=None):
    try:
        return fn(*a)
    except Exception as e:  # noqa
        if errors is not None:
            errors.append(f"{label}: {type(e).__name__}")
        print(f"[warn] {label} 获取失败: {e}", file=sys.stderr)
        return None


def collect(sources, kind, errors, *a):
    out = {}
    for name, fn in sources.items():
        r = safe(fn, *a, label=f"{name} {kind}", errors=errors)
        if r:
            out[name] = r
    return out


def fetch_all(state):
    errors = []
    data = {"errors": errors}
    data["spot"] = {c: collect(SPOT_SOURCES, f"{c}现货", errors, c) for c in COINS}
    data["perp"] = {c: collect(PERP_SOURCES, f"{c}合约", errors, c) for c in COINS}
    data["daily"] = combine_daily(collect(DAILY_SOURCES, "日线", errors))
    data["liq_gate"] = {c: safe(liq_gate, c, label=f"Gate {c}爆仓", errors=errors) for c in COINS}
    data["liq_okx"] = {c: safe(liq_okx_events, c, label=f"OKX {c}爆仓", errors=errors) for c in COINS}
    data["cz"] = safe(fetch_coinalyze, label="Coinalyze", errors=errors)
    if data["cz"]:
        for c in COINS:
            data["perp"][c].update(data["cz"]["perp"].get(c, {}))
    data["cg"] = safe(fetch_coingecko_global, label="CoinGecko", errors=errors)
    data["stable"] = safe(fetch_stablecoins, label="DefiLlama稳定币", errors=errors)
    data["dxy"] = safe(fetch_fred_dated, "DTWEXBGS", label="美元指数(FRED)", errors=errors)
    data["hy"] = safe(fetch_fred_dated, "BAMLH0A0HYM2", label="信用利差(FRED)", errors=errors)
    data["epu"] = safe(fetch_fred_dated, "USEPUINDXD", label="政策不确定性(FRED)", errors=errors)
    data["cme_cot"] = safe(fetch_cme_cot, label="CFTC持仓周报", errors=errors)
    data["cme_px"] = safe(fetch_cme_basis, label="CME期货价", errors=errors)
    data["news"] = cached(state, "gdelt", 2, fetch_news, errors, "GDELT新闻")
    data["news_age_h"] = (time.time() - state["cache"]["gdelt"]["t"]) / 3600 if state.get("cache", {}).get("gdelt") else None
    data["cnn_fg"] = safe(fetch_cnn_fg, label="CNN恐惧贪婪", errors=errors)
    data["sf_sent"] = cached(state, "sf_sent", 12, fetch_sf_news_sentiment, errors, "旧金山联储新闻情绪")
    data["gpr"] = cached(state, "gpr", 12, fetch_gpr, errors, "地缘政治风险")
    data["flow"] = safe(fetch_flow_imbalance, label="Coinalyze订单流", errors=errors) if COINALYZE_KEY else None
    data["us10y"] = safe(fetch_fred_dated, "DGS10", label="美债10年(FRED)", errors=errors)
    data["options"] = safe(fetch_btc_options, label="Deribit期权", errors=errors)
    data["dvol"] = safe(fetch_dvol, label="DVOL", errors=errors)
    data["fng"] = safe(fetch_fng, label="恐惧贪婪", errors=errors)
    data["vix_series"] = (safe(fetch_yahoo_closes, "^VIX", label="VIX(Yahoo)", errors=errors)
                          or safe(fetch_fred, "VIXCLS", label="VIX(FRED)", errors=errors))
    data["move_series"] = safe(fetch_yahoo_closes, "^MOVE", label="MOVE", errors=errors)
    data["vxn_series"] = (safe(fetch_yahoo_closes, "^VXN", label="VXN(Yahoo)", errors=errors)
                          or safe(fetch_fred, "VXNCLS", label="VXN(FRED)", errors=errors))
    data["ndx_series"] = (safe(fetch_yahoo_closes, "^NDX", label="纳指(Yahoo)", errors=errors)
                          or safe(fetch_fred, "NASDAQ100", label="纳指(FRED)", errors=errors))
    return data


# ============================================================
# 指标计算
# ============================================================
def wilder_adx_atr(high, low, close, n=14):
    if len(close) < 2 * n + 2:
        return None, None
    trs, pdms, mdms = [], [], []
    for i in range(1, len(close)):
        h, l, pc = high[i], low[i], close[i - 1]
        up, dn = high[i] - high[i - 1], low[i - 1] - low[i]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        pdms.append(up if (up > dn and up > 0) else 0.0)
        mdms.append(dn if (dn > up and dn > 0) else 0.0)
    s_tr, s_p, s_m = sum(trs[:n]), sum(pdms[:n]), sum(mdms[:n])
    dxs = []
    for i in range(n, len(trs) + 1):
        if i > n:
            s_tr = s_tr - s_tr / n + trs[i - 1]
            s_p = s_p - s_p / n + pdms[i - 1]
            s_m = s_m - s_m / n + mdms[i - 1]
        pdi = 100 * s_p / s_tr if s_tr else 0
        mdi = 100 * s_m / s_tr if s_tr else 0
        dxs.append(100 * abs(pdi - mdi) / (pdi + mdi) if (pdi + mdi) else 0)
    adx = sum(dxs[:n]) / n
    for dx in dxs[n:]:
        adx = (adx * (n - 1) + dx) / n
    atr = s_tr / n
    return adx, atr


def funding_summary(perps):
    """资金费率：各家都有持仓量时按持仓量加权平均，否则取中位数；同时给出各家的最低和最高。"""
    rates = {ex: p["funding_8h"] for ex, p in perps.items() if p.get("funding_8h") is not None}
    if not rates:
        return None, None, None, 0, ""
    w = {ex: perps[ex].get("oi_usd") for ex in rates}
    if all(w.values()):
        avg, how = sum(rates[ex] * w[ex] for ex in rates) / sum(w.values()), "持仓加权"
    else:
        avg, how = median(rates.values()), "中位数"
    return avg, min(rates.values()), max(rates.values()), len(rates), how


def liquidation_metrics(m, data, state):
    """24h 多单 / 空单爆仓金额：Gate 用交易所公布的小时数据；OKX 用 bot 自己累计的记录。"""
    now_ms = int(time.time() * 1000)
    okx = state.setdefault("okx_liq", {"since": {}, "last_ts": {}, "rows": [], "gap_ts": {}})
    for c in COINS:
        events = data.get("liq_okx", {}).get(c)
        if events is None:
            continue
        # 第一次拿到这个币的数据：从 OKX 返回的最早一条爆仓单开始算覆盖时间
        okx["since"].setdefault(c, min((e[0] for e in events), default=now_ms))
        last = okx["last_ts"].get(c, 0)
        new = [e for e in events if e[0] > last]
        if new:
            okx["last_ts"][c] = max(e[0] for e in new)
        okx["rows"] += [[e[0], c, e[1], round(e[2], 2)] for e in new]
        # 这次拿到的最早一条也比上次记录的新：和上次没有衔接上，中间可能漏记
        if last and events and min(e[0] for e in events) > last:
            okx["gap_ts"][c] = now_ms
    okx["rows"] = [r for r in okx["rows"] if now_ms - r[0] <= 24 * 3600 * 1000]
    notes = []

    for c in COINS:
        k = c.lower()
        long_ = short = 0.0
        src = []
        g = data.get("liq_gate", {}).get(c)
        if g:
            long_ += g["long"]
            short += g["short"]
            src.append("Gate")
        if data.get("liq_okx", {}).get(c) is not None:
            long_ += sum(r[3] for r in okx["rows"] if r[1] == c and r[2] == "long")
            short += sum(r[3] for r in okx["rows"] if r[1] == c and r[2] == "short")
            src.append("OKX")
        m[f"{k}_liq_long"] = long_ if src else None
        m[f"{k}_liq_short"] = short if src else None
        m[f"{k}_liq_src"] = src
        if "OKX" in src:
            hours = max(now_ms - okx["since"][c], 0) / 3.6e6
            if hours < 23:
                notes.append(f"OKX {c} 才累计了 {hours:.0f} 小时")
            elif now_ms - okx["gap_ts"].get(c, 0) <= 24 * 3600 * 1000:
                notes.append(f"OKX {c} 近 24h 爆仓太密集，可能有漏记")
    m["liq_note"] = "，".join(notes)


def bs_gamma(S, K, t, iv):
    """Black-Scholes 的 Gamma（利率按 0）：价格每变动 1 美元，期权 Delta 变多少。"""
    sd = iv * math.sqrt(t)
    d1 = (math.log(S / K) + 0.5 * sd * sd) / sd
    return math.exp(-0.5 * d1 * d1) / math.sqrt(2 * math.pi) / (S * sd)


def dealer_gex(opts, S, now):
    """做市商 Gamma 敞口（美元 / 每 1% 价格波动），常用简化假设：
    看涨期权多是客户卖给做市商（做市商持有 → 正 Gamma），看跌期权多是客户向做市商买（做市商卖出 → 负 Gamma）。"""
    total = 0.0
    for o in opts:
        t = (o["expiry"] - now) / (365 * 86400)
        if o["oi"] <= 0 or not o["iv"] or t <= 0:
            continue
        g = bs_gamma(S, o["strike"], max(t, 1 / 8760), o["iv"] / 100)
        total += (-1 if o["put"] else 1) * o["oi"] * g * S * S * 0.01
    return total


def options_metrics(m, data, state):
    opts = data.get("options")
    for key in ("put_oi", "put_oi_chg7", "put_oi_days", "pcr", "gex", "gamma_flip",
                "gamma_0dte_pct", "cliff_pct", "cliff_date"):
        m[key] = None
    if not opts:
        return
    S = m["btc_price"] or median(o["underlying"] for o in opts)
    if not S:
        return
    now = time.time()

    # 看跌保护：30 天内到期、行权价在现价下方 2%–8% 的看跌期权持仓（BTC）
    m["put_oi"] = sum(o["oi"] for o in opts if o["put"] and 0.92 * S <= o["strike"] <= 0.98 * S
                      and 0 < o["expiry"] - now <= 30 * 86400)
    calls = sum(o["oi"] for o in opts if not o["put"])
    m["pcr"] = sum(o["oi"] for o in opts if o["put"]) / calls if calls else None
    hist = state.get("put_oi_hist", [])
    hist.append([int(now), m["put_oi"]])
    hist = [x for x in hist if now - x[0] <= 8 * 86400]
    state["put_oi_hist"] = hist
    m["put_oi_days"] = (now - hist[0][0]) / 86400
    old = [x for x in hist if now - x[0] >= 6.5 * 86400]
    if old and old[-1][1]:
        m["put_oi_chg7"] = (m["put_oi"] / old[-1][1] - 1) * 100

    # 对冲断崖：下一次月度到期（每月最后一个周五）会带走多少期权 Gamma；以及 24 小时内到期（0DTE）的占比
    gross = {}
    for o in opts:
        t = (o["expiry"] - now) / (365 * 86400)
        if o["oi"] > 0 and o["iv"] and t > 0:
            g = o["oi"] * bs_gamma(S, o["strike"], max(t, 1 / 8760), o["iv"] / 100)
            gross[o["expiry"]] = gross.get(o["expiry"], 0.0) + g
    total = sum(gross.values())
    m["gamma_0dte_pct"] = m["cliff_pct"] = m["cliff_date"] = None
    if total:
        m["gamma_0dte_pct"] = sum(g for e, g in gross.items() if e - now <= 86400) / total * 100
        today = datetime.fromtimestamp(now, timezone.utc).date()
        monthly = last_weekday(today.year, today.month, 4).date()
        if monthly < today:
            nxt = today.replace(day=28) + timedelta(days=4)
            monthly = last_weekday(nxt.year, nxt.month, 4).date()
        m["cliff_date"] = monthly
        m["cliff_pct"] = sum(g for e, g in gross.items()
                             if datetime.fromtimestamp(e, timezone.utc).date() == monthly) / total * 100

    # 做市商 Gamma：现价处的敞口，以及往上下 15% 内找 Gamma 由正转负的“翻转价”
    m["gex"] = dealer_gex(opts, S, now)
    best = None
    prev_x, prev_g = None, None
    for i in range(-30, 31):
        x = S * (1 + i * 0.005)
        g = dealer_gex(opts, x, now)
        if prev_g is not None and (prev_g < 0) != (g < 0):
            flip = (prev_x + x) / 2
            if best is None or abs(flip - S) < abs(best - S):
                best = flip
        prev_x, prev_g = x, g
    m["gamma_flip"] = best


def market_wide_metrics(m, data, state):
    cz = data.get("cz")
    now_ts = int(time.time())
    hist = state.get("cz_oi_history", [])
    old = [x for x in hist if now_ts - x[0] >= 20 * 3600]
    for c in COINS:
        k = c.lower()
        x = cz and cz.get(c)
        oi = x["oi_by_sym"] if x else {}
        m[f"{k}_all_oi"] = sum(oi.values()) if oi else None
        # 24h 持仓变化：和 bot 自己约 24 小时前记下的数比，只比较两次都有数据的合约
        m[f"{k}_all_oi_chg"] = None
        if oi and old:
            base = old[-1][1]
            common = [s for s in oi if base.get(s)]
            if common:
                m[f"{k}_all_oi_chg"] = (sum(oi[s] for s in common) / sum(base[s] for s in common) - 1) * 100
        m[f"{k}_all_liq_long"] = x["liq_long"] if x else None
        m[f"{k}_all_liq_short"] = x["liq_short"] if x else None
        m[f"{k}_all_ex"] = x["exchanges"] if x else []
    if cz:
        hist.append([now_ts, {s: v for c in COINS for s, v in cz[c]["oi_by_sym"].items()}])
    state["cz_oi_history"] = [x for x in hist if now_ts - x[0] <= 48 * 3600]
    cg = data.get("cg") or {}
    for key in ("total_mcap", "mcap_chg24", "btc_dom", "eth_dom", "stable_dom"):
        m[key] = cg.get(key)
    st = data.get("stable")
    m["stable"] = st[-1][1] if st else None
    m["stable_chg7"] = (st[-1][1] / st[-8][1] - 1) * 100 if st and len(st) >= 8 else None
    m["stable_chg30"] = (st[-1][1] / st[-31][1] - 1) * 100 if st and len(st) >= 31 else None
    # 毒性订单流（近似 VPIN）：每小时 |主动买 − 主动卖| / 成交量，按成交量加权，24 小时为一个窗口
    m["flow_imb"] = m["flow_imb_pct"] = m["flow_net"] = None
    fl = data.get("flow")
    if fl and len(fl) >= 48:
        def window(rows):
            v = sum(r[1] for r in rows)
            return (sum(abs(2 * r[2] - r[1]) for r in rows) / v, (2 * sum(r[2] for r in rows) - v) / v) if v else (None, None)
        m["flow_imb"], m["flow_net"] = window(fl[-24:])
        hist = [window(fl[i - 24:i])[0] for i in range(24, len(fl) + 1)]
        m["flow_imb_pct"] = percentile_rank(hist, m["flow_imb"])

    # 新闻与情绪
    nw = data.get("news") or {}
    m["news_age_h"] = data.get("news_age_h")
    m["news_tone"], m["news_tone_base"] = nw.get("tone"), nw.get("tone_base")
    m["crisis_ratio"], m["btc_news_tone"] = nw.get("crisis_ratio"), nw.get("btc_tone")
    cf = data.get("cnn_fg") or {}
    m["cnn_fg"], m["cnn_fg_week"] = cf.get("score"), cf.get("week")
    sf = data.get("sf_sent")
    m["sf_sent"] = sf[-1][1] if sf else None
    m["sf_sent_date"] = sf[-1][0] if sf else None
    m["sf_sent_chg"] = sf[-1][1] - sf[-31][1] if sf and len(sf) > 31 else None
    m["gpr"] = data.get("gpr")

    # 信用利差（美国高收益债 OAS）和政策不确定性指数
    hy = data.get("hy")
    m["hy"] = hy[-1][1] if hy else None
    m["hy_pct"] = percentile_rank([v for _, v in hy[-260:]], m["hy"]) if hy else None
    m["hy_chg5"] = hy[-1][1] - hy[-6][1] if hy and len(hy) >= 6 else None
    epu = data.get("epu")
    m["epu"] = sum(v for _, v in epu[-7:]) / len(epu[-7:]) if epu else None

    # CME：期货溢价 + CFTC 持仓周报
    px = data.get("cme_px")
    m["cme_basis"] = m["cme_name"] = None
    if px and m.get("btc_price") and time.time() - px["time"] <= 3600:
        m["cme_basis"] = (px["price"] / m["btc_price"] - 1) * 100
        m["cme_name"] = px["name"]
    cot = data.get("cme_cot")
    m["cme"] = cot
    m["cme_am_chg"] = None
    if cot and cot["prev"] and cot["prev"]["am"] > 0:
        m["cme_am_chg"] = (cot["now"]["am"] / cot["prev"]["am"] - 1) * 100

    for key in ("dxy", "us10y"):
        rows = data.get(key)
        m[key] = rows[-1][1] if rows else None
        m[key + "_date"] = rows[-1][0][5:] if rows else None
        m[key + "_chg5"] = None
        if rows and len(rows) >= 6:
            m[key + "_chg5"] = ((rows[-1][1] / rows[-6][1] - 1) * 100 if key == "dxy"
                                else rows[-1][1] - rows[-6][1])


def compute_metrics(data, state):
    m = {"errors": data.get("errors", [])}
    spot, perp, daily, dvol = data.get("spot", {}), data.get("perp", {}), data.get("daily"), data.get("dvol")
    for c in COINS:
        k = c.lower()
        s = spot.get(c, {})
        m[f"{k}_price"] = median(x["price"] for x in s.values())
        m[f"{k}_chg24"] = median(x["chg24"] for x in s.values())
        m[f"{k}_spot_src"] = list(s)
        p = perp.get(c, {})
        (m[f"{k}_funding"], m[f"{k}_funding_min"], m[f"{k}_funding_max"],
         m[f"{k}_funding_n"], m[f"{k}_funding_how"]) = funding_summary(p)
        oi = {ex: x["oi_usd"] for ex, x in p.items() if x.get("oi_usd")}
        m[f"{k}_oi_by_ex"] = oi
        m[f"{k}_oi"] = sum(oi.values()) if oi else None
        m[f"{k}_perp_src"] = list(p)

    m["adx"] = m["atr"] = m["atr_pct"] = m["vol_ratio"] = None
    m["vol_low_5d"] = False
    m["rv7"] = m["rv30"] = None
    m["daily_src"] = daily["sources"] if daily else []
    if daily and len(daily["close"]) > 40:
        # combine_daily 已经去掉了今天还没走完的那根 K 线
        h, l, c, v = daily["high"], daily["low"], daily["close"], daily["volume"]
        adx, atr = wilder_adx_atr(h, l, c)
        m["adx"], m["atr"] = adx, atr
        if atr and c[-1]:
            m["atr_pct"] = atr / c[-1] * 100
        if len(v) >= 30:
            avg30 = sum(v[-30:]) / 30
            avg5 = sum(v[-5:]) / 5
            m["vol_ratio"] = avg5 / avg30 if avg30 else None
            # 最近 5 天是不是每一天都低于 30 天平均
            m["vol_low_5d"] = all(x < avg30 for x in v[-5:])
        # 实现波动率（年化 %）：用每天收盘价的对数涨跌幅计算
        rets = [math.log(c[i] / c[i - 1]) for i in range(1, len(c)) if c[i] and c[i - 1]]

        def rv(n):
            x = rets[-n:]
            mu = sum(x) / len(x)
            return math.sqrt(sum((r - mu) ** 2 for r in x) / (len(x) - 1)) * math.sqrt(365) * 100
        if len(rets) >= 30:
            m["rv7"], m["rv30"] = rv(7), rv(30)

    m["dvol"] = dvol and dvol["now"]
    m["dvol_d1"] = dvol and dvol["d1"]
    m["dvol_d7"] = dvol and dvol["d7"]
    m["dvol_jump"] = ((dvol["now"] / dvol["d1"] - 1) * 100) if dvol and dvol["d1"] else None

    m["fng"] = data.get("fng")
    liquidation_metrics(m, data, state)
    market_wide_metrics(m, data, state)
    options_metrics(m, data, state)

    vs = data.get("vix_series")
    m["vix"] = vs[-1] if vs else None
    m["vix_calm_days"] = None  # VIX 连续多少个交易日低于 20
    if vs:
        n = 0
        for v in reversed(vs):
            if v >= T["vix_quiet"]:
                break
            n += 1
        m["vix_calm_days"] = n
    m["vix_pct"] = percentile_rank(vs, m["vix"]) if vs else None
    xs = data.get("vxn_series")
    m["vxn"] = xs[-1] if xs else None
    m["vxn_pct"] = percentile_rank(xs, m["vxn"]) if xs else None
    ms = data.get("move_series")
    m["move"] = ms[-1] if ms else None
    m["move_pct"] = percentile_rank(ms, m["move"]) if ms else None
    ns = data.get("ndx_series")
    m["ndx_chg"] = ((ns[-1] / ns[-2] - 1) * 100) if ns and len(ns) >= 2 else None

    # BTC 合约持仓量 24h 变化（用自己保存的各家历史；只比较两次都拿到数据的交易所，避免家数变化造成假涨跌）
    m["oi_chg"] = None
    now_oi = m["btc_oi_by_ex"]
    hist = [x for x in state.get("oi_history", []) if isinstance(x[1], dict)]
    now_ts = int(time.time())
    if now_oi:
        old = [x for x in hist if now_ts - x[0] >= 20 * 3600]
        if old:
            base = old[-1][1]
            common = [ex for ex in now_oi if base.get(ex)]
            if common:
                m["oi_chg"] = (sum(now_oi[ex] for ex in common) / sum(base[ex] for ex in common) - 1) * 100
        hist.append([now_ts, now_oi])
    state["oi_history"] = [x for x in hist if now_ts - x[0] <= 48 * 3600]
    return m


# ============================================================
# 判断市场天气
# ============================================================
def gte(x, t):
    return x is not None and x >= t


def lt(x, t):
    return x is not None and x < t


def classify(m):
    r = []
    dvol, chg, vix, adx = m["dvol"], m["btc_chg24"], m["vix"], m["adx"]

    # 1. 崩溃
    if gte(dvol, T["dvol_crash"]):
        r.append(f"币市恐慌度 DVOL 高达 {dvol:.0f}")
    if chg is not None and chg <= T["btc_crash_drop"]:
        r.append(f"BTC 24 小时下跌 {abs(chg):.1f}%")
    if gte(vix, T["vix_crash"]):
        r.append(f"美股恐慌指数 VIX 高达 {vix:.0f}")
    if r:
        return "crash", r

    # 2. 爆发
    if gte(dvol, T["dvol_burst"]):
        r.append(f"币市恐慌度 DVOL 升到 {dvol:.0f}")
    if chg is not None and abs(chg) >= T["btc_burst_move"]:
        r.append(f"BTC 24 小时{'上涨' if chg > 0 else '下跌'} {abs(chg):.1f}%")
    if gte(m["dvol_jump"], T["dvol_jump_pct"]):
        r.append(f"DVOL 24 小时内跳升 {m['dvol_jump']:.0f}%")
    if gte(vix, T["vix_burst"]):
        r.append(f"美股恐慌指数 VIX 升到 {vix:.0f}")
    if r:
        return "burst", r

    # 3. 绞杀：波动不小 + 没有趋势
    if lt(adx, T["adx_no_trend"]) and (gte(dvol, T["dvol_chop"]) or gte(m["atr_pct"], 4)):
        r.append(f"趋势强度 ADX 只有 {adx:.0f}（没方向）")
        if gte(dvol, T["dvol_chop"]):
            r.append(f"但 DVOL 偏高（{dvol:.0f}）")
        else:
            r.append(f"但每天平均波动有 {m['atr_pct']:.1f}%")
        return "chop", r

    # 4. 衰退：从高位回落
    if gte(m["dvol_d7"], 60) and dvol is not None and dvol < m["dvol_d7"] * 0.85:
        r.append(f"DVOL 从一周前的 {m['dvol_d7']:.0f} 回落到 {dvol:.0f}")
        return "fading", r

    # 5. 静默
    vix_calm = m["vix_calm_days"] is None or m["vix_calm_days"] >= T["vix_quiet_days"]
    if (lt(dvol, T["dvol_quiet"]) and lt(adx, T["adx_quiet_max"])
            and lt(m["vol_ratio"], T["volume_quiet_ratio"]) and m["vol_low_5d"] and vix_calm):
        r.append(f"DVOL 低（{dvol:.0f}）")
        r.append(f"成交量连续 5 天低于平时，只有平时的 {m['vol_ratio'] * 100:.0f}%")
        r.append(f"ADX {adx:.0f}，没有明显行情")
        if m["vix_calm_days"] is not None:
            r.append(f"VIX 已连续 {m['vix_calm_days']} 个交易日低于 {T['vix_quiet']}")
        return "quiet", r

    # 6. 其他情况：平稳
    if dvol is not None:
        r.append(f"DVOL {dvol:.0f}，处于正常范围")
    if adx is not None:
        r.append(f"ADX {adx:.0f}" + ("，有一定趋势" if adx >= 25 else ""))
    return "normal", r


# ============================================================
# 操控 / 风险雷达
# ============================================================
def nth_weekday(year, month, weekday, n):
    d = datetime(year, month, 1)
    while d.weekday() != weekday:
        d += timedelta(days=1)
    return d + timedelta(weeks=n - 1)


def last_weekday(year, month, weekday):
    d = datetime(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)
    while d.weekday() != weekday:
        d -= timedelta(days=1)
    return d


def expiry_warnings(today):
    out = []
    y, mo = today.year, today.month
    us = nth_weekday(y, mo, 4, 3).date()        # 美股月度期权：每月第三个周五
    crypto = last_weekday(y, mo, 4).date()      # 币圈期权（Deribit）：每月最后一个周五
    for d, name in ((us, "美股月度期权"), (crypto, "币圈月度期权")):
        diff = (d - today).days
        if 0 <= diff <= 2:
            when = "今天" if diff == 0 else f"{diff} 天后（{d.month}月{d.day}日）"
            out.append(f"📅 {when}是{name}到期日，前后几天做市商对冲变化大，波动容易放大。")
        elif -9 <= diff < 0:
            # 对冲断崖：到期后约 7 个交易日，做市商的对冲盘大幅减少，减震效果变弱
            out.append(f"🧱 对冲断崖期：{name}在 {d.month}月{d.day}日到期（{-diff} 天前），之后约 7 个交易日做市商的"
                       "“高抛低吸”减震变弱，一有外部冲击波动就容易被放大。")
    return out


def radar(m, today):
    w = []
    f = m["btc_funding"]
    if gte(f, T["funding_hot"]):
        w.append(f"🔥 资金费率偏高（{f * 100:.3f}%/8小时）：做多的人太挤，容易向下插针扫多头止损。")
    elif f is not None and f <= T["funding_cold"]:
        w.append(f"🧊 资金费率为负（{f * 100:.3f}%/8小时）：做空的人太挤，容易突然暴拉扫空头（轧空）。")
    fng = m["fng"]
    if gte(fng, T["fng_greed"]):
        w.append(f"🤑 恐惧贪婪指数 {fng}（极度贪婪）：大家都很兴奋，追高要特别小心。")
    elif fng is not None and fng <= T["fng_fear"]:
        w.append(f"😱 恐惧贪婪指数 {fng}（极度恐慌）：情绪极端，常接近转折，但别一次性重仓抄底。")
    oi_chg = m["btc_all_oi_chg"] if m["btc_all_oi_chg"] is not None else m["oi_chg"]
    if gte(oi_chg, T["oi_jump_pct"]) and m["btc_chg24"] is not None and abs(m["btc_chg24"]) < 2:
        w.append(f"⚡ 持仓量 24 小时增加 {oi_chg:.0f}% 但价格几乎没动：大量新杠杆在堆积，后面容易出现大波动。")
    if gte(m["put_oi_chg7"], T["put_oi_jump_pct"]):
        w.append(f"🛡 BTC 现价下方 2–8% 的看跌期权持仓 7 天增加 {m['put_oi_chg7']:.0f}%：大资金在悄悄买下跌保护，要提高警惕。")
    if m["gex"] is not None and m["gex"] < 0:
        w.append("⚙️ 做市商处于负 Gamma（放大器档，估算）：涨跌容易被对冲盘放大，固定窄止损容易被瞬间扫掉，"
                 "宜减仓、按波动放宽止损或观望。")
    if (m["cliff_pct"] is not None and m["cliff_pct"] >= T["cliff_gamma_pct"] and m["cliff_date"]
            and 0 <= (m["cliff_date"] - datetime.now(timezone.utc).date()).days <= 3):
        d = m["cliff_date"]
        w.append(f"🧱 {d.month}月{d.day}日的 BTC 月度期权到期会带走约 {m['cliff_pct']:.0f}% 的期权 Gamma："
                 "到期后做市商的减震作用会明显变弱。")
    if (gte(m["rv7"], T["rv_scale_min"]) and m["rv30"]
            and m["rv7"] >= m["rv30"] * T["rv_scale_ratio"]):
        w.append(f"🌀 波动率在放大：BTC 7 天实现波动率 {m['rv7']:.0f}%，是 30 天（{m['rv30']:.0f}%）的 "
                 f"{m['rv7'] / m['rv30']:.1f} 倍。量化 / CTA 的风控模型可能被迫机械减仓，跌势容易自我加速。")
    if gte(m["flow_imb_pct"], T["flow_toxic_pct"]):
        side = "卖" if (m["flow_net"] or 0) < 0 else "买"
        w.append(f"☣️ 订单流异常单边（近似毒性订单流）：近 24h 主动{side}单明显占优，失衡度是近 7 天最高的一档。"
                 "做市商可能撤单、价差变大，留意插针。")
    if gte(m["hy_chg5"], T["hy_spread_jump"]):
        w.append(f"💳 美国高收益债信用利差 5 个交易日走阔 {m['hy_chg5']:.2f} 个百分点：信用市场开始紧张，"
                 "风险通常会接着传到股市。")
    if gte(m["epu"], T["epu_crisis"]):
        w.append(f"🏛 美国政策不确定性指数 {m['epu']:.0f}（7 日均值）：已到严重危机级，市场可能长时间高波动或受压。")
    elif gte(m["epu"], T["epu_warn"]):
        w.append(f"🏛 美国政策不确定性指数 {m['epu']:.0f}（7 日均值）：明显紧张，新消息容易引发大波动。")
    if m["cme_am_chg"] is not None and abs(m["cme_am_chg"]) >= T["cme_am_chg_pct"]:
        act = "加仓" if m["cme_am_chg"] > 0 else "减仓"
        w.append(f"🏦 CME 资产管理机构净多仓一周{act} {abs(m['cme_am_chg']):.0f}%（CFTC 周报）：传统机构在明显{act}比特币。")
    eq_name, eq_pct = ("纳指 VXN", m["vxn_pct"]) if m["vxn_pct"] is not None else ("美股 VIX", m["vix_pct"])
    credit_hot = gte(m["hy_pct"], 80)
    if (gte(m["move_pct"], T["move_high_pct"]) or credit_hot) and eq_pct is not None and eq_pct <= T["vix_low_pct"]:
        lead = []
        if gte(m["move_pct"], T["move_high_pct"]):
            lead.append(f"债市恐慌 MOVE 在一年 {m['move_pct']:.0f}% 分位")
        if credit_hot:
            lead.append(f"信用利差在一年 {m['hy_pct']:.0f}% 分位")
        w.append(f"⏳ 股债时差：{'、'.join(lead)}，{eq_name} 只在 {eq_pct:.0f}% 分位。"
                 "债市、信用市场已经紧张，股市还平静，风险可能还没完全反映出来（传导顺序：债市 → 信用 → 个股期权 → 指数）。")
    if m["vix"] is not None and T["vix_warn"] <= m["vix"] < T["vix_burst"]:
        w.append(f"🇺🇸 美股恐慌指数 VIX 升到 {m['vix']:.0f}：美股紧张时，币市常被一起拖累。")
    if gte(m["dvol_jump"], 10) and not gte(m["dvol_jump"], T["dvol_jump_pct"]):
        w.append(f"📈 DVOL 24 小时上升 {m['dvol_jump']:.0f}%：市场正在变紧张。")
    if m["btc_all_liq_long"] is not None:  # 优先用 6 大交易所的汇总数据
        ln = m["btc_all_liq_long"] + (m["eth_all_liq_long"] or 0)
        sh = m["btc_all_liq_short"] + (m["eth_all_liq_short"] or 0)
        big, where = T["liq_big_usd_all"], f"{len(m['btc_all_ex'])} 大交易所合计"
    elif m["btc_liq_long"] is not None:
        ln = m["btc_liq_long"] + (m["eth_liq_long"] or 0)
        sh = m["btc_liq_short"] + (m["eth_liq_short"] or 0)
        big, where = T["liq_big_usd"], "只含 OKX、Gate 两家"
    else:
        ln = sh = 0.0
        big, where = float("inf"), ""
    tot = ln + sh
    if tot >= big:
        side = ""
        if ln / tot * 100 >= T["liq_one_side_pct"]:
            side = f"其中多单占 {ln / tot * 100:.0f}%，做多的人被集中清算。"
        elif sh / tot * 100 >= T["liq_one_side_pct"]:
            side = f"其中空单占 {sh / tot * 100:.0f}%，做空的人被集中清算（轧空）。"
        w.append(f"💥 24h BTC+ETH 爆仓 {fmt_musd(tot)}（{where}）：大规模强平，杠杆正在被清洗。" + side)
    if m["stable_chg7"] is not None:
        if m["stable_chg7"] <= T["stable_out_pct"]:
            w.append(f"🏦 稳定币总量 7 天减少 {abs(m['stable_chg7']):.1f}%：场外资金在离场，市场的“弹药”变少。")
        elif m["stable_chg7"] >= T["stable_in_pct"]:
            w.append(f"🏦 稳定币总量 7 天增加 {m['stable_chg7']:.1f}%：有新资金在进场。")
    if gte(m["dxy_chg5"], T["dxy_jump_pct"]):
        w.append(f"💵 美元指数 5 个交易日上涨 {m['dxy_chg5']:.1f}%：美元走强时，币和美股这类风险资产常受压制。")
    if gte(m["us10y_chg5"], T["us10y_jump"]):
        w.append(f"📉 10 年期美债收益率 5 个交易日上升 {m['us10y_chg5']:.2f} 个百分点：利率快速上升，对风险资产不友好。")
    w.extend(expiry_warnings(today))
    return w


# ============================================================
# 文字描述（把数字翻译成白话）
# ============================================================
def lvl_dvol(x):
    if x is None: return ""
    return "低" if x < 45 else "正常" if x < 60 else "偏高" if x < 75 else "很高"


def lvl_vix(x):
    if x is None: return ""
    return "低" if x < 15 else "正常" if x < 20 else "偏高" if x < 30 else "很高"


def lvl_adx(x):
    if x is None: return ""
    return "没有趋势" if x < 20 else "趋势较弱" if x < 25 else "有趋势" if x < 40 else "强趋势"


def lvl_fng(x):
    if x is None: return ""
    return ("极度恐慌" if x <= 20 else "恐慌" if x < 45 else "中性" if x <= 55
            else "贪婪" if x < 80 else "极度贪婪")


def lvl_funding(x):
    if x is None: return ""
    if x >= T["funding_hot"]: return "多头过挤"
    if x <= T["funding_cold"]: return "空头过挤"
    return "正常"


def item(L, name, value, what, now):
    """一个指标：名字和读数，然后用两句白话说明“它是什么”和“现在意味着什么”。"""
    L.append(f"▫️ <b>{name}</b>：{value}\n   📖 {what}\n   👉 {now}")


def chg_word(x, flat=1.0, big=3.0):
    if x is None:
        return ""
    if abs(x) < flat:
        return "基本没动"
    return ("小幅" if abs(x) < big else "明显") + ("上涨" if x > 0 else "下跌")


def readings_block(m):
    S = []  # 每一段是一个板块

    # ---------- 价格与波动 ----------
    L = ["📊 <b>价格与波动</b>"]
    for c in COINS:
        k = c.lower()
        if m[k + "_price"] is None:
            continue
        chg = m[k + "_chg24"]
        now = f"24 小时{chg_word(chg, 1, 4)}" if chg is not None else "暂时拿不到涨跌"
        if chg is not None and abs(chg) >= T["btc_burst_move"]:
            now += "，属于大波动，要提高警惕"
        item(L, f"{c} 价格", f"{fmt_money(m[k + '_price'])}（24h {fmt_pct(chg)}）",
             (f"{len(m[k + '_spot_src'])} 家大交易所现货价格的中位数，避免单一交易所失真" if len(m[k + "_spot_src"]) > 1
              else "这次只拿到 1 家交易所的现货价格"), now)
    if m["dvol"] is not None:
        d = m["dvol"]
        now = {"低": "市场很平静，没人急着买“保险”", "正常": "波动预期正常",
               "偏高": "市场开始紧张，大家在花钱买保护", "很高": "市场很恐慌，随时可能大幅波动"}[lvl_dvol(d)]
        if gte(m["dvol_jump"], 10):
            now += f"；而且 24 小时内升了 {m['dvol_jump']:.0f}%，紧张在升温"
        item(L, "币市恐慌度 DVOL", f"{d:.0f}（{lvl_dvol(d)}）",
             "BTC 期权价格反推出的“未来 30 天预期波动”，相当于币圈的 VIX。45 以下平静，70 以上紧张", now)
    if m["rv7"] is not None:
        r = m["rv7"] / m["rv30"] if m["rv30"] else 1
        now = ("最近一周的实际波动明显比一个月平均大，波动在放大" if r >= 1.3 else
               "最近一周比一个月平均安静，波动在收敛" if r <= 0.75 else "最近一周和一个月平均差不多，波动平稳")
        item(L, "BTC 实际波动率", f"7 天 {m['rv7']:.0f}% / 30 天 {m['rv30']:.0f}%（年化）",
             "价格过去真实涨跌的剧烈程度。短期突然比长期大很多时，量化基金的风控会逼它们减仓", now)
    if m["adx"] is not None:
        a = m["adx"]
        now = ("没有方向，来回震荡，追涨杀跌容易两头挨打" if a < 20 else
               "趋势很弱，方向还不明确" if a < 25 else "有一定趋势，顺着方向做胜算较高" if a < 40 else
               "趋势很强，别逆着做")
        item(L, "趋势强度 ADX", f"{a:.0f}（{lvl_adx(a)}）", "只看“有没有方向”，不看涨跌。20 以下算没趋势，25 以上算有趋势", now)
    if m["vol_ratio"] is not None:
        v = m["vol_ratio"]
        now = ("成交冷清，大家在观望" + ("，而且已经连续 5 天偏低" if m["vol_low_5d"] else "") if v < 0.9 else
               "成交正常" if v < 1.3 else "明显放量，有资金在大举进出")
        item(L, "现货成交量", f"近 5 天是平时的 {v * 100:.0f}%",
             f"{len(m['daily_src'])} 家大交易所的现货成交量合计，和过去 30 天平均比较", now)
    if m["fng"] is not None:
        f = m["fng"]
        now = ("极度恐慌：情绪极端，常接近转折，但别一次性重仓抄底" if f <= 20 else
               "偏恐慌：大家比较悲观" if f < 45 else "情绪中性" if f <= 55 else
               "偏贪婪：大家比较乐观，追高要小心" if f < 80 else "极度贪婪：过热了，最容易在这时候被套")
        item(L, "币圈恐惧贪婪指数", f"{f}（{lvl_fng(f)}）", "综合波动、成交、社交热度等算出的币圈情绪，0 最恐慌、100 最贪婪", now)
    S.append("\n".join(L))

    # ---------- 杠杆与爆仓 ----------
    L = ["🧨 <b>杠杆与爆仓</b>"]
    for c in COINS:
        k = c.lower()
        fr = m[k + "_funding"]
        if fr is None:
            continue
        lv = lvl_funding(fr)
        now = {"正常": "多空力量比较平衡",
               "多头过挤": "做多的人太多，市场反而容易往下插针，扫掉多头的止损",
               "空头过挤": "做空的人太多，容易突然暴拉“轧空”"}[lv]
        if lv == "正常":
            now += "，" + ("略偏多头" if fr > 0.0001 else "略偏空头" if fr < 0 else "没有明显拥挤")
        item(L, f"{c} 资金费率", f"{fr * 100:.4f}%/8h（{lv}）",
             "永续合约里多空双方互相支付的费用，正数是多头付钱给空头。"
             + (f"{m[k + '_funding_n']} 家交易所{m[k + '_funding_how']}，各家从 {m[k + '_funding_min'] * 100:.4f}% "
                f"到 {m[k + '_funding_max'] * 100:.4f}%" if m[k + "_funding_n"] > 1 else "这次只拿到 1 家交易所"), now)
    oi_chg = m["btc_all_oi_chg"] if m["btc_all_oi_chg"] is not None else m["oi_chg"]
    oi_val = m["btc_all_oi"] if m["btc_all_oi"] is not None else m["btc_oi"]
    if oi_val is not None:
        src = (f"{len(m['btc_all_ex'])} 大交易所" if m["btc_all_oi"] is not None else f"{len(m['btc_oi_by_ex'])} 家")
        if oi_chg is None:
            now = "24 小时变化还在累计（bot 每小时记录一次）"
        elif oi_chg >= T["oi_jump_pct"] and m["btc_chg24"] is not None and abs(m["btc_chg24"]) < 2:
            now = "持仓大增但价格没怎么动：新杠杆在堆积，后面容易出现大波动"
        elif oi_chg >= 3:
            now = "有新资金带着杠杆进场" + ("，跟着价格一起涨，趋势有人推" if (m["btc_chg24"] or 0) > 1 else "")
        elif oi_chg <= -3:
            now = "杠杆在退场（主动平仓或被爆仓），市场在“去杠杆”"
        else:
            now = "杠杆水平变化不大"
        item(L, "BTC 合约持仓", f"{fmt_busd(oi_val)}（{src}" + (f"，24h {fmt_pct(oi_chg)}）" if oi_chg is not None else "）"),
             "所有还没平仓的 BTC 合约总价值，代表市场里杠杆有多少", now)
    for c in COINS:
        k = c.lower()
        ln, sh = m[k + "_all_liq_long"], m[k + "_all_liq_short"]
        where = f"{len(m[k + '_all_ex'])} 大交易所"
        if ln is None:
            ln, sh, where = m[k + "_liq_long"], m[k + "_liq_short"], "+".join(m[k + "_liq_src"]) or "—"
        if ln is None:
            continue
        tot = ln + sh
        now = ("几乎没有爆仓，市场很安静" if tot < 5e6 else
               "多单爆得多：价格下跌时做多的人被强制平仓" if ln > sh * 2 else
               "空单爆得多：价格上涨时做空的人被强制平仓" if sh > ln * 2 else "多空两边都有爆仓，来回扫")
        item(L, f"{c} 24h 爆仓", f"多单 {fmt_musd(ln)} / 空单 {fmt_musd(sh)}（{where}）",
             "杠杆仓位亏到保证金不够、被交易所强制平仓的金额", now)
    if m["btc_liq_long"] is not None and m["liq_note"]:
        L.append(f"<i>自采爆仓（OKX+Gate）：{m['liq_note']}</i>")
    if m["flow_imb"] is not None:
        p = m["flow_imb_pct"]
        side = "卖" if m["flow_net"] < 0 else "买"
        now = (f"主动{side}单异常集中，做市商可能撤单，留意插针" if p >= T["flow_toxic_pct"] else
               f"略偏主动{side}，还在正常范围" if p >= 70 else "买卖比较均衡，正常")
        item(L, "订单流失衡（近似毒性订单流）", f"{m['flow_imb']:.2f}（近 7 天 {p:.0f}% 分位）",
             "主动买单和主动卖单差多少。越单边，越像有“知情资金”在一边倒地砸或扫（用 Binance BTC 每小时数据近似）", now)
    S.append("\n".join(L))

    # ---------- 期权与做市商 ----------
    if m["gex"] is not None:
        L = ["🎛 <b>期权与做市商</b>（BTC，Deribit）"]
        pos = m["gex"] >= 0
        flip = f"，翻转价约 {fmt_money(m['gamma_flip'])}" if m["gamma_flip"] else ""
        now = ("减震器档：做市商会“跌了买、涨了卖”，价格容易被拉回区间" if pos else
               "放大器档：做市商会“跌了卖、涨了买”，涨跌容易被放大")
        if m["gamma_flip"] and m["btc_price"]:
            gap = (m["btc_price"] / m["gamma_flip"] - 1) * 100
            if abs(gap) <= 3:
                now += (f"。离翻转价只有 {abs(gap):.1f}%，{'跌破' if gap > 0 else '涨过'}后会切到"
                        + ("放大器档" if pos else "减震器档"))
        item(L, "做市商 Gamma（估算）", ("正 Gamma" if pos else "负 Gamma") + f"（每 1% 波动对冲约 {fmt_musd(abs(m['gex']))}{flip}）",
             "做市商为了对冲期权风险，会被迫跟着价格买卖。正 Gamma 时他们帮市场“减震”，负 Gamma 时帮市场“加速”", now)
        if m["put_oi"] is not None:
            c7 = m["put_oi_chg7"]
            now = (f"7 天变化还在累计（已有 {m['put_oi_days']:.0f} 天）" if c7 is None else
                   "一周暴增，大资金在花钱买下跌保险，要提高警惕" if c7 >= T["put_oi_jump_pct"] else
                   "一周有增加，防守意愿在上升" if c7 >= 15 else "一周减少，大家没那么担心下跌" if c7 <= -15 else "变化不大")
            item(L, "下跌保护", f"{m['put_oi']:,.0f} BTC" + (f"（7 天 {fmt_pct(c7, 0)}）" if c7 is not None else ""),
                 "30 天内到期、行权价在现价下方 2–8% 的看跌期权持仓，相当于大家买的“下跌保险”", now)
        if m["pcr"] is not None:
            p = m["pcr"]
            now = ("看涨的仓位明显更多，市场偏乐观" if p < 0.7 else "看涨看跌比较平衡" if p < 1 else "看跌仓位更多，市场偏防守")
            item(L, "看跌 / 看涨持仓比", f"{p:.2f}", "所有看跌期权持仓 ÷ 看涨期权持仓。低于 0.7 偏乐观，高于 1 偏防守", now)
        if m["cliff_pct"] is not None:
            d = m["cliff_date"]
            days = (d - datetime.now(timezone.utc).date()).days
            now = (f"{days} 天后这次到期分量很重，到期后减震会明显变弱" if m["cliff_pct"] >= T["cliff_gamma_pct"] and days <= 7 else
                   "这次到期分量较重，到期前后留意波动" if m["cliff_pct"] >= T["cliff_gamma_pct"] else "到期影响有限")
            if m["gamma_0dte_pct"] >= 25:
                now += f"；当天到期的期权占 {m['gamma_0dte_pct']:.0f}%，盘中容易急拉急跌"
            item(L, "对冲断崖", f"{d.month}月{d.day}日月度到期带走 {m['cliff_pct']:.0f}% 的 Gamma；0DTE 占 {m['gamma_0dte_pct']:.0f}%",
                 "期权到期后，做市商需要对冲的仓位一下子变少，“减震器”变弱，之后一周左右波动容易被放大", now)
        S.append("\n".join(L))

    # ---------- CME ----------
    cme = m.get("cme")
    if cme or m.get("cme_basis") is not None:
        L = ["🏛 <b>CME 机构市场</b>（芝商所，受美国监管）"]
        if m["cme_basis"] is not None:
            b = m["cme_basis"]
            now = ("溢价偏高，机构做多意愿强" if b >= 1 else "小幅溢价，正常" if b >= 0 else "贴水（比现货便宜），机构偏谨慎甚至偏空")
            item(L, "CME 期货溢价", f"{fmt_pct(b, 2)}（{esc(m['cme_name'])}）",
                 "CME 比特币期货比现货贵多少。机构越想做多，溢价越高", now)
        if cme:
            n, p = cme["now"], cme["prev"]
            am_chg = n["am"] - p["am"] if p else None
            now = ("还没有上周数据可比较" if am_chg is None else
                   "传统机构在明显加仓" if gte(m["cme_am_chg"], T["cme_am_chg_pct"]) else
                   "传统机构在明显减仓" if m["cme_am_chg"] is not None and m["cme_am_chg"] <= -T["cme_am_chg_pct"] else
                   f"传统机构一周{'小幅加仓' if am_chg >= 0 else '小幅减仓'}，变化不大")
            item(L, "资产管理机构（养老金、基金等）", f"{'净多' if n['am'] >= 0 else '净空'} {abs(n['am']):,.0f} BTC",
                 f"CFTC 每周公布的 CME 持仓（数据日 {cme['date'][5:]}）里，传统机构的多空。总持仓 {n['oi']:,.0f} BTC", now)
            item(L, "杠杆基金（对冲基金）", f"{'净多' if n['lev'] >= 0 else '净空'} {abs(n['lev']):,.0f} BTC",
                 "对冲基金的多空。它们的净空大多是“买现货 / ETF、空期货”赚价差的套利",
                 "净空不一定代表看空，主要反映套利规模" if n["lev"] < 0 else "对冲基金整体偏多")
        S.append("\n".join(L))

    # ---------- 新闻与情绪 ----------
    L = ["📰 <b>新闻与情绪</b>（多个来源对照）"]
    if gte(m.get("news_age_h"), 1.5):
        L.append(f"<i>GDELT 这次限流没取到，下面用的是 {m['news_age_h']:.0f} 小时前的数据</i>")
    if m.get("cnn_fg") is not None:
        f = m["cnn_fg"]
        now = ("美股极度恐慌，资金在避险" if f <= 25 else "美股偏恐慌" if f < 45 else "美股情绪中性" if f <= 55 else
               "美股偏贪婪" if f < 75 else "美股极度贪婪，过热")
        if m.get("cnn_fg_week") is not None:
            now += f"（一周前 {m['cnn_fg_week']:.0f}）"
        item(L, "美股恐惧贪婪（CNN）", f"{f:.0f}", "CNN 用 7 个美股指标算的情绪，0 最恐慌、100 最贪婪", now)
    if m.get("news_tone") is not None:
        t, base = m["news_tone"], m["news_tone_base"]
        diff = t - base
        now = ("新闻比平时明显更负面" if diff <= -0.5 else "新闻比平时更正面" if diff >= 0.5 else "新闻语气和平时差不多")
        item(L, "全球经济新闻语气（GDELT）", f"{t:+.2f}（近 14 天平均 {base:+.2f}）",
             "全球英文新闻里谈经济、市场的文章，平均语气是偏正面还是偏负面（负数 = 偏负面）", now)
    if m.get("crisis_ratio") is not None:
        r = m["crisis_ratio"]
        now = ("危机类词汇突然暴增（超过平时 1.5 倍），出现了市场没见过的新状况，算法和资金容易先撤退" if r >= 1.5 else
               "危机类报道比平时多一些" if r >= 1.2 else "危机类报道在正常水平")
        item(L, "危机新闻热度（GDELT）", f"平时的 {r:.1f} 倍",
             "“衰退、危机、崩盘、违约”这类词在新闻里出现的频率，和过去 30 天平均比较", now)
    if m.get("btc_news_tone") is not None:
        t = m["btc_news_tone"]
        now = "比特币新闻偏负面" if t <= -1 else "比特币新闻偏正面" if t >= 1 else "比特币新闻语气中性"
        item(L, "比特币新闻语气（GDELT）", f"{t:+.2f}（近 24 小时）", "全球英文新闻里提到比特币的文章的平均语气", now)
    if m.get("sf_sent") is not None:
        v = m["sf_sent"]
        now = ("经济新闻情绪明显偏悲观" if v <= -0.2 else "经济新闻情绪偏悲观" if v < 0 else "经济新闻情绪偏乐观")
        if m.get("sf_sent_chg") is not None:
            now += f"，比一个月前{'改善' if m['sf_sent_chg'] > 0 else '变差'}"
        item(L, "美国经济新闻情绪（旧金山联储）", f"{v:+.2f}（{m['sf_sent_date']}）",
             "旧金山联储研究员用经济新闻文章算的每日情绪，0 以上偏乐观、0 以下偏悲观", now)
    if m["epu"] is not None:
        e = m["epu"]
        now = ("严重危机级，市场可能长时间高波动或受压" if e >= T["epu_crisis"] else
               "明显紧张，新消息容易引发大波动" if e >= T["epu_warn"] else "政策面比较平静")
        item(L, "美国政策不确定性指数", f"{e:.0f}（7 日均值）",
             "学术界统计美国报纸里“政策 + 不确定”相关报道的多少。约 100 是长期平均，超过 200 紧张，超过 300 危机", now)
    if m.get("gpr") is not None:
        g = m["gpr"]
        now = ("地缘冲突风险很高" if g["pct"] >= 90 else "地缘风险偏高" if g["pct"] >= 70 else "地缘风险正常")
        item(L, "地缘政治风险指数", f"{g['now']:.0f}（7 日均值，一年内 {g['pct']:.0f}% 分位）",
             "美联储经济学家统计战争、恐袭、军事冲突类报道的多少", now)
    if len(L) > 1:
        S.append("\n".join(L))

    # ---------- 宏观与资金 ----------
    L = ["🌍 <b>宏观与资金</b>"]
    if m["total_mcap"] is not None:
        d = m["btc_dom"]
        now = ("资金集中在 BTC，偏防守，山寨币相对弱" if d >= 58 else "资金在往山寨币扩散，风险偏好较高" if d < 50 else "BTC 和山寨币比较均衡")
        item(L, "加密总市值", f"{fmt_busd(m['total_mcap'])}（24h {fmt_pct(m['mcap_chg24'])}）· BTC 占 {d:.1f}%",
             "所有加密货币加起来值多少；BTC 占比代表资金是集中在龙头还是分散到小币", now)
    if m["stable"] is not None:
        c7 = m["stable_chg7"]
        now = ("稳定币在减少，场外资金在离场，市场“弹药”变少" if gte(-(c7 or 0), 0.5) else
               "稳定币在增加，有新资金准备进场" if gte(c7, 0.5) else "稳定币总量平稳，资金没有明显进出")
        item(L, "稳定币总量", f"{fmt_busd(m['stable'])}（7 天 {fmt_pct(c7)}）",
             "链上所有美元稳定币的总量，是准备买币的“弹药”", now)
    if m["vix"] is not None:
        v = m["vix"]
        now = ("美股很平静" if v < 15 else "美股正常" if v < 20 else "美股开始紧张" if v < 25 else
               "美股明显紧张，币市常被拖累" if v < 30 else "美股恐慌" if v < 40 else "美股极度恐慌，接近崩盘级")
        if m["vix_calm_days"]:
            now += f"（已连续 {m['vix_calm_days']} 个交易日低于 {T['vix_quiet']}）"
        item(L, "美股恐慌 VIX", f"{v:.1f}", "标普 500 期权反推的未来 30 天预期波动。20 以下平静，30 以上恐慌，40 以上崩盘级", now)
    if m["vxn"] is not None:
        p = m["vxn_pct"]
        now = "纳指很紧张" if p >= 80 else "纳指偏紧张" if p >= 60 else "纳指平静"
        item(L, "纳指恐慌 VXN", f"{m['vxn']:.1f}（一年内 {p:.0f}% 分位）", "纳斯达克 100 的“VIX”，和比特币走势关系更密切", now)
    if m["ndx_chg"] is not None:
        item(L, "纳斯达克 100", f"上个交易日 {fmt_pct(m['ndx_chg'])}", "美国科技股指数，币市常跟着它走",
             f"美股科技股{chg_word(m['ndx_chg'], 0.5, 1.5)}")
    if m["move_pct"] is not None:
        p = m["move_pct"]
        now = "债市很紧张（保护很贵）" if p >= 90 else "债市偏紧张" if p >= 70 else "债市平静"
        item(L, "债市恐慌 MOVE", f"一年内 {p:.0f}% 分位", "美债期权的预期波动，相当于债市的 VIX。风险常常先在债市出现", now)
    if m["hy"] is not None:
        p = m["hy_pct"]
        now = ("信用市场明显紧张，借钱变贵" if p >= 90 else "信用市场偏紧" if p >= 70 else "信用市场平稳")
        if gte(m["hy_chg5"], 0.2):
            now += "，而且最近在走阔"
        item(L, "美国高收益债信用利差", f"{m['hy']:.2f}%（一年内 {p:.0f}% 分位）",
             "垃圾债比国债多要的利息。越高说明大家越担心企业还不起钱", now)
    if m["dxy"] is not None:
        c5 = m["dxy_chg5"]
        now = ("美元在走强，通常压制币和美股" if gte(c5, 0.8) else "美元在走弱，对风险资产有利" if c5 is not None and c5 <= -0.8
               else "美元平稳")
        item(L, "美元指数（广义）", f"{m['dxy']:.1f}（5 日 {fmt_pct(c5)}，{m['dxy_date']}）",
             "美元对一篮子货币的强弱。美元越强，全球资金越紧", now)
    if m["us10y"] is not None:
        c5 = m["us10y_chg5"]
        now = ("利率在快速上升，对风险资产不友好" if gte(c5, 0.15) else "利率在下降，资金成本变低，对风险资产有利"
               if c5 is not None and c5 <= -0.15 else "利率平稳")
        item(L, "10 年期美债收益率", f"{m['us10y']:.2f}%（5 日 {c5:+.2f}，{m['us10y_date']}）" if c5 is not None
             else f"{m['us10y']:.2f}%", "全球资产定价的“锚”。利率越高，持有不生息的币和成长股越不划算", now)
    if len(L) > 1:
        S.append("\n".join(L))
    return "\n\n".join(S)


def summary_block(key, m, warns):
    """把所有指标归纳成“支撑因素 / 风险因素”，再用几句话说清楚现在该怎么看。"""
    good, bad = [], []

    def add(cond, lst, text):
        if cond:
            lst.append(text)

    f = m["btc_funding"]
    add(gte(f, T["funding_hot"]), bad, "合约多头拥挤")
    add(f is not None and f <= T["funding_cold"], good, "空头拥挤，有轧空可能")
    add(f is not None and T["funding_cold"] < f < T["funding_hot"], good, "杠杆不拥挤")
    oi_chg = m["btc_all_oi_chg"] if m["btc_all_oi_chg"] is not None else m["oi_chg"]
    add(gte(oi_chg, T["oi_jump_pct"]), bad, "杠杆在快速堆积")
    add(m["gex"] is not None and m["gex"] >= 0, good, "做市商在减震")
    add(m["gex"] is not None and m["gex"] < 0, bad, "做市商在放大波动")
    add(gte(m["put_oi_chg7"], T["put_oi_jump_pct"]), bad, "大资金在买下跌保险")
    add(gte(m["cme_basis"], 1), good, "CME 机构做多意愿强")
    add(m["cme_basis"] is not None and m["cme_basis"] < 0, bad, "CME 期货贴水")
    add(gte(m["cme_am_chg"], T["cme_am_chg_pct"]), good, "传统机构在加仓")
    add(m["cme_am_chg"] is not None and m["cme_am_chg"] <= -T["cme_am_chg_pct"], bad, "传统机构在减仓")
    add(gte(m["stable_chg7"], 0.5), good, "稳定币在流入")
    add(m["stable_chg7"] is not None and m["stable_chg7"] <= -0.5, bad, "稳定币在流出")
    add(gte(m["rv7"], T["rv_scale_min"]) and m["rv30"] and m["rv7"] >= m["rv30"] * T["rv_scale_ratio"], bad, "波动在放大")
    add(gte(m["flow_imb_pct"], T["flow_toxic_pct"]), bad, "订单流异常单边")
    add(gte(m["move_pct"], T["move_high_pct"]), bad, "债市紧张")
    add(gte(m["hy_pct"], 80), bad, "信用利差偏高")
    add(gte(m["vxn_pct"], 80), bad, "纳指紧张")
    add(m["vix"] is not None and m["vix"] < 20, good, "美股 VIX 平静")
    add(gte(m["vix"], 25), bad, "美股恐慌升温")
    add(gte(m["epu"], T["epu_warn"]), bad, "政策不确定性高")
    add(gte(m["dxy_chg5"], 0.8), bad, "美元走强")
    add(m["dxy_chg5"] is not None and m["dxy_chg5"] <= -0.8, good, "美元走弱")
    add(gte(m["us10y_chg5"], 0.15), bad, "利率上升")
    add(m["us10y_chg5"] is not None and m["us10y_chg5"] <= -0.15, good, "利率下降")
    add(gte(m.get("crisis_ratio"), 1.5), bad, "危机新闻暴增")
    add(m.get("cnn_fg") is not None and m["cnn_fg"] <= 25, bad, "美股极度恐慌")
    add(m["fng"] is not None and m["fng"] >= 80, bad, "币圈过度贪婪")
    add(m["fng"] is not None and m["fng"] <= 20, good, "币圈极度恐慌（逆向看常接近底部）")
    add(any(w.startswith("🧱") for w in warns), bad, "处在期权到期后的对冲断崖期")

    # 币市内部 vs 外部宏观，分开说
    calm = m["dvol"] is not None and m["dvol"] < 50 and not gte(m["rv7"], 50)
    inside = "币市内部比较平静" if calm else "币市内部波动偏大"
    if f is not None:
        inside += "，杠杆" + ("偏热" if gte(f, T["funding_hot"]) else "偏冷" if f <= T["funding_cold"] else "不拥挤")
    macro_bad = [x for x in bad if x in ("债市紧张", "信用利差偏高", "纳指紧张", "美股恐慌升温", "政策不确定性高",
                                         "美元走强", "利率上升", "危机新闻暴增", "美股极度恐慌")]
    outside = ("外部环境平稳" if not macro_bad else
               f"外部有 {len(macro_bad)} 处紧张信号（{'、'.join(macro_bad)}）" if len(macro_bad) <= 2 else
               f"外部环境偏紧（{'、'.join(macro_bad)}），币市容易被宏观拖累")
    score = len(bad) - len(good)
    tilt = "偏谨慎" if score >= 3 else "偏积极" if score <= -2 else "中性"
    s = STATES[key]
    act = s["do"]
    if tilt == "偏谨慎" and key in ("quiet", "normal"):
        act += "不过风险因素明显多于支撑因素，仓位宜比平时轻一些，止损按波动放宽。"
    elif tilt == "偏积极" and key in ("quiet", "normal"):
        act += "支撑因素占优，可以按计划执行，但仍要守住止损。"

    L = ["📝 <b>小总结</b>",
         f"今天是 {s['emoji']} {s['name']}。{inside}；{outside}。"]
    if good:
        L.append(f"✅ 支撑因素（{len(good)}）：" + "；".join(good))
    if bad:
        L.append(f"⚠️ 风险因素（{len(bad)}）：" + "；".join(bad))
    L.append(f"👉 综合判断：<b>{tilt}</b>。{act}")
    L.append(f"❌ 别做：{s['dont']}")
    return "\n".join(L)


def failed_summary(errors):
    """把 "Binance BTC现货: HTTPError" 这类错误按来源归类成 "Binance（BTC现货、日线）"。"""
    groups = {}
    for e in errors:
        label = e.split(":")[0]
        name, _, kind = label.partition(" ")
        groups.setdefault(name, [])
        if kind:
            groups[name].append(kind)
    return "、".join(f"{n}（{'、'.join(k)}）" if k else n for n, k in groups.items())


def sources_block(m):
    def names(xs):
        return "、".join(xs) if xs else "无"
    return ("<i>数据来源 · 现货：" + esc(names(m["btc_spot_src"]))
            + "；合约：" + esc(names(m["btc_perp_src"]))
            + "；日线：" + esc(names(m["daily_src"]))
            + "；爆仓：" + esc(names(m["btc_liq_src"]))
            + ("；大交易所持仓/爆仓：Coinalyze（" + esc("、".join(m["btc_all_ex"])) + "）" if m["btc_all_oi"] is not None else "")
            + ("；总市值：CoinGecko" if m["total_mcap"] is not None else "")
            + ("；稳定币：DefiLlama" if m["stable"] is not None else "")
            + ("；期权：Deribit" if m["gex"] is not None else "")
            + ("；CME：CFTC 持仓周报 / Yahoo" if m.get("cme") or m.get("cme_basis") is not None else "")
            + "；新闻：GDELT / 旧金山联储 / 地缘风险指数 / CNN"
            + "；DVOL：Deribit；恐惧贪婪：alternative.me；美股：Yahoo / FRED；美元/美债/信用利差/政策不确定性：FRED</i>")


def stop_tip(m):
    price, atr = m["btc_price"], m["atr"]
    if not price or not atr:
        return None
    step = 5000 if price >= 20000 else 1000
    below = int(price // step * step)
    above = below + step
    return ("🎯 <b>止损小提醒</b>\n"
            f"BTC 一天平均波动约 {fmt_money(atr)}（{m['atr_pct']:.1f}%）。\n"
            f"附近的整数关口是 {fmt_money(below)} 和 {fmt_money(above)}，很多人的止损会挤在这些位置附近，别跟大家挤在一起。\n"
            "如果你的止损距离比一天的正常波动还小，就很容易被扫掉——这时应该减小仓位或杠杆，而不是把止损缩得更窄。"
            + gamma_tip(m))


def gamma_tip(m):
    if m["gex"] is None:
        return ""
    if m["gex"] >= 0:
        return "\n现在做市商偏正 Gamma（减震器档，估算）：价格容易被拉回区间，止损可以适中。"
    return "\n现在做市商偏负 Gamma（放大器档，估算）：容易单边追涨杀跌，止损要按波动放宽，或者先离场观望。"


def state_block(key):
    s = STATES[key]
    return (f"<b>{s['emoji']} {s['name']}</b>\n"
            f"{esc(s['meaning'])}\n"
            f"✅ 适合：{esc(s['do'])}\n"
            f"❌ 别做：{esc(s['dont'])}")


def radar_block(warns):
    if not warns:
        return "📡 <b>风险雷达</b>\n没有发现异常信号。"
    return "📡 <b>风险雷达</b>\n" + "\n".join(warns)


def build_report(key, reasons, m, warns, now, errors, title="市场天气早报"):
    wd = "一二三四五六日"[now.weekday()]
    parts = [
        f"{STATES[key]['emoji']} <b>{title}</b> · {now.month}月{now.day}日 周{wd} {now:%H:%M}",
        "今天天气：" + state_block(key),
        "判断依据：" + esc("；".join(reasons)) if reasons else "",
        readings_block(m),
        radar_block(warns),
        stop_tip(m) or "",
        summary_block(key, m, warns),
        "📖 <b>今日一课</b>\n" + LESSONS[now.timetuple().tm_yday % len(LESSONS)],
    ]
    parts.append(sources_block(m))
    if errors:
        parts.append("<i>这次没拿到：" + esc(failed_summary(errors)) + "</i>")
    parts.append(DISCLAIMER)
    return "\n\n".join(p for p in parts if p)


def build_change_alert(old, new, reasons, m, warns, now):
    o, n = STATES[old], STATES[new]
    arrow = "⬆️ 风险升高" if n["level"] > o["level"] else "⬇️ 风险降低"
    parts = [
        f"🔔 <b>市场天气变了！</b>（{arrow}）\n{o['emoji']} {o['name']} → {n['emoji']} {n['name']}",
        "原因：" + esc("；".join(reasons)) if reasons else "",
        "现在：" + state_block(new),
        f"BTC {fmt_money(m['btc_price'])}（24h {fmt_pct(m['btc_chg24'])}）"
        + (f" · DVOL {m['dvol']:.0f}" if m["dvol"] is not None else ""),
        radar_block(warns) if warns else "",
        f"<i>{now:%m-%d %H:%M} · 仅用于学习理解市场，不构成投资建议。</i>",
    ]
    return "\n\n".join(p for p in parts if p)


# ============================================================
# Telegram
# ============================================================
def tg_api(token, method, payload=None):
    r = requests.post(f"https://api.telegram.org/bot{token}/{method}", json=payload or {}, timeout=20)
    d = r.json()
    if not d.get("ok"):
        raise RuntimeError(f"Telegram 错误：{d.get('description')}")
    return d["result"]


def find_chat_id(token):
    """自动找到你的聊天 ID：只要你先给 bot 发过一条消息就行。"""
    updates = tg_api(token, "getUpdates")
    for u in reversed(updates):
        msg = u.get("message") or u.get("channel_post") or u.get("my_chat_member") or {}
        chat = msg.get("chat")
        if chat:
            return str(chat["id"])
    return None


def send(token, chat_ids, text, dry_run=False):
    """发给每一个聊天；某个群发送失败不影响其他聊天，全部失败才报错。"""
    if dry_run or not token:
        print("=" * 50)
        print(text)
        print("=" * 50)
        return
    ok = 0
    for chat_id in chat_ids:
        try:
            for part in split_message(text):
                tg_api(token, "sendMessage", {"chat_id": chat_id, "text": part,
                                              "parse_mode": "HTML", "disable_web_page_preview": True})
                time.sleep(1)
            ok += 1
        except Exception as e:  # noqa
            print(f"❗ 发送到 {chat_id} 失败：{e}（如果是群，请确认 bot 已经在群里并且可以发言）", file=sys.stderr)
    if not ok:
        raise RuntimeError("所有聊天都发送失败")


def split_message(text, limit=3900):
    """Telegram 一条消息最多 4096 字：按板块（空行）切成几条，板块太长再按行切。"""
    parts, cur = [], ""
    for block in text.split("\n\n"):
        pieces = [block] if len(block) <= limit else block.split("\n")
        for p in pieces:
            if cur and len(cur) + len(p) + 2 > limit:
                parts.append(cur)
                cur = ""
            cur = f"{cur}\n\n{p}" if cur else p
    if cur:
        parts.append(cur)
    return parts


# ============================================================
# 状态保存
# ============================================================
def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ============================================================
# 主流程
# ============================================================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true", help="马上发一份完整报告")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不发送")
    ap.add_argument("--mock", help="用模拟数据测试（json 文件路径）")
    args = ap.parse_args()

    force_report = args.report or os.environ.get("FORCE_REPORT", "").lower() == "true"
    token = os.environ.get("TG_BOT_TOKEN", "").strip()
    dry = args.dry_run or not token

    state = load_state()
    now = now_local()
    today = now.date().isoformat()

    chat_id = os.environ.get("TG_CHAT_ID", "").strip() or state.get("chat_id")
    if token and not chat_id:
        chat_id = find_chat_id(token)
        if not chat_id:
            print("❗ 找不到聊天 ID：请先在 Telegram 里打开你的 bot，发一条消息（例如 /start），然后再运行一次。")
            sys.exit(1)
        state["chat_id"] = chat_id
    # 私聊 + 设置区里的群；TG_CHAT_ID 也可以用逗号填多个
    chat_ids = list(dict.fromkeys(
        [x.strip() for x in str(chat_id or "").split(",") if x.strip()] + EXTRA_CHAT_IDS))

    if args.mock:
        with open(args.mock, encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("errors", [])
        mock_now = data.get("now")
        if mock_now:
            now = datetime.fromisoformat(mock_now)
            today = now.date().isoformat()
    else:
        data = fetch_all(state)

    m = compute_metrics(data, state)
    if m["btc_price"] is None and m["dvol"] is None:
        print("❗ 主要数据都拿不到，这次先跳过。", data["errors"])
        save_state(state)
        return

    candidate, reasons = classify(m)
    warns = radar(m, now.date())
    last = state.get("last_state")

    # 防抖：除了“崩溃 / 爆发”马上提醒，其他状态要连续 2 次（约 2 小时）一致才算真的切换
    changed = False
    if last is None:
        state["last_state"] = candidate
        force_report = True
        title = "市场天气 Bot 已启动"
    else:
        title = "市场天气早报"
        if candidate != last:
            urgent = candidate in ("crash", "burst") and STATES[candidate]["level"] > STATES[last]["level"]
            if urgent or state.get("pending_state") == candidate:
                changed = True
            else:
                state["pending_state"] = candidate
        else:
            state.pop("pending_state", None)

    if changed:
        send(token, chat_ids, build_change_alert(last, candidate, reasons, m, warns, now), dry)
        state["last_state"] = candidate
        state.pop("pending_state", None)
        state["last_change"] = now.isoformat(timespec="minutes")

    morning_due = now.hour >= MORNING_REPORT_HOUR and state.get("last_report_date") != today
    if force_report or morning_due:
        send(token, chat_ids, build_report(state["last_state"], reasons if state["last_state"] == candidate else [],
                                          m, warns, now, data["errors"], title=title), dry)
        if morning_due:
            state["last_report_date"] = today

    state["last_run"] = now.isoformat(timespec="minutes")
    state["last_candidate"] = candidate
    save_state(state)
    print(f"完成：当前判断 {candidate}，记录状态 {state['last_state']}，变化提醒={changed}")


if __name__ == "__main__":
    main()
