"""
update_portfolio.py
===================
每小時抓取來源 portfolio JSON，寫入靜態網站資料夾，由 GitHub Actions commit
回 repo，經 GitHub Pages 發佈。

來源 JSON 結構（已針對實際 payload 寫死解析邏輯）：
    {"code":0,"message":"...","data":{
        "market_items":  [{"tab_name","ratio","market"}],
        "industry_items":[{"tab_name","ratio","market"}],
        "record_items":  [{"stock_code","stock_name","market","total_ratio",
                           "cost_price","current_price","profit_and_loss_ratio",...}]}}

比例尺（由實際數據反推，見 PARAM_SCALE_*）：
    *_ratio               1e9 = 100%   →  除 1e7 得百分比
    cost_price/current_price  1e9 = 1.0 →  除 1e9 得價格
    profit_and_loss_ratio 係回報率，唔係金額。

三種執行模式（改頂部 PARAM_* 常數，唔需要 terminal 參數）：
 1. PARAM_SELFTEST = True                用內建真實樣本離線行一次（首次請用呢個）
 2. PARAM_SELFTEST = False + FALLBACK URL 本機真實抓取一次
 3. GitHub Actions                        由 workflow 提供 PORTFOLIO_SOURCE_URL

輸出：
    docs/index.html                          靜態頁
    docs/data/portfolio.json                 最新快照
    docs/data/history.jsonl                  每次 append 一行，用嚟畫走勢
    .github/workflows/update-portfolio.yml   排程（只喺本機、檔案不存在時生成）
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# ======================================================================
# 可調參數
# ======================================================================

# --- 來源 ---
PARAM_SOURCE_URL_ENV: str = "PORTFOLIO_SOURCE_URL"
PARAM_SOURCE_URL_FALLBACK: str = ""          # 只供本機測試，切勿 commit 真 URL
PARAM_HTTP_TIMEOUT_SEC: float = 20.0
PARAM_HTTP_RETRIES: int = 3
PARAM_HTTP_BACKOFF_SEC: float = 3.0
PARAM_USER_AGENT: str = "portfolio-publisher/1.0"

# --- 來源結構 ---
PARAM_ENVELOPE_KEY: str = "data"             # 外層包裝
PARAM_OK_CODE: int = 0                       # code == 0 先當成功
PARAM_RECORDS_KEY: str = "record_items"
PARAM_MARKET_KEY: str = "market_items"
PARAM_INDUSTRY_KEY: str = "industry_items"

# --- 比例尺 ---
PARAM_SCALE_RATIO: float = 1e9               # ratio 1e9 == 100%
PARAM_SCALE_PRICE: float = 1e9               # price 1e9 == 1.0

# --- market code 對照（只有 2=US 有數據佐證，其餘為推測，請自行核實補充）---
PARAM_MARKET_NAMES: dict[int, str] = {
    1: "HK",
    2: "US",
    3: "CN",
}

# --- 輸出 ---
PARAM_SITE_DIR: str = "docs"
PARAM_DATA_DIR: str = "data"
PARAM_SNAPSHOT_FILENAME: str = "portfolio.json"
PARAM_HISTORY_FILENAME: str = "history.jsonl"
PARAM_HISTORY_MAX_ROWS: int = 24 * 365       # 保留約一年（每小時一行）

# --- 顯示 ---
PARAM_SITE_TITLE: str = "Portfolio"
PARAM_SITE_TAGLINE: str = "每小時自動更新嘅持倉比重與未實現回報"
PARAM_SHOW_CASH_ROW: bool = True             # 顯示 100% − 持倉比重 為現金
PARAM_TIMEZONE_OFFSET_HOURS: int = 8
PARAM_TIMEZONE_LABEL: str = "HKT"

# --- 行為 ---
PARAM_SELFTEST: bool = False                  # 首次執行請保持 True
PARAM_DRY_RUN: bool = False                  # True = 只印結果，唔寫檔
PARAM_FORCE_REWRITE_HTML: bool = True
PARAM_WRITE_WORKFLOW: bool = True
PARAM_WORKFLOW_CRON: str = "5 * * * *"       # 每小時第 5 分，避開整點高峰
PARAM_PYTHON_VERSION: str = "3.11"
PARAM_FAIL_ON_FETCH_ERROR: bool = True       # 失敗 = 非零結束碼，但保留舊資料

# ======================================================================
# 內建樣本（PARAM_SELFTEST 用；即你提供嘅真實 payload）
# ======================================================================

SELFTEST_PAYLOAD: dict[str, Any] = {
    "code": 0, "message": "成功",
    "data": {
        "market_items": [{"tab_name": "US", "ratio": 900421186, "market": 2}],
        "industry_items": [
            {"tab_name": "Others", "ratio": 603450810, "market": 12},
            {"tab_name": "Non-Bank Financials", "ratio": 296970376, "market": 10},
        ],
        "record_items": [
            {"stock_id": 201909, "stock_code": "XLE",
             "stock_name": "Energy Select Sector SPDR Fund", "market": 2,
             "total_ratio": 199606749, "position_ratio": 199606749, "pending_ratio": 0,
             "cost_price": 63047216429, "current_price": 64060000000,
             "profit_and_loss_ratio": 16063890, "status": 2},
            {"stock_id": 202302, "stock_code": "EWZ",
             "stock_name": "iShares MSCI Brazil ETF", "market": 2,
             "total_ratio": 199645768, "position_ratio": 199645768, "pending_ratio": 0,
             "cost_price": 37990000000, "current_price": 37860000000,
             "profit_and_loss_ratio": -3421953, "status": 2},
            {"stock_id": 203520, "stock_code": "BRK.B",
             "stock_name": "Berkshire Hathaway-B", "market": 2,
             "total_ratio": 296970376, "position_ratio": 296970376, "pending_ratio": 0,
             "cost_price": 507737874491, "current_price": 506030000000,
             "profit_and_loss_ratio": -3363693, "status": 2},
            {"stock_id": 205078, "stock_code": "GLD",
             "stock_name": "SPDR Gold ETF", "market": 2,
             "total_ratio": 204198293, "position_ratio": 204198293, "pending_ratio": 0,
             "cost_price": 399330273613, "current_price": 406770000000,
             "profit_and_loss_ratio": 18630509, "status": 2},
        ],
    },
}

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
    """ratio 原始值 → 百分比。1e9 == 100%"""
    n = to_number(raw)
    return None if n is None else n / PARAM_SCALE_RATIO * 100.0


def price(raw: Any) -> float | None:
    n = to_number(raw)
    return None if n is None else n / PARAM_SCALE_PRICE


def market_name(code: Any) -> str:
    n = to_number(code)
    if n is None:
        return "—"
    return PARAM_MARKET_NAMES.get(int(n), f"Market {int(n)}")


def running_in_actions() -> bool:
    return os.environ.get("GITHUB_ACTIONS", "").lower() == "true"


# ======================================================================
# 1. 抓取
# ======================================================================


def resolve_source_url() -> str:
    url = os.environ.get(PARAM_SOURCE_URL_ENV, "").strip() or PARAM_SOURCE_URL_FALLBACK.strip()
    if not url:
        raise RuntimeError(
            f"搵唔到來源 URL。請設定環境變數 {PARAM_SOURCE_URL_ENV}，"
            f"或喺本機測試時填 PARAM_SOURCE_URL_FALLBACK。"
        )
    return url


def fetch_json(url: str) -> Any:
    last_err: Exception | None = None
    for attempt in range(1, PARAM_HTTP_RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": PARAM_USER_AGENT,
                "Accept": "application/json",
            })
            with urllib.request.urlopen(req, timeout=PARAM_HTTP_TIMEOUT_SEC) as resp:
                return json.loads(resp.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as exc:
            last_err = exc
            if 400 <= exc.code < 500 and exc.code != 429:
                raise RuntimeError(
                    f"來源回應 HTTP {exc.code}，屬用戶端錯誤，唔會重試。請檢查 URL 或授權。"
                ) from exc
            log(f"抓取失敗（第 {attempt}/{PARAM_HTTP_RETRIES} 次）：{exc}")
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            last_err = exc
            log(f"抓取失敗（第 {attempt}/{PARAM_HTTP_RETRIES} 次）：{exc}")
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


def parse_positions(body: dict[str, Any]) -> list[dict[str, Any]]:
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
        # 用價格獨立重算回報，同來源欄位互相印證
        recomputed = ((cur - cost) / cost * 100.0) if (cost and cur is not None) else None
        if ret is not None and recomputed is not None and abs(ret - recomputed) > 0.05:
            log(f"注意：{r.get('stock_code')} 來源回報 {ret:.4f}% 與由價格重算嘅 "
                f"{recomputed:.4f}% 有出入，比例尺假設可能有誤。")
        out.append({
            "code": r.get("stock_code"),
            "name": r.get("stock_name"),
            "market": market_name(r.get("market")),
            "weight_pct": pct(r.get("total_ratio")),
            "pending_pct": pct(r.get("pending_ratio")),
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


def build_snapshot(payload: Any) -> dict[str, Any]:
    body = unwrap(payload)
    positions = parse_positions(body)

    weights = [p["weight_pct"] for p in positions if p["weight_pct"] is not None]
    invested = sum(weights) if weights else None

    pairs = [(p["weight_pct"], p["return_pct"]) for p in positions
             if p["weight_pct"] is not None and p["return_pct"] is not None]
    wsum = sum(w for w, _ in pairs)
    weighted_return = (sum(w * r for w, r in pairs) / wsum) if wsum else None

    now = local_now()
    return {
        "updated_at": now.isoformat(timespec="seconds"),
        "updated_at_utc": now.astimezone(timezone.utc).isoformat(timespec="seconds"),
        "timezone_label": PARAM_TIMEZONE_LABEL,
        "invested_pct": invested,
        "cash_pct": (100.0 - invested) if invested is not None else None,
        "weighted_return_pct": weighted_return,
        "position_count": len(positions),
        "positions": positions,
        "by_market": parse_breakdown(body, PARAM_MARKET_KEY),
        "by_industry": parse_breakdown(body, PARAM_INDUSTRY_KEY),
        "show_cash_row": PARAM_SHOW_CASH_ROW,
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


def append_history(path: Path, snap: dict[str, Any]) -> None:
    row = {
        "ts": snap["updated_at"],
        "weighted_return_pct": snap["weighted_return_pct"],
        "invested_pct": snap["invested_pct"],
        "position_count": snap["position_count"],
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
  .wrap{max-width:1040px; margin:0 auto; padding:28px 20px 72px}
  header{display:flex; justify-content:space-between; align-items:baseline;
         gap:16px; flex-wrap:wrap; padding-bottom:20px}
  h1{margin:0; font-size:19px; font-weight:600; letter-spacing:.01em}
  .tag{margin:2px 0 0; color:var(--muted); font-size:13px}
  .stamp{color:var(--muted); font-size:13px; display:flex; align-items:center; gap:8px}
  .dot{width:7px; height:7px; border-radius:50%; background:var(--up); flex:none}
  .dot.stale{background:var(--gold)}

  .hero{border-top:1px solid var(--line); border-bottom:1px solid var(--line);
        padding:26px 0 20px}
  .total{font-size:clamp(38px,8vw,64px); font-weight:600; letter-spacing:-.02em; line-height:1}
  .heroSub{margin-top:12px; color:var(--muted); font-size:14.5px}
  .heroSub span{color:var(--paper)}
  .up{color:var(--up)} .down{color:var(--down)}
  svg.spark{width:100%; height:96px; display:block; margin-top:22px; overflow:hidden}

  h2{font-size:13px; font-weight:500; color:var(--muted); margin:34px 0 12px}
  .bars{display:grid; gap:9px}
  .bar{display:grid; grid-template-columns:minmax(120px,1.1fr) 3fr 64px;
       gap:12px; align-items:center; font-size:13.5px}
  .track{height:6px; background:var(--slate)}
  .fill{height:6px; background:var(--gold); opacity:.8}
  .bar .pctv{text-align:right; color:var(--muted)}
  .bar .lbl{overflow:hidden; text-overflow:ellipsis; white-space:nowrap}

  table{width:100%; border-collapse:collapse; margin-top:12px; font-size:14px}
  th{font-weight:500; color:var(--muted); text-align:left; padding:9px 12px;
     border-bottom:1px solid var(--line); white-space:nowrap; cursor:pointer;
     user-select:none; font-size:13px}
  th:hover{color:var(--paper)}
  th.sorted{color:var(--paper)}
  th.sorted::after{content:"·"; margin-left:6px; color:var(--gold)}
  td{padding:11px 12px; border-bottom:1px solid rgba(37,56,74,.5); white-space:nowrap}
  td.num,th.num{text-align:right}
  td.name{color:var(--muted); white-space:normal; min-width:180px}
  tbody tr:hover{background:var(--slate)}
  tbody tr.cash td{color:var(--muted)}
  .note{margin-top:32px; color:var(--muted); font-size:12.5px; line-height:1.7}
  .msg{padding:40px 0; color:var(--muted)}
  @media (max-width:640px){
    .wrap{padding:20px 14px 56px}
    td,th{padding:9px 8px}
    .tablescroll{overflow-x:auto; -webkit-overflow-scrolling:touch}
    .bar{grid-template-columns:minmax(96px,1fr) 2fr 56px; font-size:13px}
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
    <div class="stamp"><span class="dot" id="dot"></span><span id="stampText">載入中</span></div>
  </header>

  <section class="hero">
    <div class="total" id="total">—</div>
    <div class="heroSub" id="heroSub"></div>
    <svg class="spark" id="spark" preserveAspectRatio="none" aria-hidden="true"></svg>
  </section>

  <div id="blocks"></div>

  <h2 id="posHead" hidden>持倉明細</h2>
  <div class="tablescroll">
    <table id="tbl" hidden>
      <thead><tr id="head"></tr></thead>
      <tbody id="body"></tbody>
    </table>
  </div>
  <div class="msg" id="msg">正在讀取倉位資料</div>

  <p class="note">
    比重為佔整體組合嘅百分比；回報為現價相對成本價嘅未實現變動，未計股息、費用及匯率。<br>
    資料每小時由排程自動更新，頁面每 5 分鐘重新讀取一次檔案。更新時間超過兩小時未變，狀態點會轉為金色。<br>
    僅作記錄用途，並非投資建議。
  </p>
</div>

<script>
const DATA = "data/__SNAPSHOT__";
const HIST = "data/__HISTORY__";

const fmt = (n, d = 2) =>
  (n === null || n === undefined || !isFinite(n)) ? "—"
  : n.toLocaleString("en-US", {minimumFractionDigits: d, maximumFractionDigits: d});
const pct = (n, d = 2) => n === null || n === undefined ? "—" : fmt(n, d) + "%";
const signed = (n, d = 2) =>
  n === null || n === undefined ? "—" : (n >= 0 ? "+" : "−") + fmt(Math.abs(n), d) + "%";
const cls = (n) => n === null || n === undefined ? "" : (n >= 0 ? "up" : "down");

async function load(url) {
  const r = await fetch(url + "?t=" + Date.now(), {cache: "no-store"});
  if (!r.ok) throw new Error(url + " → HTTP " + r.status);
  return r;
}

function renderHero(s) {
  const el = document.getElementById("total");
  el.textContent = signed(s.weighted_return_pct);
  el.className = "total " + cls(s.weighted_return_pct);
  document.getElementById("heroSub").innerHTML =
    `加權未實現回報　·　持倉比重 <span>${pct(s.invested_pct)}</span>` +
    `　·　現金 <span>${pct(s.cash_pct)}</span>　·　<span>${s.position_count}</span> 個持倉`;

  const t = s.updated_at.replace("T", " ").slice(0, 16);
  document.getElementById("stampText").textContent = "更新於 " + t + " " + (s.timezone_label || "");
  if ((Date.now() - new Date(s.updated_at).getTime()) / 3.6e6 > 2)
    document.getElementById("dot").classList.add("stale");
}

function barBlock(title, items) {
  if (!items || !items.length) return "";
  const max = 100;   // 統一以 100% 為基準，令唔同區塊嘅條長可以直接比較
  const rows = items.map(i => `
    <div class="bar">
      <div class="lbl">${i.name ?? "—"}</div>
      <div class="track"><div class="fill" style="width:${Math.min(100, (i.weight_pct || 0) / max * 100).toFixed(1)}%"></div></div>
      <div class="pctv">${pct(i.weight_pct)}</div>
    </div>`).join("");
  return `<h2>${title}</h2><div class="bars">${rows}</div>`;
}

function renderTable(s) {
  const rows = s.positions || [];
  if (!rows.length) { document.getElementById("msg").textContent = "暫時冇持倉"; return; }

  const cols = [
    {k: "code",          t: "代號",   num: false},
    {k: "name",          t: "名稱",   num: false, name: true},
    {k: "market",        t: "市場",   num: false},
    {k: "weight_pct",    t: "比重",   num: true,  fn: v => pct(v)},
    {k: "cost_price",    t: "成本價", num: true,  fn: v => fmt(v, Math.abs(v) < 1 ? 4 : 2)},
    {k: "current_price", t: "現價",   num: true,  fn: v => fmt(v, Math.abs(v) < 1 ? 4 : 2)},
    {k: "return_pct",    t: "回報",   num: true,  fn: v => signed(v), color: true},
  ];
  const head = document.getElementById("head"), body = document.getElementById("body");
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

    const col = cols.find(c => c.k === sortCol);
    const data = [...rows].sort((a, b) => {
      const x = a[sortCol], y = b[sortCol];
      if (x === null || x === undefined) return 1;
      if (y === null || y === undefined) return -1;
      return (col && col.num ? (x - y) : String(x).localeCompare(String(y))) * sortDir;
    });

    body.innerHTML = "";
    data.forEach(r => {
      const tr = document.createElement("tr");
      cols.forEach(c => {
        const td = document.createElement("td");
        const v = r[c.k];
        if (c.num) td.classList.add("num");
        if (c.name) td.classList.add("name");
        td.textContent = (v === null || v === undefined) ? "—" : (c.fn ? c.fn(v) : v);
        if (c.color) td.classList.add(cls(v));
        tr.appendChild(td);
      });
      body.appendChild(tr);
    });

    if (s.show_cash_row && s.cash_pct !== null && s.cash_pct !== undefined) {
      const tr = document.createElement("tr");
      tr.className = "cash";
      tr.innerHTML = `<td>現金</td><td class="name">未投資部分</td><td>—</td>` +
                     `<td class="num">${pct(s.cash_pct)}</td>` +
                     `<td class="num">—</td><td class="num">—</td><td class="num">—</td>`;
      body.appendChild(tr);
    }
  };

  draw();
  document.getElementById("posHead").hidden = false;
  document.getElementById("tbl").hidden = false;
  document.getElementById("msg").hidden = true;
}

function renderSpark(points) {
  const svg = document.getElementById("spark");
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
    const s = await (await load(DATA)).json();
    renderHero(s);
    document.getElementById("blocks").innerHTML =
      barBlock("市場分佈", s.by_market) + barBlock("行業分佈", s.by_industry);
    renderTable(s);
    try {
      const txt = await (await load(HIST)).text();
      renderSpark(txt.trim().split("\n").filter(Boolean).map(JSON.parse));
    } catch (e) { document.getElementById("spark").style.display = "none"; }
  } catch (err) {
    document.getElementById("msg").textContent = "讀取唔到資料檔：" + err.message;
    document.getElementById("stampText").textContent = "資料未就緒";
    document.getElementById("dot").classList.add("stale");
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
            .replace("__HISTORY__", PARAM_HISTORY_FILENAME))


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
          python-version: "__PYVER__"

      - name: Fetch and build
        env:
          __URLENV__: ${{ secrets.__URLENV__ }}
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
    return (WORKFLOW_YML
            .replace("__CRON__", PARAM_WORKFLOW_CRON)
            .replace("__PYVER__", PARAM_PYTHON_VERSION)
            .replace("__URLENV__", PARAM_SOURCE_URL_ENV)
            .replace("__SITEDIR__", PARAM_SITE_DIR))


# ======================================================================
# main
# ======================================================================


def main() -> int:
    root = Path(__file__).resolve().parent
    site = root / PARAM_SITE_DIR
    data = site / PARAM_DATA_DIR

    selftest = PARAM_SELFTEST
    if selftest and running_in_actions():
        log("偵測到 GitHub Actions 環境，已自動停用 SELFTEST，改為抓取真實來源。")
        selftest = False

    if selftest:
        log("SELFTEST 模式：使用內建樣本，唔會出網絡")
        payload: Any = SELFTEST_PAYLOAD
    else:
        try:
            url = resolve_source_url()
            log(f"抓取來源：{url.split('?')[0]}")
            payload = fetch_json(url)
        except Exception as exc:
            log(f"錯誤：{exc}")
            log("保留上一次成功嘅資料，唔覆蓋。")
            return 1 if PARAM_FAIL_ON_FETCH_ERROR else 0

    try:
        snap = build_snapshot(payload)
    except Exception as exc:
        log(f"解析錯誤：{exc}")
        return 1

    log(f"持倉 {snap['position_count']} 個，"
        f"比重合計 {snap['invested_pct']:.4f}%，"
        f"加權未實現回報 {snap['weighted_return_pct']:+.4f}%")
    for p in snap["positions"]:
        log(f"  {p['code']:<8} {p['weight_pct']:>7.4f}%  "
            f"{p['cost_price']:>10.4f} → {p['current_price']:<10.4f} "
            f"{p['return_pct']:+.4f}%")

    write_text(data / PARAM_SNAPSHOT_FILENAME,
               json.dumps(snap, ensure_ascii=False, indent=2) + "\n")
    append_history(data / PARAM_HISTORY_FILENAME, snap)

    index = site / "index.html"
    if PARAM_FORCE_REWRITE_HTML or not index.exists():
        write_text(index, render_index_html())
    write_text(site / ".nojekyll", "")

    if PARAM_WRITE_WORKFLOW and not running_in_actions():
        wf = root / ".github" / "workflows" / "update-portfolio.yml"
        if not wf.exists():
            write_text(wf, render_workflow())
        else:
            log(f"workflow 已存在，略過：{wf}")

    log("完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
