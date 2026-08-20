# Copyright © 2026 深圳市深维智见教育科技有限公司 版权所有
# 未经授权，禁止转售或仿制。

"""
DocMind 文档智能解析服务

修复要点（问题1）：
旧实现的 submit_job() 在阿里云返回业务错误时静默返回 None：
    HTTP 200, body = {"Code": "DocMindServiceNotOpen",
                      "Message": "You have not open the docMind service.",
                      "RequestId": "..."}
因为 HTTP 状态码是 200，SDK 不会抛异常，`response.body.data` 为 None，
于是上层只能得到 "文档提交失败" 这种无信息量的提示。

现在：所有 DocMind 业务错误都会抛出 DocMindError，并带上 Code / Message / RequestId。
"""
import hashlib
import logging
import os
import time
from typing import Any, Dict, List, Optional

from alibabacloud_docmind_api20220711 import models as docmind_models
from alibabacloud_docmind_api20220711.client import Client as DocMindClient
from alibabacloud_tea_openapi import models as open_api_models
from alibabacloud_tea_util import models as util_models

logger = logging.getLogger(__name__)

# 占位符配置（.env.example 里的默认值）不应被当作有效配置
_PLACEHOLDER_PREFIXES = ("your-", "your_", "<", "xxx", "changeme")


class DocMindError(Exception):
    """DocMind 调用失败（携带阿里云返回的真实 Code/Message）"""

    def __init__(self, message: str, code: Optional[str] = None, request_id: Optional[str] = None):
        self.code = code
        self.request_id = request_id
        detail = message
        if code:
            detail = f"{code}: {message}"
        if request_id:
            detail = f"{detail} (RequestId={request_id})"
        super().__init__(detail)


def _is_placeholder(value: Optional[str]) -> bool:
    if not value:
        return True
    lowered = value.strip().lower()
    return any(lowered.startswith(p) for p in _PLACEHOLDER_PREFIXES)


def is_docmind_configured() -> bool:
    """DocMind 是否配置了有效的 AK/SK"""
    return not _is_placeholder(os.getenv("DOCMIND_ACCESS_KEY_ID")) and not _is_placeholder(
        os.getenv("DOCMIND_ACCESS_KEY_SECRET")
    )


def _extract_error(body: Any) -> Optional[DocMindError]:
    """从响应 body 中提取业务错误（Code 非空即为错误）"""
    if body is None:
        return DocMindError("DocMind 未返回响应体")

    code = getattr(body, "code", None)
    message = getattr(body, "message", None)
    request_id = getattr(body, "request_id", None)

    if not code and hasattr(body, "to_map"):
        try:
            mapped = body.to_map() or {}
            code = mapped.get("Code") or mapped.get("code")
            message = mapped.get("Message") or mapped.get("message")
            request_id = mapped.get("RequestId") or mapped.get("requestId")
        except Exception:  # pragma: no cover
            pass

    if code:
        return DocMindError(message or "DocMind 返回业务错误", code=str(code), request_id=request_id)
    return None


class DocMindService:
    """DocMind 文档解析服务"""

    def __init__(self):
        self.access_key_id = os.getenv("DOCMIND_ACCESS_KEY_ID")
        self.access_key_secret = os.getenv("DOCMIND_ACCESS_KEY_SECRET")
        self.endpoint = os.getenv("DOCMIND_ENDPOINT", "docmind-api.cn-hangzhou.aliyuncs.com")

        if not is_docmind_configured():
            raise DocMindError(
                "DocMind 未配置有效的 DOCMIND_ACCESS_KEY_ID / DOCMIND_ACCESS_KEY_SECRET"
            )

        self.client = self._create_client()

    def _create_client(self) -> DocMindClient:
        """创建 DocMind 客户端"""
        config = open_api_models.Config(
            access_key_id=self.access_key_id,
            access_key_secret=self.access_key_secret,
        )
        config.endpoint = self.endpoint
        return DocMindClient(config)

    def submit_job(self, file_path: str, file_name: str) -> str:
        """
        提交文档解析任务

        Returns:
            任务 ID

        Raises:
            DocMindError: 提交失败（含服务未开通、无权限、格式不支持等真实原因）
        """
        extension = file_name.split(".")[-1] if "." in file_name else None
        file_object = open(file_path, "rb")
        try:
            request = docmind_models.SubmitDocParserJobAdvanceRequest(
                file_url_object=file_object,
                file_name=file_name,
                file_name_extension=extension,
            )
            runtime = util_models.RuntimeOptions()
            response = self.client.submit_doc_parser_job_advance(request, runtime)
        except DocMindError:
            raise
        except Exception as e:
            raise DocMindError(f"提交任务异常: {type(e).__name__}: {e}") from e
        finally:
            try:
                file_object.close()
            except Exception:  # pragma: no cover
                pass

        body = getattr(response, "body", None)
        error = _extract_error(body)
        if error:
            logger.error("DocMind 提交任务失败: %s", error)
            raise error

        data = getattr(body, "data", None)
        task_id = getattr(data, "id", None) if data else None
        if not task_id:
            raise DocMindError("DocMind 未返回任务ID（响应体缺少 data.id）")

        logger.info("DocMind 任务已提交: task_id=%s, file=%s", task_id, file_name)
        return task_id

    def query_status(self, task_id: str) -> Dict:
        """
        查询任务状态

        Raises:
            DocMindError: 查询失败
        """
        try:
            request = docmind_models.QueryDocParserStatusRequest(id=task_id)
            response = self.client.query_doc_parser_status(request)
        except Exception as e:
            raise DocMindError(f"查询状态异常: {type(e).__name__}: {e}") from e

        body = getattr(response, "body", None)
        error = _extract_error(body)
        if error:
            raise error

        data = getattr(body, "data", None)
        if not data:
            raise DocMindError("查询状态失败：响应体缺少 data")
        return data.to_map()

    def wait_for_completion(self, task_id: str, poll_interval: int = 5, max_wait: int = 300) -> bool:
        """
        等待任务完成

        Raises:
            DocMindError: 任务失败或超时
        """
        logger.info("开始轮询 DocMind 任务状态: %s", task_id)
        start_time = time.time()

        while time.time() - start_time < max_wait:
            status_data = self.query_status(task_id)
            status = str(status_data.get("Status", "")).lower()
            logger.info("DocMind 任务 %s 状态: %s", task_id, status)

            if status == "success":
                return True
            if status == "failed":
                raise DocMindError(
                    f"DocMind 解析任务失败: {status_data.get('Message') or status_data}"
                )
            time.sleep(poll_interval)

        raise DocMindError(f"DocMind 解析任务超时（>{max_wait}s）: task_id={task_id}")

    def get_result(self, task_id: str, layout_num: int = 0, layout_step_size: int = 10) -> Optional[Any]:
        """获取文档解析结果（支持增量获取）"""
        try:
            request = docmind_models.GetDocParserResultRequest(
                id=task_id,
                layout_step_size=layout_step_size,
                layout_num=layout_num,
            )
            response = self.client.get_doc_parser_result(request)
        except Exception as e:
            raise DocMindError(f"获取结果异常: {type(e).__name__}: {e}") from e

        body = getattr(response, "body", None)
        error = _extract_error(body)
        if error:
            raise error
        return getattr(body, "data", None)

    def collect_all_results(self, task_id: str, layout_step_size: int = 10) -> str:
        """收集所有解析结果，拼接为完整文本"""
        all_text = ""
        layout_num = 0

        while True:
            result_data = self.get_result(task_id, layout_num, layout_step_size)
            if not result_data:
                break

            layouts = None
            if hasattr(result_data, "layouts"):
                layouts = result_data.layouts
            elif isinstance(result_data, dict):
                layouts = result_data.get("layouts", [])

            if not layouts:
                break

            logger.info("DocMind 获取到 %s 个布局块 (从 %s 开始)", len(layouts), layout_num)

            for layout in layouts:
                if hasattr(layout, "markdown_content") and layout.markdown_content:
                    all_text += layout.markdown_content + "\n"
                elif hasattr(layout, "markdownContent") and layout.markdownContent:
                    all_text += layout.markdownContent + "\n"
                elif isinstance(layout, dict):
                    if layout.get("markdownContent"):
                        all_text += layout["markdownContent"] + "\n"
                    elif layout.get("text"):
                        all_text += layout["text"] + "\n"
                elif hasattr(layout, "text") and layout.text:
                    all_text += layout.text + "\n"

            layout_num += len(layouts)
            if len(layouts) < layout_step_size:
                break

        return all_text


def chunk_text(text: str, chunk_size: int = 500, overlap: int = 50) -> List[str]:
    """文本切分（实现已统一到 document_parser_service，此处保留兼容入口）"""
    from service.document_parser_service import chunk_text as _chunk_text

    return _chunk_text(text, chunk_size=chunk_size, overlap=overlap)


def process_document_with_docmind(
    file_path: str,
    file_name: str,
    index_name: str,
    chunk_size: int = 500,
) -> Dict[str, Any]:
    """
    处理文档：解析 -> 切分 -> 向量化 -> 写入 Milvus

    注意：函数名保留向后兼容，内部已改为「本地解析优先，DocMind 兜底」。

    Args:
        file_path: 文件路径
        file_name: 文件名
        index_name: Milvus 集合名（必须是合法 ASCII 名，见 collection_naming）
        chunk_size: 切片大小

    Returns:
        {"success": bool, "message": str, "document_count": int, "parser": str}
    """
    from service.document_parser_service import (
        DocumentParseError,
        chunk_text as split_text,
        parse_document,
    )
    from service.embedding_service import EmbeddingError, generate_embeddings
    from service.milvus_service import get_milvus_service

    result: Dict[str, Any] = {
        "success": False,
        "message": "",
        "document_count": 0,
        "parser": "",
    }

    try:
        logger.info("开始处理文档: %s -> 集合 %s", file_name, index_name)

        # 1. 解析（本地优先，必要时 DocMind）
        try:
            parsed = parse_document(file_path, file_name)
        except DocumentParseError as e:
            result["message"] = f"文档解析失败: {e}"
            logger.error("文档解析失败: %s", e)
            return result

        result["parser"] = parsed.parser
        text = parsed.text
        if not text or not text.strip():
            result["message"] = f"文档内容为空（解析器: {parsed.parser}）"
            return result

        logger.info("解析完成: parser=%s, 文本长度=%s", parsed.parser, len(text))

        # 2. 切分
        chunks = split_text(text, chunk_size=chunk_size)
        if not chunks:
            result["message"] = "文档切分后无有效内容"
            return result
        logger.info("文档切分完成，共 %s 个切片", len(chunks))

        # 3. 向量化（失败会抛出带真实 API 错误的 EmbeddingError）
        try:
            embeddings = generate_embeddings(chunks)
        except EmbeddingError as e:
            result["message"] = f"向量生成失败: {e}"
            logger.error("向量生成失败: %s", e)
            return result

        logger.info("向量生成完成，维度: %s", len(embeddings[0]))

        # 4. 构建并写入 Milvus
        doc_id = hashlib.md5(file_name.encode()).hexdigest()
        documents = []
        for i, (chunk, embedding) in enumerate(zip(chunks, embeddings)):
            chunk_id = hashlib.md5(f"{index_name}_{file_name}_{i}".encode()).hexdigest()
            documents.append(
                {
                    "id": chunk_id,
                    "doc_id": doc_id,
                    "kb_id": index_name,
                    "filename": file_name,
                    "content": chunk,
                    "chunk_index": i,
                    "vector": embedding,
                }
            )

        milvus = get_milvus_service()
        milvus.insert_documents(index_name, documents)

        result["success"] = True
        result["document_count"] = len(documents)
        result["message"] = f"成功处理 {len(documents)} 个切片（解析器: {parsed.parser}）"
        logger.info("文档处理完成: %s", result["message"])

    except Exception as e:
        result["message"] = f"处理失败: {type(e).__name__}: {e}"
        logger.exception("文档处理异常: %s", e)

    return result
