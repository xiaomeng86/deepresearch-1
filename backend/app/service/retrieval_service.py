# Copyright © 2026 深圳市深维智见教育科技有限公司 版权所有
# 未经授权，禁止转售或仿制。

"""
知识库检索服务 - 基于 Milvus

功能：
1. retrieve_content - 从指定集合检索内容
2. retrieve_from_knowledge_base - 按知识库名称/ID 检索（自动兼容新旧集合命名）
3. retrieve_from_knowledge_bases - 跨多个知识库检索（用于聊天 RAG）
"""

import logging
from typing import Any, Dict, Iterable, List, Optional

from service.collection_naming import candidate_collection_names
from service.embedding_service import EmbeddingError, generate_embeddings
from service.milvus_service import get_milvus_service

logger = logging.getLogger(__name__)


def retrieve_content(
    indexNames: str,
    question: str,
    top_k: int = 5,
    kb_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    检索相关内容

    Args:
        indexNames: 集合名称（知识库索引）
        question: 查询问题
        top_k: 返回结果数量
        kb_id: 知识库ID（可选过滤）

    Returns:
        检索结果列表
    """
    try:
        query_vector = _embed_question(question)
        if query_vector is None:
            return []

        milvus = get_milvus_service()
        results = milvus.search(
            collection_name=indexNames,
            query_vector=query_vector,
            top_k=top_k,
            kb_id=kb_id,
        )
        return _format_results(results)

    except Exception as e:
        logger.error("检索错误 (collection=%s): %s", indexNames, e, exc_info=True)
        return []


def retrieve_from_knowledge_base(
    kb_name: str,
    question: str,
    top_k: int = 5,
    kb_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    从知识库检索内容

    Args:
        kb_name: 知识库名称
        question: 查询问题
        top_k: 返回结果数量
        kb_id: 知识库 UUID（推荐传入；中文知识库名只能靠 ID 定位集合）

    Returns:
        检索结果列表
    """
    try:
        query_vector = _embed_question(question)
        if query_vector is None:
            return []
    except Exception as e:
        logger.error("生成查询向量失败: %s", e)
        return []

    milvus = get_milvus_service()
    for collection_name in candidate_collection_names(kb_id, kb_name):
        try:
            results = milvus.search(
                collection_name=collection_name,
                query_vector=query_vector,
                top_k=top_k,
            )
        except Exception as e:
            logger.warning("集合 %s 检索失败: %s", collection_name, e)
            continue

        if results:
            logger.info("知识库 %s 命中 %s 条 (collection=%s)", kb_name, len(results), collection_name)
            return _format_results(results)

    return []


def retrieve_from_knowledge_bases(
    knowledge_bases: Iterable[Dict[str, Any]],
    question: str,
    top_k: int = 5,
) -> List[Dict[str, Any]]:
    """
    跨多个知识库检索（聊天 RAG 用）

    Args:
        knowledge_bases: [{"id": kb_uuid, "name": kb_name}, ...]
        question: 查询问题
        top_k: 每个知识库返回的最大条数

    Returns:
        合并后按分数降序排列的结果
    """
    try:
        query_vector = _embed_question(question)
        if query_vector is None:
            return []
    except Exception as e:
        logger.error("生成查询向量失败: %s", e)
        return []

    milvus = get_milvus_service()
    merged: List[Dict[str, Any]] = []

    for kb in knowledge_bases:
        kb_id = kb.get("id")
        kb_name = kb.get("name")
        for collection_name in candidate_collection_names(kb_id, kb_name):
            try:
                results = milvus.search(
                    collection_name=collection_name,
                    query_vector=query_vector,
                    top_k=top_k,
                )
            except Exception as e:
                logger.warning("集合 %s 检索失败: %s", collection_name, e)
                continue

            if results:
                for item in results:
                    item["kb_name"] = kb_name
                merged.extend(results)
                break

    merged.sort(key=lambda r: r.get("score", 0), reverse=True)
    return _format_results(merged[: top_k * 2])


def _embed_question(question: str) -> Optional[List[float]]:
    """生成查询向量；失败返回 None 并记录真实原因"""
    try:
        vectors = generate_embeddings([question])
    except EmbeddingError as e:
        logger.error("生成查询向量失败: %s", e)
        return None
    if not vectors:
        return None
    return vectors[0]


def _format_results(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """统一检索结果格式"""
    extracted_data = []
    for i, result in enumerate(results, start=1):
        extracted_data.append(
            {
                "id": i,
                "document_id": result.get("doc_id", "N/A"),
                "document_name": result.get("filename", "N/A"),
                "content_with_weight": result.get("content", ""),
                "score": result.get("score", 0),
                "kb_name": result.get("kb_name"),
            }
        )
    return extracted_data
