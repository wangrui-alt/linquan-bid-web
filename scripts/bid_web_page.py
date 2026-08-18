# -*- coding: utf-8 -*-
"""
招投标信息自动网页 — 临泉特供版
复用 crawler_engine 的零依赖爬虫引擎，SQLite 持久化，生成自包含的交互式 HTML 页面。
定时任务自动运行，其他人通过 Netlify 链接直接打开，无需手动爬取。

架构：crawler_engine.py（纯爬虫）+ bid_web_page.py（数据持久化 + 展示 + 部署）
"""

import json
import os
import sys
import logging
import zipfile
import io
import urllib.request
import urllib.error
import ssl
import time
import hashlib
import sqlite3
from datetime import datetime, timedelta

# 复用 crawler_engine 的爬虫引擎
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from crawler_engine import (
    crawl, PARSER_LIST, classify_and_sort, smart_dedupe,
    filter_by_region, parse_date, today_str,
    CATEGORY_LABELS, CATEGORY_COLORS, PROJECT_STATUS_MAP,
    DEFAULT_REGION_KW, DEFAULT_REGION_NAME,
)

# ==================== 基础设施 ====================
LOG_DIR = os.path.join(SCRIPT_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

import logging
from logging.handlers import RotatingFileHandler

# 清除 crawler_engine 可能已注册的 root handler，重新配置
_root = logging.getLogger()
_root.handlers.clear()
_root.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
_fh = RotatingFileHandler(os.path.join(LOG_DIR, "bid_web_page.log"), encoding="utf-8", maxBytes=5*1024*1024, backupCount=3)
_fh.setFormatter(_fmt)
_sh = logging.StreamHandler()
_sh.setFormatter(_fmt)
_root.addHandler(_fh)
_root.addHandler(_sh)
logger = logging.getLogger(__name__)

# 输出文件路径 — 桌面
DESKTOP = os.path.join(os.path.expanduser("~"), "Desktop")
OUTPUT_HTML = os.path.join(DESKTOP, "临泉招投标信息.html")

# ==================== SQLite 持久化 ====================
DATA_DIR = os.path.join(SCRIPT_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "bid_data.db")

DB_SCHEMA_ITEMS = """
CREATE TABLE IF NOT EXISTS items (
    id          TEXT PRIMARY KEY,   -- sha1(去重键)
    title       TEXT NOT NULL,
    project_no  TEXT DEFAULT '',
    notice_type TEXT DEFAULT '',
    buyer       TEXT DEFAULT '',
    budget      TEXT DEFAULT '',
    region      TEXT DEFAULT '',
    pub_date    TEXT DEFAULT '',
    source      TEXT DEFAULT '',
    url         TEXT DEFAULT '',
    category    TEXT DEFAULT '',
    priority    INTEGER DEFAULT 7,
    project_status TEXT DEFAULT '',
    first_seen  TEXT DEFAULT '',    -- 入库时间
    is_new      INTEGER DEFAULT 1  -- 本次爬取是否新增
);
"""

DB_SCHEMA_CRAWL_LOG = """
CREATE TABLE IF NOT EXISTS crawl_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    crawl_time  TEXT NOT NULL,
    site_stats  TEXT DEFAULT '{}',   -- JSON
    total_items INTEGER DEFAULT 0,
    new_items   INTEGER DEFAULT 0,
    duration    REAL DEFAULT 0
);
"""

DB_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_items_pub_date ON items(pub_date);",
    "CREATE INDEX IF NOT EXISTS idx_items_category ON items(category);",
    "CREATE INDEX IF NOT EXISTS idx_items_priority ON items(priority);",
    "CREATE INDEX IF NOT EXISTS idx_items_source ON items(source);",
    "CREATE INDEX IF NOT EXISTS idx_items_project_no ON items(project_no);",
]


def _get_db():
    """获取 SQLite 连接"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _init_db():
    """初始化数据库表和索引"""
    conn = _get_db()
    try:
        conn.executescript(DB_SCHEMA_ITEMS)
        conn.executescript(DB_SCHEMA_CRAWL_LOG)
        for idx_sql in DB_INDEXES:
            conn.execute(idx_sql)
        conn.commit()
        logger.info(f"数据库初始化完成: {DB_PATH}")
    finally:
        conn.close()


def _item_dedup_key(item):
    """计算去重键的 SHA1，与 smart_dedupe 逻辑一致"""
    norm_name = _normalize_title_for_dedupe(item.get("title", ""))
    notice_type = (item.get("notice_type") or "").strip()
    key = f"{hashlib.md5(norm_name.encode()).hexdigest()}:{notice_type}"
    return hashlib.sha1(key.encode()).hexdigest()


def _normalize_title_for_dedupe(title):
    """标题规范化去重（与 crawler_engine.smart_dedupe 逻辑一致）"""
    from crawler_engine import normalize_title_for_dedupe
    return normalize_title_for_dedupe(title)


def save_items_to_db(items):
    """增量入库，返回 (total_count, new_count)"""
    conn = _get_db()
    try:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        total = len(items)
        new_count = 0

        for item in items:
            dedup_id = _item_dedup_key(item)
            # 检查是否已存在
            row = conn.execute("SELECT id FROM items WHERE id=?", (dedup_id,)).fetchone()
            if row:
                # 已存在，更新来源合并
                existing = conn.execute("SELECT source, url FROM items WHERE id=?", (dedup_id,)).fetchone()
                if existing:
                    old_source = existing["source"] or ""
                    old_url = existing["url"] or ""
                    new_source = item.get("source", "")
                    new_url = item.get("url", "")
                    if new_source and new_source not in old_source:
                        merged_source = (old_source + " | " + new_source).strip(" |")
                        conn.execute("UPDATE items SET source=? WHERE id=?", (merged_source, dedup_id))
                    if new_url and new_url not in old_url:
                        merged_url = (old_url + " | " + new_url).strip(" |")
                        conn.execute("UPDATE items SET url=? WHERE id=?", (merged_url, dedup_id))
                continue

            # 新条目
            new_count += 1
            conn.execute("""
                INSERT OR IGNORE INTO items
                (id, title, project_no, notice_type, buyer, budget, region,
                 pub_date, source, url, category, priority, project_status,
                 first_seen, is_new)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            """, (
                dedup_id,
                item.get("title", ""),
                item.get("project_no", ""),
                item.get("notice_type", ""),
                item.get("buyer", ""),
                item.get("budget", ""),
                item.get("region", ""),
                item.get("pub_date", ""),
                item.get("source", ""),
                item.get("url", ""),
                item.get("category", ""),
                item.get("priority", 7),
                item.get("project_status", ""),
                now,
            ))

        conn.commit()
        logger.info(f"入库完成: 共 {total} 条，新增 {new_count} 条")
        return total, new_count
    finally:
        conn.close()


def log_crawl(site_stats, total_items, new_items, duration):
    """记录爬取日志"""
    conn = _get_db()
    try:
        conn.execute("""
            INSERT INTO crawl_log (crawl_time, site_stats, total_items, new_items, duration)
            VALUES (?, ?, ?, ?, ?)
        """, (
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            json.dumps(site_stats, ensure_ascii=False),
            total_items, new_items, duration,
        ))
        conn.commit()
    finally:
        conn.close()


def query_items(days=None):
    """查询数据库中的条目，返回列表

    days: 天数范围，None 表示全部
    """
    conn = _get_db()
    try:
        if days is not None:
            cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
            rows = conn.execute(
                "SELECT * FROM items WHERE pub_date >= ? ORDER BY priority, pub_date DESC",
                (cutoff,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM items ORDER BY priority, pub_date DESC"
            ).fetchall()

        items = []
        for row in rows:
            items.append({
                "id": row["id"],
                "title": row["title"],
                "project_no": row["project_no"],
                "notice_type": row["notice_type"],
                "buyer": row["buyer"],
                "budget": row["budget"],
                "region": row["region"],
                "pub_date": row["pub_date"],
                "source": row["source"],
                "url": row["url"],
                "category": row["category"],
                "priority": row["priority"],
                "project_status": row["project_status"],
                "first_seen": row["first_seen"],
                "is_new": row["is_new"],
            })
        return items
    finally:
        conn.close()


def query_stats(days=None):
    """查询统计数据，返回 dict {
        total, by_category: {cat: count}, by_date: {date: count},
        by_source: {source: count}, new_count
    }"""
    conn = _get_db()
    try:
        if days is not None:
            cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
            where = "WHERE pub_date >= ?"
            params = [cutoff]
        else:
            where = ""
            params = []

        total = conn.execute(f"SELECT COUNT(*) FROM items {where}", params).fetchone()[0]

        by_category = {}
        rows = conn.execute(f"SELECT category, COUNT(*) as cnt FROM items {where} GROUP BY category", params).fetchall()
        for r in rows:
            by_category[r[0] or "其他"] = r[1]

        by_date = {}
        rows = conn.execute(f"SELECT pub_date, COUNT(*) as cnt FROM items {where} GROUP BY pub_date ORDER BY pub_date", params).fetchall()
        for r in rows:
            by_date[r[0] or "未知"] = r[1]

        by_source = {}
        rows = conn.execute(f"SELECT source, COUNT(*) as cnt FROM items {where} GROUP BY source", params).fetchall()
        for r in rows:
            src = r[0] or "未知"
            # 多来源合并统计
            for s in src.split(" | "):
                s = s.strip()
                if s:
                    by_source[s] = by_source.get(s, 0) + r[1]

        # is_new 条件需要拼接 WHERE/AND
        new_where = f"{where} AND is_new=1" if where else "WHERE is_new=1"
        new_count = conn.execute(f"SELECT COUNT(*) FROM items {new_where}", params).fetchone()[0]

        return {
            "total": total,
            "by_category": by_category,
            "by_date": by_date,
            "by_source": by_source,
            "new_count": new_count,
        }
    finally:
        conn.close()


def get_latest_crawl_log():
    """获取最近一次爬取日志"""
    conn = _get_db()
    try:
        row = conn.execute(
            "SELECT * FROM crawl_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# ==================== 爬取 + 入库 ====================
def crawl_and_save(region_keywords=None):
    """爬取所有网站，增量入库，返回 (all_items, site_stats, new_count)"""
    start_time = time.time()

    # 爬取（不限制日期，尽量多抓）
    all_items, site_stats = crawl(target_dates=None, region_keywords=region_keywords)
    duration = time.time() - start_time

    # 先重置上一轮的 is_new 标记，再入库（入库时新条目 is_new=1）
    conn = _get_db()
    try:
        conn.execute("UPDATE items SET is_new=0 WHERE is_new=1")
        conn.commit()
    finally:
        conn.close()

    # 入库
    total, new_count = save_items_to_db(all_items)
    log_crawl(site_stats, total, new_count, duration)

    return all_items, site_stats, new_count


# ==================== HTML 生成 ====================
def build_web_html(region_name="临泉县"):
    """从数据库生成自包含的交互式 HTML 页面"""

    _today = today_str()
    _yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    _crawl_time = datetime.now().strftime("%Y-%m-%d %H:%M")

    # 从数据库查询数据
    all_items = query_items(days=None)  # 全量
    stats_all = query_stats(days=None)
    stats_7d = query_stats(days=7)
    stats_30d = query_stats(days=30)

    # 最新爬取日志
    latest_log = get_latest_crawl_log()
    site_stats = json.loads(latest_log["site_stats"]) if latest_log and latest_log.get("site_stats") else {}

    # 计算爬取健康状态
    fail_count = sum(1 for v in site_stats.values() if isinstance(v, int) and v < 0)
    total_sites = len(site_stats)
    if fail_count == 0 and total_sites > 0:
        health_class = "refresh-info ok"
        health_text = "全部数据源正常"
    elif fail_count == total_sites:
        health_class = "refresh-info error"
        health_text = "所有数据源爬取失败，请检查网络"
    elif total_sites > 0:
        health_class = "refresh-info warn"
        health_text = f"{fail_count}/{total_sites} 个数据源爬取失败"
    else:
        health_class = "refresh-info ok"
        health_text = "尚未爬取数据"

    # 序列化数据为 JSON
    data_json = json.dumps(all_items, ensure_ascii=False).replace("</", "<\\/")
    stats_all_json = json.dumps(stats_all, ensure_ascii=False).replace("</", "<\\/")
    stats_7d_json = json.dumps(stats_7d, ensure_ascii=False).replace("</", "<\\/")
    stats_30d_json = json.dumps(stats_30d, ensure_ascii=False).replace("</", "<\\/")
    site_stats_json = json.dumps(site_stats, ensure_ascii=False).replace("</", "<\\/")

    # 趋势数据（最近30天的每日条目数）
    trend_dates = []
    trend_counts = []
    for d in range(29, -1, -1):
        dt = (datetime.now() - timedelta(days=d)).strftime("%Y-%m-%d")
        trend_dates.append(dt[5:])  # MM-DD
        trend_counts.append(stats_all.get("by_date", {}).get(dt, 0))
    trend_labels_json = json.dumps(trend_dates, ensure_ascii=False)
    trend_values_json = json.dumps(trend_counts, ensure_ascii=False)

    # 来源占比数据
    source_pie_data = []
    for src, cnt in sorted(stats_all.get("by_source", {}).items(), key=lambda x: -x[1]):
        source_pie_data.append({"name": src, "value": cnt})
    source_pie_json = json.dumps(source_pie_data, ensure_ascii=False).replace("</", "<\\/")

    html = '''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__REGION_NAME__招投标信息日报</title>
<style>
:root {
  --bg: #f0f2f5; --card: #fff; --primary: #1B3A5C; --primary-light: #2C5F8A;
  --accent: #FF6B6B; --text: #333; --text2: #666; --text3: #999; --border: #e8e8e8;
  --radius: 8px; --shadow: 0 2px 12px rgba(0,0,0,0.08);
}
* { margin:0; padding:0; box-sizing:border-box; }
body { font-family:"Microsoft YaHei","PingFang SC",sans-serif; background:var(--bg); color:var(--text); min-height:100vh; }
.header { background:linear-gradient(135deg,#1B3A5C,#2C5F8A); color:#fff; padding:32px 24px 24px; text-align:center; }
.header h1 { font-size:24px; margin-bottom:8px; letter-spacing:1px; }
.header .meta { font-size:13px; opacity:0.85; }
.header .meta span { margin:0 8px; }
.controls { max-width:960px; margin:-20px auto 0; padding:0 16px; position:relative; z-index:10; }
.controls-inner { background:var(--card); border-radius:var(--radius); box-shadow:var(--shadow); padding:16px 20px; display:flex; flex-wrap:wrap; gap:12px; align-items:center; }
.tab-group { display:flex; border-radius:6px; overflow:hidden; border:1px solid var(--border); }
.tab-btn { padding:8px 16px; border:none; background:#f8f9fa; cursor:pointer; font-size:13px; color:var(--text2); transition:all .2s; font-weight:500; }
.tab-btn.active { background:var(--primary); color:#fff; }
.tab-btn:hover:not(.active) { background:#e8eaed; }
.search-box { flex:1; min-width:200px; position:relative; }
.search-box input { width:100%; padding:8px 12px 8px 36px; border:1px solid var(--border); border-radius:6px; font-size:14px; outline:none; transition:border .2s; }
.search-box input:focus { border-color:var(--primary); }
.search-box::before { content:"\\1F50D"; position:absolute; left:10px; top:50%; transform:translateY(-50%); font-size:14px; }
.filter-chips { display:flex; gap:6px; flex-wrap:wrap; }
.chip { padding:4px 12px; border-radius:16px; font-size:12px; border:1px solid var(--border); background:#fff; cursor:pointer; transition:all .2s; white-space:nowrap; }
.chip.active { border-color:var(--primary); background:#E8F0FE; color:var(--primary); font-weight:600; }
.chip:hover { border-color:var(--primary-light); }
.main { max-width:960px; margin:20px auto; padding:0 16px; }

/* 仪表盘 */
.dashboard { display:grid; grid-template-columns:1fr 1fr; gap:16px; margin-bottom:20px; }
.dash-card { background:var(--card); border-radius:var(--radius); box-shadow:var(--shadow); padding:16px; }
.dash-card h3 { font-size:14px; color:var(--text2); margin-bottom:12px; border-bottom:1px solid var(--border); padding-bottom:8px; }
.stats-row { display:flex; gap:12px; margin-bottom:20px; flex-wrap:wrap; }
.stat-card { flex:1; min-width:80px; background:var(--card); border-radius:var(--radius); padding:16px; text-align:center; box-shadow:var(--shadow); }
.stat-card .num { font-size:28px; font-weight:700; color:var(--primary); }
.stat-card .num.accent { color:var(--accent); }
.stat-card .label { font-size:12px; color:var(--text3); margin-top:4px; }
.trend-canvas { width:100%; height:120px; }
.source-list { list-style:none; }
.source-list li { display:flex; justify-content:space-between; padding:4px 0; font-size:13px; border-bottom:1px solid #f5f5f5; }
.source-list li:last-child { border:none; }
.source-bar { display:inline-block; height:14px; border-radius:3px; margin-left:8px; vertical-align:middle; }

/* 列表 */
.cat-group { margin-bottom:24px; }
.cat-header { display:flex; align-items:center; gap:8px; padding:10px 16px; border-radius:var(--radius) var(--radius) 0 0; color:#fff; font-weight:600; font-size:15px; cursor:pointer; }
.cat-header .count { background:rgba(255,255,255,0.3); border-radius:12px; padding:2px 10px; font-size:12px; font-weight:400; }
.cat-header .toggle { margin-left:auto; font-size:18px; transition:transform .2s; }
.cat-header.collapsed .toggle { transform:rotate(-90deg); }
.cat-body { background:var(--card); border-radius:0 0 var(--radius) var(--radius); box-shadow:var(--shadow); overflow:hidden; }
.cat-body.collapsed { display:none; }
.item { border-left:3px solid #ddd; padding:14px 16px; margin:0; background:#fafafa; transition:background .15s; cursor:pointer; }
.item:hover { background:#f0f0f0; }
.item+.item { border-top:1px solid #f0f0f0; }
.item-title { font-size:14px; font-weight:600; color:var(--text); margin-bottom:6px; line-height:1.6; }
.item-title a { color:inherit; text-decoration:none; }
.item-title a:hover { color:var(--primary-light); text-decoration:underline; }
.item-meta { display:flex; flex-wrap:wrap; gap:6px; align-items:center; }
.tag { display:inline-block; padding:2px 8px; border-radius:3px; font-size:11px; line-height:1.6; }
.tag-source { background:#f0f0f0; color:#555; }
.tag-date { background:#E8F0FE; color:var(--primary); }
.tag-region { background:#FFF3E0; color:#E65100; }
.tag-status { color:#fff; }
.tag-new { background:#F44336; color:#fff; font-weight:600; animation:pulse 1.5s infinite; }
@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.6} }
.item-detail { display:none; padding:12px 16px; background:#fff; border-top:1px dashed var(--border); font-size:13px; color:var(--text2); }
.item-detail.open { display:block; }
.detail-row { display:flex; gap:8px; margin-bottom:4px; }
.detail-row .key { color:var(--text3); min-width:70px; }
.detail-row .val { color:var(--text); }
.empty { text-align:center; padding:60px 20px; color:var(--text3); }
.empty-icon { font-size:48px; margin-bottom:12px; }
.footer { text-align:center; padding:24px; font-size:12px; color:var(--text3); }
@media (max-width:640px) {
  .controls-inner { flex-direction:column; }
  .stats-row { gap:8px; }
  .stat-card { min-width:60px; padding:12px 8px; }
  .stat-card .num { font-size:22px; }
  .item-meta { gap:4px; }
  .dashboard { grid-template-columns:1fr; }
}
.refresh-bar { max-width:960px; margin:0 auto; padding:0 16px; }
.refresh-info { background:linear-gradient(90deg,#E8F5E9,#fff); border:1px solid #C8E6C9; border-radius:var(--radius); padding:8px 16px; font-size:12px; color:#2E7D32; display:flex; justify-content:space-between; align-items:center; margin-bottom:16px; }
.refresh-info .dot { display:inline-block; width:8px; height:8px; background:#4CAF50; border-radius:50%; margin-right:6px; animation:blink 2s infinite; }
.refresh-info.warn { background:linear-gradient(90deg,#FFF3E0,#fff); border-color:#FFE0B2; color:#E65100; }
.refresh-info.warn .dot { background:#FF9800; }
.refresh-info.error { background:linear-gradient(90deg,#FFEBEE,#fff); border-color:#FFCDD2; color:#C62828; }
.refresh-info.error .dot { background:#F44336; }
@keyframes blink { 0%,100%{opacity:1} 50%{opacity:.3} }

/* 到期提醒 */
.tag-expiring { background:#FFF9C4; color:#F57F17; font-weight:600; }
.tag-expired { background:#FFCDD2; color:#C62828; font-weight:600; }

/* 导出按钮 */
.export-btn { padding:8px 16px; border:none; background:var(--primary); color:#fff; border-radius:6px; cursor:pointer; font-size:13px; font-weight:500; transition:all .2s; }
.export-btn:hover { background:var(--primary-light); }
</style>
</head>
<body>

<div class="header">
  <h1>__REGION_NAME__招投标信息日报</h1>
  <div class="meta">
    <span>更新于 __CRAWL_TIME__</span>
    <span>7个数据源自动采集</span>
    <span>SQLite 持久化存储</span>
  </div>
</div>

<div class="controls">
  <div class="controls-inner">
    <div class="tab-group" id="dateTabs">
      <button class="tab-btn active" onclick="switchDays(2,this)">今天+昨天</button>
      <button class="tab-btn" onclick="switchDays(7,this)">近7天</button>
      <button class="tab-btn" onclick="switchDays(30,this)">近30天</button>
    </div>
    <div class="search-box">
      <input type="text" id="searchInput" placeholder="搜索项目名称、招标单位..." oninput="renderItems()">
    </div>
    <button class="export-btn" onclick="exportCSV()">导出CSV</button>
  </div>
  <div style="margin-top:12px;">
    <div class="filter-chips" id="filterChips">
      <span class="chip active" data-cat="all" onclick="filterCategory('all',this)">全部</span>
    </div>
  </div>
</div>

<div class="main" id="mainContent">
  <div class="refresh-bar">
    <div class="__HEALTH_CLASS__">
      <span><span class="dot"></span>数据自动更新，上次爬取: __CRAWL_TIME__ | __HEALTH_TEXT__</span>
      <span id="totalCount"></span>
    </div>
  </div>

  <!-- 仪表盘 -->
  <div class="dashboard" id="dashboard">
    <div class="dash-card">
      <h3>近30天趋势</h3>
      <canvas id="trendCanvas" class="trend-canvas"></canvas>
    </div>
    <div class="dash-card">
      <h3>来源分布</h3>
      <ul class="source-list" id="sourceList"></ul>
    </div>
  </div>

  <div class="stats-row" id="statsRow"></div>
  <div id="contentArea"></div>
</div>

<div class="footer">
  临泉特供版招投标信息监控 | 7个数据源自动采集 | SQLite 持久化存储 | 星辰超级智能体生成
</div>

<script>
// ============ 数据 ============
var ALL_ITEMS = __DATA_JSON__;
var STATS_ALL = __STATS_ALL_JSON__;
var STATS_7D = __STATS_7D_JSON__;
var STATS_30D = __STATS_30D_JSON__;
var SITE_STATS = __SITE_STATS_JSON__;
var TREND_LABELS = __TREND_LABELS_JSON__;
var TREND_VALUES = __TREND_VALUES_JSON__;
var SOURCE_PIE = __SOURCE_PIE_JSON__;
var CATEGORY_COLORS = {
  "\u6838\u5fc3\u516c\u544a": "#FF6B6B", "\u91c7\u8d2d\u516c\u544a": "#4ECDC4", "\u7ed3\u679c\u516c\u793a": "#45B7D1",
  "\u66f4\u6b63\u6f84\u6e05": "#FFA07A", "\u8ba1\u5212\u9884\u516c\u793a": "#98D8C8", "\u901a\u77e5\u7ec8\u6b62": "#DDA0DD", "\u5176\u4ed6": "#D3D3D3"
};
var CATEGORY_PRIORITY = ["\u6838\u5fc3\u516c\u544a","\u91c7\u8d2d\u516c\u544a","\u7ed3\u679c\u516c\u793a","\u66f4\u6b63\u6f84\u6e05","\u8ba1\u5212\u9884\u516c\u793a","\u901a\u77e5\u7ec8\u6b62","\u5176\u4ed6"];
var SOURCE_COLORS = ["#FF6B6B","#4ECDC4","#45B7D1","#FFA07A","#98D8C8","#DDA0DD","#95E1D3","#F7DC6F"];

var currentDays = 2;
var currentCategory = "all";
var todayStr = "__TODAY__";
var yesterdayStr = "__YESTERDAY__";

// ============ 到期提醒 ============
function getExpiryTag(item) {
  // "\u62db\u6807\u4e2d"\u7684\u516c\u544a\uff0c\u53d1\u5e03\u8d85\u8fc715\u5929\u672a\u4e2d\u6807\u2192\u63d0\u9192
  if (!item.pub_date) return "";
  var p = item.priority || 7;
  if (p > 2) return "";  // \u4ec5\u62db\u6807\u4e2d/\u91c7\u8d2d\u4e2d
  var pubDate = new Date(item.pub_date);
  var now = new Date();
  var diffDays = Math.floor((now - pubDate) / 86400000);
  if (diffDays > 30) return '<span class="tag tag-expired">\u5df2\u8fc7\u671f</span>';
  if (diffDays > 15) return '<span class="tag tag-expiring">\u5373\u5c06\u5230\u671f</span>';
  return "";
}

// ============ 初始化 ============
function init() {
  // 分类芯片
  var chipContainer = document.getElementById("filterChips");
  for (var i = 0; i < CATEGORY_PRIORITY.length; i++) {
    var cat = CATEGORY_PRIORITY[i];
    var chip = document.createElement("span");
    chip.className = "chip";
    chip.dataset.cat = cat;
    chip.textContent = cat;
    chip.onclick = (function(c) { return function() { filterCategory(c, this); }; })(cat);
    chipContainer.appendChild(chip);
  }

  // 趋势图
  drawTrend();
  // 来源列表
  renderSourceList();
  // 列表
  renderItems();
}

function switchDays(days, btn) {
  currentDays = days;
  var tabs = document.querySelectorAll(".tab-btn");
  for (var i = 0; i < tabs.length; i++) tabs[i].classList.remove("active");
  btn.classList.add("active");
  currentCategory = "all";
  var chips = document.querySelectorAll(".chip");
  for (var i = 0; i < chips.length; i++) chips[i].classList.remove("active");
  document.querySelector(".chip[data-cat='all']").classList.add("active");
  renderItems();
  updateDashboard();
}

function filterCategory(cat, chip) {
  currentCategory = cat;
  var chips = document.querySelectorAll(".chip");
  for (var i = 0; i < chips.length; i++) chips[i].classList.remove("active");
  chip.classList.add("active");
  renderItems();
}

function getFilteredItems() {
  var now = new Date();
  var items = [];
  for (var j = 0; j < ALL_ITEMS.length; j++) {
    var item = ALL_ITEMS[j];
    // 时间过滤
    if (currentDays > 0) {
      var cutoff = new Date(now);
      cutoff.setDate(cutoff.getDate() - currentDays);
      var cutoffStr = cutoff.toISOString().substring(0,10);
      if ((item.pub_date || "") < cutoffStr) continue;
    }
    // 分类过滤
    if (currentCategory !== "all" && (item.category || "\u5176\u4ed6") !== currentCategory) continue;
    // 搜索过滤
    var q = document.getElementById("searchInput").value.trim().toLowerCase();
    if (q) {
      var hay = ((item.title||"") + (item.buyer||"") + (item.source||"") + (item.region||"")).toLowerCase();
      if (hay.indexOf(q) < 0) continue;
    }
    items.push(item);
  }
  return items;
}

function renderItems() {
  var items = getFilteredItems();
  renderStats(items);
  renderContent(items);
}

function renderStats(items) {
  var catCounts = {};
  for (var i = 0; i < items.length; i++) {
    var c = items[i].category || "\u5176\u4ed6";
    catCounts[c] = (catCounts[c] || 0) + 1;
  }
  var row = document.getElementById("statsRow");
  var html = '<div class="stat-card"><div class="num">' + items.length + '</div><div class="label">\u603b\u6761\u6570</div></div>';
  for (var i = 0; i < CATEGORY_PRIORITY.length; i++) {
    var cat = CATEGORY_PRIORITY[i];
    if (catCounts[cat]) {
      html += '<div class="stat-card"><div class="num" style="color:' + (CATEGORY_COLORS[cat]||'#999') + '">' + catCounts[cat] + '</div><div class="label">' + cat + '</div></div>';
    }
  }
  row.innerHTML = html;
  document.getElementById("totalCount").textContent = "\u5f53\u524d\u663e\u793a " + items.length + " \u6761";
}

function renderContent(items) {
  var area = document.getElementById("contentArea");
  if (!items.length) {
    area.innerHTML = '<div class="empty"><div class="empty-icon">\\1F4ED</div><p>\u6682\u65e0\u7b26\u5408\u6761\u4ef6\u7684\u62db\u6295\u6807\u4fe1\u606f</p></div>';
    return;
  }
  var groups = {};
  for (var i = 0; i < items.length; i++) {
    var cat = items[i].category || "\u5176\u4ed6";
    if (!groups[cat]) groups[cat] = [];
    groups[cat].push(items[i]);
  }
  var html = "";
  for (var ci = 0; ci < CATEGORY_PRIORITY.length; ci++) {
    var cat = CATEGORY_PRIORITY[ci];
    var catItems = groups[cat];
    if (!catItems) continue;
    var color = CATEGORY_COLORS[cat] || "#999";
    html += '<div class="cat-group">';
    html += '<div class="cat-header" style="background:' + color + '" onclick="toggleGroup(this)">';
    html += '<span>' + cat + '</span><span class="count">' + catItems.length + '\u6761</span>';
    html += '<span class="toggle">\u25BC</span></div>';
    html += '<div class="cat-body">';
    for (var idx = 0; idx < catItems.length; idx++) {
      var item = catItems[idx];
      var url = (item.url || "").split(" | ")[0] || "#";
      var source = (item.source || "").split(" | ")[0];
      var status = item.project_status || "";
      html += '<div class="item" onclick="toggleDetail(this)" data-id="' + escHtml(item.id) + '">';
      html += '<div class="item-title"><a href="' + escHtml(url) + '" target="_blank" onclick="event.stopPropagation()">' + (idx+1) + '. ' + escHtml(item.title || "\u65e0\u6807\u9898") + '</a></div>';
      html += '<div class="item-meta">';
      if (item.is_new) html += '<span class="tag tag-new">NEW</span>';
      if (source) html += '<span class="tag tag-source">' + escHtml(source) + '</span>';
      if (item.pub_date) html += '<span class="tag tag-date">' + escHtml(item.pub_date) + '</span>';
      if (item.region) html += '<span class="tag tag-region">' + escHtml(item.region) + '</span>';
      if (status) html += '<span class="tag tag-status" style="background:' + color + '">' + escHtml(status) + '</span>';
      var expiryTag = getExpiryTag(item);
      if (expiryTag) html += expiryTag;
      html += '</div>';
      html += '<div class="item-detail">';
      if (item.notice_type) html += '<div class="detail-row"><span class="key">\u516c\u544a\u7c7b\u578b</span><span class="val">' + escHtml(item.notice_type) + '</span></div>';
      if (item.buyer) html += '<div class="detail-row"><span class="key">\u91c7\u8d2d\u4eba</span><span class="val">' + escHtml(item.buyer) + '</span></div>';
      if (item.project_no) html += '<div class="detail-row"><span class="key">\u9879\u76ee\u7f16\u53f7</span><span class="val">' + escHtml(item.project_no) + '</span></div>';
      if (item.budget) html += '<div class="detail-row"><span class="key">\u9884\u7b97\u91d1\u989d</span><span class="val">' + escHtml(item.budget) + '</span></div>';
      if (item.source) html += '<div class="detail-row"><span class="key">\u4fe1\u606f\u6765\u6e90</span><span class="val">' + escHtml(item.source) + '</span></div>';
      if (item.first_seen) html += '<div class="detail-row"><span class="key">\u5165\u5e93\u65f6\u95f4</span><span class="val">' + escHtml(item.first_seen) + '</span></div>';
      html += '<div class="detail-row"><span class="key">\u539f\u6587\u94fe\u63a5</span><span class="val"><a href="' + escHtml(url) + '" target="_blank">\u70b9\u51fb\u67e5\u770b\u539f\u6587</a></span></div>';
      html += '</div></div>';
    }
    html += '</div></div>';
  }
  area.innerHTML = html;
}

// ============ 趋势图 ============
function drawTrend() {
  var canvas = document.getElementById("trendCanvas");
  if (!canvas) return;
  var ctx = canvas.getContext("2d");
  var W = canvas.offsetWidth;
  var H = canvas.offsetHeight;
  canvas.width = W * 2;
  canvas.height = H * 2;
  ctx.scale(2, 2);

  var data = TREND_VALUES;
  var labels = TREND_LABELS;
  var maxV = Math.max.apply(null, data) || 1;
  var padL = 30, padR = 10, padT = 10, padB = 20;
  var plotW = W - padL - padR;
  var plotH = H - padT - padB;
  var step = plotW / (data.length - 1 || 1);

  // 背景网格
  ctx.strokeStyle = "#f0f0f0";
  ctx.lineWidth = 0.5;
  for (var i = 0; i <= 4; i++) {
    var y = padT + plotH * (1 - i/4);
    ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(W - padR, y); ctx.stroke();
    ctx.fillStyle = "#999"; ctx.font = "10px sans-serif"; ctx.textAlign = "right";
    ctx.fillText(Math.round(maxV * i / 4), padL - 4, y + 3);
  }

  // 折线
  ctx.strokeStyle = "#1B3A5C"; ctx.lineWidth = 2; ctx.lineJoin = "round";
  ctx.beginPath();
  for (var i = 0; i < data.length; i++) {
    var x = padL + i * step;
    var y = padT + plotH * (1 - data[i] / maxV);
    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  }
  ctx.stroke();

  // 面积填充
  ctx.lineTo(padL + (data.length - 1) * step, padT + plotH);
  ctx.lineTo(padL, padT + plotH);
  ctx.closePath();
  ctx.fillStyle = "rgba(27,58,92,0.08)";
  ctx.fill();

  // X轴日期（只标首/中/尾）
  ctx.fillStyle = "#999"; ctx.font = "9px sans-serif"; ctx.textAlign = "center";
  if (labels.length > 0) {
    ctx.fillText(labels[0], padL, H - 4);
    var mid = Math.floor(labels.length / 2);
    ctx.fillText(labels[mid], padL + mid * step, H - 4);
    ctx.fillText(labels[labels.length - 1], padL + (labels.length - 1) * step, H - 4);
  }
}

// ============ 来源分布 ============
function renderSourceList() {
  var list = document.getElementById("sourceList");
  var html = "";
  var maxVal = SOURCE_PIE.length > 0 ? SOURCE_PIE[0].value : 1;
  for (var i = 0; i < SOURCE_PIE.length; i++) {
    var s = SOURCE_PIE[i];
    var pct = maxVal > 0 ? (s.value / maxVal * 100) : 0;
    var color = SOURCE_COLORS[i % SOURCE_COLORS.length];
    html += '<li><span>' + escHtml(s.name) + ' <span style="color:#999">(' + s.value + ')</span></span>';
    html += '<span><span class="source-bar" style="width:' + Math.max(pct, 2) + '%;background:' + color + '"></span></span></li>';
  }
  if (!html) html = '<li style="color:#999;text-align:center">\u6682\u65e0\u6570\u636e</li>';
  list.innerHTML = html;
}

function updateDashboard() {
  // 时间范围切换时，仪表盘数据不重新加载（数据在 HTML 生成时已固化）
  // 如需动态刷新，需要后端 API 支持
}

function escHtml(s) {
  if (!s) return "";
  return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;").replace(/'/g,"&#39;");
}

function toggleGroup(header) {
  header.classList.toggle("collapsed");
  header.nextElementSibling.classList.toggle("collapsed");
}

function toggleDetail(item) {
  var detail = item.querySelector(".item-detail");
  if (detail) detail.classList.toggle("open");
}

// ============ 导出CSV ============
function exportCSV() {
  var items = getFilteredItems();
  if (!items.length) { alert("\u5f53\u524d\u6ca1\u6709\u53ef\u5bfc\u51fa\u7684\u6570\u636e"); return; }
  var headers = ["\u5e8f\u53f7","\u6807\u9898","\u5206\u7c7b","\u516c\u544a\u7c7b\u578b","\u91c7\u8d2d\u4eba","\u9879\u76ee\u7f16\u53f7","\u9884\u7b97\u91d1\u989d","\u533a\u57df","\u53d1\u5e03\u65e5\u671f","\u4fe1\u606f\u6765\u6e90","\u9879\u76ee\u72b6\u6001","\u539f\u6587\u94fe\u63a5","\u5165\u5e93\u65f6\u95f4"];
  var rows = [headers.join(",")];
  for (var i = 0; i < items.length; i++) {
    var it = items[i];
    var url = (it.url || "").split(" | ")[0] || "";
    var cells = [
      i+1,
      it.title||"",
      it.category||"",
      it.notice_type||"",
      it.buyer||"",
      it.project_no||"",
      it.budget||"",
      it.region||"",
      it.pub_date||"",
      it.source||"",
      it.project_status||"",
      url,
      it.first_seen||""
    ];
    for (var j = 0; j < cells.length; j++) {
      var v = String(cells[j]||"");
      v = v.replace(/"/g,'""');
      cells[j] = '"' + v + '"';
    }
    rows.push(cells.join(","));
  }
  // BOM + CSV
  var csv = "\ufeff" + rows.join("\\n");
  var blob = new Blob([csv], {type:"text/csv;charset=utf-8"});
  var link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  var dt = new Date();
  var stamp = dt.getFullYear() + (dt.getMonth()+1<10?"0":"") + (dt.getMonth()+1) + (dt.getDate()<10?"0":"") + dt.getDate();
  link.download = "\u4e34\u6cc9\u62db\u6295\u6807\u4fe1\u606f_" + stamp + ".csv";
  link.click();
}

// ============ 启动 ============
init();
</script>
</body>
</html>'''

    # 替换占位符
    html = html.replace("__REGION_NAME__", region_name)
    html = html.replace("__TODAY__", _today)
    html = html.replace("__YESTERDAY__", _yesterday)
    html = html.replace("__CRAWL_TIME__", _crawl_time)
    html = html.replace("__DATA_JSON__", data_json)
    html = html.replace("__STATS_ALL_JSON__", stats_all_json)
    html = html.replace("__STATS_7D_JSON__", stats_7d_json)
    html = html.replace("__STATS_30D_JSON__", stats_30d_json)
    html = html.replace("__SITE_STATS_JSON__", site_stats_json)
    html = html.replace("__TREND_LABELS_JSON__", trend_labels_json)
    html = html.replace("__TREND_VALUES_JSON__", trend_values_json)
    html = html.replace("__SOURCE_PIE_JSON__", source_pie_json)
    html = html.replace("__HEALTH_CLASS__", health_class)
    html = html.replace("__HEALTH_TEXT__", health_text)

    return html


def generate(output_path=None):
    """主入口：爬取数据、入库、生成 HTML 页面"""
    if not output_path:
        output_path = OUTPUT_HTML

    logger.info("=" * 60)
    logger.info(f"招投标信息网页生成开始 {datetime.now():%Y-%m-%d %H:%M:%S}")
    logger.info("=" * 60)

    try:
        # 爬取 + 入库
        _, site_stats, new_count = crawl_and_save()

        # 从数据库生成页面
        region_name = DEFAULT_REGION_NAME
        html = build_web_html(region_name)

        with open(output_path, "w", encoding="utf-8") as f:
            f.write(html)

        all_items = query_items(days=None)
        logger.info(f"HTML 页面已生成: {output_path}")
        logger.info(f"数据库共 {len(all_items)} 条信息，本次新增 {new_count} 条")
        print(f"\n{'='*60}")
        print(f"  网页生成完成！数据库共 {len(all_items)} 条信息，新增 {new_count} 条")
        print(f"  输出文件: {output_path}")
        print(f"{'='*60}")

        return output_path

    except Exception as e:
        logger.error(f"网页生成出错: {e}", exc_info=True)
        print(f"\n错误: {e}")
        return None


# ==================== Windows 计划任务管理 ====================
TASK_WEB_MORNING = "BidWeb_Linquan_MorningUpdate"
TASK_WEB_NOON = "BidWeb_Linquan_NoonUpdate"
DESC_WEB_MORNING = "临泉招标网页-早间自动更新（8:05）"
DESC_WEB_NOON = "临泉招标网页-午间自动更新（14:35）"


def setup_web_scheduler():
    """注册网页自动更新的 Windows 计划任务"""
    import subprocess

    python_exe = sys.executable
    web_script = os.path.join(SCRIPT_DIR, "bid_web_page.py")

    print("=" * 60)
    print("  临泉招标网页 — 注册 Windows 计划任务")
    print("=" * 60)

    # 清理旧任务
    for name in [TASK_WEB_MORNING, TASK_WEB_NOON]:
        result = subprocess.run(
            ["schtasks.exe", "/query", "/tn", name],
            capture_output=True, text=True, encoding="utf-8", errors="replace"
        )
        if result.returncode == 0:
            print(f"  清理旧任务: {name}")
            subprocess.run(["schtasks.exe", "/delete", "/tn", name, "/f"],
                           capture_output=True, text=True)

    # 任务一：早间更新 8:05
    xml_morning = f'''<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>{DESC_WEB_MORNING}</Description>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>2026-01-01T08:05:00</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>
    </CalendarTrigger>
  </Triggers>
  <Actions>
    <Exec>
      <Command>{python_exe}</Command>
      <Arguments>"{web_script}" --deploy</Arguments>
      <WorkingDirectory>{SCRIPT_DIR}</WorkingDirectory>
    </Exec>
  </Actions>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>true</RunOnlyIfNetworkAvailable>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <ExecutionTimeLimit>PT30M</ExecutionTimeLimit>
  </Settings>
</Task>'''

    xml_path = os.path.join(SCRIPT_DIR, "_task_web_morning.xml")
    with open(xml_path, "w", encoding="utf-16") as f:
        f.write(xml_morning)
    try:
        subprocess.run(["schtasks.exe", "/create", "/tn", TASK_WEB_MORNING, "/xml", xml_path, "/f"],
                       capture_output=True, text=True)
        print(f"  注册完成: {DESC_WEB_MORNING}")
    finally:
        if os.path.exists(xml_path):
            os.remove(xml_path)

    # 任务二：午间更新 14:35
    xml_noon = f'''<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>{DESC_WEB_NOON}</Description>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>2026-01-01T14:35:00</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>
    </CalendarTrigger>
  </Triggers>
  <Actions>
    <Exec>
      <Command>{python_exe}</Command>
      <Arguments>"{web_script}" --deploy</Arguments>
      <WorkingDirectory>{SCRIPT_DIR}</WorkingDirectory>
    </Exec>
  </Actions>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>true</RunOnlyIfNetworkAvailable>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <ExecutionTimeLimit>PT30M</ExecutionTimeLimit>
  </Settings>
</Task>'''

    xml_path2 = os.path.join(SCRIPT_DIR, "_task_web_noon.xml")
    with open(xml_path2, "w", encoding="utf-16") as f:
        f.write(xml_noon)
    try:
        subprocess.run(["schtasks.exe", "/create", "/tn", TASK_WEB_NOON, "/xml", xml_path2, "/f"],
                       capture_output=True, text=True)
        print(f"  注册完成: {DESC_WEB_NOON}")
    finally:
        if os.path.exists(xml_path2):
            os.remove(xml_path2)

    print("\n" + "=" * 60)
    print("  注册完成！网页每天自动更新2次：")
    print("  - 早间 8:05 自动更新 + Netlify 部署")
    print("  - 午间 14:35 自动更新 + Netlify 部署")
    print("  - SQLite 持久化，数据越积越多")
    print("=" * 60)


def show_web_scheduler_status():
    """查看网页计划任务状态"""
    import subprocess
    print("=" * 60)
    print("  临泉招标网页 — Windows 计划任务状态")
    print("=" * 60)
    for name, desc in [(TASK_WEB_MORNING, "早间更新"), (TASK_WEB_NOON, "午间更新")]:
        result = subprocess.run(
            ["schtasks.exe", "/query", "/tn", name, "/v", "/fo", "list"],
            capture_output=True, text=True, encoding="utf-8", errors="replace"
        )
        if result.returncode == 0:
            print(f"\n  [{desc}] {name}")
            for line in result.stdout.split("\n"):
                line = line.strip()
                if any(k in line for k in ["状态", "Status", "下次运行", "Next Run", "上次运行", "Last Run"]):
                    print(f"    {line}")
        else:
            print(f"\n  [{desc}] {name} — 未注册")


def remove_web_scheduler():
    """卸载网页计划任务"""
    import subprocess
    print("=" * 60)
    print("  临泉招标网页 — 卸载 Windows 计划任务")
    print("=" * 60)
    for name, desc in [(TASK_WEB_MORNING, "早间更新"), (TASK_WEB_NOON, "午间更新")]:
        result = subprocess.run(
            ["schtasks.exe", "/query", "/tn", name],
            capture_output=True, text=True, encoding="utf-8", errors="replace"
        )
        if result.returncode == 0:
            subprocess.run(["schtasks.exe", "/delete", "/tn", name, "/f"],
                           capture_output=True, text=True)
            print(f"  已卸载: {desc} ({name})")
        else:
            print(f"  跳过: {desc} ({name}) — 不存在")
    print("\n  卸载完成！")


# ==================== Netlify 部署 ====================
NETLIFY_CONFIG_FILE = os.path.join(SCRIPT_DIR, "netlify_config.json")
NETLIFY_API_BASE = "https://api.netlify.com/api/v1"

_NL_SSL_CTX = ssl.create_default_context()


def _netlify_request(url, token, method="GET", data=None, content_type="application/json"):
    """发 Netlify API 请求，返回 JSON"""
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": content_type,
    }
    if data and isinstance(data, str):
        data = data.encode("utf-8")
    elif data is None:
        data = None
    req = urllib.request.Request(url, headers=headers, data=data, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60, context=_NL_SSL_CTX) as r:
            body = r.read().decode("utf-8")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        logger.error(f"Netlify API 错误 {e.code}: {err_body[:500]}")
        raise RuntimeError(f"Netlify API 返回 {e.code}: {err_body[:300]}")
    except Exception as e:
        logger.error(f"Netlify API 请求失败: {e}")
        raise


def load_netlify_config():
    """加载 Netlify 配置"""
    if os.path.exists(NETLIFY_CONFIG_FILE):
        try:
            with open(NETLIFY_CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return None


def save_netlify_config(cfg):
    """保存 Netlify 配置"""
    with open(NETLIFY_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    logger.info(f"Netlify 配置已保存到 {NETLIFY_CONFIG_FILE}")


def deploy_to_netlify(html_path=None, token=None):
    """将 HTML 部署到 Netlify，返回公网链接

    Token 优先级：参数传入 > 环境变量 NETLIFY_TOKEN > 配置文件 > 交互式输入
    """
    if not html_path:
        html_path = OUTPUT_HTML

    if not os.path.exists(html_path):
        logger.error(f"HTML 文件不存在: {html_path}")
        print(f"错误: HTML 文件不存在: {html_path}")
        print("请先运行 python bid_web_page.py 生成页面")
        return None

    cfg = load_netlify_config()

    # Token 优先级：参数 > 环境变量 > 配置文件 > 交互式输入
    env_token = os.environ.get("NETLIFY_TOKEN", "").strip() or None
    param_token = token

    if cfg and cfg.get("token") and cfg.get("site_id"):
        if param_token or env_token:
            token = param_token or env_token
        else:
            token = cfg["token"]
        site_id = cfg["site_id"]
        site_url = cfg.get("site_url", "")
        logger.info(f"使用已配置的 Netlify 站点: {site_url or site_id}")
    else:
        if not token:
            token = env_token
        if not token:
            print("\n" + "=" * 60)
            print("  Netlify 部署配置（首次使用）")
            print("=" * 60)
            print("  1. 打开 https://app.netlify.com/user/applications/personal-access-tokens")
            print("  2. 点击 'New access token'，名称填 'bid-web-page'")
            print("  3. 复制生成的 Token")
            print()
            try:
                token = input("  请输入 Netlify Personal Access Token: ").strip()
            except EOFError:
                print("  无法读取输入（非终端环境），请通过 --token 参数或 NETLIFY_TOKEN 环境变量提供 Token")
                return None
        if not token:
            print("  已取消")
            return None

        # 验证 token
        print("  正在验证 Token...")
        try:
            user = _netlify_request(f"{NETLIFY_API_BASE}/user", token)
            print(f"  验证成功！账户: {user.get('full_name', '')} ({user.get('email', '')})")
        except Exception as e:
            print(f"  Token 验证失败: {e}")
            return None

        # 创建站点
        print("  正在创建 Netlify 站点...")
        try:
            site_data = _netlify_request(
                f"{NETLIFY_API_BASE}/sites", token, method="POST",
                data=json.dumps({"name": f"linquan-bid-{datetime.now().strftime('%Y%m%d')}",
                                  "custom_domain": ""})
            )
            site_id = site_data["id"]
            site_url = site_data.get("ssl_url") or site_data.get("url", "")
            site_name = site_data.get("name", "")
            print(f"  站点创建成功！")
            print(f"  站点ID: {site_id}")
            print(f"  站点名: {site_name}")
            print(f"  URL: {site_url}")
        except Exception as e:
            print(f"  创建站点失败: {e}")
            return None

        # 保存配置
        save_netlify_config({"token": token, "site_id": site_id,
                              "site_url": site_url, "site_name": site_name})
        cfg = load_netlify_config()

    # 使用 file digest 方式部署（主方案）
    print("  正在部署到 Netlify...")
    logger.info(f"开始 Netlify 部署，站点: {site_id}")

    result_url = _deploy_via_file_digest(html_path, token, site_id, cfg)

    # 如果 file digest 失败，回退到 zip 直传
    if not result_url:
        logger.warning("file digest 部署失败，尝试 zip 直传方式...")
        print("  尝试备用部署方式（zip 直传）...")
        result_url = _deploy_via_zip(html_path, token, site_id, cfg)

    if not result_url:
        return None

    final_url = result_url

    print("\n" + "=" * 60)
    print(f"  公网链接: {final_url}")
    print("=" * 60)
    logger.info(f"Netlify 部署完成，公网链接: {final_url}")

    # 如果参数/环境变量 token 覆盖了配置文件中的旧 token，回写
    if cfg and (param_token or env_token) and cfg.get("token") != token:
        cfg["token"] = token
        save_netlify_config(cfg)
        logger.info("已将新 Token 更新到配置文件")

    return final_url


def _deploy_via_file_digest(html_path, token, site_id, cfg):
    """主部署方式：使用 file digest + SHA1 上传单个文件"""
    with open(html_path, "rb") as f:
        file_content = f.read()

    sha1 = hashlib.sha1(file_content).hexdigest()
    logger.info(f"文件 SHA1: {sha1}, 大小: {len(file_content)} 字节")

    # 步骤1：创建 deploy，声明文件
    deploy_body = json.dumps({
        "files": {"index.html": sha1},
        "draft": False,
    })
    try:
        deploy_result = _netlify_request(
            f"{NETLIFY_API_BASE}/sites/{site_id}/deploys", token,
            method="POST", data=deploy_body
        )
    except Exception as e:
        logger.error(f"创建部署失败: {e}")
        print(f"  部署创建失败: {e}")
        return None

    deploy_id = deploy_result.get("id", "")
    if not deploy_id:
        logger.error(f"创建部署返回无 id: {deploy_result}")
        print("  部署创建失败：未获取到部署ID")
        return None
    required = deploy_result.get("required", [])
    logger.info(f"部署创建成功, deploy_id={deploy_id}, required={required}")

    # 步骤2：如果 Netlify 还需要上传文件
    if sha1 in required:
        logger.info("上传文件内容...")
        upload_url = f"{NETLIFY_API_BASE}/deploys/{deploy_id}/files/index.html"
        upload_headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/octet-stream",
        }
        req = urllib.request.Request(upload_url, headers=upload_headers,
                                      data=file_content, method="PUT")
        try:
            with urllib.request.urlopen(req, timeout=120, context=_NL_SSL_CTX):
                logger.info("文件上传成功")
        except urllib.error.HTTPError as e:
            err = e.read().decode("utf-8", errors="replace")
            logger.error(f"文件上传失败 {e.code}: {err[:300]}")
            print(f"  文件上传失败: {e.code}")
            return None
    else:
        logger.info("文件已在 Netlify 缓存中，跳过上传")

    # 步骤3：等待部署完成
    print("  等待部署生效...")
    final_url = cfg.get("site_url", "") if cfg else ""
    for i in range(12):
        time.sleep(5)
        try:
            status = _netlify_request(
                f"{NETLIFY_API_BASE}/sites/{site_id}/deploys/{deploy_id}", token
            )
            state = status.get("state", "")
            logger.info(f"  部署状态: {state}")
            if state == "ready":
                print("  部署完成！")
                break
            elif state in ("error", "rejected"):
                print(f"  部署失败: {state}")
                return None
        except Exception:
            pass
    else:
        print("  部署已提交（可能仍在处理中，链接稍后生效）")

    return final_url


def _deploy_via_zip(html_path, token, site_id, cfg):
    """备用部署方式：打包为 zip 直接上传"""
    logger.info("正在打包 HTML 文件...")
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(html_path, "index.html")
    zip_data = zip_buffer.getvalue()
    logger.info(f"ZIP 打包完成: {len(zip_data)} 字节")

    if len(zip_data) > 10 * 1024 * 1024:
        logger.error(f"部署包过大: {len(zip_data)} 字节 (限制 10MB)")
        print(f"  错误: 部署包 {len(zip_data)/1024/1024:.1f}MB 超过 Netlify 10MB 限制")
        return None

    deploy_url = f"{NETLIFY_API_BASE}/sites/{site_id}/deploys"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/zip",
    }
    req = urllib.request.Request(deploy_url, headers=headers, data=zip_data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120, context=_NL_SSL_CTX) as r:
            deploy_result = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        logger.error(f"zip 部署失败 {e.code}: {err_body[:500]}")
        print(f"  zip 部署失败: {e.code}")
        return None
    except Exception as e:
        logger.error(f"zip 部署请求失败: {e}")
        print(f"  zip 部署请求失败: {e}")
        return None

    deploy_id = deploy_result.get("id", "")
    final_url = cfg.get("site_url", "") if cfg else ""

    logger.info(f"zip 部署已提交, deploy_id={deploy_id}")

    print("  等待部署生效...")
    for i in range(12):
        time.sleep(5)
        try:
            status = _netlify_request(
                f"{NETLIFY_API_BASE}/sites/{site_id}/deploys/{deploy_id}", token
            )
            state = status.get("state", "")
            logger.info(f"  部署状态: {state}")
            if state == "ready":
                print("  部署完成！")
                break
            elif state in ("error", "rejected"):
                print(f"  部署失败: {state}")
                return None
        except Exception:
            pass
    else:
        print("  部署已提交（可能仍在处理中，链接稍后生效）")

    return final_url


# ==================== CLI 入口 ====================
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="招投标信息自动网页 — 临泉特供版")
    parser.add_argument("--output", default="", help="输出HTML文件路径（默认桌面）")
    parser.add_argument("--deploy", action="store_true", help="生成HTML后部署到Netlify公网")
    parser.add_argument("--deploy-only", action="store_true", help="仅部署已有HTML到Netlify（不重新爬取）")
    parser.add_argument("--token", default="", help="Netlify Personal Access Token（首次部署时使用）")
    parser.add_argument("--setup-scheduler", action="store_true", help="注册/更新 Windows 计划任务")
    parser.add_argument("--scheduler-status", action="store_true", help="查看计划任务状态")
    parser.add_argument("--remove-scheduler", action="store_true", help="卸载计划任务")
    args = parser.parse_args()

    if args.setup_scheduler:
        setup_web_scheduler()
    elif args.scheduler_status:
        show_web_scheduler_status()
    elif args.remove_scheduler:
        remove_web_scheduler()
    elif args.deploy_only:
        if not load_netlify_config() and not args.token:
            print("错误: 尚未配置 Netlify，请先运行:")
            print("  python bid_web_page.py --deploy --token YOUR_TOKEN")
            sys.exit(1)
        deploy_to_netlify(args.output if args.output else None, token=args.token or None)
    else:
        output = args.output if args.output else None
        html_path = generate(output_path=output)
        if html_path and args.deploy:
            if not load_netlify_config() and not args.token:
                logger.warning("Netlify 未配置，跳过部署。请先手动运行 --deploy --token 完成首次配置")
                print("\n提示: Netlify 未配置，已生成本地 HTML 但未部署。")
                print("首次部署请运行: python bid_web_page.py --deploy --token YOUR_TOKEN")
            else:
                print("\n正在部署到 Netlify...")
                deploy_to_netlify(html_path, token=args.token or None)
