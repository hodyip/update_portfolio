"""
update_portfolio.py
===================
每次執行抓取一個或多個 portfolio JSON，寫入靜態網站資料夾，由 GitHub Actions
commit 回 repo，經 GitHub Pages 發佈。頁面將各組合左右並排顯示。

來源 JSON 結構：
    {"code":0,"message":"...","data":{
        "market_items":  [{"tab_name","ratio","market"}],
        "industry_items":[{"tab_name","ratio","market"}],
        "record_items":  [{"stock_code","stock_name","market","total_ratio",
                           "cost_price","current_price","profit_and_loss_ratio",...}]}}

比例尺（已由實際數據反推核實）：
    *_ratio                   1e9 = 100%  → 除 1e7 得百分比
    cost_price/current_price  1e9 = 1.0   → 除 1e9 得價格
    profit_and_loss_ratio     係回報率，唔係金額

容錯行為：
    某個來源失敗 → 沿用該組合上一次嘅資料並標示為 stale，其他組合照常更新。
    全部來源失敗 → 結束碼 1，完全唔覆蓋舊資料。
    （只有全滅先回傳非零，否則 Actions 會跳過 commit 步驟，令成功嗰邊嘅新資料白費。）

三種執行模式（改頂部 PARAM_* 常數，唔需要 terminal 參數）：
 1. PARAM_SELFTEST = True     用內建樣本離線行一次
 2. PARAM_SELFTEST = False    讀 PARAM_LOCAL_URLS 喺本機真實抓取
 3. GitHub Actions            讀各組合嘅環境變數（存喺 repo Secrets）

輸出：
    docs/index.html                          靜態頁
    docs/data/portfolio.json                 全部組合嘅最新快照
    docs/data/history_<key>.jsonl            每個組合一個歷史檔
    .github/workflows/update-portfolio.yml   排程（本機執行時按 PARAM 重新生成）
"""

from __future__ import annotations

import functools
import http.server
import json
import os
import socketserver
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# ======================================================================
# 組合設定 —— 加減組合只需要改呢個 list
# ======================================================================
# key       : 內部識別碼，會用嚟命名歷史檔 history_<key>.jsonl，只用英數同底線
# label     : 頁面上顯示嘅名
# url_env   : 對應嘅 GitHub Secret / 環境變數名稱
PARAM_PORTFOLIOS: list[dict[str, str]] = [
    {"key": "main",   "label": "主組合",   "url_env": "PORTFOLIO_SOURCE_URL"},
    {"key": "second", "label": "第二組合", "url_env": "PORTFOLIO_SOURCE_URL_2"},
]

# 本機測試用嘅網址（key → url）。切勿 commit 真實網址，測完請清空。
PARAM_LOCAL_URLS: dict[str, str] = {
    # "main":   "https://.../portfolio?token=...",
    # "second": "https://.../portfolio?token=...",
}

# ======================================================================
# 排程
# ======================================================================
# cron 一律用 UTC，GitHub Actions 唔支援時區設定，亦唔會處理美國夏令時轉換。
# 現時設定 `5 13-21 * * 1-5`：週一至五 UTC 13:05 至 21:05 每小時一次，共 9 次。
#   夏令時 (EDT, UTC-4)：對應美東 09:05–17:05，涵蓋 09:30 開市至 16:00 收市
#   冬令時 (EST, UTC-5)：對應美東 08:05–16:05，同樣涵蓋整段 RTH
# 改完之後喺本機 Run 一次，yml 會重新生成，再 commit 上去先會生效。
#
# 其他常用寫法：
#   每小時（全日）      "5 * * * *"
#   每 30 分鐘          "5,35 * * * *"
#   每 4 小時           "5 */4 * * *"
#   美股 RTH 每 30 分鐘 "5,35 13-21 * * 1-5"
PARAM_WORKFLOW_CRON: str = "5 13-21 * * 1-5"

# ======================================================================
# 其他可調參數
# ======================================================================

# --- HTTP ---
PARAM_HTTP_TIMEOUT_SEC: float = 20.0
PARAM_HTTP_RETRIES: int = 3
PARAM_HTTP_BACKOFF_SEC: float = 3.0
PARAM_USER_AGENT: str = "portfolio-publisher/2.0"

# --- 來源結構 ---
PARAM_ENVELOPE_KEY: str = "data"
PARAM_OK_CODE: int = 0
PARAM_RECORDS_KEY: str = "record_items"
PARAM_MARKET_KEY: str = "market_items"
PARAM_INDUSTRY_KEY: str = "industry_items"

# --- 比例尺 ---
PARAM_SCALE_RATIO: float = 1e9
PARAM_SCALE_PRICE: float = 1e9

# --- market code 對照（只有 2=US 有數據佐證，其餘為推測，請自行核實）---
PARAM_MARKET_NAMES: dict[int, str] = {1: "HK", 2: "US", 3: "CN"}

# --- 輸出 ---
PARAM_SITE_DIR: str = "docs"
PARAM_DATA_DIR: str = "data"
PARAM_SNAPSHOT_FILENAME: str = "portfolio.json"
PARAM_HISTORY_PREFIX: str = "history_"
# 按 RTH 每小時計，一日約 9 筆；2500 筆約等於一年。改頻率記得一齊改。
PARAM_HISTORY_MAX_ROWS: int = 2500

# --- 顯示 ---
PARAM_SITE_TITLE: str = "Portfolio"
PARAM_SITE_TAGLINE: str = "美股交易時段每小時自動更新"
PARAM_SHOW_CASH_ROW: bool = True
PARAM_STALE_HOURS: float = 3.0          # 超過幾多個鐘未更新就標示為過期
PARAM_TIMEZONE_OFFSET_HOURS: int = 8
PARAM_TIMEZONE_LABEL: str = "HKT"

# --- 本機預覽（Actions 內自動略過）---
PARAM_SERVE_PREVIEW: bool = True
PARAM_PREVIEW_PORT: int = 8800
PARAM_PREVIEW_OPEN_BROWSER: bool = True

# --- 行為 ---
PARAM_SELFTEST: bool = True
PARAM_DRY_RUN: bool = False
PARAM_FORCE_REWRITE_HTML: bool = True
PARAM_FORCE_REWRITE_WORKFLOW: bool = True   # 本機執行時按上面嘅 cron 重寫 yml

# ======================================================================
# 內建樣本（PARAM_SELFTEST 用）
# ======================================================================

_SAMPLE_MAIN: dict[str, Any] = {
    "code": 0, "message": "成功",
    "data": {
        "market_items": [{"tab_name": "US", "ratio": 900421186, "market": 2}],
        "industry_items": [
            {"tab_name": "Others", "ratio": 603450810, "market": 12},
            {"tab_name": "Non-Bank Financials", "ratio": 296970376, "market": 10},
        ],
        "record_items": [
            {"stock_code": "XLE", "stock_name": "Energy Select Sector SPDR Fund",
             "market": 2, "total_ratio": 199606749, "pending_ratio": 0,
             "cost_price": 63047216429, "current_price": 64060000000,
             "profit_and_loss_ratio": 16063890},
            {"stock_code": "EWZ", "stock_name": "iShares MSCI Brazil ETF",
             "market": 2, "total_ratio": 199645768, "pending_ratio": 0,
             "cost_price": 37990000000, "current_price": 37860000000,
             "profit_and_loss_ratio": -3421953},
            {"stock_code": "BRK.B", "stock_name": "Berkshire Hathaway-B",
             "market": 2, "total_ratio": 296970376, "pending_ratio": 0,
             "cost_price": 507737874491, "current_price": 506030000000,
             "profit_and_loss_ratio": -3363693},
            {"stock_code": "GLD", "stock_name": "SPDR Gold ETF",
             "market": 2, "total_ratio": 204198293, "pending_ratio": 0,
             "cost_price": 399330273613, "current_price": 406770000000,
             "profit_and_loss_ratio": 18630509},
        ],
    },
}

_SAMPLE_SECOND: dict[str, Any] = {
    "code": 0, "message": "成功",
    "data": {
        "market_items": [
            {"tab_name": "HK", "ratio": 480000000, "market": 1},
            {"tab_name": "US", "ratio": 300000000, "market": 2},
        ],
        "industry_items": [
            {"tab_name": "Information Technology", "ratio": 480000000, "market": 5},
            {"tab_name": "Others", "ratio": 300000000, "market": 12},
        ],
        "record_items": [
            {"stock_code": "0700", "stock_name": "騰訊控股", "market": 1,
             "total_ratio": 480000000, "pending_ratio": 0,
             "cost_price": 512400000000, "current_price": 548000000000,
             "profit_and_loss_ratio": 69477000},
            {"stock_code": "QQQ", "stock_name": "Invesco QQQ Trust", "market": 2,
             "total_ratio": 300000000, "pending_ratio": 0,
             "cost_price": 498120000000, "current_price": 491700000000,
             "profit_and_loss_ratio": -12888000},
        ],
    },
}

SELFTEST_PAYLOADS: dict[str, Any] = {"main": _SAMPLE_MAIN, "second": _SAMPLE_SECOND}

# ======================================================================
# 工具
# ======================================================================


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def local_now() -> datetime:
    return datetime.now(timezone(timedelta(hours=PARAM_TIMEZONE_OFFSET_HOURS)))


def to_number(x: Any) -> float | None:
    if isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        return float(x)
    if isinstance(x, str):
        s = x.strip().replace(",", "")
        try:
            return float(s) if s else None
        except ValueError:
            return None
    return None


def pct(raw: Any) -> float | None:
    n = to_number(raw)
    return None if n is None else n / PARAM_SCALE_RATIO * 100.0


def price(raw: Any) -> float | None:
    n = to_number(raw)
    return None if n is None else n / PARAM_SCALE_PRICE


def market_name(code: Any) -> str:
    n = to_number(code)
    return "—" if n is None else PARAM_MARKET_NAMES.get(int(n), f"Market {int(n)}")


def running_in_actions() -> bool:
    return os.environ.get("GITHUB_ACTIONS", "").lower() == "true"


# ======================================================================
# 1. 抓取
# ======================================================================


def resolve_url(cfg: dict[str, str]) -> str:
    url = os.environ.get(cfg["url_env"], "").strip() or PARAM_LOCAL_URLS.get(cfg["key"], "").strip()
    if not url:
        raise RuntimeError(
            f"組合 '{cfg['key']}' 搵唔到來源 URL。"
            f"請設定環境變數 {cfg['url_env']}（GitHub Secret），"
            f"或喺本機測試時填 PARAM_LOCAL_URLS['{cfg['key']}']。"
        )
    return url


def fetch_json(url: str) -> Any:
    last_err: Exception | None = None
    for attempt in range(1, PARAM_HTTP_RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": PARAM_USER_AGENT, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=PARAM_HTTP_TIMEOUT_SEC) as resp:
                return json.loads(resp.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as exc:
            last_err = exc
            if 400 <= exc.code < 500 and exc.code != 429:
                raise RuntimeError(
                    f"來源回應 HTTP {exc.code}，屬用戶端錯誤，唔會重試。請檢查 URL 或授權。"
                ) from exc
            log(f"  抓取失敗（第 {attempt}/{PARAM_HTTP_RETRIES} 次）：{exc}")
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            last_err = exc
            log(f"  抓取失敗（第 {attempt}/{PARAM_HTTP_RETRIES} 次）：{exc}")
        if attempt < PARAM_HTTP_RETRIES:
            time.sleep(PARAM_HTTP_BACKOFF_SEC * attempt)
    raise RuntimeError(f"重試 {PARAM_HTTP_RETRIES} 次後仍然失敗：{last_err}")


# ======================================================================
# 2. 解析
# ======================================================================


def unwrap(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise RuntimeError("來源頂層唔係 JSON object。")
    code = to_number(payload.get("code"))
    if code is not None and int(code) != PARAM_OK_CODE:
        raise RuntimeError(f"來源回傳 code={int(code)}，message={payload.get('message')!r}")
    body = payload.get(PARAM_ENVELOPE_KEY, payload)
    if not isinstance(body, dict):
        raise RuntimeError(f"搵唔到 '{PARAM_ENVELOPE_KEY}' object。")
    return body


def parse_positions(body: dict[str, Any], key: str) -> list[dict[str, Any]]:
    raw = body.get(PARAM_RECORDS_KEY)
    if not isinstance(raw, list):
        raise RuntimeError(f"'{PARAM_RECORDS_KEY}' 唔存在或者唔係 list。")

    out: list[dict[str, Any]] = []
    for r in raw:
        if not isinstance(r, dict):
            continue
        cost = price(r.get("cost_price"))
        cur = price(r.get("current_price"))
        ret = pct(r.get("profit_and_loss_ratio"))
        recomputed = ((cur - cost) / cost * 100.0) if (cost and cur is not None) else None
        if ret is not None and recomputed is not None and abs(ret - recomputed) > 0.05:
            log(f"  注意[{key}]：{r.get('stock_code')} 來源回報 {ret:.4f}% 與由價格重算嘅 "
                f"{recomputed:.4f}% 有出入，比例尺假設可能有誤。")
        out.append({
            "code": r.get("stock_code"),
            "name": r.get("stock_name"),
            "market": market_name(r.get("market")),
            "weight_pct": pct(r.get("total_ratio")),
            "cost_price": cost,
            "current_price": cur,
            "return_pct": ret if ret is not None else recomputed,
        })
    out.sort(key=lambda x: x["weight_pct"] if x["weight_pct"] is not None else -1, reverse=True)
    return out


def parse_breakdown(body: dict[str, Any], key: str) -> list[dict[str, Any]]:
    raw = body.get(key)
    if not isinstance(raw, list):
        return []
    items = [{"name": i.get("tab_name"), "weight_pct": pct(i.get("ratio"))}
             for i in raw if isinstance(i, dict)]
    items.sort(key=lambda x: x["weight_pct"] if x["weight_pct"] is not None else -1, reverse=True)
    return items


def build_portfolio(cfg: dict[str, str], payload: Any) -> dict[str, Any]:
    body = unwrap(payload)
    positions = parse_positions(body, cfg["key"])

    weights = [p["weight_pct"] for p in positions if p["weight_pct"] is not None]
    invested = sum(weights) if weights else None

    pairs = [(p["weight_pct"], p["return_pct"]) for p in positions
             if p["weight_pct"] is not None and p["return_pct"] is not None]
    wsum = sum(w for w, _ in pairs)
    weighted = (sum(w * r for w, r in pairs) / wsum) if wsum else None

    now = local_now()
    return {
        "key": cfg["key"],
        "label": cfg["label"],
        "ok": True,
        "error": None,
        "stale": False,
        "updated_at": now.isoformat(timespec="seconds"),
        "invested_pct": invested,
        "cash_pct": (100.0 - invested) if invested is not None else None,
        "weighted_return_pct": weighted,
        "position_count": len(positions),
        "positions": positions,
        "by_market": parse_breakdown(body, PARAM_MARKET_KEY),
        "by_industry": parse_breakdown(body, PARAM_INDUSTRY_KEY),
    }


# ======================================================================
# 3. 寫檔
# ======================================================================


def write_text(path: Path, content: str) -> None:
    if PARAM_DRY_RUN:
        log(f"[DRY RUN] 略過寫入 {path}（{len(content)} 字元）")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    log(f"已寫入 {path}（{len(content)} 字元）")


def read_previous(path: Path) -> dict[str, dict[str, Any]]:
    """讀返上一次嘅快照，用嚟喺個別來源失敗時保留舊資料。"""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {p["key"]: p for p in data.get("portfolios", []) if isinstance(p, dict)}
    except (json.JSONDecodeError, OSError, KeyError) as exc:
        log(f"讀唔到舊快照（{exc}），當作冇舊資料處理。")
        return {}


def append_history(path: Path, p: dict[str, Any]) -> None:
    row = {
        "ts": p["updated_at"],
        "weighted_return_pct": p["weighted_return_pct"],
        "invested_pct": p["invested_pct"],
        "position_count": p["position_count"],
    }
    lines: list[str] = []
    if path.exists():
        lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    lines.append(json.dumps(row, ensure_ascii=False))
    write_text(path, "\n".join(lines[-PARAM_HISTORY_MAX_ROWS:]) + "\n")


# ======================================================================
# 4. 靜態頁
# ======================================================================

INDEX_HTML = r"""<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root{
    --ink:#0d1b24; --slate:#16242f; --line:#25384a;
    --paper:#f2f5f7; --muted:#8ca0b3;
    --up:#3fbf8f; --down:#e2685f; --gold:#d8a13a;
  }
  *{box-sizing:border-box}
  html{-webkit-text-size-adjust:100%}
  body{
    margin:0; background:var(--ink); color:var(--paper);
    font-family:"IBM Plex Sans",system-ui,"Noto Sans TC",sans-serif;
    font-variant-numeric:tabular-nums; font-feature-settings:"tnum" 1; line-height:1.5;
  }
  .wrap{max-width:1180px; margin:0 auto; padding:26px 20px 64px}
  header{display:flex; justify-content:space-between; align-items:baseline;
         gap:16px; flex-wrap:wrap; padding-bottom:18px; border-bottom:1px solid var(--line)}
  h1{margin:0; font-size:18px; font-weight:600}
  .tag{margin:2px 0 0; color:var(--muted); font-size:13px}
  .stamp{color:var(--muted); font-size:13px}

  .grid{display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:0}
  .col{padding:22px 0 0}
  .col + .col{border-left:1px solid var(--line); padding-left:26px; margin-left:26px}

  .plabel{display:flex; align-items:center; gap:8px; font-size:14px; font-weight:500}
  .dot{width:7px; height:7px; border-radius:50%; background:var(--up); flex:none}
  .dot.stale{background:var(--gold)}
  .ret{font-size:clamp(30px,4.6vw,44px); font-weight:600; letter-spacing:-.02em;
       line-height:1.1; margin-top:8px}
  .sub{margin-top:6px; color:var(--muted); font-size:13px}
  .sub b{color:var(--paper); font-weight:500}
  .warn{margin-top:8px; color:var(--gold); font-size:12.5px}
  .up{color:var(--up)} .down{color:var(--down)}
  svg.spark{width:100%; height:64px; display:block; margin-top:14px; overflow:hidden}

  h2{font-size:12.5px; font-weight:500; color:var(--muted); margin:22px 0 9px}
  .bars{display:grid; gap:7px}
  .bar{display:grid; grid-template-columns:minmax(0,1.2fr) 2fr 52px;
       gap:10px; align-items:center; font-size:12.5px}
  .track{height:5px; background:var(--slate)}
  .fill{height:5px; background:var(--gold); opacity:.8}
  .bar .pctv{text-align:right; color:var(--muted)}
  .bar .lbl{overflow:hidden; text-overflow:ellipsis; white-space:nowrap}

  table{width:100%; border-collapse:collapse; font-size:13px}
  th{font-weight:500; color:var(--muted); text-align:left; padding:7px 8px;
     border-bottom:1px solid var(--line); white-space:nowrap; cursor:pointer;
     user-select:none; font-size:12.5px}
  th:hover{color:var(--paper)}
  th.sorted{color:var(--paper)}
  th.sorted::after{content:"·"; margin-left:5px; color:var(--gold)}
  td{padding:8px; border-bottom:1px solid rgba(37,56,74,.5); white-space:nowrap}
  td.num,th.num{text-align:right}
  td.sym{font-weight:500}
  tbody tr:hover{background:var(--slate)}
  tbody tr.cash td{color:var(--muted)}
  .note{margin-top:30px; padding-top:18px; border-top:1px solid var(--line);
        color:var(--muted); font-size:12.5px; line-height:1.7}
  .msg{padding:36px 0; color:var(--muted)}
  @media (max-width:820px){
    .wrap{padding:20px 14px 52px}
    .grid{grid-template-columns:minmax(0,1fr)}
    .col + .col{border-left:none; padding-left:0; margin-left:0;
                border-top:1px solid var(--line); margin-top:24px}
  }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div>
      <h1>__TITLE__</h1>
      <p class="tag">__TAGLINE__</p>
    </div>
    <div class="stamp" id="stamp">載入中</div>
  </header>

  <div class="grid" id="grid"></div>
  <div class="msg" id="msg">正在讀取倉位資料</div>

  <p class="note">
    比重為佔各自組合嘅百分比；回報為現價相對成本價嘅未實現變動，未計股息、費用及匯率。<br>
    資料由排程自動更新，頁面每 5 分鐘重新讀取一次檔案。標示「資料未更新」代表該來源最近一次抓取失敗，顯示緊上一次成功嘅數值。<br>
    僅作記錄用途，並非投資建議。
  </p>
</div>

<script>
const DATA = "data/__SNAPSHOT__";
const HIST_PREFIX = "data/__HISTPREFIX__";

const fmt = (n, d = 2) =>
  (n === null || n === undefined || !isFinite(n)) ? "—"
  : n.toLocaleString("en-US", {minimumFractionDigits: d, maximumFractionDigits: d});
const pc = (n, d = 2) => n === null || n === undefined ? "—" : fmt(n, d) + "%";
const sg = (n, d = 2) =>
  n === null || n === undefined ? "—" : (n >= 0 ? "+" : "−") + fmt(Math.abs(n), d) + "%";
const cls = (n) => n === null || n === undefined ? "" : (n >= 0 ? "up" : "down");
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, c =>
  ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));

async function load(url) {
  const r = await fetch(url + "?t=" + Date.now(), {cache: "no-store"});
  if (!r.ok) throw new Error(url + " → HTTP " + r.status);
  return r;
}

function barBlock(title, items) {
  if (!items || !items.length) return "";
  const rows = items.map(i => `
    <div class="bar">
      <div class="lbl" title="${esc(i.name)}">${esc(i.name)}</div>
      <div class="track"><div class="fill" style="width:${Math.min(100, i.weight_pct || 0).toFixed(1)}%"></div></div>
      <div class="pctv">${pc(i.weight_pct)}</div>
    </div>`).join("");
  return `<h2>${title}</h2><div class="bars">${rows}</div>`;
}

function buildColumn(p) {
  const col = document.createElement("div");
  col.className = "col";
  const stamp = (p.updated_at || "").replace("T", " ").slice(0, 16);
  col.innerHTML =
    `<div class="plabel"><span class="dot${p.stale ? " stale" : ""}"></span>${esc(p.label)}</div>` +
    `<div class="ret ${cls(p.weighted_return_pct)}">${sg(p.weighted_return_pct)}</div>` +
    `<div class="sub">加權未實現回報　·　持倉 <b>${pc(p.invested_pct)}</b>` +
    `　·　現金 <b>${pc(p.cash_pct)}</b>　·　<b>${p.position_count ?? 0}</b> 隻</div>` +
    (p.stale ? `<div class="warn">資料未更新（${stamp}）：${esc(p.error || "來源抓取失敗")}</div>` : "") +
    `<svg class="spark" preserveAspectRatio="none" aria-hidden="true"></svg>` +
    `<h2>持倉明細</h2><table><thead><tr></tr></thead><tbody></tbody></table>` +
    barBlock("市場分佈", p.by_market) + barBlock("行業分佈", p.by_industry);
  fillTable(col, p);
  return col;
}

function fillTable(col, p) {
  const rows = p.positions || [];
  const head = col.querySelector("thead tr"), body = col.querySelector("tbody");
  if (!rows.length) { body.innerHTML = `<tr><td colspan="4">暫時冇持倉</td></tr>`; return; }

  const cols = [
    {k: "code",          t: "代號", num: false},
    {k: "weight_pct",    t: "比重", num: true, fn: v => pc(v)},
    {k: "current_price", t: "現價", num: true, fn: v => fmt(v, Math.abs(v) < 1 ? 4 : 2)},
    {k: "return_pct",    t: "回報", num: true, fn: v => sg(v), color: true},
  ];
  let sortCol = "weight_pct", sortDir = -1;

  const draw = () => {
    head.innerHTML = "";
    cols.forEach(c => {
      const th = document.createElement("th");
      th.textContent = c.t;
      if (c.num) th.className = "num";
      if (c.k === sortCol) th.classList.add("sorted");
      th.onclick = () => { sortDir = (c.k === sortCol) ? -sortDir : -1; sortCol = c.k; draw(); };
      head.appendChild(th);
    });
    const meta = cols.find(c => c.k === sortCol);
    const data = [...rows].sort((a, b) => {
      const x = a[sortCol], y = b[sortCol];
      if (x === null || x === undefined) return 1;
      if (y === null || y === undefined) return -1;
      return (meta && meta.num ? (x - y) : String(x).localeCompare(String(y))) * sortDir;
    });

    body.innerHTML = "";
    data.forEach(r => {
      const tr = document.createElement("tr");
      tr.title = `${r.name ?? ""}　${r.market ?? ""}　成本 ${fmt(r.cost_price)}`;
      cols.forEach(c => {
        const td = document.createElement("td");
        const v = r[c.k];
        if (c.num) td.classList.add("num");
        if (c.k === "code") td.classList.add("sym");
        td.textContent = (v === null || v === undefined) ? "—" : (c.fn ? c.fn(v) : v);
        if (c.color) td.classList.add(cls(v));
        tr.appendChild(td);
      });
      body.appendChild(tr);
    });

    if (__SHOWCASH__ && p.cash_pct !== null && p.cash_pct !== undefined) {
      const tr = document.createElement("tr");
      tr.className = "cash";
      tr.innerHTML = `<td>現金</td><td class="num">${pc(p.cash_pct)}</td>` +
                     `<td class="num">—</td><td class="num">—</td>`;
      body.appendChild(tr);
    }
  };
  draw();
}

function renderSpark(svg, points) {
  const vals = points.map(p => p.weighted_return_pct)
                     .filter(v => typeof v === "number" && isFinite(v));
  if (vals.length < 2) { svg.style.display = "none"; return; }
  const W = 1000, H = 100, lo = Math.min(...vals, 0), hi = Math.max(...vals, 0);
  const flat = hi === lo, span = flat ? 1 : hi - lo;
  const x = i => (i / (vals.length - 1)) * W;
  const y = v => flat ? H / 2 : H - ((v - lo) / span) * (H - 12) - 6;
  const line = vals.map((v, i) => (i ? "L" : "M") + x(i).toFixed(1) + " " + y(v).toFixed(1)).join(" ");
  const col = vals[vals.length - 1] >= 0 ? "var(--up)" : "var(--down)";
  const zero = y(0).toFixed(1);
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.innerHTML =
    `<line x1="0" y1="${zero}" x2="${W}" y2="${zero}" stroke="var(--line)" stroke-width="1"` +
    ` vector-effect="non-scaling-stroke"/>` +
    `<path d="${line} L ${W} ${zero} L 0 ${zero} Z" fill="${col}" opacity=".07"/>` +
    `<path d="${line}" fill="none" stroke="${col}" stroke-width="2"` +
    ` vector-effect="non-scaling-stroke" stroke-linejoin="round"/>`;
}

async function refresh() {
  try {
    const snap = await (await load(DATA)).json();
    const grid = document.getElementById("grid");
    grid.innerHTML = "";
    const cols = [];
    (snap.portfolios || []).forEach(p => {
      const col = buildColumn(p);
      grid.appendChild(col);
      cols.push([p, col]);
    });
    document.getElementById("msg").hidden = true;

    const t = (snap.generated_at || "").replace("T", " ").slice(0, 16);
    document.getElementById("stamp").textContent = "更新於 " + t + " " + (snap.timezone_label || "");

    for (const [p, col] of cols) {
      const svg = col.querySelector("svg.spark");
      try {
        const txt = await (await load(HIST_PREFIX + p.key + ".jsonl")).text();
        renderSpark(svg, txt.trim().split("\n").filter(Boolean).map(JSON.parse));
      } catch (e) { svg.style.display = "none"; }
    }
  } catch (err) {
    const m = document.getElementById("msg");
    m.hidden = false;
    m.textContent = "讀取唔到資料檔：" + err.message;
    document.getElementById("stamp").textContent = "資料未就緒";
  }
}

refresh();
setInterval(refresh, 5 * 60 * 1000);
</script>
</body>
</html>
"""


def render_index_html() -> str:
    return (INDEX_HTML
            .replace("__TITLE__", PARAM_SITE_TITLE)
            .replace("__TAGLINE__", PARAM_SITE_TAGLINE)
            .replace("__SNAPSHOT__", PARAM_SNAPSHOT_FILENAME)
            .replace("__HISTPREFIX__", PARAM_HISTORY_PREFIX)
            .replace("__SHOWCASH__", "true" if PARAM_SHOW_CASH_ROW else "false"))


# ======================================================================
# 5. GitHub Actions workflow
# ======================================================================

WORKFLOW_YML = """name: Update portfolio

on:
  schedule:
    - cron: "__CRON__"
  workflow_dispatch:

permissions:
  contents: write

concurrency:
  group: update-portfolio
  cancel-in-progress: false

jobs:
  update:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Fetch and build
        env:
__ENVLINES__
        run: python update_portfolio.py

      - name: Commit updated data
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
          git add __SITEDIR__
          git diff --staged --quiet || git commit -m "portfolio: $(date -u +'%Y-%m-%dT%H:%MZ')"
          git push
"""


def render_workflow() -> str:
    env_lines = "\n".join(
        f"          {c['url_env']}: ${{{{ secrets.{c['url_env']} }}}}"
        for c in PARAM_PORTFOLIOS
    )
    return (WORKFLOW_YML
            .replace("__CRON__", PARAM_WORKFLOW_CRON)
            .replace("__ENVLINES__", env_lines)
            .replace("__SITEDIR__", PARAM_SITE_DIR))


# ======================================================================
# 6. 本機預覽 server
# ======================================================================


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:
        pass

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


def serve_preview(site: Path) -> None:
    handler = functools.partial(_QuietHandler, directory=str(site))
    socketserver.TCPServer.allow_reuse_address = True
    try:
        httpd = socketserver.TCPServer(("127.0.0.1", PARAM_PREVIEW_PORT), handler)
    except OSError as exc:
        log(f"起唔到預覽 server（port {PARAM_PREVIEW_PORT}）：{exc}")
        log("可能係上一次未關閉，或者 port 被佔用。改 PARAM_PREVIEW_PORT 再試。")
        return
    url = f"http://127.0.0.1:{PARAM_PREVIEW_PORT}/"
    log(f"預覽已啟動：{url}")
    log("撳 PyCharm 嘅停止鍵（紅色方塊）即可關閉。")
    if PARAM_PREVIEW_OPEN_BROWSER:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log("預覽已關閉。")
    finally:
        httpd.server_close()


# ======================================================================
# main
# ======================================================================


def main() -> int:
    root = Path(__file__).resolve().parent
    site = root / PARAM_SITE_DIR
    data = site / PARAM_DATA_DIR
    snapshot_path = data / PARAM_SNAPSHOT_FILENAME

    selftest = PARAM_SELFTEST
    if selftest and running_in_actions():
        log("偵測到 GitHub Actions 環境，已自動停用 SELFTEST，改為抓取真實來源。")
        selftest = False
    if selftest:
        log("SELFTEST 模式：使用內建樣本，唔會出網絡")

    previous = read_previous(snapshot_path)
    results: list[dict[str, Any]] = []
    fresh_keys: list[str] = []

    for cfg in PARAM_PORTFOLIOS:
        key, label = cfg["key"], cfg["label"]
        log(f"── {label}（{key}）")
        try:
            if selftest:
                payload = SELFTEST_PAYLOADS.get(key)
                if payload is None:
                    raise RuntimeError(f"SELFTEST 冇 '{key}' 嘅樣本，請喺 SELFTEST_PAYLOADS 補上。")
            else:
                url = resolve_url(cfg)
                log(f"  抓取來源：{url.split('?')[0]}")
                payload = fetch_json(url)
            p = build_portfolio(cfg, payload)
        except Exception as exc:
            log(f"  失敗：{exc}")
            old = previous.get(key)
            if old:
                log("  沿用上一次成功嘅資料，標示為未更新。")
                old.update({"ok": False, "stale": True, "error": str(exc), "label": label})
                results.append(old)
            else:
                log("  冇舊資料可以沿用，該組合喺頁面上會顯示為空。")
                results.append({
                    "key": key, "label": label, "ok": False, "stale": True,
                    "error": str(exc), "updated_at": None,
                    "invested_pct": None, "cash_pct": None,
                    "weighted_return_pct": None, "position_count": 0,
                    "positions": [], "by_market": [], "by_industry": [],
                })
            continue

        results.append(p)
        fresh_keys.append(key)
        log(f"  持倉 {p['position_count']} 個，比重合計 {p['invested_pct']:.4f}%，"
            f"加權未實現回報 {p['weighted_return_pct']:+.4f}%")
        for row in p["positions"]:
            log(f"    {str(row['code']):<8} {row['weight_pct']:>7.4f}%  "
                f"{row['cost_price']:>10.4f} → {row['current_price']:<10.4f} "
                f"{row['return_pct']:+.4f}%")

    if not fresh_keys:
        log("全部來源都失敗，唔覆蓋任何資料。")
        return 1

    now = local_now()
    snapshot = {
        "generated_at": now.isoformat(timespec="seconds"),
        "generated_at_utc": now.astimezone(timezone.utc).isoformat(timespec="seconds"),
        "timezone_label": PARAM_TIMEZONE_LABEL,
        "stale_hours": PARAM_STALE_HOURS,
        "portfolios": results,
    }
    write_text(snapshot_path, json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n")

    for p in results:                       # 只有今次抓成功嘅先寫入歷史
        if p["key"] in fresh_keys:
            append_history(data / f"{PARAM_HISTORY_PREFIX}{p['key']}.jsonl", p)

    index = site / "index.html"
    if PARAM_FORCE_REWRITE_HTML or not index.exists():
        write_text(index, render_index_html())
    write_text(site / ".nojekyll", "")

    if not running_in_actions():
        wf = root / ".github" / "workflows" / "update-portfolio.yml"
        if PARAM_FORCE_REWRITE_WORKFLOW or not wf.exists():
            write_text(wf, render_workflow())
            log(f"workflow cron = '{PARAM_WORKFLOW_CRON}'（記得 commit 個 yml 先會生效）")

    if len(fresh_keys) < len(PARAM_PORTFOLIOS):
        log(f"注意：{len(PARAM_PORTFOLIOS) - len(fresh_keys)} 個組合抓取失敗，"
            f"已沿用舊資料。結束碼仍為 0，以免 Actions 跳過 commit 步驟。")
    log("完成。")

    if PARAM_SERVE_PREVIEW and not running_in_actions() and not PARAM_DRY_RUN:
        serve_preview(site)
    return 0


if __name__ == "__main__":
    sys.exit(main())
