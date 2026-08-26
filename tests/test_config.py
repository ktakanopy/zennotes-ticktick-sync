from ztsync.config import Settings


def test_oauth_defaults_use_server_listener() -> None:
    settings = Settings(
        vault_path="/vault",
        task_paths=["inbox/ticktick.md"],
        state_dir="/state",
    )

    assert settings.ticktick_oauth_bind_host == "0.0.0.0"
    assert settings.ticktick_oauth_port == 8765
    assert settings.ticktick_redirect_uri == "http://192.168.15.14:8765/oauth/callback"
