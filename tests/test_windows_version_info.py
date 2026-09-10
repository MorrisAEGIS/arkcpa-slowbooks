"""The Windows exe gets a version resource generated from app/__init__.py
(issue #106). The generator is import-free so the spec can run it."""

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "version_info", ROOT / "packaging" / "windows" / "version_info.py"
)
version_info = importlib.util.module_from_spec(spec)
spec.loader.exec_module(version_info)


def test_version_comes_from_the_app_package():
    import app

    assert version_info.read_version(ROOT / "app" / "__init__.py") == app.__version__


def test_version_tuple_is_four_integers():
    assert version_info.version_tuple("2.10.0") == (2, 10, 0, 0)
    assert version_info.version_tuple("2.10.0rc1") == (2, 10, 0, 1)
    assert version_info.version_tuple("3.0") == (3, 0, 0, 0)


def test_rendered_resource_names_the_product(tmp_path):
    out = tmp_path / "version_info.txt"
    v = version_info.write(ROOT / "app" / "__init__.py", out)
    text = out.read_text(encoding="utf-8")
    assert text.startswith("# Generated")
    assert "VSVersionInfo(" in text
    assert f"StringStruct('FileVersion', '{v}')" in text
    assert f"StringStruct('ProductVersion', '{v}')" in text
    assert "StringStruct('ProductName', 'SlowBooks Pro 2026')" in text
    assert "StringStruct('OriginalFilename', 'SlowBooksPro.exe')" in text
    # PyInstaller evaluates this file: it must be a valid Python expression
    # over these names.
    names = {
        n: (lambda *a, **k: (a, k))
        for n in (
            "VSVersionInfo",
            "FixedFileInfo",
            "StringFileInfo",
            "StringTable",
            "StringStruct",
            "VarFileInfo",
            "VarStruct",
        )
    }
    eval(compile(text, "version_info.txt", "eval"), names)
