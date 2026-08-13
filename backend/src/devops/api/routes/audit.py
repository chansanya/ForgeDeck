"""提供追加式审计事件的只读查询接口。"""

from fastapi import APIRouter, Query
from sqlalchemy import select

from devops.api.deps import CurrentUser, SessionDep
from devops.domain.models import AuditEvent
from devops.schemas import AuditEventRead

router = APIRouter(prefix="/audit", tags=["audit"])


@router.get("", response_model=list[AuditEventRead])
async def list_audit_events(
    _: CurrentUser,
    session: SessionDep,
    actor: str | None = None,
    action: str | None = None,
    resource_type: str | None = None,
    outcome: str | None = None,
    limit: int = Query(default=200, ge=1, le=1000),
) -> list[AuditEvent]:
    query = select(AuditEvent)
    if actor:
        query = query.where(AuditEvent.actor == actor)
    if action:
        query = query.where(AuditEvent.action == action)
    if resource_type:
        query = query.where(AuditEvent.resource_type == resource_type)
    if outcome:
        query = query.where(AuditEvent.outcome == outcome)
    query = query.order_by(AuditEvent.created_at.desc()).limit(limit)
    return list((await session.scalars(query)).all())
