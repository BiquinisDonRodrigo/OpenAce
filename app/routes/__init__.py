from app.routes.play import play_bp
from app.routes.hls import hls_bp, set_manager
from app.routes.playlist import playlist_bp
from app.routes.panel import panel_bp
from app.routes.check import check_bp
from app.routes.eula import eula_bp
from app.routes.plugins_api import plugins_api_bp
from app.routes.auth import auth_bp
from app.routes.setup import setup_bp
from app.utils.ffmpeg_manager import FFmpegManager


def register_blueprints(app):
    manager = FFmpegManager(
        acestream_host=app.config.get("ACESTREAM_HOST", "127.0.0.1"),
        acestream_port=str(app.config.get("ACESTREAM_PORT", "6878")),
    )
    set_manager(manager)
    app.register_blueprint(setup_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(play_bp)
    app.register_blueprint(hls_bp)
    app.register_blueprint(plugins_api_bp)
    app.register_blueprint(playlist_bp)
    app.register_blueprint(panel_bp)
    app.register_blueprint(check_bp)
    app.register_blueprint(eula_bp)
