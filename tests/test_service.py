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


def test_vault_signature_handles_symlinked_vault(tmp_path):
    real_vault = tmp_path / "real-vault"
    daily = real_vault / "daily-notes"
    daily.mkdir(parents=True)
    (daily / "one.md").write_text("- [ ] included\n", encoding="utf-8")
    linked_vault = tmp_path / "vault"
    linked_vault.symlink_to(real_vault, target_is_directory=True)
    settings = Settings(
        vault_path=linked_vault,
        task_paths=["daily-notes"],
        state_dir=tmp_path / "state",
    )

    signature = vault_signature(settings)

    stat = (daily / "one.md").stat()
    assert signature == (("daily-notes/one.md", stat.st_mtime_ns, stat.st_size),)
