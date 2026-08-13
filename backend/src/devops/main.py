"""提供 `devops-api` 控制台脚本入口并以固定生产参数启动 Uvicorn。"""

from __future__ import annotations

import uvicorn


def run_api() -> None:
    """启动 API Uvicorn 入口；监听参数由环境变量决定。"""
    uvicorn.run("devops.api.main:app", host="0.0.0.0", port=8000, reload=False)


if __name__ == "__main__":
    run_api()
