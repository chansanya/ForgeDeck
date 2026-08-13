"""导出 Runner 租约引擎、任务协议、进程执行与状态机公共接口。"""

from devops.runner.contracts import RunnerTaskStore, TaskExecutionContext, TaskHandler, TaskLease
from devops.runner.engine import LeaseLostError, LeaseRunner, RetryableTaskError, TaskCancelledError
from devops.runner.process import (
    AsyncCommandRunner,
    CommandExecutionError,
    CommandResult,
    CommandSpec,
)
from devops.runner.state import (
    InvalidTaskTransition,
    allowed_transitions,
    ensure_task_transition,
    is_terminal_task_state,
)

__all__ = [
    "AsyncCommandRunner",
    "CommandExecutionError",
    "CommandResult",
    "CommandSpec",
    "InvalidTaskTransition",
    "LeaseLostError",
    "LeaseRunner",
    "RetryableTaskError",
    "RunnerTaskStore",
    "TaskExecutionContext",
    "TaskHandler",
    "TaskLease",
    "TaskCancelledError",
    "allowed_transitions",
    "ensure_task_transition",
    "is_terminal_task_state",
]
