"""本地启动入口：uv run python main.py"""

import os

import uvicorn

from release_pipeline.app import app

if __name__ == "__main__":
    uvicorn.run(
        app,
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
    )
