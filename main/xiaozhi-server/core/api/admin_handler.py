"""
Backend quản trị robot Xiaozhi cho giao diện admin_web (lấy từ Web_templates).

- Lưu dữ liệu vào file JSON (data/admin/db.json), không cần database ngoài.
- Xác thực bằng JWT trong cookie HttpOnly (giao diện gọi fetch cùng origin, không gửi header Authorization).
- Tài khoản admin lấy từ biến môi trường ADMIN_USERNAME / ADMIN_PASSWORD.
"""

import os
import json
import time
import uuid
import hmac
import hashlib
import secrets
import random
from datetime import datetime, timezone

import jwt
from aiohttp import web

from config.logger import setup_logging
from config.config_loader import get_project_dir
from core.utils import online_registry

TAG = __name__
COOKIE_NAME = "megabot_session"
TOKEN_TTL = 7 * 24 * 3600
PUBLIC_API = {
    "/api/auth/login",
    "/api/login",
    "/api/auth/register",
    "/api/auth/accounts",
    "/api/captcha",
}
# Các trường không bao giờ trả về cho trình duyệt
SECRET_FIELDS = {"password_hash", "password_salt"}
# Các trường client được phép ghi vào bản ghi thiết bị
DEVICE_WRITABLE = {
    "name", "initial", "prompt", "custom_prompt", "system_prompt", "model", "model_id",
    "voice", "voice_id", "voice_name", "volume", "language", "voice_speed", "voice_pitch",
    "memory_enabled", "openai_api_key", "deepseek_api_key", "gemini_api_key",
    "device_code", "device_mac", "badge_color", "initial_color",
}


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _hash_password(password: str, salt: str = None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), 120_000
    ).hex()
    return digest, salt


def _verify_password(password: str, digest: str, salt: str) -> bool:
    if not digest or not salt:
        return False
    check, _ = _hash_password(password, salt)
    return hmac.compare_digest(check, digest)


class AdminStore:
    """Kho dữ liệu JSON đơn giản, ghi nguyên tử."""

    DEFAULT = {
        "users": [],
        "devices": [],
        "speakers": [],
        "memories": [],
        "documents": [],
        "feedback": [],
        "wss_requests": [],
        "commands": [],
    }

    def __init__(self, path):
        self.path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.data = json.loads(json.dumps(self.DEFAULT))
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    self.data.update(json.load(f))
            except Exception:
                pass

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)


class AdminHandler:
    def __init__(self, config: dict):
        self.config = config
        self.logger = setup_logging()
        data_dir = os.path.join(get_project_dir(), config.get("log", {}).get("data_dir", "data"))
        self.store = AdminStore(os.path.join(data_dir, "admin", "db.json"))
        self.secret = config["server"].get("auth_key") or secrets.token_hex(32)
        self._ensure_admin()

    # ---------- khởi tạo ----------
    def _ensure_admin(self):
        username = os.environ.get("ADMIN_USERNAME", "admin").strip() or "admin"
        password = os.environ.get("ADMIN_PASSWORD", "").strip()
        users = self.store.data["users"]
        existing = next((u for u in users if u["username"] == username), None)
        if existing and not password:
            return
        generated = False
        if not password:
            password = secrets.token_urlsafe(9)
            generated = True
        digest, salt = _hash_password(password)
        if existing:
            existing.update(password_hash=digest, password_salt=salt, is_admin=True)
        else:
            users.append(
                {
                    "id": uuid.uuid4().hex,
                    "username": username,
                    "password_hash": digest,
                    "password_salt": salt,
                    "is_admin": True,
                    "created_at": _now_iso(),
                }
            )
        self.store.save()
        if generated:
            self.logger.bind(tag=TAG).warning(
                f"Chưa đặt ADMIN_PASSWORD. Đã tạo mật khẩu admin tạm thời cho '{username}': {password}"
            )

    # ---------- auth ----------
    def _issue_token(self, subject, role, device_id=None):
        payload = {"sub": subject, "role": role, "exp": int(time.time()) + TOKEN_TTL}
        if device_id:
            payload["device_id"] = device_id
        return jwt.encode(payload, self.secret, algorithm="HS256")

    def _session(self, request):
        token = request.cookies.get(COOKIE_NAME)
        if not token:
            auth = request.headers.get("Authorization", "")
            if auth.lower().startswith("bearer "):
                token = auth[7:].strip()
        if not token:
            return None
        try:
            return jwt.decode(token, self.secret, algorithms=["HS256"])
        except Exception:
            return None

    @web.middleware
    async def auth_middleware(self, request, handler):
        path = request.path
        if path.startswith("/api/") and path not in PUBLIC_API:
            session = self._session(request)
            if session is None:
                return web.json_response({"error": "Chưa đăng nhập"}, status=401)
            request["session"] = session
        return await handler(request)

    def _json_response(self, data, status=200, token=None):
        resp = web.json_response(data, status=status)
        if token:
            resp.set_cookie(
                COOKIE_NAME, token, max_age=TOKEN_TTL, httponly=True, samesite="Lax",
                secure=os.environ.get("COOKIE_SECURE", "").lower() in ("1", "true"),
            )
        return resp

    @staticmethod
    def _public(record):
        out = {k: v for k, v in record.items() if k not in SECRET_FIELDS}
        if "device_code" in record or "device_mac" in record:
            out["is_online"] = online_registry.is_online(
                record.get("device_mac"), record.get("device_code"), record.get("id")
            )
        return out

    async def _body(self, request):
        try:
            data = await request.json()
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _find_device(self, ident):
        ident = str(ident or "")
        for d in self.store.data["devices"]:
            if ident in (d["id"], d.get("device_code"), d.get("device_mac")):
                return d
        return None

    def _visible_devices(self, session):
        if session.get("role") == "admin":
            return self.store.data["devices"]
        return [d for d in self.store.data["devices"] if d["id"] == session.get("device_id")]

    def _can_access(self, session, device):
        return session.get("role") == "admin" or session.get("device_id") == device["id"]

    # ---------- /api/auth ----------
    async def login(self, request):
        body = await self._body(request)
        username = str(body.get("username", "")).strip()
        password = str(body.get("password", ""))
        if not username or not password:
            return self._json_response({"success": False, "error": "Thiếu tài khoản hoặc mật khẩu"}, 400)

        user = next((u for u in self.store.data["users"] if u["username"] == username), None)
        if user and _verify_password(password, user["password_hash"], user["password_salt"]):
            token = self._issue_token(user["username"], "admin")
            return self._json_response(
                {"success": True, "token": token, "is_admin": True, "role": "admin",
                 "user": {"username": user["username"]}},
                token=token,
            )

        device = self._find_device(username) or next(
            (d for d in self.store.data["devices"] if d.get("name") == username), None
        )
        if device and _verify_password(password, device.get("password_hash"), device.get("password_salt")):
            token = self._issue_token(device["id"], "device", device["id"])
            return self._json_response(
                {"success": True, "token": token, "is_admin": False, "role": "device",
                 "device": self._public(device)},
                token=token,
            )
        return self._json_response({"success": False, "error": "Sai tài khoản hoặc mật khẩu"}, 401)

    async def logout(self, request):
        resp = web.json_response({"success": True})
        resp.del_cookie(COOKIE_NAME)
        return resp

    async def register(self, request):
        body = await self._body(request)
        name = str(body.get("name", "")).strip()
        code = str(body.get("device_code", "")).strip()
        password = str(body.get("password", ""))
        if not name or not code or len(password) < 6:
            return self._json_response(
                {"success": False, "error": "Cần tên, mã thiết bị và mật khẩu tối thiểu 6 ký tự"}, 400
            )
        if self._find_device(code):
            return self._json_response({"success": False, "error": "Mã thiết bị đã tồn tại"}, 409)
        device = self._new_device(name, code, "", password)
        token = self._issue_token(device["id"], "device", device["id"])
        return self._json_response(
            {"success": True, "token": token, "device": self._public(device)}, token=token
        )

    async def accounts(self, request):
        session = self._session(request)
        if session is None:
            return web.json_response([])
        devices = self._visible_devices(session)
        return web.json_response(
            [self._public(d) for d in devices]
        )

    async def change_password(self, request):
        session = request["session"]
        body = await self._body(request)
        password = str(body.get("password") or body.get("new_password") or "")
        if len(password) < 6:
            return self._json_response({"success": False, "error": "Mật khẩu tối thiểu 6 ký tự"}, 400)
        digest, salt = _hash_password(password)
        device_id = body.get("device_id")
        if device_id:
            device = self._find_device(device_id)
            if not device or not self._can_access(session, device):
                return self._json_response({"success": False, "error": "Không có quyền"}, 403)
            device.update(password_hash=digest, password_salt=salt)
        elif session.get("role") == "admin":
            user = next((u for u in self.store.data["users"] if u["username"] == session["sub"]), None)
            if not user:
                return self._json_response({"success": False, "error": "Không tìm thấy admin"}, 404)
            user.update(password_hash=digest, password_salt=salt)
        else:
            return self._json_response({"success": False, "error": "Thiếu device_id"}, 400)
        self.store.save()
        return self._json_response({"success": True})

    # ---------- /api/devices ----------
    def _new_device(self, name, code, mac, password=None):
        if not code:
            code = "".join(random.choice("0123456789") for _ in range(6))
        digest, salt = _hash_password(password) if password else (None, None)
        device = {
            "id": uuid.uuid4().hex[:12],
            "name": name,
            "initial": (name[:1] or "R").upper(),
            "device_code": code,
            "device_mac": mac,
            "prompt": "",
            "language": "vi",
            "voice": "vi-VN-HoaiMyNeural",
            "is_online": False,
            "created_at": _now_iso(),
            "password_hash": digest,
            "password_salt": salt,
        }
        self.store.data["devices"].append(device)
        self.store.save()
        return device

    async def list_devices(self, request):
        return web.json_response([self._public(d) for d in self._visible_devices(request["session"])])

    async def create_device(self, request):
        body = await self._body(request)
        name = str(body.get("name", "")).strip()
        if not name:
            return web.json_response({"error": "Thiếu tên thiết bị"}, status=400)
        code = str(body.get("device_code", "")).strip()
        mac = str(body.get("device_mac", "")).strip()
        if (code and self._find_device(code)) or (mac and self._find_device(mac)):
            return web.json_response({"error": "Thiết bị đã tồn tại"}, status=409)
        device = self._new_device(name, code, mac, body.get("password") or None)
        return web.json_response(self._public(device))

    def _device_or_error(self, request):
        device = self._find_device(request.match_info["id"])
        if not device:
            return None, web.json_response({"error": "Không tìm thấy thiết bị"}, status=404)
        if not self._can_access(request["session"], device):
            return None, web.json_response({"error": "Không có quyền"}, status=403)
        return device, None

    async def get_device(self, request):
        device, err = self._device_or_error(request)
        return err or web.json_response(self._public(device))

    async def update_device(self, request):
        device, err = self._device_or_error(request)
        if err:
            return err
        body = await self._body(request)
        for key in DEVICE_WRITABLE & body.keys():
            device[key] = body[key]
        if body.get("password"):
            device["password_hash"], device["password_salt"] = _hash_password(str(body["password"]))
        device["updated_at"] = _now_iso()
        self.store.save()
        return web.json_response(self._public(device))

    async def delete_device(self, request):
        device, err = self._device_or_error(request)
        if err:
            return err
        self.store.data["devices"].remove(device)
        self.store.save()
        return web.json_response({"success": True})

    # ---------- danh sách đơn giản: speakers / memories / documents ----------
    def _collection(self, name, device_scoped=False):
        async def list_items(request):
            items = self.store.data[name]
            if device_scoped:
                dev = request.query.get("deviceId")
                if dev:
                    items = [i for i in items if i.get("device_id") == dev]
                limit = int(request.query.get("limit", 100) or 100)
                items = items[-limit:]
            return web.json_response(items)

        async def create_item(request):
            body = await self._body(request)
            item = {"id": uuid.uuid4().hex[:12], "created_at": _now_iso(), **body}
            if name == "documents":
                item.setdefault("title", "Tài liệu")
                item.setdefault("status", "ready")
                item["chunkCount"] = max(1, len(str(body.get("content", ""))) // 800 + 1)
            self.store.data[name].append(item)
            self.store.save()
            return web.json_response(item)

        async def delete_item(request):
            item_id = request.match_info["id"]
            before = len(self.store.data[name])
            self.store.data[name] = [i for i in self.store.data[name] if i.get("id") != item_id]
            if len(self.store.data[name]) == before:
                return web.json_response({"error": "Không tìm thấy"}, status=404)
            self.store.save()
            return web.json_response({"success": True})

        return list_items, create_item, delete_item

    # ---------- feedback / yêu cầu / lệnh robot ----------
    def _append_only(self, name):
        async def handler(request):
            body = await self._body(request)
            self.store.data[name].append({"id": uuid.uuid4().hex[:12], "created_at": _now_iso(), **body})
            self.store.save()
            return web.json_response({"success": True, "status": "ok"})

        return handler

    def _robot_command(self, kind):
        async def handler(request):
            body = await self._body(request)
            device = self._find_device(body.get("device_id"))
            if not device or not self._can_access(request["session"], device):
                return web.json_response({"error": "Không tìm thấy thiết bị"}, status=404)
            cmds = self.store.data["commands"]
            cmds.append(
                {"id": uuid.uuid4().hex[:12], "kind": kind, "device_id": device["id"],
                 "payload": {k: v for k, v in body.items() if k != "device_id"},
                 "status": "queued", "created_at": _now_iso()}
            )
            del cmds[:-500]
            self.store.save()
            return web.json_response({"status": "ok", "queued": True})

        return handler

    async def system_status(self, request):
        """Chẩn đoán: trả về cấu hình ASR/LLM/TTS đang dùng và API key đã đặt hay chưa (không lộ giá trị key)."""
        cfg = self.config
        selected = cfg.get("selected_module", {})

        def has_key(module):
            conf = cfg.get(module, {}).get(selected.get(module), {})
            key = str(conf.get("api_key", "") or "")
            return bool(key) and "你" not in key and "web key" not in key

        return web.json_response(
            {
                "selected_module": selected,
                "asr_key_set": has_key("ASR"),
                "llm_key_set": has_key("LLM"),
                "tts_voice": cfg.get("TTS", {}).get(selected.get("TTS"), {}).get("voice"),
                "online_devices": online_registry.online_ids(),
            }
        )

    async def not_implemented(self, request):
        return web.json_response(
            {"error": "Chức năng này chưa được hỗ trợ ở backend", "path": request.path}, status=501
        )

    # ---------- đăng ký route ----------
    def add_routes(self, app: web.Application):
        app.middlewares.append(self.auth_middleware)
        r = app.router
        r.add_post("/api/auth/login", self.login)
        r.add_post("/api/login", self.login)
        r.add_post("/api/auth/logout", self.logout)
        r.add_post("/api/auth/register", self.register)
        r.add_get("/api/auth/accounts", self.accounts)
        r.add_post("/api/auth/change-password", self.change_password)
        r.add_post("/api/admin/change-password", self.change_password)

        r.add_get("/api/system/status", self.system_status)
        r.add_get("/api/devices", self.list_devices)
        r.add_post("/api/devices", self.create_device)
        r.add_get("/api/devices/{id}", self.get_device)
        r.add_put("/api/devices/{id}", self.update_device)
        r.add_delete("/api/devices/{id}", self.delete_device)

        for path, name, scoped in (
            ("/api/speakers", "speakers", False),
            ("/api/memories", "memories", True),
            ("/api/knowledge-base/documents", "documents", False),
        ):
            list_items, create_item, delete_item = self._collection(name, scoped)
            r.add_get(path, list_items)
            r.add_post(path, create_item)
            r.add_delete(path + "/{id}", delete_item)

        r.add_post("/api/feedback", self._append_only("feedback"))
        r.add_post("/api/wss-request", self._append_only("wss_requests"))
        for kind in ("control", "display", "speak"):
            r.add_post(f"/api/robot/{kind}", self._robot_command(kind))
        # Mọi /api còn lại của template (english-tutor, ocr, music...) chưa có backend
        r.add_route("*", "/api/{tail:.*}", self.not_implemented)
