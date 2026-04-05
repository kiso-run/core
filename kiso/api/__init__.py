"""FastAPI router groups for `kiso.main`."""

from .admin import router as admin_router
from .knowledge import router as knowledge_router
from .projects import router as projects_router
from .runtime import router as runtime_router
from .sessions import router as sessions_router

__all__ = [
    "admin_router",
    "knowledge_router",
    "projects_router",
    "runtime_router",
    "sessions_router",
]
