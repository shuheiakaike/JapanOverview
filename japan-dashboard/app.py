from flask import Flask, render_template_string, jsonify, request
import yfinance as yf
import requests
import xml.etree.ElementTree as ET
import re
from datetime import datetime, timezone, timedelta
import os
import subprocess
import pathlib

CLAUDE_CLI = str(pathlib.Path.home() / ".local" / "bin" / "claude.exe")

app = Flask(__name__)

STOCKS = [
    {"ticker": "^N225",   "name": "日経平均",         "type": "index"},
    {"ticker": "1306.T",  "name": "TOPIX ETF",        "type": "index"},
    {"ticker": "JPY=X",   "name": "ドル/円",           "type": "fx"},
    {"ticker": "^TNX",    "name": "米10年債利回り",    "type": "bond"},
    {"ticker": "7203.T",  "name": "トヨタ",            "type": "stock"},
    {"ticker": "6758.T",  "name": "ソニー",            "type": "stock"},
    {"ticker": "9984.T",  "name": "ソフトバンクG",     "type": "stock"},
    {"ticker": "8306.T",  "name": "三菱UFJ",           "type": "stock"},
    {"ticker": "6861.T",  "name": "キーエンス",        "type": "stock"},
    {"ticker": "9432.T",  "name": "NTT",               "type": "stock"},
    {"ticker": "8035.T",  "name": "東京エレクトロン",  "type": "stock"},
]

DOMESTIC_FEEDS = [
    ("https://www3.nhk.or.jp/rss/news/cat3.xml", "NHK 経済"),
    ("https://www3.nhk.or.jp/rss/news/cat4.xml", "NHK 社会"),
]

OVERSEAS_FEEDS = [
    ("https://feeds.content.dowjones.io/public/rss/mw_realtimeheadlines", "MarketWatch"),
    ("https://www.reutersagency.com/feed/?best-topics=business-finance&post_type=best", "Reuters"),
    ("https://feeds.bbci.co.uk/news/business/rss.xml", "BBC"),
]

PERIOD_MAP = {
    "1D": ("1d",  "5m"),
    "1W": ("5d",  "1h"),
    "1M": ("1mo", "1d"),
    "3M": ("3mo", "1d"),
    "6M": ("6mo", "1d"),
    "1Y": ("1y",  "1wk"),
    "5Y": ("5y",  "1wk"),
}

PERIOD_LABELS = {
    "1D": "1日",
    "1W": "1週",
    "1M": "1ヶ月",
    "3M": "3ヶ月",
    "6M": "6ヶ月",
    "1Y": "1年",
    "5Y": "5年",
}

# Investment banking impact keywords
IB_HIGH_KW = [
    'm&a','買収','合併','ipo','上場廃止','上場','fed ','boj','日銀','利上げ','利下げ',
    '金利','gdp','決算','acquisition','merger','rate hike','rate cut','interest rate',
    'yield curve','inflation','recession','intervention','為替介入','景気後退','インフレ',
    'リセッション','central bank','中央銀行','quantitative','monetary policy','金融政策',
    '財政','budget','earnings','profit warning','bankruptcy','破綻','倒産',
    'fund raise','調達','hedge fund','ヘッジファンド','private equity','bond','社債','国債',
    'tariff','関税','sanctions','制裁','surplus','deficit','赤字','黒字','instability',
    'volatility','volatil','crash','crisi','shock','ショック','暴落','急騰','暴騰',
    'default','debt ceiling','財政','贸易戦','贸易战','trade war',
]
IB_MED_KW = [
    '株','market','市場','経済','economy','oil','原油','半導体','chip',
    '銀行','bank','finance','trade','輸出','energy','supply chain','nvidia','apple',
    'トヨタ','ソニー','invest','投資','nasdaq','dow','日経','topix','nikkei','yen',
    '円','dollar','ドル','tech','テクノロジー','ai ','人工知能','growth','成長',
    'profit','revenue','売上','企業','corporate','株価','index','指数',
]

def score_impact(article):
    text = (article.get('title', '') + ' ' + article.get('desc', '')).lower()
    score = (sum(2 for kw in IB_HIGH_KW if kw in text)
           + sum(1 for kw in IB_MED_KW  if kw in text))
    if score >= 4:
        return 'high'
    elif score >= 1:
        return 'medium'
    return 'low'

@app.route("/api/prices")
def api_prices():
    results = []
    for s in STOCKS:
        try:
            info = yf.Ticker(s["ticker"]).fast_info
            price = round(info.last_price, 2) if info.last_price else None
            prev  = info.previous_close
            if price and prev:
                change     = round(price - prev, 2)
                change_pct = round((change / prev) * 100, 2)
                positive   = change >= 0
            else:
                change = change_pct = None
                positive = None
        except:
            price = change = change_pct = None
            positive = None
        results.append({**s, "price": price, "change": change,
                        "change_pct": change_pct, "positive": positive})
    return jsonify(results)

@app.route("/api/chart/<path:ticker>/<period>")
def api_chart(ticker, period):
    yf_period, interval = PERIOD_MAP.get(period, ("1mo", "1d"))
    try:
        hist = yf.Ticker(ticker).history(period=yf_period, interval=interval)
        if hist.empty:
            return jsonify({"error": "No data"}), 404
        fmt = "%b %d %H:%M" if interval in ["5m","30m","1h"] else "%b %d"
        labels = [idx.strftime(fmt) for idx in hist.index]
        prices = [round(p, 2) for p in hist["Close"].tolist()]
        first, last = prices[0], prices[-1]
        change_pct = round(((last - first) / first) * 100, 2) if first else 0
        return jsonify({"labels": labels, "prices": prices,
                        "change_pct": change_pct, "positive": change_pct >= 0})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

import threading
_analyze_sem = threading.Semaphore(3)  # max 3 concurrent claude CLI calls

@app.route("/api/analyze", methods=["POST"])
def api_analyze():
    data  = request.get_json(silent=True) or {}
    title = data.get("title", "")
    desc  = data.get("desc",  "")
    size  = data.get("size",  "large")  # "large" = 3 bullets, "small" = 1 bullet

    if not title:
        return jsonify({"error": "no title"}), 400

    if size == "large":
        prompt = f"""以下のニュース記事について、投資銀行アナリストの視点から簡潔な日本語の分析を3つの箇条書きで提供してください。

記事タイトル: {title}
記事概要: {desc}

以下の3点について、それぞれ1文で答えてください：
1. このニュースに関わる主要な企業・組織・政府機関はどこか
2. このニュースが投資銀行業界に与える影響はどうか
3. 中規模の投資銀行がこのニュースを活用して収益機会を得るにはどうすればよいか

形式：
• [1の回答]
• [2の回答]
• [3の回答]

余計な前置きや説明は不要です。箇条書き3行のみ返してください。"""
    else:
        prompt = f"""以下のニュース記事について、投資銀行アナリストの視点から1文の日本語分析を提供してください。

記事タイトル: {title}

このニュースが投資銀行業界に与える影響について、簡潔な1文のみ返してください。前置きや説明は不要です。"""

    acquired = _analyze_sem.acquire(timeout=5)
    if not acquired:
        return jsonify({"error": "busy"}), 503
    try:
        result = subprocess.run(
            [CLAUDE_CLI, "-p", prompt, "--output-format", "text"],
            capture_output=True, text=True, timeout=45,
            encoding="utf-8", errors="replace",
            env={**os.environ, "NO_COLOR": "1"},
        )
        text = (result.stdout or "").strip()
        if not text:
            err = result.stderr.strip()
            return jsonify({"error": err or "empty response"}), 500
        return jsonify({"analysis": text})
    except subprocess.TimeoutExpired:
        return jsonify({"error": "timeout"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        _analyze_sem.release()


# ── WATCHLIST BACKEND ─────────────────────────────────────────────────

_JST = timezone(timedelta(hours=9))

def _safe_yf(symbol, digits=2):
    try:
        fi = yf.Ticker(symbol).fast_info
        p = fi.last_price
        prev = fi.previous_close
        if not p:
            return None
        chg = round(p - prev, digits) if prev else None
        pct = round((chg / prev) * 100, 2) if prev and chg is not None else None
        return {'v': round(p, digits), 'chg': chg, 'pct': pct}
    except Exception:
        return None

def _try_stooq(symbol, digits=3):
    """Fetch latest close price from stooq.com CSV API."""
    try:
        r = requests.get(
            f'https://stooq.com/q/d/l/?s={symbol}&i=d&l=1',
            timeout=7, headers={'User-Agent': 'Mozilla/5.0'}
        )
        lines = [l for l in r.text.strip().split('\n')
                 if l and not l.startswith('Date') and 'No data' not in l]
        if lines:
            parts = lines[-1].split(',')
            val = float(parts[4]) if len(parts) > 4 else None
            if val:
                return {'v': round(val, digits), 'chg': None, 'pct': None}
    except Exception:
        pass
    return None

def _try_stooq_chg(symbol, digits=2):
    """Fetch latest 2 rows from stooq to compute daily change."""
    try:
        r = requests.get(
            f'https://stooq.com/q/d/l/?s={symbol}&i=d&l=2',
            timeout=7, headers={'User-Agent': 'Mozilla/5.0'}
        )
        lines = [l for l in r.text.strip().split('\n')
                 if l and not l.startswith('Date') and 'No data' not in l]
        if len(lines) >= 2:
            prev = float(lines[-2].split(',')[4])
            curr = float(lines[-1].split(',')[4])
            chg  = round(curr - prev, digits)
            pct  = round(chg / prev * 100, 2) if prev else None
            return {'v': round(curr, digits), 'chg': chg, 'pct': pct}
        elif len(lines) == 1:
            val = float(lines[0].split(',')[4])
            return {'v': round(val, digits), 'chg': None, 'pct': None}
    except Exception:
        pass
    return None

def _try_fred(series_id, digits=3):
    """Fetch latest value from FRED (St. Louis Fed) CSV API."""
    try:
        r = requests.get(
            f'https://fred.stlouisfed.org/graph/fredgraph.csv?series_id={series_id}',
            timeout=8, headers={'User-Agent': 'Mozilla/5.0'}
        )
        lines = [l for l in r.text.strip().split('\n')
                 if l and not l.startswith('DATE') and not l.endswith(',.')
                 and not l.split(',')[-1].strip() == '.']
        if lines:
            parts = lines[-1].split(',')
            if len(parts) >= 2 and parts[1].strip() not in ('', '.'):
                val = float(parts[1].strip())
                return {'v': round(val, digits), 'chg': None, 'pct': None}
    except Exception:
        pass
    return None

_wl_cache = {'data': None, 'ts': 0}
_WL_TTL   = 4 * 60  # 4-minute cache

def _fetch_trkd_pid(pid):
    """Fetch TRKD Asia pagecontent callback and return parsed dict."""
    try:
        import json as _json
        r = requests.get(
            f'https://www.trkd-asia.com/rakutensecj/pagecontent?pid={pid}',
            timeout=10,
            headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
                'Referer': 'https://www.rakuten-sec.co.jp/',
                'Accept': '*/*',
            }
        )
        m = re.search(r'\w+\(([\s\S]*)\)', r.text)
        if not m:
            return None
        return _json.loads(m.group(1))
    except Exception:
        return None

def _trkd_by_ric(rows, ric):
    """Return the first row that contains `ric` as an exact field value."""
    ric_lower = ric.lower()
    for row in (rows or []):
        for cell in row:
            if isinstance(cell, str) and cell.lower() == ric_lower:
                return row
    return None

def _trkd_val(row, digits=2):
    """Extract {v, chg, pct} from a TRKD row. Value is always row[2];
    chg/pct are row[4]/row[5] when they are numeric (7-field rows)."""
    if not row or len(row) < 3:
        return None
    try:
        v = float(row[2]) if str(row[2]) not in ('', '-', '--') else None
        if v is None:
            return None
        chg = pct = None
        if len(row) > 5:
            if str(row[4]) not in ('', '-', '--'):
                try: chg = round(float(row[4]), digits)
                except Exception: pass
            if str(row[5]) not in ('', '-', '--'):
                try: pct = round(float(row[5]), 2)
                except Exception: pass
        return {'v': round(v, digits), 'chg': chg, 'pct': pct}
    except Exception:
        return None

def fetch_watchlist():
    import time
    from concurrent.futures import ThreadPoolExecutor, as_completed

    now = time.time()
    if _wl_cache['data'] and now - _wl_cache['ts'] < _WL_TTL:
        return _wl_cache['data']

    fetched_at = datetime.now(_JST).strftime('%Y-%m-%d %H:%M jst')

    # Tickers to fetch in parallel via yfinance
    # TOPIX/JPX400/Growth250/REIT → TRKD API (pid=101)
    yf_tasks = {
        'nikkei':    ('^N225',     2),
        'dow':       ('^DJI',      2),
        'nasdaq':    ('^IXIC',     2),
        'eurostoxx': ('^STOXX50E', 2),
        'shanghai':  ('000001.SS', 2),
        'usdjpy':    ('JPY=X',     2),
        'eurjpy':    ('EURJPY=X',  2),
        'us10y':     ('^TNX',      3),
        'crude':     ('CL=F',      2),
        'gold':      ('GC=F',      2),
        'crb':       ('^TRCCRB',   2),
    }

    d = {k: None for k in list(yf_tasks.keys()) + [
        'topix', 'jpx400', 'growth250', 'reit',
        'tse_prime_vol', 'tse_prime_cap',
        'nikkei_per', 'nikkei_pbr', 'nikkei_yield',
        'jp10y', 'call_rate'
    ]}

    def _fetch(key, sym, digits):
        return key, _safe_yf(sym, digits)

    with ThreadPoolExecutor(max_workers=14) as ex:
        # yfinance tasks
        yf_futs = {ex.submit(_fetch, k, sym, dig): k
                   for k, (sym, dig) in yf_tasks.items()}
        # TRKD tasks in parallel
        trkd101 = ex.submit(_fetch_trkd_pid, 101)
        trkd104 = ex.submit(_fetch_trkd_pid, 104)
        trkd106 = ex.submit(_fetch_trkd_pid, 106)

        for f in as_completed(yf_futs):
            try:
                key, val = f.result(timeout=15)
                d[key] = val
            except Exception:
                pass

        # pid=101: actual index prices (TOPIX, JPX400, Growth250, REIT)
        # RICs: .TOPX, .JPXNK400, .MTHR, .TREIT
        try:
            idx = trkd101.result(timeout=15)
            if idx:
                rows = idx.get('list', [])
                d['topix']     = _trkd_val(_trkd_by_ric(rows, '.TOPX'),     2)
                d['jpx400']    = _trkd_val(_trkd_by_ric(rows, '.JPXNK400'), 2)
                d['growth250'] = _trkd_val(_trkd_by_ric(rows, '.MTHR'),     2)
                d['reit']      = _trkd_val(_trkd_by_ric(rows, '.TREIT'),    2)
        except Exception:
            pass

        # pid=104: Japan 10Y bond yield (RIC: JP10YT=XX)
        try:
            bond = trkd104.result(timeout=15)
            if bond:
                rows = bond.get('list', [])
                d['jp10y'] = _trkd_val(_trkd_by_ric(rows, 'JP10YT=XX'), 3)
        except Exception:
            pass

        # pid=106: overnight call rate (RIC: JPONMU=RR)
        try:
            rate = trkd106.result(timeout=15)
            if rate:
                rows = rate.get('list', [])
                row = _trkd_by_ric(rows, 'JPONMU=RR')
                if row and len(row) >= 3 and str(row[2]) not in ('', '-', '--'):
                    d['call_rate'] = {'v': round(float(row[2]), 3), 'chg': None, 'pct': None}
        except Exception:
            pass

    # Nikkei PER/PBR/yield
    try:
        r = requests.get(
            'https://query1.finance.yahoo.com/v10/finance/quoteSummary/%5EN225'
            '?modules=summaryDetail,defaultKeyStatistics',
            timeout=8, headers={'User-Agent': 'Mozilla/5.0'}
        )
        res = r.json().get('quoteSummary', {}).get('result', [{}])[0]
        sd  = res.get('summaryDetail', {})
        per_raw = (sd.get('trailingPE') or {}).get('raw')
        pbr_raw = (sd.get('priceToBook') or {}).get('raw')
        yld_raw = (sd.get('dividendYield') or {}).get('raw')
        if per_raw: d['nikkei_per']   = round(per_raw, 2)
        if pbr_raw: d['nikkei_pbr']   = round(pbr_raw, 2)
        if yld_raw: d['nikkei_yield'] = round(yld_raw * 100, 2)
    except Exception:
        pass

    d['fetched_at'] = fetched_at
    _wl_cache['data'] = d
    _wl_cache['ts']   = now
    return d


@app.route('/api/watchlist')
def api_watchlist():
    return jsonify(fetch_watchlist())


def get_image(item):
    for ns in ["http://search.yahoo.com/mrss/", "http://www.w3.org/2005/Atom"]:
        mc = item.find(f"{{{ns}}}content")
        if mc is not None and mc.get("url"):
            return mc.get("url")
        mc = item.find(f"{{{ns}}}thumbnail")
        if mc is not None and mc.get("url"):
            return mc.get("url")
    enc = item.find("enclosure")
    if enc is not None and "image" in enc.get("type", ""):
        return enc.get("url")
    return None

_news_cache: dict = {}
_NEWS_TTL = 5 * 60  # 5-minute news cache

def fetch_feed_group(feeds):
    import time
    cache_key = tuple(url for url, _ in feeds)
    entry = _news_cache.get(cache_key)
    if entry and time.time() - entry['ts'] < _NEWS_TTL:
        return entry['data']

    articles = []
    for url, source in feeds:
        try:
            r = requests.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
            root = ET.fromstring(r.content)
            for item in root.findall(".//item")[:10]:
                title = item.findtext("title", "").strip()
                link  = item.findtext("link",  "").strip()
                pub   = item.findtext("pubDate","").strip()[:25]
                desc  = re.sub("<[^<]+?>","", item.findtext("description",""))[:300].strip()
                img   = get_image(item)
                if title and link:
                    a = {"title": title, "link": link, "pub": pub,
                         "source": source, "desc": desc, "img": img}
                    a["impact"] = score_impact(a)
                    articles.append(a)
        except:
            continue
    # Sort: high first, then medium, then low
    order = {"high": 0, "medium": 1, "low": 2}
    articles.sort(key=lambda x: order.get(x["impact"], 2))

    # Deduplicate by title similarity (Jaccard overlap > 40%)
    def _fp(title):
        words = re.sub(r'[^\w\s]', '', title.lower()).split()
        return frozenset(w for w in words if len(w) > 2)

    seen, deduped = [], []
    for a in articles:
        fp = _fp(a.get('title', ''))
        if any(s and len(fp & s) / max(len(fp | s), 1) > 0.4 for s in seen):
            continue
        deduped.append(a)
        seen.append(fp)

    _news_cache[cache_key] = {'data': deduped, 'ts': time.time()}
    return deduped


def limit_news(articles, max_large=3, max_small=5):
    """Cap large (high+med) to max_large and small (low) to max_small."""
    large = [a for a in articles if a['impact'] in ('high', 'medium')][:max_large]
    small = [a for a in articles if a['impact'] == 'low'][:max_small]
    return large + small

HTML = r"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>日本市場ダッシュボード</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
/* ── THEME VARIABLES ──────────────────────────────────────── */
:root {
  --bg:          #07090f;
  --bg-card:     #0d1117;
  --bg-card2:    #0f1520;
  --bg-hover:    #161d2a;
  --border:      #1c2333;
  --border2:     #2a3446;
  --txt:         #e6edf3;
  --txt-sub:     #8b949e;
  --txt-dim:     #3d4a5c;
  --txt-head:    #f0f6fc;
  --accent:      #58a6ff;
  --accent2:     #1f6feb;
  --up:          #3fb950;
  --down:        #f85149;
  --up-glow:     rgba(63,185,80,.15);
  --down-glow:   rgba(248,81,73,.15);
  --chart-grid:  rgba(255,214,0,.18);
  --chart-tick:  #ffd600;
  --tip-bg:      #0d1117;
  --tip-bdr:     #2a3446;
  --tip-title:   #8b949e;
  --tip-body:    #f0f6fc;
  --sb-bg:       #0d1117;
  --sb-thumb:    #2a3446;
  --hdr-bg:      rgba(7,9,15,.95);
  --badge-high:  #f85149;
  --badge-med:   #e3b341;
  --badge-low:   #3d4a5c;
}
body.light {
  --bg:          #f0f2f5;
  --bg-card:     #ffffff;
  --bg-card2:    #f8fafc;
  --bg-hover:    #eef1f6;
  --border:      #d0d7de;
  --border2:     #adb5c2;
  --txt:         #1a2332;
  --txt-sub:     #4a5568;
  --txt-dim:     #9aa5b4;
  --txt-head:    #0d1117;
  --accent:      #0969da;
  --accent2:     #0550ae;
  --up:          #1a7f37;
  --down:        #c2161b;
  --up-glow:     rgba(26,127,55,.12);
  --down-glow:   rgba(194,22,27,.12);
  --chart-grid:  #eaecf0;
  --chart-tick:  #9aa5b4;
  --tip-bg:      #ffffff;
  --tip-bdr:     #d0d7de;
  --tip-title:   #4a5568;
  --tip-body:    #0d1117;
  --sb-bg:       #f0f2f5;
  --sb-thumb:    #adb5c2;
  --hdr-bg:      rgba(240,242,245,.96);
  --badge-high:  #c2161b;
  --badge-med:   #8a6914;
  --badge-low:   #9aa5b4;
}

/* ── RESET ───────────────────────────────────────────────── */
*, *::before, *::after { margin:0; padding:0; box-sizing:border-box }
html { scroll-behavior: smooth }
body {
  background: var(--bg);
  color: var(--txt);
  font-family: 'Hiragino Sans','Yu Gothic UI','Meiryo','Segoe UI',system-ui,sans-serif;
  min-height: 100vh;
  transition: background .25s, color .25s;
}
::-webkit-scrollbar { width:5px; background:var(--sb-bg) }
::-webkit-scrollbar-thumb { background:var(--sb-thumb); border-radius:3px }
a { text-decoration:none; color:inherit }

/* ── STICKY HEADER ───────────────────────────────────────── */
header {
  position: sticky;
  top: 0;
  z-index: 200;
  backdrop-filter: blur(18px);
  -webkit-backdrop-filter: blur(18px);
  background: var(--hdr-bg);
  border-bottom: 1px solid var(--border);
  padding: 0 32px;
  height: 58px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  transition: background .25s, border-color .25s;
}
.brand {
  display: flex;
  align-items: baseline;
  gap: 14px;
}
.brand h1 {
  font-size: 20px;
  font-weight: 800;
  letter-spacing: -.5px;
  background: linear-gradient(110deg, var(--accent) 0%, #a5d6ff 60%, var(--accent) 100%);
  background-size: 200% auto;
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
  background-clip: text;
  animation: shimmer 4s linear infinite;
}
@keyframes shimmer { to { background-position: 200% center } }
.brand-date {
  font-size: 11px;
  color: var(--txt-dim);
  font-weight: 500;
}
.header-right { display: flex; align-items: center; gap: 14px }
.last-upd { font-size: 11px; color: var(--txt-dim) }

/* Toggle */
.theme-toggle { display:flex; align-items:center; gap:7px; cursor:pointer; user-select:none }
.theme-toggle span { font-size:11px; color:var(--txt-sub) }
.theme-toggle input { display:none }
.tog-track {
  width:38px; height:21px; border-radius:11px;
  background:var(--border2); position:relative; transition:background .2s; flex-shrink:0;
}
.theme-toggle input:checked ~ .tog-track { background:var(--accent2) }
.tog-thumb {
  width:15px; height:15px; border-radius:50%; background:#fff;
  position:absolute; top:3px; left:3px; transition:left .2s; pointer-events:none;
}
.theme-toggle input:checked ~ .tog-track .tog-thumb { left:20px }

.refresh-btn {
  background: transparent;
  border: 1px solid var(--border2);
  color: var(--txt-sub);
  padding: 5px 13px;
  border-radius: 6px;
  cursor: pointer;
  font-size: 12px;
  font-family: inherit;
  transition: .15s;
}
.refresh-btn:hover { background:var(--bg-hover); color:var(--txt) }

/* ── PAGE GRID ───────────────────────────────────────────── */
.page-body {
  display: grid;
  grid-template-columns: 1fr 1fr;
  align-items: start;
  min-height: calc(100vh - 58px);
}

/* ── LEFT PANEL ──────────────────────────────────────────── */
.left-panel {
  border-right: 1px solid var(--border);
  padding: 28px 28px 60px;
  display: flex;
  flex-direction: column;
  gap: 32px;
}

/* ── SECTION LABEL ───────────────────────────────────────── */
.sec-label {
  font-size: 10px;
  font-weight: 700;
  color: var(--txt-dim);
  text-transform: uppercase;
  letter-spacing: 2px;
  margin-bottom: 18px;
  display: flex;
  align-items: center;
  gap: 10px;
}
.sec-label::after { content:''; flex:1; height:1px; background:var(--border) }

/* ── HERO CHART ──────────────────────────────────────────── */
.hero-chart-wrap {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: 16px;
  overflow: hidden;
  transition: background .25s, border-color .25s;
}
.hero-chart-top {
  padding: 24px 28px 0;
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 12px;
}
.hero-name { font-size: 13px; color: var(--txt-sub); font-weight: 600; letter-spacing: .5px; margin-bottom:6px }
.hero-price {
  font-size: 52px;
  font-weight: 800;
  letter-spacing: -2px;
  line-height: 1;
  color: var(--txt-head);
  transition: color .25s;
}
.hero-change { font-size: 18px; font-weight: 600; margin-top: 6px }
.period-btns { display:flex; gap:5px; flex-wrap:wrap; align-items:flex-start; padding-top:4px }
.period-btn {
  background: transparent;
  border: 1px solid var(--border2);
  color: var(--txt-sub);
  padding: 5px 13px;
  border-radius: 6px;
  cursor: pointer;
  font-size: 12px;
  font-family: inherit;
  font-weight: 600;
  transition: .15s;
}
.period-btn:hover { color:var(--txt); border-color:var(--txt-sub) }
.period-btn.active { background:var(--accent2); border-color:var(--accent2); color:#fff }
.hero-canvas-wrap { position:relative; height:340px; padding:12px 0 0 }

/* ── STOCK QUICK-SELECT BUTTONS ──────────────────────────── */
.stock-btns {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  padding: 14px 28px 18px;
  border-top: 1px solid var(--border);
}
.stock-btn {
  background: transparent;
  border: 1px solid var(--border2);
  color: var(--txt-sub);
  padding: 4px 11px;
  border-radius: 20px;
  cursor: pointer;
  font-size: 11px;
  font-weight: 600;
  font-family: inherit;
  transition: background .15s, color .15s, border-color .15s;
  white-space: nowrap;
}
.stock-btn:hover { background: var(--bg-hover); color: var(--txt); border-color: var(--txt-sub) }
.stock-btn.active { background: var(--accent2); border-color: var(--accent2); color: #fff }

/* ── STOCK CARD GRID ─────────────────────────────────────── */
.stock-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 14px;
}
.stock-card {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: 14px;
  padding: 18px 18px 14px;
  cursor: pointer;
  transition: border-color .15s, background .15s, transform .15s, box-shadow .15s;
  overflow: hidden;
  position: relative;
}
.stock-card::before {
  content: '';
  position: absolute;
  inset: 0;
  border-radius: 14px;
  opacity: 0;
  transition: opacity .2s;
  pointer-events: none;
}
.stock-card.up-card::before   { background: var(--up-glow) }
.stock-card.down-card::before { background: var(--down-glow) }
.stock-card:hover { border-color:var(--border2); transform:translateY(-2px); box-shadow:0 8px 24px rgba(0,0,0,.25) }
.stock-card:hover::before { opacity:1 }
.stock-card.active { border-color: var(--accent) }
.sc-top { display:flex; justify-content:space-between; align-items:flex-start; margin-bottom:4px }
.sc-name { font-size:12px; font-weight:700; color:var(--txt-sub) }
.sc-ticker { font-size:10px; color:var(--txt-dim); margin-top:1px }
.sc-badge {
  font-size:9px; font-weight:700; letter-spacing:.6px;
  padding:2px 6px; border-radius:4px; text-transform:uppercase;
}
.sc-badge.type-index { background:rgba(88,166,255,.15); color:var(--accent) }
.sc-badge.type-stock { background:rgba(63,185,80,.12); color:var(--up) }
.sc-badge.type-fx    { background:rgba(227,179,65,.12); color:#e3b341 }
.sc-badge.type-bond  { background:rgba(188,140,255,.12); color:#bc8cff }
.sc-price { font-size:28px; font-weight:800; letter-spacing:-1px; margin:10px 0 2px; line-height:1 }
.sc-change { font-size:12px; font-weight:600 }
.up   { color:var(--up) }
.down { color:var(--down) }
.neutral { color:var(--txt-sub) }
.sc-chart-wrap { position:relative; height:140px; margin-top:12px }

/* ── RIGHT PANEL ─────────────────────────────────────────── */
.right-panel {
  display: flex;
  flex-direction: column;
}

/* ── NEWS PANE ───────────────────────────────────────────── */
.news-pane {
  padding: 28px 28px 40px;
  border-bottom: 2px solid var(--border);
}
.news-pane:last-child { border-bottom: none }

/* ── NEWS CARD — HIGH IMPACT ─────────────────────────────── */
.nc-high {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-left: 4px solid var(--badge-high);
  border-radius: 14px;
  overflow: hidden;
  margin-bottom: 22px;
  transition: border-color .15s, transform .15s, box-shadow .15s;
}
.nc-high:hover { transform:translateY(-3px); box-shadow:0 12px 32px rgba(0,0,0,.3); border-color:var(--badge-high) }
.nc-high a { display:flex; flex-direction:column }
.nc-high .nc-img { width:100%; height:240px; object-fit:cover; background:var(--bg-card2); display:block }
.nc-high .nc-img-ph {
  width:100%; height:240px; background: linear-gradient(135deg,#0d1f3c,#1a3566);
  display:flex; align-items:center; justify-content:center; font-size:48px;
}
.nc-high .nc-body { padding:20px 22px 18px }
.nc-high .nc-impact-badge {
  display:inline-flex; align-items:center; gap:5px;
  font-size:10px; font-weight:800; color:var(--badge-high);
  letter-spacing:1.5px; text-transform:uppercase; margin-bottom:10px;
}
.nc-high .nc-impact-badge::before { content:''; width:6px; height:6px; border-radius:50%; background:var(--badge-high); animation:pulse 1.5s ease-in-out infinite }
@keyframes pulse { 0%,100%{opacity:1;transform:scale(1)} 50%{opacity:.5;transform:scale(.8)} }
.nc-high .nc-source { font-size:11px; font-weight:700; color:var(--accent); letter-spacing:.8px; text-transform:uppercase; margin-bottom:8px }
.nc-high .nc-title { font-size:26px; font-weight:800; line-height:1.3; color:var(--txt-head); margin-bottom:10px; letter-spacing:-.3px }
.nc-high .nc-desc { font-size:14px; color:var(--txt-sub); line-height:1.7 }
.nc-high .nc-date { font-size:11px; color:var(--txt-dim); margin-top:12px }

/* ── NEWS CARD — MEDIUM IMPACT ───────────────────────────── */
.nc-med {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-left: 3px solid var(--badge-med);
  border-radius: 12px;
  overflow: hidden;
  margin-bottom: 14px;
  transition: border-color .15s, transform .15s, box-shadow .15s;
}
.nc-med:hover { transform:translateY(-2px); box-shadow:0 8px 20px rgba(0,0,0,.25) }
.nc-med a { display:flex; align-items:stretch }
.nc-med .nc-img { width:160px; min-width:160px; object-fit:cover; background:var(--bg-card2) }
.nc-med .nc-img-ph {
  width:160px; min-width:160px; height:100%;
  background:linear-gradient(135deg,#0d2340,#1a3a5c);
  display:flex; align-items:center; justify-content:center; font-size:30px;
}
.nc-med .nc-body { padding:14px 16px; flex:1; display:flex; flex-direction:column; gap:5px; min-width:0 }
.nc-med .nc-source { font-size:10px; font-weight:700; color:var(--accent); letter-spacing:.8px; text-transform:uppercase }
.nc-med .nc-badge-row { display:flex; align-items:center; gap:8px; margin-bottom:3px }
.nc-med .nc-impact-badge {
  font-size:9px; font-weight:800; color:var(--badge-med);
  letter-spacing:1px; text-transform:uppercase;
  border:1px solid var(--badge-med); border-radius:3px; padding:1px 5px;
}
.nc-med .nc-title {
  font-size:17px; font-weight:700; color:var(--txt-head); line-height:1.4;
  display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden;
}
.nc-med .nc-desc {
  font-size:12px; color:var(--txt-sub); line-height:1.6;
  display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden;
}
.nc-med .nc-date { font-size:10px; color:var(--txt-dim); margin-top:auto; padding-top:6px }

/* ── NEWS CARD — LOW IMPACT ──────────────────────────────── */
.nc-low {
  background: var(--bg-card2);
  border: 1px solid var(--border);
  border-radius: 8px;
  overflow: hidden;
  margin-bottom: 8px;
  transition: border-color .15s, background .15s;
}
.nc-low:hover { border-color:var(--border2); background:var(--bg-hover) }
.nc-low a { display:flex; align-items:center; gap:12px; padding:10px 14px }
.nc-low .nc-img { width:64px; min-width:64px; height:48px; object-fit:cover; border-radius:6px; background:var(--border) }
.nc-low .nc-img-ph {
  width:64px; min-width:64px; height:48px; border-radius:6px;
  background:var(--bg-hover); display:flex; align-items:center; justify-content:center; font-size:18px;
}
.nc-low .nc-body { flex:1; min-width:0 }
.nc-low .nc-source { font-size:9px; font-weight:700; color:var(--txt-dim); text-transform:uppercase; letter-spacing:.6px }
.nc-low .nc-title {
  font-size:13px; font-weight:600; color:var(--txt);
  display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden; line-height:1.4;
}
.nc-low .nc-date { font-size:9px; color:var(--txt-dim); margin-top:3px }
.nc-low .nc-arrow { color:var(--txt-dim); font-size:14px; flex-shrink:0 }

/* ── NEWS GROUP DIVIDER ──────────────────────────────────── */
.news-group-label {
  font-size:9px; font-weight:700; color:var(--txt-dim); letter-spacing:2px;
  text-transform:uppercase; margin:18px 0 10px; display:flex; align-items:center; gap:8px;
}
.news-group-label::after { content:''; flex:1; height:1px; background:var(--border) }

.no-news { color:var(--txt-dim); font-size:14px; padding:24px 0 }

/* ── AI ANALYSIS ──────────────────────────────────────────── */
.ai-analysis {
  padding: 10px 14px 12px;
  border-top: 1px solid var(--border);
  background: var(--bg-card2);
}
.ai-analysis-header {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 7px;
  font-size: 10px;
  font-weight: 700;
  letter-spacing: .06em;
  text-transform: uppercase;
  color: var(--accent);
  opacity: .85;
}
.ai-analysis-header::before {
  content: '✦';
  font-size: 9px;
}
.ai-bullet {
  font-size: 12px;
  color: var(--txt-sub);
  line-height: 1.55;
  padding: 2px 0 2px 14px;
  position: relative;
}
.ai-bullet::before {
  content: '•';
  position: absolute;
  left: 2px;
  color: var(--accent);
  opacity: .7;
}
.ai-loading {
  font-size: 11px;
  color: var(--txt-dim);
  padding: 4px 0;
  display: flex;
  align-items: center;
  gap: 6px;
}
.ai-loading::before {
  content: '';
  width: 10px; height: 10px;
  border: 1.5px solid var(--accent);
  border-top-color: transparent;
  border-radius: 50%;
  display: inline-block;
  animation: spin .8s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg) } }

/* ── RESPONSIVE ──────────────────────────────────────────── */
@media (max-width:900px) {
  .page-body { grid-template-columns:1fr }
  .left-panel { border-right:none; border-bottom:2px solid var(--border) }
  .stock-grid { grid-template-columns:1fr 1fr }
  .hero-price { font-size:36px }
  header { padding:0 16px }
  .left-panel, .news-pane { padding-left:16px; padding-right:16px }
}
@media (max-width:560px) {
  .stock-grid { grid-template-columns:1fr }
  .nc-med .nc-img, .nc-med .nc-img-ph { width:100px; min-width:100px }
}
</style>
</head>
<body>

<!-- ════════════════ HEADER ════════════════ -->
<header>
  <div class="brand">
    <h1>日本市場ダッシュボード</h1>
    <span class="brand-date" id="brandDate"></span>
  </div>
  <div class="header-right">
    <span class="last-upd" id="lastUpd">読み込み中…</span>
    <label class="theme-toggle" title="ライト／ダークモード切替">
      <input type="checkbox" id="themeToggle" onchange="toggleTheme()">
      <div class="tog-track"><div class="tog-thumb"></div></div>
      <span>ライトモード</span>
    </label>
    <button class="refresh-btn" onclick="location.reload()">⟳ 更新</button>
    <a href="/watchlist" style="font-size:12px;padding:5px 11px;border:1px solid var(--border);border-radius:6px;color:var(--txt-sub);text-decoration:none;transition:color .2s,border-color .2s" onmouseover="this.style.color='var(--accent)';this.style.borderColor='var(--accent)'" onmouseout="this.style.color='var(--txt-sub)';this.style.borderColor='var(--border)'">📊 ウォッチリスト</a>
  </div>
</header>

<div class="page-body">

  <!-- ════════════ LEFT — MARKET PANEL ════════════ -->
  <div class="left-panel">

    <!-- HERO CHART -->
    <section>
      <div class="sec-label">市場チャート</div>
      <div class="hero-chart-wrap">
        <div class="hero-chart-top">
          <div>
            <div class="hero-name" id="heroName">日経平均</div>
            <div class="hero-price" id="heroPrice">—</div>
            <div class="hero-change" id="heroChange"></div>
          </div>
          <div class="period-btns">
            {% for p, label in period_labels.items() %}
            <button class="period-btn {% if p == '1M' %}active{% endif %}"
                    data-period="{{ p }}" onclick="setPeriod('{{ p }}')">{{ label }}</button>
            {% endfor %}
          </div>
        </div>
        <div class="hero-canvas-wrap"><canvas id="mainChart"></canvas></div>
        <div class="stock-btns">
          {% for s in stocks %}
          <button class="stock-btn {% if loop.first %}active{% endif %}"
                  id="sbtn-{{ loop.index0 }}"
                  onclick="selectStock('{{ s.ticker }}','{{ s.name }}',{{ loop.index0 }})">{{ s.name }}</button>
          {% endfor %}
        </div>
      </div>
    </section>

    <!-- STOCK CARDS -->
    <section>
      <div class="sec-label">銘柄・指数一覧</div>
      <div class="stock-grid">
        {% for s in stocks %}
        <div class="price-card stock-card" id="card-{{ loop.index0 }}"
             onclick="selectStock('{{ s.ticker }}','{{ s.name }}',{{ loop.index0 }})">
          <div class="sc-top">
            <div>
              <div class="sc-name">{{ s.name }}</div>
              <div class="sc-ticker">{{ s.ticker }}</div>
            </div>
            <span class="sc-badge type-{{ s.type }}">{{ {'index':'指数','stock':'株式','fx':'為替','bond':'債券'}.get(s.type, s.type) }}</span>
          </div>
          <div class="sc-price" id="price-{{ loop.index0 }}">—</div>
          <div class="sc-change" id="change-{{ loop.index0 }}">—</div>
          <div class="sc-chart-wrap"><canvas id="mini-{{ loop.index0 }}"></canvas></div>
        </div>
        {% endfor %}
      </div>
    </section>

  </div><!-- /left-panel -->

  <!-- ════════════ RIGHT — NEWS PANEL ════════════ -->
  <div class="right-panel">

    <!-- TOP-RIGHT: DOMESTIC -->
    <div class="news-pane">
      <div class="sec-label">国内ニュース</div>

      {% set dom_high = domestic_news | selectattr('impact','eq','high') | list %}
      {% set dom_med  = domestic_news | selectattr('impact','eq','medium') | list %}
      {% set dom_low  = domestic_news | selectattr('impact','eq','low')  | list %}

      {% for a in dom_high %}
      <div class="nc-high">
        <a href="{{ a.link }}" target="_blank" rel="noopener">
          {% if a.img %}<img class="nc-img" src="{{ a.img }}" alt="" loading="lazy" onerror="this.style.display='none'">
          {% else %}<div class="nc-img-ph">{{ {'NHK 経済':'📊','NHK 社会':'📰'}.get(a.source,'🗞️') }}</div>{% endif %}
          <div class="nc-body">
            <div class="nc-impact-badge">重要度：高</div>
            <div class="nc-source">{{ a.source }}</div>
            <div class="nc-title">{{ a.title }}</div>
            <div class="nc-desc">{{ a.desc }}</div>
            <div class="nc-date">{{ a.pub }}</div>
          </div>
        </a>
        <div class="ai-analysis" data-title="{{ a.title|e }}" data-desc="{{ a.desc|e }}" data-size="large"></div>
      </div>
      {% endfor %}

      {% if dom_med %}
      {% if dom_high %}<div class="news-group-label">注目ニュース</div>{% endif %}
      {% for a in dom_med %}
      <div class="nc-med">
        <a href="{{ a.link }}" target="_blank" rel="noopener">
          {% if a.img %}<img class="nc-img" src="{{ a.img }}" alt="" loading="lazy" onerror="this.style.display='none'">
          {% else %}<div class="nc-img-ph">{{ {'NHK 経済':'📊','NHK 社会':'📰'}.get(a.source,'🗞️') }}</div>{% endif %}
          <div class="nc-body">
            <div class="nc-badge-row">
              <div class="nc-source">{{ a.source }}</div>
              <div class="nc-impact-badge">注目</div>
            </div>
            <div class="nc-title">{{ a.title }}</div>
            <div class="nc-desc">{{ a.desc }}</div>
            <div class="nc-date">{{ a.pub }}</div>
          </div>
        </a>
        <div class="ai-analysis" data-title="{{ a.title|e }}" data-desc="{{ a.desc|e }}" data-size="large"></div>
      </div>
      {% endfor %}
      {% endif %}

      {% if dom_low %}
      {% if dom_high or dom_med %}<div class="news-group-label">その他</div>{% endif %}
      {% for a in dom_low %}
      <div class="nc-low">
        <a href="{{ a.link }}" target="_blank" rel="noopener">
          {% if a.img %}<img class="nc-img" src="{{ a.img }}" alt="" loading="lazy" onerror="this.style.display='none'">
          {% else %}<div class="nc-img-ph">{{ {'NHK 経済':'📊','NHK 社会':'📰'}.get(a.source,'🗞️') }}</div>{% endif %}
          <div class="nc-body">
            <div class="nc-source">{{ a.source }}</div>
            <div class="nc-title">{{ a.title }}</div>
            <div class="nc-date">{{ a.pub }}</div>
          </div>
          <span class="nc-arrow">›</span>
        </a>
        <div class="ai-analysis" data-title="{{ a.title|e }}" data-size="small"></div>
      </div>
      {% endfor %}
      {% endif %}

      {% if not domestic_news %}<div class="no-news">国内ニュースを取得できませんでした</div>{% endif %}
    </div>

    <!-- BOTTOM-RIGHT: OVERSEAS -->
    <div class="news-pane">
      <div class="sec-label">海外ニュース</div>

      {% set ov_high = overseas_news | selectattr('impact','eq','high') | list %}
      {% set ov_med  = overseas_news | selectattr('impact','eq','medium') | list %}
      {% set ov_low  = overseas_news | selectattr('impact','eq','low')  | list %}

      {% for a in ov_high %}
      <div class="nc-high">
        <a href="{{ a.link }}" target="_blank" rel="noopener">
          {% if a.img %}<img class="nc-img" src="{{ a.img }}" alt="" loading="lazy" onerror="this.style.display='none'">
          {% else %}<div class="nc-img-ph">{{ {'MarketWatch':'📈','Reuters':'🌐','BBC':'📰'}.get(a.source,'📊') }}</div>{% endif %}
          <div class="nc-body">
            <div class="nc-impact-badge">重要度：高</div>
            <div class="nc-source">{{ a.source }}</div>
            <div class="nc-title">{{ a.title }}</div>
            <div class="nc-desc">{{ a.desc }}</div>
            <div class="nc-date">{{ a.pub }}</div>
          </div>
        </a>
        <div class="ai-analysis" data-title="{{ a.title|e }}" data-desc="{{ a.desc|e }}" data-size="large"></div>
      </div>
      {% endfor %}

      {% if ov_med %}
      {% if ov_high %}<div class="news-group-label">注目ニュース</div>{% endif %}
      {% for a in ov_med %}
      <div class="nc-med">
        <a href="{{ a.link }}" target="_blank" rel="noopener">
          {% if a.img %}<img class="nc-img" src="{{ a.img }}" alt="" loading="lazy" onerror="this.style.display='none'">
          {% else %}<div class="nc-img-ph">{{ {'MarketWatch':'📈','Reuters':'🌐','BBC':'📰'}.get(a.source,'📊') }}</div>{% endif %}
          <div class="nc-body">
            <div class="nc-badge-row">
              <div class="nc-source">{{ a.source }}</div>
              <div class="nc-impact-badge">注目</div>
            </div>
            <div class="nc-title">{{ a.title }}</div>
            <div class="nc-desc">{{ a.desc }}</div>
            <div class="nc-date">{{ a.pub }}</div>
          </div>
        </a>
        <div class="ai-analysis" data-title="{{ a.title|e }}" data-desc="{{ a.desc|e }}" data-size="large"></div>
      </div>
      {% endfor %}
      {% endif %}

      {% if ov_low %}
      {% if ov_high or ov_med %}<div class="news-group-label">その他</div>{% endif %}
      {% for a in ov_low %}
      <div class="nc-low">
        <a href="{{ a.link }}" target="_blank" rel="noopener">
          {% if a.img %}<img class="nc-img" src="{{ a.img }}" alt="" loading="lazy" onerror="this.style.display='none'">
          {% else %}<div class="nc-img-ph">{{ {'MarketWatch':'📈','Reuters':'🌐','BBC':'📰'}.get(a.source,'📊') }}</div>{% endif %}
          <div class="nc-body">
            <div class="nc-source">{{ a.source }}</div>
            <div class="nc-title">{{ a.title }}</div>
            <div class="nc-date">{{ a.pub }}</div>
          </div>
          <span class="nc-arrow">›</span>
        </a>
        <div class="ai-analysis" data-title="{{ a.title|e }}" data-size="small"></div>
      </div>
      {% endfor %}
      {% endif %}

      {% if not overseas_news %}<div class="no-news">海外ニュースを取得できませんでした</div>{% endif %}
    </div>

  </div><!-- /right-panel -->

</div><!-- /page-body -->

<script>
// ── State ─────────────────────────────────────────────────
const STOCKS      = {{ stocks_js|safe }};
let currentTicker = "^N225";
let currentPeriod = "1M";
let activeIdx     = 0;
let mainChart     = null;
const miniCharts  = {};

// ── Date in header ────────────────────────────────────────
(function(){
  const d = new Date();
  const fmt = new Intl.DateTimeFormat('ja-JP',{year:'numeric',month:'long',day:'numeric',weekday:'short'});
  document.getElementById('brandDate').textContent = fmt.format(d);
})();

// ── Theme ─────────────────────────────────────────────────
function isLight() { return document.body.classList.contains('light'); }

function chartPalette() {
  const l = isLight();
  return {
    grid:     l ? '#eaecf0' : 'rgba(255,214,0,.18)',
    tick:     l ? '#9aa5b4' : '#ffd600',
    tipBg:    l ? '#ffffff' : '#0d1117',
    tipBdr:   l ? '#d0d7de' : '#2a3446',
    tipTitle: l ? '#4a5568' : '#8b949e',
    tipBody:  l ? '#0d1117' : '#f0f6fc',
  };
}

function toggleTheme() {
  const on = document.getElementById('themeToggle').checked;
  document.body.classList.toggle('light', on);
  refreshChartThemes();
}

function refreshChartThemes() {
  if (mainChart) applyPaletteToChart(mainChart);
  Object.values(miniCharts).forEach(c => {
    if (c && c.options) {
      // mini charts have no axes/grid to update, but bg might matter
      c.update();
    }
  });
}

function applyPaletteToChart(chart) {
  if (!chart) return;
  const p  = chartPalette();
  const sc = chart.options.scales;
  if (sc && sc.x) {
    sc.x.grid.color   = p.grid;
    sc.x.ticks.color  = p.tick;
    sc.x.border.color = p.tipBdr;
    sc.y.grid.color   = p.grid;
    sc.y.ticks.color  = p.tick;
    sc.y.border.color = p.tipBdr;
  }
  if (chart.options.plugins && chart.options.plugins.tooltip) {
    const tp = chart.options.plugins.tooltip;
    tp.backgroundColor = p.tipBg;
    tp.borderColor     = p.tipBdr;
    tp.titleColor      = p.tipTitle;
    tp.bodyColor       = p.tipBody;
  }
  chart.update('none');
}

// ── Colour helpers ────────────────────────────────────────
function lineC(pos) { return pos ? '#3fb950' : '#f85149'; }
function fillC(pos) { return pos ? 'rgba(63,185,80,.14)' : 'rgba(248,81,73,.14)'; }

// ── Hero / Main chart ─────────────────────────────────────
async function loadMainChart(ticker, period) {
  const res = await fetch(`/api/chart/${encodeURIComponent(ticker)}/${period}`);
  if (!res.ok) return;
  const d = await res.json();
  if (d.error) return;

  const color = lineC(d.positive);
  const fill  = fillC(d.positive);
  const sign  = d.positive ? '+' : '';
  const p     = chartPalette();

  document.getElementById('heroChange').textContent = `${period}   ${sign}${d.change_pct}%`;
  document.getElementById('heroChange').className   = 'hero-change ' + (d.positive ? 'up' : 'down');

  const ctx = document.getElementById('mainChart').getContext('2d');
  if (mainChart) mainChart.destroy();
  mainChart = new Chart(ctx, {
    type: 'line',
    data: {
      labels: d.labels,
      datasets: [{
        data: d.prices,
        borderColor: color,
        backgroundColor: fill,
        borderWidth: 2.5,
        pointRadius: 0,
        pointHoverRadius: 5,
        pointHoverBackgroundColor: color,
        fill: true,
        tension: 0.35
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: p.tipBg,
          borderColor:     p.tipBdr,
          borderWidth:     1,
          titleColor:      p.tipTitle,
          bodyColor:       p.tipBody,
          padding:         12,
          callbacks: { label: c => '  ' + c.parsed.y.toLocaleString() }
        }
      },
      scales: {
        x: {
          grid:   { color: p.grid },
          ticks:  { color: p.tick, maxTicksLimit: 9, font: { size: 11 } },
          border: { color: p.tipBdr }
        },
        y: {
          grid:     { color: p.grid },
          ticks:    { color: p.tick, font: { size: 11 }, callback: v => v.toLocaleString() },
          border:   { color: p.tipBdr },
          position: 'right'
        }
      }
    }
  });
}

// ── Mini (card-level) chart ───────────────────────────────
async function loadMiniChart(ticker, idx) {
  const res = await fetch(`/api/chart/${encodeURIComponent(ticker)}/1M`);
  if (!res.ok) return;
  const d = await res.json();
  if (d.error || !d.prices) return;

  const color = lineC(d.positive);
  const fill  = fillC(d.positive);
  const ctx   = document.getElementById(`mini-${idx}`).getContext('2d');
  if (miniCharts[idx]) miniCharts[idx].destroy();
  miniCharts[idx] = new Chart(ctx, {
    type: 'line',
    data: {
      labels: d.labels,
      datasets: [{ data: d.prices, borderColor: color, backgroundColor: fill,
                   borderWidth: 2, pointRadius: 0, fill: true, tension: 0.35 }]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false }, tooltip: { enabled: false } },
      scales:  { x: { display: false }, y: { display: false } },
      animation: { duration: 500 }
    }
  });
}

// ── Prices ────────────────────────────────────────────────
async function loadPrices() {
  const res  = await fetch('/api/prices');
  const data = await res.json();
  const now  = new Date().toLocaleTimeString('ja-JP');
  document.getElementById('lastUpd').textContent = '最終更新 ' + now;

  data.forEach((s, i) => {
    const pe = document.getElementById(`price-${i}`);
    const ce = document.getElementById(`change-${i}`);
    const cd = document.getElementById(`card-${i}`);
    if (!pe) return;

    if (s.price !== null) {
      pe.textContent = s.price.toLocaleString();
      if (s.change !== null) {
        const sign = s.positive ? '+' : '';
        ce.textContent = `${sign}${s.change}  (${sign}${s.change_pct}%)`;
        ce.className = 'sc-change ' + (s.positive ? 'up' : 'down');
        pe.className = 'sc-price '  + (s.positive ? 'up' : 'down');
        cd.classList.add(s.positive ? 'up-card' : 'down-card');
      }
      // Update hero price if this is the active stock
      if (i === activeIdx) {
        document.getElementById('heroPrice').textContent = s.price.toLocaleString();
        document.getElementById('heroPrice').className =
          'hero-price ' + (s.positive ? 'up' : 'down');
      }
    } else {
      pe.textContent = 'N/A'; ce.textContent = '—';
    }
    setTimeout(() => loadMiniChart(s.ticker, i), i * 150);
  });
}

// ── Stock selection ───────────────────────────────────────
function selectStock(ticker, name, idx) {
  document.querySelectorAll('.price-card').forEach(c => c.classList.remove('active'));
  document.getElementById(`card-${idx}`).classList.add('active');
  document.querySelectorAll('.stock-btn').forEach(b => b.classList.remove('active'));
  document.getElementById(`sbtn-${idx}`).classList.add('active');
  document.getElementById('heroName').textContent = name;
  currentTicker = ticker;
  activeIdx = idx;
  loadMainChart(ticker, currentPeriod);
}

// ── Period ────────────────────────────────────────────────
function setPeriod(p) {
  currentPeriod = p;
  document.querySelectorAll('.period-btn').forEach(b =>
    b.classList.toggle('active', b.dataset.period === p));
  loadMainChart(currentTicker, currentPeriod);
}

// ── AI Analysis ──────────────────────────────────────────
async function fetchAnalysis(el) {
  const title = el.dataset.title || '';
  const desc  = el.dataset.desc  || '';
  const size  = el.dataset.size  || 'large';
  if (!title) return;

  el.innerHTML = '<div class="ai-loading">AI分析中</div>';

  try {
    const res = await fetch('/api/analyze', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title, desc, size }),
    });
    const data = await res.json();
    if (data.error || !data.analysis) { el.innerHTML = ''; return; }

    const text = data.analysis;
    let html = '<div class="ai-analysis-header">AI分析</div>';

    if (size === 'large') {
      // Parse bullet lines starting with • or numbers
      const lines = text.split('\n')
        .map(l => l.replace(/^[\s•\-\d\.]+/, '').trim())
        .filter(l => l.length > 0);
      lines.forEach(line => { html += `<div class="ai-bullet">${line}</div>`; });
    } else {
      const line = text.replace(/^[\s•\-\d\.]+/, '').trim();
      html += `<div class="ai-bullet">${line}</div>`;
    }

    el.innerHTML = html;
  } catch(e) {
    el.innerHTML = '';
  }
}

async function loadAllAnalysis() {
  const els = Array.from(document.querySelectorAll('.ai-analysis'));
  const BATCH = 4;
  for (let i = 0; i < els.length; i += BATCH) {
    await Promise.all(els.slice(i, i + BATCH).map(el => fetchAnalysis(el)));
  }
}

// ── Init ──────────────────────────────────────────────────
document.getElementById('card-0').classList.add('active');
loadPrices();
loadMainChart('^N225', '1M');
setTimeout(loadAllAnalysis, 1200);
</script>
</body>
</html>"""

WATCHLIST_HTML = r"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>経済指標ウォッチリスト</title>
<style>
:root {
  --bg:       #07090f;
  --bg-card:  #0d1117;
  --bg-row:   rgba(88,166,255,.03);
  --border:   #1c2333;
  --border2:  #2a3446;
  --txt:      #e6edf3;
  --txt-sub:  #8b949e;
  --txt-dim:  #3d4a5c;
  --accent:   #58a6ff;
  --up:       #3fb950;
  --down:     #f85149;
  --sec-hdr:  #ffd600;
  --hdr-bg:   rgba(7,9,15,.96);
}
*,*::before,*::after { margin:0; padding:0; box-sizing:border-box }
html { scroll-behavior:smooth }
body {
  background: var(--bg);
  color: var(--txt);
  font-family: 'Hiragino Sans','Yu Gothic UI','Meiryo','Segoe UI',system-ui,sans-serif;
  min-height: 100vh;
}
::-webkit-scrollbar { width:5px; background:var(--bg) }
::-webkit-scrollbar-thumb { background:var(--border2); border-radius:3px }
a { text-decoration:none; color:inherit }

/* ── HEADER ─────────────────────────────────────────────── */
header {
  position: sticky; top:0; z-index:200;
  background: var(--hdr-bg);
  backdrop-filter: blur(18px);
  -webkit-backdrop-filter: blur(18px);
  border-bottom: 1px solid var(--border);
  padding: 0 28px;
  height: 56px;
  display: flex; align-items:center; justify-content:space-between;
}
.brand { display:flex; align-items:baseline; gap:12px }
.brand h1 {
  font-size: 18px; font-weight:800; letter-spacing:-.3px;
  background: linear-gradient(110deg, var(--accent) 0%, #a5d6ff 60%, var(--accent) 100%);
  background-size: 200% auto;
  -webkit-background-clip: text; -webkit-text-fill-color: transparent;
  background-clip: text;
  animation: shimmer 4s linear infinite;
}
@keyframes shimmer { to { background-position: 200% center } }
.brand-sub { font-size:11px; color:var(--txt-dim) }
.hdr-right { display:flex; align-items:center; gap:10px }
.back-btn {
  font-size:12px; padding:5px 11px;
  border:1px solid var(--border); border-radius:6px;
  color:var(--txt-sub); cursor:pointer; background:transparent;
  transition: color .2s, border-color .2s;
}
.back-btn:hover { color:var(--accent); border-color:var(--accent) }
.refresh-btn {
  font-size:12px; padding:5px 12px;
  background:transparent; border:1px solid var(--border);
  color:var(--txt-sub); border-radius:6px; cursor:pointer;
  transition: all .2s; display:flex; align-items:center; gap:4px;
}
.refresh-btn:hover { border-color:var(--accent); color:var(--accent) }
.spin { animation: rotate .7s linear infinite; display:inline-block }
@keyframes rotate { to { transform:rotate(360deg) } }
.pulse-wrap { display:flex; align-items:center; gap:5px; font-size:10px; color:var(--txt-dim) }
.pulse-dot {
  width:6px; height:6px; border-radius:50%; background:var(--up);
  animation: pulse 2s ease-in-out infinite;
}
@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.25} }

/* ── MAIN GRID ───────────────────────────────────────────── */
main {
  max-width: 1280px;
  margin: 0 auto;
  padding: 24px 24px 56px;
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(380px, 1fr));
  gap: 18px;
}

/* ── SECTION CARD ────────────────────────────────────────── */
.card {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: 12px;
  overflow: hidden;
}
.card-wide { grid-column: 1 / -1 }
.sec-head {
  padding: 12px 20px;
  display: flex; align-items:center; justify-content:space-between;
  border-bottom: 1px solid var(--border);
}
.sec-title {
  font-size: 11px; font-weight:700; letter-spacing:.9px;
  text-transform: uppercase; color: var(--sec-hdr);
  display: flex; align-items:center; gap:7px;
}
.fetch-ts {
  font-size: 10px; color: var(--txt-dim);
  font-weight:400; letter-spacing:0; text-transform:none;
}

/* ── INDICATOR ROW ───────────────────────────────────────── */
.ind-row {
  display: grid;
  grid-template-columns: 1fr auto auto;
  align-items: center;
  gap: 14px;
  padding: 10px 20px;
  border-bottom: 1px solid rgba(28,35,51,.45);
  transition: background .15s;
}
.ind-row:last-child { border-bottom:none }
.ind-row:hover { background: var(--bg-row) }
.ind-name { font-size:13px; color:var(--txt); font-weight:500; line-height:1.3 }
.ind-name small { display:block; font-size:10px; color:var(--txt-dim); font-weight:400; margin-top:1px }
.ind-val {
  font-size:15px; font-weight:700;
  font-variant-numeric: tabular-nums;
  text-align:right; white-space:nowrap;
  color: var(--txt);
}
.ind-val .unit { font-size:10px; font-weight:400; color:var(--txt-sub); margin-left:2px }
.ind-chg {
  font-size:11px; font-variant-numeric:tabular-nums;
  text-align:right; white-space:nowrap; min-width:82px;
  line-height:1.55;
}
.pos { color: var(--up) }
.neg { color: var(--down) }
.neu { color: var(--txt-dim) }
.na  { color: var(--txt-dim); font-size:12px }

/* skeleton */
.skel {
  display:inline-block; height:.85em; border-radius:3px;
  background: linear-gradient(90deg, var(--border) 25%, rgba(255,255,255,.05) 50%, var(--border) 75%);
  background-size:200% 100%;
  animation: skshimmer 1.4s linear infinite;
  vertical-align:middle;
}
@keyframes skshimmer { to { background-position:-200% center } }

/* ── ERROR BANNER ────────────────────────────────────────── */
#err-banner {
  display:none;
  background: rgba(248,81,73,.1);
  border:1px solid rgba(248,81,73,.3);
  color: #f85149;
  font-size:12px; padding:10px 24px;
  text-align:center;
}
</style>
</head>
<body>
<header>
  <div class="brand">
    <h1>経済指標ウォッチリスト</h1>
    <span class="brand-sub" id="last-upd">読み込み中…</span>
  </div>
  <div class="hdr-right">
    <div class="pulse-wrap">
      <div class="pulse-dot"></div>
      <span>5分毎自動更新</span>
    </div>
    <button class="refresh-btn" id="ref-btn" onclick="load()">
      <span id="ref-icon">⟳</span> 更新
    </button>
    <a href="/" class="back-btn">← ダッシュボード</a>
  </div>
</header>

<div id="err-banner">データ取得に失敗しました。ネットワークを確認して再試行してください。</div>

<main>
  <!-- 国内株式市場 -->
  <div class="card" id="card-domestic">
    <div class="sec-head">
      <span class="sec-title">🏯 国内株式市場</span>
      <span class="fetch-ts" id="ft-d">—</span>
    </div>
    <div id="sec-d"></div>
  </div>

  <!-- 海外株式市場 -->
  <div class="card" id="card-overseas">
    <div class="sec-head">
      <span class="sec-title">🌐 海外株式市場</span>
      <span class="fetch-ts" id="ft-o">—</span>
    </div>
    <div id="sec-o"></div>
  </div>

  <!-- 外国為替市場 -->
  <div class="card" id="card-fx">
    <div class="sec-head">
      <span class="sec-title">💱 外国為替市場</span>
      <span class="fetch-ts" id="ft-f">—</span>
    </div>
    <div id="sec-f"></div>
  </div>

  <!-- 短期金融・債券市場 -->
  <div class="card" id="card-bond">
    <div class="sec-head">
      <span class="sec-title">📈 短期金融・債券市場</span>
      <span class="fetch-ts" id="ft-b">—</span>
    </div>
    <div id="sec-b"></div>
  </div>

  <!-- 商品市況・その他 -->
  <div class="card" id="card-cmd">
    <div class="sec-head">
      <span class="sec-title">🛢 商品市況・その他</span>
      <span class="fetch-ts" id="ft-c">—</span>
    </div>
    <div id="sec-c"></div>
  </div>
</main>

<script>
// ── helpers ─────────────────────────────────────────────────────────
function fmtNum(v, digits, comma) {
  if (v == null || isNaN(Number(v))) return null;
  const n = Number(v);
  if (comma === false) return n.toFixed(digits);
  return n.toLocaleString('ja-JP', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits
  });
}

function valHtml(v, digits, unit, chg) {
  if (v == null) return '<span class="na">—</span>';
  const cls = chg != null ? (chg > 0 ? 'pos' : chg < 0 ? 'neg' : '') : '';
  const u = unit ? `<span class="unit">${unit}</span>` : '';
  return `<span class="ind-val ${cls}">${fmtNum(v, digits)}${u}</span>`;
}

function chgHtml(chg, pct, digits) {
  if (chg == null && pct == null) return '<span class="na">—</span>';
  const cls = (chg ?? pct) >= 0 ? 'pos' : 'neg';
  const sign = (chg ?? pct) >= 0 ? '+' : '';
  const c = chg != null ? `${sign}${fmtNum(chg, digits)}` : '';
  const p = pct != null ? `${pct >= 0 ? '+' : ''}${Number(pct).toFixed(2)}%` : '';
  return `<span class="${cls}">${c}${c && p ? '<br>' : ''}${p}</span>`;
}

function skRow(name) {
  return `<div class="ind-row">
    <span class="ind-name">${name}</span>
    <span class="ind-val"><span class="skel" style="width:90px">&nbsp;</span></span>
    <span class="ind-chg"><span class="skel" style="width:65px">&nbsp;</span></span>
  </div>`;
}

function dataRow(name, sub, v, chg, pct, unit, digits) {
  const nm = sub
    ? `<span class="ind-name">${name}<small>${sub}</small></span>`
    : `<span class="ind-name">${name}</span>`;
  return `<div class="ind-row">
    ${nm}
    ${valHtml(v, digits, unit, chg)}
    <span class="ind-chg">${chgHtml(chg, pct, digits)}</span>
  </div>`;
}

function staticRow(name, sub, v, unit, digits) {
  const nm = sub
    ? `<span class="ind-name">${name}<small>${sub}</small></span>`
    : `<span class="ind-name">${name}</span>`;
  const vh = v != null
    ? `<span class="ind-val">${fmtNum(v, digits)}<span class="unit">${unit}</span></span>`
    : '<span class="na">—</span>';
  return `<div class="ind-row">${nm}${vh}<span class="ind-chg"></span></div>`;
}

// ── skeleton ─────────────────────────────────────────────────────────
function renderSkeleton() {
  const dRows = ['日経平均株価','東証株価指数','JPX日経400','東証グロース250指数',
    '東証プライム売買代金','東証プライム時価総額','日経平均予想PER','日経平均PBR','日経平均配当利回り'];
  document.getElementById('sec-d').innerHTML = dRows.map(skRow).join('');

  const oRows = ['ダウ工業30種平均','ナスダック総合株価指数','ユーロ・ストック50','上海総合指数'];
  document.getElementById('sec-o').innerHTML = oRows.map(skRow).join('');

  document.getElementById('sec-f').innerHTML = ['1ドル＝何円','1ユーロ＝何円'].map(skRow).join('');
  document.getElementById('sec-b').innerHTML = ['無担保翌日物コールレート','日本長期金利','米長期金利'].map(skRow).join('');
  document.getElementById('sec-c').innerHTML = ['NY原油先物','NY金先物','CRB指数','東証REIT指数'].map(skRow).join('');
}

// ── render ───────────────────────────────────────────────────────────
function render(d) {
  const v   = k => d[k]?.v   ?? null;
  const chg = k => d[k]?.chg ?? null;
  const pct = k => d[k]?.pct ?? null;
  const ft  = d.fetched_at || '—';

  document.getElementById('last-upd').textContent = `最終更新: ${ft}`;
  ['ft-d','ft-o','ft-f','ft-b','ft-c'].forEach(id => {
    document.getElementById(id).textContent = ft;
  });

  // 国内株式市場
  document.getElementById('sec-d').innerHTML = [
    dataRow('日経平均株価',     '',                   v('nikkei'),    chg('nikkei'),    pct('nikkei'),    '',   2),
    dataRow('東証株価指数 (TOPIX)', '',  v('topix'),  chg('topix'),  pct('topix'),  '', 2),
    dataRow('JPX日経400',          '',  v('jpx400'), chg('jpx400'), pct('jpx400'), '', 2),
    dataRow('東証グロース250指数','',                 v('growth250'), chg('growth250'), pct('growth250'), '',   2),
    staticRow('東証プライム売買代金','日次合計',      v('tse_prime_vol'), '兆円', 2),
    staticRow('東証プライム時価総額','',              v('tse_prime_cap'), '兆円', 1),
    staticRow('日経平均予想PER', '',                  d.nikkei_per,   '倍',  2),
    staticRow('日経平均PBR',    '',                   d.nikkei_pbr,   '倍',  2),
    staticRow('日経平均配当利回り','',                d.nikkei_yield, '%',   2),
  ].join('');

  // 海外株式市場
  document.getElementById('sec-o').innerHTML = [
    dataRow('ダウ工業30種平均',      '',  v('dow'),       chg('dow'),       pct('dow'),       '',  2),
    dataRow('ナスダック総合株価指数', '',  v('nasdaq'),    chg('nasdaq'),    pct('nasdaq'),    '',  2),
    dataRow('ユーロ・ストック50',     '',  v('eurostoxx'), chg('eurostoxx'), pct('eurostoxx'), '',  2),
    dataRow('上海総合指数',           '',  v('shanghai'),  chg('shanghai'),  pct('shanghai'),  '',  2),
  ].join('');

  // 外国為替
  document.getElementById('sec-f').innerHTML = [
    dataRow('1ドル＝何円',   'USD/JPY',  v('usdjpy'), chg('usdjpy'), pct('usdjpy'), '円', 2),
    dataRow('1ユーロ＝何円', 'EUR/JPY',  v('eurjpy'), chg('eurjpy'), pct('eurjpy'), '円', 2),
  ].join('');

  // 短期・債券市場
  document.getElementById('sec-b').innerHTML = [
    staticRow('無担保翌日物コールレート', 'BOJ政策目標',      v('call_rate'), '%', 3),
    dataRow('日本長期金利',  '10年国債利回り',  v('jp10y'),    chg('jp10y'),   pct('jp10y'),   '%', 3),
    dataRow('米長期金利',    '10年国債利回り',  v('us10y'),    chg('us10y'),   pct('us10y'),   '%', 3),
  ].join('');

  // 商品市況・その他
  document.getElementById('sec-c').innerHTML = [
    dataRow('NY原油先物', 'WTI',  v('crude'), chg('crude'), pct('crude'), 'USD', 2),
    dataRow('NY金先物',   '',     v('gold'),  chg('gold'),  pct('gold'),  'USD', 2),
    dataRow('CRB指数',    'Thomson Reuters/CoreCommodity CRB', v('crb'), chg('crb'), pct('crb'), '', 2),
    dataRow('東証REIT指数','',    v('reit'),  chg('reit'),  pct('reit'),  '',    2),
  ].join('');
}

// ── load ─────────────────────────────────────────────────────────────
let timer = null;
async function load() {
  const btn  = document.getElementById('ref-btn');
  const icon = document.getElementById('ref-icon');
  btn.disabled = true;
  icon.classList.add('spin');
  document.getElementById('err-banner').style.display = 'none';

  try {
    const res = await fetch('/api/watchlist');
    if (!res.ok) throw new Error(res.statusText);
    render(await res.json());
  } catch(e) {
    document.getElementById('err-banner').style.display = 'block';
    document.getElementById('last-upd').textContent = '取得エラー';
  } finally {
    btn.disabled = false;
    icon.classList.remove('spin');
  }
}

renderSkeleton();
load();
timer = setInterval(load, 5 * 60 * 1000);
</script>
</body>
</html>"""


@app.route('/watchlist')
def watchlist():
    return render_template_string(WATCHLIST_HTML)


@app.route("/")
def index():
    import json
    domestic_news = limit_news(fetch_feed_group(DOMESTIC_FEEDS))
    overseas_news = limit_news(fetch_feed_group(OVERSEAS_FEEDS))
    stocks_js = json.dumps([{"ticker": s["ticker"], "name": s["name"]} for s in STOCKS])
    return render_template_string(
        HTML,
        stocks=STOCKS,
        domestic_news=domestic_news,
        overseas_news=overseas_news,
        stocks_js=stocks_js,
        period_labels=PERIOD_LABELS,
    )

if __name__ == "__main__":
    print("Dashboard running at http://127.0.0.1:5000")
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
