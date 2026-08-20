# Copyright © 2026 深圳市深维智见教育科技有限公司 版权所有
# 未经授权，禁止转售或仿制。

"""
行业资讯和招投标信息采集服务
- 使用 Bocha API 搜索行业资讯
- 使用 81API 搜索招投标信息
- 支持多行业配置
"""
import os
import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional
import httpx
from sqlalchemy.orm import Session

from models.news import IndustryNews, BiddingInfo, NewsCollectionTask
from service.bidding_service import get_bidding_service
from service.news_source_service import NewsSearchService, is_placeholder
from config.industry_config import get_industry_config, get_all_industries

logger = logging.getLogger(__name__)


class NewsCollectionService:
    """资讯采集服务"""

    def __init__(self, db: Session):
        self.db = db
        self.bocha_api_key = os.getenv("BOCHA_API_KEY", "")
        self.bidding_service = get_bidding_service()
        # 每次采集使用独立的搜索服务实例，保证"本轮禁用"状态不跨请求污染
        self.search_service = NewsSearchService()

        if is_placeholder(self.bocha_api_key):
            logger.warning("BOCHA_API_KEY 未配置或为占位符，将使用回退搜索源")

    async def _search(self, query: str, count: int = 10) -> tuple[List[Dict], Optional[str], str]:
        """
        执行搜索（带多源回退）

        Returns:
            (results, error, provider)
            error 为真实失败原因（例如 "bocha: HTTP 403: You do not have enough money or package quota"）
        """
        logger.info(f"[_search] 搜索: query='{query}', count={count}")
        outcome = await self.search_service.search(query, count=count)

        if outcome.results:
            logger.info(
                f"[_search] provider={outcome.provider} 返回 {len(outcome.results)} 条结果"
            )
            return outcome.results, None, outcome.provider

        logger.warning(f"[_search] 搜索 '{query}' 失败: {outcome.error}")
        return [], outcome.error, outcome.provider

    async def _bocha_search(self, query: str, count: int = 10) -> List[Dict]:
        """兼容旧调用方的入口（内部已改为多源回退）"""
        results, _, _ = await self._search(query, count)
        return results

    async def collect_news(self, max_items: int = 20, industry_id: Optional[str] = None) -> Dict[str, Any]:
        """
        采集行业资讯

        Args:
            max_items: 最大采集数量
            industry_id: 行业ID，用于获取对应的搜索关键词

        Returns:
            采集结果
        """
        # 获取行业配置
        industry_config = get_industry_config(industry_id)
        news_keywords = industry_config.news_keywords
        logger.info(f"[collect_news] 使用行业: {industry_config.name}, 关键词数量: {len(news_keywords)}")

        task = NewsCollectionTask(
            task_type="news",
            status="running",
            started_at=datetime.utcnow()
        )
        self.db.add(task)
        self.db.commit()

        collected = []
        errors = []
        providers_used = set()

        try:
            items_per_keyword = max(2, max_items // len(news_keywords))

            for keyword in news_keywords:
                if len(collected) >= max_items:
                    break

                try:
                    # 多源搜索（Bocha -> Serper -> 免费源）
                    results, search_error, provider = await self._search(
                        keyword, count=items_per_keyword + 2
                    )

                    if not results:
                        errors.append(
                            f"搜索 '{keyword}' 失败: {search_error or '所有搜索源均无结果'}"
                        )
                        continue

                    providers_used.add(provider)
                    if search_error:
                        # 回退成功但主源有问题：记录真实原因，便于运维处理配额
                        logger.warning("搜索 '%s' 已回退 %s；前序失败: %s", keyword, provider, search_error)

                    count = 0
                    for item in results:
                        if count >= items_per_keyword:
                            break
                        if len(collected) >= max_items:
                            break

                        source_url = item.get('url', '')
                        if not source_url:
                            continue

                        # 检查是否已存在
                        existing = self.db.query(IndustryNews).filter(
                            IndustryNews.source_url == source_url
                        ).first()

                        if existing:
                            continue

                        # 解析发布时间
                        publish_time = self._parse_datetime(item.get('datePublished', ''))
                        if not publish_time:
                            publish_time = self._extract_date_from_snippet(item.get('snippet', ''))

                        # 判断分类
                        title = item.get('title', '')
                        content = item.get('summary', '') or item.get('snippet', '')
                        category = self._categorize_news(title, content)

                        news = IndustryNews(
                            industry_id=industry_id or "smart_transportation",
                            title=title[:500] if title else '无标题',
                            content=content,
                            source=item.get('siteName', '') or self._extract_source_from_link(source_url),
                            source_url=source_url,
                            category=category,
                            department=self._extract_department(title, content),
                            publish_time=publish_time,
                            keywords=keyword,
                            collected_at=datetime.utcnow()
                        )

                        self.db.add(news)
                        collected.append(news)
                        count += 1

                    # 避免请求过于频繁
                    await asyncio.sleep(0.3)

                except Exception as e:
                    errors.append(f"处理关键词 '{keyword}' 时出错: {str(e)}")
                    logger.error(f"Error processing keyword '{keyword}': {e}")

            self.db.commit()

            # success 反映真实结果：一条都没落库且全部关键词报错时不算成功
            success = len(collected) > 0 or not errors

            task.status = "completed" if success else "failed"
            task.total_collected = len(collected)
            task.completed_at = datetime.utcnow()
            if errors:
                task.error_message = "; ".join(errors[:5])
            self.db.commit()

            return {
                "success": success,
                "collected": len(collected),
                "errors": errors,
                "providers": sorted(providers_used),
            }

        except Exception as e:
            task.status = "failed"
            task.error_message = str(e)
            task.completed_at = datetime.utcnow()
            self.db.commit()
            logger.error(f"News collection failed: {e}", exc_info=True)

            return {
                "success": False,
                "error": str(e),
                "collected": len(collected),
                "errors": errors + [str(e)],
            }

    async def collect_bidding(self, max_items: int = 20, industry_id: Optional[str] = None) -> Dict[str, Any]:
        """
        采集招投标信息

        Args:
            max_items: 最大采集数量
            industry_id: 行业ID，用于获取对应的搜索关键词

        Returns:
            采集结果
        """
        # 获取行业配置
        industry_config = get_industry_config(industry_id)
        bidding_keywords = industry_config.bidding_keywords
        logger.info(f"[collect_bidding] 使用行业: {industry_config.name}, 关键词数量: {len(bidding_keywords)}")

        task = NewsCollectionTask(
            task_type="bidding",
            status="running",
            started_at=datetime.utcnow()
        )
        self.db.add(task)
        self.db.commit()

        collected = []
        errors = []
        quota_exhausted = False

        try:
            items_per_keyword = max(3, max_items // len(bidding_keywords))

            for keyword in bidding_keywords:
                if len(collected) >= max_items or quota_exhausted:
                    break

                try:
                    # 查询招标信息
                    bid_result = await self.bidding_service.search_bid_notices(
                        keyword=keyword,
                        page=1
                    )

                    # 检查是否配额用尽
                    if bid_result.get("quota_exhausted"):
                        logger.warning(f"[collect_bidding] API 配额已用尽，停止采集")
                        errors.append("招投标 API 配额已用尽，请续费或等待配额重置")
                        quota_exhausted = True
                        break

                    if bid_result.get("success"):
                        for item in bid_result.get("results", [])[:items_per_keyword]:
                            if len(collected) >= max_items:
                                break

                            bid_id = item.get("id")
                            if not bid_id:
                                continue

                            # 检查是否已存在
                            existing = self.db.query(BiddingInfo).filter(
                                BiddingInfo.bid_id == bid_id
                            ).first()

                            if existing:
                                continue

                            # 解析发布时间
                            publish_time = self._parse_datetime(item.get("publish_time"))

                            bidding = BiddingInfo(
                                industry_id=industry_id or "smart_transportation",
                                bid_id=bid_id,
                                title=item.get("title", "")[:500],
                                notice_type=item.get("notice_type", "招标"),
                                province=item.get("province"),
                                city=item.get("city"),
                                publish_time=publish_time,
                                source=item.get("source", "81api"),
                                collected_at=datetime.utcnow()
                            )

                            self.db.add(bidding)
                            collected.append(bidding)
                    else:
                        errors.append(f"查询招标 '{keyword}' 失败: {bid_result.get('error', '未知错误')}")

                    if quota_exhausted:
                        break

                    # 查询中标信息
                    win_result = await self.bidding_service.search_win_bids(
                        keyword=keyword,
                        page=1
                    )

                    # 检查是否配额用尽
                    if win_result.get("quota_exhausted"):
                        logger.warning(f"[collect_bidding] API 配额已用尽，停止采集")
                        errors.append("招投标 API 配额已用尽，请续费或等待配额重置")
                        quota_exhausted = True
                        break

                    if win_result.get("success"):
                        for item in win_result.get("results", [])[:items_per_keyword]:
                            if len(collected) >= max_items:
                                break

                            bid_id = item.get("id")
                            if not bid_id:
                                continue

                            # 检查是否已存在
                            existing = self.db.query(BiddingInfo).filter(
                                BiddingInfo.bid_id == bid_id
                            ).first()

                            if existing:
                                continue

                            publish_time = self._parse_datetime(item.get("publish_time"))

                            bidding = BiddingInfo(
                                industry_id=industry_id or "smart_transportation",
                                bid_id=bid_id,
                                title=item.get("title", "")[:500],
                                notice_type=item.get("notice_type", "中标"),
                                province=item.get("province"),
                                city=item.get("city"),
                                publish_time=publish_time,
                                source=item.get("source", "81api"),
                                collected_at=datetime.utcnow()
                            )

                            self.db.add(bidding)
                            collected.append(bidding)

                    if not quota_exhausted:
                        await asyncio.sleep(0.3)

                except Exception as e:
                    errors.append(f"处理关键词 '{keyword}' 时出错: {str(e)}")
                    logger.error(f"Error processing bidding keyword '{keyword}': {e}")

            self.db.commit()

            task.status = "completed"
            task.total_collected = len(collected)
            task.completed_at = datetime.utcnow()
            if errors:
                task.error_message = "; ".join(errors[:5])
            self.db.commit()

            return {
                "success": True,
                "collected": len(collected),
                "errors": errors
            }

        except Exception as e:
            task.status = "failed"
            task.error_message = str(e)
            task.completed_at = datetime.utcnow()
            self.db.commit()
            logger.error(f"Bidding collection failed: {e}")

            return {
                "success": False,
                "error": str(e),
                "collected": len(collected)
            }

    async def collect_all(
        self,
        max_news: int = 20,
        max_bidding: int = 20,
        industry_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        采集所有资讯（行业资讯+招投标）

        Args:
            max_news: 最大资讯采集数量
            max_bidding: 最大招投标采集数量
            industry_id: 行业ID
        """
        industry_config = get_industry_config(industry_id)
        logger.info(f"[NewsCollectionService] collect_all 开始: industry={industry_config.name}, max_news={max_news}, max_bidding={max_bidding}")
        logger.info(f"[NewsCollectionService] 搜索源状态: {self.search_service.provider_status()}")

        news_result = await self.collect_news(max_news, industry_id)
        logger.info(f"[NewsCollectionService] collect_news 结果: {news_result}")

        # 检查招投标 API 是否真的可用（占位符 your-bid-app-code 会被 81API 拒为 401）
        if is_placeholder(self.bidding_service.app_code):
            logger.warning("[NewsCollectionService] 招投标 API 未配置有效 BID_APP_CODE，跳过招投标采集")
            bidding_result = {
                "success": True,
                "collected": 0,
                "errors": ["招投标采集已跳过：BID_APP_CODE 未配置有效值（当前为空或占位符）"],
                "skipped": "BID_APP_CODE 未配置"
            }
        else:
            bidding_result = await self.collect_bidding(max_bidding, industry_id)
            logger.info(f"[NewsCollectionService] collect_bidding 结果: {bidding_result}")

        news_ok = news_result.get("success", False)
        bidding_ok = bidding_result.get("success", False)

        result = {
            "success": news_ok or bidding_ok,
            "news": news_result,
            "bidding": bidding_result,
            "industry": industry_config.name,
            "providers": news_result.get("providers", []),
        }
        logger.info(f"[NewsCollectionService] collect_all 返回: success={result['success']}, "
                    f"news={news_result.get('collected')}, bidding={bidding_result.get('collected')}")
        return result

    def get_news_list(
        self,
        category: Optional[str] = None,
        industry_id: Optional[str] = None,
        limit: int = 20,
        offset: int = 0
    ) -> tuple[List[Dict], int]:
        """获取资讯列表，返回 (items, filtered_total)"""
        query = self.db.query(IndustryNews).order_by(IndustryNews.collected_at.desc())

        # 按行业筛选
        if industry_id:
            query = query.filter(IndustryNews.industry_id == industry_id)

        if category:
            query = query.filter(IndustryNews.category == category)

        # 先获取筛选后的总数
        filtered_total = query.count()

        items = query.offset(offset).limit(limit).all()
        return [item.to_dict() for item in items], filtered_total

    def get_bidding_list(
        self,
        notice_type: Optional[str] = None,
        province: Optional[str] = None,
        industry_id: Optional[str] = None,
        limit: int = 20,
        offset: int = 0
    ) -> tuple[List[Dict], int]:
        """获取招投标列表，返回 (items, filtered_total)"""
        from sqlalchemy import or_

        query = self.db.query(BiddingInfo).order_by(BiddingInfo.collected_at.desc())

        # 按行业筛选
        if industry_id:
            query = query.filter(BiddingInfo.industry_id == industry_id)

        if notice_type:
            # 支持模糊匹配：如 "招标" 匹配 "招标公告"、"采购公告" 等
            if notice_type == "招标":
                query = query.filter(
                    or_(
                        BiddingInfo.notice_type.like("%招标%"),
                        BiddingInfo.notice_type.like("%采购%"),
                        BiddingInfo.notice_type.like("%询价%")
                    )
                )
            elif notice_type == "中标":
                query = query.filter(
                    or_(
                        BiddingInfo.notice_type.like("%中标%"),
                        BiddingInfo.notice_type.like("%结果%")
                    )
                )
            else:
                query = query.filter(BiddingInfo.notice_type == notice_type)
        if province:
            query = query.filter(BiddingInfo.province == province)

        # 先获取筛选后的总数
        filtered_total = query.count()

        items = query.offset(offset).limit(limit).all()
        return [item.to_dict() for item in items], filtered_total

    def get_news_stats(self, industry_id: Optional[str] = None) -> Dict[str, Any]:
        """获取资讯统计"""
        from sqlalchemy import func

        query = self.db.query(IndustryNews)
        if industry_id:
            query = query.filter(IndustryNews.industry_id == industry_id)

        total = query.count()

        # 按分类统计
        cat_query = self.db.query(
            IndustryNews.category,
            func.count(IndustryNews.id).label("count")
        )
        if industry_id:
            cat_query = cat_query.filter(IndustryNews.industry_id == industry_id)
        category_stats = cat_query.group_by(IndustryNews.category).all()

        # 获取最近24小时更新数
        yesterday = datetime.utcnow() - timedelta(hours=24)
        recent_query = self.db.query(IndustryNews).filter(
            IndustryNews.collected_at >= yesterday
        )
        if industry_id:
            recent_query = recent_query.filter(IndustryNews.industry_id == industry_id)
        recent_count = recent_query.count()

        return {
            "total": total,
            "recent_24h": recent_count,
            "by_category": {cat: cnt for cat, cnt in category_stats if cat}
        }

    def get_bidding_stats(self, industry_id: Optional[str] = None) -> Dict[str, Any]:
        """获取招投标统计"""
        from sqlalchemy import func

        query = self.db.query(BiddingInfo)
        if industry_id:
            query = query.filter(BiddingInfo.industry_id == industry_id)

        total = query.count()

        # 按类型统计（原始类型）
        type_query = self.db.query(
            BiddingInfo.notice_type,
            func.count(BiddingInfo.id).label("count")
        )
        if industry_id:
            type_query = type_query.filter(BiddingInfo.industry_id == industry_id)
        type_stats = type_query.group_by(BiddingInfo.notice_type).all()

        # 归类统计：中标类 vs 招标类
        bid_count = 0  # 招标
        win_count = 0  # 中标
        raw_by_type = {}
        for notice_type, count in type_stats:
            if notice_type:
                raw_by_type[notice_type] = count
                # 归类
                if "中标" in notice_type or "结果" in notice_type:
                    win_count += count
                elif "招标" in notice_type or "采购" in notice_type or "询价" in notice_type:
                    bid_count += count

        # 按省份统计（前10）
        province_query = self.db.query(
            BiddingInfo.province,
            func.count(BiddingInfo.id).label("count")
        )
        if industry_id:
            province_query = province_query.filter(BiddingInfo.industry_id == industry_id)
        province_stats = province_query.group_by(BiddingInfo.province).order_by(
            func.count(BiddingInfo.id).desc()
        ).limit(10).all()

        return {
            "total": total,
            "by_type": {
                "招标": bid_count,
                "中标": win_count,
                **raw_by_type  # 保留原始类型以便调试
            },
            "by_province": {p: c for p, c in province_stats if p}
        }

    def has_data(self) -> bool:
        """检查是否有数据"""
        news_count = self.db.query(IndustryNews).count()
        bidding_count = self.db.query(BiddingInfo).count()
        return news_count > 0 or bidding_count > 0

    def _extract_date_from_snippet(self, snippet: str) -> Optional[datetime]:
        """从snippet中提取日期"""
        import re

        if not snippet:
            return None

        patterns = [
            r'(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})[日号]?',
            r'(\d{4})\.(\d{1,2})\.(\d{1,2})',
        ]

        for pattern in patterns:
            match = re.search(pattern, snippet)
            if match:
                try:
                    year, month, day = int(match.group(1)), int(match.group(2)), int(match.group(3))
                    if 2020 <= year <= 2030 and 1 <= month <= 12 and 1 <= day <= 31:
                        return datetime(year, month, day)
                except:
                    pass

        return None

    def _parse_datetime(self, date_str: str) -> Optional[datetime]:
        """解析日期时间字符串"""
        if not date_str:
            return None

        formats = [
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%dT%H:%M:%SZ",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%Y-%m-%d",
            "%Y/%m/%d %H:%M:%S",
            "%Y/%m/%d",
            "%Y年%m月%d日",
        ]

        for fmt in formats:
            try:
                return datetime.strptime(date_str.split('.')[0].split('+')[0], fmt)
            except:
                pass

        return None

    def _categorize_news(self, title: str, content: str) -> str:
        """判断资讯分类"""
        text = f"{title} {content}".lower()

        if any(kw in text for kw in ["政策", "通知", "意见", "办法", "规定", "条例", "规划", "法规"]):
            return "政策"
        if any(kw in text for kw in ["纪要", "会议", "座谈", "研讨"]):
            return "纪要"
        if any(kw in text for kw in ["研报", "研究报告", "分析报告", "白皮书", "行业报告"]):
            return "研报"

        return "新闻"

    def _extract_source_from_link(self, link: str) -> str:
        """从链接提取来源"""
        if not link:
            return "未知来源"

        try:
            from urllib.parse import urlparse
            parsed = urlparse(link)
            domain = parsed.netloc

            source_map = {
                "gov.cn": "政府网站",
                "xinhuanet.com": "新华网",
                "people.com.cn": "人民网",
                "mot.gov.cn": "交通运输部",
                "ndrc.gov.cn": "国家发改委",
                "miit.gov.cn": "工信部",
                "163.com": "网易",
                "sohu.com": "搜狐",
                "sina.com": "新浪",
                "qq.com": "腾讯",
                "baidu.com": "百度",
            }

            for key, name in source_map.items():
                if key in domain:
                    return name

            # 返回主域名
            parts = domain.split('.')
            if len(parts) >= 2:
                return '.'.join(parts[-2:])
            return domain
        except:
            return "未知来源"

    def _extract_department(self, title: str, content: str) -> Optional[str]:
        """提取发布部门"""
        text = f"{title} {content}"

        departments = [
            "国务院", "交通运输部", "工信部", "发改委", "科技部",
            "住建部", "公安部", "财政部", "自然资源部", "工业和信息化部",
            "国家发展改革委", "交通运输厅",
        ]

        for dept in departments:
            if dept in text:
                return dept

        return None


def get_news_collection_service(db: Session) -> NewsCollectionService:
    """获取服务实例"""
    return NewsCollectionService(db)
