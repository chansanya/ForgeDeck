"""定义持久任务状态机及合法状态迁移校验。"""

from __future__ import annotations

from collections.abc import Iterable

from devops.domain.models import TaskState


class InvalidTaskTransition(ValueError):
    """Raised when code attempts an impossible persistent task transition."""


_ALLOWED_TRANSITIONS: dict[TaskState, frozenset[TaskState]] = {
    TaskState.PENDING: frozenset({TaskState.LEASED, TaskState.CANCELLED}),
    TaskState.LEASED: frozenset(
        {TaskState.PENDING, TaskState.RUNNING, TaskState.FAILED, TaskState.CANCELLED}
    ),
    TaskState.RUNNING: frozenset(
        {TaskState.PENDING, TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED}
    ),
    TaskState.SUCCEEDED: frozenset(),
    TaskState.FAILED: frozenset(),
    TaskState.CANCELLED: frozenset(),
}

TERMINAL_TASK_STATES = frozenset(
    {TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED}
)


def allowed_transitions(state: TaskState) -> frozenset[TaskState]:
    """返回状态机允许的下一状态集合，供仓储和测试共享同一约束。"""
    return _ALLOWED_TRANSITIONS[state]


def ensure_task_transition(current: TaskState, target: TaskState) -> None:
    """校验一次状态迁移，拒绝终态回退或跳过租约保护。"""
    if target not in _ALLOWED_TRANSITIONS[current]:
        raise InvalidTaskTransition(f"invalid task transition: {current.value} -> {target.value}")


def is_terminal_task_state(state: TaskState) -> bool:
    """判断任务是否已进入不可再迁移的终态。"""
    return state in TERMINAL_TASK_STATES


def validate_transition_path(states: Iterable[TaskState]) -> None:
    """逐段校验一条状态路径，空路径视为无需验证。"""
    iterator = iter(states)
    try:
        current = next(iterator)
    except StopIteration:
        return
    for target in iterator:
        ensure_task_transition(current, target)
        current = target
