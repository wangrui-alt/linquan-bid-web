# -*- coding: utf-8 -*-
"""
云端网页部署入口 — GitHub Actions 使用
职责：爬取 → 生成交互式网页 HTML → 生成 RSS → 部署到 Netlify
与 cloud_email.py 各司其职，通过 bid_history.json 共享去重状态。

环境变量（通过 GitHub Secrets 注入）：
  NETLIFY_TOKEN    - Netlify Personal Access Token
  NETLIFY_SITE_ID  - Netlify 站点 ID
  REGION_NAME      - 地区名称（默认 临泉县）
  REGION_KEYWORDS  - 地区关键词，逗号分隔（默认 临泉,236400）
"""

import json
import os
import sys
import hashlib
import urllib.request
import urllib.error
import ssl
import time
from datetime import datetime, timedelta

# 复用爬虫引擎
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from crawler_engine import (
    crawl, classify_and_sort, smart_dedupe,
    today_str,
    CATEGORY_LABELS, CATEGORY_COLORS,
    DEFAULT_REGION_KW, DEFAULT_REGION_NAME,
    normalize_title_for_dedupe,
)

# ==================== 日志 ====================
import logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ==================== 配置读取 ====================
def get_env(key, default=""):
    return os.environ.get(key, default).strip()

REGION_NAME = get_env("REGION_NAME") or DEFAULT_REGION_NAME
REGION_KEYWORDS = [kw.strip() for kw in get_env("REGION_KEYWORDS", "临泉,236400").split(",") if kw.strip()] or DEFAULT_REGION_KW
NETLIFY_TOKEN = get_env("NETLIFY_TOKEN")
NETLIFY_SITE_ID = get_env("NETLIFY_SITE_ID")

# ==================== 历史去重（与 cloud_email.py 共享同一文件）====================
HISTORY_FILE = os.path.join(SCRIPT_DIR, "data", "bid_history.json")


def load_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"sent_ids": [], "last_update": ""}


def dedup_with_history(items, history):
    """历史去重，标记新增条目（只读，不更新 history——由 cloud_email.py 负责）"""
    sent_ids = set(history.get("sent_ids", []))
    new_count = 0
    for item in items:
        dedup_id = _item_dedup_id(item)
        item["_dedup_id"] = dedup_id
        if dedup_id in sent_ids:
            item["_is_new"] = False
        else:
            item["_is_new"] = True
            new_count += 1
    return items, new_count


def _item_dedup_id(item):
    norm_name = normalize_title_for_dedupe(item.get("title", ""))
    notice_type = (item.get("notice_type") or "").strip()
    key = f"{hashlib.md5(norm_name.encode()).hexdigest()}:{notice_type}"
    return hashlib.sha1(key.encode()).hexdigest()


# ==================== 网页 HTML 生成 ====================
def build_cloud_html(all_items, site_stats, new_count):
    """从爬取数据直接生成自包含 HTML 页面（无需 SQLite）"""
    _crawl_time = datetime.now().strftime("%Y-%m-%d %H:%M")

    # 统计
    stats_by_cat = {}
    stats_by_date = {}
    stats_by_source = {}
    for item in all_items:
        cat = item.get("category", "其他")
        stats_by_cat[cat] = stats_by_cat.get(cat, 0) + 1
        d = item.get("pub_date", "未知")
        stats_by_date[d] = stats_by_date.get(d, 0) + 1
        for src in (item.get("source") or "").split(" | "):
            s = src.strip()
            if s:
                stats_by_source[s] = stats_by_source.get(s, 0) + 1

    total = len(all_items)

    # 爬取健康状态
    fail_count = sum(1 for v in site_stats.values() if isinstance(v, int) and v < 0)
    total_sites = len(site_stats)
    if fail_count == 0 and total_sites > 0:
        health_class = "refresh-info ok"
        health_text = "全部数据源正常"
    elif fail_count == total_sites:
        health_class = "refresh-info error"
        health_text = "所有数据源爬取失败"
    elif total_sites > 0:
        health_class = "refresh-info warn"
        health_text = f"{fail_count}/{total_sites} 个数据源爬取失败"
    else:
        health_class = "refresh-info ok"
        health_text = "尚未爬取数据"

    # 序列化数据
    for i, item in enumerate(all_items):
        if "id" not in item:
            item["id"] = item.get("_dedup_id") or hashlib.sha1(item.get("title", "").encode()).hexdigest()

    data_json = json.dumps(all_items, ensure_ascii=False).replace("</", "<\\/")
    stats_json = json.dumps({"total": total, "by_category": stats_by_cat, "new_count": new_count}, ensure_ascii=False).replace("</", "<\\/")
    site_stats_json = json.dumps(site_stats, ensure_ascii=False).replace("</", "<\\/")

    # 趋势数据（最近30天）
    trend_dates = []
    trend_counts = []
    for d in range(29, -1, -1):
        dt = (datetime.now() - timedelta(days=d)).strftime("%Y-%m-%d")
        trend_dates.append(dt[5:])
        trend_counts.append(stats_by_date.get(dt, 0))
    trend_labels_json = json.dumps(trend_dates, ensure_ascii=False)
    trend_values_json = json.dumps(trend_counts, ensure_ascii=False)

    # 来源占比
    source_pie_data = []
    for src, cnt in sorted(stats_by_source.items(), key=lambda x: -x[1]):
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
.footer { text-align:center; padding:24px; font-size:12px; color:var(--text3); }
.refresh-bar { max-width:960px; margin:0 auto; padding:0 16px; }
.refresh-info { background:linear-gradient(90deg,#E8F5E9,#fff); border:1px solid #C8E6C9; border-radius:var(--radius); padding:8px 16px; font-size:12px; color:#2E7D32; display:flex; justify-content:space-between; align-items:center; margin-bottom:16px; }
.refresh-info .dot { display:inline-block; width:8px; height:8px; background:#4CAF50; border-radius:50%; margin-right:6px; animation:blink 2s infinite; }
.refresh-info.warn { background:linear-gradient(90deg,#FFF3E0,#fff); border-color:#FFE0B2; color:#E65100; }
.refresh-info.warn .dot { background:#FF9800; }
.refresh-info.error { background:linear-gradient(90deg,#FFEBEE,#fff); border-color:#FFCDD2; color:#C62828; }
.refresh-info.error .dot { background:#F44336; }
@keyframes blink { 0%,100%{opacity:1} 50%{opacity:.3} }
.tag-expiring { background:#FFF9C4; color:#F57F17; font-weight:600; }
.tag-expired { background:#FFCDD2; color:#C62828; font-weight:600; }
.export-btn { padding:8px 16px; border:none; background:var(--primary); color:#fff; border-radius:6px; cursor:pointer; font-size:13px; font-weight:500; }
.export-btn:hover { background:var(--primary-light); }
@media (max-width:640px) {
  .controls-inner { flex-direction:column; }
  .stats-row { gap:8px; }
  .stat-card { min-width:60px; padding:12px 8px; }
  .stat-card .num { font-size:22px; }
  .dashboard { grid-template-columns:1fr; }
}
</style>
</head>
<body>

<div class="header">
  <h1>__REGION_NAME__招投标信息日报</h1>
  <div class="meta">
    <span>更新于 __CRAWL_TIME__</span>
    <span>7个数据源自动采集</span>
    <span>GitHub Actions 云端运行</span>
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
  临泉特供版招投标信息监控 | 7个数据源自动采集 | GitHub Actions 云端运行 | 星辰超级智能体生成
</div>

<script>
var ALL_ITEMS = __DATA_JSON__;
var STATS = __STATS_JSON__;
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

function init() {
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
  drawTrend();
  renderSourceList();
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
    if (currentDays > 0) {
      var cutoff = new Date(now);
      cutoff.setDate(cutoff.getDate() - currentDays);
      var cutoffStr = cutoff.toISOString().substring(0,10);
      if ((item.pub_date || "") < cutoffStr) continue;
    }
    if (currentCategory !== "all" && (item.category || "\u5176\u4ed6") !== currentCategory) continue;
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
    area.innerHTML = '<div class="empty"><p>\u6682\u65e0\u7b26\u5408\u6761\u4ef6\u7684\u62db\u6295\u6807\u4fe1\u606f</p></div>';
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
    html += '<span class="toggle">\u25bc</span></div>';
    html += '<div class="cat-body">';
    for (var idx = 0; idx < catItems.length; idx++) {
      var item = catItems[idx];
      var url = (item.url || "").split(" | ")[0] || "#";
      var source = (item.source || "").split(" | ")[0];
      var status = item.project_status || "";
      html += '<div class="item" onclick="toggleDetail(this)" data-id="' + escHtml(item.id) + '">';
      html += '<div class="item-title"><a href="' + escHtml(url) + '" target="_blank" onclick="event.stopPropagation()">' + (idx+1) + '. ' + escHtml(item.title || "\u65e0\u6807\u9898") + '</a></div>';
      html += '<div class="item-meta">';
      if (item._is_new) html += '<span class="tag tag-new">NEW</span>';
      if (source) html += '<span class="tag tag-source">' + escHtml(source) + '</span>';
      if (item.pub_date) html += '<span class="tag tag-date">' + escHtml(item.pub_date) + '</span>';
      if (item.region) html += '<span class="tag tag-region">' + escHtml(item.region) + '</span>';
      if (status) html += '<span class="tag tag-status" style="background:' + color + '">' + escHtml(status) + '</span>';
      html += '</div>';
      html += '<div class="item-detail">';
      if (item.notice_type) html += '<div class="detail-row"><span class="key">\u516c\u544a\u7c7b\u578b</span><span class="val">' + escHtml(item.notice_type) + '</span></div>';
      if (item.buyer) html += '<div class="detail-row"><span class="key">\u91c7\u8d2d\u4eba</span><span class="val">' + escHtml(item.buyer) + '</span></div>';
      if (item.project_no) html += '<div class="detail-row"><span class="key">\u9879\u76ee\u7f16\u53f7</span><span class="val">' + escHtml(item.project_no) + '</span></div>';
      if (item.budget) html += '<div class="detail-row"><span class="key">\u9884\u7b97\u91d1\u989d</span><span class="val">' + escHtml(item.budget) + '</span></div>';
      if (item.source) html += '<div class="detail-row"><span class="key">\u4fe1\u606f\u6765\u6e90</span><span class="val">' + escHtml(item.source) + '</span></div>';
      html += '<div class="detail-row"><span class="key">\u539f\u6587\u94fe\u63a5</span><span class="val"><a href="' + escHtml(url) + '" target="_blank">\u70b9\u51fb\u67e5\u770b\u539f\u6587</a></span></div>';
      html += '</div></div>';
    }
    html += '</div></div>';
  }
  area.innerHTML = html;
}

function drawTrend() {
  var canvas = document.getElementById("trendCanvas");
  if (!canvas) return;
  var ctx = canvas.getContext("2d");
  var W = canvas.offsetWidth, H = canvas.offsetHeight;
  canvas.width = W*2; canvas.height = H*2; ctx.scale(2,2);
  var data = TREND_VALUES, labels = TREND_LABELS;
  var maxV = Math.max.apply(null, data) || 1;
  var padL=30,padR=10,padT=10,padB=20,plotW=W-padL-padR,plotH=H-padT-padB;
  var step = plotW / (data.length-1||1);
  ctx.strokeStyle="#f0f0f0"; ctx.lineWidth=0.5;
  for(var i=0;i<=4;i++){var y=padT+plotH*(1-i/4);ctx.beginPath();ctx.moveTo(padL,y);ctx.lineTo(W-padR,y);ctx.stroke();ctx.fillStyle="#999";ctx.font="10px sans-serif";ctx.textAlign="right";ctx.fillText(Math.round(maxV*i/4),padL-4,y+3);}
  ctx.strokeStyle="#1B3A5C";ctx.lineWidth=2;ctx.lineJoin="round";ctx.beginPath();
  for(var i=0;i<data.length;i++){var x=padL+i*step,y=padT+plotH*(1-data[i]/maxV);if(i===0)ctx.moveTo(x,y);else ctx.lineTo(x,y);}
  ctx.stroke();
  ctx.lineTo(padL+(data.length-1)*step,padT+plotH);ctx.lineTo(padL,padT+plotH);ctx.closePath();ctx.fillStyle="rgba(27,58,92,0.08)";ctx.fill();
  ctx.fillStyle="#999";ctx.font="9px sans-serif";ctx.textAlign="center";
  if(labels.length>0){ctx.fillText(labels[0],padL,H-4);var mid=Math.floor(labels.length/2);ctx.fillText(labels[mid],padL+mid*step,H-4);ctx.fillText(labels[labels.length-1],padL+(labels.length-1)*step,H-4);}
}

function renderSourceList() {
  var list = document.getElementById("sourceList");
  var html = "", maxVal = SOURCE_PIE.length > 0 ? SOURCE_PIE[0].value : 1;
  for(var i=0;i<SOURCE_PIE.length;i++){var s=SOURCE_PIE[i];var pct=maxVal>0?(s.value/maxVal*100):0;var color=SOURCE_COLORS[i%SOURCE_COLORS.length];html+='<li><span>'+escHtml(s.name)+' <span style="color:#999">('+s.value+')</span></span><span><span class="source-bar" style="width:'+Math.max(pct,2)+'%;background:'+color+'"></span></span></li>';}
  if(!html) html='<li style="color:#999;text-align:center">\u6682\u65e0\u6570\u636e</li>';
  list.innerHTML = html;
}

function escHtml(s) {
  if(!s) return "";
  return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;").replace(/'/g,"&#39;");
}

function toggleGroup(header) {
  header.classList.toggle("collapsed");
  header.nextElementSibling.classList.toggle("collapsed");
}

function toggleDetail(item) {
  var detail = item.querySelector(".item-detail");
  if(detail) detail.classList.toggle("open");
}

function exportCSV() {
  var items = getFilteredItems();
  if(!items.length){alert("\u5f53\u524d\u6ca1\u6709\u53ef\u5bfc\u51fa\u7684\u6570\u636e");return;}
  var headers=["\u5e8f\u53f7","\u6807\u9898","\u5206\u7c7b","\u516c\u544a\u7c7b\u578b","\u91c7\u8d2d\u4eba","\u9879\u76ee\u7f16\u53f7","\u9884\u7b97\u91d1\u989d","\u533a\u57df","\u53d1\u5e03\u65e5\u671f","\u4fe1\u606f\u6765\u6e90","\u9879\u76ee\u72b6\u6001","\u539f\u6587\u94fe\u63a5"];
  var rows=[headers.join(",")];
  for(var i=0;i<items.length;i++){var it=items[i];var url=(it.url||"").split(" | ")[0]||"";var cells=[i+1,it.title||"",it.category||"",it.notice_type||"",it.buyer||"",it.project_no||"",it.budget||"",it.region||"",it.pub_date||"",it.source||"",it.project_status||"",url];for(var j=0;j<cells.length;j++){var v=String(cells[j]||"");v=v.replace(/"/g,'""');cells[j]='"'+v+'"';}rows.push(cells.join(","));}
  var csv="\ufeff"+rows.join("\\n");
  var blob=new Blob([csv],{type:"text/csv;charset=utf-8"});
  var link=document.createElement("a");link.href=URL.createObjectURL(blob);
  var dt=new Date();var stamp=dt.getFullYear()+(dt.getMonth()+1<10?"0":"")+(dt.getMonth()+1)+(dt.getDate()<10?"0":"")+dt.getDate();
  link.download="\u4e34\u6cc9\u62db\u6295\u6807\u4fe1\u606f_"+stamp+".csv";link.click();
}

init();
</script>
</body>
</html>'''

    # 替换占位符
    html = html.replace("__REGION_NAME__", REGION_NAME)
    html = html.replace("__CRAWL_TIME__", _crawl_time)
    html = html.replace("__DATA_JSON__", data_json)
    html = html.replace("__STATS_JSON__", stats_json)
    html = html.replace("__SITE_STATS_JSON__", site_stats_json)
    html = html.replace("__TREND_LABELS_JSON__", trend_labels_json)
    html = html.replace("__TREND_VALUES_JSON__", trend_values_json)
    html = html.replace("__SOURCE_PIE_JSON__", source_pie_json)
    html = html.replace("__HEALTH_CLASS__", health_class)
    html = html.replace("__HEALTH_TEXT__", health_text)

    return html


# ==================== RSS 生成 ====================
def build_rss(all_items, site_url=""):
    _crawl_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rss_base = site_url or "https://chipper-rolypoly-1a1e55.netlify.app"

    items_xml = ""
    cutoff = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    recent_items = [it for it in all_items if (it.get("pub_date") or "") >= cutoff]
    recent_items.sort(key=lambda x: x.get("pub_date", ""), reverse=True)
    recent_items = recent_items[:100]

    for item in recent_items:
        url = (item.get("url") or "").split(" | ")[0] or ""
        title = _rss_esc(item.get("title", "无标题"))
        pub_date = item.get("pub_date", "")
        if pub_date:
            try:
                dt = datetime.strptime(pub_date, "%Y-%m-%d")
                rss_date = dt.strftime("%a, %d %b %Y 00:00:00 +0800")
            except Exception:
                rss_date = pub_date
        else:
            rss_date = _crawl_time
        description = f"分类: {_rss_esc(item.get('category', '其他'))} | 类型: {_rss_esc(item.get('notice_type', ''))} | 采购人: {_rss_esc(item.get('buyer', ''))} | 区域: {_rss_esc(item.get('region', ''))}"
        if item.get("_is_new"):
            description = "[NEW] " + description
        items_xml += f"""    <item>
      <title>{title}</title>
      <link>{_rss_esc(url)}</link>
      <description>{description}</description>
      <pubDate>{rss_date}</pubDate>
      <guid isPermaLink="true">{_rss_esc(url)}</guid>
    </item>
"""

    rss = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>{_rss_esc(REGION_NAME)}招投标信息日报</title>
    <link>{rss_base}</link>
    <description>{_rss_esc(REGION_NAME)}招投标信息自动采集，7个数据源</description>
    <language>zh-CN</language>
    <pubDate>{_crawl_time}</pubDate>
    <atom:link href="{rss_base}/rss.xml" rel="self" type="application/rss+xml"/>
{items_xml}  </channel>
</rss>"""
    return rss


def _rss_esc(s):
    if not s:
        return ""
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ==================== Netlify 部署 ====================
_NL_SSL_CTX = ssl.create_default_context()
NETLIFY_API_BASE = "https://api.netlify.com/api/v1"


def _netlify_request(url, token, method="GET", data=None, content_type="application/json"):
    headers = {"Authorization": f"Bearer {token}", "Content-Type": content_type}
    if data and isinstance(data, str):
        data = data.encode("utf-8")
    req = urllib.request.Request(url, headers=headers, data=data, method=method)
    with urllib.request.urlopen(req, timeout=120, context=_NL_SSL_CTX) as r:
        body = r.read().decode("utf-8")
        return json.loads(body) if body else {}


def deploy_to_netlify(html_content, rss_content, token, site_id):
    """将 HTML + RSS 部署到 Netlify（file digest 方式）"""
    logger.info(f"开始 Netlify 部署，站点: {site_id}")

    html_sha1 = hashlib.sha1(html_content.encode("utf-8")).hexdigest()
    rss_sha1 = hashlib.sha1(rss_content.encode("utf-8")).hexdigest()
    logger.info(f"HTML SHA1: {html_sha1}, RSS SHA1: {rss_sha1}")

    # 步骤1：创建 deploy
    deploy_body = json.dumps({
        "files": {"index.html": html_sha1, "rss.xml": rss_sha1},
        "draft": False,
    })
    try:
        deploy_result = _netlify_request(
            f"{NETLIFY_API_BASE}/sites/{site_id}/deploys", token,
            method="POST", data=deploy_body
        )
    except Exception as e:
        logger.error(f"创建部署失败: {e}")
        return None

    deploy_id = deploy_result.get("id", "")
    if not deploy_id:
        logger.error(f"部署返回无 id: {deploy_result}")
        return None

    required = deploy_result.get("required", [])
    logger.info(f"部署创建成功, deploy_id={deploy_id}, required={required}")

    # 步骤2：上传文件
    for filename, content, sha1 in [
        ("index.html", html_content.encode("utf-8"), html_sha1),
        ("rss.xml", rss_content.encode("utf-8"), rss_sha1),
    ]:
        if sha1 not in required:
            logger.info(f"{filename} 已在缓存中，跳过上传")
            continue

        upload_url = f"{NETLIFY_API_BASE}/deploys/{deploy_id}/files/{filename}"
        upload_headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/octet-stream",
        }
        req = urllib.request.Request(upload_url, headers=upload_headers, data=content, method="PUT")
        try:
            with urllib.request.urlopen(req, timeout=120, context=_NL_SSL_CTX):
                logger.info(f"{filename} 上传成功")
        except urllib.error.HTTPError as e:
            err = e.read().decode("utf-8", errors="replace")
            logger.error(f"{filename} 上传失败 {e.code}: {err[:300]}")
            return None

    # 步骤3：等待部署完成
    logger.info("等待部署生效...")
    site_url = ""
    for i in range(12):
        time.sleep(5)
        try:
            status = _netlify_request(
                f"{NETLIFY_API_BASE}/sites/{site_id}/deploys/{deploy_id}", token
            )
            state = status.get("state", "")
            logger.info(f"部署状态: {state}")
            if not site_url:
                site_url = status.get("ssl_url") or status.get("url", "")
            if state == "ready":
                logger.info("部署完成！")
                break
            elif state in ("error", "rejected"):
                logger.error(f"部署失败: {state}")
                return None
        except Exception:
            pass
    else:
        logger.info("部署已提交（可能仍在处理中）")

    return site_url


# ==================== 主流程 ====================
def main():
    logger.info("=" * 60)
    logger.info(f"云端网页部署开始 {datetime.now():%Y-%m-%d %H:%M:%S}")
    logger.info(f"地区: {REGION_NAME}  关键词: {REGION_KEYWORDS}")
    logger.info("=" * 60)

    # ---- 步骤1：爬取 ----
    target_dates = [today_str(), (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")]
    logger.info(f"目标日期: {target_dates}")
    all_items, site_stats = crawl(target_dates=target_dates, region_keywords=REGION_KEYWORDS)

    if not all_items:
        logger.info("爬取结果为空")
        all_items = []

    # ---- 步骤2：分类去重 ----
    if all_items:
        classify_and_sort(all_items)
        all_items, _ = smart_dedupe(all_items)

    # ---- 步骤3：历史去重（只读，标记NEW，不更新history）----
    history = load_history()
    all_items, new_count = dedup_with_history(all_items, history)

    logger.info(f"爬取完成: 共 {len(all_items)} 条，新增 {new_count} 条")

    # ---- 步骤4：生成网页 + RSS ----
    logger.info("生成网页 HTML...")
    html = build_cloud_html(all_items, site_stats, new_count)
    rss = build_rss(all_items)

    # ---- 步骤5：部署到 Netlify ----
    if NETLIFY_TOKEN and NETLIFY_SITE_ID:
        logger.info("部署到 Netlify...")
        result_url = deploy_to_netlify(html, rss, NETLIFY_TOKEN, NETLIFY_SITE_ID)
        if result_url:
            logger.info(f"Netlify 部署完成: {result_url}")
        else:
            logger.error("Netlify 部署失败")
    else:
        logger.warning("Netlify 配置缺失，跳过部署")
        output_dir = os.path.join(SCRIPT_DIR, "data")
        os.makedirs(output_dir, exist_ok=True)
        with open(os.path.join(output_dir, "index.html"), "w", encoding="utf-8") as f:
            f.write(html)
        with open(os.path.join(output_dir, "rss.xml"), "w", encoding="utf-8") as f:
            f.write(rss)
        logger.info(f"HTML/RSS 已保存到 {output_dir}")

    logger.info("=" * 60)
    logger.info(f"云端网页部署完成 {datetime.now():%Y-%m-%d %H:%M:%S}")
    logger.info(f"共 {len(all_items)} 条，新增 {new_count} 条")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
