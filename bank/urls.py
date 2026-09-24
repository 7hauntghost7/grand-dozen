from django.urls import path

from . import views

app_name = "bank"

urlpatterns = [
    path("", views.home, name="home"),
    path("subject/<slug:slug>/", views.subject_detail, name="subject_detail"),
    path("deck/<slug:slug>/", views.bank_detail, name="bank_detail"),
    path("deck/<slug:slug>/quiz/", views.quiz, name="quiz"),
    path("deck/<slug:slug>/export/", views.export_bank, name="export"),
    path("deck/<slug:slug>/manage/", views.manage_deck, name="manage_deck"),
    path("deck/<slug:slug>/add/", views.question_add, name="question_add"),

    path("edit/question/<int:pk>/", views.question_edit, name="question_edit"),
    path("edit/question/<int:pk>/delete/", views.question_delete, name="question_delete"),
    path("import/", views.import_questions, name="import"),
    path("comments/", views.comments_inbox, name="comments"),
    path("comments/<int:pk>/toggle/", views.comment_toggle, name="comment_toggle"),

    path("saved/", views.saved_home, name="saved_home"),
    path("saved/subject/<slug:slug>/", views.saved_subject, name="saved_subject"),
    path("saved/deck/<slug:slug>/", views.saved_deck, name="saved_deck"),
    path("saved/deck/<slug:slug>/quiz/", views.saved_quiz, name="saved_quiz"),
    path("saved/remove/<int:pk>/", views.saved_remove, name="saved_remove"),

    path("api/deck/<slug:slug>/questions/", views.api_questions, name="api_questions"),
    path("api/saved/deck/<slug:slug>/questions/", views.api_saved_questions, name="api_saved_questions"),
    path("api/question/<int:pk>/check/", views.api_check, name="api_check"),
    path("api/question/<int:pk>/save/", views.api_save, name="api_save"),
    path("api/question/<int:pk>/comment/", views.api_comment, name="api_comment"),

    path("accounts/login/", views.SingleSessionLoginView.as_view(), name="login"),
    path("accounts/logout/", views.logout_view, name="logout"),
]
