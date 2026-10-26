import json

from django import forms
from django.forms.models import BaseInlineFormSet, inlineformset_factory

from .models import ImageTarget, Option, Question, QuestionBank, QuestionImage


class QuestionForm(forms.ModelForm):
    class Meta:
        model = Question
        fields = ["bank", "number", "label", "kind", "stem", "answer", "explanation", "note"]
        widgets = {
            "stem": forms.Textarea(attrs={"rows": 3}),
            "answer": forms.Textarea(attrs={"rows": 3}),
            "explanation": forms.Textarea(attrs={"rows": 3}),
            "note": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        if self.instance.kind != Question.IMAGE:
            self.fields["kind"].choices = [
                choice for choice in self.fields["kind"].choices
                if choice[0] != Question.IMAGE
            ]
        f = self.fields["bank"]
        f.queryset = QuestionBank.objects.select_related("subject", "section").order_by(
            "subject__order", "semester", "title")
        f.label_from_instance = lambda d: d.path
        f.label = "Deck (subject › semester › title)"

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("kind") == Question.DESC and not (cleaned.get("answer") or "").strip():
            self.add_error("answer", "Enter the answer shown on the descriptive flashcard.")
        return cleaned


class ImageUploadForm(forms.Form):
    image = forms.FileField(
        help_text="Upload a clear image, scan, diagram, or table screenshot (up to 20 MB).",
        widget=forms.ClearableFileInput(attrs={
            "accept": "image/jpeg,image/png,image/webp,image/gif",
        }),
    )

    def clean_image(self):
        uploaded = self.cleaned_data["image"]
        if uploaded.size > 20 * 1024 * 1024:
            raise forms.ValidationError("Image must be 20 MB or smaller.")
        from PIL import Image, UnidentifiedImageError

        try:
            uploaded.seek(0)
            image = Image.open(uploaded)
            if image.format not in {"JPEG", "PNG", "WEBP", "GIF"}:
                raise forms.ValidationError("Use a JPEG, PNG, WebP, or GIF image.")
            if image.width * image.height > 80_000_000:
                raise forms.ValidationError("Image dimensions are too large to process.")
            image.verify()
        except (UnidentifiedImageError, OSError) as error:
            raise forms.ValidationError("The uploaded file is not a valid supported image.") from error
        finally:
            uploaded.seek(0)
        return uploaded


class ImageQuestionForm(forms.Form):
    is_lab = forms.BooleanField(
        label="Import this question to Labs",
        required=False,
        help_text="Labs can contain MCQs, True / False, descriptive, and image questions.",
    )
    answer_mode = forms.ChoiceField(choices=(
        (Question.IMAGE_IDENTIFY, "Identify labelled structures"),
        (Question.MCQ, "MCQ"),
        (Question.TF, "True / False"),
        (Question.DESC, "Descriptive"),
    ))
    stem = forms.CharField(label="Question / instructions", widget=forms.Textarea(attrs={"rows": 3}))
    answer = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 3}))
    explanation = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 3}))
    note = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))
    targets_json = forms.CharField(widget=forms.HiddenInput(), initial="[]")
    options_json = forms.CharField(widget=forms.HiddenInput(), initial="[]")

    def clean_stem(self):
        stem = self.cleaned_data["stem"].strip()
        if not stem:
            raise forms.ValidationError("Enter the question or identification instructions.")
        return stem

    def _json_list(self, name):
        try:
            value = json.loads(self.cleaned_data[name])
        except (TypeError, ValueError) as error:
            raise forms.ValidationError("Question entries could not be read; please check them and try again.") from error
        if not isinstance(value, list):
            raise forms.ValidationError("Question entries must be a list.")
        return value

    def clean(self):
        cleaned = super().clean()
        if "answer_mode" not in cleaned:
            return cleaned
        mode = cleaned["answer_mode"]
        if mode == Question.IMAGE_IDENTIFY:
            targets = self._json_list("targets_json") if "targets_json" in cleaned else []
            if not 1 <= len(targets) <= 50:
                self.add_error("targets_json", "Add between 1 and 50 identification targets.")
                return cleaned
            seen_labels = set()
            for target in targets:
                if not isinstance(target, dict):
                    self.add_error("targets_json", "Each target must have a label and answer.")
                    break
                label = target.get("label")
                answer = target.get("answer")
                if not isinstance(label, str) or not label.strip():
                    self.add_error("targets_json", "Every target needs a label, such as A or 1.")
                    break
                if not isinstance(answer, str) or not answer.strip():
                    self.add_error("targets_json", f"Target {label.strip()} needs an answer.")
                    break
                if label.strip().casefold() in seen_labels:
                    self.add_error("targets_json", f"Target label {label.strip()} is repeated.")
                    break
                seen_labels.add(label.strip().casefold())
                x, y = target.get("x_percent"), target.get("y_percent")
                if (x in (None, "")) != (y in (None, "")):
                    self.add_error("targets_json", f"Target {label.strip()} needs both marker coordinates or neither.")
                    break
                if x not in (None, "") and not self._valid_coordinates(x, y):
                    self.add_error("targets_json", f"Target {label.strip()} marker must be within the image.")
                    break
            cleaned["targets"] = targets
        elif mode in {Question.MCQ, Question.TF}:
            options = self._json_list("options_json") if "options_json" in cleaned else []
            minimum = 1 if mode == Question.TF else 2
            if not minimum <= len(options) <= 8:
                self.add_error("options_json", f"Add between {minimum} and 8 answer choices.")
                return cleaned
            if any(
                not isinstance(option, dict)
                or not isinstance(option.get("text"), str)
                or not option["text"].strip()
                or not isinstance(option.get("is_correct"), bool)
                for option in options
            ):
                self.add_error("options_json", "Every answer choice needs text and a correct/incorrect selection.")
                return cleaned
            if mode == Question.MCQ and not any(option["is_correct"] for option in options):
                self.add_error("options_json", "Mark at least one correct answer choice.")
            cleaned["options"] = options
        elif not cleaned.get("answer", "").strip():
            self.add_error("answer", "Enter the answer shown on the descriptive flashcard.")
        return cleaned

    @staticmethod
    def _valid_coordinates(x, y):
        try:
            x_value, y_value = float(x), float(y)
        except (TypeError, ValueError):
            return False
        return 0 <= x_value <= 100 and 0 <= y_value <= 100


class OptionFormSet(BaseInlineFormSet):
    """Shared by the editor page and the Django admin."""

    def clean(self):
        super().clean()
        if any(self.errors):
            return
        kind = self.data.get("kind", self.instance.kind)
        if kind == Question.DESC:
            return
        opts = [f.cleaned_data for f in self.forms
                if f.cleaned_data and not f.cleaned_data.get("DELETE")]
        is_tf = kind == Question.TF
        need = 1 if is_tf else 2
        if len(opts) < need:
            raise forms.ValidationError(f"Add at least {need} option(s).")
        if len(opts) > 8:
            raise forms.ValidationError("At most 8 options.")
        if not is_tf and not any(o.get("is_correct") for o in opts):
            raise forms.ValidationError("Tick at least one correct option.")


def option_formset(extra=2):
    return inlineformset_factory(
        Question, Option, formset=OptionFormSet, fields=("text", "is_correct"),
        extra=extra, can_delete=True,
        widgets={"text": forms.Textarea(attrs={"rows": 2})},
    )
