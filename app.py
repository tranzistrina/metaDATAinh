"""Local Flask metadata editor. Requires the ExifTool executable."""
from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path, PurePath

from flask import Flask, abort, jsonify, render_template, request, send_file, session

MAX_UPLOAD_BYTES = 100 * 1024 * 1024
MAX_FIELDS = 100
MAX_TEMPLATES = 100
MAX_FIELD_LENGTH = 10_000
TAG_RE = re.compile(r"^(?:[A-Za-z][A-Za-z0-9_-]{0,49}:)?[A-Za-z][A-Za-z0-9_]{0,79}$")
FILE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
READ_ONLY_GROUPS = {"file", "system", "composite", "exiftool", "makernotes"}
PROTECTED_TAGS = {
    "all", "filename", "directory", "sourcefile", "filemodifydate",
    "filecreatedate", "fileaccessdate", "filepermissions", "fileattributes",
    "geotag", "geotime", "tagsfromfile",
}
_store_lock = threading.Lock()


class MetadataError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _execute(arguments: list[str], timeout: int = 90) -> subprocess.CompletedProcess[str]:
    binary = os.environ.get("EXIFTOOL_PATH") or shutil.which("exiftool")
    if not binary:
        raise MetadataError(
            "ExifTool не найден. На macOS выполните: brew install exiftool", 503
        )
    try:
        return subprocess.run(
            [binary, *arguments],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise MetadataError("ExifTool превысил время ожидания.", 504) from exc
    except OSError as exc:
        raise MetadataError(f"Не удалось запустить ExifTool: {exc}", 503) from exc


@lru_cache(maxsize=1)
def writable_tag_names() -> frozenset[str]:
    """ExifTool's supported writable tag names (file-specific support may differ)."""
    result = _execute(["-listw"])
    if result.returncode != 0:
        raise MetadataError(result.stderr.strip() or "Не удалось получить список тегов.", 503)
    # -listw returns a whitespace-delimited text listing with a short heading.
    return frozenset(
        word.lower()
        for word in re.findall(r"[A-Za-z][A-Za-z0-9_]*", result.stdout)
    )


def validate_tag(tag: str) -> str:
    if not isinstance(tag, str) or not TAG_RE.fullmatch(tag):
        raise MetadataError("Некорректное имя тега. Пример: XMP-dc:Title.")
    if ":" in tag:
        group, name = tag.rsplit(":", 1)
        if group.lower() in READ_ONLY_GROUPS:
            raise MetadataError(f"Группа {group} доступна только для чтения.")
    else:
        name = tag
    if name.lower() in PROTECTED_TAGS:
        raise MetadataError(f"Изменение служебного тега {name} запрещено.")
    if name.lower() not in writable_tag_names():
        raise MetadataError(f"ExifTool не поддерживает запись тега {tag}.")
    return tag


def _normalize_value(value: object) -> str | list[str]:
    if isinstance(value, str):
        if len(value) > MAX_FIELD_LENGTH or "\x00" in value:
            raise MetadataError("Значение слишком длинное или содержит недопустимые символы.")
        return value
    if isinstance(value, list) and len(value) <= 50:
        if all(isinstance(item, str) and len(item) <= MAX_FIELD_LENGTH
               and "\x00" not in item for item in value):
            return value
    raise MetadataError("Значение должно быть строкой или массивом строк.")


def _validate_fields(fields: object) -> dict[str, str | list[str]]:
    if not isinstance(fields, dict) or len(fields) > MAX_FIELDS:
        raise MetadataError("Ожидается объект с максимум 100 полями.")
    return {validate_tag(tag): _normalize_value(value) for tag, value in fields.items()}


def read_metadata(path: Path) -> list[dict[str, object]]:
    result = _execute(["-json", "-G1", "-a", "-s", "-u", "-U", "-struct",
                       "-charset", "filename=utf8", "--", str(path)])
    if result.returncode != 0:
        raise MetadataError(result.stderr.strip() or "Файл не удалось прочитать.", 422)
    try:
        raw = json.loads(result.stdout)[0]
    except (ValueError, IndexError, TypeError, KeyError) as exc:
        raise MetadataError("ExifTool вернул некорректный ответ.", 422) from exc
    if not isinstance(raw, dict):
        raise MetadataError("ExifTool не вернул данные метаданных.", 422)
    writable = writable_tag_names()
    fields: list[dict[str, object]] = []
    for key, value in raw.items():
        if key == "SourceFile":
            continue  # ExifTool's temporary server path, not a file tag.
        group, sep, name = key.rpartition(":")
        if not sep:
            group, name = "Other", key
        can_edit = bool(
            TAG_RE.fullmatch(key) and name.lower() in writable
            and name.lower() not in PROTECTED_TAGS
            and group.lower() not in READ_ONLY_GROUPS
            and isinstance(value, (str, int, float, list))
            and not isinstance(value, bool)
            and (not isinstance(value, list) or all(isinstance(x, str) for x in value))
        )
        fields.append({
            "key": key, "group": group, "name": name,
            "value": value, "editable": can_edit,
        })
    return fields


def _metadata_file(directory: Path) -> Path:
    return directory / "file.json"


def _file_info(directory: Path) -> dict[str, str]:
    try:
        info = json.loads(_metadata_file(directory).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MetadataError("Файл не найден.", 404) from exc
    if not isinstance(info, dict) or not isinstance(info.get("extension"), str):
        raise MetadataError("Некорректная информация о файле.", 404)
    return info


def _paths(root: Path, file_id: str) -> tuple[Path, Path, Path, dict]:
    if not FILE_ID_RE.fullmatch(file_id):
        raise MetadataError("Файл не найден.", 404)
    directory = root / "uploads" / file_id
    info = _file_info(directory)
    extension = info["extension"]
    if not re.fullmatch(r"\.[A-Za-z0-9]{1,15}|\.bin", extension):
        raise MetadataError("Некорректное расширение.", 404)
    original = directory / ("original" + extension)
    working = directory / ("working" + extension)
    if not original.is_file() or not working.is_file():
        raise MetadataError("Файл не найден.", 404)
    return directory, original, working, info


def _write_changes(working: Path, changes: dict[str, str | list[str]],
                   removals: list[str]) -> None:
    instructions: list[str] = []
    for tag, value in changes.items():
        if isinstance(value, list):
            instructions.append(f"-{tag}=")
            instructions.extend(f"-{tag}+={item}" for item in value)
        else:
            instructions.append(f"-{tag}={value}")
    for tag in removals:
        instructions.append(f"-{tag}=")
    if not instructions:
        raise MetadataError("Нет изменений для сохранения.")

    fd, candidate_name = tempfile.mkstemp(
        prefix="candidate_", suffix=working.suffix, dir=working.parent
    )
    os.close(fd)
    candidate = Path(candidate_name)
    try:
        shutil.copy2(working, candidate)
        result = _execute(["-overwrite_original", "-charset", "filename=utf8",
                           *instructions, "--", str(candidate)])
        if result.returncode != 0:
            error = (result.stderr or result.stdout).strip()
            raise MetadataError("ExifTool: " + (error or "изменение не удалось"), 422)
        os.replace(candidate, working)
    finally:
        candidate.unlink(missing_ok=True)


def _read_templates(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    raise MetadataError("Хранилище шаблонов повреждено.", 500)


def _save_templates(path: Path, templates: list[dict]) -> None:
    fd, temp_name = tempfile.mkstemp(prefix="templates_", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(templates, f, ensure_ascii=False, indent=2)
        os.replace(temp_name, path)
    finally:
        Path(temp_name).unlink(missing_ok=True)


def _cleanup_old_uploads(root: Path) -> None:
    uploads = root / "uploads"
    if not uploads.exists():
        return
    cutoff = time.time() - 24 * 3600
    for directory in uploads.iterdir():
        if (directory.is_dir() and FILE_ID_RE.fullmatch(directory.name)
                and directory.stat().st_mtime < cutoff):
            shutil.rmtree(directory, ignore_errors=True)


def create_app(data_dir: str | Path | None = None) -> Flask:
    app = Flask(__name__)
    root = Path(data_dir or os.environ.get("METADATA_DATA_DIR") or app.instance_path)
    root.mkdir(parents=True, exist_ok=True)
    (root / "uploads").mkdir(exist_ok=True)
    _cleanup_old_uploads(root)
    templates_path = root / "templates.json"

    app.config.update(
        SECRET_KEY=os.environ.get("METADATA_SECRET") or secrets.token_hex(32),
        MAX_CONTENT_LENGTH=MAX_UPLOAD_BYTES,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        JSON_AS_ASCII=False,
    )

    @app.before_request
    def check_csrf():
        if request.path.startswith("/api/") and request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            expected = session.get("csrf_token", "")
            submitted = request.headers.get("X-CSRF-Token", "")
            if not expected or not hmac.compare_digest(expected, submitted):
                raise MetadataError("Неверный CSRF-токен. Обновите страницу.", 403)

    @app.after_request
    def security_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.errorhandler(MetadataError)
    def handle_metadata_error(error):
        return jsonify(error=str(error)), error.status

    @app.errorhandler(413)
    def handle_too_large(_error):
        return jsonify(error="Файл превышает лимит 100 МБ."), 413

    @app.errorhandler(404)
    def handle_not_found(error):
        if request.path.startswith("/api/"):
            return jsonify(error="Ресурс не найден."), 404
        return error

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/api/status")
    def status():
        if "csrf_token" not in session:
            session["csrf_token"] = secrets.token_urlsafe(32)
        binary = os.environ.get("EXIFTOOL_PATH") or shutil.which("exiftool")
        version = None
        if binary:
            try:
                answer = _execute(["-ver"], timeout=10)
                if answer.returncode == 0:
                    version = answer.stdout.strip()
            except MetadataError:
                pass
        return jsonify(available=bool(version), version=version,
                       csrf_token=session["csrf_token"], max_bytes=MAX_UPLOAD_BYTES)

    @app.post("/api/files")
    def upload_file():
        upload = request.files.get("file")
        if not upload or not upload.filename:
            raise MetadataError("Выберите файл.")
        filename = PurePath(upload.filename.replace("\\", "/")).name
        if filename in {"", ".", ".."}:
            raise MetadataError("Некорректное имя файла.")
        extension = Path(filename).suffix
        if not re.fullmatch(r"\.[A-Za-z0-9]{1,15}", extension):
            extension = ".bin"
        file_id = uuid.uuid4().hex
        directory = root / "uploads" / file_id
        directory.mkdir()
        original = directory / ("original" + extension)
        working = directory / ("working" + extension)
        try:
            upload.save(original)
            if original.stat().st_size == 0:
                raise MetadataError("Пустой файл не поддерживается.")
            shutil.copy2(original, working)
            _metadata_file(directory).write_text(
                json.dumps({"name": filename[:200], "extension": extension},
                           ensure_ascii=False), encoding="utf-8"
            )
            fields = read_metadata(working)
        except Exception:
            shutil.rmtree(directory, ignore_errors=True)
            raise
        return jsonify(id=file_id, name=filename[:200], metadata=fields), 201

    @app.get("/api/files/<file_id>")
    def file_metadata(file_id):
        _, _, working, info = _paths(root, file_id)
        return jsonify(id=file_id, name=info["name"], metadata=read_metadata(working))

    @app.post("/api/files/<file_id>/apply")
    def apply_edits(file_id):
        _, _, working, info = _paths(root, file_id)
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            raise MetadataError("Ожидается JSON-объект.")
        changes = _validate_fields(body.get("changes", {}))
        removals = body.get("removals", [])
        if not isinstance(removals, list) or len(removals) > MAX_FIELDS:
            raise MetadataError("Некорректный список удаляемых тегов.")
        checked_removals = [validate_tag(tag) for tag in removals]
        if set(checked_removals) & set(changes):
            raise MetadataError("Нельзя одновременно изменить и удалить один тег.")
        _write_changes(working, changes, checked_removals)
        return jsonify(id=file_id, name=info["name"], metadata=read_metadata(working))

    @app.post("/api/files/<file_id>/reset")
    def reset_file(file_id):
        _, original, working, info = _paths(root, file_id)
        shutil.copy2(original, working)
        return jsonify(id=file_id, name=info["name"], metadata=read_metadata(working))

    @app.get("/api/files/<file_id>/download")
    def download_file(file_id):
        _, _, working, info = _paths(root, file_id)
        name = info["name"]
        stem, suffix = os.path.splitext(name)
        return send_file(working, as_attachment=True,
                         download_name=f"{stem}_edited{suffix}")

    @app.get("/api/templates")
    def list_templates():
        with _store_lock:
            return jsonify(templates=_read_templates(templates_path))

    @app.post("/api/templates")
    def create_template():
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            raise MetadataError("Ожидается JSON-объект.")
        name = body.get("name")
        if not isinstance(name, str) or not (1 <= len(name.strip()) <= 80):
            raise MetadataError("Название шаблона должно содержать 1–80 символов.")
        fields = _validate_fields(body.get("fields"))
        if not fields:
            raise MetadataError("Добавьте хотя бы одно поле в шаблон.")
        item = {
            "id": uuid.uuid4().hex,
            "name": name.strip(),
            "fields": fields,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        with _store_lock:
            templates = _read_templates(templates_path)
            if len(templates) >= MAX_TEMPLATES:
                raise MetadataError("Достигнут лимит 100 шаблонов.")
            templates.append(item)
            _save_templates(templates_path, templates)
        return jsonify(template=item), 201

    @app.delete("/api/templates/<template_id>")
    def delete_template(template_id):
        if not FILE_ID_RE.fullmatch(template_id):
            raise MetadataError("Шаблон не найден.", 404)
        with _store_lock:
            templates = _read_templates(templates_path)
            remaining = [entry for entry in templates if entry.get("id") != template_id]
            if len(remaining) == len(templates):
                raise MetadataError("Шаблон не найден.", 404)
            _save_templates(templates_path, remaining)
        return jsonify(ok=True)

    @app.post("/api/files/<file_id>/apply-template")
    def apply_template(file_id):
        _, _, working, info = _paths(root, file_id)
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or not isinstance(body.get("template_id"), str):
            raise MetadataError("Выберите шаблон.")
        with _store_lock:
            item = next(
                (entry for entry in _read_templates(templates_path)
                 if entry.get("id") == body["template_id"]), None
            )
        if item is None:
            raise MetadataError("Шаблон не найден.", 404)
        fields = _validate_fields(item["fields"])
        _write_changes(working, fields, [])
        return jsonify(id=file_id, name=info["name"], metadata=read_metadata(working))

    return app


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    create_app().run(host="127.0.0.1", port=port, debug=False, use_reloader=False)
