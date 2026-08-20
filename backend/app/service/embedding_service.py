# Copyright © 2026 深圳市深维智见教育科技有限公司 版权所有
# 未经授权，禁止转售或仿制。

"""
Embedding 服务 - 使用阿里 DashScope

功能：
1. generate_embedding - 使用 text-embedding-v4 生成向量
2. rerank_similarity - 使用 DashScope Rerank 重排序
"""

import logging
import os
from typing import List, Optional, Tuple
import numpy as np
from openai import OpenAI
from llama_index.core.data_structs import Node
from llama_index.core.schema import NodeWithScore
from llama_index.postprocessor.dashscope_rerank import DashScopeRerank

from dotenv import load_dotenv
load_dotenv()

logger = logging.getLogger(__name__)


class EmbeddingError(Exception):
    """Embedding 生成失败（携带底层 API 的真实错误信息）"""


def generate_embedding(
    text: str | List[str],
    api_key: str = None,
    base_url: str = None,
    model_name: str = "text-embedding-v4",
    dimensions: int = 1024,
    encoding_format: str = "float",
    max_batch_size: int = 10
) -> Optional[List[float] | List[List[float]]]:
    """
    生成文本的向量嵌入（使用阿里 text-embedding-v4）

    Args:
        text: 单个文本或文本列表
        api_key: API密钥（默认从环境变量获取）
        base_url: API基础URL（默认从环境变量获取）
        model_name: 模型名称
        dimensions: 向量维度（默认1024）
        encoding_format: 编码格式
        max_batch_size: 最大批量大小（阿里云限制为10）

    Returns:
        单个文本时返回向量，文本列表时返回向量列表
    """
    api_key = api_key or os.getenv("DASHSCOPE_API_KEY")
    base_url = base_url or os.getenv("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")

    if not api_key:
        print("错误: 缺少 DASHSCOPE_API_KEY 环境变量")
        return None

    try:
        client = OpenAI(api_key=api_key, base_url=base_url)
    except Exception as e:
        print(f"初始化 OpenAI 客户端失败: {e}")
        return None

    # 单个文本
    if isinstance(text, str):
        try:
            completion = client.embeddings.create(
                model=model_name,
                input=text,
                dimensions=dimensions,
                encoding_format=encoding_format
            )
            return completion.data[0].embedding
        except Exception as e:
            print(f"Embedding 请求失败: {e}")
            return None

    # 文本列表 - 分批处理
    if isinstance(text, list):
        all_embeddings = []

        for i in range(0, len(text), max_batch_size):
            batch = text[i:i + max_batch_size]

            try:
                completion = client.embeddings.create(
                    model=model_name,
                    input=batch,
                    dimensions=dimensions,
                    encoding_format=encoding_format
                )
                batch_embeddings = [item.embedding for item in completion.data]
                all_embeddings.extend(batch_embeddings)
            except Exception as e:
                print(f"Embedding 批量请求失败 (batch {i // max_batch_size + 1}): {e}")
                all_embeddings.extend([None] * len(batch))

        return all_embeddings

    return None


def generate_embeddings(
    texts: List[str],
    api_key: str = None,
    base_url: str = None,
    model_name: str = "text-embedding-v4",
    dimensions: int = 1024,
    encoding_format: str = "float",
    max_batch_size: int = 10,
) -> List[List[float]]:
    """
    批量生成向量（严格模式）

    与 generate_embedding 的区别：任何一批失败都会抛出 EmbeddingError，
    并附带底层 API 的真实错误信息，而不是静默塞入 None 让上层在
    `len(embeddings[0])` 处炸成 "object of type 'NoneType' has no len()"。

    Args:
        texts: 文本列表

    Returns:
        与 texts 等长的向量列表

    Raises:
        EmbeddingError: 缺少配置、客户端初始化失败或 API 调用失败
    """
    if not texts:
        return []

    api_key = api_key or os.getenv("DASHSCOPE_API_KEY")
    base_url = base_url or os.getenv(
        "DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"
    )
    model_name = os.getenv("EMBEDDING_MODEL", model_name)

    if not api_key:
        raise EmbeddingError("缺少 DASHSCOPE_API_KEY 环境变量")

    try:
        client = OpenAI(api_key=api_key, base_url=base_url)
    except Exception as e:
        raise EmbeddingError(f"初始化 Embedding 客户端失败: {type(e).__name__}: {e}") from e

    all_embeddings: List[List[float]] = []
    total_batches = (len(texts) + max_batch_size - 1) // max_batch_size

    for i in range(0, len(texts), max_batch_size):
        batch = texts[i:i + max_batch_size]
        batch_no = i // max_batch_size + 1
        try:
            completion = client.embeddings.create(
                model=model_name,
                input=batch,
                dimensions=dimensions,
                encoding_format=encoding_format,
            )
        except Exception as e:
            raise EmbeddingError(
                f"Embedding 请求失败 (batch {batch_no}/{total_batches}, model={model_name}, "
                f"base_url={base_url}): {type(e).__name__}: {e}"
            ) from e

        batch_embeddings = [item.embedding for item in completion.data]
        if len(batch_embeddings) != len(batch):
            raise EmbeddingError(
                f"Embedding 数量不匹配 (batch {batch_no}): 期望 {len(batch)} 个，"
                f"实际 {len(batch_embeddings)} 个"
            )
        all_embeddings.extend(batch_embeddings)

    if len(all_embeddings) != len(texts):
        raise EmbeddingError(
            f"Embedding 数量不匹配: 期望 {len(texts)} 个，实际 {len(all_embeddings)} 个"
        )

    logger.info("生成 %s 个向量，维度 %s", len(all_embeddings), len(all_embeddings[0]))
    return all_embeddings


def rerank_similarity(
    query: str,
    texts: List[str],
    top_n: int = None
) -> Tuple[np.ndarray, None]:
    """
    使用 DashScope Rerank 对文本进行重排序

    Args:
        query: 查询文本
        texts: 待排序的文本列表
        top_n: 返回前N个结果（默认返回全部）

    Returns:
        (scores, None) - 分数数组和占位符
    """
    api_key = os.getenv("DASHSCOPE_API_KEY")

    if not api_key:
        print("错误: 缺少 DASHSCOPE_API_KEY 环境变量")
        return np.array([]), None

    top_n = top_n or len(texts)

    # 创建节点列表
    nodes = [NodeWithScore(node=Node(text=text), score=1.0) for text in texts]

    # 初始化 DashScopeRerank
    dashscope_rerank = DashScopeRerank(top_n=top_n, api_key=api_key)

    # 执行重排序
    results = dashscope_rerank.postprocess_nodes(nodes, query_str=query)

    # 提取分数
    scores = np.array([res.score for res in results])

    return scores, None
