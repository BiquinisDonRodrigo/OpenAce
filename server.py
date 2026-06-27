from app import create_app

app = create_app()

from app.utils.logging_utils import log_event
log_event("info", "app_started", "core")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8888)
