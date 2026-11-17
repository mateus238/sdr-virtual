"""Configuração via variáveis de ambiente (ver .env.example)."""
import os


class Cfg:
    def __init__(self):
        self.load()

    def load(self):
        e = os.environ.get
        self.APP_ENV = e("APP_ENV", "development")
        self.DATABASE_PATH = e("DATABASE_PATH", "data/sdr.db")
        self.UPLOAD_DIR = e("UPLOAD_DIR", "data/uploads")
        self.SECRET_KEY = e("SECRET_KEY", "dev-insecure-change-me")
        self.PHONE_PEPPER = e("PHONE_PEPPER", self.SECRET_KEY)
        self.TIMEZONE = e("TIMEZONE", "America/Sao_Paulo")
        # WhatsApp
        self.WHATSAPP_PROVIDER = e("WHATSAPP_PROVIDER", "mock")  # mock | cloud
        self.WA_GRAPH_VERSION = e("WA_GRAPH_VERSION", "v21.0")
        self.WA_VERIFY_TOKEN = e("WA_VERIFY_TOKEN", "change-me-verify-token")
        self.WA_APP_SECRET = e("WA_APP_SECRET", "")
        # Provedor "qr" (WhatsApp Web não oficial via bridge Node/Baileys) — OPCIONAL e sob risco do operador
        self.QR_BRIDGE_URL = e("QR_BRIDGE_URL", "http://127.0.0.1:3100")
        self.QR_BRIDGE_TOKEN = e("QR_BRIDGE_TOKEN", "")
        self.QR_DEFAULT_DAILY_LIMIT = int(e("QR_DEFAULT_DAILY_LIMIT", "50"))
        # IA
        self.ANTHROPIC_API_KEY = e("ANTHROPIC_API_KEY", "")
        self.AI_MODEL = e("AI_MODEL", "claude-sonnet-5-5")
        self.AI_TIMEOUT = int(e("AI_TIMEOUT", "30"))
        # Operação
        self.INBOUND_DEBOUNCE_SECONDS = int(e("INBOUND_DEBOUNCE_SECONDS", "3"))
        self.RETENTION_DAYS = int(e("RETENTION_DAYS", "365"))
        self.SESSION_HOURS = int(e("SESSION_HOURS", "12"))
        self.HANDOFF_WEBHOOK_URL = e("HANDOFF_WEBHOOK_URL", "")
        self.WORKER_POLL_SECONDS = float(e("WORKER_POLL_SECONDS", "1.0"))
        self.PORT = int(e("PORT", "8080"))
        if self.APP_ENV == "production" and self.SECRET_KEY == "dev-insecure-change-me":
            raise RuntimeError("Defina SECRET_KEY em produção")


cfg = Cfg()
