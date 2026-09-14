import os

import uvicorn

from ai_service_agent.config import get_settings, make_model
from ai_service_agent.harness.server import create_app


def main() -> None:
    settings = get_settings()
    db_path = settings.db_path
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    model = "fake" if settings.fake_model else make_model()
    app = create_app(db_path, model=model)
    uvicorn.run(app, host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
