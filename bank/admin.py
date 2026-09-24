from django.contrib import admin

from .forms import OptionFormSet
from .models import Comment, Option, Question, QuestionBank, Subject


class OptionInline(admin.TabularInline):
    model = Option
    formset = OptionFormSet
    extra = 4
    fields = ("order", "text", "is_correct")


@admin.register(Subject)
class SubjectAdmin(admin.ModelAdmin):
    list_display = ("name", "order", "slug")
    list_editable = ("order",)
    prepopulated_fields = {"slug": ("name",)}


@admin.register(QuestionBank)
class QuestionBankAdmin(admin.ModelAdmin):
    list_display = ("title", "subject", "semester", "slug")
    list_editable = ("subject", "semester")   # quick way to file decks
    list_filter = ("subject", "semester")
    search_fields = ("title",)
    prepopulated_fields = {"slug": ("title",)}


@admin.register(Question)
class QuestionAdmin(admin.ModelAdmin):
    list_display = ("__str__", "bank", "kind")
    list_filter = ("bank__subject", "bank", "kind")
    search_fields = ("stem", "number")
    inlines = [OptionInline]
    fieldsets = (
        (None, {"fields": ("bank", "number", "kind", "stem")}),
        ("Feedback shown after answering", {"fields": ("explanation", "note")}),
    )


@admin.register(Comment)
class CommentAdmin(admin.ModelAdmin):
    list_display = ("__str__", "user", "created_at", "resolved")
    list_filter = ("resolved",)
    readonly_fields = ("question", "user", "text", "created_at", "resolved_at", "resolved_by")
