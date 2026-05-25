import json
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.app_info import router as app_info_router
from app.api.auth import router as auth_router
from app.api.desires import router as desires_router
from app.api.revenuecat import router as revenuecat_router
from app.api.stories import router as stories_router
from app.api.subscription import router as subscription_router
from app.api.users import router as users_router
from app.api.voice import router as voice_router
from app.core.config import settings
from app.core.reminder_scheduler import start_reminder_scheduler, stop_reminder_scheduler


@asynccontextmanager
async def lifespan(app: FastAPI):
    start_reminder_scheduler()
    yield
    stop_reminder_scheduler()


app = FastAPI(
    title="ALREADY API",
    description="Backend for ALREADY — voice cloning and stories.",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(app_info_router, prefix="/api")
app.include_router(auth_router, prefix="/api")
app.include_router(voice_router, prefix="/api")
app.include_router(stories_router, prefix="/api")
app.include_router(desires_router, prefix="/api")
app.include_router(subscription_router, prefix="/api")
app.include_router(revenuecat_router, prefix="/api")
app.include_router(users_router, prefix="/api")


@app.exception_handler(RequestValidationError)
async def request_validation_exception_handler(
    request: Request, exc: RequestValidationError
):
    if request.url.path.endswith("/stories/generate"):
        body_bytes = getattr(exc, "body", None)
        if body_bytes is None:
            try:
                body_bytes = await request.body()
            except Exception:
                body_bytes = b""
        raw = (
            body_bytes.decode("utf-8", errors="replace")
            if body_bytes
            else "<empty>"
        )
        print(
            "[stories.generate] validation failed — raw body:",
            raw,
        )
        print(
            "[stories.generate] validation errors:",
            json.dumps(exc.errors(), default=str),
        )
    return JSONResponse(status_code=422, content={"detail": exc.errors()})


@app.get("/")
async def root():
    return {"app": "ALREADY", "docs": "/docs"}


@app.get("/health")
async def health():
    return {"status": "ok"}
