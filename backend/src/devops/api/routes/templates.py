"""安全读取平台内置的 Dockerfile 与 Compose 项目模板。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status

from devops.api.deps import CurrentUser
from devops.schemas import TemplateRead

router = APIRouter(prefix="/templates", tags=["templates"])

_TEMPLATE_IDS = ("java-maven", "java-gradle", "node", "python")
_MAX_MANIFEST_BYTES = 64 * 1024
_MAX_TEMPLATE_BYTES = 512 * 1024


@router.get("", response_model=list[TemplateRead])
async def list_templates(_: CurrentUser, request: Request) -> list[TemplateRead]:
    try:
        return _load_templates(request.app.state.settings.template_dir)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Template catalog is invalid",
        ) from exc


def _load_templates(template_dir: Path) -> list[TemplateRead]:
    if not template_dir.exists():
        return []
    root = template_dir.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("template_dir must be a directory")
    compose = _read_text(root, root / "compose" / "compose.yaml", _MAX_TEMPLATE_BYTES)
    templates: list[TemplateRead] = []
    for template_id in _TEMPLATE_IDS:
        directory = root / template_id
        manifest_text = _read_text(
            root, directory / "template.json", _MAX_MANIFEST_BYTES
        )
        manifest = json.loads(manifest_text)
        if not isinstance(manifest, dict):
            raise ValueError(f"template {template_id} manifest must be an object")
        if manifest.get("id") != template_id or manifest.get("dockerfile") != "Dockerfile":
            raise ValueError(f"template {template_id} manifest is not whitelisted")
        dockerfile = _read_text(
            root, directory / "Dockerfile", _MAX_TEMPLATE_BYTES
        )
        templates.append(_template_read(manifest, dockerfile=dockerfile, compose=compose))
    return templates


def _read_text(root: Path, path: Path, max_bytes: int) -> str:
    resolved = path.resolve(strict=True)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError("template path escapes template_dir") from exc
    if not resolved.is_file() or resolved.stat().st_size > max_bytes:
        raise ValueError("template file is missing or too large")
    return resolved.read_text(encoding="utf-8")


def _template_read(
    manifest: dict[str, Any], *, dockerfile: str, compose: str
) -> TemplateRead:
    name = manifest.get("name")
    language = manifest.get("language")
    description = manifest.get("description")
    default_port = manifest.get("default_port")
    health_path = manifest.get("health_path")
    if not isinstance(name, str) or not name:
        raise ValueError("template name is invalid")
    if not isinstance(language, str) or not language:
        raise ValueError("template language is invalid")
    if description is not None and not isinstance(description, str):
        raise ValueError("template description is invalid")
    if default_port is not None and (
        isinstance(default_port, bool)
        or not isinstance(default_port, int)
        or not 1 <= default_port <= 65535
    ):
        raise ValueError("template default_port is invalid")
    if health_path is not None and (
        not isinstance(health_path, str) or not health_path.startswith("/")
    ):
        raise ValueError("template health_path is invalid")
    return TemplateRead(
        id=str(manifest["id"]),
        name=name,
        language=language,
        description=description,
        dockerfile=dockerfile,
        compose=compose,
        default_port=default_port,
        health_path=health_path,
    )
