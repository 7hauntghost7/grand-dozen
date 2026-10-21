"""Launch the local question-format converter."""
import sys
from pathlib import Path
import tkinter as tk
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bank.converter import convert_source


class QuestionConverter(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Question Format Converter")
        self.geometry("1180x850")
        self.minsize(850, 650)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=2)
        self.rowconfigure(5, weight=3)

        ttk.Label(
            self,
            text="Paste numbered questions with options, Correct Answer, and Explanation. "
            "Descriptive questions use Correct Answer as the written answer.",
            wraplength=1100,
        ).grid(row=0, column=0, sticky="w", padx=10, pady=(10, 6))

        metadata = ttk.Frame(self)
        metadata.grid(row=1, column=0, sticky="ew", padx=10, pady=5)
        ttk.Label(metadata, text="Subject").grid(row=0, column=0, sticky="w")
        self.subject = ttk.Entry(metadata, width=24)
        self.subject.grid(row=0, column=1, padx=(5, 16))
        ttk.Label(metadata, text="Semester").grid(row=0, column=2, sticky="w")
        self.semester = ttk.Entry(metadata, width=8)
        self.semester.grid(row=0, column=3, padx=(5, 16))
        ttk.Label(metadata, text="Deck title").grid(row=0, column=4, sticky="w")
        self.deck = ttk.Entry(metadata)
        self.deck.insert(0, "Converted Questions")
        self.deck.grid(row=0, column=5, sticky="ew", padx=5)
        metadata.columnconfigure(5, weight=1)

        ttk.Label(self, text="Source questions").grid(row=2, column=0, sticky="w", padx=10)
        self.source = ScrolledText(self, wrap="word", height=14)
        self.source.grid(row=3, column=0, sticky="nsew", padx=10, pady=6)

        actions = ttk.Frame(self)
        actions.grid(row=4, column=0, sticky="ew", padx=10, pady=4)
        ttk.Button(actions, text="Convert and validate", command=self.convert).pack(side="left")
        ttk.Button(actions, text="Copy output", command=self.copy_output).pack(side="left", padx=8)
        self.status = ttk.Label(actions, text="Paste text, then convert.")
        self.status.pack(side="left", padx=8)

        result_frame = ttk.Panedwindow(self, orient="vertical")
        result_frame.grid(row=5, column=0, sticky="nsew", padx=10, pady=(4, 10))
        output_panel = ttk.Frame(result_frame)
        issue_panel = ttk.Frame(result_frame)
        output_panel.rowconfigure(1, weight=1)
        output_panel.columnconfigure(0, weight=1)
        issue_panel.rowconfigure(1, weight=1)
        issue_panel.columnconfigure(0, weight=1)
        ttk.Label(output_panel, text="Website import format").grid(row=0, column=0, sticky="w")
        self.output = ScrolledText(output_panel, wrap="word", height=14)
        self.output.grid(row=1, column=0, sticky="nsew", pady=(4, 8))
        ttk.Label(issue_panel, text="Validation and review notes").grid(row=0, column=0, sticky="w")
        self.diagnostics = ScrolledText(issue_panel, wrap="word", height=8, state="disabled")
        self.diagnostics.grid(row=1, column=0, sticky="nsew", pady=(4, 0))
        result_frame.add(output_panel, weight=3)
        result_frame.add(issue_panel, weight=2)

    def convert(self):
        result = convert_source(
            self.source.get("1.0", "end-1c"),
            deck_title=self.deck.get(),
            subject=self.subject.get(),
            semester=self.semester.get(),
        )
        self.output.delete("1.0", "end")
        self.output.insert("1.0", result.output)

        lines = []
        lines.extend(f"Review: {issue}" for issue in result.issues)
        valid_count = 0
        for question in result.questions:
            label = f"Question {question.number or question.line}"
            if question.ok:
                valid_count += 1
                lines.append(f"{label}: OK")
            else:
                lines.append(f"{label}: ERROR")
            lines.extend(f"  ERROR: {message}" for message in question.errors)
            lines.extend(f"  WARNING: {message}" for message in question.warnings)
            if question.note:
                lines.append(f"  NOTE: {question.note}")
            if question.duplicate_of:
                lines.append(f"  WARNING: Duplicate: {question.duplicate_of}")

        self.diagnostics.configure(state="normal")
        self.diagnostics.delete("1.0", "end")
        self.diagnostics.insert("1.0", "\n".join(lines) or "No questions found.")
        self.diagnostics.configure(state="disabled")
        self.status.configure(
            text=f"{valid_count}/{len(result.questions)} questions pass the website parser."
        )

    def copy_output(self):
        text = self.output.get("1.0", "end-1c")
        self.clipboard_clear()
        self.clipboard_append(text)
        self.update()
        self.status.configure(text="Output copied to clipboard.")


if __name__ == "__main__":
    QuestionConverter().mainloop()
