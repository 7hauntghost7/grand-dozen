import json
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen
from uuid import uuid4

from django.conf import settings
from django.core.files.storage import FileSystemStorage
from PIL import Image


MAX_IMAGE_BYTES = 20 * 1024 * 1024


class ImageStorageError(Exception):
    pass


def supabase_is_configured():
    return bool(settings.SUPABASE_URL and settings.SUPABASE_SERVICE_ROLE_KEY)


def image_uploads_enabled():
    return settings.DEBUG or supabase_is_configured()


def _local_storage():
    return FileSystemStorage(
        location=Path(settings.MEDIA_ROOT) / "question-images",
        base_url=f"{settings.MEDIA_URL.rstrip('/')}/question-images/",
    )


def _supabase_object_url(path=""):
    base_url = (
        f"{settings.SUPABASE_URL}/storage/v1/object/"
        f"{quote(settings.SUPABASE_STORAGE_BUCKET, safe='')}"
    )
    return f"{base_url}/{quote(path, safe='/')}" if path else base_url


def _supabase_headers(content_type="application/json"):
    if not supabase_is_configured():
        raise ValueError(
            "Image uploads are not configured. Add SUPABASE_URL and "
            "SUPABASE_SERVICE_ROLE_KEY to the server environment."
        )
    return {
        "apikey": settings.SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {settings.SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": content_type,
    }


def _supabase_request(request, action):
    try:
        with urlopen(request, timeout=30) as response:
            response.read()
    except HTTPError as error:
        detail = error.read(1000).decode("utf-8", errors="replace")
        raise ImageStorageError(
            f"Supabase could not {action} the image (HTTP {error.code}): {detail}"
        ) from error
    except (URLError, OSError) as error:
        raise ImageStorageError(f"Supabase could not {action} the image: {error}") from error


def upload_question_image(uploaded_file):
    if settings.DEBUG:
        with Image.open(uploaded_file) as image:
            width, height = image.size
        extension = Path(uploaded_file.name).suffix.lower()
        filename = f"{uuid4().hex}{extension}"
        storage = _local_storage()
        saved_name = storage.save(filename, uploaded_file)
        return {
            "public_id": f"local:{saved_name}",
            "secure_url": storage.url(saved_name),
            "width": width,
            "height": height,
        }

    with Image.open(uploaded_file) as image:
        width, height = image.size
    extension = Path(uploaded_file.name).suffix.lower()
    object_path = f"{uuid4().hex}{extension}"
    uploaded_file.seek(0)
    content_type = uploaded_file.content_type or "application/octet-stream"
    request = Request(
        _supabase_object_url(object_path),
        data=uploaded_file.read(),
        headers={
            **_supabase_headers(content_type),
            "x-upsert": "false",
        },
        method="POST",
    )
    _supabase_request(request, "upload")
    return {
        "public_id": f"supabase:{object_path}",
        "secure_url": (
            f"{settings.SUPABASE_URL}/storage/v1/object/public/"
            f"{quote(settings.SUPABASE_STORAGE_BUCKET, safe='')}/"
            f"{quote(object_path, safe='/')}"
        ),
        "width": width,
        "height": height,
    }


def delete_question_image(public_id):
    if public_id.startswith("local:"):
        if not settings.DEBUG:
            raise ValueError("Local development images cannot be deleted outside DEBUG mode.")
        name = public_id[len("local:"):]
        if Path(name).name != name:
            raise ValueError("Invalid local image reference.")
        _local_storage().delete(name)
        return

    if not public_id.startswith("supabase:"):
        raise ValueError("Unsupported image storage reference.")

    object_path = public_id[len("supabase:"):]
    if not object_path or any(part in {"", ".", ".."} for part in object_path.split("/")):
        raise ValueError("Invalid Supabase image reference.")
    request = Request(
        _supabase_object_url(),
        data=json.dumps({"prefixes": [object_path]}).encode("utf-8"),
        headers=_supabase_headers(),
        method="DELETE",
    )
    _supabase_request(request, "delete")
