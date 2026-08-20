# Copyright © 2026 深圳市深维智见教育科技有限公司 版权所有
# 未经授权，禁止转售或仿制。

"""知识库管理路由"""
import logging
import os
import shutil
from typing import List
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File, BackgroundTasks
from sqlalchemy.orm import Session

from core.database import get_db
from models.knowledge import KnowledgeBase, Document
from models.user import User
from router.auth_router import get_current_user_required
from schemas.knowledge import (
    KnowledgeBaseCreate,
    KnowledgeBaseUpdate,
    KnowledgeBaseResponse,
    KnowledgeBaseWithDocuments,
    DocumentResponse,
    DocumentUploadResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/knowledge-bases", tags=["知识库管理"])

# 文件上传目录（可通过环境变量覆盖；重启不丢失才能支持失败重试）
UPLOAD_DIR = os.getenv("KNOWLEDGE_UPLOAD_DIR", "/tmp/knowledge_uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

# 支持的文件类型
ALLOWED_EXTENSIONS = {
    # 文档类型
    '.pdf', '.docx', '.doc', '.txt', '.md', '.html', '.xlsx', '.xls', '.pptx', '.ppt',
    # 图片类型
    '.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp',
    # 代码/数据类型
    '.py', '.js', '.ts', '.json', '.yaml', '.yml', '.xml', '.csv',
}


def get_file_extension(filename: str) -> str:
    """获取文件扩展名"""
    return os.path.splitext(filename)[1].lower()


def kb_to_response(kb: KnowledgeBase) -> KnowledgeBaseResponse:
    """将知识库模型转换为响应"""
    return KnowledgeBaseResponse(
        id=str(kb.id),
        name=kb.name,
        description=kb.description,
        document_count=kb.document_count or 0,
        created_at=kb.created_at,
        updated_at=kb.updated_at,
    )


def doc_to_response(doc: Document) -> DocumentResponse:
    """将文档模型转换为响应"""
    return DocumentResponse(
        id=str(doc.id),
        knowledge_base_id=str(doc.knowledge_base_id),
        filename=doc.filename,
        file_type=doc.file_type,
        file_size=doc.file_size,
        status=doc.status,
        chunk_count=doc.chunk_count or 0,
        error_message=doc.error_message,
        created_at=doc.created_at,
        updated_at=doc.updated_at,
    )


async def process_document(document_id: str, file_path: str, kb_id: str, kb_name: str, db_session_factory):
    """
    后台处理文档：解析 -> 切分 -> 向量化 -> 写入 Milvus

    修复点：
    1. 所有 import 与逻辑都在 try 内，import 失败也会把文档标记为 failed（旧代码会让任务静默崩溃、
       文档永远停留在 pending）
    2. 集合名由知识库 UUID 生成，避免中文知识库名导致 Milvus "Invalid collection name"
    3. 失败时写入真实异常类型与信息，并打印完整堆栈到后端日志
    4. 不再删除源文件，便于失败后重试（reprocess 接口）
    """
    db = db_session_factory()
    doc = None
    try:
        doc = db.query(Document).filter(Document.id == document_id).first()
        if not doc:
            logger.error("[process_document] 文档记录不存在: %s", document_id)
            return

        doc.status = "processing"
        doc.error_message = None
        db.commit()

        from service.collection_naming import kb_collection_name
        from service.docmind_service import process_document_with_docmind

        collection_name = kb_collection_name(kb_id, kb_name)
        logger.info(
            "[process_document] 开始处理 doc=%s file=%s kb=%s collection=%s",
            document_id, doc.filename, kb_name, collection_name,
        )

        result = process_document_with_docmind(
            file_path=file_path,
            file_name=doc.filename,
            index_name=collection_name,
        )

        if result.get("success"):
            doc.status = "completed"
            doc.chunk_count = result.get("document_count", 0)
            doc.error_message = None
            logger.info("[process_document] 处理成功 doc=%s %s", document_id, result.get("message"))
        else:
            doc.status = "failed"
            doc.error_message = result.get("message") or "未知错误"
            logger.error("[process_document] 处理失败 doc=%s: %s", document_id, doc.error_message)

        db.commit()

    except Exception as e:
        logger.exception("[process_document] 处理异常 doc=%s: %s", document_id, e)
        try:
            if doc is None:
                doc = db.query(Document).filter(Document.id == document_id).first()
            if doc:
                doc.status = "failed"
                doc.error_message = f"{type(e).__name__}: {e}"
                db.commit()
        except Exception:
            logger.exception("[process_document] 写入失败状态时再次异常 doc=%s", document_id)
    finally:
        db.close()


@router.get("", response_model=List[KnowledgeBaseResponse])
async def get_knowledge_bases(
    current_user: User = Depends(get_current_user_required),
    db: Session = Depends(get_db),
):
    """获取用户的知识库列表"""
    kbs = db.query(KnowledgeBase).filter(
        KnowledgeBase.user_id == current_user.id
    ).order_by(KnowledgeBase.updated_at.desc()).all()

    return [kb_to_response(kb) for kb in kbs]


@router.post("", response_model=KnowledgeBaseResponse, status_code=status.HTTP_201_CREATED)
async def create_knowledge_base(
    kb_data: KnowledgeBaseCreate,
    current_user: User = Depends(get_current_user_required),
    db: Session = Depends(get_db),
):
    """创建知识库"""
    # 检查是否已存在同名知识库
    existing = db.query(KnowledgeBase).filter(
        KnowledgeBase.user_id == current_user.id,
        KnowledgeBase.name == kb_data.name
    ).first()

    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="已存在同名知识库"
        )

    kb = KnowledgeBase(
        user_id=current_user.id,
        name=kb_data.name,
        description=kb_data.description,
    )
    db.add(kb)
    db.commit()
    db.refresh(kb)

    return kb_to_response(kb)


@router.get("/{kb_id}", response_model=KnowledgeBaseWithDocuments)
async def get_knowledge_base(
    kb_id: str,
    current_user: User = Depends(get_current_user_required),
    db: Session = Depends(get_db),
):
    """获取知识库详情（包含文档列表）"""
    try:
        kb_uuid = UUID(kb_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="无效的知识库ID格式"
        )

    kb = db.query(KnowledgeBase).filter(
        KnowledgeBase.id == kb_uuid,
        KnowledgeBase.user_id == current_user.id
    ).first()

    if not kb:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="知识库不存在"
        )

    documents = db.query(Document).filter(
        Document.knowledge_base_id == kb.id
    ).order_by(Document.created_at.desc()).all()

    return KnowledgeBaseWithDocuments(
        id=str(kb.id),
        name=kb.name,
        description=kb.description,
        document_count=kb.document_count or 0,
        created_at=kb.created_at,
        updated_at=kb.updated_at,
        documents=[doc_to_response(doc) for doc in documents],
    )


@router.put("/{kb_id}", response_model=KnowledgeBaseResponse)
async def update_knowledge_base(
    kb_id: str,
    kb_data: KnowledgeBaseUpdate,
    current_user: User = Depends(get_current_user_required),
    db: Session = Depends(get_db),
):
    """更新知识库"""
    try:
        kb_uuid = UUID(kb_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="无效的知识库ID格式"
        )

    kb = db.query(KnowledgeBase).filter(
        KnowledgeBase.id == kb_uuid,
        KnowledgeBase.user_id == current_user.id
    ).first()

    if not kb:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="知识库不存在"
        )

    if kb_data.name is not None:
        # 检查是否与其他知识库重名
        existing = db.query(KnowledgeBase).filter(
            KnowledgeBase.user_id == current_user.id,
            KnowledgeBase.name == kb_data.name,
            KnowledgeBase.id != kb_uuid
        ).first()
        if existing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="已存在同名知识库"
            )
        kb.name = kb_data.name

    if kb_data.description is not None:
        kb.description = kb_data.description

    db.commit()
    db.refresh(kb)

    return kb_to_response(kb)


@router.delete("/{kb_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_knowledge_base(
    kb_id: str,
    current_user: User = Depends(get_current_user_required),
    db: Session = Depends(get_db),
):
    """删除知识库"""
    try:
        kb_uuid = UUID(kb_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="无效的知识库ID格式"
        )

    kb = db.query(KnowledgeBase).filter(
        KnowledgeBase.id == kb_uuid,
        KnowledgeBase.user_id == current_user.id
    ).first()

    if not kb:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="知识库不存在"
        )

    # 删除对应的 Milvus 集合（忽略失败，不阻断数据库删除）
    try:
        from service.collection_naming import candidate_collection_names
        from service.milvus_service import get_milvus_service

        milvus = get_milvus_service()
        for collection_name in candidate_collection_names(kb.id, kb.name):
            milvus.delete_collection(collection_name)
    except Exception as e:
        logger.warning("[delete_knowledge_base] 删除集合失败（忽略）: %s", e)

    db.delete(kb)
    db.commit()
    return None


@router.post("/{kb_id}/documents", response_model=DocumentUploadResponse)
async def upload_document(
    kb_id: str,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user_required),
    db: Session = Depends(get_db),
):
    """上传文档到知识库"""
    try:
        kb_uuid = UUID(kb_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="无效的知识库ID格式"
        )

    # 验证知识库存在
    kb = db.query(KnowledgeBase).filter(
        KnowledgeBase.id == kb_uuid,
        KnowledgeBase.user_id == current_user.id
    ).first()

    if not kb:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="知识库不存在"
        )

    # 验证文件类型
    ext = get_file_extension(file.filename)
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"不支持的文件类型: {ext}，支持的类型: {', '.join(ALLOWED_EXTENSIONS)}"
        )

    # 保存文件到临时目录
    file_path = os.path.join(UPLOAD_DIR, f"{kb_uuid}_{file.filename}")
    try:
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"文件保存失败: {str(e)}"
        )

    # 获取文件大小
    file_size = os.path.getsize(file_path)

    # 创建文档记录
    doc = Document(
        knowledge_base_id=kb_uuid,
        user_id=current_user.id,
        filename=file.filename,
        file_type=ext[1:] if ext else None,  # 去掉点
        file_size=file_size,
        file_path=file_path,
        status="pending",
    )
    db.add(doc)

    # 更新知识库文档计数
    kb.document_count = (kb.document_count or 0) + 1

    db.commit()
    db.refresh(doc)

    # 获取数据库会话工厂
    from core.database import SessionLocal

    # 在后台处理文档
    background_tasks.add_task(
        process_document,
        str(doc.id),
        file_path,
        str(kb_uuid),
        kb.name,
        SessionLocal
    )

    return DocumentUploadResponse(
        id=str(doc.id),
        filename=doc.filename,
        process_status="pending",
        message="文档已上传，正在后台处理中"
    )


@router.post("/{kb_id}/documents/{doc_id}/reprocess", response_model=DocumentUploadResponse)
async def reprocess_document(
    kb_id: str,
    doc_id: str,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user_required),
    db: Session = Depends(get_db),
):
    """重新处理文档（用于修复失败文档，无需重新上传）"""
    try:
        kb_uuid = UUID(kb_id)
        doc_uuid = UUID(doc_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="无效的ID格式"
        )

    kb = db.query(KnowledgeBase).filter(
        KnowledgeBase.id == kb_uuid,
        KnowledgeBase.user_id == current_user.id
    ).first()
    if not kb:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="知识库不存在")

    doc = db.query(Document).filter(
        Document.id == doc_uuid,
        Document.knowledge_base_id == kb_uuid
    ).first()
    if not doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="文档不存在")

    if not doc.file_path or not os.path.exists(doc.file_path):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="源文件已不存在，请重新上传该文档"
        )

    doc.status = "pending"
    doc.error_message = None
    db.commit()
    db.refresh(doc)

    from core.database import SessionLocal

    background_tasks.add_task(
        process_document,
        str(doc.id),
        doc.file_path,
        str(kb_uuid),
        kb.name,
        SessionLocal
    )

    return DocumentUploadResponse(
        id=str(doc.id),
        filename=doc.filename,
        process_status="pending",
        message="文档已重新提交处理"
    )


@router.get("/{kb_id}/documents", response_model=List[DocumentResponse])
async def get_documents(
    kb_id: str,
    current_user: User = Depends(get_current_user_required),
    db: Session = Depends(get_db),
):
    """获取知识库的文档列表"""
    try:
        kb_uuid = UUID(kb_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="无效的知识库ID格式"
        )

    # 验证知识库存在
    kb = db.query(KnowledgeBase).filter(
        KnowledgeBase.id == kb_uuid,
        KnowledgeBase.user_id == current_user.id
    ).first()

    if not kb:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="知识库不存在"
        )

    documents = db.query(Document).filter(
        Document.knowledge_base_id == kb_uuid
    ).order_by(Document.created_at.desc()).all()

    return [doc_to_response(doc) for doc in documents]


@router.get("/{kb_id}/documents/{doc_id}/chunks")
async def get_document_chunks(
    kb_id: str,
    doc_id: str,
    current_user: User = Depends(get_current_user_required),
    db: Session = Depends(get_db),
):
    """获取文档的所有切片"""
    from service.milvus_service import get_milvus_service

    try:
        kb_uuid = UUID(kb_id)
        doc_uuid = UUID(doc_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="无效的ID格式"
        )

    # 验证知识库存在
    kb = db.query(KnowledgeBase).filter(
        KnowledgeBase.id == kb_uuid,
        KnowledgeBase.user_id == current_user.id
    ).first()

    if not kb:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="知识库不存在"
        )

    # 获取文档
    doc = db.query(Document).filter(
        Document.id == doc_uuid,
        Document.knowledge_base_id == kb_uuid
    ).first()

    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="文档不存在"
        )

    if doc.status != "completed":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="文档尚未处理完成"
        )

    # 从 Milvus 获取切片（兼容新旧集合命名）
    from service.collection_naming import candidate_collection_names

    chunks = []
    tried = []
    try:
        milvus = get_milvus_service()
        for collection_name in candidate_collection_names(kb.id, kb.name):
            tried.append(collection_name)
            found = milvus.get_chunks_by_filename(collection_name, doc.filename)
            if found:
                chunks = found
                break
        logger.info("[get_document_chunks] collections=%s, 命中 %s 个切片", tried, len(chunks))
    except Exception as e:
        logger.error("[get_document_chunks] Milvus 查询失败 (collections=%s): %s", tried, e)
        # 返回空结果而不是报错
        chunks = []

    return {
        "document_id": str(doc.id),
        "filename": doc.filename,
        "chunk_count": len(chunks),
        "chunks": [
            {
                "index": chunk.get("chunk_index", i),
                "content": chunk.get("content", ""),
            }
            for i, chunk in enumerate(chunks)
        ]
    }


@router.delete("/{kb_id}/documents/{doc_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(
    kb_id: str,
    doc_id: str,
    current_user: User = Depends(get_current_user_required),
    db: Session = Depends(get_db),
):
    """删除文档"""
    try:
        kb_uuid = UUID(kb_id)
        doc_uuid = UUID(doc_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="无效的ID格式"
        )

    # 验证知识库存在
    kb = db.query(KnowledgeBase).filter(
        KnowledgeBase.id == kb_uuid,
        KnowledgeBase.user_id == current_user.id
    ).first()

    if not kb:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="知识库不存在"
        )

    # 获取文档
    doc = db.query(Document).filter(
        Document.id == doc_uuid,
        Document.knowledge_base_id == kb_uuid
    ).first()

    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="文档不存在"
        )

    # 删除文件（如果存在）
    if doc.file_path and os.path.exists(doc.file_path):
        os.remove(doc.file_path)

    # 同步删除 Milvus 中的切片，避免检索到已删除文档
    try:
        import hashlib

        from service.collection_naming import candidate_collection_names
        from service.milvus_service import get_milvus_service

        milvus = get_milvus_service()
        doc_hash = hashlib.md5(doc.filename.encode()).hexdigest()
        for collection_name in candidate_collection_names(kb.id, kb.name):
            milvus.delete_by_doc_id(collection_name, doc_hash)
    except Exception as e:
        logger.warning("[delete_document] 删除向量失败（忽略）: %s", e)

    # 更新知识库文档计数
    kb.document_count = max((kb.document_count or 0) - 1, 0)

    db.delete(doc)
    db.commit()
    return None
