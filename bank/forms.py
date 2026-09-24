from django import forms
from django.forms.models import BaseInlineFormSet, inlineformset_factory

from .models import Option, Question, QuestionBank


class QuestionForm(forms.ModelForm):
    class Meta:
        model = Question
        fields = ["bank", "number", "kind", "stem", "explanation", "note"]
        widgets = {
            "stem": forms.Textarea(attrs={"rows": 3}),
            "explanation": forms.Textarea(attrs={"rows": 3}),
            "note": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        f = self.fields["bank"]
        f.queryset = QuestionBank.objects.select_related("subject").order_by(
            "subject__order", "semester", "title")
        f.label_from_instance = lambda d: d.path
        f.label = "Deck (subject › semester › title)"


class OptionFormSet(BaseInlineFormSet):
    """Shared by the editor page and the Django admin."""

    def clean(self):
        super().clean()
        if any(self.errors):
            return
        opts = [f.cleaned_data for f in self.forms
                if f.cleaned_data and not f.cleaned_data.get("DELETE")]
        is_tf = self.instance.kind == Question.TF
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

