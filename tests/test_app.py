import json
import subprocess
from io import BytesIO
from pathlib import Path

import pytest

import app as editor


@pytest.fixture
def env(tmp_path, monkeypatch):
    def fake_execute(arguments, timeout=90):
        if "-ver" in arguments:
            return subprocess.CompletedProcess([], 0, "13.50\n", "")
        if "-listw" in arguments:
            return subprocess.CompletedProcess(
                [], 0, "List of writable tags:\n Artist Copyright Title Keywords Subject\n", ""
            )
        if "-json" in arguments:
            return subprocess.CompletedProcess(
                [], 0,
                json.dumps([{
                    "SourceFile": arguments[-1],
                    "File:FileName": "example.jpg",
                    "EXIF:Artist": "Old author",
                    "XMP-dc:Title": "Old title",
                    "XMP-dc:Subject": ["one", "two"],
                    "EXIF:Make": "A camera",
                }]), ""
            )
        if "-overwrite_original" in arguments:
            candidate = Path(arguments[-1])
            candidate.write_bytes(candidate.read_bytes() + b"\nEDITED")
            return subprocess.CompletedProcess([], 0, "1 image files updated\n", "")
        raise AssertionError("Unexpected ExifTool call: " + repr(arguments))

    monkeypatch.setattr(editor, "_execute", fake_execute)
    editor.writable_tag_names.cache_clear()
    flask_app = editor.create_app(tmp_path)
    flask_app.config["TESTING"] = True
    with flask_app.test_client() as client:
        status = client.get("/api/status").get_json()
        token = status["csrf_token"]
        headers = {"X-CSRF-Token": token}
        yield client, headers, tmp_path
    editor.writable_tag_names.cache_clear()


def upload(client, headers, filename="sample.jpg"):
    return client.post(
        "/api/files", data={"file": (BytesIO(b"JPG-ORIGINAL"), filename)},
        content_type="multipart/form-data", headers=headers
    )


def test_upload_and_read_tags(env):
    client, headers, _ = env
    result = upload(client, headers)
    assert result.status_code == 201
    data = result.get_json()
    assert len(data["metadata"]) == 5
    fields = {item["key"]: item for item in data["metadata"]}
    assert fields["EXIF:Artist"]["editable"] is True
    assert fields["File:FileName"]["editable"] is False
    assert fields["EXIF:Make"]["editable"] is False
    assert fields["XMP-dc:Subject"]["value"] == ["one", "two"]
    assert client.get("/api/files/" + data["id"]).status_code == 200


def test_write_changes_keeps_original_and_downloads_copy(env):
    client, headers, root = env
    file_id = upload(client, headers).get_json()["id"]
    response = client.post(
        f"/api/files/{file_id}/apply",
        json={"changes": {"XMP-dc:Title": "New title"}, "removals": ["EXIF:Artist"]},
        headers=headers
    )
    assert response.status_code == 200
    directory = root / "uploads" / file_id
    assert (directory / "original.jpg").read_bytes() == b"JPG-ORIGINAL"
    assert (directory / "working.jpg").read_bytes().endswith(b"EDITED")
    downloaded = client.get(f"/api/files/{file_id}/download")
    assert downloaded.status_code == 200
    assert downloaded.data.endswith(b"EDITED")
    assert "sample_edited.jpg" in downloaded.headers["Content-Disposition"]
    reset = client.post(f"/api/files/{file_id}/reset", headers=headers)
    assert reset.status_code == 200
    assert (directory / "working.jpg").read_bytes() == b"JPG-ORIGINAL"


def test_save_and_apply_template(env):
    client, headers, root = env
    file_id = upload(client, headers).get_json()["id"]
    created = client.post(
        "/api/templates",
        json={"name": "Copyright preset",
              "fields": {"EXIF:Artist": "Team", "XMP-dc:Subject": ["one", "two"]}},
        headers=headers
    )
    assert created.status_code == 201
    template_id = created.get_json()["template"]["id"]
    listed = client.get("/api/templates").get_json()["templates"]
    assert len(listed) == 1
    assert listed[0]["name"] == "Copyright preset"
    applied = client.post(
        f"/api/files/{file_id}/apply-template",
        json={"template_id": template_id}, headers=headers
    )
    assert applied.status_code == 200
    assert (root / "uploads" / file_id / "working.jpg").read_bytes().endswith(b"EDITED")

    # Templates survive recreating the Flask app.
    new_app = editor.create_app(root)
    new_app.config["TESTING"] = True
    with new_app.test_client() as new_client:
        assert len(new_client.get("/api/templates").get_json()["templates"]) == 1

    deleted = client.delete("/api/templates/" + template_id, headers=headers)
    assert deleted.status_code == 200
    assert client.get("/api/templates").get_json()["templates"] == []


def test_csrf_protected_and_unknown_file(env):
    client, headers, _ = env
    assert upload(client, {}).status_code == 403
    assert client.post("/api/templates", json={"name": "a", "fields": {}}).status_code == 403
    assert client.get("/api/files/" + "a" * 32).status_code == 404
    assert client.get("/api/files/../../etc/passwd").status_code == 404


@pytest.mark.parametrize("tag", [
    "File:FileName", "System:FileModifyDate", "EXIF:All",
    "EXIF:NotWritable", "-unsafe", "XMP-dc:Title=oops", "XMP-dc:Title/../",
])
def test_reject_disallowed_tags(env, tag):
    client, headers, _ = env
    file_id = upload(client, headers).get_json()["id"]
    response = client.post(
        f"/api/files/{file_id}/apply",
        json={"changes": {tag: "hack"}, "removals": []}, headers=headers
    )
    assert response.status_code == 400


def test_write_failure_is_atomic(env, monkeypatch):
    client, headers, root = env
    file_id = upload(client, headers).get_json()["id"]
    working = root / "uploads" / file_id / "working.jpg"
    before = working.read_bytes()
    previous_execute = editor._execute

    def failing_execute(arguments, timeout=90):
        if "-overwrite_original" in arguments:
            Path(arguments[-1]).write_bytes(b"DAMAGED")
            return subprocess.CompletedProcess([], 1, "", "Invalid tag")
        return previous_execute(arguments, timeout)

    monkeypatch.setattr(editor, "_execute", failing_execute)
    result = client.post(
        f"/api/files/{file_id}/apply",
        json={"changes": {"EXIF:Artist": "new"}, "removals": []}, headers=headers
    )
    assert result.status_code == 422
    assert working.read_bytes() == before
    assert not list(working.parent.glob("candidate_*"))


def test_invalid_template_values(env):
    client, headers, _ = env
    response = client.post(
        "/api/templates",
        json={"name": "Invalid", "fields": {"XMP-dc:Title": {"nested": True}}},
        headers=headers
    )
    assert response.status_code == 400
    response = client.post(
        "/api/templates",
        json={"name": "Invalid", "fields": {}}, headers=headers
    )
    assert response.status_code == 400
