"""编排流水线、部署、脚本和指标任务，并持久化阶段 checkpoint。"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from devops.db.results import affected_rows
from devops.domain.models import (
    ApprovalState,
    Credential,
    CredentialKind,
    Deployment,
    DeploymentStatus,
    HostMetric,
    OperationRequest,
    PipelineRun,
    RunnerTask,
    ScriptVersion,
    Server,
    TaskState,
    utcnow,
)
from devops.integrations.notifications import (
    deliver_event,
    deployment_result_notification,
)
from devops.runner.build import BuildRequest, BuildxBuilder
from devops.runner.contracts import TaskExecutionContext
from devops.runner.credentials import (
    RegistryCredentials,
    create_git_askpass,
    parse_git_credentials,
    parse_registry_credentials,
    parse_ssh_credentials,
)
from devops.runner.deploy import (
    ComposeDeployer,
    DeploymentError,
    DeploymentRequest,
    HealthCheckKind,
    HealthCheckSpec,
    cleanup_stale_docker_configs,
)
from devops.runner.engine import TaskCancelledError
from devops.runner.metrics import HostMetricsCollector
from devops.runner.process import AsyncCommandRunner, CommandExecutionError
from devops.runner.source import (
    GitSourceManager,
    canonical_snapshot,
    resolve_repository_path,
)
from devops.runner.ssh import AsyncSSHConnector, SSHConnectionConfig, SSHCredentials
from devops.security import SecretManager


@dataclass(frozen=True, slots=True)
class RunnerDependencies:
    session_factory: async_sessionmaker[AsyncSession]
    workspace_dir: Path
    secrets: SecretManager
    commands: AsyncCommandRunner
    ssh: AsyncSSHConnector


class PipelineTaskHandler:
    def __init__(self, dependencies: RunnerDependencies) -> None:
        self._deps = dependencies
        self._source = GitSourceManager(dependencies.commands)
        self._builder = BuildxBuilder(dependencies.commands)
        self._deployer = ComposeDeployer()

    async def __call__(self, context: TaskExecutionContext) -> None:
        payload = context.lease.payload
        run_id = _required_string(payload, "run_id")
        commit_sha = _required_string(payload, "commit_sha")
        snapshot = _required_mapping(payload, "config_snapshot")
        expected_snapshot_hash = _required_string(payload, "snapshot_sha256")
        _, actual_snapshot_hash = canonical_snapshot(snapshot)
        if not _constant_time_equal(expected_snapshot_hash, actual_snapshot_hash):
            raise ValueError("pipeline config snapshot hash does not match its content")
        project = _required_mapping(snapshot, "project")
        environment_value = snapshot.get("environment")
        environment = (
            _mapping(environment_value, "environment") if environment_value is not None else None
        )

        workspace_root = self._deps.workspace_dir
        workspace_root.mkdir(parents=True, exist_ok=True)
        workspace = Path(
            tempfile.mkdtemp(
                prefix=f"run-{_safe_filename(run_id)}-{context.lease.attempts}-",
                dir=workspace_root,
            )
        )
        try:
            await self._stage(context, run_id, "checkout", "Checking out pinned Git commit")
            git_env = await self._git_environment(project, workspace, context)
            checkout = await self._source.checkout(
                repo_url=_required_string(project, "repo_url"),
                commit_sha=commit_sha,
                destination=workspace / "source",
                sink=context.log,
                cancel_event=context.cancel_event,
                extra_env=git_env,
            )
            self._raise_if_cancelled(context)

            expected_image_ref = _tag_image(
                _required_string(project, "image_repository"), commit_sha
            )
            registry_credentials = await self._registry_credentials(project, context)
            checkpoint = await self._load_artifact(run_id, expected_image_ref)
            if checkpoint is None:
                dockerfile_path = _required_string(project, "dockerfile_path")
                if project.get("dockerfile_source") == "inline":
                    inline_dockerfile = _required_string(project, "dockerfile_content")
                    dockerfile_path = _write_inline_dockerfile(
                        checkout.directory, inline_dockerfile
                    )
                await self._stage(
                    context,
                    run_id,
                    "build",
                    f"Building and pushing {expected_image_ref}",
                )
                artifact = await self._builder.build_and_push(
                    BuildRequest(
                        source_root=checkout.directory,
                        dockerfile_path=dockerfile_path,
                        context_path=_required_string(project, "build_context"),
                        image_ref=expected_image_ref,
                        build_args=_string_mapping(
                            project.get("build_args", {}), "build_args"
                        ),
                        labels={
                            "devops.run_id": run_id,
                            "devops.commit_sha": commit_sha,
                            "org.opencontainers.image.revision": commit_sha,
                        },
                        registry_credentials=registry_credentials,
                    ),
                    sink=context.log,
                    cancel_event=context.cancel_event,
                )
                image_ref = artifact.image_ref
                image_digest = artifact.digest
                await self._save_artifact(run_id, image_ref, image_digest)
            else:
                # 镜像已推送且 digest 已持久化时复用 checkpoint，避免进程恢复后
                # 用同一 commit 再次构建出内容不同的镜像并破坏可追溯性。
                image_ref, image_digest = checkpoint
                await self._stage(
                    context,
                    run_id,
                    "registry",
                    "Reusing checkpointed immutable image "
                    f"{_immutable_image_ref(image_ref, image_digest)}",
                )
            await context.log.write(
                f"Published immutable image {_immutable_image_ref(image_ref, image_digest)}\n",
                stage="registry",
            )
            self._raise_if_cancelled(context)

            if environment is not None:
                await self._deploy_pipeline_artifact(
                    context,
                    run_id=run_id,
                    project=project,
                    environment=environment,
                    source_root=checkout.directory,
                    image_ref=image_ref,
                    image_digest=image_digest,
                    revision=commit_sha,
                    registry_credentials=registry_credentials,
                )
            await self._stage(context, run_id, "complete", "Pipeline completed successfully")
        except CommandExecutionError as exc:
            if context.cancel_event.is_set() or exc.result.cancelled:
                raise TaskCancelledError("pipeline was cancelled") from exc
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if context.cancel_event.is_set():
                raise TaskCancelledError("pipeline was cancelled") from exc
            raise
        finally:
            await asyncio.to_thread(shutil.rmtree, workspace, True)

    async def _deploy_pipeline_artifact(
        self,
        context: TaskExecutionContext,
        *,
        run_id: str,
        project: Mapping[str, Any],
        environment: Mapping[str, Any],
        source_root: Path,
        image_ref: str,
        image_digest: str,
        revision: str,
        registry_credentials: RegistryCredentials | None,
    ) -> None:
        await self._stage(context, run_id, "deploy", "Deploying immutable image")
        compose_content = _compose_content(environment, source_root)
        server_id = _required_string(environment, "server_id")
        ssh_config, ssh_credentials, secret_values = await _load_ssh_target(
            self._deps, server_id
        )
        context.log.add_secrets(secret_values)
        pipeline_config = _mapping(project.get("pipeline_config", {}), "pipeline_config")
        service_name = str(pipeline_config.get("service_name") or "app")
        env_content = _encode_env(_string_mapping(environment.get("env_config", {}), "env_config"))
        health = _health_spec(environment.get("healthcheck", {}))
        deployment_snapshot = {
            **dict(environment),
            "project_id": _required_string(project, "id"),
            "registry_credential_id": project.get("registry_credential_id"),
            "service_name": service_name,
            "min_free_bytes": _minimum_free_bytes(
                pipeline_config.get("min_free_bytes")
            ),
        }
        deployment_id, deployment_status = await self._create_pipeline_deployment(
            context,
            run_id=run_id,
            project_id=_required_string(project, "id"),
            environment_id=_required_string(environment, "id"),
            server_id=server_id,
            image_ref=image_ref,
            image_digest=image_digest,
            revision=revision,
            compose_content=compose_content,
            environment_snapshot=deployment_snapshot,
        )
        if deployment_status == DeploymentStatus.HEALTHY:
            # 数据库终态已经确认成功，重启恢复时禁止再次触发远端 Compose 副作用。
            await context.log.write(
                "Deployment checkpoint is already healthy; skipping remote replay\n",
                stage="deploy",
            )
            return
        if deployment_status in {
            DeploymentStatus.ROLLED_BACK,
            DeploymentStatus.FAILED,
        }:
            raise DeploymentError(
                "the recovered pipeline deployment already reached a failed terminal state",
                rolled_back=deployment_status == DeploymentStatus.ROLLED_BACK,
            )
        rollback_registry_credentials, rollback_image_ref, rollback_redactions = (
            await _load_rollback_registry_context(self._deps, deployment_id)
        )
        context.log.add_secrets(rollback_redactions)
        try:
            async with self._deps.ssh.connect(ssh_config, ssh_credentials) as session:
                result = await self._deployer.deploy(
                    DeploymentRequest(
                        project_name=_compose_project_name(
                            _required_string(project, "id"),
                            _required_string(environment, "id"),
                        ),
                        service_name=service_name,
                        remote_directory=_required_string(environment, "deploy_path"),
                        compose_content=compose_content,
                        env_content=env_content,
                        image_ref=image_ref,
                        image_digest=image_digest,
                        revision=revision,
                        registry_credentials=registry_credentials,
                        rollback_registry_credentials=rollback_registry_credentials,
                        rollback_image_ref=rollback_image_ref,
                        min_free_bytes=_minimum_free_bytes(
                            pipeline_config.get("min_free_bytes")
                        ),
                        health_check=health,
                    ),
                    session=session,
                )
        except asyncio.CancelledError:
            await self._finish_deployment(
                deployment_id,
                status=DeploymentStatus.FAILED,
                succeeded=False,
                error="pipeline deployment was cancelled",
            )
            raise
        except Exception as exc:
            rolled_back = isinstance(exc, DeploymentError) and exc.rolled_back
            await self._finish_deployment(
                deployment_id,
                status=(
                    DeploymentStatus.ROLLED_BACK if rolled_back else DeploymentStatus.FAILED
                ),
                succeeded=False,
                error=str(exc),
            )
            raise
        await self._finish_deployment(
            deployment_id,
            status=DeploymentStatus.HEALTHY,
            succeeded=True,
            health=dict(result.health),
        )

    async def _git_environment(
        self,
        project: Mapping[str, Any],
        workspace: Path,
        context: TaskExecutionContext,
    ) -> Mapping[str, str]:
        credential_id = project.get("git_credential_id")
        if not credential_id:
            return {}
        secret = await _load_git_credential_secret(self._deps, str(credential_id))
        credentials = parse_git_credentials(secret)
        context.log.add_secrets((credentials.username, credentials.password))
        _, environment = create_git_askpass(workspace / ".auth", credentials)
        return environment

    async def _registry_credentials(
        self,
        project: Mapping[str, Any],
        context: TaskExecutionContext,
    ) -> RegistryCredentials | None:
        credential_id = project.get("registry_credential_id")
        if not credential_id:
            return None
        credentials, redactions = await _load_registry_credentials(
            self._deps, str(credential_id)
        )
        context.log.add_secrets(redactions)
        return credentials

    async def _stage(
        self, context: TaskExecutionContext, run_id: str, stage: str, message: str
    ) -> None:
        async with self._deps.session_factory() as session:
            await session.execute(
                update(PipelineRun).where(PipelineRun.id == run_id).values(current_stage=stage)
            )
            await session.commit()
        await context.log.write(f"{message}\n", stage=stage)

    async def _save_artifact(self, run_id: str, image_ref: str, digest: str) -> None:
        async with self._deps.session_factory() as session:
            await session.execute(
                update(PipelineRun)
                .where(PipelineRun.id == run_id)
                .values(image_ref=image_ref, image_digest=digest, current_stage="registry")
            )
            await session.commit()

    async def _load_artifact(
        self,
        run_id: str,
        expected_image_ref: str,
    ) -> tuple[str, str] | None:
        async with self._deps.session_factory() as session:
            row = (
                await session.execute(
                    select(PipelineRun.image_ref, PipelineRun.image_digest).where(
                        PipelineRun.id == run_id
                    )
                )
            ).one_or_none()
        if row is None:
            raise ValueError("pipeline run no longer exists")
        image_ref, image_digest = row
        if image_ref is None and image_digest is None:
            return None
        if image_ref != expected_image_ref or not _is_sha256_digest(image_digest):
            raise ValueError("pipeline artifact checkpoint is incomplete or inconsistent")
        assert isinstance(image_ref, str) and isinstance(image_digest, str)
        return image_ref, image_digest

    async def _create_pipeline_deployment(
        self,
        context: TaskExecutionContext,
        *,
        run_id: str,
        project_id: str,
        environment_id: str,
        server_id: str,
        image_ref: str,
        image_digest: str,
        revision: str,
        compose_content: bytes,
        environment_snapshot: Mapping[str, Any],
    ) -> tuple[str, DeploymentStatus]:
        async with self._deps.session_factory() as session:
            existing = await session.scalar(
                select(Deployment)
                .where(
                    Deployment.run_id == run_id,
                    Deployment.environment_id == environment_id,
                    Deployment.revision == revision,
                )
                .order_by(Deployment.created_at.desc())
                .limit(1)
            )
            compose_text = compose_content.decode("utf-8")
            compose_sha256 = hashlib.sha256(compose_content).hexdigest()
            if existing is not None:
                if (
                    existing.project_id != project_id
                    or existing.server_id != server_id
                    or existing.image_ref != image_ref
                    or existing.image_digest != image_digest
                    or existing.compose_content != compose_text
                    or existing.compose_sha256 != compose_sha256
                    or existing.environment_snapshot != dict(environment_snapshot)
                ):
                    raise ValueError(
                        "existing pipeline deployment does not match its immutable snapshot"
                    )
                deployment = existing
                if existing.status in {
                    DeploymentStatus.PENDING,
                    DeploymentStatus.DEPLOYING,
                }:
                    previous = await _latest_healthy_deployment(
                        session,
                        environment_id=environment_id,
                        exclude_deployment_id=existing.id,
                    )
                    existing.status = DeploymentStatus.DEPLOYING
                    existing.previous_revision = previous.revision if previous else None
                    existing.previous_deployment_id = previous.id if previous else None
                    existing.started_at = existing.started_at or utcnow()
                    existing.finished_at = None
                    existing.error_message = None
            else:
                previous = await _latest_healthy_deployment(
                    session,
                    environment_id=environment_id,
                )
                deployment = Deployment(
                    project_id=project_id,
                    environment_id=environment_id,
                    server_id=server_id,
                    run_id=run_id,
                    status=DeploymentStatus.DEPLOYING,
                    image_ref=image_ref,
                    image_digest=image_digest,
                    revision=revision,
                    previous_revision=previous.revision if previous else None,
                    previous_deployment_id=previous.id if previous else None,
                    compose_content=compose_text,
                    compose_sha256=compose_sha256,
                    environment_snapshot=dict(environment_snapshot),
                    started_at=utcnow(),
                )
                session.add(deployment)
                await session.flush()
            now = utcnow()
            linked = await session.execute(
                update(RunnerTask)
                .where(
                    RunnerTask.id == context.lease.id,
                    RunnerTask.leased_by == context.lease.leased_by,
                    RunnerTask.attempts == context.lease.attempts,
                    RunnerTask.state.in_((TaskState.LEASED, TaskState.RUNNING)),
                    RunnerTask.lease_expires_at > now,
                )
                .values(deployment_id=deployment.id)
            )
            if affected_rows(linked) != 1:
                await session.rollback()
                raise TaskCancelledError(
                    "pipeline lease was lost before deployment could be bound"
                )
            await session.commit()
            return deployment.id, deployment.status

    async def _finish_deployment(
        self,
        deployment_id: str,
        *,
        status: DeploymentStatus,
        succeeded: bool,
        health: Mapping[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        async with self._deps.session_factory() as session:
            result = await session.execute(
                update(Deployment)
                .where(Deployment.id == deployment_id)
                .values(
                    status=status,
                    healthcheck_result=dict(health or {}),
                    error_message=error,
                    finished_at=utcnow(),
                )
            )
            await session.commit()
        if affected_rows(result) == 1:
            await deliver_event(
                self._deps.session_factory,
                self._deps.secrets,
                deployment_result_notification(
                    deployment_id=deployment_id,
                    succeeded=succeeded,
                    status=status.value,
                ),
                actor="system:runner",
            )

    @staticmethod
    def _raise_if_cancelled(context: TaskExecutionContext) -> None:
        if context.cancel_event.is_set():
            raise TaskCancelledError("pipeline was cancelled")


class DeploymentTaskHandler:
    def __init__(self, dependencies: RunnerDependencies) -> None:
        self._deps = dependencies
        self._deployer = ComposeDeployer()

    async def __call__(self, context: TaskExecutionContext) -> None:
        payload = context.lease.payload
        action = _required_string(payload, "action")
        operation_id = context.lease.operation_id
        if operation_id:
            await _set_operation_state(
                self._deps.session_factory, operation_id, ApprovalState.EXECUTING
            )
        try:
            if action == "deploy":
                await self._deploy(context, payload)
            elif action == "rollback":
                await self._rollback(context, payload)
            else:
                raise ValueError(f"unsupported deployment action: {action}")
        except Exception as exc:
            if operation_id:
                await _set_operation_state(
                    self._deps.session_factory,
                    operation_id,
                    ApprovalState.FAILED,
                    result={"error": str(exc)},
                )
            raise
        if operation_id:
            await _set_operation_state(
                self._deps.session_factory,
                operation_id,
                ApprovalState.SUCCEEDED,
                result={"task_id": context.lease.id},
            )

    async def _deploy(self, context: TaskExecutionContext, payload: Mapping[str, Any]) -> None:
        deployment_id = _required_string(payload, "deployment_id")
        project_id = _required_string(payload, "project_id")
        compose_content = _snapshotted_compose_content(payload)
        environment_snapshot = _required_mapping(payload, "environment_snapshot")
        server_id = _required_string(environment_snapshot, "server_id")
        service_name = _required_string(payload, "service_name")
        registry_credentials = await self._registry_credentials(payload, context)
        async with self._deps.session_factory() as session:
            deployment = await session.get(Deployment, deployment_id)
            if deployment is None:
                raise ValueError("deployment record no longer exists")
            if (
                deployment.project_id != project_id
                or deployment.environment_id
                != _required_string(environment_snapshot, "id")
                or deployment.server_id != server_id
                or deployment.image_ref != _required_string(payload, "image_ref")
                or deployment.image_digest != _required_string(payload, "image_digest")
                or deployment.revision != _required_string(payload, "revision")
                or deployment.compose_content != compose_content.decode("utf-8")
                or deployment.compose_sha256 != _required_string(payload, "compose_sha256")
                or deployment.environment_snapshot != dict(environment_snapshot)
            ):
                raise ValueError("deployment does not match the approved immutable snapshot")
            if deployment.status == DeploymentStatus.HEALTHY:
                return
            if deployment.status in {
                DeploymentStatus.FAILED,
                DeploymentStatus.ROLLED_BACK,
            }:
                raise ValueError("deployment already reached a failed terminal state")
            previous = await _latest_healthy_deployment(
                session,
                environment_id=deployment.environment_id,
                exclude_deployment_id=deployment.id,
            )
            deployment.previous_revision = previous.revision if previous else None
            deployment.previous_deployment_id = previous.id if previous else None
            deployment.status = DeploymentStatus.DEPLOYING
            deployment.started_at = deployment.started_at or utcnow()
            deployment.finished_at = None
            deployment.error_message = None
            image_ref = deployment.image_ref
            image_digest = deployment.image_digest
            revision = deployment.revision
            await session.commit()
        rollback_registry_credentials, rollback_image_ref, rollback_redactions = (
            await _load_rollback_registry_context(self._deps, deployment_id)
        )
        context.log.add_secrets(rollback_redactions)
        request = _deployment_request_from_snapshot(
            project_id,
            service_name,
            environment_snapshot,
            compose_content=compose_content,
            image_ref=image_ref,
            image_digest=image_digest,
            revision=revision,
            registry_credentials=registry_credentials,
            rollback_registry_credentials=rollback_registry_credentials,
            rollback_image_ref=rollback_image_ref,
        )
        await self._execute_deployment(context, deployment_id, server_id, request)

    async def _rollback(self, context: TaskExecutionContext, payload: Mapping[str, Any]) -> None:
        deployment_id = _required_string(payload, "deployment_id")
        target_deployment_id = _required_string(payload, "target_deployment_id")
        project_id = _required_string(payload, "project_id")
        target_revision = _required_string(payload, "target_revision")
        target_image_ref = _required_string(payload, "target_image_ref")
        target_image_digest = _required_string(payload, "target_image_digest")
        compose_content = _snapshotted_compose_content(payload)
        environment_snapshot = _required_mapping(payload, "environment_snapshot")
        server_id = _required_string(environment_snapshot, "server_id")
        service_name = _required_string(payload, "service_name")
        registry_credentials = await self._registry_credentials(payload, context)
        async with self._deps.session_factory() as session:
            deployment = await session.get(Deployment, deployment_id)
            target = await session.get(Deployment, target_deployment_id)
            if deployment is None or target is None:
                raise ValueError("deployment record no longer exists")
            if deployment.project_id != project_id:
                raise ValueError("deployment project does not match the approved snapshot")
            immutable_target_matches = (
                target.status == DeploymentStatus.HEALTHY
                and target.environment_id == deployment.environment_id
                and target.project_id == deployment.project_id
                and target.server_id == deployment.server_id
                and target.revision == target_revision
                and target.image_ref == target_image_ref
                and target.image_digest == target_image_digest
                and target.compose_content == compose_content.decode("utf-8")
                and target.compose_sha256 == _required_string(payload, "compose_sha256")
                and target.environment_snapshot == dict(environment_snapshot)
                and deployment.previous_deployment_id == target.id
                and deployment.previous_revision == target.revision
            )
            if deployment.status == DeploymentStatus.ROLLED_BACK:
                if not immutable_target_matches:
                    raise ValueError(
                        "completed rollback no longer matches its approved snapshot"
                    )
                return
            latest_healthy = await _latest_healthy_deployment(
                session,
                environment_id=deployment.environment_id,
            )
            if (
                deployment.status != DeploymentStatus.HEALTHY
                or latest_healthy is None
                or latest_healthy.id != deployment.id
                or not immutable_target_matches
            ):
                raise ValueError("approved rollback target no longer matches its snapshot")
        rollback_registry_credentials, rollback_image_ref, rollback_redactions = (
            await _load_rollback_registry_context(
                self._deps,
                deployment_id,
                baseline_deployment_id=deployment_id,
            )
        )
        context.log.add_secrets(rollback_redactions)
        request = _deployment_request_from_snapshot(
            project_id,
            service_name,
            environment_snapshot,
            compose_content=compose_content,
            image_ref=target_image_ref,
            image_digest=target_image_digest,
            revision=target_revision,
            registry_credentials=registry_credentials,
            rollback_registry_credentials=rollback_registry_credentials,
            rollback_image_ref=rollback_image_ref,
        )
        await self._execute_deployment(
            context,
            deployment_id,
            server_id,
            request,
            success_status=DeploymentStatus.ROLLED_BACK,
            restored_status=DeploymentStatus.HEALTHY,
        )

    async def _registry_credentials(
        self,
        payload: Mapping[str, Any],
        context: TaskExecutionContext,
    ) -> RegistryCredentials | None:
        credential_id = _optional_string(payload, "registry_credential_id")
        if credential_id is None:
            return None
        credentials, redactions = await _load_registry_credentials(
            self._deps, credential_id
        )
        context.log.add_secrets(redactions)
        return credentials

    async def _execute_deployment(
        self,
        context: TaskExecutionContext,
        deployment_id: str,
        server_id: str,
        request: DeploymentRequest,
        *,
        success_status: DeploymentStatus = DeploymentStatus.HEALTHY,
        restored_status: DeploymentStatus = DeploymentStatus.ROLLED_BACK,
    ) -> None:
        ssh_config, ssh_credentials, secret_values = await _load_ssh_target(
            self._deps, server_id
        )
        context.log.add_secrets(secret_values)
        try:
            async with self._deps.ssh.connect(ssh_config, ssh_credentials) as session:
                result = await self._deployer.deploy(request, session=session)
        except Exception as exc:
            rolled_back = isinstance(exc, DeploymentError) and exc.rolled_back
            await _update_deployment(
                self._deps.session_factory,
                self._deps.secrets,
                deployment_id,
                restored_status if rolled_back else DeploymentStatus.FAILED,
                succeeded=False,
                error=str(exc),
            )
            raise
        await _update_deployment(
            self._deps.session_factory,
            self._deps.secrets,
            deployment_id,
            success_status,
            succeeded=True,
            health=result.health,
        )


class ScriptTaskHandler:
    def __init__(self, dependencies: RunnerDependencies) -> None:
        self._deps = dependencies

    async def __call__(self, context: TaskExecutionContext) -> None:
        payload = context.lease.payload
        operation_id = context.lease.operation_id
        if operation_id:
            await _set_operation_state(
                self._deps.session_factory, operation_id, ApprovalState.EXECUTING
            )
        try:
            script_id = _required_string(payload, "script_version_id")
            server_id = _required_string(payload, "server_id")
            expected_hash = _required_string(payload, "script_sha256")
            arguments = _string_mapping(payload.get("arguments", {}), "arguments")
            async with self._deps.session_factory() as session:
                version = await session.get(ScriptVersion, script_id)
                if version is None:
                    raise ValueError("approved script version no longer exists")
                actual_hash = hashlib.sha256(version.content.encode()).hexdigest()
                if not _constant_time_equal(expected_hash, actual_hash):
                    raise ValueError("approved script content hash mismatch")
                content = version.content.encode()
            ssh_config, ssh_credentials, secret_values = await _load_ssh_target(
                self._deps, server_id
            )
            context.log.add_secrets(secret_values)
            remote_path = f"/tmp/light-devops-scripts/{_safe_filename(context.lease.id)}.sh"
            argv = ["/bin/sh", remote_path]
            for name, value in sorted(arguments.items()):
                argv.extend((f"--{name}", value))
            async with self._deps.ssh.connect(ssh_config, ssh_credentials) as session:
                await session.write_file_atomic(remote_path, content, mode=0o700)
                try:
                    result = await session.run(
                        tuple(argv), timeout_seconds=600, check=False
                    )
                    await context.log.write(result.stdout, stage="script")
                    await context.log.write(result.stderr, level="error", stage="script")
                    result.check_returncode()
                finally:
                    await session.remove_file(remote_path)
            if operation_id:
                await _set_operation_state(
                    self._deps.session_factory,
                    operation_id,
                    ApprovalState.SUCCEEDED,
                    result={"exit_status": result.exit_status},
                )
        except Exception as exc:
            if operation_id:
                await _set_operation_state(
                    self._deps.session_factory,
                    operation_id,
                    ApprovalState.FAILED,
                    result={"error": str(exc)},
                )
            raise


class MetricsTaskHandler:
    def __init__(self, dependencies: RunnerDependencies) -> None:
        self._deps = dependencies
        self._collector = HostMetricsCollector()

    async def __call__(self, context: TaskExecutionContext) -> None:
        server_id = _required_string(context.lease.payload, "server_id")
        ssh_config, ssh_credentials, _ = await _load_ssh_target(self._deps, server_id)
        async with self._deps.ssh.connect(ssh_config, ssh_credentials) as session:
            with contextlib.suppress(Exception):
                await cleanup_stale_docker_configs(session)
            metrics = await self._collector.collect(server_id, session)
        async with self._deps.session_factory() as db_session:
            db_session.add(
                HostMetric(
                    server_id=metrics.server_id,
                    cpu_cores=metrics.cpu_cores,
                    cpu_percent=metrics.cpu_percent,
                    memory_total=metrics.memory_total,
                    memory_used=metrics.memory_used,
                    disk_total=metrics.disk_total,
                    disk_used=metrics.disk_used,
                    network_rx=metrics.network_rx,
                    network_tx=metrics.network_tx,
                    collected_at=metrics.collected_at,
                )
            )
            await db_session.execute(
                update(Server)
                .where(Server.id == server_id)
                .values(last_seen_at=metrics.collected_at)
            )
            await db_session.commit()


async def _load_git_credential_secret(
    dependencies: RunnerDependencies, credential_id: str
) -> str:
    async with dependencies.session_factory() as session:
        credential = await session.get(Credential, credential_id)
        if credential is None:
            raise ValueError("credential no longer exists")
        if credential.kind != CredentialKind.GIT:
            raise ValueError("project credential is not a Git credential")
        encrypted = credential.encrypted_secret
    return dependencies.secrets.decrypt(encrypted)


async def _load_registry_credentials(
    dependencies: RunnerDependencies, credential_id: str
) -> tuple[RegistryCredentials, tuple[str, ...]]:
    async with dependencies.session_factory() as session:
        credential = await session.get(Credential, credential_id)
        if credential is None:
            raise ValueError("registry credential no longer exists")
        if credential.kind != CredentialKind.REGISTRY:
            raise ValueError("project credential is not a Registry credential")
        encrypted = credential.encrypted_secret
        details = dict(credential.details)
    secret = dependencies.secrets.decrypt(encrypted)
    credentials = parse_registry_credentials(secret, details)
    redactions = tuple(
        value for value in (credentials.password, secret) if isinstance(value, str) and value
    )
    return credentials, redactions


async def _load_rollback_registry_context(
    dependencies: RunnerDependencies,
    deployment_id: str,
    *,
    baseline_deployment_id: str | None = None,
) -> tuple[RegistryCredentials | None, str | None, tuple[str, ...]]:
    async with dependencies.session_factory() as session:
        deployment = await session.get(Deployment, deployment_id)
        if deployment is None:
            raise ValueError("deployment record no longer exists")
        target_id = baseline_deployment_id or deployment.previous_deployment_id
        baseline = (
            await session.get(Deployment, target_id) if target_id is not None else None
        )
    if baseline is None:
        return None, None, ()
    credential_id = baseline.environment_snapshot.get("registry_credential_id")
    if credential_id is None:
        return None, None, ()
    if not isinstance(credential_id, str) or not credential_id:
        raise ValueError("deployment rollback snapshot has an invalid registry credential")
    try:
        credentials, redactions = await _load_registry_credentials(
            dependencies, credential_id
        )
    except ValueError:
        return None, None, ()
    return credentials, baseline.image_ref, redactions


async def _load_ssh_target(
    dependencies: RunnerDependencies, server_id: str
) -> tuple[SSHConnectionConfig, SSHCredentials, tuple[str, ...]]:
    async with dependencies.session_factory() as session:
        server = await session.get(Server, server_id)
        if server is None or not server.enabled:
            raise ValueError("target server does not exist or is disabled")
        if not server.host_key:
            raise ValueError("target server has no pinned SSH host key")
        if not server.ssh_credential_id:
            raise ValueError("target server has no SSH credential")
        credential = await session.get(Credential, server.ssh_credential_id)
        if credential is None:
            raise ValueError("target server SSH credential no longer exists")
        if credential.kind != CredentialKind.SSH:
            raise ValueError("target server credential is not an SSH credential")
        secret = dependencies.secrets.decrypt(credential.encrypted_secret)
        config = SSHConnectionConfig(
            host=server.host,
            port=server.port,
            username=server.username,
            host_key=server.host_key,
        )
    credentials = parse_ssh_credentials(secret)
    redactions = tuple(
        value
        for value in (credentials.password, credentials.passphrase, secret)
        if isinstance(value, str) and value
    )
    return config, credentials, redactions


async def _set_operation_state(
    session_factory: async_sessionmaker[AsyncSession],
    operation_id: str,
    state: ApprovalState,
    *,
    result: Mapping[str, Any] | None = None,
) -> None:
    values: dict[str, Any] = {"state": state}
    if result is not None:
        values["result"] = dict(result)
    async with session_factory() as session:
        await session.execute(
            update(OperationRequest).where(OperationRequest.id == operation_id).values(**values)
        )
        await session.commit()


async def _latest_healthy_deployment(
    session: AsyncSession,
    *,
    environment_id: str,
    exclude_deployment_id: str | None = None,
) -> Deployment | None:
    query = select(Deployment).where(
        Deployment.environment_id == environment_id,
        Deployment.status == DeploymentStatus.HEALTHY,
    )
    if exclude_deployment_id is not None:
        query = query.where(Deployment.id != exclude_deployment_id)
    return await session.scalar(
        query.order_by(
            Deployment.finished_at.desc().nulls_last(),
            Deployment.created_at.desc(),
        ).limit(1)
    )


async def _update_deployment(
    session_factory: async_sessionmaker[AsyncSession],
    secret_manager: SecretManager,
    deployment_id: str,
    status: DeploymentStatus,
    *,
    succeeded: bool,
    health: Mapping[str, Any] | None = None,
    error: str | None = None,
) -> None:
    async with session_factory() as session:
        result = await session.execute(
            update(Deployment)
            .where(Deployment.id == deployment_id)
            .values(
                status=status,
                healthcheck_result=dict(health or {}),
                error_message=error,
                finished_at=utcnow(),
            )
        )
        await session.commit()
    if affected_rows(result) == 1:
        await deliver_event(
            session_factory,
            secret_manager,
            deployment_result_notification(
                deployment_id=deployment_id,
                succeeded=succeeded,
                status=status.value,
            ),
            actor="system:runner",
        )


def _deployment_request_from_snapshot(
    project_id: str,
    service_name: str,
    environment: Mapping[str, Any],
    *,
    compose_content: bytes,
    image_ref: str,
    image_digest: str,
    revision: str,
    registry_credentials: RegistryCredentials | None,
    rollback_registry_credentials: RegistryCredentials | None = None,
    rollback_image_ref: str | None = None,
) -> DeploymentRequest:
    return DeploymentRequest(
        project_name=_compose_project_name(
            project_id,
            _required_string(environment, "id"),
        ),
        service_name=service_name,
        remote_directory=_required_string(environment, "deploy_path"),
        compose_content=compose_content,
        env_content=_encode_env(_string_mapping(environment.get("env_config", {}), "env_config")),
        image_ref=image_ref,
        image_digest=image_digest,
        revision=revision,
        registry_credentials=registry_credentials,
        rollback_registry_credentials=rollback_registry_credentials,
        rollback_image_ref=rollback_image_ref,
        min_free_bytes=_minimum_free_bytes(environment.get("min_free_bytes")),
        health_check=_health_spec(environment.get("healthcheck", {})),
    )


def _snapshotted_compose_content(payload: Mapping[str, Any]) -> bytes:
    payload_content = payload.get("compose_content")
    if isinstance(payload_content, str) and payload_content:
        encoded = payload_content.encode()
        expected_hash = _required_string(payload, "compose_sha256")
        actual_hash = hashlib.sha256(encoded).hexdigest()
        if not _constant_time_equal(expected_hash, actual_hash):
            raise ValueError("Compose snapshot hash does not match its content")
        return encoded
    raise ValueError("standalone deployment requires an immutable Compose snapshot")


def _compose_content(environment: Mapping[str, Any], source_root: Path) -> bytes:
    if environment.get("compose_source") == "inline":
        return _required_string(environment, "compose_content").encode()
    path = resolve_repository_path(
        source_root, _required_string(environment, "compose_path"), must_exist=True
    )
    if not path.is_file():
        raise ValueError("Compose repository path does not point to a file")
    return path.read_bytes()


def _write_inline_dockerfile(source_root: Path, content: str) -> str:
    generated_directory = Path(
        tempfile.mkdtemp(prefix=".light-devops-", dir=source_root)
    )
    generated = generated_directory / "Dockerfile"
    generated.write_text(content, encoding="utf-8")
    return str(generated.relative_to(source_root)).replace("\\", "/")


def _health_spec(value: object) -> HealthCheckSpec:
    config = _mapping(value or {}, "healthcheck")
    raw_kind = str(config.get("kind") or config.get("type") or "compose").lower()
    try:
        kind = HealthCheckKind(raw_kind)
    except ValueError as exc:
        raise ValueError(f"unsupported health-check kind: {raw_kind}") from exc
    command_value = config.get("command") or ()
    if isinstance(command_value, str):
        raise ValueError("health-check command must be an argument array, not a shell string")
    command = tuple(str(item) for item in command_value)
    return HealthCheckSpec(
        kind=kind,
        timeout_seconds=float(config.get("timeout_seconds", 120)),
        interval_seconds=float(config.get("interval_seconds", 2)),
        url=str(config["url"]) if config.get("url") else None,
        host=str(config["host"]) if config.get("host") else None,
        port=int(config["port"]) if config.get("port") is not None else None,
        command=command,
        expected_http_status_min=int(config.get("status_min", 200)),
        expected_http_status_max=int(config.get("status_max", 399)),
    )


def _encode_env(values: Mapping[str, str]) -> bytes | None:
    if not values:
        return None
    lines: list[str] = []
    for name, value in sorted(values.items()):
        if (
            not name
            or not (name[0].isascii() and (name[0].isalpha() or name[0] == "_"))
            or not all(
                character.isascii() and (character.isalnum() or character == "_")
                for character in name[1:]
            )
        ):
            raise ValueError(f"invalid environment variable name: {name!r}")
        escaped = (
            value.replace("\\", "\\\\")
            .replace("\n", "\\n")
            .replace('"', '\\"')
            .replace("$", "$$")
        )
        lines.append(f'{name}="{escaped}"')
    return ("\n".join(lines) + "\n").encode()


def _tag_image(repository: str, commit_sha: str) -> str:
    repository = repository.split("@", 1)[0]
    slash = repository.rfind("/")
    colon = repository.rfind(":")
    if colon > slash:
        repository = repository[:colon]
    return f"{repository}:{commit_sha.lower()[:12]}"


def _immutable_image_ref(image_ref: str, image_digest: str) -> str:
    repository = image_ref.split("@", 1)[0]
    slash = repository.rfind("/")
    colon = repository.rfind(":")
    if colon > slash:
        repository = repository[:colon]
    return f"{repository}@{image_digest}"


def _is_sha256_digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 71
        and value.startswith("sha256:")
        and all(character in "0123456789abcdef" for character in value[7:])
    )


def _compose_project_name(project_id: str, environment_id: str) -> str:
    value = f"{project_id}-{environment_id}"
    normalised = "".join(
        character.lower() if character.isascii() and character.isalnum() else "-"
        for character in value
    ).strip("-")
    digest = hashlib.sha256(value.encode()).hexdigest()[:12]
    prefix = (normalised or "app")[:50].rstrip("-")
    return f"{prefix}-{digest}"


def _safe_filename(value: str) -> str:
    normalised = "".join(
        character if character.isalnum() or character in "-_" else "_"
        for character in value
    )
    return normalised[:80]


def _required_string(value: Mapping[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise ValueError(f"{key} must be a non-empty string")
    return item


def _optional_string(value: Mapping[str, Any], key: str) -> str | None:
    item = value.get(key)
    if item is None:
        return None
    if not isinstance(item, str) or not item:
        raise ValueError(f"{key} must be a non-empty string or null")
    return item


def _minimum_free_bytes(value: object) -> int:
    if value is None:
        return 512 * 1024 * 1024
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError("min_free_bytes must be an integer")
    result = int(value)
    if result < 0:
        raise ValueError("min_free_bytes cannot be negative")
    return result


def _required_mapping(value: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    return _mapping(value.get(key), key)


def _mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    return value


def _string_mapping(value: object, name: str) -> Mapping[str, str]:
    raw = _mapping(value, name)
    result: dict[str, str] = {}
    for key, item in raw.items():
        if not isinstance(key, str) or not isinstance(item, (str, int, float, bool)):
            raise ValueError(f"{name} must contain scalar string-compatible values")
        result[key] = str(item)
    return result


def _constant_time_equal(expected: str, actual: str) -> bool:
    import hmac

    return hmac.compare_digest(expected.lower(), actual.lower())
