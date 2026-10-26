"""Danh mục MCP server để duyệt & cài (web `/mcp/browse`, RPC `mcp.catalog`).

Nguồn: MCP Registry chính thức `GET https://registry.modelcontextprotocol.io/v0/servers?search=&limit=&cursor=&version=latest`
(một mục: `GET /v0/servers/<tên url-encode>/versions/latest`). Dạng trả về (đã kiểm bằng curl 2026-10-26):
`{"servers":[{"server":{name,title?,description,version,repository{url},websiteUrl?,remotes:[{type: streamable-http|sse, url,
headers:[{name,description,isRequired,isSecret,value}], variables:{tên:{description,isRequired,default}}}],
packages:[{registryType: npm|pypi|oci|nuget|mcpb, identifier, version, runtimeHint, transport{type},
environmentVariables:[{name,description,isRequired,isSecret,default}], packageArguments:[{type: positional|named, name?, value?}]}]},
"_meta":{...}}], "metadata":{nextCursor?, count}}`.

- Cache: RAM + đĩa (`HUB_HOME/cache/mcp_catalog/`), TTL `CACHE_TTL_S`. Lỗi mạng → bản cache cũ (kể cả hết hạn) → danh sách
  tuyển chọn đóng gói sẵn (`CURATED`, chỉ gồm mục đã kiểm trên registry/npm/PyPI).
- Chỉ ĐỌC dữ liệu từ mạng; không chạy gì. Phương án cài (`install_options`) chỉ dựng cấu hình đề xuất — cài thật đi qua
  `mcp.add` (probe + trust mặc định untrusted) như thêm tay.
- `check_oauth(url)`: remote không có header bắt buộc mà `initialize` trả 401 → cần đăng nhập OAuth (web cài với
  `auth: {type: oauth}`, người bấm Kết nối — docs/dev-notes/mcp-oauth.md).
Test thay `_http_get_json`/`_http_status` (không gọi mạng thật).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from ..secrets import normalize_name

log = logging.getLogger("cfharness.mcp_client.catalog")

REGISTRY_URL = "https://registry.modelcontextprotocol.io"
CACHE_TTL_S = 600.0
TIMEOUT_S = 8.0
LIMIT_MAX = 50
USER_AGENT = "cfharness-mcp-catalog/1"
REMOTE_TRANSPORTS = {"streamable-http": "http", "http": "http", "sse": "sse"}
SUPPORTED_PACKAGES = ("npm", "pypi")

_mem: dict[str, tuple[float, Any]] = {}
_lock = threading.Lock()


# ============================================================================ danh sách tuyển chọn (offline)
WORK, DEV, DATA, BIZ, DOCS, LOCAL = "Công việc & cộng tác", "Lập trình & vận hành", "Dữ liệu & phân tích", "Kinh doanh & thanh toán", \
    "Tài liệu & tìm kiếm", "Chạy trên máy"
CATEGORIES = (WORK, DEV, DATA, BIZ, DOCS, LOCAL)  # thứ tự nhóm ở trang "Đề xuất"


def _remote(name: str, title: str, description: str, url: str, *, category: str, version: str = "", repo: str = "", website: str = "",
            oauth: bool = False, client: str = "dcr", setup_url: str = "", transport: str = "streamable-http",
            headers: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """`oauth` = server trả 401 + metadata OAuth; `client` = `dcr` (tự đăng ký client, Kết nối một chạm) | `manual` (người tạo app
    OAuth ở nhà cung cấp rồi nhập client_id — `setup_url` chỉ chỗ tạo)."""
    return {"server": {"name": name, "title": title, "description": description, "version": version, "repository": {"url": repo},
                       "websiteUrl": website, "remotes": [{"type": transport, "url": url, **({"headers": headers} if headers else {})}]},
            "_meta": {"cfharness/curated": {"oauth": oauth, "client": client if oauth else None, "setup_url": setup_url, "category": category}}}


def _package(name: str, title: str, description: str, version: str, rtype: str, ident: str, repo: str, *, category: str = LOCAL,
             env: list[dict[str, Any]] | None = None, args: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    pkg: dict[str, Any] = {"registryType": rtype, "identifier": ident, "version": version, "transport": {"type": "stdio"}}
    if env:
        pkg["environmentVariables"] = env
    if args:
        pkg["packageArguments"] = args
    return {"server": {"name": name, "title": title, "description": description, "version": version, "repository": {"url": repo},
                       "packages": [pkg]}, "_meta": {"cfharness/curated": {"category": category}}}


# Đã kiểm 2026-10-26 bằng `initialize` không token + metadata OAuth (RFC 9728 → RFC 8414):
# - remote `oauth=True`: trả 401 + protected-resource metadata, máy chủ cấp quyền có S256; `client="manual"` = KHÔNG có
#   registration_endpoint (GitHub/Slack/HubSpot/Box/PagerDuty/Asana v2) → người nhập client_id; còn lại DCR (một chạm).
# - remote không `oauth`: `initialize` trả 200 không cần đăng nhập.
# - tên: lấy nguyên tên MCP Registry khi registry có remote cùng URL; tên khác (vd `com.slack/mcp`) là của hub.
# - gói npm/PyPI: phiên bản mới nhất trên npm/PyPI lúc kiểm. Mục tuyển chọn THẮNG mục registry cùng tên (`get`).
CURATED: list[dict[str, Any]] = [
    # ---- công việc & cộng tác
    _remote("com.atlassian/atlassian-mcp-server", "Atlassian Rovo", "Jira, Confluence, Compass, Loom: tìm, tạo và cập nhật issue, trang, dự án.",
            "https://mcp.atlassian.com/v2/mcp", category=WORK, version="2.0.0", repo="https://github.com/atlassian/atlassian-mcp-server", oauth=True),
    _remote("com.notion/mcp", "Notion", "Tìm, đọc, tạo và sửa trang, database trong workspace Notion.",
            "https://mcp.notion.com/mcp", category=WORK, website="https://developers.notion.com/docs/mcp", oauth=True),
    _remote("app.linear/linear", "Linear", "Issue, dự án, cycle và bình luận trong Linear.",
            "https://mcp.linear.app/mcp", category=WORK, website="https://linear.app/docs/mcp", oauth=True),
    _remote("com.asana/mcp", "Asana", "Task, dự án, portfolio trong Asana (bản V2 — cần tạo app OAuth trong Asana).",
            "https://mcp.asana.com/v2/mcp", category=WORK, website="https://developers.asana.com/docs/using-asanas-mcp-server", oauth=True,
            client="manual", setup_url="https://app.asana.com/0/my-apps"),
    _remote("com.monday/monday.com", "monday.com", "Board, item, cập nhật và tài liệu trên monday.com.",
            "https://mcp.monday.com/mcp", category=WORK, website="https://monday.com/w/mcp", oauth=True),
    _remote("com.clickup/mcp", "ClickUp", "Task, list, tài liệu và thời gian trong ClickUp.",
            "https://mcp.clickup.com/mcp", category=WORK, website="https://developer.clickup.com/docs/connect-an-ai-assistant-to-clickups-mcp-server", oauth=True),
    _remote("net.todoist/mcp", "Todoist", "Việc cần làm, dự án và nhãn trong Todoist.",
            "https://ai.todoist.net/mcp", category=WORK, website="https://www.todoist.com/help/articles/use-todoist-with-ai-assistants", oauth=True),
    _remote("com.airtable/mcp", "Airtable", "Đọc, tìm và sửa bản ghi trong base Airtable.",
            "https://mcp.airtable.com/mcp", category=WORK, website="https://support.airtable.com/docs/using-the-airtable-mcp-server", oauth=True),
    _remote("com.slack/mcp", "Slack", "Đọc kênh, tìm tin nhắn, gửi tin trong Slack (cần tạo app Slack có OAuth).",
            "https://mcp.slack.com/mcp", category=WORK, website="https://docs.slack.dev/ai/mcp-server", oauth=True, client="manual",
            setup_url="https://api.slack.com/apps"),
    _remote("io.github.miroapp/mcp-server", "Miro", "Đọc và tạo nội dung trên bảng Miro.",
            "https://mcp.miro.com/", category=WORK, repo="https://github.com/miroapp/miro-ai", oauth=True),
    _remote("com.figma.mcp/mcp", "Figma", "Đưa ngữ cảnh thiết kế Figma (frame, component, biến) vào agent.",
            "https://mcp.figma.com/mcp", category=WORK, version="1.0.3", repo="https://github.com/figma/mcp-server-guide", oauth=True),
    _remote("com.canva.mcp/mcp", "Canva", "Tìm, tạo và xuất thiết kế Canva.",
            "https://mcp.canva.com/mcp", category=WORK, website="https://www.canva.dev/docs/connect/canva-mcp-server-setup/", oauth=True),
    _remote("app.gamma/mcp", "Gamma", "Tạo bài trình bày, tài liệu, trang web bằng Gamma.",
            "https://mcp.gamma.app/mcp", category=WORK, website="https://gamma.app", oauth=True),
    _remote("com.box/mcp", "Box", "Tìm và đọc file, thư mục trong Box (cần tạo app OAuth trong Box).",
            "https://mcp.box.com", category=WORK, website="https://developer.box.com/guides/box-mcp/remote/", oauth=True, client="manual",
            setup_url="https://app.box.com/developers/console"),
    _remote("com.dropbox/mcp", "Dropbox", "Tìm, đọc và quản lý file trong Dropbox.",
            "https://mcp.dropbox.com/mcp", category=WORK, website="https://www.dropbox.com", oauth=True),
    _remote("ai.granola/mcp", "Granola", "Ghi chú và bản ghi cuộc họp trong Granola.",
            "https://mcp.granola.ai/mcp", category=WORK, website="https://www.granola.ai", oauth=True),
    _remote("ai.fireflies/fireflies", "Fireflies.ai", "Bản ghi, tóm tắt và việc cần làm từ cuộc họp Fireflies.",
            "https://api.fireflies.ai/mcp", category=WORK, website="https://fireflies.ai", oauth=True),
    _remote("com.webflow/mcp", "Webflow", "Quản lý site, trang và CMS Webflow.",
            "https://mcp.webflow.com/mcp", category=WORK, website="https://developers.webflow.com/mcp", oauth=True),
    _remote("com.wix/mcp", "Wix", "Quản lý site, nội dung và cửa hàng Wix.",
            "https://mcp.wix.com/mcp", category=WORK, website="https://dev.wix.com/docs/sdk/articles/use-the-wix-mcp/about-the-wix-mcp", oauth=True),
    _remote("com.zapier/mcp", "Zapier", "Gọi hàng nghìn ứng dụng qua action Zapier đã cấu hình.",
            "https://mcp.zapier.com/api/mcp/mcp", category=WORK, website="https://zapier.com/mcp", oauth=True),
    # ---- lập trình & vận hành
    _remote("io.github.github/github-mcp-server", "GitHub", "Repo, issue, PR, Actions trên GitHub (dùng Personal Access Token).",
            "https://api.githubcopilot.com/mcp/", category=DEV, version="2.0.3", repo="https://github.com/github/github-mcp-server",
            headers=[{"name": "Authorization", "description": "Bearer <Personal Access Token> — tạo ở github.com/settings/tokens",
                      "isRequired": True, "isSecret": True}]),
    _remote("com.gitlab/mcp", "GitLab", "Project, issue, merge request, pipeline trên GitLab.com.",
            "https://gitlab.com/api/v4/mcp", category=DEV, website="https://docs.gitlab.com/user/gitlab_duo/model_context_protocol/mcp_server/", oauth=True),
    _remote("io.github.getsentry/sentry-mcp", "Sentry", "Lỗi, issue, release và trace trong Sentry.",
            "https://mcp.sentry.dev/mcp", category=DEV, repo="https://github.com/getsentry/sentry-mcp", oauth=True),
    _remote("com.vercel/vercel-mcp", "Vercel", "Project, deployment, log và tài liệu Vercel.",
            "https://mcp.vercel.com", category=DEV, website="https://vercel.com/docs/mcp/vercel-mcp", oauth=True),
    _remote("com.netlify/mcp", "Netlify", "Tạo, deploy và quản lý site Netlify.",
            "https://netlify-mcp.netlify.app/mcp", category=DEV, website="https://docs.netlify.com/build/build-with-ai/netlify-mcp-server/", oauth=True),
    _remote("com.supabase/mcp", "Supabase", "Database, bảng, migration, edge function của project Supabase.",
            "https://mcp.supabase.com/mcp", category=DEV, website="https://supabase.com/docs/guides/getting-started/mcp", oauth=True),
    _remote("com.neon/mcp", "Neon", "Postgres serverless Neon: project, branch, truy vấn SQL.",
            "https://mcp.neon.tech/mcp", category=DEV, website="https://neon.com/docs/ai/neon-mcp-server", oauth=True),
    _remote("io.prisma/mcp", "Prisma Postgres", "Database Prisma Postgres: tạo, migrate, truy vấn.",
            "https://mcp.prisma.io/mcp", category=DEV, website="https://www.prisma.io/docs/postgres/integrations/mcp-server", oauth=True),
    _remote("com.cloudflare.mcp/cloudflare-workers", "Cloudflare Workers", "Worker, KV, R2, D1 trong tài khoản Cloudflare.",
            "https://bindings.mcp.cloudflare.com/mcp", category=DEV, repo="https://github.com/cloudflare/mcp-server-cloudflare", oauth=True),
    _remote("com.cloudflare.mcp/cloudflare-observability", "Cloudflare Observability", "Log và số liệu của Worker trên Cloudflare.",
            "https://observability.mcp.cloudflare.com/mcp", category=DEV, repo="https://github.com/cloudflare/mcp-server-cloudflare", oauth=True),
    _remote("com.postman/postman-mcp-server", "Postman", "Collection, workspace, API và môi trường Postman.",
            "https://mcp.postman.com/mcp", category=DEV, repo="https://github.com/postmanlabs/postman-mcp-server", oauth=True),
    _remote("dev.jam/jam", "Jam", "Báo lỗi Jam: video, console, network để debug.",
            "https://mcp.jam.dev/mcp", category=DEV, website="https://jam.dev/docs/debug-a-jam/mcp", oauth=True),
    _remote("com.buildkite/mcp", "Buildkite", "Pipeline, build, job log trên Buildkite.",
            "https://mcp.buildkite.com/mcp", category=DEV, website="https://buildkite.com/docs/apis/mcp-server", oauth=True),
    _remote("ai.semgrep/mcp", "Semgrep", "Quét bảo mật mã nguồn và xem phát hiện Semgrep.",
            "https://mcp.semgrep.ai/mcp", category=DEV, website="https://semgrep.dev/docs/mcp", oauth=True),
    _remote("io.github.grafana/mcp-grafana", "Grafana Cloud", "Dashboard, truy vấn Prometheus/Loki, cảnh báo trên Grafana Cloud.",
            "https://mcp.grafana.com/mcp", category=DEV, repo="https://github.com/grafana/mcp-grafana", oauth=True),
    _remote("io.honeycomb/mcp", "Honeycomb", "Truy vấn trace, SLO, trigger trên Honeycomb.",
            "https://mcp.honeycomb.io/mcp", category=DEV, website="https://docs.honeycomb.io/integrations/mcp/", oauth=True),
    _remote("com.pagerduty/mcp", "PagerDuty", "Sự cố, lịch trực, dịch vụ trên PagerDuty (cần tạo app OAuth trong PagerDuty).",
            "https://mcp.pagerduty.com/mcp", category=DEV, website="https://support.pagerduty.com/main/docs/pagerduty-mcp-server-integration-guide",
            oauth=True, client="manual", setup_url="https://developer.pagerduty.com/docs/app-integration-development"),
    _remote("co.huggingface/hf-mcp-server", "Hugging Face", "Tìm model, dataset, Space và bài báo trên Hugging Face (không cần đăng nhập).",
            "https://huggingface.co/mcp", category=DEV, website="https://huggingface.co/settings/mcp"),
    # ---- dữ liệu & phân tích
    _remote("com.mixpanel/mcp", "Mixpanel", "Truy vấn sự kiện, funnel, retention trên Mixpanel.",
            "https://mcp.mixpanel.com/mcp", category=DATA, website="https://docs.mixpanel.com/docs/features/mcp", oauth=True),
    _remote("io.github.PostHog/mcp", "PostHog", "Insight, feature flag, lỗi và truy vấn trên PostHog.",
            "https://mcp.posthog.com/mcp", category=DATA, repo="https://github.com/PostHog/mcp", oauth=True),
    _remote("com.amplitude/mcp-server", "Amplitude", "Biểu đồ, cohort, thí nghiệm trên Amplitude.",
            "https://mcp.amplitude.com/mcp", category=DATA, website="https://amplitude.com/docs/amplitude-ai/amplitude-mcp", oauth=True),
    # ---- kinh doanh & thanh toán
    _remote("com.hubspot/mcp", "HubSpot", "Contact, company, deal, ticket trong HubSpot CRM (cần tạo app MCP trong HubSpot).",
            "https://mcp.hubspot.com/", category=BIZ, website="https://developers.hubspot.com/mcp", oauth=True, client="manual",
            setup_url="https://developers.hubspot.com/docs/apps/developer-platform/build-apps/integrate-with-the-remote-hubspot-mcp-server"),
    _remote("com.intercom/mcp", "Intercom", "Hội thoại, contact, ticket hỗ trợ trên Intercom.",
            "https://mcp.intercom.com/mcp", category=BIZ, website="https://developers.intercom.com/docs/guides/mcp", oauth=True),
    _remote("com.attio/mcp", "Attio", "Bản ghi, danh sách, ghi chú trong CRM Attio.",
            "https://mcp.attio.com/mcp", category=BIZ, website="https://docs.attio.com/mcp/overview", oauth=True),
    _remote("com.close/close-mcp", "Close", "Lead, cơ hội, hoạt động trong CRM Close.",
            "https://mcp.close.com/mcp", category=BIZ, website="https://help.close.com/docs/mcp-server", oauth=True),
    _remote("io.github.Zoominfo/zoominfo-mcp", "ZoomInfo", "Tra cứu công ty, người liên hệ, tín hiệu mua hàng ZoomInfo.",
            "https://mcp.zoominfo.com/mcp", category=BIZ, website="https://www.zoominfo.com", oauth=True),
    _remote("com.stripe/mcp", "Stripe", "Khách hàng, thanh toán, hoá đơn, subscription trên Stripe.",
            "https://mcp.stripe.com", category=BIZ, website="https://docs.stripe.com/mcp", oauth=True),
    _remote("com.paypal.mcp/mcp", "PayPal", "Hoá đơn, đơn hàng, giao dịch PayPal.",
            "https://mcp.paypal.com/mcp", category=BIZ, website="https://developer.paypal.com/tools/mcp-server/", oauth=True),
    _remote("com.squareup/mcp", "Square", "Đơn hàng, thanh toán, kho, khách hàng trên Square.",
            "https://mcp.squareup.com/sse", category=BIZ, website="https://developer.squareup.com/docs/mcp", oauth=True, transport="sse"),
    # ---- tài liệu & tìm kiếm (không cần đăng nhập, trừ Tavily)
    _remote("io.github.upstash/context7", "Context7", "Tài liệu thư viện/framework luôn mới cho agent khi viết code.",
            "https://mcp.context7.com/mcp", category=DOCS, version="4.4.4", repo="https://github.com/upstash/context7"),
    _remote("com.deepwiki/mcp", "DeepWiki", "Hỏi đáp và đọc tài liệu sinh tự động của repo GitHub công khai.",
            "https://mcp.deepwiki.com/mcp", category=DOCS, website="https://docs.devin.ai/work-with-devin/deepwiki-mcp"),
    _remote("com.microsoft/microsoft-learn-mcp", "Microsoft Learn", "Tìm và đọc tài liệu chính thức của Microsoft/Azure.",
            "https://learn.microsoft.com/api/mcp", category=DOCS, repo="https://github.com/MicrosoftDocs/mcp"),
    _remote("com.amazonaws/knowledge-mcp", "AWS Knowledge", "Tài liệu, blog, hướng dẫn kiến trúc AWS.",
            "https://knowledge-mcp.global.api.aws", category=DOCS, website="https://awslabs.github.io/mcp/servers/aws-knowledge-mcp-server"),
    _remote("com.cloudflare.mcp/cloudflare-docs", "Cloudflare Docs", "Tìm trong tài liệu Cloudflare.",
            "https://docs.mcp.cloudflare.com/mcp", category=DOCS, repo="https://github.com/cloudflare/mcp-server-cloudflare"),
    _remote("ai.exa/exa", "Exa", "Tìm kiếm web và code bằng Exa.",
            "https://mcp.exa.ai/mcp", category=DOCS, website="https://docs.exa.ai/reference/exa-mcp"),
    _remote("com.tavily/mcp", "Tavily", "Tìm kiếm và trích nội dung web bằng Tavily.",
            "https://mcp.tavily.com/mcp", category=DOCS, website="https://docs.tavily.com/documentation/mcp", oauth=True),
    # ---- chạy trên máy (stdio)
    _package("io.github.microsoft/playwright-mcp", "Playwright", "Điều khiển trình duyệt: mở trang, bấm, điền form, chụp ảnh.",
             "0.0.83", "npm", "@playwright/mcp", "https://github.com/microsoft/playwright-mcp"),
    _package("io.github.ChromeDevTools/chrome-devtools-mcp", "Chrome DevTools", "Điều khiển và debug Chrome: console, network, hiệu năng.",
             "1.12.0", "npm", "chrome-devtools-mcp", "https://github.com/ChromeDevTools/chrome-devtools-mcp"),
    _package("modelcontextprotocol/server-filesystem", "Filesystem", "Đọc/ghi file trong các thư mục được cho phép.",
             "2.0.1", "npm", "@modelcontextprotocol/server-filesystem", "https://github.com/modelcontextprotocol/servers",
             args=[{"type": "positional", "valueHint": "thư mục được phép", "isRequired": True, "description": "Thư mục server được phép truy cập"}]),
    _package("modelcontextprotocol/server-git", "Git", "Đọc lịch sử, diff, nhánh và commit trong repo git trên máy.",
             "2026.10.19", "pypi", "mcp-server-git", "https://github.com/modelcontextprotocol/servers"),
    _package("modelcontextprotocol/server-fetch", "Fetch", "Tải trang web và chuyển sang markdown.",
             "2026.10.24", "pypi", "mcp-server-fetch", "https://github.com/modelcontextprotocol/servers"),
    _package("modelcontextprotocol/server-memory", "Memory", "Bộ nhớ dài hạn dạng đồ thị tri thức (file trên máy).",
             "2.0.0", "npm", "@modelcontextprotocol/server-memory", "https://github.com/modelcontextprotocol/servers"),
    _package("modelcontextprotocol/server-sequential-thinking", "Sequential Thinking", "Công cụ suy nghĩ từng bước cho bài toán nhiều bước.",
             "2.0.0", "npm", "@modelcontextprotocol/server-sequential-thinking", "https://github.com/modelcontextprotocol/servers"),
    _package("modelcontextprotocol/server-time", "Time", "Giờ hiện tại và đổi múi giờ.",
             "2026.10.19", "pypi", "mcp-server-time", "https://github.com/modelcontextprotocol/servers"),
]


# ============================================================================ HTTP (thay được trong test)
def _http_get_json(url: str, timeout: float) -> Any:
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — URL cố định https registry
        return json.loads(resp.read(4_000_000).decode("utf-8"))


def _http_status(url: str, timeout: float) -> int:
    """POST `initialize` tối thiểu (không header xác thực) → mã HTTP. Không đọc/ghi gì khác."""
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "cfharness", "version": "0"}}}).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": "application/json", "User-Agent": USER_AGENT,
                                                                         "Accept": "application/json, text/event-stream"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — chỉ http(s) (kiểm ở check_oauth)
            return int(resp.status)
    except urllib.error.HTTPError as e:
        return int(e.code)


# ============================================================================ cache
def _cache_file(cache_dir: Path | None, key: str) -> Path | None:
    if cache_dir is None:
        return None
    return cache_dir / (hashlib.sha256(key.encode()).hexdigest()[:32] + ".json")


def _cache_get(key: str, cache_dir: Path | None, *, max_age: float | None) -> Any:
    now = time.time()
    with _lock:
        hit = _mem.get(key)
    if hit is not None and (max_age is None or now - hit[0] <= max_age):
        return hit[1]
    f = _cache_file(cache_dir, key)
    if f is not None and f.is_file():
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            if max_age is None or now - float(d.get("ts") or 0) <= max_age:
                with _lock:
                    _mem[key] = (float(d.get("ts") or 0), d.get("data"))
                return d.get("data")
        except (OSError, ValueError):
            return None
    return None


def _cache_put(key: str, data: Any, cache_dir: Path | None) -> None:
    ts = time.time()
    with _lock:
        _mem[key] = (ts, data)
    f = _cache_file(cache_dir, key)
    if f is None:
        return
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps({"ts": ts, "key": key, "data": data}, ensure_ascii=False), encoding="utf-8")
        tmp.replace(f)
    except OSError:  # pragma: no cover — cache đĩa là phụ
        log.debug("không ghi được cache catalog", exc_info=True)


def clear_cache() -> None:
    with _lock:
        _mem.clear()


# ============================================================================ chuẩn hoá
def _s(v: Any, n: int = 2000) -> str:
    return str(v)[:n] if isinstance(v, (str, int, float)) else ""


def _url(v: Any) -> str:
    """Link hiển thị (repo/website) chỉ nhận http(s) — chặn `javascript:` & co. từ dữ liệu bên thứ ba."""
    u = _s(v, 500).strip()
    return u if re.match(r"^https?://[^\s\"'<>]+$", u) else ""


def _field(d: dict[str, Any]) -> dict[str, Any]:
    return {"name": _s(d.get("name"), 200), "description": _s(d.get("description"), 500), "required": bool(d.get("isRequired")),
            "secret": bool(d.get("isSecret")), "default": _s(d.get("default") if d.get("default") is not None else d.get("value"), 500)}


def normalize(item: dict[str, Any], *, trusted_meta: bool = False) -> dict[str, Any] | None:
    """Một phần tử `servers[]` của registry → mục gọn, chỉ trường cần hiển thị/cài. Dữ liệu là của bên thứ ba: chỉ là CHỮ.
    `trusted_meta` chỉ bật cho `CURATED` (khoá `cfharness/curated` trong dữ liệu registry bị bỏ qua)."""
    srv = item.get("server") if isinstance(item, dict) else None
    if not isinstance(srv, dict) or not isinstance(srv.get("name"), str):
        return None
    meta = item.get("_meta") if isinstance(item.get("_meta"), dict) else {}
    official = meta.get("io.modelcontextprotocol.registry/official") if isinstance(meta, dict) else None
    curated = meta.get("cfharness/curated") if trusted_meta and isinstance(meta, dict) else None
    remotes = []
    for r in srv.get("remotes") or []:
        if not isinstance(r, dict) or not isinstance(r.get("url"), str):
            continue
        variables = r.get("variables") if isinstance(r.get("variables"), dict) else {}
        remotes.append({"type": _s(r.get("type"), 40), "transport": REMOTE_TRANSPORTS.get(str(r.get("type"))),
                        "url": _s(r.get("url"), 1000),
                        "headers": [_field(h) for h in r.get("headers") or [] if isinstance(h, dict) and h.get("name")],
                        "variables": [{**_field(v), "name": _s(k, 100)} for k, v in variables.items() if isinstance(v, dict)]})
    packages = []
    for p in srv.get("packages") or []:
        if not isinstance(p, dict) or not isinstance(p.get("identifier"), str):
            continue
        tr = p.get("transport") if isinstance(p.get("transport"), dict) else {}
        args = []
        for a in p.get("packageArguments") or []:
            if isinstance(a, dict):
                args.append({"type": _s(a.get("type"), 20) or "positional", "name": _s(a.get("name"), 200), "value": _s(a.get("value"), 500),
                             "hint": _s(a.get("valueHint"), 100), "description": _s(a.get("description"), 300), "required": bool(a.get("isRequired")),
                             "secret": bool(a.get("isSecret"))})
        packages.append({"registry_type": _s(p.get("registryType"), 20), "identifier": _s(p.get("identifier"), 300),
                         "version": _s(p.get("version"), 60), "runtime_hint": _s(p.get("runtimeHint"), 40), "transport": _s(tr.get("type"), 40) or "stdio",
                         "env": [_field(e) for e in p.get("environmentVariables") or [] if isinstance(e, dict) and e.get("name")], "args": args})
    repo = srv.get("repository") if isinstance(srv.get("repository"), dict) else {}
    out = {"name": srv["name"][:200], "title": _s(srv.get("title"), 200) or srv["name"].rsplit("/", 1)[-1],
           "description": _s(srv.get("description"), 1000), "version": _s(srv.get("version"), 60),
           "repository": _url(repo.get("url")), "website": _url(srv.get("websiteUrl")), "remotes": remotes, "packages": packages,
           "status": _s((official or {}).get("status"), 20) if isinstance(official, dict) else "",
           "curated": isinstance(curated, dict), "needs_oauth": bool((curated or {}).get("oauth")) if isinstance(curated, dict) else None,
           "oauth_client": "manual" if isinstance(curated, dict) and curated.get("client") == "manual" else None,
           "setup_url": _url(curated.get("setup_url")) if isinstance(curated, dict) else "",
           "category": _s(curated.get("category"), 60) if isinstance(curated, dict) else ""}
    out["kinds"] = sorted({"remote" for _ in remotes} | {p["registry_type"] for p in packages if p["registry_type"]})
    out["installable"] = bool(install_options(out))
    return out


# ============================================================================ phương án cài
_GENERIC_SEGMENTS = frozenset({"mcp", "mcp-server", "server", "remote", "remote-mcp"})
_NS_NOISE = frozenset({"com", "io", "ai", "app", "dev", "net", "org", "co", "github", "mcp", "www"})


def suggest_name(registry_name: str) -> str:
    """`io.github.microsoft/playwright-mcp` → `playwright-mcp` (khớp NAME_RE của registry hub). Đoạn cuối chung chung
    (`com.notion/mcp`, `io.github.miroapp/mcp-server`) → lấy tên tổ chức trong namespace: `notion`, `miroapp`."""
    ns, _, base = registry_name.rpartition("/")
    base = base or registry_name
    if base.lower() in _GENERIC_SEGMENTS and ns:
        labels = [x for x in ns.split(".") if x and x.lower() not in _NS_NOISE]
        base = labels[-1] if labels else base
    s = re.sub(r"[^A-Za-z0-9_-]+", "-", base).strip("-_")[:63] or "mcp"
    return s if s[0].isalnum() else "m" + s[:62]


def secret_name(server: str, key: str) -> str:
    """Tên secret cho trường bí mật — cùng quy ước với bộ tách secret của import (`MCP_<server>_<khoá>`)."""
    return normalize_name(f"MCP_{server}_{key}")


def _pkg_command(p: dict[str, Any]) -> tuple[str, list[str]] | None:
    ident, ver = p["identifier"], p.get("version") or ""
    if p["registry_type"] == "npm":
        return "npx", ["-y", f"{ident}@{ver}" if ver and ver != "latest" else ident]
    if p["registry_type"] == "pypi":
        return "uvx", [f"{ident}=={ver}" if ver and ver != "latest" else ident]
    return None


def install_options(entry: dict[str, Any]) -> list[dict[str, Any]]:
    """Các cách cài được hỗ trợ: remote http/sse, gói npm (`npx -y id@ver`), pypi (`uvx id==ver`) chạy stdio.
    Gói oci/nuget/mcpb hoặc gói tự chạy http → chưa hỗ trợ (hiển thị để người tự thêm tay)."""
    opts: list[dict[str, Any]] = []
    for i, r in enumerate(entry.get("remotes") or []):
        if not r.get("transport"):
            continue
        need_oauth = entry.get("needs_oauth")
        opts.append({"id": f"r{i}", "kind": "remote", "label": f"Remote {r['transport']} — {r['url']}", "transport": r["transport"], "url": r["url"],
                     "headers": r.get("headers") or [], "variables": r.get("variables") or [], "env": [], "args": [],
                     "needs_oauth": need_oauth if need_oauth is not None else None,
                     "oauth_client": entry.get("oauth_client") if need_oauth else None, "setup_url": entry.get("setup_url") or ""})
    for i, p in enumerate(entry.get("packages") or []):
        if p.get("registry_type") not in SUPPORTED_PACKAGES or p.get("transport") not in ("stdio", ""):
            continue
        cmd = _pkg_command(p)
        if cmd is None:
            continue
        fixed: list[str] = []
        fields: list[dict[str, Any]] = []
        for j, a in enumerate(p.get("args") or []):
            if a.get("value") and "{" not in a["value"] and not a.get("secret"):
                fixed += ([a["name"]] if a["type"] == "named" and a.get("name") else []) + [a["value"]]
            else:
                fields.append({"key": f"arg{j}", "name": a.get("name") or a.get("hint") or f"tham số {j + 1}", "named": a["type"] == "named" and a.get("name"),
                               "description": a.get("description") or "", "required": a.get("required"), "secret": a.get("secret"),
                               "default": "" if "{" in (a.get("value") or "") else a.get("value") or ""})
        opts.append({"id": f"p{i}", "kind": p["registry_type"], "label": f"{p['registry_type']} {p['identifier']}" + (f"@{p['version']}" if p.get("version") else ""),
                     "transport": "stdio", "command": cmd[0], "args": cmd[1] + fixed, "arg_fields": fields, "env": p.get("env") or [],
                     "headers": [], "variables": [], "needs_oauth": False})
    return opts


def check_oauth(option: dict[str, Any], *, timeout: float = 5.0) -> bool | None:
    """Remote KHÔNG có header bắt buộc mà `initialize` trả 401 → cần OAuth. None = không xác định (lỗi mạng, có header bắt buộc)."""
    if option.get("kind") != "remote" or option.get("needs_oauth") is not None:
        return option.get("needs_oauth")
    url = str(option.get("url") or "")
    if not url.startswith("https://") or "{" in url or _local_host(url):
        return None  # chỉ https công khai — daemon không bị dùng để gõ cửa dịch vụ cục bộ/nội bộ (dữ liệu registry là của bên thứ ba)
    if any(h.get("required") for h in option.get("headers") or []):
        return False
    key = "oauth:" + url
    hit = _cache_get(key, None, max_age=CACHE_TTL_S)
    if hit is not None:
        return bool(hit)
    try:
        code = _http_status(url, timeout)
    except (OSError, ValueError) as e:
        log.info("kiểm OAuth %s lỗi: %r", url, e)
        return None
    res = code == 401
    _cache_put(key, res, None)
    return res


def _local_host(url: str) -> bool:
    import ipaddress

    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    if not host or host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_unspecified


# ============================================================================ truy vấn
def _offline(search: str, limit: int) -> list[dict[str, Any]]:
    q = (search or "").strip().lower()
    out = []
    for it in CURATED:
        e = normalize(it, trusted_meta=True)
        if e and (not q or q in (e["name"] + " " + e["title"] + " " + e["description"]).lower()):
            out.append(e)
    return out[:limit]


def search(query: str = "", *, limit: int = 20, cursor: str | None = None, cache_dir: Path | None = None, offline: bool = False,
           timeout: float = TIMEOUT_S) -> dict[str, Any]:
    """Tìm trong MCP Registry. Trả `{servers, next_cursor, source: registry|cache|stale-cache|offline, error?}`."""
    limit = max(1, min(int(limit or 20), LIMIT_MAX))
    q = (query or "").strip()[:200]
    if offline:  # danh sách tuyển chọn nằm sẵn trong gói → trả đủ (trang "Đề xuất" nhóm theo loại)
        return {"servers": _offline(q, len(CURATED)), "next_cursor": None, "source": "offline", "query": q}
    params = {"limit": str(limit), "version": "latest"}
    if q:
        params["search"] = q
    if cursor:
        params["cursor"] = str(cursor)[:500]
    url = f"{REGISTRY_URL}/v0/servers?{urllib.parse.urlencode(params)}"
    hit = _cache_get(url, cache_dir, max_age=CACHE_TTL_S)
    source = "cache"
    err = None
    if hit is None:
        try:
            raw = _http_get_json(url, timeout)
            if not isinstance(raw, dict) or not isinstance(raw.get("servers"), list):
                raise ValueError("registry trả dữ liệu không đúng dạng")
            meta = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
            hit = {"servers": [e for e in (normalize(x) for x in raw["servers"]) if e], "next_cursor": meta.get("nextCursor") or None}
            _cache_put(url, hit, cache_dir)
            source = "registry"
        except (OSError, ValueError) as e:  # URLError/HTTPError/timeout là OSError
            err = f"không tải được MCP Registry: {type(e).__name__}: {e}"[:300]
            hit = _cache_get(url, cache_dir, max_age=None)
            source = "stale-cache"
            if hit is None:
                return {"servers": _offline(q, limit), "next_cursor": None, "source": "offline", "error": err, "query": q}
    servers = hit["servers"]
    if q and not cursor:  # trang đầu: mục tuyển chọn khớp đứng trước, bỏ bản registry trùng tên
        cur = _offline(q, limit)
        names = {e["name"] for e in cur}
        servers = cur + [e for e in servers if e.get("name") not in names]
    out = {"servers": servers, "next_cursor": hit.get("next_cursor"), "source": source, "query": q}
    if err:
        out["error"] = err
    return out


def get(name: str, *, cache_dir: Path | None = None, offline: bool = False, timeout: float = TIMEOUT_S) -> dict[str, Any] | None:
    """Một mục theo tên registry (bản mới nhất). Ưu tiên danh sách tuyển chọn khi offline / lỗi mạng."""
    name = (name or "").strip()[:200]
    if not name:
        return None
    cur = next((e for e in (normalize(x, trusted_meta=True) for x in CURATED) if e and e["name"] == name), None)
    if offline or cur is not None:  # mục tuyển chọn đã kiểm thật (URL, OAuth, header) → thắng dữ liệu registry cùng tên
        return cur
    url = f"{REGISTRY_URL}/v0/servers/{urllib.parse.quote(name, safe='')}/versions/latest"
    hit = _cache_get(url, cache_dir, max_age=CACHE_TTL_S)
    if hit is not None:
        return _merge_curated(hit, cur)
    try:
        raw = _http_get_json(url, timeout)
        e = normalize(raw) if isinstance(raw, dict) else None
        if e is not None and e["name"] != name:
            return cur  # registry trả mục khác tên → không tin
    except urllib.error.HTTPError as ex:
        if ex.code == 404:
            return cur
        e = None
    except (OSError, ValueError):
        e = None
    if e is None:
        return cur or _cache_get(url, cache_dir, max_age=None)
    _cache_put(url, e, cache_dir)
    return _merge_curated(e, cur)


def _merge_curated(e: dict[str, Any], cur: dict[str, Any] | None) -> dict[str, Any]:
    if cur and e.get("needs_oauth") is None and cur.get("needs_oauth") is not None:
        e = {**e, "needs_oauth": cur["needs_oauth"]}
        e["installable"] = bool(install_options(e))
    return e


__all__ = ["CURATED", "CATEGORIES", "search", "get", "install_options", "check_oauth", "normalize", "suggest_name", "secret_name", "clear_cache"]
