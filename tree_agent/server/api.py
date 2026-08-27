"""The HTTP surface.

Every route is thin on purpose: authenticate, validate the shape, call one
service method, return what it produced. No route reaches for a connection or
decides who may see what — that all lives in the services, so the desktop client
and any future caller get identical rules.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Body, Depends, FastAPI, Header, Query, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from .errors import PayloadTooLarge, ServiceError, ValidationError
from .services.access import Actor
from .services.attachments import CHUNK_SIZE
from .services.idempotency import fingerprint

SESSION_COOKIE = "tree_agent_session"


# ------------------------------------------------------------------ schemas


class LoginBody(BaseModel):
    username: str
    password: str


class BootstrapBody(BaseModel):
    token: str
    username: str
    password: str
    email: str
    display_name: str = ""


class CreateUserBody(BaseModel):
    username: str
    password: str
    email: str
    display_name: str = ""
    role: Literal["admin", "member", "viewer"] = "member"


class PasswordBody(BaseModel):
    password: str
    current_password: str | None = None


class EmailBody(BaseModel):
    email: str


class EmailVerificationBody(BaseModel):
    email: str
    code: str


class PasswordResetConfirmBody(BaseModel):
    email: str
    code: str
    password: str


class ActiveBody(BaseModel):
    is_active: bool


class RoleBody(BaseModel):
    role: Literal["admin", "member", "viewer"]


class CreateProjectBody(BaseModel):
    parent_id: str | None = None
    name: str
    index: int | None = None
    settings: dict[str, Any] | None = None


class UpdateProjectBody(BaseModel):
    revision: int
    fields: dict[str, Any] = Field(default_factory=dict)


class MoveProjectBody(BaseModel):
    revision: int
    parent_id: str | None = None
    index: int | None = None


class ExpandedBody(BaseModel):
    is_expanded: bool


class MembershipBody(BaseModel):
    user_id: str
    permission: Literal["owner", "editor", "viewer"]


class CreateConversationBody(BaseModel):
    project_id: str
    name: str | None = None
    agent_id: Literal["codex", "claude"] = "codex"
    model: str | None = None
    index: int | None = None


class UpdateConversationBody(BaseModel):
    revision: int
    fields: dict[str, Any] = Field(default_factory=dict)


class MoveConversationBody(BaseModel):
    revision: int
    project_id: str
    index: int | None = None


class RunnerBody(BaseModel):
    codex_thread_id: str | None = None
    claude_session_id: str | None = None


class AppendMessageBody(BaseModel):
    role: str
    content: str = ""
    content_format: Literal["plain", "markdown", "json"] = "plain"
    agent_id: str | None = None
    model: str | None = None
    external_event_id: str | None = None
    metadata: dict[str, Any] | None = None
    attachment_ids: list[str] | None = None
    completed: bool = False


class DeltaBody(BaseModel):
    delta: str


class CompleteBody(BaseModel):
    content: str | None = None
    usage: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None


class ToolCallBody(BaseModel):
    tool_name: str
    status: Literal["pending", "running", "completed", "failed", "cancelled"] = "running"
    input: dict[str, Any] | None = None
    output_text: str = ""
    error_text: str | None = None


class ToolCallPatchBody(BaseModel):
    status: Literal["pending", "running", "completed", "failed", "cancelled"] | None = None
    output_text: str | None = None
    error_text: str | None = None


class CancelBody(BaseModel):
    note: str = "（已停止）"


class InitiateUploadBody(BaseModel):
    conversation_id: str
    file_name: str
    mime_type: str = "application/octet-stream"
    byte_size: int
    sha256: str | None = None
    message_id: str | None = None


class CommitUploadBody(BaseModel):
    message_id: str | None = None


class PurgeBody(BaseModel):
    retention_days: int = 30
    dry_run: bool = True


class BackupBody(BaseModel):
    destination: str


class ImportBody(BaseModel):
    source_path: str
    parent_project_id: str | None = None
    dry_run: bool = False


class MailSettingsBody(BaseModel):
    host: str
    port: int
    from_address: str
    encryption: Literal["none", "starttls", "ssl"] = "none"
    username: str = ""
    # Omit this property, or use __UNCHANGED__, to retain the encrypted value.
    # An empty string clears it when SMTP AUTH is disabled.
    password: str | None = None


class TestMailBody(BaseModel):
    recipient: str


# ------------------------------------------------------------------- routes


def build_router(services: Any) -> APIRouter:
    """`services` is the container built in `app.py`."""
    router = APIRouter(prefix="/api")

    def actor_from(request: Request, authorization: str | None) -> Actor:
        token = None
        if authorization and authorization.lower().startswith("bearer "):
            token = authorization[7:].strip()
        token = token or request.cookies.get(SESSION_COOKIE)
        return services.auth.authenticate(token)

    def current_actor(
        request: Request, authorization: str | None = Header(default=None)
    ) -> Actor:
        return actor_from(request, authorization)

    def idempotent(request: Request, key: str | None, body: Any, actor: Actor, produce):
        return services.idempotency.run(
            user_id=actor.id,
            request_key=key,
            request_fingerprint=fingerprint(
                request.method, request.url.path, _jsonable(body)
            ),
            produce=produce,
        )

    Auth = Depends(current_actor)
    IdemKey = Header(default=None, alias="Idempotency-Key")

    # ---------------------------------------------------------------- meta

    @router.get("/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "needs_bootstrap": services.auth.needs_bootstrap(),
            "writer_queue_depth": services.db.writer_depth,
        }

    # ---------------------------------------------------------------- auth

    @router.post("/auth/bootstrap")
    def bootstrap(body: BootstrapBody) -> dict[str, Any]:
        return services.auth.bootstrap_admin(
            token=body.token,
            username=body.username,
            password=body.password,
            email=body.email,
            display_name=body.display_name,
        )

    @router.post("/auth/login")
    def login(body: LoginBody, response: Response) -> dict[str, Any]:
        token, user = services.auth.login(body.username, body.password)
        response.set_cookie(
            SESSION_COOKIE,
            token,
            httponly=True,
            secure=services.config.secure_cookies,
            samesite="lax",
            max_age=30 * 24 * 60 * 60,
            path="/",
        )
        return {"token": token, "user": user}

    @router.post("/auth/logout")
    def logout(response: Response, actor: Actor = Auth) -> dict[str, Any]:
        services.auth.logout(actor)
        response.delete_cookie(SESSION_COOKIE, path="/")
        return {"status": "ok"}

    @router.post("/auth/password-reset/request")
    def request_password_reset(body: EmailBody) -> dict[str, Any]:
        # The answer is intentionally identical for unknown, disabled and
        # unverified accounts so this endpoint cannot enumerate account email.
        services.auth.request_password_reset(body.email)
        return {"status": "ok"}

    @router.post("/auth/password-reset/confirm")
    def confirm_password_reset(body: PasswordResetConfirmBody) -> dict[str, Any]:
        services.auth.confirm_password_reset(
            email=body.email, code=body.code, password=body.password
        )
        return {"status": "ok"}

    @router.get("/auth/me")
    def me(actor: Actor = Auth) -> dict[str, Any]:
        result = {
            "id": actor.id,
            "username": actor.username,
            "display_name": actor.display_name,
            "role": actor.role,
        }
        result.update(services.auth.user_email(actor.id))
        return result

    @router.post("/auth/password")
    def change_password(body: PasswordBody, actor: Actor = Auth) -> dict[str, Any]:
        if not body.current_password:
            raise ValidationError("需要提供目前的密碼")
        services.auth.change_own_password(actor, body.current_password, body.password)
        return {"status": "ok"}

    @router.post("/auth/email-verification/request")
    def request_email_verification(body: EmailBody, actor: Actor = Auth) -> dict[str, Any]:
        services.auth.request_email_verification(actor, body.email)
        return {"status": "ok"}

    @router.post("/auth/email-verification/confirm")
    def confirm_email_verification(
        body: EmailVerificationBody, actor: Actor = Auth
    ) -> dict[str, Any]:
        return services.auth.confirm_email_verification(actor, email=body.email, code=body.code)

    # --------------------------------------------------------------- users

    @router.get("/users")
    def list_users(actor: Actor = Auth) -> list[dict[str, Any]]:
        return services.auth.list_users(actor)

    @router.post("/users")
    def create_user(
        request: Request, body: CreateUserBody, actor: Actor = Auth,
        idempotency_key: str | None = IdemKey,
    ) -> dict[str, Any]:
        return idempotent(request, idempotency_key, body, actor, lambda: services.auth.create_user(
            actor, username=body.username, password=body.password,
            email=body.email, display_name=body.display_name, role=body.role,
        ))

    @router.post("/users/{user_id}/active")
    def set_active(request: Request, user_id: str, body: ActiveBody, actor: Actor = Auth,
                   idempotency_key: str | None = IdemKey) -> dict[str, Any]:
        return idempotent(request, idempotency_key, body, actor,
                          lambda: services.auth.set_active(actor, user_id, body.is_active))

    @router.post("/users/{user_id}/role")
    def set_role(request: Request, user_id: str, body: RoleBody, actor: Actor = Auth,
                 idempotency_key: str | None = IdemKey) -> dict[str, Any]:
        return idempotent(request, idempotency_key, body, actor,
                          lambda: services.auth.set_role(actor, user_id, body.role))

    @router.post("/users/{user_id}/password")
    def reset_password(request: Request, user_id: str, body: PasswordBody, actor: Actor = Auth,
                       idempotency_key: str | None = IdemKey) -> dict[str, Any]:
        return idempotent(request, idempotency_key, body, actor,
                          lambda: _reset_password(services, actor, user_id, body.password))

    # ------------------------------------------------------------ projects

    @router.get("/tree")
    def tree(actor: Actor = Auth) -> dict[str, Any]:
        return services.tree.tree(actor)

    @router.post("/projects")
    def create_project(
        request: Request,
        body: CreateProjectBody,
        actor: Actor = Auth,
        idempotency_key: str | None = IdemKey,
    ) -> dict[str, Any]:
        return idempotent(
            request,
            idempotency_key,
            body,
            actor,
            lambda: services.tree.create_project(
                actor,
                parent_id=body.parent_id,
                name=body.name,
                index=body.index,
                settings=body.settings,
            ),
        )

    @router.get("/projects/{project_id}")
    def get_project(project_id: str, actor: Actor = Auth) -> dict[str, Any]:
        return services.tree.get_project(actor, project_id)

    @router.patch("/projects/{project_id}")
    def update_project(
        request: Request, project_id: str, body: UpdateProjectBody, actor: Actor = Auth,
        idempotency_key: str | None = IdemKey,
    ) -> dict[str, Any]:
        return idempotent(request, idempotency_key, body, actor, lambda: services.tree.update_project(
            actor, project_id, revision=body.revision, fields=body.fields
        ))

    @router.post("/projects/{project_id}/move")
    def move_project(
        request: Request, project_id: str, body: MoveProjectBody, actor: Actor = Auth,
        idempotency_key: str | None = IdemKey,
    ) -> dict[str, Any]:
        return idempotent(request, idempotency_key, body, actor, lambda: services.tree.move_project(
            actor,
            project_id,
            revision=body.revision,
            parent_id=body.parent_id,
            index=body.index,
        ))

    @router.post("/projects/{project_id}/expanded")
    def set_expanded(
        request: Request, project_id: str, body: ExpandedBody, actor: Actor = Auth,
        idempotency_key: str | None = IdemKey,
    ) -> dict[str, Any]:
        return idempotent(request, idempotency_key, body, actor,
                          lambda: _set_expanded(services, actor, project_id, body.is_expanded))

    @router.delete("/projects/{project_id}")
    def delete_project(
        request: Request, project_id: str, revision: int = Query(...), actor: Actor = Auth,
        idempotency_key: str | None = IdemKey,
    ) -> dict[str, Any]:
        return idempotent(request, idempotency_key, {"revision": revision}, actor,
                          lambda: services.tree.delete_project(actor, project_id, revision=revision))

    @router.post("/projects/{project_id}/restore")
    def restore_project(request: Request, project_id: str, actor: Actor = Auth,
                        idempotency_key: str | None = IdemKey) -> dict[str, Any]:
        return idempotent(request, idempotency_key, {}, actor,
                          lambda: services.tree.restore_project(actor, project_id))

    @router.get("/projects/{project_id}/memberships")
    def list_memberships(project_id: str, actor: Actor = Auth) -> list[dict[str, Any]]:
        return services.tree.memberships(actor, project_id)

    @router.post("/projects/{project_id}/memberships")
    def grant_membership(
        request: Request, project_id: str, body: MembershipBody, actor: Actor = Auth,
        idempotency_key: str | None = IdemKey,
    ) -> list[dict[str, Any]]:
        return idempotent(request, idempotency_key, body, actor, lambda: services.tree.grant(
            actor, project_id, user_id=body.user_id, permission=body.permission
        ))

    @router.delete("/projects/{project_id}/memberships/{user_id}")
    def revoke_membership(
        request: Request, project_id: str, user_id: str, actor: Actor = Auth,
        idempotency_key: str | None = IdemKey,
    ) -> list[dict[str, Any]]:
        return idempotent(request, idempotency_key, {"user_id": user_id}, actor,
                          lambda: services.tree.revoke(actor, project_id, user_id=user_id))

    @router.get("/projects/{project_id}/conversations")
    def list_conversations(project_id: str, actor: Actor = Auth) -> list[dict[str, Any]]:
        return services.tree.list_conversations(actor, project_id)

    # ------------------------------------------------- inherited settings

    @router.get("/nodes/{node_id}/settings")
    def resolved_settings(node_id: str, actor: Actor = Auth) -> dict[str, Any]:
        return services.tree.resolve(actor, node_id)

    @router.get("/nodes/{node_id}/instructions")
    def instructions(node_id: str, actor: Actor = Auth) -> dict[str, Any]:
        return {"instructions": services.tree.instructions(actor, node_id)}

    @router.get("/nodes/{node_id}/path")
    def node_path(node_id: str, actor: Actor = Auth) -> dict[str, Any]:
        return {"path": services.tree.path_of(actor, node_id)}

    @router.get("/nodes/{node_id}/usage")
    def usage(node_id: str, actor: Actor = Auth) -> dict[str, Any]:
        return services.messages.usage_for(actor, node_id)

    # ------------------------------------------------------- conversations

    @router.post("/conversations")
    def create_conversation(
        request: Request,
        body: CreateConversationBody,
        actor: Actor = Auth,
        idempotency_key: str | None = IdemKey,
    ) -> dict[str, Any]:
        return idempotent(
            request,
            idempotency_key,
            body,
            actor,
            lambda: services.tree.create_conversation(
                actor,
                project_id=body.project_id,
                name=body.name,
                agent_id=body.agent_id,
                model=body.model,
                index=body.index,
            ),
        )

    @router.get("/conversations/{conversation_id}")
    def get_conversation(conversation_id: str, actor: Actor = Auth) -> dict[str, Any]:
        return services.tree.get_conversation(actor, conversation_id)

    @router.patch("/conversations/{conversation_id}")
    def update_conversation(
        request: Request, conversation_id: str, body: UpdateConversationBody, actor: Actor = Auth,
        idempotency_key: str | None = IdemKey,
    ) -> dict[str, Any]:
        return idempotent(request, idempotency_key, body, actor, lambda: services.tree.update_conversation(
            actor, conversation_id, revision=body.revision, fields=body.fields
        ))

    @router.post("/conversations/{conversation_id}/move")
    def move_conversation(
        request: Request, conversation_id: str, body: MoveConversationBody, actor: Actor = Auth,
        idempotency_key: str | None = IdemKey,
    ) -> dict[str, Any]:
        return idempotent(request, idempotency_key, body, actor, lambda: services.tree.move_conversation(
            actor,
            conversation_id,
            revision=body.revision,
            project_id=body.project_id,
            index=body.index,
        ))

    @router.post("/conversations/{conversation_id}/fork")
    def fork_conversation(
        request: Request,
        conversation_id: str,
        actor: Actor = Auth,
        idempotency_key: str | None = IdemKey,
    ) -> dict[str, Any]:
        return idempotent(
            request,
            idempotency_key,
            {"conversation_id": conversation_id},
            actor,
            lambda: services.tree.fork_conversation(actor, conversation_id),
        )

    @router.post("/conversations/{conversation_id}/runner")
    def set_runner(
        request: Request, conversation_id: str, body: RunnerBody, actor: Actor = Auth,
        idempotency_key: str | None = IdemKey,
    ) -> dict[str, Any]:
        return idempotent(request, idempotency_key, body, actor, lambda: services.tree.set_runner_state(
            actor,
            conversation_id,
            codex_thread_id=body.codex_thread_id,
            claude_session_id=body.claude_session_id,
        ))

    @router.post("/conversations/{conversation_id}/reset")
    def reset_conversation(request: Request, conversation_id: str, actor: Actor = Auth,
                           idempotency_key: str | None = IdemKey) -> dict[str, Any]:
        return idempotent(request, idempotency_key, {}, actor,
                          lambda: services.tree.reset_conversation(actor, conversation_id))

    @router.delete("/conversations/{conversation_id}")
    def delete_conversation(
        request: Request, conversation_id: str, revision: int = Query(...), actor: Actor = Auth,
        idempotency_key: str | None = IdemKey,
    ) -> dict[str, Any]:
        return idempotent(request, idempotency_key, {"revision": revision}, actor,
                          lambda: services.tree.delete_conversation(actor, conversation_id, revision=revision))

    @router.post("/conversations/{conversation_id}/restore")
    def restore_conversation(request: Request, conversation_id: str, actor: Actor = Auth,
                             idempotency_key: str | None = IdemKey) -> dict[str, Any]:
        return idempotent(request, idempotency_key, {}, actor,
                          lambda: services.tree.restore_conversation(actor, conversation_id))

    @router.post("/conversations/{conversation_id}/cancel")
    def cancel_turn(
        request: Request, conversation_id: str, body: CancelBody, actor: Actor = Auth,
        idempotency_key: str | None = IdemKey,
    ) -> dict[str, Any]:
        return idempotent(request, idempotency_key, body, actor,
                          lambda: services.messages.cancel_turn(actor, conversation_id, body.note))

    # ------------------------------------------------------------ messages

    @router.get("/conversations/{conversation_id}/messages")
    def list_messages(
        conversation_id: str,
        after_sequence_no: int | None = Query(default=None),
        before_sequence_no: int | None = Query(default=None),
        limit: int = Query(default=200, ge=1, le=1000),
        actor: Actor = Auth,
    ) -> dict[str, Any]:
        return services.messages.list_messages(
            actor,
            conversation_id,
            after_sequence_no=after_sequence_no,
            before_sequence_no=before_sequence_no,
            limit=limit,
        )

    @router.post("/conversations/{conversation_id}/messages")
    def append_message(
        request: Request,
        conversation_id: str,
        body: AppendMessageBody,
        actor: Actor = Auth,
        idempotency_key: str | None = IdemKey,
    ) -> dict[str, Any]:
        return idempotent(
            request,
            idempotency_key,
            body,
            actor,
            lambda: services.messages.append(
                actor,
                conversation_id,
                role=body.role,
                content=body.content,
                content_format=body.content_format,
                agent_id=body.agent_id,
                model=body.model,
                external_event_id=body.external_event_id,
                metadata=body.metadata,
                attachment_ids=body.attachment_ids,
                completed=body.completed,
            ),
        )

    @router.post("/messages/{message_id}/append")
    def append_delta(request: Request, message_id: str, body: DeltaBody, actor: Actor = Auth,
                     idempotency_key: str | None = IdemKey) -> dict[str, Any]:
        return idempotent(request, idempotency_key, body, actor,
                          lambda: services.messages.append_delta(actor, message_id, body.delta))

    @router.post("/messages/{message_id}/complete")
    def complete_message(
        request: Request, message_id: str, body: CompleteBody, actor: Actor = Auth,
        idempotency_key: str | None = IdemKey,
    ) -> dict[str, Any]:
        return idempotent(request, idempotency_key, body, actor, lambda: services.messages.complete(
            actor, message_id, content=body.content, usage=body.usage, metadata=body.metadata
        ))

    @router.delete("/messages/{message_id}")
    def delete_message(request: Request, message_id: str, actor: Actor = Auth,
                       idempotency_key: str | None = IdemKey) -> dict[str, Any]:
        return idempotent(request, idempotency_key, {}, actor,
                          lambda: services.messages.delete_message(actor, message_id))

    @router.post("/messages/{message_id}/tool-calls")
    def add_tool_call(
        request: Request, message_id: str, body: ToolCallBody, actor: Actor = Auth,
        idempotency_key: str | None = IdemKey,
    ) -> dict[str, Any]:
        return idempotent(request, idempotency_key, body, actor, lambda: services.messages.add_tool_call(
            actor,
            message_id,
            tool_name=body.tool_name,
            status=body.status,
            payload=body.input,
            output_text=body.output_text,
            error_text=body.error_text,
        ))

    @router.patch("/tool-calls/{tool_call_id}")
    def update_tool_call(
        request: Request, tool_call_id: str, body: ToolCallPatchBody, actor: Actor = Auth,
        idempotency_key: str | None = IdemKey,
    ) -> dict[str, Any]:
        return idempotent(request, idempotency_key, body, actor, lambda: services.messages.update_tool_call(
            actor,
            tool_call_id,
            status=body.status,
            output_text=body.output_text,
            error_text=body.error_text,
        ))

    # --------------------------------------------------------- attachments

    @router.post("/attachments/uploads")
    def initiate_upload(
        request: Request,
        body: InitiateUploadBody,
        actor: Actor = Auth,
        idempotency_key: str | None = IdemKey,
    ) -> dict[str, Any]:
        return idempotent(
            request,
            idempotency_key,
            body,
            actor,
            lambda: services.attachments.initiate(
                actor,
                conversation_id=body.conversation_id,
                file_name=body.file_name,
                mime_type=body.mime_type,
                byte_size=body.byte_size,
                sha256=body.sha256,
                message_id=body.message_id,
            ),
        )

    @router.put("/attachments/uploads/{upload_id}/chunks/{chunk_no}")
    async def put_chunk(
        request: Request, upload_id: str, chunk_no: int, actor: Actor = Auth,
        idempotency_key: str | None = IdemKey,
    ) -> dict[str, Any]:
        payload = await _read_bounded(request, CHUNK_SIZE)
        # Chunk retransmission is a normal part of uploads.  Include a digest
        # rather than raw bytes in the replay fingerprint so a key cannot be
        # reused to replace a chunk with different contents.
        return idempotent(
            request,
            idempotency_key,
            {"chunk_no": chunk_no, "byte_size": len(payload), "sha256": _sha256(payload)},
            actor,
            lambda: services.attachments.put_chunk(actor, upload_id, chunk_no, payload),
        )

    @router.post("/attachments/uploads/{upload_id}/commit")
    def commit_upload(
        request: Request,
        upload_id: str,
        body: CommitUploadBody = Body(default=CommitUploadBody()),
        actor: Actor = Auth,
        idempotency_key: str | None = IdemKey,
    ) -> dict[str, Any]:
        return idempotent(
            request,
            idempotency_key,
            {"upload_id": upload_id, "message_id": body.message_id},
            actor,
            lambda: services.attachments.commit(actor, upload_id, message_id=body.message_id),
        )

    @router.get("/attachments/{attachment_id}")
    def attachment_metadata(attachment_id: str, actor: Actor = Auth) -> dict[str, Any]:
        return services.attachments.metadata(actor, attachment_id)

    @router.get("/attachments/{attachment_id}/content")
    def download_attachment(attachment_id: str, actor: Actor = Auth) -> StreamingResponse:
        meta, chunks = services.attachments.stream(actor, attachment_id)
        return StreamingResponse(
            chunks,
            media_type=meta["mime_type"],
            headers={
                # Always an attachment: never let stored bytes render inline in
                # a browser context that trusts this origin.
                "Content-Disposition": _content_disposition(meta["file_name"]),
                "Content-Length": str(meta["byte_size"]),
                "X-Attachment-Sha256": meta["sha256"],
                "X-Content-Type-Options": "nosniff",
            },
        )

    @router.delete("/messages/{message_id}/attachments/{attachment_id}")
    def detach_attachment(
        request: Request, message_id: str, attachment_id: str, actor: Actor = Auth,
        idempotency_key: str | None = IdemKey,
    ) -> dict[str, Any]:
        return idempotent(request, idempotency_key, {"attachment_id": attachment_id}, actor,
                          lambda: services.attachments.detach(actor, message_id, attachment_id))

    # -------------------------------------------------------------- search

    @router.get("/search")
    def search(
        q: str = Query(min_length=1),
        kinds: str = Query(default="project,conversation,message"),
        limit: int = Query(default=20, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
        actor: Actor = Auth,
    ) -> dict[str, Any]:
        wanted = tuple(part.strip() for part in kinds.split(",") if part.strip())
        return services.search.search(actor, q, kinds=wanted, limit=limit, offset=offset)

    # --------------------------------------------------------------- admin

    @router.get("/admin/mail-settings")
    def get_mail_settings(actor: Actor = Auth) -> dict[str, Any]:
        from .services.access import require_admin

        require_admin(actor)
        return services.mail_settings.get()

    @router.put("/admin/mail-settings")
    def update_mail_settings(body: MailSettingsBody, actor: Actor = Auth) -> dict[str, Any]:
        from .services.access import require_admin

        require_admin(actor)
        return services.mail_settings.update(
            host=body.host, port=body.port, from_address=body.from_address,
            encryption=body.encryption, username=body.username, password=body.password,
        )

    @router.post("/admin/mail-settings/test")
    def test_mail_settings(body: TestMailBody, actor: Actor = Auth) -> dict[str, Any]:
        from .services.access import require_admin

        require_admin(actor)
        try:
            services.mail_settings.send_test(body.recipient)
        except ValidationError:
            raise
        except Exception as exc:  # noqa: BLE001 - smtplib has platform-specific failures
            raise ValidationError("無法寄送測試信，請檢查 SMTP 設定") from exc
        return {"status": "ok"}

    @router.get("/admin/stats")
    def stats(actor: Actor = Auth) -> dict[str, Any]:
        from .services.access import require_admin

        require_admin(actor)
        return services.maintenance.stats()

    @router.post("/admin/sweep")
    def sweep(request: Request, actor: Actor = Auth, idempotency_key: str | None = IdemKey) -> dict[str, Any]:
        from .services.access import require_admin

        require_admin(actor)
        return idempotent(request, idempotency_key, {}, actor, services.maintenance.sweep)

    @router.post("/admin/purge")
    def purge(request: Request, body: PurgeBody, actor: Actor = Auth,
              idempotency_key: str | None = IdemKey) -> dict[str, Any]:
        return idempotent(request, idempotency_key, body, actor, lambda: services.maintenance.purge_deleted(
            actor, retention_days=body.retention_days, dry_run=body.dry_run
        ))

    @router.post("/admin/backup")
    def backup(request: Request, body: BackupBody, actor: Actor = Auth,
               idempotency_key: str | None = IdemKey) -> dict[str, Any]:
        return idempotent(request, idempotency_key, body, actor,
                          lambda: services.maintenance.backup(actor, body.destination))

    @router.post("/admin/import-legacy")
    def import_legacy(request: Request, body: ImportBody, actor: Actor = Auth,
                      idempotency_key: str | None = IdemKey) -> dict[str, Any]:
        from .migrations.legacy_workspace_import import import_workspace

        return idempotent(request, idempotency_key, body, actor, lambda: import_workspace(
            services, actor, source_path=body.source_path,
            parent_project_id=body.parent_project_id, dry_run=body.dry_run,
        ))

    return router


# ------------------------------------------------------------------ helpers


async def _read_bounded(request: Request, limit: int) -> bytes:
    """Read a request body, refusing anything over `limit` without buffering it."""
    buffer = bytearray()
    async for part in request.stream():
        buffer.extend(part)
        if len(buffer) > limit:
            raise PayloadTooLarge(f"單一 chunk 最大 {limit} bytes")
    return bytes(buffer)


def _content_disposition(file_name: str) -> str:
    """RFC 5987 encoding, so a CJK file name survives the header intact."""
    from urllib.parse import quote

    ascii_fallback = file_name.encode("ascii", "replace").decode("ascii").replace('"', "_")
    return f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{quote(file_name)}"


def _sha256(payload: bytes) -> str:
    import hashlib

    return hashlib.sha256(payload).hexdigest()


def _reset_password(services: Any, actor: Actor, user_id: str, password: str) -> dict[str, str]:
    services.auth.reset_password(actor, user_id, password)
    return {"status": "ok"}


def _set_expanded(services: Any, actor: Actor, project_id: str, is_expanded: bool) -> dict[str, str]:
    services.tree.set_expanded(actor, project_id, is_expanded)
    return {"status": "ok"}


def _jsonable(body: Any) -> Any:
    if isinstance(body, BaseModel):
        return body.model_dump(mode="json")
    return body


def install_error_handler(app: FastAPI) -> None:
    @app.exception_handler(ServiceError)
    async def handle(_: Request, exc: ServiceError) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content=exc.payload())
