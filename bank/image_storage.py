from pathlib import Path
from uuid import uuid4

from django.conf import settings
from django.core.files.storage import FileSystemStorage


MAX_IMAGE_BYTES = 20 * 1024 * 1024


class ImageStorageError(Exception):
    pass


def cloudinary_is_configured():
    return bool(settings.CLOUDINARY_URL)


def image_uploads_enabled():
    return settings.DEBUG or cloudinary_is_configured()


def _local_storage():
    return FileSystemStorage(
        location=Path(settings.MEDIA_ROOT) / "question-images",
        base_url=f"{settings.MEDIA_URL.rstrip('/')}/question-images/",
    )


def _configure_cloudinary():
    if not cloudinary_is_configured():
        raise ValueError("Image uploads are not configured. Add CLOUDINARY_URL to the server environment.")
    import cloudinary

    cloudinary.config(cloudinary_url=settings.CLOUDINARY_URL, secure=True)
    return cloudinary


def upload_question_image(uploaded_file):
    if settings.DEBUG:
        from PIL import Image

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

    cloudinary = _configure_cloudinary()
    import cloudinary.uploader
    from cloudinary.exceptions import Error as CloudinaryError

    try:
        result = cloudinary.uploader.upload(
            uploaded_file,
            folder="rawasi-question-images",
            resource_type="image",
            use_filename=True,
            unique_filename=True,
            overwrite=False,
        )
    except CloudinaryError as error:
        raise ImageStorageError(f"Cloudinary could not upload the image: {error}") from error
    return {
        "public_id": result["public_id"],
        "secure_url": result["secure_url"],
        "width": result.get("width", 0),
        "height": result.get("height", 0),
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

    cloudinary = _configure_cloudinary()
    import cloudinary.uploader
    from cloudinary.exceptions import Error as CloudinaryError

    try:
        result = cloudinary.uploader.destroy(
            public_id, invalidate=True, resource_type="image",
        )
    except CloudinaryError as error:
        raise ImageStorageError(f"Cloudinary could not delete the image: {error}") from error
    if result.get("result") not in {"ok", "not found"}:
        raise ImageStorageError(
            f"Cloudinary did not confirm deletion of the image (result: {result.get('result', 'unknown')})."
        )
