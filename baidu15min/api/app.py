"""FastAPI 应用组装：实例、静态资源挂载、校验异常统一处理、路由注册。"""
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import config
from .routes import router

# 静态资源目录 = 包内 static/（不依赖运行 cwd）
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

app = FastAPI(title="15分钟便民生活圈体检报告", version="v4")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.exception_handler(RequestValidationError)
async def _validation_exc_handler(request: Request, exc: RequestValidationError):
    """Pydantic 校验失败统一转 400 {error}，兼容前端 fetchJson 契约"""
    msgs = []
    for e in (exc.errors() or [])[:3]:
        loc = ".".join(str(x) for x in e.get("loc", []) if x != "body")
        msgs.append("%s: %s" % (loc, e.get("msg", "")))
    return JSONResponse(status_code=400, content={"error": "请求参数格式非法：%s" % "; ".join(msgs)})


app.include_router(router, prefix="")