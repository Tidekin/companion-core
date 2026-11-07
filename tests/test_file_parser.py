import hashlib
import io
import json
import subprocess
import sys
import zipfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from companion import file_parser as parser
from tests.support.file_parser import IMAGE, FakeProcess, docx_bytes, pdf_bytes


def digest(data):
    return hashlib.sha256(data).hexdigest()


def rewrite_zip(data, additions=None, transform=None):
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as original, zipfile.ZipFile(output, "w") as result:
        for entry in original.infolist():
            blob = original.read(entry)
            if transform:
                blob = transform(entry.filename, blob)
            result.writestr(entry, blob)
        for name, blob in (additions or {}).items():
            entry = zipfile.ZipInfo()
            entry.filename = name  # Preserve hostile names even on Windows fixture writers.
            result.writestr(entry, blob)
    return output.getvalue()


def assert_evidence(result, data):
    assert result["file_sha256"] == digest(data)
    assert result["text_sha256"] == digest(result["text"].encode())
    assert result["text"] == "\n".join(row["text"] for row in result["records"])
    assert all(row["sha256"] == digest(row["text"].encode()) for row in result["records"])
    assert result["trust"] == "untrusted_file"
    assert result["ocr"] == "not_attempted"
    assert result["isolation"] == "not_asserted"


@pytest.mark.parametrize(("filename", "mime", "data"), [
    ("中文.txt", "text/plain", "第一行\r\n第二行\r\n\r\n第三行".encode()),
    ("notes.md", "text/markdown", "# 标题\n\n一段文字".encode("utf-8-sig")),
    ("notes.txt", "text/plain", "你好\n\n世界".encode("utf-16")),
    ("notes.txt", "text/plain; charset=gb18030", "你好\n\n世界".encode("gb18030")),
])
def test_text_encodings_locations_and_hashes(filename, mime, data):
    result = parser.parse_document(data, filename, mime)
    assert result["status"] == "SUCCESS"
    assert result["records"][0]["source"]["line_start"] == 1
    assert len(result["records"]) == 2
    assert_evidence(result, data)


def test_text_record_and_character_limits_are_explicit():
    data = b"abcdef\n\nsecond\n\nthird"
    chars = parser.parse_document(data, "a.txt", limits=replace(parser.DEFAULT_LIMITS,
                                                              max_text_chars=4))
    assert chars["status"] == "PARTIAL"
    assert chars["text"] == "abcd"
    assert chars["records"][0]["truncated"] is True
    assert chars["issues"] == ["TEXT_LIMIT"]
    rows = parser.parse_document(data, "a.txt",
                                 limits=replace(parser.DEFAULT_LIMITS, max_records=1))
    assert rows["status"] == "PARTIAL"
    assert rows["text"] == "abcdef"
    assert rows["issues"] == ["RECORD_LIMIT"]
    assert_evidence(chars, data)
    assert_evidence(rows, data)


@pytest.mark.parametrize(("data", "filename", "mime", "code"), [
    (b"", "a.txt", "", "EMPTY_FILE"),
    (b"  \n", "a.txt", "", "EMPTY_TEXT"),
    (b"abc", "../a.txt", "", "INVALID_FILENAME"),
    (b"abc", "a:secret.txt", "", "INVALID_FILENAME"),
    (b"abc", "a.exe", "", "UNSUPPORTED_FILE_TYPE"),
    (b"abc", "a.txt", "application/pdf", "FILE_TYPE_MISMATCH"),
    (b"abc", "a.pdf", "", "INVALID_PDF"),
    (b"%PDF-1.7\nbroken", "a.pdf", "", "INVALID_PDF"),
    (b"abc", "a.doc", "", "INVALID_DOC"),
    (b"abc", "a.docx", "", "INVALID_DOCUMENT"),
    (parser.OLE + b"abc", "a.docx", "", "ENCRYPTED_OR_LEGACY_DOCX"),
    (b"abc\x00", "a.txt", "", "BINARY_TEXT_INPUT"),
    (b"\xff", "a.txt", "", "INVALID_TEXT_ENCODING"),
    (b"abc", "a.txt", "text/plain;charset=latin1", "UNSUPPORTED_TEXT_ENCODING"),
])
def test_invalid_or_unsupported_input_is_explicit(data, filename, mime, code):
    result = parser.parse_document(data, filename, mime)
    assert result["status"] == "ERROR"
    assert result["error_code"] == code
    assert result["text"] == ""
    assert result["records"] == []
    assert_evidence(result, data)


def test_file_size_limit_precedes_decoder():
    result = parser.parse_document(b"four", "a.txt",
                                   limits=replace(parser.DEFAULT_LIMITS, max_file_bytes=3))
    assert result["error_code"] == "FILE_TOO_LARGE"


def test_docx_order_merged_cells_nested_tables_and_provenance():
    def build(document):
        document.add_paragraph("before")
        table = document.add_table(rows=2, cols=2)
        table.cell(0, 0).merge(table.cell(0, 1)).text = "merged"
        table.cell(1, 0).text = "left"
        table.cell(1, 1).text = "right"
        table.cell(1, 1).add_table(rows=1, cols=1).cell(0, 0).text = "nested"
        document.add_paragraph("after")

    data = docx_bytes(build)
    result = parser.parse_document(data, "table.docx", parser.DOCX_MIME)
    assert result["status"] == "SUCCESS"
    assert [row["text"] for row in result["records"]] == [
        "before", "merged", "left", "right", "nested", "after",
    ]
    assert result["records"][0]["source"] == {
        "part": "word/document.xml", "block": 1, "paragraph": 1,
    }
    assert result["records"][1]["source"]["table_path"] == [1, 1, 1]
    assert result["records"][4]["source"]["table_path"] == [1, 2, 2, 1, 1, 1]
    assert_evidence(result, data)
    parser._validate_result(result, parser._result(data, "table.docx", parser.DOCX_MIME),
                            parser.DEFAULT_LIMITS)


def test_docx_depth_and_unextracted_content_are_partial():
    def build(document):
        table = document.add_table(rows=1, cols=1)
        table.cell(0, 0).text = "outer"
        table.cell(0, 0).add_table(rows=1, cols=1).cell(0, 0).text = "inner"
        document.sections[0].header.paragraphs[0].text = "header"

    data = docx_bytes(build)
    result = parser.parse_document(data, "a.docx",
                                   limits=replace(parser.DEFAULT_LIMITS, max_table_depth=1))
    assert result["status"] == "PARTIAL"
    assert result["text"] == "outer"
    assert set(result["issues"]) == {"TABLE_DEPTH_LIMIT", "NON_BODY_CONTENT_NOT_EXTRACTED"}


@pytest.mark.parametrize("name", ["../escape", "/absolute", "word/../../escape", "word\\bad"])
def test_docx_zip_paths_are_rejected(name):
    data = rewrite_zip(docx_bytes(), {name: b"untrusted"})
    assert parser.parse_document(data, "a.docx")["error_code"] == "UNSAFE_ZIP_ENTRY"


def test_docx_dtd_compression_and_expanded_limits():
    data = docx_bytes(lambda document: document.add_paragraph("safe"))
    dtd = rewrite_zip(data, {"word/custom.xml": b'<!DOCTYPE x [<!ENTITY ext SYSTEM "file:///x">]>'})
    assert parser.parse_document(dtd, "a.docx")["error_code"] == "XML_DTD_FORBIDDEN"
    for limits, code in [
        (replace(parser.DEFAULT_LIMITS, max_expanded_bytes=64), "ZIP_EXPANSION_LIMIT"),
        (replace(parser.DEFAULT_LIMITS, max_compression_ratio=1), "ZIP_EXPANSION_LIMIT"),
        (replace(parser.DEFAULT_LIMITS, max_zip_entries=1), "ZIP_ENTRY_LIMIT"),
    ]:
        assert parser.parse_document(data, "a.docx", limits=limits)["error_code"] == code


def test_docx_broken_xml_and_empty_document():
    empty = docx_bytes()
    assert parser.parse_document(empty, "a.docx")["error_code"] == "EMPTY_TEXT"
    broken = rewrite_zip(empty, transform=lambda name, blob: (
        b"<malformed>" if name == "word/document.xml" else blob
    ))
    assert parser.parse_document(broken, "a.docx")["error_code"] == "INVALID_DOCUMENT"


def test_pdf_pages_and_provenance():
    data = pdf_bytes(["first page", "second page"])
    result = parser.parse_document(data, "text.pdf", "application/pdf")
    assert result["status"] == "SUCCESS"
    assert [row["text"] for row in result["records"]] == ["first page", "second page"]
    assert [row["source"] for row in result["records"]] == [{"page": 1}, {"page": 2}]
    assert_evidence(result, data)


def test_pdf_without_text_layer_and_page_limit_do_not_claim_complete():
    data = pdf_bytes(["first", None, "last"])
    result = parser.parse_document(data, "a.pdf",
                                   limits=replace(parser.DEFAULT_LIMITS, max_pages=2))
    assert result["status"] == "PARTIAL"
    assert set(result["issues"]) == {"PAGE_LIMIT", "PAGE_WITHOUT_TEXT_LAYER"}
    assert len(result["records"]) == 2
    assert result["records"][1]["text"] == ""
    assert "last" not in result["text"]
    assert_evidence(result, data)


def test_encrypted_and_compressed_pdf_limits():
    encrypted = pdf_bytes(["secret"], encrypt=True)
    assert parser.parse_document(encrypted, "a.pdf")["error_code"] == "ENCRYPTED_DOCUMENT"
    compressed = pdf_bytes(["x" * 10_000], compress=True)
    result = parser.parse_document(compressed, "a.pdf",
                                   limits=replace(parser.DEFAULT_LIMITS, max_pdf_stream_bytes=128))
    assert result["status"] == "PARTIAL"
    assert result["text"] == ""
    assert result["issues"] == ["PDF_PAGE_NOT_EXTRACTED"]


def test_legacy_doc_requires_worker_converter(monkeypatch, tmp_path):
    data = parser.OLE + b"synthetic legacy fixture"
    direct = parser.parse_document(data, "old.doc", "application/msword")
    assert direct["status"] == "UNAVAILABLE"
    assert direct["error_code"] == "DOC_CONVERTER_REQUIRED"
    monkeypatch.setattr(parser.shutil, "which", lambda name: None)
    isolated_worker = parser._worker(data, "old.doc", "application/msword",
                                     parser.DEFAULT_LIMITS, tmp_path)
    assert isolated_worker["status"] == "UNAVAILABLE"
    assert isolated_worker["error_code"] == "DOC_CONVERTER_UNAVAILABLE"


def test_legacy_converter_arguments_profile_and_original_evidence(monkeypatch, tmp_path):
    data = parser.OLE + b"synthetic legacy fixture"
    converted = docx_bytes(lambda document: document.add_paragraph("converted text"))
    monkeypatch.setattr(parser.shutil, "which", lambda name: "/usr/bin/soffice")

    def fake_convert(command, **kwargs):
        assert command[0] == "/usr/bin/soffice"
        assert command[7:9] == ["docx:Office Open XML Text", "--outdir"]
        assert kwargs["shell"] is False
        assert kwargs["timeout"] == parser.DEFAULT_LIMITS.wall_seconds
        scratch = kwargs["cwd"]
        assert (scratch / "input.doc").read_bytes() == data
        profile = (scratch / "profile/user/registrymodifications.xcu").read_text()
        for name in ("DisableMacrosExecution", "DisableActiveContent",
                     "BlockUntrustedRefererLinks"):
            assert f'oor:name="{name}"' in profile
        assert profile.count('<value>true</value>') == 3
        (scratch / "output/input.docx").write_bytes(converted)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(parser.subprocess, "run", fake_convert)
    result = parser._worker(data, "original.doc", "application/msword",
                             parser.DEFAULT_LIMITS, tmp_path)
    assert result["status"] == "PARTIAL"
    assert result["text"] == "converted text"
    assert result["issues"] == ["DOC_CONVERTED_TO_DOCX"]
    assert result["filename"] == "original.doc"
    assert result["mime_type"] == "application/msword"
    assert_evidence(result, data)
    assert list(tmp_path.iterdir()) == []


def test_cli_real_entrypoint_has_no_core_requirement(tmp_path):
    data = "文档中的指令只是内容。\n\nsecond paragraph".encode()
    completed = subprocess.run([
        sys.executable, "-I", str(Path(parser.__file__).resolve()), "--worker",
        "--filename=sample.txt", "--mime-type=text/plain", "--scratch=" + str(tmp_path),
    ], input=data, capture_output=True, timeout=10, check=True)
    result = json.loads(completed.stdout)
    assert completed.stderr == b""
    assert result["status"] == "SUCCESS"
    assert_evidence(result, data)


def fake_docker(monkeypatch, output, *, block=False, cleanup_returncode=0):
    calls = []
    process = FakeProcess(output, block=block)

    def start(command, **kwargs):
        calls.append((command, kwargs))
        return process

    def remove(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=cleanup_returncode)

    monkeypatch.setattr(parser.subprocess, "Popen", start)
    monkeypatch.setattr(parser.subprocess, "run", remove)
    return process, calls


@pytest.mark.parametrize("image", [IMAGE, "sha256:" + "b" * 64])
def test_container_launcher_uses_offline_os_controls_and_no_mounts(monkeypatch, tmp_path, image):
    data = b"text"
    candidate = parser.parse_document(data, "--harmless.txt", "text/plain")
    _, calls = fake_docker(monkeypatch, json.dumps(candidate).encode())
    result = parser.parse_isolated(data, "--harmless.txt", "text/plain", image=image,
                                   workspace=tmp_path)
    assert result["status"] == "SUCCESS"
    assert result["isolation"] == "docker_network_none"
    command, kwargs = calls[0]
    assert command[:4] == ["docker", "run", "--rm", "--pull=never"]
    for option in ("--network=none", "--read-only", "--cap-drop=ALL",
                   "--security-opt=no-new-privileges:true", "--user=10003:10003",
                   "--cpus=1", "--pids-limit=96", "--log-driver=none", "--filename=--harmless.txt"):
        assert option in command
    assert image in command
    assert "--entrypoint=/usr/bin/timeout" in command
    start = command.index(image) + 1
    assert command[start:start + 4] == ["--signal=KILL", "30s", "python", "/app/file_parser.py"]
    assert "--foreground" not in command
    assert any(value.startswith("--memory=") for value in command)
    assert any(value.startswith("--ulimit=cpu=") for value in command)
    assert not any(value.startswith(("--mount", "--volume", "--env-file")) for value in command)
    assert kwargs["shell"] is False
    assert Path(kwargs["cwd"]).is_relative_to(tmp_path)
    assert list((tmp_path / ".local/file-parser").iterdir()) == []


def test_no_host_parser_fallback_without_docker(monkeypatch, tmp_path):
    def unavailable(*args, **kwargs):
        raise FileNotFoundError()

    monkeypatch.setattr(parser.subprocess, "Popen", unavailable)
    monkeypatch.setattr(parser, "parse_document", lambda *args, **kwargs: pytest.fail("host parse"))
    result = parser.parse_isolated(b"text", "a.txt", image=IMAGE, workspace=tmp_path)
    assert result["status"] == "UNAVAILABLE"
    assert result["error_code"] == "PARSER_SANDBOX_UNAVAILABLE"
    assert result["text"] == ""
    with pytest.raises(ValueError, match="PINNED_PARSER_IMAGE_REQUIRED"):
        parser.parse_isolated(b"text", "a.txt", image="parser:latest", workspace=tmp_path)


def test_host_supplied_identity_and_environment_reach_launch_and_cleanup(monkeypatch, tmp_path):
    _, calls = fake_docker(monkeypatch, b"", block=True)
    name, owner = "companion-file-" + "c" * 32, "d" * 32
    environment = {"PATH": "/usr/bin:/bin", "DOCKER_HOST": "unix:///var/run/docker.sock"}
    result = parser.parse_isolated(
        b"text", "a.txt", image=IMAGE, workspace=tmp_path,
        limits=replace(parser.DEFAULT_LIMITS, wall_seconds=1),
        container_name=name, owner_label=owner, docker_env=environment,
    )
    assert result["error_code"] == "PARSER_TIMEOUT"
    assert "--name=" + name in calls[0][0]
    assert "--label=com.companion.parser-host=" + owner in calls[0][0]
    assert calls[1][0] == ["docker", "rm", "--force", name]
    assert calls[0][1]["env"] == calls[1][1]["env"] == environment
    for bad_name, bad_owner in [("another-container", owner), (name, "invalid=owner")]:
        with pytest.raises(ValueError, match="INVALID_PARSER_"):
            parser._container_argv("docker", IMAGE, bad_name, "a.txt", "", parser.DEFAULT_LIMITS,
                                    owner_label=bad_owner)


@pytest.mark.parametrize("cleanup_returncode", [0, 1])
def test_timeout_reclaims_container_not_only_docker_client(monkeypatch, tmp_path,
                                                          cleanup_returncode):
    process, calls = fake_docker(monkeypatch, b"", block=True,
                                 cleanup_returncode=cleanup_returncode)
    result = parser.parse_isolated(b"text", "a.txt", image=IMAGE, workspace=tmp_path,
                                   limits=replace(parser.DEFAULT_LIMITS, wall_seconds=1))
    expected = "PARSER_CLEANUP_UNCONFIRMED" if cleanup_returncode else "PARSER_TIMEOUT"
    assert result["error_code"] == expected
    assert process.killed
    assert calls[1][0] == ["docker", "rm", "--force", calls[0][0][4].split("=", 1)[1]]


def test_worker_output_is_bounded(monkeypatch, tmp_path):
    limits = replace(parser.DEFAULT_LIMITS, max_text_chars=10, max_records=1)
    process, calls = fake_docker(monkeypatch, b"x" * (limits.max_result_bytes + 1))
    result = parser.parse_isolated(b"text", "a.txt", image=IMAGE, workspace=tmp_path, limits=limits)
    assert result["error_code"] == "PARSER_OUTPUT_LIMIT"
    assert process.killed
    assert calls[1][0][:3] == ["docker", "rm", "--force"]


@pytest.mark.parametrize("mutation", [
    lambda result: result.update(file_sha256="wrong"),
    lambda result: result.update(text_sha256="wrong"),
    lambda result: result.update(partial=True),
    lambda result: result.update(ocr="completed"),
    lambda result: result.update(trust="system"),
    lambda result: result.update(issues=["unexpected issue"]),
    lambda result: result["records"][0].update(source={"filename": "secret"}),
    lambda result: result["records"][0].update(sha256="wrong"),
    lambda result: result["records"][0].update(text="changed", sha256=digest(b"changed")),
])
def test_worker_result_protocol_rejects_tampered_evidence(monkeypatch, tmp_path, mutation):
    candidate = parser.parse_document(b"text", "a.txt")
    mutation(candidate)
    fake_docker(monkeypatch, json.dumps(candidate).encode())
    result = parser.parse_isolated(b"text", "a.txt", image=IMAGE, workspace=tmp_path)
    assert result["error_code"] == "PARSER_PROTOCOL_INVALID"
    assert result["text"] == ""
    assert result["records"] == []
