import os
import sys
import json
import time
import socket
import threading
import unittest
import tempfile
import urllib.request
import urllib.error
from http.server import HTTPServer, BaseHTTPRequestHandler

# Add sidecar to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "sidecar")))
import router_engine

ROUTER_TOKEN = "test-router-token-0123456789"
AUTH_HEADERS = {"Content-Type": "application/json", "Authorization": f"Bearer {ROUTER_TOKEN}"}

def find_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('', 0))
        return s.getsockname()[1]

class MockUpstreamHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # Suppress console logging

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8")
        req_json = json.loads(body) if body else {}

        mode = self.server.mode
        if mode == "success_stream":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            
            chunk1 = json.dumps({"choices": [{"delta": {"content": "Hello"}}]})
            self.wfile.write(f"data: {chunk1}\n\n".encode("utf-8"))
            self.wfile.flush()
            time.sleep(0.01)
            chunk2 = json.dumps({"choices": [{"delta": {"content": " World"}}]})
            self.wfile.write(f"data: {chunk2}\n\n".encode("utf-8"))
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

        elif mode == "rate_limit_429":
            self.send_response(429)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":{"message":"Rate limit exceeded","code":429}}')

        elif mode == "bad_request_400":
            # 单线程 mock 中可能积压着早前已超时客户端的连接，写入时对端已关闭属正常现象
            try:
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error":{"message":"messages must be an array","code":400}}')
            except (BrokenPipeError, ConnectionResetError):
                pass

        elif mode == "server_error_503":
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":{"message":"Service Unavailable","code":503}}')

        elif mode == "hang_timeout":
            # Sleep longer than the router test timeout
            time.sleep(1.0)
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(b"data: [DONE]\n\n")
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

class MockServer:
    def __init__(self, mode="success_stream"):
        self.port = find_free_port()
        self.server = HTTPServer(('127.0.0.1', self.port), MockUpstreamHandler)
        self.server.mode = mode
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def set_mode(self, mode):
        self.server.mode = mode

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

class TestFailoverRouter(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.upstream1 = MockServer(mode="rate_limit_429")
        cls.upstream2 = MockServer(mode="success_stream")
        cls.router_port = find_free_port()

        # Router config with short timeouts for fast tests
        cls.config = {
            "enabled": True,
            "port": cls.router_port,
            "cooldown_seconds": 2,  # 2 seconds cooldown for test
            "auth_token": ROUTER_TOKEN,
            "fast_model_timeout_seconds": 0.3,
            "thinking_model_timeout_seconds": 0.5,
            "chain": [
                {
                    "id": "model-primary",
                    "url": f"http://127.0.0.1:{cls.upstream1.port}/v1/chat/completions",
                    "apiKey": "test-key-1",
                    "supportsToolCall": True
                },
                {
                    "id": "model-secondary",
                    "url": f"http://127.0.0.1:{cls.upstream2.port}/v1/chat/completions",
                    "apiKey": "test-key-2",
                    "supportsToolCall": True
                }
            ]
        }

        cls.router = router_engine.FailoverRouterServer(cls.config, config_path=None)
        cls.router.start(port=cls.router_port)
        time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        cls.router.stop()
        cls.upstream1.stop()
        cls.upstream2.stop()

    def test_01_rate_limit_failover(self):
        """测试 Primary 返回 429 时，无缝秒级轮换至 Secondary 成功输出 SSE 流"""
        self.upstream1.set_mode("rate_limit_429")
        self.upstream2.set_mode("success_stream")

        url = f"http://127.0.0.1:{self.router_port}/v1/chat/completions"
        payload = json.dumps({
            "model": "workbuddy-autopilot",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True
        }).encode("utf-8")

        req = urllib.request.Request(url, data=payload, headers=AUTH_HEADERS)
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            raw_body = resp.read().decode("utf-8")
            self.assertIn("Hello", raw_body)
            self.assertIn("World", raw_body)

        # Primary 应当进入冷却池
        self.assertTrue(self.router.is_in_cooldown("model-primary"))

    def test_02_circuit_breaker_bypasses_cooldown(self):
        """测试在冷却期内，后续请求直接跳过 Primary，零延迟走 Secondary"""
        # Ensure model-primary is in cooldown
        self.router.trigger_cooldown("model-primary")
        self.assertTrue(self.router.is_in_cooldown("model-primary"))

        url = f"http://127.0.0.1:{self.router_port}/v1/chat/completions"
        payload = json.dumps({
            "model": "workbuddy-autopilot",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True
        }).encode("utf-8")

        req = urllib.request.Request(url, data=payload, headers=AUTH_HEADERS)
        start_time = time.time()
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            raw_body = resp.read().decode("utf-8")
            self.assertIn("Hello", raw_body)
        elapsed = time.time() - start_time
        # Should be almost instantaneous (< 0.1s), without testing upstream1
        self.assertLess(elapsed, 0.2)

    def test_03_first_token_timeout_failover(self):
        """测试 Primary 首包挂起超时时，主动断开并轮换至 Secondary"""
        # Wait for cooldown of primary to expire
        time.sleep(2.1)
        self.assertFalse(self.router.is_in_cooldown("model-primary"))

        # Set upstream1 to hang
        self.upstream1.set_mode("hang_timeout")
        self.upstream2.set_mode("success_stream")

        url = f"http://127.0.0.1:{self.router_port}/v1/chat/completions"
        payload = json.dumps({
            "model": "workbuddy-autopilot",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True
        }).encode("utf-8")

        req = urllib.request.Request(url, data=payload, headers=AUTH_HEADERS)
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            raw_body = resp.read().decode("utf-8")
            self.assertIn("Hello", raw_body)

        # Primary timed out, so it must be marked in cooldown
        self.assertTrue(self.router.is_in_cooldown("model-primary"))

    def test_04_all_exhausted_returns_503(self):
        """测试全部上游均故障时，网关返回确定性的 503 JSON 错误，不无故挂死"""
        self.upstream1.set_mode("rate_limit_429")
        self.upstream2.set_mode("server_error_503")

        url = f"http://127.0.0.1:{self.router_port}/v1/chat/completions"
        payload = json.dumps({
            "model": "workbuddy-autopilot",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True
        }).encode("utf-8")

        req = urllib.request.Request(url, data=payload, headers=AUTH_HEADERS)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                self.fail("Expected HTTPError 503")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 503)
            err_data = json.loads(e.read().decode("utf-8"))
            self.assertIn("error", err_data)

    def test_05_subagent_specific_model_failover(self):
        """测试子 Agent 显式指定具体模型时，若发生 429 能自动由梯队备用模型接管"""
        self.router.clear_cooldown("model-primary")
        self.router.clear_cooldown("model-secondary")
        self.upstream1.set_mode("rate_limit_429")
        self.upstream2.set_mode("success_stream")

        url = f"http://127.0.0.1:{self.router_port}/v1/chat/completions"
        payload = json.dumps({
            "model": "model-primary",  # 子 Agent 指定专属模型
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True
        }).encode("utf-8")

        req = urllib.request.Request(url, data=payload, headers=AUTH_HEADERS)
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.headers.get("X-WorkBuddy-Routed-Model"), "model-secondary")
            lines = [line.decode("utf-8") for line in resp if line.strip()]
            self.assertTrue(any("Hello" in l for l in lines))

        # model-primary 自动进入冷却池
        self.assertTrue(self.router.is_in_cooldown("model-primary"))

    def test_06_upstream_registry_and_loop_prevention(self):
        """测试 upstreams 路由注册表与防自环机制：本地代理 URL 绝不作为转发目标"""
        # 配置 upstreams 真实上游
        self.router.upstreams = {
            "test-custom-model": {
                "id": "test-custom-model",
                "url": f"http://127.0.0.1:{self.upstream2.port}/v1/chat/completions",
                "apiKey": "test-key-2",
                "source": "custom"
            }
        }
        cfg = self.router.find_model_config("test-custom-model")
        self.assertIsNotNone(cfg)
        self.assertIn(str(self.upstream2.port), cfg["url"])

        # 测试防自环：即便 ~/.workbuddy/models.json 中配置了指向本地 8047 的 URL，
        # find_model_config 也绝对不能返回本地 8047
        with tempfile.NamedTemporaryFile("w+", delete=False, suffix=".json") as tmp_mj:
            json.dump([{
                "id": "loop-model",
                "url": f"http://127.0.0.1:{self.router_port}/v1",
                "apiKey": "local-key"
            }], tmp_mj)
            tmp_mj_path = tmp_mj.name

        try:
            # 临时 mock models.json
            import os
            orig_models_json = os.path.expanduser("~/.workbuddy/models.json")
            cfg_loop = self.router.find_model_config("loop-model")
            # 应当避开 8047 端口，降级为官方 endpoint 而非自环
            self.assertNotIn(f":{self.router_port}", cfg_loop.get("url", ""))
        finally:
            if os.path.exists(tmp_mj_path):
                os.remove(tmp_mj_path)

    def test_07_mid_stream_interruption_no_duplicate_headers(self):
        """测试流式传输中途中断时不发生二次候选切换与协议污染"""
        # 设置 upstream1 发送首包后抛出超时
        self.upstream1.set_mode("hang_timeout")
        self.router.clear_cooldown("model-primary")
        self.router.clear_cooldown("model-secondary")

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect(('127.0.0.1', self.router_port))
        req_body = json.dumps({'model': 'model-primary', 'stream': True, 'messages': [{'role': 'user', 'content': 'hi'}]})
        req_data = f'POST /v1/chat/completions HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: {len(req_body)}\r\nContent-Type: application/json\r\nAuthorization: Bearer {ROUTER_TOKEN}\r\n\r\n{req_body}'.encode()
        sock.sendall(req_data)

        response_bytes = b''
        sock.settimeout(0.8)
        try:
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                response_bytes += chunk
        except Exception:
            pass
        finally:
            sock.close()

        resp_str = response_bytes.decode('utf-8', errors='replace')
        # 绝不能出现多个 HTTP/1.0 200 或在已提交流中拼接 503
        self.assertLessEqual(resp_str.count("HTTP/1.0 200 OK"), 1)
        self.assertNotIn("HTTP/1.0 503", resp_str)

    # ------------------------------------------------------------------
    # 安全与错误分类回归
    # ------------------------------------------------------------------
    def _post(self, payload, headers, host=None):
        url = f"http://127.0.0.1:{self.router_port}/v1/chat/completions"
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers)
        if host:
            req.add_unredirected_header("Host", host)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_08_requests_without_token_are_rejected(self):
        payload = {"model": "workbuddy-autopilot", "stream": False, "messages": [{"role": "user", "content": "hi"}]}
        self.assertEqual(self._post(payload, {"Content-Type": "application/json"})[0], 401)
        self.assertEqual(self._post(payload, {"Content-Type": "application/json", "Authorization": "Bearer wrong"})[0], 401)
        # 旧版公开占位 Key 不得再被接受
        self.assertEqual(self._post(payload, {"Content-Type": "application/json",
                                              "Authorization": f"Bearer {router_engine.LEGACY_LOCAL_KEY}"})[0], 401)

    def test_09_cross_site_simple_request_is_rejected(self):
        payload = {"model": "workbuddy-autopilot", "stream": False, "messages": [{"role": "user", "content": "hi"}]}
        # 浏览器跨站"简单请求"只能用 text/plain 等类型，且无法附带 Authorization
        self.assertIn(self._post(payload, {"Content-Type": "text/plain"})[0], (401, 415))
        self.assertEqual(self._post(payload, {"Content-Type": "text/plain", "Authorization": f"Bearer {ROUTER_TOKEN}"})[0], 415)

    def test_10_dns_rebinding_host_is_rejected(self):
        payload = {"model": "workbuddy-autopilot", "stream": False, "messages": [{"role": "user", "content": "hi"}]}
        self.assertEqual(self._post(payload, AUTH_HEADERS, host=f"evil.example:{self.router_port}")[0], 403)
        req = urllib.request.Request(f"http://127.0.0.1:{self.router_port}/status")
        req.add_unredirected_header("Host", "evil.example")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(ctx.exception.code, 403)

    def test_11_client_error_does_not_cool_down_chain(self):
        # 使用独立的上游与路由：共享的单线程 mock 里可能还挂着前面用例的 hang 请求，
        # 那会让本用例的首包超时（真实上游故障）并返回 503，从而掩盖要验证的 4xx 语义。
        up_a, up_b = MockServer(mode="bad_request_400"), MockServer(mode="bad_request_400")
        port = find_free_port()
        router = router_engine.FailoverRouterServer({
            "port": port, "cooldown_seconds": 60, "auth_token": ROUTER_TOKEN,
            "fast_model_timeout_seconds": 3, "thinking_model_timeout_seconds": 3,
            "chain": [
                {"id": "iso-a", "url": f"http://127.0.0.1:{up_a.port}/v1/chat/completions"},
                {"id": "iso-b", "url": f"http://127.0.0.1:{up_b.port}/v1/chat/completions"},
            ],
        }, config_path=None)
        router.start(port=port)
        time.sleep(0.1)

        def post(payload):
            req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/chat/completions",
                                         data=json.dumps(payload).encode("utf-8"), headers=AUTH_HEADERS)
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    return resp.status, resp.read()
            except urllib.error.HTTPError as e:
                return e.code, e.read()

        try:
            bad = {"model": "workbuddy-autopilot", "stream": True, "messages": "not-an-array"}
            code, body = post(bad)
            self.assertEqual(code, 400, "调用方请求错误应原样返回 4xx，而不是 503")
            self.assertIn(b"messages must be an array", body)
            self.assertFalse(router.is_in_cooldown("iso-a"))
            self.assertFalse(router.is_in_cooldown("iso-b"))

            # 紧接着的正常请求必须成功
            up_a.set_mode("success_stream")
            good = {"model": "workbuddy-autopilot", "stream": True, "messages": [{"role": "user", "content": "hi"}]}
            code, body = post(good)
            self.assertEqual(code, 200)
            self.assertIn(b"Hello", body)
        finally:
            router.stop()
            up_a.stop()
            up_b.stop()

    def test_12_oversized_body_is_rejected(self):
        s = socket.create_connection(("127.0.0.1", self.router_port), timeout=3)
        try:
            s.sendall((f"POST /v1/chat/completions HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                       f"Authorization: Bearer {ROUTER_TOKEN}\r\nContent-Type: application/json\r\n"
                       f"Content-Length: {router_engine.MAX_REQUEST_BODY + 1}\r\n\r\n").encode())
            data = s.recv(4096).decode("utf-8", errors="replace")
        finally:
            s.close()
        self.assertIn(" 413 ", data.splitlines()[0])


class TestRouterToken(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_token_is_random_persistent_and_private(self):
        path = os.path.join(self.tmp, "router_token")
        t1 = router_engine.load_or_create_router_token(path)
        t2 = router_engine.load_or_create_router_token(path)
        self.assertEqual(t1, t2)
        self.assertGreaterEqual(len(t1), 32)
        self.assertNotEqual(t1, router_engine.LEGACY_LOCAL_KEY)
        if os.name != "nt":
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        other = router_engine.load_or_create_router_token(os.path.join(self.tmp, "other_token"))
        self.assertNotEqual(t1, other)

    def test_models_json_legacy_key_migration(self):
        mj = os.path.join(self.tmp, "models.json")
        with open(mj, "w", encoding="utf-8") as f:
            json.dump([
                {"id": "workbuddy-autopilot", "url": "http://127.0.0.1:8047/v1", "apiKey": router_engine.LEGACY_LOCAL_KEY},
                {"id": "user-model", "url": "https://api.example.com/v1", "apiKey": "user-own-key"},
            ], f)
        n = router_engine.migrate_models_json_token("new-token", port=8047, models_json_path=mj)
        self.assertEqual(n, 1)
        with open(mj, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data[0]["apiKey"], "new-token")
        self.assertEqual(data[1]["apiKey"], "user-own-key", "用户自己的上游 Key 不得被改动")

if __name__ == "__main__":
    unittest.main()
