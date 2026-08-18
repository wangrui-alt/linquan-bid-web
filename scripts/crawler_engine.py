# -*- coding: utf-8 -*-
"""
招投标信息爬虫引擎 — 临泉特供版
纯标准库实现，零外部依赖。
从 7 个招投标网站爬取公告，分类排序，智能去重，地区筛选。

导出接口：
  PARSER_LIST, crawl(), classify_and_sort(), smart_dedupe(),
  filter_by_region(), parse_date(), today_str(),
  CATEGORY_LABELS, CATEGORY_COLORS, PROJECT_STATUS_MAP,
  DEFAULT_REGION_KW, DEFAULT_REGION_NAME,
"""

import json, re, urllib.request, urllib.error, urllib.parse
import os, sys, logging, hashlib, ssl, time
from datetime import datetime, timedelta
from html.parser import HTMLParser
from collections import OrderedDict

# ==================== 基础设施 ====================
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(SCRIPT_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

# 日志配置：使用 named logger，不污染 root logger，避免与导入方冲突
logger = logging.getLogger(__name__)
if not logger.handlers:
    logger.setLevel(logging.INFO)
    _ch = logging.StreamHandler(sys.stdout)
    _ch.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(_ch)

SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

# ==================== 常量 ====================
DEFAULT_REGION_KW = ["临泉", "236400"]
DEFAULT_REGION_NAME = "临泉县"

CATEGORY_PRIORITY = {
    "招标公告": 1, "公开招标": 1,
    "采购公告": 2, "竞争性磋商": 2, "竞争性谈判": 2, "询价公告": 2,
    "交易公告": 2, "出让公告": 2, "拍卖公告": 2,
    "中标候选人公示": 3, "中标结果公示": 3, "中标公告": 3, "中标结果": 3,
    "成交结果公示": 3, "成交公告": 3, "中标结果公告": 3,
    "更正公告": 4, "澄清公告": 4,
    "招标计划": 5, "文件预公示": 5,
    "通知公告": 6, "终止公告": 6,
    "其他公告": 7, "": 7,
}
CATEGORY_LABELS = {
    1: "核心公告", 2: "采购公告", 3: "结果公示",
    4: "更正澄清", 5: "计划预公示", 6: "通知终止", 7: "其他",
}
PROJECT_STATUS_MAP = {
    1: "招标中", 2: "招标中", 3: "已中标",
    4: "更正中", 5: "计划中", 6: "已终止", 7: "未知",
}
CATEGORY_COLORS = {
    "核心公告": "#FF6B6B", "采购公告": "#4ECDC4", "结果公示": "#45B7D1",
    "更正澄清": "#FFA07A", "计划预公示": "#98D8C8", "通知终止": "#DDA0DD", "其他": "#D3D3D3",
}

_DEDUPE_SUFFIXES = sorted([
    "招标公告", "公开招标", "采购公告", "中标候选人公示", "中标结果公示",
    "中标公告", "中标结果", "中标结果公告",
    "成交结果公示", "成交公告", "成交结果",
    "更正公告", "澄清公告", "终止公告", "通知公告",
    "竞争性磋商", "竞争性谈判", "询价公告",
    "招标计划", "文件预公示", "交易公告", "拍卖公告", "出让公告",
    "其他公告", "评审结束通知",
], key=len, reverse=True)

REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}
REQUEST_TIMEOUT = 20
RETRY_COUNT = 1
RETRY_DELAY = 3

# ==================== 工具类 ====================
class TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self._t = []
        self._skip = False
    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self._skip = True
    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript"):
            self._skip = False
    def handle_data(self, data):
        if not self._skip:
            self._t.append(data.strip())
    def get_text(self):
        return " ".join(t for t in self._t if t)


class LinkExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self._current_href = None
        self._current_text = []
    def handle_starttag(self, tag, attrs):
        if tag == "a":
            attr_dict = dict(attrs)
            self._current_href = attr_dict.get("href", "")
            self._current_text = []
    def handle_data(self, data):
        if self._current_href is not None:
            self._current_text.append(data)
    def handle_endtag(self, tag):
        if tag == "a" and self._current_href is not None:
            text = "".join(self._current_text).strip()
            self.links.append((self._current_href, text))
            self._current_href = None
            self._current_text = []


class SimpleDOM:
    @staticmethod
    def get_all_links(html, base_url=""):
        extractor = LinkExtractor()
        try:
            extractor.feed(html)
        except Exception:
            pass
        links = extractor.links
        if base_url:
            links = [(urllib.parse.urljoin(base_url, h), t) for h, t in links]
        return links

    @staticmethod
    def strip_tags(html):
        extractor = TextExtractor()
        try:
            extractor.feed(html)
            return extractor.get_text()
        except Exception:
            return re.sub(r'<[^>]+>', '', html)


# ==================== 网络请求 ====================
def fetch(url, timeout=REQUEST_TIMEOUT, encoding=None, extra_headers=None, method="GET", data=None):
    hdrs = {**REQUEST_HEADERS}
    if extra_headers:
        hdrs.update(extra_headers)
    for attempt in range(RETRY_COUNT + 1):
        try:
            if data and isinstance(data, str):
                data = data.encode("utf-8")
            req = urllib.request.Request(url, headers=hdrs, data=data, method=method)
            with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
                raw = r.read()
                if encoding:
                    return raw.decode(encoding, errors="ignore")
                for enc in ["utf-8", "gbk", "gb2312"]:
                    try:
                        return raw.decode(enc)
                    except (UnicodeDecodeError, ValueError):
                        continue
                return raw.decode("utf-8", errors="ignore")
        except Exception as e:
            if attempt < RETRY_COUNT:
                time.sleep(RETRY_DELAY)
            else:
                logger.warning(f"获取失败: {url} - {e}")
                return None


def fetch_json(url, method="POST", data=None, extra_headers=None):
    hdrs = {**REQUEST_HEADERS}
    if extra_headers:
        hdrs.update(extra_headers)
    for attempt in range(RETRY_COUNT + 1):
        try:
            if data is not None:
                if isinstance(data, dict):
                    data = json.dumps(data)
                if isinstance(data, str):
                    data = data.encode("utf-8")
            req = urllib.request.Request(url, headers=hdrs, data=data, method=method)
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT, context=SSL_CTX) as r:
                raw = r.read()
                text = raw.decode("utf-8", errors="ignore")
                return json.loads(text)
        except Exception as e:
            if attempt < RETRY_COUNT:
                time.sleep(RETRY_DELAY)
            else:
                logger.warning(f"JSON请求失败: {url} - {e}")
                return None


# ==================== 日期与分类 ====================
def parse_date(text):
    if not text:
        return ""
    text = text.strip()
    m = re.search(r'(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})', text)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m = re.search(r'(\d{2})-(\d{2})-(\d{2})$', text)
    if m:
        yy = int(m.group(1))
        year = 2000 + yy if yy < 50 else 1900 + yy
        return f"{year}-{m.group(2)}-{m.group(3)}"
    return ""


def today_str():
    return datetime.now().strftime("%Y-%m-%d")


def infer_bid_type_from_title(title):
    if not title:
        return ""
    if "更正" in title or "澄清" in title: return "更正公告"
    if "终止" in title: return "终止公告"
    if "中标候选人" in title: return "中标候选人公示"
    if "中标结果" in title or "中标公告" in title: return "中标公告"
    if "中标公示" in title: return "中标结果公示"
    if "成交结果" in title or "成交公告" in title: return "成交公告"
    if "招标公告" in title or "公开招标" in title: return "招标公告"
    if "采购公告" in title: return "采购公告"
    if "磋商" in title: return "竞争性磋商"
    if "谈判" in title: return "竞争性谈判"
    if "询价" in title or "询比" in title: return "询价公告"
    if "招标计划" in title: return "招标计划"
    if "文件预公示" in title: return "文件预公示"
    if "拍卖" in title: return "拍卖公告"
    if "出让" in title: return "出让公告"
    if "交易公告" in title: return "交易公告"
    if "采购项目" in title: return "采购公告"
    return ""


def classify_and_sort(items):
    for item in items:
        bid_type = (item.get("notice_type") or "").strip()
        title_type = infer_bid_type_from_title(item.get("title") or "")
        if title_type:
            if title_type in ("更正公告", "澄清公告", "终止公告",
                              "中标候选人公示", "中标公告", "中标结果公示", "中标结果",
                              "成交结果公示", "成交公告", "中标结果公告"):
                bid_type = title_type
            elif not bid_type:
                bid_type = title_type
            item["notice_type"] = bid_type
        priority = CATEGORY_PRIORITY.get(bid_type, 7)
        item["priority"] = priority
        item["category"] = CATEGORY_LABELS.get(priority, "其他")
        item["project_status"] = PROJECT_STATUS_MAP.get(priority, "未知")
    items.sort(key=lambda x: x.get("pub_date", ""), reverse=True)
    items.sort(key=lambda x: x.get("priority", 7))
    return items


# ==================== 智能去重 ====================
def normalize_title_for_dedupe(title):
    t = title.strip()
    for s in _DEDUPE_SUFFIXES:
        if t.endswith(s):
            return t[:-len(s)].strip()
    return t


def smart_dedupe(items):
    seen = OrderedDict()
    deduped_count = 0
    for item in items:
        notice_type = (item.get("notice_type") or "").strip()
        name = (item.get("title") or "").strip()
        if name:
            norm_name = normalize_title_for_dedupe(name)
            key = f"{hashlib.md5(norm_name.encode()).hexdigest()}:{notice_type}"
        else:
            continue
        if key in seen:
            existing = seen[key]
            if item.get("url") and item["url"] not in existing.get("url", ""):
                existing["url"] = (existing.get("url", "") + " | " + item["url"]).strip(" |")
            if item.get("source") and item["source"] not in existing.get("source", ""):
                existing["source"] = (existing.get("source", "") + " | " + item["source"]).strip(" |")
            deduped_count += 1
        else:
            seen[key] = item
    return list(seen.values()), deduped_count


# ==================== 地区筛选 ====================
def filter_by_region(items, region_keywords):
    if not region_keywords:
        return items
    result = []
    for item in items:
        text = f"{item.get('title', '')} {item.get('region', '')} {item.get('buyer', '')} {item.get('summary', '')}"
        for kw in region_keywords:
            if kw in text:
                result.append(item)
                break
    return result


# ==================== 7 个网站解析器 ====================

def parse_ccgp_gov_cn(target_dates):
    source_name = "中国政府采购网"
    base_url = "http://www.ccgp.gov.cn"
    results = []
    type_paths = {
        "/cggg/dfgg/gkzb/": "公开招标",
        "/cggg/dfgg/jzxcs/": "竞争性磋商",
        "/cggg/dfgg/jzxtpgg/": "竞争性谈判",
        "/cggg/dfgg/zbgg/": "中标公告",
        "/cggg/dfgg/gzgg/": "更正公告",
        "/cggg/dfgg/cjgg/": "成交公告",
        "/cggg/dfgg/qtgg/": "其他公告",
    }
    for path, bid_type in type_paths.items():
        url = base_url + path
        html = fetch(url, encoding="utf-8")
        if not html:
            continue
        ul_match = re.search(r'<ul\s+[^>]*class="[^"]*c_list_bid[^"]*"[^>]*>(.*?)</ul>', html, re.DOTALL)
        if not ul_match:
            continue
        ul_inner = ul_match.group(1)
        li_pattern = re.compile(r'<li[^>]*>(.*?)</li>', re.DOTALL)
        for li_m in li_pattern.finditer(ul_inner):
            li_html = li_m.group(1)
            a_m = re.search(r'<a[^>]*href="([^"]*)"[^>]*>(.*?)</a>', li_html, re.DOTALL)
            if not a_m:
                continue
            href = a_m.group(1)
            title = SimpleDOM.strip_tags(a_m.group(2)).strip()
            if not title or len(title) < 5:
                continue
            if href and not href.startswith("http"):
                href = urllib.parse.urljoin(url, href)
            ems = re.findall(r'<em[^>]*>(.*?)</em>', li_html, re.DOTALL)
            pub_date = parse_date(SimpleDOM.strip_tags(ems[0]).strip()) if len(ems) > 0 else ""
            region = SimpleDOM.strip_tags(ems[1]).strip() if len(ems) > 1 else ""
            buyer = SimpleDOM.strip_tags(ems[2]).strip() if len(ems) > 2 else ""
            if target_dates and pub_date not in target_dates:
                continue
            results.append({
                "title": title, "project_no": "", "notice_type": bid_type,
                "buyer": buyer, "budget": "", "region": region,
                "pub_date": pub_date, "source": source_name, "url": href,
            })
        time.sleep(0.5)
    logger.info(f"  {source_name}: {len(results)} 条")
    return results


def parse_cebpubservice(target_dates):
    source_name = "中国招标投标公共服务平台"
    api_url = "https://www.cebpubservice.com/ctpsp_iiss/searchbusinesstypebeforedooraction/getStringMethod.do"
    results = []
    biz_types = ["招标公告", "中标公告", "更正公告"]
    req_headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": "https://www.cebpubservice.com/ctpsp_iiss/searchbusinesstypebeforedooraction/getSearch.do",
        "Origin": "https://www.cebpubservice.com",
    }
    for biz_type in biz_types:
        data_str = (
            f"searchName=&searchArea=&searchIndustry=&centerPlat=&"
            f"businessType={urllib.parse.quote(biz_type)}&"
            f"searchTimeStart=&searchTimeStop=&timeTypeParam=&"
            f"bulletinIssnTime=&bulletinIssnTimeStart=&bulletinIssnTimeStop=&"
            f"pageNo=1&row=50"
        )
        result = fetch_json(api_url, method="POST", data=data_str, extra_headers=req_headers)
        if not result or not result.get("success"):
            continue
        returnlist = result.get("object", {}).get("returnlist", [])
        for item in returnlist:
            title = (item.get("businessObjectName") or "").strip()
            if not title:
                continue
            region = (item.get("regionName") or "").strip()
            receive_time = item.get("receiveTime") or ""
            pub_date = parse_date(receive_time) if receive_time else ""
            if target_dates and pub_date not in target_dates:
                continue
            row_guid = (item.get("rowGuid") or "") or (item.get("businessId") or "")
            url = f"http://connect.cebpubservice.com/PSPFrame/infobasemis/socialpublic/publicyewu/Frame_yewuDetail?rowguid={row_guid}" if row_guid else ""
            results.append({
                "title": title, "project_no": (item.get("tenderProjectCode") or ""),
                "notice_type": biz_type, "buyer": (item.get("transactionPlatfName") or ""),
                "budget": "", "region": region,
                "pub_date": pub_date, "source": source_name, "url": url,
            })
    logger.info(f"  {source_name}: {len(results)} 条")
    return results


def parse_ahtba_org_cn(target_dates):
    source_name = "安徽省招标投标信息网"
    base_url = "https://www.ahtba.org.cn"
    results = []
    for trade_type in ["01", "02"]:
        url = f"{base_url}/site/trade/affiche/gotoTradeList?tradeType={trade_type}"
        html = fetch(url, encoding="utf-8")
        if not html:
            continue
        rbl_html = ""
        for ul_m in re.finditer(r'<ul[^>]*>(.*?)</ul>', html, re.DOTALL):
            if "titBox" in ul_m.group(1):
                rbl_html = ul_m.group(1)
                break
        if not rbl_html:
            continue
        li_pattern = re.compile(r'<li[^>]*>(.*?)</li>', re.DOTALL)
        for li_m in li_pattern.finditer(rbl_html):
            li_html = li_m.group(1)
            tit_m = re.search(r'class="[^"]*titBox[^"]*"[^>]*>.*?<a[^>]*href="([^"]*)"[^>]*>(.*?)</a>', li_html, re.DOTALL)
            if not tit_m:
                tit_m = re.search(r'<a[^>]*href="([^"]*)"[^>]*>(.*?)</a>', li_html, re.DOTALL)
            if not tit_m:
                continue
            href = tit_m.group(1)
            title = SimpleDOM.strip_tags(tit_m.group(2)).strip()
            if not title or len(title) < 5:
                continue
            if href and not href.startswith("http"):
                href = urllib.parse.urljoin(base_url, href)
            nums_m = re.search(r'class="[^"]*nums[^"]*"[^>]*>(.*?)</div>', li_html, re.DOTALL)
            pub_date = parse_date(SimpleDOM.strip_tags(nums_m.group(1)).strip()) if nums_m else ""
            if target_dates and pub_date not in target_dates:
                continue
            region = "安徽"
            detail_m = re.search(r'class="[^"]*detailCons[^"]*"[^>]*>(.*?)</div>', li_html, re.DOTALL)
            if detail_m:
                detail_text = SimpleDOM.strip_tags(detail_m.group(1))
                m = re.search(r'项目区域：(.+?)(?:\s|$|项目类型)', detail_text)
                if m:
                    region = m.group(1).strip()
            results.append({
                "title": title, "project_no": "", "notice_type": "",
                "buyer": "", "budget": "", "region": region,
                "pub_date": pub_date, "source": source_name, "url": href,
            })
        time.sleep(0.5)
    logger.info(f"  {source_name}: {len(results)} 条")
    return results


def parse_ccgp_anhui(target_dates):
    source_name = "安徽政府采购网"
    base_url = "https://www.ccgp-anhui.gov.cn"
    api_url = f"{base_url}/portal/searchHome"
    results = []
    req_headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Referer": f"{base_url}/",
    }
    code_map = {
        "ZcyAnnouncement1": "文件预公示",
        "ZcyAnnouncement2": "采购公告",
        "ZcyAnnouncement3": "更正公告",
        "ZcyAnnouncement4": "",
        "ZcyAnnouncement5": "成交公告",
        "ZcyAnnouncement7": "成交公告",
    }
    for code, label in code_map.items():
        body = {"code": code, "keywords": "", "pageNo": 1, "pageSize": 100}
        data = json.dumps(body)
        result = fetch_json(api_url, method="POST", data=data, extra_headers=req_headers)
        if not result:
            continue
        result_data = result.get("result", {}).get("data", {})
        children = result_data.get("children", [])
        for item in children:
            title = (item.get("title") or "").strip()
            if not title:
                continue
            pub_ts = item.get("pubDate")
            pub_date = ""
            if pub_ts:
                try:
                    pub_date = datetime.fromtimestamp(pub_ts / 1000).strftime("%Y-%m-%d")
                except Exception:
                    pub_date = ""
            if target_dates and pub_date not in target_dates:
                continue
            article_id = (item.get("articleId") or "")
            url = f"{base_url}/portal/detail?articleId={article_id}" if article_id else ""
            title_type = infer_bid_type_from_title(title)
            notice_type = title_type if title_type else label
            results.append({
                "title": title, "project_no": (item.get("projectNo") or ""),
                "notice_type": notice_type,
                "buyer": (item.get("purchaseName") or ""), "budget": "",
                "region": (item.get("districtName") or "安徽"),
                "pub_date": pub_date, "source": source_name, "url": url,
            })
        time.sleep(0.3)
    logger.info(f"  {source_name}: {len(results)} 条")
    return results


def parse_youzhicai(target_dates):
    source_name = "优质采"
    base_url = "https://www.youzhicai.com"
    results = []
    html = fetch(base_url, encoding="utf-8")
    if not html:
        logger.info(f"  {source_name}: 0 条")
        return results
    links = SimpleDOM.get_all_links(html, base_url)
    for href, text in links:
        if "/nd/" not in href:
            continue
        text = text.strip()
        if len(text) < 5:
            continue
        title = text
        pub_date = ""
        m_date = re.search(r'(\d{2,4})[-/年](\d{1,2})[-/月](\d{1,2})', text)
        if m_date:
            yy = int(m_date.group(1))
            year = 2000 + yy if yy < 50 else yy
            pub_date = f"{year}-{int(m_date.group(2)):02d}-{int(m_date.group(3)):02d}"
            title = re.split(r'[\n_]{2,}', text)[0].strip()
        if target_dates and pub_date and pub_date not in target_dates:
            continue
        bid_type = infer_bid_type_from_title(title)
        results.append({
            "title": title, "project_no": "", "notice_type": bid_type,
            "buyer": "", "budget": "", "region": "",
            "pub_date": pub_date, "source": source_name, "url": href,
        })
    logger.info(f"  {source_name}: {len(results)} 条")
    return results


def parse_fuyang_ggzy(target_dates):
    source_name = "阜阳市公共资源交易中心"
    base_url = "https://jyzx.fy.gov.cn"
    results = []
    html = fetch(base_url, encoding="utf-8", extra_headers={"Referer": base_url})
    if not html:
        logger.info(f"  {source_name}: 0 条")
        return results
    today_compacts = [d.replace("-", "") for d in target_dates] if target_dates else []
    path_type_map = {
        "006002001": "招标计划", "006002002": "招标公告", "006002003": "招标公告",
        "006002004": "更正公告", "006002005": "招标公告",
        "006002006": "中标候选人公示", "006002007": "中标结果公示",
        "006002008": "中标结果", "006002009": "中标结果", "006002010": "招标公告",
        "006002012": "文件预公示",
        "006001001": "采购公告", "006001002": "采购公告", "006001003": "更正公告",
        "006001004": "采购公告", "006001005": "中标结果公告", "006001006": "中标结果公告",
        "006001007": "采购公告", "006001008": "终止公告", "006001009": "文件预公示",
        "006003001": "交易公告", "006003002": "更正公告", "006003003": "成交结果公示",
        "006003004": "拍卖公告", "006003005": "拍卖公告",
        "006004001": "出让公告", "006004002": "出让公告", "006004003": "成交公告",
        "006006001": "竞争性磋商", "006006003": "中标结果公告", "006006004": "采购公告",
        "tzgg": "通知公告",
    }
    seen_urls = set()
    links = SimpleDOM.get_all_links(html, base_url)
    for href, text in links:
        text = text.strip()
        m = re.search(r'/(\d{8})/', href)
        if not m or len(text) < 5:
            continue
        date_str = m.group(1)
        if today_compacts and date_str not in today_compacts:
            continue
        if href in seen_urls:
            continue
        seen_urls.add(href)
        bid_type = ""
        for code, btype in path_type_map.items():
            if code in href:
                bid_type = btype
                break
        clean_title = re.sub(r'\d{4}-\d{2}-\d{2}$', '', text).strip()
        if not clean_title:
            clean_title = text
        region = "阜阳市"
        for kw, area in [("临泉", "阜阳市临泉县"), ("太和", "阜阳市太和县"),
                         ("界首", "阜阳市界首市"), ("阜南", "阜阳市阜南县"),
                         ("颍上", "阜阳市颍上县"), ("颍东", "阜阳市颍东区"),
                         ("颍泉", "阜阳市颍泉区"), ("颍州", "阜阳市颍州区")]:
            if kw in clean_title:
                region = area
                break
        pub_date = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
        results.append({
            "title": clean_title, "project_no": "", "notice_type": bid_type,
            "buyer": "", "budget": "", "region": region,
            "pub_date": pub_date, "source": source_name, "url": href,
        })
    logger.info(f"  {source_name}: {len(results)} 条")
    return results


def parse_linquan_gov(target_dates):
    source_name = "临泉县人民政府"
    base_url = "https://www.linquan.gov.cn"
    results = []
    for page in range(1, 4):
        list_url = f"{base_url}/OpennessTarget/798/64728/page_{page}.html"
        html = fetch(list_url, encoding="utf-8")
        if not html:
            break
        tr_pattern = re.compile(r'<tr[^>]*>(.*?)</tr>', re.DOTALL)
        for tr_m in tr_pattern.finditer(html):
            tr_html = tr_m.group(1)
            tds = re.findall(r'<td[^>]*>(.*?)</td>', tr_html, re.DOTALL)
            if len(tds) < 3:
                continue
            a_m = re.search(r'<a[^>]*href="([^"]*)"[^>]*>(.*?)</a>', tds[1], re.DOTALL)
            if not a_m:
                continue
            href = a_m.group(1)
            title = SimpleDOM.strip_tags(a_m.group(2)).strip()
            if not title or len(title) < 3:
                continue
            if href and not href.startswith("http"):
                href = urllib.parse.urljoin(base_url, href)
            pub_date = parse_date(SimpleDOM.strip_tags(tds[-1]).strip())
            if target_dates and pub_date not in target_dates:
                continue
            results.append({
                "title": title, "project_no": "", "notice_type": "",
                "buyer": "", "budget": "", "region": "临泉县",
                "pub_date": pub_date, "source": source_name, "url": href,
            })
        time.sleep(0.3)
    logger.info(f"  {source_name}: {len(results)} 条")
    return results


PARSER_LIST = [
    ("中国政府采购网", parse_ccgp_gov_cn),
    ("中国招标投标公共服务平台", parse_cebpubservice),
    ("安徽省招标投标信息网", parse_ahtba_org_cn),
    ("安徽政府采购网", parse_ccgp_anhui),
    ("优质采", parse_youzhicai),
    ("阜阳市公共资源交易中心", parse_fuyang_ggzy),
    ("临泉县人民政府", parse_linquan_gov),
]


# ==================== 统一爬取入口 ====================
def crawl(target_dates=None, region_keywords=None):
    """爬取所有网站，返回 (items, site_stats)

    target_dates: 日期字符串列表，如 ['2026-08-17', '2026-08-18']
                  None 则不过滤日期
    region_keywords: 地区关键词列表，如 ['临泉', '236400']
                     None 则使用 DEFAULT_REGION_KW
    """
    if not region_keywords:
        region_keywords = DEFAULT_REGION_KW

    all_items = []
    site_stats = {}

    for i, (name, parser) in enumerate(PARSER_LIST, 1):
        logger.info(f"[{i}/7] 正在爬取 {name}...")
        try:
            items = parser(target_dates)
            all_items.extend(items)
            site_stats[name] = len(items)
        except Exception as e:
            site_stats[name] = -1
            logger.warning(f"  {name} 爬取出错: {e}")

    logger.info(f"列表页爬取完成，共 {len(all_items)} 条原始数据")

    # 地区筛选
    if region_keywords:
        before = len(all_items)
        all_items = filter_by_region(all_items, region_keywords)
        logger.info(f"地区筛选: {before} → {len(all_items)} 条")

    # 分类排序
    if all_items:
        classify_and_sort(all_items)

    # 智能去重
    if all_items:
        before = len(all_items)
        all_items, deduped_count = smart_dedupe(all_items)
        logger.info(f"智能去重: {before} → {len(all_items)} 条 (去除 {deduped_count} 条重复)")

    return all_items, site_stats
