# -*- coding: utf-8 -*-
"""
云端邮件推送入口 — GitHub Actions 使用
职责：爬取 → 历史去重（标记NEW）→ 邮件推送（全部展示+NEW标签）
与 cloud_web.py 各司其职，通过 bid_history.json 共享去重状态。

环境变量（通过 GitHub Secrets 注入）：
  SMTP_SERVER      - SMTP 服务器（默认 smtp.qq.com）
  SMTP_PORT        - SMTP 端口（默认 465）
  EMAIL_FROM       - 发件邮箱
  EMAIL_AUTH_CODE  - 邮箱授权码
  EMAIL_TO         - 收件邮箱（多人用逗号分隔，如 a@xx.com,b@xx.com）
  REGION_NAME      - 地区名称（默认 临泉县）
  REGION_KEYWORDS  - 地区关键词，逗号分隔（默认 临泉,236400）
"""

import json
import os
import sys
import smtplib
import ssl as ssl_module
import hashlib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formatdate
from datetime import datetime, timedelta

# 复用爬虫引擎（与 bid-web-linquan 共享同一份 crawler_engine.py）
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
SMTP_SERVER = get_env("SMTP_SERVER", "smtp.qq.com")
SMTP_PORT = int(get_env("SMTP_PORT", "465"))
EMAIL_FROM = get_env("EMAIL_FROM")
EMAIL_AUTH_CODE = get_env("EMAIL_AUTH_CODE")
EMAIL_TO_RAW = get_env("EMAIL_TO")
EMAIL_TO_LIST = [addr.strip() for addr in EMAIL_TO_RAW.split(",") if addr.strip()]

if EMAIL_TO_LIST:
    logger.info(f"收件人列表: {EMAIL_TO_LIST}")

# ==================== 历史去重（与 cloud_web.py 共享同一文件）====================
HISTORY_FILE = os.path.join(SCRIPT_DIR, "data", "bid_history.json")


def load_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"sent_ids": [], "last_update": ""}


def save_history(history):
    os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def dedup_with_history(items, history):
    """历史去重，标记新增条目"""
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
            sent_ids.add(dedup_id)
    return items, new_count


def _item_dedup_id(item):
    norm_name = normalize_title_for_dedupe(item.get("title", ""))
    notice_type = (item.get("notice_type") or "").strip()
    key = f"{hashlib.md5(norm_name.encode()).hexdigest()}:{notice_type}"
    return hashlib.sha1(key.encode()).hexdigest()


# ==================== 邮件发送 ====================
def send_email(html_content, subject):
    if not EMAIL_FROM or not EMAIL_AUTH_CODE or not EMAIL_TO_LIST:
        logger.warning("邮箱配置不完整，跳过邮件发送")
        return False

    msg = MIMEMultipart("alternative")
    msg["From"] = EMAIL_FROM
    msg["To"] = ", ".join(EMAIL_TO_LIST)
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    html_part = MIMEText(html_content, "html", "utf-8")
    msg.attach(html_part)

    try:
        context = ssl_module.create_default_context()
        with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT, context=context) as server:
            server.login(EMAIL_FROM, EMAIL_AUTH_CODE)
            server.sendmail(EMAIL_FROM, EMAIL_TO_LIST, msg.as_string())
        logger.info(f"邮件发送成功 ({len(EMAIL_TO_LIST)}人): {subject}")
        return True
    except Exception as e:
        logger.error(f"邮件发送失败: {e}")
        return False


# ==================== 邮件 HTML 构建 ====================
def build_email_html(all_items, new_count):
    """构建邮件 HTML（全部展示+NEW标签模式）"""
    # 按分类分组
    groups = {}
    for item in all_items:
        cat = item.get("category", "其他")
        if cat not in groups:
            groups[cat] = []
        groups[cat].append(item)

    total = len(all_items)

    if new_count > 0:
        banner_class = "banner-new"
        banner_text = f"今日新增 {new_count} 条 / 共 {total} 条"
    else:
        banner_class = "banner-none"
        banner_text = f"今日无新增 / 共 {total} 条历史公告汇总"

    cat_sections = ""
    for cat_name in [CATEGORY_LABELS.get(i, "其他") for i in range(1, 8)]:
        items_in_cat = groups.get(cat_name, [])
        if not items_in_cat:
            continue
        cat_color = CATEGORY_COLORS.get(cat_name, "#D3D3D3")
        cat_new_count = sum(1 for it in items_in_cat if it.get("_is_new"))

        rows_html = ""
        for idx, item in enumerate(items_in_cat, 1):
            url = (item.get("url") or "").split(" | ")[0] or "#"
            source = (item.get("source") or "").split(" | ")[0]
            is_new = item.get("_is_new", False)
            new_tag = '<span class="tag-new">NEW</span>' if is_new else ""
            item_class = "a-new" if is_new else ""
            rows_html += f'''
            <tr class="item-row {item_class}">
              <td class="col-idx">{idx}</td>
              <td class="col-title">
                <a href="{_esc(url)}" target="_blank">{_esc(item.get('title', '无标题'))}</a>
                {new_tag}
              </td>
              <td class="col-type">{_esc(item.get('notice_type', ''))}</td>
              <td class="col-buyer">{_esc(item.get('buyer', ''))}</td>
              <td class="col-date">{_esc(item.get('pub_date', ''))}</td>
              <td class="col-source">{_esc(source)}</td>
            </tr>'''

        cat_sections += f'''
      <div class="cat-section">
        <div class="cat-header" style="background:{cat_color}">
          <span>{_esc(cat_name)}</span>
          <span class="cat-count">{len(items_in_cat)} 条{' (新增 ' + str(cat_new_count) + ' 条)' if cat_new_count > 0 else ''}</span>
        </div>
        <table class="cat-table">
          <thead><tr>
            <th class="col-idx">#</th><th class="col-title">公告标题</th>
            <th class="col-type">类型</th><th class="col-buyer">采购人</th>
            <th class="col-date">日期</th><th class="col-source">来源</th>
          </tr></thead>
          <tbody>{rows_html}</tbody>
        </table>
      </div>'''

    # 分类概览卡片
    cat_stats = {cat: len(items) for cat, items in groups.items()}
    summary_cards = ""
    for cat, count in cat_stats.items():
        color = CATEGORY_COLORS.get(cat, "#D3D3D3")
        summary_cards += f'<div class="summary-stat"><div class="num" style="color:{color}">{count}</div><div class="label">{_esc(cat)}</div></div>'

    html = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  body {{ font-family:"Microsoft YaHei","PingFang SC",sans-serif; background:#f0f2f5; margin:0; padding:20px; color:#333; }}
  .container {{ max-width:960px; margin:0 auto; }}
  .header {{ background:linear-gradient(135deg,#1B3A5C,#2C5F8A); color:#fff; padding:24px; border-radius:8px 8px 0 0; text-align:center; }}
  .header h1 {{ margin:0 0 8px; font-size:22px; letter-spacing:1px; }}
  .header .meta {{ font-size:13px; opacity:0.85; }}
  .banner {{ padding:12px 20px; text-align:center; font-size:14px; font-weight:600; border-radius:0 0 8px 8px; margin-bottom:16px; }}
  .banner-new {{ background:linear-gradient(90deg,#FFF3E0,#fff); color:#E65100; border:1px solid #FFE0B2; }}
  .banner-none {{ background:linear-gradient(90deg,#F5F5F5,#fff); color:#666; border:1px solid #e0e0e0; }}
  .summary {{ background:#fff; border-radius:8px; padding:16px; margin-bottom:16px; box-shadow:0 2px 8px rgba(0,0,0,0.06); }}
  .summary-title {{ font-size:14px; font-weight:600; margin-bottom:10px; color:#1B3A5C; }}
  .summary-stats {{ display:flex; gap:12px; flex-wrap:wrap; }}
  .summary-stat {{ flex:1; min-width:60px; text-align:center; padding:8px; border-radius:6px; background:#f8f9fa; }}
  .summary-stat .num {{ font-size:22px; font-weight:700; color:#1B3A5C; }}
  .summary-stat .num.accent {{ color:#F44336; }}
  .summary-stat .label {{ font-size:11px; color:#999; margin-top:2px; }}
  .cat-section {{ margin-bottom:16px; border-radius:8px; overflow:hidden; box-shadow:0 2px 8px rgba(0,0,0,0.06); }}
  .cat-header {{ padding:10px 16px; color:#fff; font-size:15px; font-weight:600; display:flex; justify-content:space-between; align-items:center; }}
  .cat-count {{ background:rgba(255,255,255,0.3); border-radius:12px; padding:2px 10px; font-size:12px; font-weight:400; }}
  .cat-table {{ width:100%; border-collapse:collapse; background:#fff; }}
  .cat-table th {{ background:#f8f9fa; padding:8px 10px; font-size:12px; color:#666; text-align:left; border-bottom:1px solid #e8e8e8; }}
  .cat-table td {{ padding:8px 10px; font-size:13px; border-bottom:1px solid #f0f0f0; }}
  .col-idx {{ width:30px; text-align:center; color:#999; }}
  .col-title {{ min-width:200px; }}
  .col-title a {{ color:#1B3A5C; text-decoration:none; }}
  .col-title a:hover {{ color:#2C5F8A; text-decoration:underline; }}
  .col-type {{ width:70px; white-space:nowrap; }}
  .col-buyer {{ width:80px; white-space:nowrap; }}
  .col-date {{ width:80px; white-space:nowrap; }}
  .col-source {{ width:80px; white-space:nowrap; }}
  .tag-new {{ display:inline-block; background:#F44336; color:#fff; font-size:11px; padding:1px 6px; border-radius:3px; font-weight:600; animation:pulse 1.5s infinite; margin-left:4px; }}
  @keyframes pulse {{ 0%,100%{{opacity:1}} 50%{{opacity:.6}} }}
  .item-row.a-new {{ border-left:3px solid #FF9800; background:#FFF8E1 !important; }}
  .footer {{ text-align:center; padding:16px; font-size:12px; color:#999; }}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <h1>{_esc(REGION_NAME)}招投标信息日报</h1>
    <div class="meta">
      <span>更新于 {datetime.now():%Y-%m-%d %H:%M}</span>
      <span>|</span>
      <span>7个数据源自动采集</span>
      <span>|</span>
      <span>GitHub Actions 云端运行</span>
    </div>
  </div>
  <div class="banner {banner_class}">{banner_text}</div>

  <div class="summary">
    <div class="summary-title">分类概览</div>
    <div class="summary-stats">
      <div class="summary-stat"><div class="num">{total}</div><div class="label">总条数</div></div>
      <div class="summary-stat"><div class="num accent">{new_count}</div><div class="label">新增</div></div>
      {summary_cards}
    </div>
  </div>

  {cat_sections}

  <div class="footer">
    临泉特供版招投标信息自动推送 | 7个数据源自动采集 | GitHub Actions 云端运行 | 星辰超级智能体生成
  </div>
</div>
</body>
</html>'''
    return html


def _esc(s):
    if not s:
        return ""
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


# ==================== 主流程 ====================
def main():
    logger.info("=" * 60)
    logger.info(f"云端邮件推送开始 {datetime.now():%Y-%m-%d %H:%M:%S}")
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

    # ---- 步骤3：历史去重，标记新增 ----
    history = load_history()
    all_items, new_count = dedup_with_history(all_items, history)

    logger.info(f"爬取完成: 共 {len(all_items)} 条，新增 {new_count} 条")

    # ---- 步骤4：邮件推送 ----
    _yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    _today = today_str()
    if new_count > 0:
        subject = f"{REGION_NAME}招投标日报 ({_yesterday}~{_today}) - 新增{new_count}条/共{len(all_items)}条"
    elif len(all_items) > 0:
        subject = f"{REGION_NAME}招投标日报 ({_yesterday}~{_today}) - 今日无新增/共{len(all_items)}条"
    else:
        subject = f"{REGION_NAME}招投标日报 ({_yesterday}~{_today}) - 今日暂无信息"

    email_ok = send_email(build_email_html(all_items, new_count), subject)

    # ---- 步骤5：更新历史 ----
    if email_ok:
        existing_ids = set(history.get("sent_ids", []))
        existing_ids.update(it["_dedup_id"] for it in all_items if it.get("_dedup_id"))
        history["sent_ids"] = list(existing_ids)
        history["last_update"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        save_history(history)
        logger.info("历史去重数据已更新")
    else:
        logger.warning("邮件发送失败，历史数据未更新")

    logger.info("=" * 60)
    logger.info(f"云端邮件推送完成 {datetime.now():%Y-%m-%d %H:%M:%S}")
    logger.info(f"共 {len(all_items)} 条，新增 {new_count} 条，邮件: {'已发送' if email_ok else '未发送'}")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
