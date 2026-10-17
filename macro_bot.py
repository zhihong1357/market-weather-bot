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
# 数据获取（每个来源单独容错：拿不到就显示“—”，不影响其他）
# ============================================================
DERIBIT = "https://www.deribit.com/api/v2/public"


def fetch_ticker(instrument):
    d = get_json(f"{DERIBIT}/ticker", {"instrument_name": instrument})["result"]
    return {
        "price": d.get("index_price") or d.get("last_price"),
        "chg24": (d.get("stats") or {}).get("price_change"),
        "funding_8h": d.get("funding_8h"),
        "oi": d.get("open_interest"),
    }


def fetch_btc_daily_deribit(days=120):
    end = int(time.time() * 1000)
    start = end - days * 86400 * 1000
    d = get_json(f"{DERIBIT}/get_tradingview_chart_data", {
        "instrument_name": "BTC-PERPETUAL", "start_timestamp": start,
        "end_timestamp": end, "resolution": "1D"})["result"]
    return {"high": d["high"], "low": d["low"], "close": d["close"],
            "volume": d.get("cost") or d["volume"], "ticks": d["ticks"]}


def fetch_btc_daily_coinbase():
    # 备用来源：Coinbase（返回最新在前，需要倒序）
    rows = get_json("https://api.exchange.coinbase.com/products/BTC-USD/candles",
                    {"granularity": 86400})
    rows = sorted(rows, key=lambda r: r[0])
    return {"high": [r[2] for r in rows], "low": [r[1] for r in rows],
            "close": [r[4] for r in rows], "volume": [r[5] * r[4] for r in rows],
            "ticks": [r[0] * 1000 for r in rows]}


def fetch_dvol(hours=8 * 24):
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


def fetch_vix_fred():
    r = requests.get("https://fred.stlouisfed.org/graph/fredgraph.csv",
                     params={"id": "VIXCLS"}, headers=UA, timeout=20)
    r.raise_for_status()
    vals = []
    for line in r.text.strip().splitlines()[1:]:
        parts = line.split(",")
        try:
            vals.append(float(parts[1]))
        except (ValueError, IndexError):
            pass
    return vals[-260:] if vals else None


def safe(fn, *a, label="", errors=None):
    try:
        return fn(*a)
    except Exception as e:  # noqa
        if errors is not None:
            errors.append(f"{label}: {type(e).__name__}")
        print(f"[warn] {label} 获取失败: {e}", file=sys.stderr)
        return None


def fetch_all():
    errors = []
    data = {"errors": errors}
    data["btc"] = safe(fetch_ticker, "BTC-PERPETUAL", label="BTC", errors=errors)
    data["eth"] = safe(fetch_ticker, "ETH-PERPETUAL", label="ETH", errors=errors)
    daily = safe(fetch_btc_daily_deribit, label="BTC日线", errors=errors)
    if not daily:
        daily = safe(fetch_btc_daily_coinbase, label="BTC日线(备用)", errors=errors)
    data["daily"] = daily
    data["dvol"] = safe(fetch_dvol, label="DVOL", errors=errors)
    data["fng"] = safe(fetch_fng, label="恐惧贪婪", errors=errors)
    vix = safe(fetch_yahoo_closes, "^VIX", label="VIX", errors=errors)
    if not vix:
        vix = safe(fetch_vix_fred, label="VIX(备用)", errors=errors)
    data["vix_series"] = vix
    data["move_series"] = safe(fetch_yahoo_closes, "^MOVE", label="MOVE", errors=errors)
    data["ndx_series"] = safe(fetch_yahoo_closes, "^NDX", label="纳指", errors=errors)
    # 如果 Deribit 的 BTC 行情拿不到，用日线补价格和 24h 涨跌
    if not data["btc"] and daily and len(daily["close"]) >= 2:
        c = daily["close"]
        data["btc"] = {"price": c[-1], "chg24": (c[-1] / c[-2] - 1) * 100,
                       "funding_8h": None, "oi": None}
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


def compute_metrics(data, state):
    m = {}
    btc, eth, daily, dvol = data.get("btc"), data.get("eth"), data.get("daily"), data.get("dvol")
    m["btc_price"] = btc and btc.get("price")
    m["btc_chg24"] = btc and btc.get("chg24")
    m["btc_funding"] = btc and btc.get("funding_8h")
    m["btc_oi"] = btc and btc.get("oi")
    m["eth_price"] = eth and eth.get("price")
    m["eth_chg24"] = eth and eth.get("chg24")
    m["eth_funding"] = eth and eth.get("funding_8h")

    m["adx"] = m["atr"] = m["atr_pct"] = m["vol_ratio"] = None
    if daily and len(daily["close"]) > 40:
        # 去掉今天还没走完的那根 K 线
        h, l, c, v = daily["high"][:-1], daily["low"][:-1], daily["close"][:-1], daily["volume"][:-1]
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

    vs = data.get("vix_series")
    m["vix"] = vs[-1] if vs else None
    m["vix_pct"] = percentile_rank(vs, m["vix"]) if vs else None
    ms = data.get("move_series")
    m["move"] = ms[-1] if ms else None
    m["move_pct"] = percentile_rank(ms, m["move"]) if ms else None
    ns = data.get("ndx_series")
    m["ndx_chg"] = ((ns[-1] / ns[-2] - 1) * 100) if ns and len(ns) >= 2 else None

    # 持仓量 24h 变化（用自己保存的历史）
    m["oi_chg"] = None
    hist = state.get("oi_history", [])
    now_ts = int(time.time())
    if m["btc_oi"]:
        old = [x for x in hist if now_ts - x[0] >= 20 * 3600]
        if old:
            base = old[-1][1]
            if base:
                m["oi_chg"] = (m["btc_oi"] / base - 1) * 100
        hist.append([now_ts, m["btc_oi"]])
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
    L.append(f"BTC {fmt_money(m['btc_price'])}（24h {fmt_pct(m['btc_chg24'])}）")
    L.append(f"ETH {fmt_money(m['eth_price'])}（24h {fmt_pct(m['eth_chg24'])}）")
    if m["dvol"] is not None:
        L.append(f"币市恐慌度 DVOL：{m['dvol']:.0f}（{lvl_dvol(m['dvol'])}）")
    if m["btc_funding"] is not None:
        L.append(f"BTC 资金费率：{m['btc_funding'] * 100:.4f}%/8h（{lvl_funding(m['btc_funding'])}）")
    if m["fng"] is not None:
        L.append(f"恐惧贪婪指数：{m['fng']}（{lvl_fng(m['fng'])}）")
    if m["adx"] is not None:
        L.append(f"趋势强度 ADX：{m['adx']:.0f}（{lvl_adx(m['adx'])}）")
    if m["vol_ratio"] is not None:
        L.append(f"近 5 天成交量：平时的 {m['vol_ratio'] * 100:.0f}%")
    if m["vix"] is not None:
        L.append(f"美股恐慌 VIX：{m['vix']:.1f}（{lvl_vix(m['vix'])}）")
    if m["ndx_chg"] is not None:
        L.append(f"纳斯达克 100：上个交易日 {fmt_pct(m['ndx_chg'])}")
    if m["move_pct"] is not None:
        L.append(f"债市恐慌 MOVE：一年内 {m['move_pct']:.0f}% 分位")
    return "\n".join(L)


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
    if errors:
        parts.append("<i>部分数据暂时拿不到：" + esc("、".join(e.split(":")[0] for e in errors)) + "</i>")
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
