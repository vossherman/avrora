import os
from pathlib import Path
from dataclasses import dataclass

def _load_dotenv_manual():
    candidates = []
    cwd_env = Path.cwd() / '.env'
    pkg_root_env = Path(__file__).resolve().parents[1] / '.env'
    for p in (cwd_env, pkg_root_env):
        if p not in candidates:
            candidates.append(p)
    for path in candidates:
        try:
            if not path.is_file():
                continue
            for line in path.read_text(encoding='utf-8').splitlines():
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                (key, _, val) = line.partition('=')
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val
        except Exception:
            pass
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass
_load_dotenv_manual()

@dataclass
class Config:
    ws_url: str = 'wss://mirea.aco-avrora.ru/student/arm/'
    protocol_version: int = 213
    login: str = ''
    password: str = ''
    timeout: int = 60
    manual_key: str = ''
    manual_challenge: str = ''
    id_for_sign: int = 0
    neoscrypt_profile: int = 2147485472
    neoscrypt_password_len: int = 256
    user_agent: str = 'Mozilla/5.0 (Windows NT 6.2; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) QtWebEngine/6.10.0 Chrome/126.0.6478.165 Safari/537.36'
    origin: str = 'https://mirea.aco-avrora.ru'
    transport: str = 'auto'

    @classmethod
    def from_env(cls) -> 'Config':

        def _int0(name, default):
            v = os.getenv(name)
            return int(v, 0) if v else default
        return cls(ws_url=os.getenv('AURORA_WS_URL', 'wss://mirea.aco-avrora.ru/student/arm/'), protocol_version=int(os.getenv('AURORA_PROTOCOL_VERSION', '213')), login=os.getenv('AURORA_LOGIN', ''), password=os.getenv('AURORA_PASSWORD', ''), timeout=int(os.getenv('AURORA_TIMEOUT', '60')), manual_key=os.getenv('AURORA_MANUAL_KEY', ''), manual_challenge=os.getenv('AURORA_MANUAL_CHALLENGE', ''), id_for_sign=_int0('AURORA_ID_FOR_SIGN', 0), neoscrypt_profile=_int0('AURORA_NEOSCRYPT_PROFILE', 2147485472), neoscrypt_password_len=_int0('AURORA_NEOSCRYPT_PASSWORD_LEN', 256), user_agent=os.getenv('AURORA_USER_AGENT', cls.user_agent), origin=os.getenv('AURORA_ORIGIN', cls.origin), transport=os.getenv('AURORA_TRANSPORT', cls.transport))

    def validate_auth(self):
        if not self.login or not self.password:
            raise ValueError('AURORA_LOGIN and AURORA_PASSWORD must be set in .env or environment')
