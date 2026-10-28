(() => {
  const filter = document.getElementById("exam-semester-filter");
  const rows = [...document.querySelectorAll(".exam-chapter-row")];
  const total = document.getElementById("exam-chapter-total");
  if (!filter || !total || !rows.length) return;

  const updateShare = () => {
    let share = 0;
    rows.forEach(row => {
      const selected = row.querySelector('input[type="checkbox"]');
      const percentage = row.querySelector('input[type="number"]');
      percentage.disabled = !selected.checked;
      percentage.required = selected.checked;
      if (selected.checked) share += Number(percentage.value || 0);
    });
    total.textContent = `Selected chapter share: ${share}%${share === 100 ? " ✓" : " (must total 100%)"}`;
    total.classList.toggle("is-valid", share === 100);
  };

  filter.addEventListener("change", () => {
    rows.forEach(row => {
      row.hidden = filter.value !== "all" && row.dataset.semester !== filter.value;
    });
  });
  rows.forEach(row => {
    row.querySelector('input[type="checkbox"]').addEventListener("change", updateShare);
    row.querySelector('input[type="number"]').addEventListener("input", updateShare);
  });
  updateShare();
})();
