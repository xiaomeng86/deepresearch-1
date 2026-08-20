# Copyright © 2026 深圳市深维智见教育科技有限公司 版权所有
# 未经授权，禁止转售或仿制。

"""
Milvus 集合命名规则（修复问题1的第二个阻断点）

Milvus 约束：集合名只能包含字母、数字、下划线，且必须以字母或下划线开头。
旧实现 `f"kb_{kb.name}".lower().replace(" ", "_")` 在知识库名含中文时会生成
`kb_ai硬件`，Milvus 直接拒绝：
    MilvusException: (code=1100, message=Invalid collection name: kb_ai硬件.
    collection name can only contain numbers, letters and underscores)

本模块统一命名：优先使用知识库 UUID（天然 ASCII、稳定、不随改名变化），
并保留旧命名解析能力，便于历史数据兼容。
"""

import hashlib
import re
from typing import List, Optional
from uuid import UUID

COLLECTION_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
MAX_COLLECTION_NAME_LENGTH = 255


def is_valid_collection_name(name: str) -> bool:
    """是否符合 Milvus 集合命名规范"""
    if not name or len(name) > MAX_COLLECTION_NAME_LENGTH:
        return False
    return bool(COLLECTION_NAME_PATTERN.match(name))


def sanitize_collection_name(raw: str, prefix: str = "kb_") -> str:
    """
    将任意字符串转换成合法集合名。

    非 ASCII 字符（如中文）会被替换为下划线，并追加内容哈希以避免不同名字冲突。
    """
    raw = (raw or "").strip()
    ascii_part = re.sub(r"[^A-Za-z0-9_]", "_", raw).strip("_").lower()
    digest = hashlib.md5(raw.encode("utf-8")).hexdigest()[:8]

    if ascii_part:
        name = f"{prefix}{ascii_part}_{digest}"
    else:
        name = f"{prefix}{digest}"

    if not COLLECTION_NAME_PATTERN.match(name):
        name = f"{prefix}{digest}"
    return name[:MAX_COLLECTION_NAME_LENGTH]


def kb_collection_name(kb_id, kb_name: Optional[str] = None) -> str:
    """
    知识库对应的 Milvus 集合名（首选命名）

    Args:
        kb_id: 知识库 UUID（str 或 UUID）
        kb_name: 知识库名称，仅在 kb_id 缺失时用于兜底

    Returns:
        合法的集合名，如 kb_d90f4de06b6a470a81a6262f71a84c49
    """
    if kb_id:
        try:
            return f"kb_{UUID(str(kb_id)).hex}"
        except (ValueError, AttributeError, TypeError):
            return sanitize_collection_name(str(kb_id))

    if kb_name:
        return sanitize_collection_name(kb_name)

    raise ValueError("kb_id 与 kb_name 不能同时为空")


def legacy_kb_collection_name(kb_name: Optional[str]) -> Optional[str]:
    """
    旧版命名（f"kb_{name}".lower().replace(" ", "_")）

    仅当结果合法时返回，用于兼容历史写入的数据；中文名会返回 None。
    """
    if not kb_name:
        return None
    legacy = f"kb_{kb_name}".lower().replace(" ", "_")
    return legacy if is_valid_collection_name(legacy) else None


def candidate_collection_names(kb_id=None, kb_name: Optional[str] = None) -> List[str]:
    """
    返回所有可能承载该知识库数据的集合名（按优先级）

    读取路径（检索/查看切片）应遍历这些候选名，兼容历史数据。
    """
    names: List[str] = []

    if kb_id:
        try:
            names.append(kb_collection_name(kb_id))
        except ValueError:
            pass

    legacy = legacy_kb_collection_name(kb_name)
    if legacy and legacy not in names:
        names.append(legacy)

    if kb_name:
        sanitized = sanitize_collection_name(kb_name)
        if sanitized not in names:
            names.append(sanitized)

    return names
