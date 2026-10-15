#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkBuddy 容灾轮换路由引擎 (Failover Router Engine)
提供 OpenAI 兼容的 Wire 协议接缝 (POST /v1/chat/completions)
集成首包防挂起、429秒级容灾、熔断冷却与 macOS 轻通知
"""

import os
import sys
import time
import json
import hmac
import socket
import secrets
import tempfile
import urllib.request
import urllib.error
import threading
import subprocess
from typing import Dict, Any, Optional
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn

DEFAULT_WORKBUDDY_DIR = os.path.expanduser("~/.workbuddy")
DEFAULT_ROUTER_CONFIG = os.path.join(DEFAULT_WORKBUDDY_DIR, "failover_router.json")
DEFAULT_LOG_PATH = os.path.join(DEFAULT_WORKBUDDY_DIR, "logs", "failover_router.log")
DEFAULT_TOKEN_PATH = os.path.join(DEFAULT_WORKBUDDY_DIR, "router_token")
DEFAULT_MODELS_JSON = os.path.join(DEFAULT_WORKBUDDY_DIR, "models.json")

# 旧版本写入 models.json 的固定占位 Key（公开字符串，不能再作为鉴权凭据）
LEGACY_LOCAL_KEY = "sk-workbuddy-router-local"
# 请求体上限：带图片的对话也足够，防止无界读取
MAX_REQUEST_BODY = 64 * 1024 * 1024
# 只接受回环地址作为 Host，抵御 DNS rebinding
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1"}
# 调用方自身请求有误导致的状态码：不代表上游故障，不触发冷却
CLIENT_ERROR_CODES = {400, 413, 415, 422}


def load_or_create_router_token(token_path: str = DEFAULT_TOKEN_PATH) -> str:
    """读取（不存在则生成）本机路由鉴权 Token，文件权限 0600。"""
    try:
        with open(token_path, "r", encoding="utf-8") as f:
            token = f.read().strip()
        if token:
            return token
    except OSError:
        pass
    os.makedirs(os.path.dirname(os.path.abspath(token_path)), exist_ok=True)
    token = "wbr-" + secrets.token_urlsafe(32)
    try:
        fd = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        # 并发创建：以先写入者为准
        with open(token_path, "r", encoding="utf-8") as f:
            return f.read().strip() or token
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(token + "\n")
    return token


def _is_local_router_url(url: str, port: int) -> bool:
    return any(f"{h}:{port}" in (url or "") for h in ("127.0.0.1", "localhost", "[::1]"))


def migrate_models_json_token(token: str, port: int = 8047, models_json_path: str = DEFAULT_MODELS_JSON) -> int:
    """把 models.json 中指向本地路由、仍使用旧占位 Key 的条目升级为本机 Token。返回更新条数。"""
    try:
        with open(models_json_path, "r", encoding="utf-8") as f:
            models = json.load(f)
    except (OSError, ValueError):
        return 0
    if not isinstance(models, list):
        return 0
    changed = 0
    for m in models:
        if isinstance(m, dict) and _is_local_router_url(m.get("url", ""), port) and m.get("apiKey") == LEGACY_LOCAL_KEY:
            m["apiKey"] = token
            changed += 1
    if changed:
        target_dir = os.path.dirname(os.path.abspath(models_json_path))
        try:
            mode = os.stat(models_json_path).st_mode & 0o777
        except OSError:
            mode = 0o600
        fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".json", dir=target_dir)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(models, f, indent=2, ensure_ascii=False)
        if os.name != "nt":
            os.chmod(tmp, mode)
        os.replace(tmp, models_json_path)
    return changed

def send_macos_notification(title: str, message: str):
    """通过系统 osascript 弹出轻量 macOS 通知，静默后台触发"""
    if sys.platform != "darwin":
        return
    def _run():
        try:
            clean_title = title.replace('"', '\\"')
            clean_msg = message.replace('"', '\\"')
            cmd = f'display notification "{clean_msg}" with title "{clean_title}"'
            subprocess.run(["osascript", "-e", cmd], capture_output=True, timeout=3, close_fds=True)
        except Exception:
            pass
    threading.Thread(target=_run, daemon=True).start()

def append_audit_log(entry: Dict[str, Any], log_file: str = DEFAULT_LOG_PATH):
    """写入结构化物理审计日志"""
    try:
        os.makedirs(os.path.dirname(os.path.abspath(log_file)), exist_ok=True)
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass

_wb_cli_module = None

def get_official_auth():
    """动态获取 WorkBuddy 官方当前活跃账号的凭证 (accessToken 与 uid)"""
    global _wb_cli_module
    if _wb_cli_module is None:
        candidates = [
            os.path.expanduser("~/.local/bin/workbuddy"),
            os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "bin", "workbuddy"))
        ]
        import runpy
        for c in candidates:
            if os.path.exists(c):
                try:
                    _wb_cli_module = runpy.run_path(c)
                    break
                except Exception:
                    pass
    if _wb_cli_module:
        try:
            acc = _wb_cli_module["get_active_account"]()
            if acc:
                raw_token = acc.get("auth", {}).get("accessToken")
                uid = acc.get("account", {}).get("uid", "")
                token = _wb_cli_module["resolve_credential_field"](raw_token, "accessToken")
                return token, uid
        except Exception:
            pass
    return None, None

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

class FailoverRequestHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # 内部静默，走结构化审计日志
        pass

    def _send_json_error(self, code: int, message: str):
        body = json.dumps({"error": {"message": message, "code": code}}).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _host_allowed(self) -> bool:
        host = (self.headers.get("Host") or "").strip().lower()
        if host.startswith("["):
            hostname = host[1:].split("]", 1)[0]
        else:
            hostname = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
        return hostname in ALLOWED_HOSTS

    def _authorized(self) -> bool:
        expected = getattr(self.server.router_instance, "auth_token", None)
        if not expected:
            return False
        supplied = ""
        auth = self.headers.get("Authorization") or ""
        if auth.lower().startswith("bearer "):
            supplied = auth[7:].strip()
        elif self.headers.get("X-Api-Key"):
            supplied = self.headers.get("X-Api-Key").strip()
        return bool(supplied) and hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))

    def do_GET(self):
        if not self._host_allowed():
            self._send_json_error(403, "Host not allowed")
            return
        if self.path in ("/health", "/v1/health", "/"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"ok","service":"workbuddy-failover-router"}\n')
            return
        elif self.path in ("/status", "/v1/status"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            status_data = self.server.router_instance.get_status()
            self.wfile.write(json.dumps(status_data).encode("utf-8") + b"\n")
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        if not self._host_allowed():
            self._send_json_error(403, "Host not allowed")
            return
        if self.path not in ("/v1/chat/completions", "/chat/completions"):
            self.send_response(404)
            self.end_headers()
            return
        if not self._authorized():
            # 路由会自动为官方模型注入当前账号的 accessToken，必须鉴权，否则任意本机进程/网页都能消耗积分
            self._send_json_error(401, "Missing or invalid router token (see ~/.workbuddy/router_token)")
            return
        ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if ctype != "application/json":
            self._send_json_error(415, "Content-Type must be application/json")
            return
        try:
            content_length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            self._send_json_error(400, "Invalid Content-Length")
            return
        if content_length < 0 or content_length > MAX_REQUEST_BODY:
            self._send_json_error(413, f"Request body exceeds {MAX_REQUEST_BODY} bytes")
            return
        body_bytes = self.rfile.read(content_length)
        try:
            req_data = json.loads(body_bytes.decode("utf-8"))
        except Exception as e:
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": {"message": f"Invalid JSON payload: {e}", "code": 400}}).encode("utf-8"))
            return

        self.server.router_instance.handle_chat_completions(self, req_data, body_bytes)

class FailoverRouterServer:
    def __init__(self, config: Dict[str, Any], config_path: str = DEFAULT_ROUTER_CONFIG):
        self.config_path = config_path
        self.config = config
        self._last_mtime = 0
        if config_path and os.path.exists(config_path):
            try:
                self._last_mtime = os.path.getmtime(config_path)
            except Exception:
                pass
        self.cooldown_seconds = config.get("cooldown_seconds", 600)
        self.fast_timeout = config.get("fast_model_timeout_seconds", 15)
        self.thinking_timeout = config.get("thinking_model_timeout_seconds", 45)
        self.chain = config.get("chain", [])
        self.upstreams: Dict[str, Any] = config.get("upstreams", {})
        self.cooldowns: Dict[str, float] = {}
        self.lock = threading.Lock()
        # 鉴权 Token：显式配置优先，否则读取/生成 ~/.workbuddy/router_token
        self.auth_token: str = config.get("auth_token") or load_or_create_router_token()
        self.server: Optional[ThreadedHTTPServer] = None
        self.server_thread: Optional[threading.Thread] = None

    def reload_config_if_needed(self):
        if not self.config_path or not os.path.exists(self.config_path):
            return
        try:
            mtime = os.path.getmtime(self.config_path)
            if mtime > self._last_mtime:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    new_cfg = json.load(f)
                with self.lock:
                    self.config = new_cfg
                    self.chain = new_cfg.get("chain", [])
                    self.upstreams = new_cfg.get("upstreams", {})
                    self.cooldown_seconds = new_cfg.get("cooldown_seconds", 600)
                    self.fast_timeout = new_cfg.get("fast_model_timeout_seconds", 15)
                    self.thinking_timeout = new_cfg.get("thinking_model_timeout_seconds", 45)
                self._last_mtime = mtime
        except Exception:
            pass

    def is_in_cooldown(self, model_id: str) -> bool:
        with self.lock:
            exp = self.cooldowns.get(model_id, 0)
            return time.time() < exp

    def trigger_cooldown(self, model_id: str):
        with self.lock:
            self.cooldowns[model_id] = time.time() + self.cooldown_seconds

    def clear_cooldown(self, model_id: str):
        with self.lock:
            self.cooldowns.pop(model_id, None)

    def get_status(self) -> Dict[str, Any]:
        self.reload_config_if_needed()
        with self.lock:
            now = time.time()
            cooldown_status = {
                mid: max(0, int(exp - now))
                for mid, exp in self.cooldowns.items()
                if exp > now
            }
        return {
            "enabled": self.config.get("enabled", True),
            "port": self.config.get("port", 8047),
            "chain": [c.get("id") for c in self.chain],
            "active_cooldowns": cooldown_status,
            "auth_required": True
        }

    def find_model_config(self, model_id: str) -> Optional[Dict[str, Any]]:
        # 1. 优先查阅显式配置的 upstreams 真实上游路由字典
        if hasattr(self, "upstreams") and self.upstreams and model_id in self.upstreams:
            return dict(self.upstreams[model_id])

        # 2. 查阅预设梯队 chain
        for c in self.chain:
            if c.get("id") == model_id:
                return dict(c)

        # 3. 查阅 ~/.workbuddy/models.json（严格防自环：绝对不可将请求转发回本地 8047 端口）
        my_port = self.config.get("port", 8047)
        models_json = os.path.expanduser("~/.workbuddy/models.json")
        if os.path.exists(models_json):
            try:
                with open(models_json, "r", encoding="utf-8") as f:
                    for m in json.load(f):
                        if m.get("id") == model_id:
                            m_url = m.get("url", "")
                            # 防自环：如果 URL 指向本地 8047 代理，则跳过防止递归死循环
                            if f":{my_port}" in m_url or f"127.0.0.1:{my_port}" in m_url or f"localhost:{my_port}" in m_url:
                                continue
                            return {
                                "id": model_id,
                                "name": m.get("name", model_id),
                                "source": "custom",
                                "supportsToolCall": m.get("supportsToolCall", True),
                                "supportsReasoning": m.get("supportsReasoning", False),
                                "url": m_url,
                                "apiKey": m.get("apiKey", "")
                            }
            except Exception:
                pass

        # 4. 默认官方内置模型兜底（走腾讯官方 Copilot v2 接口）
        return {
            "id": model_id,
            "name": model_id,
            "source": "official",
            "supportsToolCall": True,
            "supportsReasoning": True,
            "url": "https://copilot.tencent.com/v2/chat/completions",
            "apiKey": ""
        }

    def start(self, port: Optional[int] = None):
        target_port = port or self.config.get("port", 8047)
        self.server = ThreadedHTTPServer(('127.0.0.1', target_port), FailoverRequestHandler)
        self.server.router_instance = self
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None

    def handle_chat_completions(self, handler: BaseHTTPRequestHandler, req_data: Dict[str, Any], raw_body: bytes):
        """核心容灾轮换分发逻辑"""
        self.reload_config_if_needed()
        # 如果未显式配置 chain，从默认配置重新读取
        chain = self.chain
        if not chain:
            if os.path.exists(DEFAULT_ROUTER_CONFIG):
                try:
                    with open(DEFAULT_ROUTER_CONFIG, "r", encoding="utf-8") as f:
                        saved_cfg = json.load(f)
                        chain = saved_cfg.get("chain", [])
                except Exception:
                    pass

        if not chain:
            handler.send_response(503)
            handler.send_header("Content-Type", "application/json")
            handler.end_headers()
            handler.wfile.write(b'{"error":{"message":"Failover router has no candidate models configured","code":503}}\n')
            return

        is_streaming = req_data.get("stream", True)
        last_error = "All candidates failed"
        # 若所有候选都只返回了"调用方请求有误"类错误，最终原样返回该 4xx，而不是伪装成 503
        last_client_error: Optional[tuple] = None
        upstream_failure_seen = False

        # 支持子 Agent 指定专属模型：若调用方显式指定了具体模型（非 workbuddy-autopilot），
        # 则将该模型置为首位尝试；若发生 429 或故障，自动降级并由预设梯队无缝接管
        req_model = req_data.get("model", "")
        effective_chain = [dict(c) for c in chain]
        if req_model and req_model not in ("workbuddy-autopilot", "auto", "default"):
            match_idx = next((i for i, c in enumerate(effective_chain) if c.get("id") == req_model), None)
            if match_idx is not None:
                fav = effective_chain.pop(match_idx)
                effective_chain.insert(0, fav)
            else:
                custom_c = self.find_model_config(req_model)
                if custom_c:
                    effective_chain.insert(0, custom_c)

        for idx, candidate in enumerate(effective_chain):
            mid = candidate.get("id")
            if not mid:
                continue

            # 1. 熔断检查
            if self.is_in_cooldown(mid):
                continue

            # 2. 超时计算 (思考模型放宽至 45s，普通模型严格 15s)
            is_thinking = candidate.get("supportsReasoning", False) or "thinking" in mid or "pro" in mid or "opus" in mid
            timeout_limit = self.thinking_timeout if is_thinking else self.fast_timeout

            # 3. 构造请求目标与凭据
            url = candidate.get("url")
            api_key = candidate.get("apiKey", "")
            is_official = (candidate.get("source") == "official") or not url

            # 若为官方内置模型且未显式指定 URL
            if is_official:
                url = url or "https://copilot.tencent.com/v2/chat/completions"

            # 保证 URL 以 /chat/completions 结尾 (兼容 /v1 等前缀)
            if not url.endswith("/chat/completions") and not url.endswith("/v2/chat/completions"):
                url = url.rstrip("/") + "/chat/completions"

            # 构造转发 Payload (重写 model 字段)
            forward_payload = dict(req_data)
            forward_payload["model"] = mid

            # 官方接口要求必须 stream: true
            if is_official:
                forward_payload["stream"] = True

            # 若目标模型不支持 reasoning，安全剥离 reasoning_effort 避免上游报错
            if not candidate.get("supportsReasoning", False) and "reasoning_effort" in forward_payload:
                forward_payload.pop("reasoning_effort", None)

            out_body = json.dumps(forward_payload).encode("utf-8")

            # Headers
            headers = {
                "Content-Type": "application/json",
                "User-Agent": "WorkBuddy/5.5.3" if is_official else "WorkBuddy-FailoverRouter/2.0"
            }
            if is_streaming or is_official:
                headers["Accept"] = "text/event-stream"

            if is_official:
                if not api_key:
                    off_token, off_uid = get_official_auth()
                    if off_token:
                        api_key = off_token
                    if off_uid:
                        headers["X-User-Id"] = str(off_uid)

            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"

            # 4. 首包探测与零泄露容灾
            req = urllib.request.Request(url, data=out_body, headers=headers)
            start_time = time.time()
            resp = None

            try:
                resp = urllib.request.urlopen(req, timeout=timeout_limit)
                status_code = resp.status

                # 若上游状态异常
                if status_code in (408, 429, 500, 502, 503, 504):
                    raise urllib.error.HTTPError(url, status_code, "Upstream status error", None, None)

                # 对于流式请求：进行“首包窥探 (Peek First Chunk)”
                if is_streaming:
                    first_chunk = resp.readline()
                    # 首包若是空或异常
                    if not first_chunk:
                        raise ValueError("Upstream closed stream before sending initial chunk")

                    # 探测成功！首包到达，立即将 HTTP 200 响应与首包透传给 WorkBuddy 客户端
                    handler.send_response(200)
                    handler.send_header("Content-Type", "text/event-stream")
                    handler.send_header("Cache-Control", "no-cache")
                    handler.send_header("Connection", "close")
                    handler.send_header("X-WorkBuddy-Routed-Model", mid)
                    handler.end_headers()
                    handler.close_connection = True

                    handler.wfile.write(first_chunk)
                    handler.wfile.flush()

                    # 继续流式透传剩余 Chunks (已发送 200 Headers，严禁向已提交的连接发起二次候选轮换)
                    stream_error = None
                    try:
                        while True:
                            chunk = resp.readline()
                            if not chunk:
                                break
                            handler.wfile.write(chunk)
                            handler.wfile.flush()
                            if b"[DONE]" in chunk:
                                break
                    except (BrokenPipeError, ConnectionResetError):
                        # 客户端主动断开
                        pass
                    except Exception as e:
                        stream_error = e

                    if stream_error:
                        append_audit_log({
                            "timestamp": int(time.time()),
                            "event": "stream_interrupted",
                            "model": mid,
                            "error": str(stream_error)
                        })
                    else:
                        # 成功完成该轮会话，解除该模型冷却
                        self.clear_cooldown(mid)
                        append_audit_log({
                            "timestamp": int(time.time()),
                            "event": "success",
                            "model": mid,
                            "duration_ms": int((time.time() - start_time) * 1000)
                        })
                    return

                else:
                    # 非流式客户端请求
                    if is_official:
                        # 官方接口为 SSE 流，汇聚为单一 JSON 返回
                        content_parts = []
                        reasoning_parts = []
                        usage_obj = None
                        call_id = f"chatcmpl-{int(time.time()*1000)}"
                        for raw_line in resp:
                            line_str = raw_line.decode("utf-8", errors="ignore").strip()
                            if line_str.startswith("data: "):
                                data_str = line_str[6:]
                                if data_str == "[DONE]":
                                    break
                                try:
                                    chunk = json.loads(data_str)
                                    if "id" in chunk:
                                        call_id = chunk["id"]
                                    delta = chunk.get("choices", [{}])[0].get("delta", {})
                                    if "content" in delta and delta["content"]:
                                        content_parts.append(delta["content"])
                                    if "reasoning_content" in delta and delta["reasoning_content"]:
                                        reasoning_parts.append(delta["reasoning_content"])
                                    if "usage" in chunk and chunk["usage"]:
                                        usage_obj = chunk["usage"]
                                except Exception:
                                    pass
                        msg_obj = {
                            "role": "assistant",
                            "content": "".join(content_parts)
                        }
                        if reasoning_parts:
                            msg_obj["reasoning_content"] = "".join(reasoning_parts)
                        out_resp = {
                            "id": call_id,
                            "object": "chat.completion",
                            "created": int(time.time()),
                            "model": mid,
                            "choices": [{
                                "index": 0,
                                "message": msg_obj,
                                "finish_reason": "stop"
                            }],
                            "usage": usage_obj or {
                                "prompt_tokens": 0,
                                "completion_tokens": len("".join(content_parts)),
                                "total_tokens": len("".join(content_parts))
                            }
                        }
                        full_content = json.dumps(out_resp, ensure_ascii=False).encode("utf-8")
                    else:
                        full_content = resp.read()

                    handler.send_response(200)
                    handler.send_header("Content-Type", "application/json")
                    handler.send_header("Content-Length", str(len(full_content)))
                    handler.send_header("X-WorkBuddy-Routed-Model", mid)
                    handler.end_headers()
                    handler.wfile.write(full_content)

                    self.clear_cooldown(mid)
                    append_audit_log({
                        "timestamp": int(time.time()),
                        "event": "success",
                        "model": mid,
                        "duration_ms": int((time.time() - start_time) * 1000)
                    })
                    return

            except urllib.error.HTTPError as he:
                last_error = f"HTTP {he.code}: {he.reason}"
                if he.code in CLIENT_ERROR_CODES:
                    # 请求本身被拒绝（参数错误/体积过大等）：不是上游故障，不能冷却模型，
                    # 否则一次坏请求就能让整条梯队停摆 cooldown_seconds。仍尝试下一个候选以兼容模型差异。
                    try:
                        err_body = he.read() or b""
                    except Exception:
                        err_body = b""
                    last_client_error = (he.code, err_body[:65536])
                    append_audit_log({
                        "timestamp": int(time.time()),
                        "event": "client_error",
                        "reason": f"HTTP {he.code}",
                        "model": mid
                    })
                    continue
                upstream_failure_seen = True
                self.trigger_cooldown(mid)
                next_model = effective_chain[idx + 1].get("id") if idx + 1 < len(effective_chain) else "无备用"
                send_macos_notification(
                    "WorkBuddy 故障自动轮换",
                    f"模型 {mid} 返回 {he.code}，已无缝切换至 {next_model} 继续执行"
                )
                append_audit_log({
                    "timestamp": int(time.time()),
                    "event": "failover",
                    "reason": f"HTTP {he.code}",
                    "failed_model": mid,
                    "cooldown_seconds": self.cooldown_seconds
                })
                continue

            except (socket.timeout, TimeoutError):
                last_error = f"Timeout after {timeout_limit}s on first chunk"
                upstream_failure_seen = True
                self.trigger_cooldown(mid)
                next_model = effective_chain[idx + 1].get("id") if idx + 1 < len(effective_chain) else "无备用"
                send_macos_notification(
                    "WorkBuddy 首包挂起超时",
                    f"模型 {mid} 首包等待超过 {timeout_limit}s，已切换至 {next_model}"
                )
                append_audit_log({
                    "timestamp": int(time.time()),
                    "event": "timeout_failover",
                    "reason": "First-token timeout",
                    "failed_model": mid,
                    "timeout_seconds": timeout_limit
                })
                continue

            except Exception as ex:
                last_error = str(ex)
                upstream_failure_seen = True
                self.trigger_cooldown(mid)
                continue
            finally:
                if resp:
                    try:
                        resp.close()
                    except Exception:
                        pass

        if last_client_error and not upstream_failure_seen:
            code, body = last_client_error
            handler.send_response(code)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", str(len(body)))
            handler.end_headers()
            handler.wfile.write(body)
            return

        # 若全部候选模型均告竭
        handler.send_response(503)
        handler.send_header("Content-Type", "application/json")
        handler.end_headers()
        err_resp = {
            "error": {
                "message": f"All candidate models exhausted. Last error: {last_error}",
                "code": 503
            }
        }
        handler.wfile.write(json.dumps(err_resp).encode("utf-8") + b"\n")
        append_audit_log({
            "timestamp": int(time.time()),
            "event": "all_exhausted",
            "last_error": last_error
        })

def run_standalone_daemon(config_path: str = DEFAULT_ROUTER_CONFIG):
    """运行常驻路由器服务"""
    if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    else:
        cfg = {"enabled": True, "port": 8047, "chain": []}

    port = cfg.get("port", 8047)
    token = cfg.get("auth_token") or load_or_create_router_token()
    migrated = migrate_models_json_token(token, port=port)
    if migrated:
        append_audit_log({"timestamp": int(time.time()), "event": "models_json_token_migrated", "entries": migrated})
        print(f"Upgraded {migrated} models.json entr{'y' if migrated == 1 else 'ies'} to the per-install router token; restart WorkBuddy to apply.")
    router = FailoverRouterServer(dict(cfg, auth_token=token), config_path=config_path)
    print(f"WorkBuddy Failover Router starting on http://127.0.0.1:{port}...")
    router.start(port=port)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping Failover Router...")
        router.stop()

if __name__ == "__main__":
    run_standalone_daemon()
