# -*- coding: utf-8 -*-
"""
市场天气 Bot —— 宏观感知 + 操控雷达（学习用）

每次运行：
  1. 拉取免费公开数据（BTC/ETH、币市波动率 DVOL、资金费率、恐惧贪婪、VIX、MOVE、纳指）
  2. 判断现在是哪种"市场天气"（6 种状态）
  3. 状态变化 -> 推送 Telegram 提醒；每天早上 8 点 -> 推送一份早报

仅用于学习理解市场，不构成投资建议。
"""
import json
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
    "adx_quiet_max": 25,    # 静默期要求 ADX < 25
    # --- 成交量 ---
    "volume_quiet_ratio": 0.9,  # 近 5 天成交量 / 近 30 天平均 < 0.9：成交冷清
    # --- 美股恐慌指数 VIX ---
    "vix_quiet": 20,
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
    "move_high_pct": 90,        # 债市恐慌 MOVE 处于 1 年 90% 分位以上
    "vix_low_pct": 50,          # 同时 VIX 在 1 年 50% 分位以下 → 股债"时差"
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
    "<b>状态比方向重要</b>：先判断现在是什么“天气”，再决定要不要出门。"
    "同一个策略，在静默期可能赚钱，在绞杀期可能被来回打止损。",
]

DISCLAIMER = "<i>仅用于学习理解市场，不构成投资建议。</i>"


# ============================================================
# 工具函数
# ============================================================
def now_local():
    return datetime.now(timezone.utc) + timedelta(hours=TIMEZONE_OFFSET_HOURS)


def get_json(url, params=None, timeout=20, tries=2):
    last = None
    for i in range(tries):
        try:
            r = requests.get(url, params=params, headers=UA, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except requests.HTTPError as e:
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


def fetch_fred(series):
    # 美联储圣路易斯分行 FRED 的官方收盘数据，作为 Yahoo 拿不到时的第二来源
    r = requests.get("https://fred.stlouisfed.org/graph/fredgraph.csv",
                     params={"id": series}, headers=UA, timeout=20)
    r.raise_for_status()
    vals = []
    for line in r.text.strip().splitlines()[1:]:
        parts = line.split(",")
        try:
            vals.append(float(parts[1]))
        except (ValueError, IndexError):
            pass
    return vals[-260:] if vals else None


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


def fetch_all():
    errors = []
    data = {"errors": errors}
    data["spot"] = {c: collect(SPOT_SOURCES, f"{c}现货", errors, c) for c in COINS}
    data["perp"] = {c: collect(PERP_SOURCES, f"{c}合约", errors, c) for c in COINS}
    data["daily"] = combine_daily(collect(DAILY_SOURCES, "日线", errors))
    data["liq_gate"] = {c: safe(liq_gate, c, label=f"Gate {c}爆仓", errors=errors) for c in COINS}
    data["liq_okx"] = {c: safe(liq_okx_events, c, label=f"OKX {c}爆仓", errors=errors) for c in COINS}
    data["dvol"] = safe(fetch_dvol, label="DVOL", errors=errors)
    data["fng"] = safe(fetch_fng, label="恐惧贪婪", errors=errors)
    data["vix_series"] = (safe(fetch_yahoo_closes, "^VIX", label="VIX(Yahoo)", errors=errors)
                          or safe(fetch_fred, "VIXCLS", label="VIX(FRED)", errors=errors))
    data["move_series"] = safe(fetch_yahoo_closes, "^MOVE", label="MOVE", errors=errors)
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
            hours = (now_ms - okx["since"][c]) / 3.6e6
            if hours < 23:
                notes.append(f"OKX {c} 才累计了 {hours:.0f} 小时")
            elif now_ms - okx["gap_ts"].get(c, 0) <= 24 * 3600 * 1000:
                notes.append(f"OKX {c} 近 24h 爆仓太密集，可能有漏记")
    m["liq_note"] = "，".join(notes)


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

    m["dvol"] = dvol and dvol["now"]
    m["dvol_d1"] = dvol and dvol["d1"]
    m["dvol_d7"] = dvol and dvol["d7"]
    m["dvol_jump"] = ((dvol["now"] / dvol["d1"] - 1) * 100) if dvol and dvol["d1"] else None

    m["fng"] = data.get("fng")
    liquidation_metrics(m, data, state)

    vs = data.get("vix_series")
    m["vix"] = vs[-1] if vs else None
    m["vix_pct"] = percentile_rank(vs, m["vix"]) if vs else None
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
    if (lt(dvol, T["dvol_quiet"]) and lt(adx, T["adx_quiet_max"])
            and lt(m["vol_ratio"], T["volume_quiet_ratio"])
            and (vix is None or vix < T["vix_quiet"])):
        r.append(f"DVOL 低（{dvol:.0f}）")
        r.append(f"成交量只有平时的 {m['vol_ratio'] * 100:.0f}%")
        r.append(f"ADX {adx:.0f}，没有明显行情")
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
        elif -1 <= diff < 0:
            out.append(f"📅 昨天{name}刚到期，做市商的“减震”作用变弱，波动可能放大。")
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
    if gte(m["oi_chg"], T["oi_jump_pct"]) and m["btc_chg24"] is not None and abs(m["btc_chg24"]) < 2:
        w.append(f"⚡ 持仓量 24 小时增加 {m['oi_chg']:.0f}% 但价格几乎没动：大量新杠杆在堆积，后面容易出现大波动。")
    if gte(m["move_pct"], T["move_high_pct"]) and m["vix_pct"] is not None and m["vix_pct"] <= T["vix_low_pct"]:
        w.append(f"⏳ 股债时差：债市恐慌 MOVE 在一年 {m['move_pct']:.0f}% 分位，美股 VIX 只在 {m['vix_pct']:.0f}% 分位。"
                 "债市已经紧张、股市还平静，风险可能还没完全反映出来。")
    if m["vix"] is not None and T["vix_warn"] <= m["vix"] < T["vix_burst"]:
        w.append(f"🇺🇸 美股恐慌指数 VIX 升到 {m['vix']:.0f}：美股紧张时，币市常被一起拖累。")
    if gte(m["dvol_jump"], 10) and not gte(m["dvol_jump"], T["dvol_jump_pct"]):
        w.append(f"📈 DVOL 24 小时上升 {m['dvol_jump']:.0f}%：市场正在变紧张。")
    if m["btc_liq_long"] is not None:
        ln = m["btc_liq_long"] + (m["eth_liq_long"] or 0)
        sh = m["btc_liq_short"] + (m["eth_liq_short"] or 0)
        tot = ln + sh
        if tot >= T["liq_big_usd"]:
            side = ""
            if ln / tot * 100 >= T["liq_one_side_pct"]:
                side = f"其中多单占 {ln / tot * 100:.0f}%，做多的人被集中清算。"
            elif sh / tot * 100 >= T["liq_one_side_pct"]:
                side = f"其中空单占 {sh / tot * 100:.0f}%，做空的人被集中清算（轧空）。"
            w.append(f"💥 24h BTC+ETH 爆仓 {fmt_musd(tot)}（OKX、Gate 两家）：大规模强平，杠杆正在被清洗。" + side)
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


def readings_block(m):
    L = ["📊 <b>关键读数</b>"]
    for c in COINS:
        k = c.lower()
        L.append(f"{c} {fmt_money(m[k + '_price'])}（24h {fmt_pct(m[k + '_chg24'])}）"
                 f"· {len(m[k + '_spot_src'])} 家现货中位数")
    if m["dvol"] is not None:
        L.append(f"币市恐慌度 DVOL：{m['dvol']:.0f}（{lvl_dvol(m['dvol'])}）")
    for c in COINS:
        k = c.lower()
        if m[k + "_funding"] is not None:
            L.append(f"{c} 资金费率：{m[k + '_funding'] * 100:.4f}%/8h（{lvl_funding(m[k + '_funding'])}）"
                     f"· {m[k + '_funding_n']} 家{m[k + '_funding_how']}，各家 {m[k + '_funding_min'] * 100:.4f}% ~ "
                     f"{m[k + '_funding_max'] * 100:.4f}%")
    for c in COINS:
        k = c.lower()
        if m[k + "_liq_long"] is not None:
            L.append(f"{c} 24h 爆仓：多单 {fmt_musd(m[k + '_liq_long'])} / 空单 {fmt_musd(m[k + '_liq_short'])}"
                     f"（{'、'.join(m[k + '_liq_src'])}）")
    if m["btc_liq_long"] is not None and m["liq_note"]:
        L.append(f"<i>爆仓只含 OKX、Gate（不含 Binance、Bybit），{m['liq_note']}</i>")
    if m["btc_oi"] is not None:
        L.append(f"BTC 合约持仓：${m['btc_oi'] / 1e9:.1f}B（{len(m['btc_oi_by_ex'])} 家合计）"
                 + (f"，24h {fmt_pct(m['oi_chg'])}" if m["oi_chg"] is not None else ""))
    if m["fng"] is not None:
        L.append(f"恐惧贪婪指数：{m['fng']}（{lvl_fng(m['fng'])}）")
    if m["adx"] is not None:
        L.append(f"趋势强度 ADX：{m['adx']:.0f}（{lvl_adx(m['adx'])}）")
    if m["vol_ratio"] is not None:
        L.append(f"近 5 天现货成交量：平时的 {m['vol_ratio'] * 100:.0f}%（{len(m['daily_src'])} 家合计）")
    if m["vix"] is not None:
        L.append(f"美股恐慌 VIX：{m['vix']:.1f}（{lvl_vix(m['vix'])}）")
    if m["ndx_chg"] is not None:
        L.append(f"纳斯达克 100：上个交易日 {fmt_pct(m['ndx_chg'])}")
    if m["move_pct"] is not None:
        L.append(f"债市恐慌 MOVE：一年内 {m['move_pct']:.0f}% 分位")
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
            + "；DVOL：Deribit；恐惧贪婪：alternative.me；美股：Yahoo / FRED</i>")


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
            "如果你的止损距离比一天的正常波动还小，就很容易被扫掉——这时应该减小仓位或杠杆，而不是把止损缩得更窄。")


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


def send(token, chat_id, text, dry_run=False):
    if dry_run or not token:
        print("=" * 50)
        print(text)
        print("=" * 50)
        return
    tg_api(token, "sendMessage", {"chat_id": chat_id, "text": text,
                                  "parse_mode": "HTML", "disable_web_page_preview": True})


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

    if args.mock:
        with open(args.mock, encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("errors", [])
        mock_now = data.get("now")
        if mock_now:
            now = datetime.fromisoformat(mock_now)
            today = now.date().isoformat()
    else:
        data = fetch_all()

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
        send(token, chat_id, build_change_alert(last, candidate, reasons, m, warns, now), dry)
        state["last_state"] = candidate
        state.pop("pending_state", None)
        state["last_change"] = now.isoformat(timespec="minutes")

    morning_due = now.hour >= MORNING_REPORT_HOUR and state.get("last_report_date") != today
    if force_report or morning_due:
        send(token, chat_id, build_report(state["last_state"], reasons if state["last_state"] == candidate else [],
                                          m, warns, now, data["errors"], title=title), dry)
        if morning_due:
            state["last_report_date"] = today

    state["last_run"] = now.isoformat(timespec="minutes")
    state["last_candidate"] = candidate
    save_state(state)
    print(f"完成：当前判断 {candidate}，记录状态 {state['last_state']}，变化提醒={changed}")


if __name__ == "__main__":
    main()
