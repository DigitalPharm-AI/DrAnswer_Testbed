from system_app.routes.agent_api import create_agent_api_router
from system_app.routes.agent_async_api import create_agent_async_api_router
from system_app.routes.chat import create_chat_router
from system_app.routes.health import create_health_router
from system_app.routes.medications import create_medications_router
from system_app.routes.notifications import create_notifications_router
from system_app.routes.nutrition import create_nutrition_router
from system_app.routes.pages import create_pages_router
from system_app.routes.simulation import create_simulation_router

__all__ = [
    "create_agent_api_router",
    "create_agent_async_api_router",
    "create_chat_router",
    "create_health_router",
    "create_medications_router",
    "create_notifications_router",
    "create_nutrition_router",
    "create_pages_router",
    "create_simulation_router",
]
