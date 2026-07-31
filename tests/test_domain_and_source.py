from dataclasses import fields

from codehistory.codegraph_reader import FunctionDef as LegacyFunctionDef
from codehistory.domain.knowledge import ApiEndpoint, FunctionDef
from codehistory.infrastructure.source_filesystem import FileSystemSourceProvider
from codehistory.knowledge import ApiEndpoint as LegacyApiEndpoint


def test_legacy_domain_imports_are_reexports():
    assert LegacyFunctionDef is FunctionDef
    assert LegacyApiEndpoint is ApiEndpoint
    assert [field.name for field in fields(LegacyFunctionDef)] == [
        field.name for field in fields(FunctionDef)
    ]


def test_source_provider_reads_text_and_bounded_snippets(tmp_path):
    source = tmp_path / "src" / "example.py"
    source.parent.mkdir()
    source.write_text("one\ntwo\nthree\n", encoding="utf-8")
    provider = FileSystemSourceProvider(tmp_path)

    assert provider.read_text("src/example.py") == "one\ntwo\nthree\n"
    assert provider.snippet("src/example.py", 2, 99) == "two\nthree"
    assert provider.snippet("src/example.py", 0, 1) is None
    assert provider.snippet("src/example.py", 10, 12) is None


def test_source_provider_handles_missing_bad_encoding_and_escape(tmp_path):
    (tmp_path / "bad.txt").write_bytes(b"\xff\xfe")
    provider = FileSystemSourceProvider(tmp_path)

    assert provider.read_text("missing.py") is None
    assert provider.read_text("bad.txt") is None
    assert provider.read_text("../outside.txt") is None
