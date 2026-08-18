# 临泉县招投标信息云端爬虫

零依赖 Python 爬虫，通过 GitHub Actions 每天6次自动爬取 7 个招投标网站的临泉县公告，发送邮件推送并生成 Netlify 公网页面。

## 功能

- 自动爬取 7 个招投标网站
- 地区关键词筛选（临泉县）
- 智能去重
- 邮件推送（全部展示 + NEW 标签）
- 交互式网页展示（Netlify 公网部署）
- RSS Feed
- 导出 CSV

## 数据源

1. 中国政府采购网
2. 中国招标投标公共服务平台
3. 安徽省招标投标信息网
4. 安徽政府采购网
5. 优质采
6. 阜阳市公共资源交易中心
7. 临泉县人民政府

## GitHub Secrets 配置

| Secret | 说明 | 示例 |
|--------|------|------|
| `GH_TOKEN` | GitHub Token（Actions 推送用） | `github_pat_...` |
| `NETLIFY_TOKEN` | Netlify Personal Access Token | `nfp_...` |
| `NETLIFY_SITE_ID` | Netlify 站点 ID | `d66afe47-...` |
| `SMTP_SERVER` | SMTP 服务器 | `smtp.qq.com` |
| `SMTP_PORT` | SMTP 端口 | `465` |
| `EMAIL_FROM` | 发件邮箱 | `xxx@qq.com` |
| `EMAIL_AUTH_CODE` | 邮箱授权码 | `otdnng...` |
| `EMAIL_TO` | 收件邮箱 | `xxx@qq.com` |
| `REGION_NAME` | 地区名称 | `临泉县` |
| `REGION_KEYWORDS` | 地区关键词（逗号分隔） | `临泉,236400` |

## 本地测试

```bash
cd scripts
# 需要设置环境变量或直接修改 cloud_main.py 的默认值
NETLIFY_TOKEN=xxx NETLIFY_SITE_ID=xxx EMAIL_FROM=xxx EMAIL_AUTH_CODE=xxx EMAIL_TO=xxx python cloud_main.py
```
