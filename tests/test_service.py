from ztsync.config import Settings
from ztsync.service import vault_signature


def test_vault_signature_is_limited_to_configured_paths(tmp_path):
    daily = tmp_path / "daily-notes"
    daily.mkdir()
    included = daily / "one.md"
    included.write_text("- [ ] included\n", encoding="utf-8")
    outside = tmp_path / "outside.md"
    outside.write_text("- [ ] ignored\n", encoding="utf-8")

    settings = Settings(
        vault_path=tmp_path,
        task_paths=["daily-notes"],
        state_dir=tmp_path / "state",
    )

    signature = vault_signature(settings)

    assert len(signature) == 1
    assert signature[0][0] == "daily-notes/one.md"
