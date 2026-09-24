# The Grand Dozen

A Django site for hosting exam-question decks.

```
Home: big subject blocks
 └─ Subject page: dark "Semester N" bands
     └─ Deck cards (one per lesson, e.g. "Haematology")
         └─ Quiz  ·  ★ Save  ·  💬 Comment to admins  ·  ✎ Edit (editors)
```

* **Subjects → semesters → decks.** The home page shows one large block per subject. A subject page groups its decks under dark semester bands. A deck is a lesson.
* **Quizzes** with instant feedback, explanations, progress saving, shuffle, retry-missed. Correct answers are checked **server-side**.
* **★ Saved questions** – logged-in users can save any question. The **Saved** section mirrors the same layout (subject blocks → semester bands → decks) and lets you practise just the saved questions.
* **💬 Comments** – logged-in users can send a private comment about a question. Editors get a **Comments inbox** (with an open-count badge in the nav), jump straight to the question's edit page, and can resolve comments.
* **Editor pages** (not the Django admin) – manage a deck's questions, edit or add a question, delete it.
* **Bulk import** with subject + semester + deck title, preview and validation, duplicate detection, and a copy-paste **AI formatting prompt**.

## Run it

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python manage.py makemigrations bank
python manage.py migrate
python manage.py createsuperuser
python manage.py seed_subjects                          # creates 4 placeholder subjects – rename them in /admin/
python manage.py import_questions data/*.txt --subject Physiology --semester 1   # optional seed data
python manage.py runserver
```

Open http://127.0.0.1:8000/

> Migrations aren't shipped with the zip (it was written without a Django install to generate them). Run `makemigrations bank` once and commit the folder.
>
> **Upgrading a database from the previous version:** `makemigrations` will create a new migration. Existing decks get no subject, so they appear under **Not filed yet** on the home page; open **/admin/ → Decks**, and set Subject and Semester right in the list (they're editable inline). If you had no real data yet, simply delete `db.sqlite3` and `bank/migrations/` and start again.

### Subjects
Subjects are ordinary database rows: **/admin/ → Subjects**. Rename the placeholders, change `order`, or add more. The home page shows one block per subject.

### Who can do what
| Person | Can |
|---|---|
| Anyone | browse and take quizzes |
| Logged-in user (anyone can **Sign up**) | save questions, send comments |
| Editor | edit / add / delete questions, read comments, bulk-import |
| Superuser | everything, plus subjects, decks and users in `/admin/` |

To make an editor: `/admin/` → Users → add user → give permissions `bank | question | Can add / change / delete question` (or use a Group). Editors don't need "Staff status" for the site's own pages; they only need it to enter `/admin/`.

## Adding questions

**Bulk (recommended):** *Import* in the top nav.
1. Copy the **AI formatting prompt**, paste it with your raw questions into any AI chat.
2. On the Import page choose **Subject**, **Semester**, type the **Deck title** (typing an existing title adds to that deck; a new title creates a deck), paste the AI's output, **Preview**, fix flagged items, **Import**.

**One at a time:** open a deck → *Manage / edit questions* → *+ Add question*.

Command line: `python manage.py import_questions file.txt --subject Physiology --semester 2 --deck "Haematology"`.
`python manage.py export_bank <deck-slug> > backup.txt` writes a deck back in the same format (also on each deck page: *Download as text*).

## The text format

```
SUBJECT: Physiology            (optional – or use the form fields)
SEMESTER: 1                    (optional)
DECK: Haematology              (optional)

Q: The globin part of haemoglobin F consists of:
NO: 17                         (optional – number in your source)
A. 2 alpha and 2 beta chains
B. 2 alpha and 2 gamma chains
C. 2 beta and 2 gamma chains
ANSWER: B                      (several correct? "A, C" – any is accepted)
EXPLANATION: HbF is alpha2 gamma2.
NOTE: shown to students as a warning (ambiguous / incomplete source)

Q: Regarding erythrocytes:
TYPE: TF                       (each option is a statement)
A. They contain carbonic anhydrase.
B. They have mitochondria.
ANSWER: TF                     (one T/F per statement; "A=T, B=F" also works)
```

Rules: each question starts with `Q:`; options are `A.`–`H.` in order; anything the parser can't understand is reported with its line number in the preview — nothing is guessed. Duplicates (same question + same options, ignoring case, punctuation and option order) are detected inside the batch and against the target deck and skipped by default.

## Layout

```
config/                  settings, urls
bank/models.py           Subject → QuestionBank (deck) → Question → Option; SavedQuestion; Comment
bank/parser.py           plain-text format <-> Python (no Django imports; has its own tests)
bank/views.py            pages, JSON API, editor pages, import, signup
bank/forms.py            question editor form + option formset, signup form
bank/static/bank/        quiz.js, style.css
bank/templates/bank/     home, subject_detail, bank_detail (deck), quiz, saved_*, manage_deck,
                         question_edit, comments, import, login, signup
bank/management/         import_questions, export_bank, seed_subjects
data/*.txt               244 seed questions
```

## Tests

```bash
python -m unittest bank.test_parser     # parser only, no Django needed
python manage.py test                   # everything
```

## Deploying
Set `DJANGO_SECRET_KEY`, `DJANGO_DEBUG=0`, `DJANGO_ALLOWED_HOSTS=yourdomain`, `DJANGO_CSRF_ORIGINS=https://yourdomain`; run `collectstatic`; serve with gunicorn behind nginx (or WhiteNoise / a PaaS). Use Postgres if many editors work at once. Consider email verification or a CAPTCHA on sign-up if the site is public, since anyone can register and comment.

## Ideas for next steps
Attempt history and weak-deck stats per user · spaced repetition on saved questions · timed exam mode · images in questions · comment notifications by email · a public "report this question" count shown to editors.
