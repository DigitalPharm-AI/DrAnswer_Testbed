from __future__ import annotations

from fastapi import APIRouter, Depends

from agent_app.providers.config import describe_model_config, set_runtime_model_tier
from agent_app.security import require_internal_api_token
from shared.schemas import AgentModelConfig, AgentModelTierRequest

router = APIRouter()


@router.get("/agent/model-config", response_model=AgentModelConfig, dependencies=[Depends(require_internal_api_token)])
async def agent_model_config() -> AgentModelConfig:
    return describe_model_config()


@router.post("/agent/model-config", response_model=AgentModelConfig, dependencies=[Depends(require_internal_api_token)])
async def update_agent_model_config(payload: AgentModelTierRequest) -> AgentModelConfig:
    return set_runtime_model_tier(payload.model_tier)
