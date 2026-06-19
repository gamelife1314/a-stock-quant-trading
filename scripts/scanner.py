#!/usr/bin/env python3
"""
A股全市场扫描器 V4.0 - 开源版
数据源: 腾讯财经 HTTP 接口 (K线 + 实时行情不含PE)
依赖: 零外部依赖，纯 Python 标准库
策略: 获取K线 → 本地计算技术指标 → 三层信号链路扫描
三层扫描链路: 地量止跌 → RSI拐头 → MACD金叉(+KDJ辅助)
"""

import json
import math
import time
import os
import urllib.request
import urllib.error
import ssl
import csv
import io
from datetime import datetime

# SSL 兼容
ssl._create_default_https_context = ssl._create_unverified_context

# ══════════════════════════════════════════════════════════════
# 技术指标计算（纯 Python 实现，零依赖）
# ══════════════════════════════════════════════════════════════

def mean(data):
    """计算均值"""
    if not data:
        return 0
    return sum(data) / len(data)

def calc_ema(data, period):
    """计算 EMA"""
    if len(data) < period:
        return []
    k = 2.0 / (period + 1.0)
    result = [mean(data[:period])]
    for v in data[period:]:
        result.append(v * k + result[-1] * (1 - k))
    return result

def calc_ma(closes, period):
    """计算移动均线，返回数组（前 period-1 个值为均值，之后为 MA）"""
    result = []
    buf = []
    for i, v in enumerate(closes):
        buf.append(v)
        if i >= period:
            buf.pop(0)
        result.append(sum(buf) / len(buf))
    return result

def calc_macd(closes, fast=12, slow=26, signal=9):
    """计算 MACD，返回 (dif, dea, macd_hist) 三个数组"""
    ema_fast = calc_ema(closes, fast)
    ema_slow = calc_ema(closes, slow)
    offset = len(ema_fast) - len(ema_slow)
    dif = [ema_fast[i+offset] - ema_slow[i] for i in range(len(ema_slow))]
    dea = calc_ema(dif, signal)
    offset2 = len(dif) - len(dea)
    macd_hist = [2.0 * (dif[i+offset2] - dea[i]) for i in range(len(dea))]
    return dif, dea, macd_hist

def calc_rsi(closes, period=6):
    """计算 RSI(6)"""
    if len(closes) < period + 1:
        return [50.0] * len(closes)
    result = [50.0] * period
    gains, losses = [], []
    for i in range(1, period+1):
        diff = closes[i] - closes[i-1]
        if diff > 0:
            gains.append(diff)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(-diff)
    avg_gain = mean(gains)
    avg_loss = mean(losses)
    for i in range(period+1, len(closes)):
        diff = closes[i] - closes[i-1]
        gain = diff if diff > 0 else 0
        loss = -diff if diff < 0 else 0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        if avg_loss == 0:
            rsi = 100.0
        else:
            rs = avg_gain / avg_loss
            rsi = 100.0 - 100.0 / (1.0 + rs)
        result.append(rsi)
    return result

def calc_kdj(highs, lows, closes, n=9):
    """计算 KDJ(9,3,3)"""
    length = len(closes)
    k_vals = [50.0] * length
    d_vals = [50.0] * length
    j_vals = [50.0] * length
    for i in range(n-1, length):
        hn = max(highs[i-n+1:i+1])
        ln = min(lows[i-n+1:i+1])
        rng = hn - ln
        if rng > 0:
            rsv = (closes[i] - ln) / rng * 100.0
        else:
            rsv = 50.0
        k_vals[i] = 2.0/3.0 * k_vals[i-1] + 1.0/3.0 * rsv
        d_vals[i] = 2.0/3.0 * d_vals[i-1] + 1.0/3.0 * k_vals[i]
        j_vals[i] = 3.0 * k_vals[i] - 2.0 * d_vals[i]
    return k_vals, d_vals, j_vals

def calc_bollinger(closes, period=20, k=2.0):
    """计算布林线 (中轨, 上轨, 下轨, 带宽)"""
    ma = calc_ma(closes, period)
    upper = [0.0] * len(closes)
    lower = [0.0] * len(closes)
    bandwidth = [0.0] * len(closes)
    for i in range(period-1, len(closes)):
        window = closes[i-period+1:i+1]
        m = ma[i]
        std = math.sqrt(sum((x - m) ** 2 for x in window) / period)
        upper[i] = m + k * std
        lower[i] = m - k * std
        bandwidth[i] = upper[i] - lower[i]
    return ma, upper, lower, bandwidth

# ══════════════════════════════════════════════════════════════
# 数据获取: 腾讯财经 HTTP 接口
# ══════════════════════════════════════════════════════════════

REQ_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
}

def http_get(url, timeout=15, encoding='utf-8', raw=False):
    """HTTP GET 请求"""
    req = urllib.request.Request(url, headers=REQ_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
            return data if raw else data.decode(encoding, errors='replace')
    except Exception:
        return None

def get_stock_list():
    """获取全市场A股列表（腾讯接口）"""
    stocks = {}
    # 分别获取上证、深证、北证
    for market_prefix in ['sh', 'sz', 'bj']:
        url = f"https://smartbox.gtimg.cn/s3/?q=mt%3A{market_prefix}&t=all&c=ecode,ename,cname,pName&o=m&p=1&ps=6000"
        text = http_get(url)
        if not text:
            continue
        for line in text.split('\n'):
            line = line.strip()
            if not line or '=' not in line:
                continue
            parts = line.split('"')
            if len(parts) < 2:
                continue
            entries = parts[1].split('^')
            for entry in entries:
                fields = entry.split('~')
                if len(fields) < 4:
                    continue
                code = fields[1]
                name = fields[2]
                market = fields[0]
                if 'ST' in name.upper() or '*ST' in name:
                    continue
                stocks[code] = {"code": code, "name": name, "market": market}
    return stocks

def get_realtime_quotes(codes, batch_size=50):
    """批量获取腾讯实时行情（不含PE）"""
    result = {}
    for i in range(0, len(codes), batch_size):
        batch = codes[i:i+batch_size]
        prefixed = []
        code_to_prefix = {}
        for c in batch:
            if c.startswith(('6','9')):
                prefix = f'sh{c}'
            elif c.startswith('8'):
                prefix = f'bj{c}'
            else:
                prefix = f'sz{c}'
            prefixed.append(prefix)
            code_to_prefix[prefix] = c
        url = 'https://qt.gtimg.cn/q=' + ','.join(prefixed)
        text = http_get(url, encoding='gbk')
        if not text:
            continue
        for line in text.strip().split(';'):
            if '=' not in line or '"' not in line:
                continue
            parts = line.split('"')
            if len(parts) < 2:
                continue
            key_part = line.split('=')[0]
            if '_' not in key_part:
                continue
            prefix = key_part.split('_')[1]
            code = code_to_prefix.get(prefix, prefix[2:])
            vals = parts[1].split('~')
            if len(vals) < 50:
                continue
            try:
                price = float(vals[3]) if vals[3] else 0
                last_close = float(vals[4]) if vals[4] else 0
                change_pct = float(vals[32]) if vals[32] else 0
                pb = float(vals[46]) if vals[46] else 0
                mcap_yi = float(vals[44]) if vals[44] else 0
                # PE 腾讯不提供，设为0（需其他数据源补充）
                pe_ttm = 0
                result[code] = {
                    "name": vals[1],
                    "price": price,
                    "last_close": last_close,
                    "change_pct": change_pct,
                    "pe_ttm": pe_ttm,
                    "pb": pb,
                    "mcap_yi": mcap_yi,
                }
            except (ValueError, IndexError):
                continue
    return result

def fetch_klines(code, market, days=45):
    """获取单只股票K线（腾讯接口）"""
    # 市场后缀映射
    mkt = {'sh': 0, 'sz': 0, 'bj': 0}
    url = f"http://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={market}{code},day,,,{days},qfq"
    raw = http_get(url)
    if not raw:
        return None
    try:
        data = json.loads(raw)
        kline_key = f"{market}{code}"
        kline_data = data.get('data', {}).get(kline_key, {})
        klines = kline_data.get('qfqday') or kline_data.get('day') or []
        if not klines:
            return None
        closes = []
        vols = []
        highs = []
        lows = []
        for k in klines[-days:]:
            closes.append(float(k[2]))
            vols.append(float(k[5]))
            highs.append(float(k[3]))
            lows.append(float(k[4]))
        if len(closes) < 25:
            return None
        return {
            "close": closes,
            "vol": vols,
            "high": highs,
            "low": lows,
        }
    except Exception:
        return None

# ══════════════════════════════════════════════════════════════
# 三层扫描信号
# ══════════════════════════════════════════════════════════════

def scan_shrinkage_bottom(kline_data, code_list):
    """第一层：地量止跌"""
    results = []
    for code in code_list:
        data = kline_data.get(code)
        if not data:
            continue
        try:
            closes = data["close"]
            vols = data["vol"]
            highs = data["high"]
            lows = data["low"]
            if len(closes) < 25:
                continue
            latest_close = closes[-1]
            prev_close = closes[-2]
            latest_high = highs[-1]
            latest_low = lows[-1]
            latest_vol = vols[-1]
            avg_vol_20 = mean(vols[-21:-1])
            if avg_vol_20 <= 0 or latest_vol >= avg_vol_20 * 0.6:
                continue
            change_pct = (latest_close / prev_close - 1.0) * 100.0 if prev_close > 0 else 0
            if change_pct < -0.5 or change_pct > 1.5:
                continue
            amplitude = (latest_high - latest_low) / latest_close * 100.0 if latest_close > 0 else 100.0
            if amplitude > 3.0:
                continue
            ma20 = mean(closes[-20:])
            dist_ma20 = (latest_close - ma20) / ma20 * 100.0
            if dist_ma20 < -8.0 or dist_ma20 > 10.0:
                continue
            vol_ratio = latest_vol / avg_vol_20 if avg_vol_20 > 0 else 0
            rsi_vals = calc_rsi(closes, 6)
            rsi6 = rsi_vals[-1]
            # 连续缩量天数
            shrink_days = 0
            for j in range(-1, -min(10, len(vols)) - 1, -1):
                if j - 20 >= 0:
                    aj = mean(vols[j-20:j])
                else:
                    aj = avg_vol_20
                if aj > 0 and vols[j] < aj * 0.8:
                    shrink_days += 1
                else:
                    break
            results.append({
                "code": code,
                "close": round(latest_close, 2),
                "ma20": round(ma20, 2),
                "change_pct": round(change_pct, 2),
                "vol_ratio": round(vol_ratio, 3),
                "amplitude": round(amplitude, 2),
                "dist_ma20": round(dist_ma20, 2),
                "rsi6": round(rsi6, 1),
                "shrink_days": shrink_days,
                "signal_type": "shrinkage",
            })
        except Exception:
            continue
    return results

def scan_rsi_turn(kline_data, code_list):
    """第二层：RSI拐头"""
    results = []
    for code in code_list:
        data = kline_data.get(code)
        if not data:
            continue
        try:
            closes = data["close"]
            vols = data["vol"]
            if len(closes) < 25:
                continue
            latest_close = closes[-1]
            prev_close = closes[-2]
            rsi_vals = calc_rsi(closes, 6)
            rsi_today = rsi_vals[-1]
            rsi_yesterday = rsi_vals[-2]
            if rsi_today <= rsi_yesterday or rsi_yesterday >= 35:
                continue
            ma20 = mean(closes[-20:])
            dist_ma20 = (latest_close - ma20) / ma20 * 100.0
            if dist_ma20 < -8.0 or dist_ma20 > 5.0:
                continue
            avg_vol_20 = mean(vols[-21:-1])
            vol_ratio = vols[-1] / avg_vol_20 if avg_vol_20 > 0 else 0
            if vol_ratio > 2.5:
                continue
            dif, dea, _ = calc_macd(closes)
            if len(dif) < 2 or dif[-1] > dea[-1]:
                continue
            gap = abs(dif[-1] - dea[-1])
            gap_prev = abs(dif[-2] - dea[-2])
            if gap > 0.5 or gap >= gap_prev:
                continue
            change_pct = (latest_close / prev_close - 1.0) * 100.0 if prev_close > 0 else 0
            results.append({
                "code": code,
                "close": round(latest_close, 2),
                "ma20": round(ma20, 2),
                "change_pct": round(change_pct, 2),
                "vol_ratio": round(vol_ratio, 2),
                "rsi6_today": round(rsi_today, 1),
                "rsi6_yesterday": round(rsi_yesterday, 1),
                "dif": round(dif[-1], 4),
                "dea": round(dea[-1], 4),
                "dif_gap": round(gap, 4),
                "dist_ma20": round(dist_ma20, 2),
                "signal_type": "rsi_turn",
            })
        except Exception:
            continue
    return results

def scan_golden_cross(kline_data, code_list):
    """第三层：MACD金叉确认 + KDJ辅助"""
    results = []
    for code in code_list:
        data = kline_data.get(code)
        if not data:
            continue
        try:
            closes = data["close"]
            vols = data["vol"]
            highs = data.get("high", closes)
            lows = data.get("low", closes)
            if len(closes) < 25:
                continue
            latest_close = closes[-1]
            prev_close = closes[-2]
            ma20 = mean(closes[-20:])
            avg_vol_20 = mean(vols[-21:-1])
            vol_ratio = vols[-1] / avg_vol_20 if avg_vol_20 > 0 else 0
            change_pct = (latest_close / prev_close - 1.0) * 100.0 if prev_close > 0 else 0
            if change_pct < 3.0 or change_pct > 9.5:
                continue
            if latest_close <= ma20:
                continue
            if vol_ratio < 1.5:
                continue
            dif, dea, _ = calc_macd(closes)
            if dif[-1] <= dea[-1]:
                continue
            # 金叉天数
            cross_days = 99
            for d in range(-1, -min(15, len(dif)) - 1, -1):
                if dif[d] > dea[d] and dif[d-1] <= dea[d-1]:
                    cross_days = abs(d + 1)
                    break
            if cross_days > 5:
                continue
            # KDJ
            k_vals, d_vals, j_vals = calc_kdj(highs, lows, closes)
            k_latest = k_vals[-1]
            d_latest = d_vals[-1]
            j_latest = j_vals[-1]
            # KDJ金叉天数
            kdj_cross_days = 99
            for d in range(-1, -min(15, len(k_vals)) - 1, -1):
                if k_vals[d] > d_vals[d] and k_vals[d-1] <= d_vals[d-1]:
                    kdj_cross_days = abs(d + 1)
                    break
            # KDJ位置分级
            if k_latest < 20:
                kdj_position = "超卖"
            elif k_latest < 50:
                kdj_position = "低位"
            elif k_latest < 80:
                kdj_position = "中位"
            else:
                kdj_position = "超买"
            # KDJ钝化判断
            kdj_dunhua = False
            if k_latest > 80:
                count = sum(1 for kv in k_vals[-5:] if kv > 80)
                kdj_dunhua = count >= 4
            elif k_latest < 20:
                count = sum(1 for kv in k_vals[-5:] if kv < 20)
                kdj_dunhua = count >= 4
            results.append({
                "code": code,
                "close": round(latest_close, 2),
                "ma20": round(ma20, 2),
                "change_pct": round(change_pct, 2),
                "vol_ratio": round(vol_ratio, 2),
                "dif": round(dif[-1], 4),
                "dea": round(dea[-1], 4),
                "cross_days": cross_days,
                "dif_above_zero": dif[-1] > 0,
                "kdj_k": round(k_latest, 1),
                "kdj_d": round(d_latest, 1),
                "kdj_j": round(j_latest, 1),
                "kdj_cross_days": kdj_cross_days,
                "kdj_position": kdj_position,
                "kdj_dunhua": kdj_dunhua,
                "signal_type": "golden_cross",
            })
        except Exception:
            continue
    return results

# ══════════════════════════════════════════════════════════════
# 过滤 & 叠加实时行情
# ══════════════════════════════════════════════════════════════

def enrich_with_rt(results, rt_data):
    """叠加实时行情数据"""
    for r in results:
        rt = rt_data.get(r["code"], {})
        if rt:
            r["name"] = rt.get("name", "")
            r["price_now"] = rt.get("price", r["close"])
            r["change_pct_now"] = rt.get("change_pct", r["change_pct"])
            r["pe_ttm"] = rt.get("pe_ttm", 0)
            r["mcap_yi"] = rt.get("mcap_yi", 0)
    return results

def filter_early(results):
    """早期信号过滤（地量止跌/RSI拐头）"""
    valid = []
    for r in results:
        pe = r.get("pe_ttm", 0)
        mcap = r.get("mcap_yi", 0)
        if pe <= 0 or pe > 200:
            continue
        if mcap < 30 or mcap > 5000:
            continue
        valid.append(r)
    st = results[0].get("signal_type", "") if results else ""
    if st == "shrinkage":
        valid.sort(key=lambda x: (-x.get("shrink_days", 0), x.get("rsi6", 50)))
    else:
        valid.sort(key=lambda x: x.get("rsi6", 50))
    return valid

def filter_golden(results):
    """金叉信号过滤"""
    valid = []
    for r in results:
        pe = r.get("pe_ttm", 0)
        mcap = r.get("mcap_yi", 0)
        chg = r.get("change_pct_now", r["change_pct"])
        if pe <= 0 or pe > 200:
            continue
        if mcap < 30 or mcap > 5000:
            continue
        if chg < 2.0 or chg > 9.5:
            continue
        valid.append(r)
    valid.sort(key=lambda x: (x["cross_days"], -x.get("vol_ratio", 0)))
    return valid

# ══════════════════════════════════════════════════════════════
# 主流程
# ══════════════════════════════════════════════════════════════

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'data')
CACHE_FILE = os.path.join(CACHE_DIR, 'scan_results.json')

def main():
    print("=" * 60)
    print(f"🔍 A股全市场扫描器 V4.0 | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("数据源: 腾讯财经 HTTP 接口 | 零外部依赖")
    print("=" * 60)
    
    t0 = time.time()
    
    # ━━━ 1. 获取股票列表 ━━━
    print("\n[1/4] 获取A股股票列表...")
    stocks = get_stock_list()
    codes = list(stocks.keys())
    print(f"  获取 {len(codes)} 只（已过滤ST）")
    
    # ━━━ 2. 批量拉K线 ━━━
    print(f"\n[2/4] 分批拉取K线数据（{len(codes)}只）...")
    kline_data = {}
    batch_size = 30
    success = 0
    for i in range(0, min(len(codes), 5000), batch_size):
        batch = codes[i:i+batch_size]
        for code in batch:
            stock = stocks.get(code, {})
            market = stock.get("market", "sz")
            klines = fetch_klines(code, market, 45)
            if klines:
                kline_data[code] = klines
                success += 1
        if (i // batch_size + 1) % 10 == 0:
            print(f"  进度: {i+len(batch)}/{len(codes)} (获取{success}只有效)")
        time.sleep(0.15)
    print(f"  K线获取完成: {success}只有效数据")
    
    # ━━━ 3. 三层扫描 ━━━
    print("\n[3/4] 三层信号链路扫描...")
    codes_to_scan = list(kline_data.keys())
    
    print("  第一层: 地量止跌...")
    shrinkage_raw = scan_shrinkage_bottom(kline_data, codes_to_scan)
    print(f"    命中: {len(shrinkage_raw)}只")
    
    print("  第二层: RSI拐头...")
    rsi_raw = scan_rsi_turn(kline_data, codes_to_scan)
    print(f"    命中: {len(rsi_raw)}只")
    
    print("  第三层: MACD金叉...")
    golden_raw = scan_golden_cross(kline_data, codes_to_scan)
    print(f"    命中: {len(golden_raw)}只")
    
    # ━━━ 4. 实时行情叠加 & 保存 ━━━
    print("\n[4/4] 叠加实时行情 & 过滤...")
    all_codes = list(set(
        [r["code"] for r in shrinkage_raw]
        + [r["code"] for r in rsi_raw]
        + [r["code"] for r in golden_raw]
    ))
    rt_data = get_realtime_quotes(all_codes) if all_codes else {}
    
    shrinkage_valid = filter_early(enrich_with_rt(shrinkage_raw, rt_data))
    rsi_valid = filter_early(enrich_with_rt(rsi_raw, rt_data))
    golden_valid = filter_golden(enrich_with_rt(golden_raw, rt_data))
    
    total_t = time.time() - t0
    
    # ━━━ 打印报告 ━━━
    print(f"\n{'='*60}")
    print(f"📊 全市场三层扫描 | {len(codes)}只 | 耗时{total_t:.0f}s | 有效{success}只")
    print(f"{'='*60}")
    
    # 第一层
    print(f"\n### 📡 第一层：地量止跌（{len(shrinkage_valid)}只，卖盘枯竭变盘前夜）")
    if shrinkage_valid:
        for i, r in enumerate(shrinkage_valid[:8]):
            name = r.get("name", "")
            print(f"  {i+1}. {r['code']} {name} | "
                  f"收{r['close']:.2f} {r['change_pct']:+.1f}% | "
                  f"量比{r['vol_ratio']:.2f} | 振幅{r['amplitude']:.1f}% | "
                  f"缩量{r['shrink_days']}天 | RSI={r['rsi6']:.0f} | "
                  f"PE={r.get('pe_ttm',0):.0f} | {r.get('mcap_yi',0):.0f}亿")
    else:
        print("  ⚠️ 当前无符合条件标的")
    
    # 第二层
    print(f"\n### 📡 第二层：RSI拐头（{len(rsi_valid)}只，空方衰竭试探买入）")
    if rsi_valid:
        for i, r in enumerate(rsi_valid[:8]):
            name = r.get("name", "")
            print(f"  {i+1}. {r['code']} {name} | "
                  f"收{r['close']:.2f} {r['change_pct']:+.1f}% | "
                  f"RSI={r['rsi6_yesterday']:.0f}→{r['rsi6_today']:.0f} | "
                  f"DIF间距{r['dif_gap']:.3f} | 量比{r['vol_ratio']:.1f} | "
                  f"PE={r.get('pe_ttm',0):.0f}")
    else:
        print("  ⚠️ 当前无符合条件标的")
    
    # 第三层
    print(f"\n### 📡 第三层：MACD金叉确认（{len(golden_valid)}只，趋势确立）")
    groups = {
        "🟢 当天金叉 + 零轴上 + KDJ低位": [r for r in golden_valid if r["cross_days"] <= 0 and r["dif_above_zero"] and r.get("kdj_k",50) < 50],
        "🟡 当天金叉 + 零轴上 + KDJ中高位": [r for r in golden_valid if r["cross_days"] <= 0 and r["dif_above_zero"] and r.get("kdj_k",0) >= 50],
        "🟡 1-2天前金叉 + 零轴上": [r for r in golden_valid if 1 <= r["cross_days"] <= 2 and r["dif_above_zero"]],
        "🟠 3-5天前金叉 + 零轴上": [r for r in golden_valid if 3 <= r["cross_days"] <= 5 and r["dif_above_zero"]],
        "⚪ 零轴下金叉": [r for r in golden_valid if not r["dif_above_zero"]],
    }
    for label, group in groups.items():
        if not group:
            continue
        print(f"\n  {label}（{len(group)}只）")
        for i, r in enumerate(group):
            name = r.get("name", "")
            kdj_info = f"K={r['kdj_k']:.0f} D={r['kdj_d']:.0f} J={r['kdj_j']:.0f} | {r['kdj_position']}"
            if r['kdj_dunhua']:
                kdj_info += " ⚠️钝化"
            print(f"    {i+1}. {r['code']} {name} | "
                  f"涨{r['change_pct']:+.1f}% | 量比{r['vol_ratio']:.1f} | "
                  f"PE={r.get('pe_ttm',0):.0f} | 市值{r.get('mcap_yi',0):.0f}亿 | "
                  f"DIF={r['dif']:+.3f}")
            print(f"       📊 {kdj_info} | MACD金叉{r['cross_days']}天前")
    
    if not golden_valid:
        print("\n  ⚠️ 当前无符合条件标的")
    
    # ━━━ 保存JSON ━━━
    os.makedirs(CACHE_DIR, exist_ok=True)
    all_results = {
        "scan_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total_scanned": len(codes),
        "valid_klines": success,
        "elapsed_s": round(total_t, 0),
        "shrinkage": shrinkage_valid,
        "rsi_turn": rsi_valid,
        "golden_cross": golden_valid,
    }
    # 处理非标准类型
    def clean(obj):
        if isinstance(obj, dict):
            return {str(k): clean(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [clean(v) for v in obj]
        elif isinstance(obj, bool):
            return obj
        elif isinstance(obj, (int, float)):
            return obj
        return str(obj)
    with open(CACHE_FILE, 'w', encoding='utf-8') as f:
        json.dump(clean(all_results), f, ensure_ascii=False, indent=2)
    print(f"\n✓ 结果已保存: {CACHE_FILE}")
    print(f"  三层: {len(shrinkage_valid)}+{len(rsi_valid)}+{len(golden_valid)}只")
    
    return all_results

if __name__ == "__main__":
    main()
