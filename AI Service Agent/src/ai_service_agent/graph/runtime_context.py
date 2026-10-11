"""Runtime dependency context shared by the root graph and write tools."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from ai_service_agent.domain.orders import OrderRepository


class GraphContext(BaseModel):
    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        extra="allow",
    )

    order_repository: OrderRepository | None = None
    approval_service: Any | None = None
    user_id: str = ""
