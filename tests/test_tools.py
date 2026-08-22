import os

from xharness.tools import ToolContext
from xharness.tools.bash import bash_tool
from xharness.tools.fs import edit_tool, read_tool, write_tool
from xharness.tools.search import glob_to_regex, glob_tool, grep_tool


def make_ctx(tmp_path, allow=True):
    return ToolContext(cwd=str(tmp_path), approve=lambda _summary: allow)


def test_write_then_read_roundtrip(tmp_path):
    ctx = make_ctx(tmp_path)
    result = write_tool().execute({"path": "sub/hello.txt", "content": "one\ntwo\nthree"}, ctx)
    assert not result.is_error
    shown = read_tool().execute({"path": "sub/hello.txt"}, ctx)
    assert "two" in shown.output
    assert "3\tthree" in shown.output.replace("     ", "")


def test_read_offset_and_limit(tmp_path):
    ctx = make_ctx(tmp_path)
    write_tool().execute({"path": "big.txt", "content": "\n".join(str(i) for i in range(1, 11))}, ctx)
    shown = read_tool().execute({"path": "big.txt", "offset": 4, "limit": 2}, ctx)
    assert "4\t4" in shown.output.replace("     ", "")
    assert "more lines" in shown.output


def test_edit_unique_ambiguous_and_missing(tmp_path):
    ctx = make_ctx(tmp_path)
    file = tmp_path / "edit.txt"
    file.write_text("alpha beta alpha")
    ambiguous = edit_tool().execute({"path": str(file), "old_string": "alpha", "new_string": "gamma"}, ctx)
    assert ambiguous.is_error
    replaced = edit_tool().execute(
        {"path": str(file), "old_string": "alpha", "new_string": "gamma", "replace_all": True}, ctx
    )
    assert not replaced.is_error
    assert file.read_text() == "gamma beta gamma"
    missing = edit_tool().execute({"path": str(file), "old_string": "nope", "new_string": "x"}, ctx)
    assert missing.is_error


def test_mutating_tools_respect_denial(tmp_path):
    ctx = make_ctx(tmp_path, allow=False)
    denied = write_tool().execute({"path": "denied.txt", "content": "x"}, ctx)
    assert denied.is_error and "denied" in denied.output
    assert not os.path.exists(tmp_path / "denied.txt")
    denied_bash = bash_tool().execute({"command": "true"}, ctx)
    assert denied_bash.is_error


def test_bash_runs_and_reports_failure(tmp_path):
    ctx = make_ctx(tmp_path)
    ok = bash_tool().execute({"command": "echo hello"}, ctx)
    assert not ok.is_error and ok.output.strip() == "hello"
    bad = bash_tool().execute({"command": "exit 3"}, ctx)
    assert bad.is_error and "exit code 3" in bad.output


def test_glob_to_regex():
    assert glob_to_regex("src/**/*.py").match("src/a/b/c.py")
    assert glob_to_regex("src/**/*.py").match("src/top.py")
    assert not glob_to_regex("src/**/*.py").match("lib/top.py")
    assert glob_to_regex("*.md").match("README.md")
    assert not glob_to_regex("*.md").match("docs/README.md")
    assert glob_to_regex("file.?y").match("file.py")


def test_glob_and_grep(tmp_path):
    ctx = make_ctx(tmp_path)
    (tmp_path / "notes.md").write_text("the needle is here")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "deep.md").write_text("nothing")
    globbed = glob_tool().execute({"pattern": "**/*.md"}, ctx)
    assert "notes.md" in globbed.output and "sub/deep.md" in globbed.output
    grepped = grep_tool().execute({"pattern": "needle", "include": "**/*.md"}, ctx)
    assert "notes.md:1" in grepped.output
    none = grep_tool().execute({"pattern": "absent-token-xyz"}, ctx)
    assert none.output == "no matches"


def test_grep_skips_binary(tmp_path):
    ctx = make_ctx(tmp_path)
    (tmp_path / "blob.bin").write_bytes(b"needle\x00needle")
    result = grep_tool().execute({"pattern": "needle"}, ctx)
    assert result.output == "no matches"
