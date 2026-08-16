from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_readme_has_one_canonical_dsh_installer_command():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    heading = "## DSH 新手一键配置"
    command = "curl -fsSL https://raw.githubusercontent.com/windyslime/DeepSee/main/scripts/install-dsh-dsv.sh | bash"
    assert command in readme
    assert "/Users/" not in readme
    start = readme.index(heading)
    end = readme.index("## 安装", start)
    section = readme[start:end]
    assert start < readme.index("## 安装")
    assert "public key" in section
    assert "Y/n/c" in section
    assert "api_key =" not in section
    assert "--configure" in section
    assert "--no-configure" in section
    assert "--verify" in section
    assert ".credentials.yaml" not in section
    assert "/dev/tty" not in section


def test_contributing_contains_dsh_installer_technical_contract():
    text = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    assert "dsh-credentials.py" in text
    assert ".credentials.yaml" in text
    assert "/dev/tty" in text
    assert "0600" in text
    assert "test_dsh_installer_script.py" in text


def test_guides_are_dsh_only_and_do_not_embed_credentials():
    for filename in ("docs/DSH-DSV-INSTALL.zh.md", "docs/DSH-DSV-INSTALL.md"):
        text = (ROOT / filename).read_text(encoding="utf-8")
        assert "install-dsh-dsv.sh" in text
        assert "<DSV public key>" in text
        assert "DEEPSEEK_API_KEY" not in text or "留在" in text
        assert "/Users/" not in text
        assert "--configure" in text
        assert "--no-configure" in text
        assert ".credentials.yaml" in text
