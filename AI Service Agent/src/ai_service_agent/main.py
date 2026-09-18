import os

import uvicorn

from ai_service_agent.config import get_settings, make_model
from ai_service_agent.harness.server import create_app


def main() -> None:
    settings = get_settings()
    db_path = settings.db_path
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    model = "fake" if settings.fake_model else make_model()
    app = create_app(db_path, model=model, simulate_approval=get_settings().simulate_approval)
    # 默认绑 0.0.0.0：Chatwoot inbox webhook 经宿主机 LAN IP:8000 回推（real-model-smoke §10/§11）；
    # 可用 AI_SERVICE_HOST / AI_SERVICE_PORT 覆盖。
    uvicorn.run(app, host=os.environ.get("AI_SERVICE_HOST", "0.0.0.0"),
                port=int(os.environ.get("AI_SERVICE_PORT", "8000")))


if __name__ == "__main__":
    main()
