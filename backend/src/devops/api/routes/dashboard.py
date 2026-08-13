"""聚合首页所需的项目、服务器、运行和审批统计。"""

from fastapi import APIRouter
from sqlalchemy import func, select

from devops.api.deps import CurrentUser, SessionDep
from devops.domain.models import (
    ApprovalState,
    OperationRequest,
    PipelineRun,
    Project,
    RunStatus,
    Server,
)
from devops.schemas import DashboardSummary

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("/summary", response_model=DashboardSummary)
async def summary(_: CurrentUser, session: SessionDep) -> DashboardSummary:
    async def count(model, *criteria) -> int:
        value = await session.scalar(select(func.count()).select_from(model).where(*criteria))
        return int(value or 0)

    return DashboardSummary(
        server_count=await count(Server),
        project_count=await count(Project),
        queued_runs=await count(PipelineRun, PipelineRun.status == RunStatus.QUEUED),
        running_runs=await count(PipelineRun, PipelineRun.status == RunStatus.RUNNING),
        failed_runs=await count(PipelineRun, PipelineRun.status == RunStatus.FAILED),
        pending_approvals=await count(
            OperationRequest, OperationRequest.state == ApprovalState.PENDING
        ),
    )
