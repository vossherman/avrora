"""Network-backend selection without importing Qt into the Tk GUI process."""
from urllib.parse import urlsplit


def backend_for(config):
    name = config.transport.strip().lower()
    if name not in ("auto", "qtwebengine", "websockets"):
        raise ValueError("AURORA_TRANSPORT: допустимы auto, qtwebengine, websockets")
    if name == "auto":
        return "qtwebengine" if urlsplit(config.ws_url).scheme == "wss" else "websockets"
    return name
