from threading import Thread

import httpx

from ztsync.cli import _OAuthCallbackServer


def test_oauth_callback_server_captures_valid_callback() -> None:
    server = _OAuthCallbackServer(("127.0.0.1", 0), "state-1", "/oauth/callback")
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]

    try:
        response = httpx.get(
            f"http://127.0.0.1:{port}/oauth/callback?code=code-1&state=state-1",
            timeout=2,
        )
        thread.join(timeout=2)
        assert response.status_code == 200
        assert server.callback_query == {"code": ["code-1"], "state": ["state-1"]}
        assert not thread.is_alive()
    finally:
        if thread.is_alive():
            server.shutdown()
        server.server_close()
