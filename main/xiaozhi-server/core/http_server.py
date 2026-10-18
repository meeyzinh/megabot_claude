import asyncio
from aiohttp import web
from config.logger import setup_logging
from core.api.ota_handler import OTAHandler
from core.api.vision_handler import VisionHandler
from core.api.admin_handler import AdminHandler
import os
import websockets
from config.config_loader import get_project_dir

TAG = __name__


class SimpleHttpServer:
    def __init__(self, config: dict):
        self.config = config
        self.logger = setup_logging()
        self.ota_handler = OTAHandler(config)
        self.vision_handler = VisionHandler(config)
        self.admin_handler = AdminHandler(config)
        self.admin_dir = os.path.join(get_project_dir(), "admin_web")

    def _get_websocket_url(self, local_ip: str, port: int) -> str:
        """获取websocket地址

        Args:
            local_ip: 本地IP地址
            port: 端口号

        Returns:
            str: websocket地址
        """
        server_config = self.config["server"]
        websocket_config = server_config.get("websocket")

        if websocket_config and "你" not in websocket_config:
            return websocket_config
        else:
            return f"ws://{local_ip}:{port}/xiaozhi/v1/"

    async def _handle_health(self, request):
        return web.json_response({"status": "ok"})

    async def _handle_index(self, request):
        return web.FileResponse(os.path.join(self.admin_dir, "index.html"))

    async def _handle_ws_proxy(self, request):
        """Chế độ 1 cổng (Render): chuyển tiếp WebSocket thiết bị sang WebSocket server nội bộ."""
        if request.headers.get("Upgrade", "").lower() != "websocket":
            return web.Response(text="Server is running\n")
        ws_port = int(self.config["server"].get("port", 8000))
        forward = {
            k: v
            for k, v in request.headers.items()
            if k.lower() in ("device-id", "client-id", "authorization", "protocol-version")
        }
        client = web.WebSocketResponse(max_msg_size=0, heartbeat=None)
        await client.prepare(request)
        try:
            async with websockets.connect(
                f"ws://127.0.0.1:{ws_port}{request.path_qs}",
                additional_headers=forward,
                max_size=None,
                ping_interval=None,
            ) as upstream:

                async def client_to_upstream():
                    async for msg in client:
                        if msg.type == web.WSMsgType.TEXT:
                            await upstream.send(msg.data)
                        elif msg.type == web.WSMsgType.BINARY:
                            await upstream.send(msg.data)
                        else:
                            break

                async def upstream_to_client():
                    async for msg in upstream:
                        if isinstance(msg, str):
                            await client.send_str(msg)
                        else:
                            await client.send_bytes(msg)

                tasks = [
                    asyncio.create_task(client_to_upstream()),
                    asyncio.create_task(upstream_to_client()),
                ]
                _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for t in pending:
                    t.cancel()
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"WebSocket proxy lỗi: {e}")
        finally:
            await client.close()
        return client

    async def start(self):
        try:
            server_config = self.config["server"]
            read_config_from_api = self.config.get("read_config_from_api", False)
            host = server_config.get("ip", "0.0.0.0")
            port = int(server_config.get("http_port", 8003))

            if port:
                app = web.Application()
                app.add_routes([web.get("/health", self._handle_health)])
                self.admin_handler.add_routes(app)
                if server_config.get("single_port_mode"):
                    app.add_routes([web.get("/xiaozhi/v1/", self._handle_ws_proxy)])

                if not read_config_from_api:
                    # 如果没有开启智控台，只是单模块运行，就需要再添加简单OTA接口，用于下发websocket接口
                    app.add_routes(
                        [
                            web.get("/xiaozhi/ota/", self.ota_handler.handle_get),
                            web.post("/xiaozhi/ota/", self.ota_handler.handle_post),
                            web.options(
                                "/xiaozhi/ota/", self.ota_handler.handle_options
                            ),
                            # 下载接口，仅提供 data/bin/*.bin 下载
                            web.get(
                                "/xiaozhi/ota/download/{filename}",
                                self.ota_handler.handle_download,
                            ),
                            web.options(
                                "/xiaozhi/ota/download/{filename}",
                                self.ota_handler.handle_options,
                            ),
                        ]
                    )
                # 添加路由
                app.add_routes(
                    [
                        web.get("/mcp/vision/explain", self.vision_handler.handle_get),
                        web.post(
                            "/mcp/vision/explain", self.vision_handler.handle_post
                        ),
                        web.options(
                            "/mcp/vision/explain", self.vision_handler.handle_options
                        ),
                    ]
                )

                # Giao diện admin (static) - đăng ký cuối cùng để không đè route khác
                if os.path.isdir(self.admin_dir):
                    app.router.add_get("/", self._handle_index)
                    app.router.add_static("/", self.admin_dir, follow_symlinks=False)

                # 运行服务
                runner = web.AppRunner(app)
                await runner.setup()
                site = web.TCPSite(runner, host, port)
                await site.start()

                # 保持服务运行
                while True:
                    await asyncio.sleep(3600)  # 每隔 1 小时检查一次
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"HTTP服务器启动失败: {e}")
            import traceback

            self.logger.bind(tag=TAG).error(f"错误堆栈: {traceback.format_exc()}")
            raise
