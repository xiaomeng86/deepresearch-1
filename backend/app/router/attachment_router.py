# Copyright © 2026 深圳市深维智见教育科技有限公司 版权所有
# 未经授权，禁止转售或仿制。

"""聊天附件路由"""
import logging
import os
import shutil
from typing import List, Optional
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File, BackgroundTasks, Form
from sqlalchemy.orm import Session

from core.database import get_db
from models.chat import ChatAttachment, ChatSession
from models.user import User
from router.auth_router import get_current_user
from schemas.chat import AttachmentResponse, AttachmentListResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/attachments", tags=["聊天附件"])

# 文件上传目录
UPLOAD_DIR = os.getenv("ATTACHMENT_UPLOAD_DIR", "/tmp/chat_attachments")
os.makedirs(UPLOAD_DIR, exist_ok=True)

# 支持的文件类型
ALLOWED_EXTENSIONS = {
    # 文档类型
    '.pdf', '.docx', '.doc', '.txt', '.md', '.html', '.xlsx', '.xls', '.pptx', '.ppt',
    # 图片类型
    '.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp',
    # 代码类型
    '.py', '.js', '.ts', '.json', '.yaml', '.yml', '.xml', '.csv',
}


def get_file_extension(filename: str) -> str:
    """获取文件扩展名"""
    return os.path.splitext(filename)[1].lower()


def attachment_to_response(att: ChatAttachment) -> AttachmentResponse:
    """将附件模型转换为响应（status 是接口级状态，业务状态用 process_status）"""
    return AttachmentResponse(
        status="success",
        id=str(att.id),
        session_id=str(att.session_id),
        message_id=str(att.message_id) if att.message_id else None,
        filename=att.filename,
        file_type=att.file_type,
        file_size=att.file_size,
        process_status=att.status,
        error_message=att.error_message,
        content_length=len(att.content_text or ""),
        created_at=att.created_at,
    )


async def process_attachment(attachment_id: str, file_path: str, db_session_factory):
    """
    后台处理附件（提取文本内容）

    修复点（问题2的第二部分）：
    旧实现只对纯文本类做真实读取，PDF/Word/图片一律写入 "[PDF 文件: xxx]" 占位符，
    导致带附件提问时模型拿不到任何内容，"聊天引用文件回答" 无法闭环。
    现在统一走 document_parser_service（本地解析优先，必要时 DocMind OCR）。
    """
    db = db_session_factory()
    att = None
    try:
        att = db.query(ChatAttachment).filter(ChatAttachment.id == attachment_id).first()
        if not att:
            logger.error("[process_attachment] 附件记录不存在: %s", attachment_id)
            return

        att.status = "processing"
        att.error_message = None
        db.commit()

        from service.document_parser_service import DocumentParseError, parse_document

        try:
            parsed = parse_document(file_path, att.filename)
            content_text = parsed.text or ""

            if not content_text.strip():
                att.status = "failed"
                att.error_message = f"未能从文件中提取到文本（解析器: {parsed.parser}）"
                logger.warning("[process_attachment] %s: %s", attachment_id, att.error_message)
            else:
                # 限制内容长度
                if len(content_text) > 50000:
                    content_text = content_text[:50000] + "\n...[内容已截断]"
                att.content_text = content_text
                att.status = "completed"
                att.error_message = None
                logger.info(
                    "[process_attachment] 附件解析成功 %s: parser=%s, chars=%s",
                    attachment_id, parsed.parser, len(content_text),
                )

        except DocumentParseError as e:
            logger.error("[process_attachment] 解析失败 %s: %s", attachment_id, e)
            att.status = "failed"
            att.error_message = str(e)

        db.commit()

    except Exception as e:
        logger.exception("[process_attachment] 附件处理异常 %s: %s", attachment_id, e)
        try:
            if att is None:
                att = db.query(ChatAttachment).filter(ChatAttachment.id == attachment_id).first()
            if att:
                att.status = "failed"
                att.error_message = f"{type(e).__name__}: {e}"
                db.commit()
        except Exception:
            logger.exception("[process_attachment] 写入失败状态时再次异常 %s", attachment_id)
    finally:
        db.close()


@router.post("", response_model=AttachmentResponse, status_code=status.HTTP_201_CREATED)
async def upload_attachment(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    session_id: str = Form(...),
    current_user: Optional[User] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """上传聊天附件"""
    # 解析 session_id
    try:
        session_uuid = UUID(session_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="无效的会话ID格式"
        )

    # 验证会话存在
    session = db.query(ChatSession).filter(ChatSession.id == session_uuid).first()
    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="会话不存在"
        )

    # 验证文件类型
    ext = get_file_extension(file.filename)
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"不支持的文件类型: {ext}，支持的类型: {', '.join(sorted(ALLOWED_EXTENSIONS))}"
        )

    # 生成唯一文件名
    import uuid
    unique_filename = f"{uuid.uuid4()}_{file.filename}"
    file_path = os.path.join(UPLOAD_DIR, unique_filename)

    # 保存文件
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

    # 创建附件记录
    att = ChatAttachment(
        session_id=session_uuid,
        user_id=current_user.id if current_user else None,
        filename=file.filename,
        file_type=ext[1:] if ext else "unknown",
        file_size=file_size,
        file_path=file_path,
        status="pending",
    )
    db.add(att)
    db.commit()
    db.refresh(att)

    # 后台处理附件
    from core.database import SessionLocal
    background_tasks.add_task(
        process_attachment,
        str(att.id),
        file_path,
        SessionLocal
    )

    return attachment_to_response(att)


@router.get("/{attachment_id}", response_model=AttachmentResponse)
async def get_attachment(
    attachment_id: str,
    current_user: Optional[User] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """获取附件详情"""
    try:
        att_uuid = UUID(attachment_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="无效的附件ID格式"
        )

    att = db.query(ChatAttachment).filter(ChatAttachment.id == att_uuid).first()
    if not att:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="附件不存在"
        )

    return attachment_to_response(att)


@router.get("/session/{session_id}", response_model=AttachmentListResponse)
async def get_session_attachments(
    session_id: str,
    current_user: Optional[User] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """获取会话的所有附件"""
    try:
        session_uuid = UUID(session_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="无效的会话ID格式"
        )

    # 验证会话存在
    session = db.query(ChatSession).filter(ChatSession.id == session_uuid).first()
    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="会话不存在"
        )

    attachments = db.query(ChatAttachment).filter(
        ChatAttachment.session_id == session_uuid
    ).order_by(ChatAttachment.created_at.desc()).all()

    return AttachmentListResponse(
        attachments=[attachment_to_response(att) for att in attachments],
        total=len(attachments),
    )


@router.delete("/{attachment_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_attachment(
    attachment_id: str,
    current_user: Optional[User] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """删除附件"""
    try:
        att_uuid = UUID(attachment_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="无效的附件ID格式"
        )

    att = db.query(ChatAttachment).filter(ChatAttachment.id == att_uuid).first()
    if not att:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="附件不存在"
        )

    # 删除文件
    if att.file_path and os.path.exists(att.file_path):
        try:
            os.remove(att.file_path)
        except Exception:
            pass  # 忽略文件删除错误

    db.delete(att)
    db.commit()
    return None
