import os
from flask import Flask, jsonify
from dotenv import load_dotenv
from extensions import db, login_manager, migrate  # db, login_manager e migrate ficam em extensions.py

load_dotenv()


def create_app():
    app = Flask(__name__, instance_relative_config=True)

    # -------------------------
    # Configurações principais
    # -------------------------
    app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY", "troque-esta-chave")

    # Use a URL literal no .env (sem ${...}) e com senha URL-encodada se tiver caracteres especiais
    app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv(
        "DATABASE_URL",
        "sqlite:///recibotaxi.db",  # fallback para dev local
    )
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "pool_pre_ping": True,   # evita conexões quebradas no pool
        "pool_recycle": 1800,    # recicla a cada 30 min
    }

    # -------------------------
    # Extensões
    # -------------------------
    db.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = "auth.login"
    migrate.init_app(app, db)  # Flask-Migrate habilitado

    # -------------------------
    # Blueprints (importe aqui dentro)
    # -------------------------
    from auth import auth_bp
    from routes import core_bp
    from billing import billing_bp

    app.register_blueprint(auth_bp, url_prefix="/auth")
    app.register_blueprint(core_bp)
    app.register_blueprint(billing_bp)

    # -------------------------
    # Healthcheck simples
    # -------------------------
    @app.get("/health")
    def health():
        return jsonify(status="ok"), 200

    return app


app = create_app()

if __name__ == "__main__":
    # Em produção (Fly) o Gunicorn vai assumir.
    # Deixa host/port configuráveis para rodar local e em containers.
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", 5000))
    app.run(debug=True, host=host, port=port)
