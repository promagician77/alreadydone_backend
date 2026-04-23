from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

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

app.include_router(auth_router, prefix="/api")
app.include_router(voice_router, prefix="/api")
app.include_router(stories_router, prefix="/api")
app.include_router(desires_router, prefix="/api")
app.include_router(subscription_router, prefix="/api")
app.include_router(revenuecat_router, prefix="/api")
app.include_router(users_router, prefix="/api")


@app.get("/")
async def root():
    return {"app": "ALREADY", "docs": "/docs"}


@app.get("/health")
async def health():
    return {"status": "ok"}
