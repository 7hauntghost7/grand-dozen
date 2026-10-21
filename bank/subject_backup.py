import json
from decimal import Decimal, InvalidOperation

from django.core.management.color import no_style
from django.db import IntegrityError, connection, transaction
from django.db.models import Prefetch, Q
from django.utils import timezone

from .models import ImageTarget, Option, Question, QuestionBank, QuestionImage, Subject, SubjectSection


FORMAT = "grand-dozen-subject-backup"
VERSION = 2
MAX_BACKUP_BYTES = 100 * 1024 * 1024


class BackupError(ValueError):
    pass


def _positive_id(value, label):
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 9223372036854775807:
        raise BackupError(f"{label} must be an integer between 1 and 9223372036854775807.")
    return value


def _string(value, label, max_length=None):
    if not isinstance(value, str):
        raise BackupError(f"{label} must be text.")
    if max_length is not None and len(value) > max_length:
        raise BackupError(f"{label} exceeds {max_length} characters.")
    return value


def _order(value, label):
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 32767:
        raise BackupError(f"{label} must be an integer between 0 and 32767.")
    return value


def export_backup():
    subjects = Subject.objects.prefetch_related(
        Prefetch("sections", queryset=SubjectSection.objects.order_by("pk")),
        Prefetch(
            "decks",
            queryset=QuestionBank.objects.order_by("pk").prefetch_related(
                Prefetch("images", queryset=QuestionImage.objects.order_by("pk")),
                Prefetch(
                    "questions",
                    queryset=Question.objects.order_by("pk").select_related(
                        "image_asset",
                    ).prefetch_related(
                        Prefetch("options", queryset=Option.objects.order_by("order", "pk")),
                        Prefetch("image_targets", queryset=ImageTarget.objects.order_by("order", "pk")),
                    ),
                ),
            ),
        ),
    ).order_by("order", "name", "pk")
    payload = {
        "format": FORMAT,
        "version": VERSION,
        "exported_at": timezone.now().isoformat(),
        "subjects": [],
    }
    for subject in subjects:
        subject_data = {
            "id": subject.pk,
            "name": subject.name,
            "slug": subject.slug,
            "description": subject.description,
            "order": subject.order,
            "sections": [
                {
                    "id": section.pk,
                    "name": section.name,
                    "slug": section.slug,
                    "order": section.order,
                }
                for section in subject.sections.all()
            ],
            "decks": [],
        }
        for deck in subject.decks.all():
            deck_data = {
                "id": deck.pk,
                "semester": deck.semester,
                "title": deck.title,
                "slug": deck.slug,
                "description": deck.description,
                "section_id": deck.section_id,
                "images": [
                    {
                        "id": image.pk,
                        "public_id": image.public_id,
                        "secure_url": image.secure_url,
                        "original_name": image.original_name,
                        "width": image.width,
                        "height": image.height,
                    }
                    for image in deck.images.all()
                ],
                "questions": [],
            }
            for question in deck.questions.all():
                deck_data["questions"].append({
                    "id": question.pk,
                    "number": question.number,
                    "kind": question.kind,
                    "stem": question.stem,
                    "answer": question.answer,
                    "explanation": question.explanation,
                    "note": question.note,
                    "image_asset_id": question.image_asset_id,
                    "image_answer_mode": question.image_answer_mode,
                    "image_targets": [
                        {
                            "id": target.pk,
                            "label": target.label,
                            "answer": target.answer,
                            "x_percent": str(target.x_percent) if target.x_percent is not None else None,
                            "y_percent": str(target.y_percent) if target.y_percent is not None else None,
                            "order": target.order,
                        }
                        for target in question.image_targets.all()
                    ],
                    "options": [
                        {
                            "id": option.pk,
                            "text": option.text,
                            "is_correct": option.is_correct,
                            "order": option.order,
                        }
                        for option in question.options.all()
                    ],
                })
            subject_data["decks"].append(deck_data)
        payload["subjects"].append(subject_data)
    return payload


def encode_backup():
    encoder = json.JSONEncoder(ensure_ascii=False, indent=2)
    chunks = []
    byte_count = 0
    for chunk in encoder.iterencode(export_backup()):
        encoded = chunk.encode("utf-8")
        byte_count += len(encoded)
        if byte_count > MAX_BACKUP_BYTES:
            raise BackupError("The export exceeds the 100 MB backup limit.")
        chunks.append(encoded)
    return b"".join(chunks)


def validate_backup(raw):
    if not isinstance(raw, bytes) or len(raw) > MAX_BACKUP_BYTES:
        raise BackupError("The backup file exceeds the 100 MB limit.")
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise BackupError("The file is not valid UTF-8 JSON.") from error
    if not isinstance(payload, dict) or payload.get("format") != FORMAT:
        raise BackupError("This is not a Grand Dozen subject backup.")
    if (
        isinstance(payload.get("version"), bool)
        or not isinstance(payload.get("version"), int)
        or payload.get("version") not in {1, VERSION}
    ):
        raise BackupError("This backup format version is not supported.")
    version = payload["version"]
    if not isinstance(payload.get("subjects"), list):
        raise BackupError("The backup must contain a subjects list.")

    seen = {
        "subjects": set(), "sections": set(), "decks": set(),
        "images": set(), "questions": set(), "options": set(), "image_targets": set(),
    }
    subject_slugs = set()
    subject_names = set()
    deck_slugs = set()
    deck_paths = set()
    section_subject_ids = {}
    image_deck_ids = {}
    valid_kinds = {choice[0] for choice in Question.KIND_CHOICES}
    valid_image_modes = {choice[0] for choice in Question.IMAGE_ANSWER_CHOICES}
    for subject in payload["subjects"]:
        if not isinstance(subject, dict):
            raise BackupError("Each subject entry must be an object.")
        subject_id = _positive_id(subject.get("id"), "Subject ID")
        if subject_id in seen["subjects"]:
            raise BackupError(f"Subject ID {subject_id} appears more than once.")
        seen["subjects"].add(subject_id)
        name = _string(subject.get("name"), "Subject name", 100)
        slug = _string(subject.get("slug"), "Subject slug", 120)
        description = _string(subject.get("description"), "Subject description", 200)
        order = _order(subject.get("order"), "Subject order")
        if not name or not slug or not isinstance(subject.get("decks"), list):
            raise BackupError("Subject name, slug, and decks are required.")
        if name in subject_names or slug in subject_slugs:
            raise BackupError("The backup contains duplicate subject names or slugs.")
        subject_names.add(name)
        subject_slugs.add(slug)
        subject.update(name=name, slug=slug, description=description, order=order)
        sections = subject.get("sections", [])
        if not isinstance(sections, list):
            raise BackupError(f"Subject {subject_id} must contain a sections list.")
        subject.update(sections=sections)
        if version == 1 and sections:
            raise BackupError("Version 1 backups cannot contain subject sections.")
        section_names = set()
        section_slugs = set()
        for section in sections:
            if not isinstance(section, dict):
                raise BackupError("Each subject section must be an object.")
            section_id = _positive_id(section.get("id"), "Section ID")
            if section_id in seen["sections"]:
                raise BackupError(f"Section ID {section_id} appears more than once.")
            seen["sections"].add(section_id)
            section_subject_ids[section_id] = subject_id
            section_name = _string(section.get("name"), "Section name", 100)
            section_slug = _string(section.get("slug"), "Section slug", 120)
            section_order = _order(section.get("order"), "Section order")
            if not section_name or not section_slug or section_name in section_names or section_slug in section_slugs:
                raise BackupError(f"Subject {subject_id} has invalid or duplicate sections.")
            section_names.add(section_name)
            section_slugs.add(section_slug)
            section.update(name=section_name, slug=section_slug, order=section_order)

        for deck in subject["decks"]:
            if not isinstance(deck, dict):
                raise BackupError("Each deck entry must be an object.")
            deck_id = _positive_id(deck.get("id"), "Deck ID")
            if deck_id in seen["decks"]:
                raise BackupError(f"Deck ID {deck_id} appears more than once.")
            seen["decks"].add(deck_id)
            semester = deck.get("semester")
            if isinstance(semester, bool) or not isinstance(semester, int) or semester not in range(1, 13):
                raise BackupError(f"Deck {deck_id} has an invalid semester.")
            title = _string(deck.get("title"), "Deck title", 200)
            slug = _string(deck.get("slug"), "Deck slug", 220)
            description = _string(deck.get("description"), "Deck description")
            section_id = deck.get("section_id")
            if section_id is not None:
                _positive_id(section_id, "Deck section ID")
                if section_subject_ids.get(section_id) != subject_id:
                    raise BackupError(f"Deck {deck_id} refers to a section from another subject.")
            if not title or not slug or not isinstance(deck.get("questions"), list):
                raise BackupError(f"Deck {deck_id} is missing a title, slug, or questions list.")
            if slug in deck_slugs:
                raise BackupError("The backup contains duplicate deck slugs.")
            path = (subject_id, semester, title)
            if path in deck_paths:
                raise BackupError("The backup contains duplicate subject/semester/deck titles.")
            deck_slugs.add(slug)
            deck_paths.add(path)
            deck.update(
                semester=semester, title=title, slug=slug, description=description,
                section_id=section_id,
            )
            images = deck.get("images", [])
            if not isinstance(images, list):
                raise BackupError(f"Deck {deck_id} must contain an images list.")
            if version == 1 and images:
                raise BackupError("Version 1 backups cannot contain image assets.")
            deck.update(images=images)
            for image in images:
                if not isinstance(image, dict):
                    raise BackupError("Each image asset must be an object.")
                image_id = _positive_id(image.get("id"), "Image ID")
                if image_id in seen["images"]:
                    raise BackupError(f"Image ID {image_id} appears more than once.")
                seen["images"].add(image_id)
                image_deck_ids[image_id] = deck_id
                public_id = _string(image.get("public_id"), "Image storage ID", 255)
                secure_url = _string(image.get("secure_url"), "Image URL", 2048)
                original_name = _string(image.get("original_name"), "Image filename", 255)
                width = _positive_id(image.get("width"), "Image width")
                height = _positive_id(image.get("height"), "Image height")
                if not public_id or not secure_url or not original_name:
                    raise BackupError(f"Image {image_id} is missing required metadata.")
                image.update(
                    public_id=public_id, secure_url=secure_url,
                    original_name=original_name, width=width, height=height,
                )

            for question in deck["questions"]:
                if not isinstance(question, dict):
                    raise BackupError("Each question entry must be an object.")
                question_id = _positive_id(question.get("id"), "Question ID")
                if question_id in seen["questions"]:
                    raise BackupError(f"Question ID {question_id} appears more than once.")
                seen["questions"].add(question_id)
                number = _string(question.get("number"), "Question number", 20)
                kind = _string(
                    question.get("kind"), "Question type",
                    Question._meta.get_field("kind").max_length,
                )
                stem = _string(question.get("stem"), "Question text")
                answer = _string(question.get("answer"), "Question answer")
                explanation = _string(question.get("explanation"), "Question explanation")
                note = _string(question.get("note"), "Question note")
                if kind not in valid_kinds:
                    raise BackupError(f"Question {question_id} has an unsupported type.")
                if not isinstance(question.get("options"), list):
                    raise BackupError(f"Question {question_id} must contain an options list.")
                image_asset_id = question.get("image_asset_id")
                image_answer_mode = question.get("image_answer_mode", Question.IMAGE_IDENTIFY)
                image_targets = question.get("image_targets", [])
                if version == 1 and kind == Question.IMAGE:
                    raise BackupError(
                        "This version 1 backup contains image questions but does not include "
                        "their image data. Download a fresh backup after updating the site."
                    )
                if image_asset_id is not None:
                    _positive_id(image_asset_id, "Question image ID")
                    if image_deck_ids.get(image_asset_id) != deck_id:
                        raise BackupError(f"Question {question_id} refers to an image from another deck.")
                if image_answer_mode not in valid_image_modes:
                    raise BackupError(f"Question {question_id} has an invalid image answer mode.")
                if not isinstance(image_targets, list):
                    raise BackupError(f"Question {question_id} must contain an image_targets list.")
                for target in image_targets:
                    if not isinstance(target, dict):
                        raise BackupError("Each image target must be an object.")
                    target_id = _positive_id(target.get("id"), "Image target ID")
                    if target_id in seen["image_targets"]:
                        raise BackupError(f"Image target ID {target_id} appears more than once.")
                    seen["image_targets"].add(target_id)
                    label = _string(target.get("label"), "Image target label", 40)
                    target_answer = _string(target.get("answer"), "Image target answer")
                    target_order = _order(target.get("order"), "Image target order")
                    coordinates = {}
                    for coordinate in ("x_percent", "y_percent"):
                        value = target.get(coordinate)
                        if value is not None:
                            try:
                                decimal_value = Decimal(str(value))
                            except (InvalidOperation, ValueError):
                                raise BackupError(f"Image target {coordinate} must be a number or null.")
                            if not decimal_value.is_finite() or not 0 <= decimal_value <= 100:
                                raise BackupError(f"Image target {coordinate} must be between 0 and 100.")
                            coordinates[coordinate] = decimal_value
                        else:
                            coordinates[coordinate] = None
                    target.update(
                        label=label, answer=target_answer, order=target_order,
                        **coordinates,
                    )
                question.update(
                    number=number, kind=kind, stem=stem, answer=answer,
                    explanation=explanation, note=note,
                    image_asset_id=image_asset_id,
                    image_answer_mode=image_answer_mode,
                    image_targets=image_targets,
                )
                if kind == Question.IMAGE and image_asset_id is None:
                    raise BackupError(f"Image question {question_id} is missing its image asset.")
                if kind != Question.IMAGE and (image_asset_id is not None or image_targets):
                    raise BackupError(f"Non-image question {question_id} contains image data.")
                for option in question["options"]:
                    if not isinstance(option, dict):
                        raise BackupError("Each answer option must be an object.")
                    option_id = _positive_id(option.get("id"), "Option ID")
                    if option_id in seen["options"]:
                        raise BackupError(f"Option ID {option_id} appears more than once.")
                    seen["options"].add(option_id)
                    option_text = _string(option.get("text"), "Option text")
                    correct = option.get("is_correct")
                    if not isinstance(correct, bool):
                        raise BackupError(f"Option {option_id} has an invalid correct-answer flag.")
                    option_order = _order(option.get("order"), "Option order")
                    option.update(text=option_text, is_correct=correct, order=option_order)

    return payload


def _matches(instance, values):
    return all(getattr(instance, field) == value for field, value in values.items())


def _find_or_plan(model, pk, values, *, conflict_filter=None, label):
    existing = model.objects.filter(pk=pk).first()
    if existing:
        if not _matches(existing, values):
            raise BackupError(
                f"{label} ID {pk} already exists with different content. Nothing was imported."
            )
        return False
    if conflict_filter is not None and model.objects.filter(conflict_filter).exists():
        raise BackupError(
            f"A different {label.lower()} already uses this name or location. Nothing was imported."
        )
    return True


@transaction.atomic
def import_backup(payload):
    additions = {
        "subjects": [], "sections": [], "decks": [], "images": [],
        "questions": [], "options": [], "image_targets": [],
    }
    skipped = {key: 0 for key in additions}

    for subject in payload["subjects"]:
        values = {
            "name": subject["name"], "slug": subject["slug"],
            "description": subject["description"], "order": subject["order"],
        }
        if _find_or_plan(
            Subject, subject["id"], values,
            conflict_filter=Q(name=subject["name"]) | Q(slug=subject["slug"]),
            label="Subject",
        ):
            additions["subjects"].append(subject)
        else:
            skipped["subjects"] += 1

        for section in subject["sections"]:
            values = {
                "subject_id": subject["id"], "name": section["name"],
                "slug": section["slug"], "order": section["order"],
            }
            conflict = (
                Q(subject_id=subject["id"], name=section["name"])
                | Q(subject_id=subject["id"], slug=section["slug"])
            )
            if _find_or_plan(
                SubjectSection, section["id"], values,
                conflict_filter=conflict, label="Section",
            ):
                additions["sections"].append((subject["id"], section))
            else:
                skipped["sections"] += 1

    for subject in payload["subjects"]:
        for deck in subject["decks"]:
            values = {
                "subject_id": subject["id"], "semester": deck["semester"],
                "title": deck["title"], "slug": deck["slug"],
                "description": deck["description"], "section_id": deck["section_id"],
            }
            conflict = (
                Q(slug=deck["slug"])
                | Q(subject_id=subject["id"], semester=deck["semester"], title=deck["title"])
            )
            if _find_or_plan(
                QuestionBank, deck["id"], values,
                conflict_filter=conflict, label="Deck",
            ):
                additions["decks"].append((subject["id"], deck))
            else:
                skipped["decks"] += 1

            for image in deck["images"]:
                values = {
                    "bank_id": deck["id"], "public_id": image["public_id"],
                    "secure_url": image["secure_url"], "original_name": image["original_name"],
                    "width": image["width"], "height": image["height"],
                }
                if _find_or_plan(
                    QuestionImage, image["id"], values,
                    conflict_filter=Q(public_id=image["public_id"]), label="Image",
                ):
                    additions["images"].append((deck["id"], image))
                else:
                    skipped["images"] += 1

            for question in deck["questions"]:
                values = {
                    "bank_id": deck["id"], "number": question["number"],
                    "kind": question["kind"], "stem": question["stem"],
                    "answer": question["answer"], "explanation": question["explanation"],
                    "note": question["note"], "image_asset_id": question["image_asset_id"],
                    "image_answer_mode": question["image_answer_mode"],
                }
                if _find_or_plan(
                    Question, question["id"], values, label="Question",
                ):
                    additions["questions"].append((deck["id"], question))
                else:
                    skipped["questions"] += 1

                for option in question["options"]:
                    values = {
                        "question_id": question["id"], "text": option["text"],
                        "is_correct": option["is_correct"], "order": option["order"],
                    }
                    if _find_or_plan(
                        Option, option["id"], values, label="Option",
                    ):
                        additions["options"].append((question["id"], option))
                    else:
                        skipped["options"] += 1
                for target in question["image_targets"]:
                    values = {
                        "question_id": question["id"], "label": target["label"],
                        "answer": target["answer"], "x_percent": target["x_percent"],
                        "y_percent": target["y_percent"], "order": target["order"],
                    }
                    if _find_or_plan(
                        ImageTarget, target["id"], values, label="Image target",
                    ):
                        additions["image_targets"].append((question["id"], target))
                    else:
                        skipped["image_targets"] += 1

    for subject in additions["subjects"]:
        Subject.objects.create(
            pk=subject["id"], name=subject["name"], slug=subject["slug"],
            description=subject["description"], order=subject["order"],
        )
    for subject_id, section in additions["sections"]:
        SubjectSection.objects.create(
            pk=section["id"], subject_id=subject_id, name=section["name"],
            slug=section["slug"], order=section["order"],
        )
    for subject_id, deck in additions["decks"]:
        QuestionBank.objects.create(
            pk=deck["id"], subject_id=subject_id, semester=deck["semester"],
            title=deck["title"], slug=deck["slug"], description=deck["description"],
            section_id=deck["section_id"],
        )
    for deck_id, image in additions["images"]:
        QuestionImage.objects.create(
            pk=image["id"], bank_id=deck_id, public_id=image["public_id"],
            secure_url=image["secure_url"], original_name=image["original_name"],
            width=image["width"], height=image["height"],
        )
    for deck_id, question in additions["questions"]:
        Question.objects.create(
            pk=question["id"], bank_id=deck_id, number=question["number"],
            kind=question["kind"], stem=question["stem"], answer=question["answer"],
            explanation=question["explanation"], note=question["note"],
            image_asset_id=question["image_asset_id"],
            image_answer_mode=question["image_answer_mode"],
        )
    for question_id, option in additions["options"]:
        Option.objects.create(
            pk=option["id"], question_id=question_id, text=option["text"],
            is_correct=option["is_correct"], order=option["order"],
        )
    for question_id, target in additions["image_targets"]:
        ImageTarget.objects.create(
            pk=target["id"], question_id=question_id, label=target["label"],
            answer=target["answer"], x_percent=target["x_percent"],
            y_percent=target["y_percent"], order=target["order"],
        )

    if any(additions.values()):
        statements = connection.ops.sequence_reset_sql(
            no_style(), [
                Subject, SubjectSection, QuestionBank, QuestionImage, Question,
                Option, ImageTarget,
            ],
        )
        with connection.cursor() as cursor:
            for statement in statements:
                cursor.execute(statement)

    added = {key: len(value) for key, value in additions.items()}
    return {"added": added, "skipped": skipped}


def restore_backup(raw):
    payload = validate_backup(raw)
    try:
        return import_backup(payload)
    except IntegrityError as error:
        raise BackupError(
            "The backup conflicts with current database constraints. No data was imported."
        ) from error
