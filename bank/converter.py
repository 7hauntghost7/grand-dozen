"""Convert numbered question documents into the bank import format."""
import re
from dataclasses import dataclass, field

from . import parser


QUESTION_RE = re.compile(r"^\s*(\d+)\.\s*(.*)$")
OPTION_RE = re.compile(r"^\s*([A-H])[\.\)]\s*(.*?)\s*$", re.I)
ANSWER_RE = re.compile(r"^\s*(?:Correct\s+)?Answer\s*:\s*(.*)$", re.I)
EXPLANATION_RE = re.compile(r"^\s*Explanation\s*:\s*(.*)$", re.I)
LETTER_ANSWER_RE = re.compile(
    r"^\s*([A-H](?:\s*(?:,|and|&)\s*[A-H])*)"
    r"(?:\s*(?:[—–-]|[.:])\s*(.*))?\s*$",
    re.I,
)


@dataclass
class SourceQuestion:
    number: str
    line: int
    stem: str = ""
    lines: list = field(default_factory=list)
    option_letters: list = field(default_factory=list)
    options: list = field(default_factory=list)
    answer: str = ""
    explanation: str = ""
    answer_seen: bool = False


@dataclass
class ConversionResult:
    output: str
    questions: list
    issues: list = field(default_factory=list)


def _clean_markup(text):
    return text.replace("**", "").replace("__", "").replace("\u00a0", " ").strip()


def _source_questions(text):
    questions = []
    preamble = []
    current = None

    for line_number, raw_line in enumerate((text or "").replace("\r\n", "\n").split("\n"), 1):
        line = _clean_markup(raw_line)
        match = QUESTION_RE.match(line)
        if match:
            current = SourceQuestion(number=match.group(1), line=line_number, stem=match.group(2))
            questions.append(current)
        elif current is None:
            if line:
                preamble.append((line_number, line))
        else:
            current.lines.append(line)

    return questions, preamble


def _read_question_lines(question, lines):
    state = "stem"
    question.stem = question.stem.strip()
    for raw_line in lines:
        line = _clean_markup(raw_line)
        if not line:
            continue
        answer_match = ANSWER_RE.match(line)
        explanation_match = EXPLANATION_RE.match(line)
        option_match = OPTION_RE.match(line)
        if answer_match:
            question.answer = answer_match.group(1).strip()
            question.answer_seen = True
            state = "answer"
        elif explanation_match:
            question.explanation = explanation_match.group(1).strip()
            state = "explanation"
        elif option_match:
            question.option_letters.append(option_match.group(1).upper())
            question.options.append(option_match.group(2).strip())
            state = "option"
        elif state == "stem":
            question.stem = "\n".join(filter(None, [question.stem, line]))
        elif state == "option" and question.options:
            question.options[-1] = " ".join(filter(None, [question.options[-1], line]))
        elif state == "answer":
            question.answer = " ".join(filter(None, [question.answer, line]))
        elif state == "explanation":
            question.explanation = " ".join(filter(None, [question.explanation, line]))


def _answer_letters(answer, options):
    match = LETTER_ANSWER_RE.match(answer)
    if match:
        letters = re.findall(r"[A-H]", match.group(1).upper())
        label = (match.group(2) or "").strip()
        if label:
            normalized_label = parser.norm(label)
            selected = [options[ord(letter) - ord("A")] for letter in letters
                        if ord(letter) - ord("A") < len(options)]
            if (len(selected) != len(letters)
                    or len(letters) != 1
                    or parser.norm(selected[0]) != normalized_label):
                return [], "The answer text does not match its option letter; check the source."
        return letters, ""

    normalized_answer = parser.norm(answer)
    matching = [index for index, option in enumerate(options)
                if parser.norm(option) == normalized_answer]
    if len(matching) == 1:
        return [chr(ord("A") + matching[0])], ""
    return [], "Could not safely identify the correct option from the answer text."


def convert_source(text, deck_title="Converted Questions", subject="", semester=""):
    """Convert source questions and validate the generated text with the import parser."""
    source_questions, preamble = _source_questions(text)
    issues = []
    if preamble:
        first_line, first_text = preamble[0]
        issues.append(
            f"Text before the first numbered question (line {first_line}) was ignored: "
            f"{first_text[:100]}"
        )
    if not source_questions and (text or "").strip():
        issues.append("No numbered questions found. Questions should start with a number and period.")

    converted = []
    for question in source_questions:
        _read_question_lines(question, question.lines)

        data = {
            "number": question.number,
            "stem": question.stem.strip(),
            "explanation": question.explanation.strip(),
            "note": "",
        }
        if question.options:
            data["kind"] = parser.MCQ
            expected_letters = [chr(ord("A") + index) for index in range(len(question.options))]
            if question.option_letters != expected_letters:
                letters = []
                answer_issue = (
                    "Source options must be labeled A, B, C… in order; found "
                    + ", ".join(question.option_letters)
                    + "."
                )
            else:
                letters, answer_issue = _answer_letters(question.answer, question.options)
            if answer_issue:
                data["note"] = (
                    f"Manual review required: {answer_issue} "
                    f"Source answer: {question.answer or '(missing)'}"
                )
                correct_letters = set()
            else:
                correct_letters = set(letters)
            data["options"] = [(option, chr(ord("A") + i) in correct_letters)
                               for i, option in enumerate(question.options)]
        elif LETTER_ANSWER_RE.match(question.answer):
            data["kind"] = parser.MCQ
            data["options"] = []
            data["note"] = "Manual review required: answer key indicates an MCQ, but no options were found."
        else:
            data["kind"] = parser.DESC
            data["answer"] = question.answer.strip()
            data["options"] = []
            if not question.answer_seen:
                data["note"] = "Manual review required: no Correct Answer field was found."

        if question.answer_seen and re.search(r"manual\s+review\s+required", question.answer, re.I):
            data["note"] = (
                "Manual review required in source. Original Correct Answer: "
                + question.answer
            )
            if data["kind"] == parser.DESC:
                data["answer"] = ""
            else:
                data["options"] = [(option, False) for option, _ in data["options"]]

        converted.append(data)

    try:
        semester_number = int(semester) if str(semester).strip() else None
    except ValueError:
        semester_number = None
        issues.append(f"Semester “{semester}” is not a number; it was omitted.")
    if semester_number is not None and not 1 <= semester_number <= 12:
        issues.append(f"Semester {semester_number} is outside the supported range 1–12; it was omitted.")
        semester_number = None

    output = parser.to_text(
        (deck_title or "").strip() or "Converted Questions",
        converted,
        subject=(subject or "").strip() or None,
        semester=semester_number,
    )
    validation = parser.parse(output).questions
    return ConversionResult(output=output, questions=validation, issues=issues)
