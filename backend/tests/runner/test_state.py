from __future__ import annotations

import pytest

from devops.domain.models import TaskState
from devops.runner.state import (
    InvalidTaskTransition,
    ensure_task_transition,
    is_terminal_task_state,
    validate_transition_path,
)


def test_valid_task_lifecycle() -> None:
    validate_transition_path(
        (
            TaskState.PENDING,
            TaskState.LEASED,
            TaskState.RUNNING,
            TaskState.SUCCEEDED,
        )
    )
    assert is_terminal_task_state(TaskState.SUCCEEDED)


def test_terminal_state_cannot_be_resurrected() -> None:
    with pytest.raises(InvalidTaskTransition, match="succeeded -> pending"):
        ensure_task_transition(TaskState.SUCCEEDED, TaskState.PENDING)


def test_expired_running_task_can_be_requeued() -> None:
    ensure_task_transition(TaskState.RUNNING, TaskState.PENDING)
