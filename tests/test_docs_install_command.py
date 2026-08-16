from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_readme_has_one_canonical_dsh_installer_command():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    command = "curl -fsSL https://raw.githubusercontent.com/windyslime/DeepSee/main/scripts/install-dsh-dsv.sh | bash"
    assert command in readme
    assert "api_key =" not in readme[readme.index("## DSH 专用一键安装"):]
    assert "/Users/" not in readme


def test_guides_are_dsh_only_and_do_not_embed_credentials():
    for filename in ("docs/DSH-DSV-INSTALL.zh.md", "docs/DSH-DSV-INSTALL.md"):
        text = (ROOT / filename).read_text(encoding="utf-8")
        assert "install-dsh-dsv.sh" in text
        assert "<DSV public key>" in text
        assert "DEEPSEEK_API_KEY" not in text or "留在" in text
        assert "/Users/" not in text
