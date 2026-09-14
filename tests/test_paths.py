"""Where the exe thinks it lives, decided by the checkout it sits in and never by its own directory.

Frozen or not is passed in, not patched onto `sys`, because `paths.FROZEN` is read once at import
and a patch after that would test nothing. One case patches the module constant to prove the
default path reads it.
"""
import pytest

from c4x import paths


def install_at(tmp_path, name):
    root = tmp_path / name
    (root / "tools").mkdir(parents=True)
    (root / "tools" / "harvest.mjs").write_text("// the marker\n", encoding="utf-8")
    return root


def test_not_frozen_is_the_checkout_for_every_root():
    assert paths.install_root(frozen=False) == paths.REPO_ROOT
    assert paths.bundle_root() == paths.REPO_ROOT
    assert (paths.REPO_ROOT / "tools" / "harvest.mjs").is_file(), "the marker is the real harvester"


def test_the_store_and_the_api_take_their_root_from_the_install():
    import c4x.api.main as main
    from c4x import store
    assert store.ROOT == paths.REPO_ROOT
    assert main.ROOT == paths.REPO_ROOT


def test_frozen_inside_a_checkout_finds_it_above_the_exe(tmp_path):
    root = install_at(tmp_path, "checkout")
    exe = root / "dist" / "c4x-api" / "c4x-api.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    assert paths.install_root(frozen=True, executable=str(exe), env={}) == root


def test_frozen_outside_any_checkout_exits_2_with_the_reason(tmp_path, capsys):
    exe = tmp_path / "elsewhere" / "c4x-api.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    with pytest.raises(SystemExit) as stopped:
        paths.install_root(frozen=True, executable=str(exe), env={})
    assert stopped.value.code == 2
    err = capsys.readouterr().err
    assert "inside a c4x install" in err and "replaces Python, not node" in err
    assert "c4x.exe" in err and "<root>/dist/c4x/" in err, "the message names the exe as built"


def test_a_store_inside_a_checkout_names_that_checkout(tmp_path):
    root = install_at(tmp_path, "checkout")
    (root / "data").mkdir()
    (root / "data" / "context.db").write_bytes(b"")
    exe = tmp_path / "elsewhere" / "c4x-api.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    env = {"C4X_DB": str(root / "data" / "context.db")}
    assert paths.install_root(frozen=True, executable=str(exe), env=env) == root


def test_a_store_outside_any_checkout_does_not_rescue_a_stray_exe(tmp_path):
    exe = tmp_path / "elsewhere" / "c4x-api.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    (tmp_path / "stray").mkdir()
    env = {"C4X_DB": str(tmp_path / "stray" / "context.db")}
    with pytest.raises(SystemExit):
        paths.install_root(frozen=True, executable=str(exe), env=env)


def test_the_exe_beside_the_marker_is_an_install_too(tmp_path):
    """`find_install` starts AT the exe's directory, not above it."""
    root = install_at(tmp_path, "flat")
    exe = root / "c4x-api.exe"
    exe.write_bytes(b"")
    assert paths.install_root(frozen=True, executable=str(exe), env={}) == root


def test_the_bundle_is_the_extraction_directory_when_frozen(tmp_path, monkeypatch):
    import sys
    monkeypatch.setattr(paths, "FROZEN", True)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "_internal"), raising=False)
    assert paths.bundle_root() == tmp_path / "_internal"
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    assert paths.bundle_root() == paths.REPO_ROOT, "frozen with no extraction dir falls back"


def test_the_defaults_read_the_module_and_the_interpreter(tmp_path, monkeypatch):
    import sys
    root = install_at(tmp_path, "checkout")
    exe = root / "dist" / "c4x-api" / "c4x-api.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    monkeypatch.setattr(paths, "FROZEN", True)
    monkeypatch.setattr(sys, "executable", str(exe))
    monkeypatch.delenv("C4X_DB", raising=False)
    assert paths.install_root() == root
