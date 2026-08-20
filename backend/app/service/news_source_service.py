# Copyright © 2026 深圳市深维智见教育科技有限公司 版权所有
# 未经授权，禁止转售或仿制。

"""
资讯搜索源服务（修复问题3）

真实故障（已实测）：
1. Bocha API Key 有效但账户无额度：
   POST https://api.bochaai.com/v1/web-search -> HTTP 403
   {"code":"403","message":"You do not have enough money or package quota"}
   旧代码 `response.raise_for_status()` 抛异常后被吞掉，只 return []，
   上层于是给出 "搜索 'xxx' 无结果"，掩盖了真实原因（配额不足）。
2. BID_APP_CODE 仍是 .env.example 的占位符 "your-bid-app-code"，
   81API 返回 HTTP 401，但代码把占位符当成"已配置"，于是每个关键词都刷一条 401 错误。

本模块提供带优先级的搜索源链：
    Bocha（付费，有 key 且有额度时优先）
 -> Serper（付费，可选）
 -> Eastmoney 免费搜索（无需 key，保证采集不至于 0 条）

每个源都返回结构化的 SearchOutcome，包含 provider / results / error，
让上层能上报真实原因而不是笼统的"无结果"。
"""

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

_PLACEHOLDER_PREFIXES = ("your-", "your_", "<", "xxx", "changeme", "replace-me")


def is_placeholder(value: Optional[str]) -> bool:
    """判断配置值是否为占位符/空值（.env.example 默认值不应被当成已配置）"""
    if not value:
        return True
    lowered = value.strip().lower()
    if not lowered:
        return True
    return any(lowered.startswith(p) for p in _PLACEHOLDER_PREFIXES)


@dataclass
class SearchOutcome:
    """单次搜索的结果与真实错误"""
    provider: str
    results: List[Dict[str, Any]] = field(default_factory=list)
    error: Optional[str] = None
    fatal: bool = False   # fatal=True 表示该源本轮不必再试（如配额用尽、鉴权失败）

    @property
    def ok(self) -> bool:
        return bool(self.results)


def _clean_html(text: str) -> str:
    """去掉搜索结果里的高亮标签"""
    if not text:
        return ""
    return re.sub(r"<[^>]+>", "", text).strip()


class BochaSearchProvider:
    """博查搜索（付费）"""

    name = "bocha"
    URL = "https://api.bochaai.com/v1/web-search"

    def __init__(self):
        self.api_key = os.getenv("BOCHA_API_KEY", "")

    @property
    def available(self) -> bool:
        return not is_placeholder(self.api_key)

    async def search(self, query: str, count: int = 10) -> SearchOutcome:
        if not self.available:
            return SearchOutcome(self.name, error="BOCHA_API_KEY 未配置", fatal=True)

        payload = {"query": query, "summary": True, "count": max(1, min(count, 50)), "page": 1}
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(self.URL, headers=headers, json=payload)
        except Exception as e:
            return SearchOutcome(self.name, error=f"请求异常: {type(e).__name__}: {e}")

        # 明确区分鉴权/配额类错误，避免被当成"无结果"
        if response.status_code in (401, 403, 429):
            detail = ""
            try:
                body = response.json()
                detail = body.get("message") or body.get("msg") or ""
            except Exception:
                detail = response.text[:200]
            return SearchOutcome(
                self.name,
                error=f"HTTP {response.status_code}: {detail or '鉴权失败或配额不足'}",
                fatal=True,
            )

        if response.status_code != 200:
            return SearchOutcome(self.name, error=f"HTTP {response.status_code}: {response.text[:200]}")

        try:
            data = response.json()
        except Exception as e:
            return SearchOutcome(self.name, error=f"响应解析失败: {e}")

        # Bocha 业务错误也可能带 200 外壳
        code = str(data.get("code", "200"))
        if code not in ("200", "0"):
            return SearchOutcome(
                self.name,
                error=f"业务错误 code={code}: {data.get('message') or data.get('msg') or ''}",
                fatal=code in ("401", "403", "429"),
            )

        value_list = (data.get("data") or {}).get("webPages", {}).get("value", [])
        if not isinstance(value_list, list):
            return SearchOutcome(self.name, error=f"返回结构异常: webPages.value 类型为 {type(value_list)}")

        results = []
        for item in value_list:
            url = item.get("url")
            if not url:
                continue
            summary = item.get("summary") or item.get("snippet") or ""
            if not summary:
                continue
            results.append(
                {
                    "url": url,
                    "title": _clean_html(item.get("name", "")),
                    "summary": _clean_html(summary),
                    "snippet": _clean_html(item.get("snippet", "")),
                    "siteName": item.get("siteName", ""),
                    "datePublished": item.get("datePublished", ""),
                    "provider": self.name,
                }
            )

        return SearchOutcome(self.name, results=results)


class SerperSearchProvider:
    """Serper.dev（Google 搜索，付费，可选）"""

    name = "serper"
    URL = "https://google.serper.dev/search"

    def __init__(self):
        self.api_key = os.getenv("SERPER_API_KEY", "")

    @property
    def available(self) -> bool:
        return not is_placeholder(self.api_key)

    async def search(self, query: str, count: int = 10) -> SearchOutcome:
        if not self.available:
            return SearchOutcome(self.name, error="SERPER_API_KEY 未配置", fatal=True)

        headers = {"X-API-KEY": self.api_key, "Content-Type": "application/json"}
        payload = {"q": query, "gl": "cn", "hl": "zh-cn", "num": max(1, min(count, 20))}

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(self.URL, headers=headers, json=payload)
        except Exception as e:
            return SearchOutcome(self.name, error=f"请求异常: {type(e).__name__}: {e}")

        if response.status_code in (401, 403, 429):
            return SearchOutcome(
                self.name, error=f"HTTP {response.status_code}: 鉴权失败或配额不足", fatal=True
            )
        if response.status_code != 200:
            return SearchOutcome(self.name, error=f"HTTP {response.status_code}: {response.text[:200]}")

        try:
            data = response.json()
        except Exception as e:
            return SearchOutcome(self.name, error=f"响应解析失败: {e}")

        results = []
        for item in data.get("organic", []):
            if not item.get("link"):
                continue
            results.append(
                {
                    "url": item["link"],
                    "title": _clean_html(item.get("title", "")),
                    "summary": _clean_html(item.get("snippet", "")),
                    "snippet": _clean_html(item.get("snippet", "")),
                    "siteName": item.get("source", ""),
                    "datePublished": item.get("date", ""),
                    "provider": self.name,
                }
            )
        return SearchOutcome(self.name, results=results)


class EastmoneySearchProvider:
    """
    东方财富资讯搜索（免费、无需 API Key）

    作为兜底源，保证在付费搜索不可用时"立即采集"仍能落库真实数据。
    接口：https://search-api-web.eastmoney.com/search/jsonp
    """

    name = "eastmoney"
    URL = "https://search-api-web.eastmoney.com/search/jsonp"

    @property
    def available(self) -> bool:
        return os.getenv("ENABLE_FREE_NEWS_SOURCE", "true").lower() not in ("0", "false", "no")

    async def search(self, query: str, count: int = 10) -> SearchOutcome:
        if not self.available:
            return SearchOutcome(self.name, error="免费源已通过 ENABLE_FREE_NEWS_SOURCE 关闭", fatal=True)

        param = {
            "uid": "",
            "keyword": query,
            "type": ["cmsArticleWebOld"],
            "client": "web",
            "clientType": "web",
            "clientVersion": "curr",
            "param": {
                "cmsArticleWebOld": {
                    "searchScope": "default",
                    "sort": "default",
                    "pageIndex": 1,
                    "pageSize": max(1, min(count, 30)),
                    "preTag": "<em>",
                    "postTag": "</em>",
                }
            },
        }
        url = f"{self.URL}?cb=jQuery&param={quote(json.dumps(param, ensure_ascii=False))}"
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
            ),
            "Referer": "https://so.eastmoney.com/",
        }

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(url, headers=headers)
        except Exception as e:
            return SearchOutcome(self.name, error=f"请求异常: {type(e).__name__}: {e}")

        if response.status_code != 200:
            return SearchOutcome(self.name, error=f"HTTP {response.status_code}")

        text = response.text.strip()
        # 剥离 jsonp 包装: jQuery({...})
        start = text.find("(")
        end = text.rfind(")")
        if start == -1 or end == -1 or end <= start:
            return SearchOutcome(self.name, error="JSONP 响应格式异常")

        try:
            data = json.loads(text[start + 1:end])
        except Exception as e:
            return SearchOutcome(self.name, error=f"响应解析失败: {e}")

        items = (data.get("result") or {}).get("cmsArticleWebOld") or []
        results = []
        for item in items:
            url_value = item.get("url")
            if not url_value:
                continue
            results.append(
                {
                    "url": url_value,
                    "title": _clean_html(item.get("title", "")),
                    "summary": _clean_html(item.get("content", "")),
                    "snippet": _clean_html(item.get("content", "")),
                    "siteName": item.get("mediaName", "") or "东方财富",
                    "datePublished": item.get("date", ""),
                    "provider": self.name,
                }
            )

        return SearchOutcome(self.name, results=results)


class NewsSearchService:
    """
    带回退链的资讯搜索服务

    search() 会依次尝试可用源，返回第一个有结果的 SearchOutcome；
    全部失败时返回最后一次的错误信息（真实原因，而不是"无结果"）。
    """

    def __init__(self):
        self.providers = [
            BochaSearchProvider(),
            SerperSearchProvider(),
            EastmoneySearchProvider(),
        ]
        # 本轮已判定不可用的源（配额/鉴权类错误），避免重复无效请求
        self._disabled: Dict[str, str] = {}

    def provider_status(self) -> Dict[str, Any]:
        """当前各源的配置状态（用于诊断接口）"""
        return {
            p.name: {
                "available": p.available,
                "disabled_reason": self._disabled.get(p.name),
            }
            for p in self.providers
        }

    def reset(self):
        """重置本轮禁用状态"""
        self._disabled.clear()

    async def search(self, query: str, count: int = 10) -> SearchOutcome:
        errors: List[str] = []
        last_outcome: Optional[SearchOutcome] = None

        for provider in self.providers:
            if provider.name in self._disabled:
                errors.append(f"{provider.name}: {self._disabled[provider.name]}(已跳过)")
                continue
            if not provider.available:
                errors.append(f"{provider.name}: 未配置")
                continue

            outcome = await provider.search(query, count=count)
            last_outcome = outcome

            if outcome.ok:
                if errors:
                    logger.warning("搜索 '%s' 回退到 %s；前序失败: %s", query, provider.name, "; ".join(errors))
                outcome.error = "; ".join(errors) if errors else None
                return outcome

            reason = outcome.error or "无结果"
            errors.append(f"{provider.name}: {reason}")
            if outcome.fatal:
                self._disabled[provider.name] = reason
                logger.warning("搜索源 %s 本轮禁用: %s", provider.name, reason)

        failed = SearchOutcome(
            provider=last_outcome.provider if last_outcome else "none",
            results=[],
            error="; ".join(errors) if errors else "所有搜索源均无结果",
        )
        return failed


_news_search_service: Optional[NewsSearchService] = None


def get_news_search_service() -> NewsSearchService:
    """获取搜索服务单例"""
    global _news_search_service
    if _news_search_service is None:
        _news_search_service = NewsSearchService()
    return _news_search_service
