export function createPolicyConfirmationRenderer({
  createButton,
  fetchNotification,
  postAction,
  postFormAction,
  refreshPanels,
  updatePopup,
  dismissPopup,
  showAgentProcessing,
  lockNotificationActionArea,
}) {
  function createPolicyConfirmationForm(notification, toast) {
    const panel = document.createElement("div");
    panel.className = "popup-reply-form policy-confirmation-card";

    const label = document.createElement("label");
    label.textContent = "정책 변경 선택";

    const note = document.createElement("p");
    note.className = "muted inline-note";
    const metadata = notification.metadata || {};
    const multipleChoice = metadata.multiple_choice || {};
    const policyChange = metadata.policy_change || {};
    note.textContent = policyChange.question || multipleChoice.question || notification.body || "알림 설정을 변경하시는 건 어떨까요?";

    const candidates = Array.isArray(policyChange.candidates) ? policyChange.candidates : [];

    const actions = document.createElement("div");
    actions.className = "popup-actions policy-choice-actions";

    const fallbackOptions = [
      { number: 1, value: "increase", label: "늘리기", text: "1. 늘리기" },
      { number: 2, value: "decrease", label: "줄이기", text: "2. 줄이기" },
      { number: 3, value: "keep", label: "현행 유지", text: "3. 현행 유지" },
    ];
    const options = Array.isArray(multipleChoice.options) && multipleChoice.options.length > 0
      ? multipleChoice.options
      : fallbackOptions;
    options.forEach((option, index) => {
      const optionButton = document.createElement("button");
      optionButton.type = "button";
      optionButton.className = "policy-choice-button";

      const number = document.createElement("span");
      number.className = "policy-option-number";
      number.textContent = String(policyOptionNumber(option, index));

      const labelText = document.createElement("span");
      labelText.className = "policy-option-label";
      labelText.textContent = policyOptionLabel(option);

      optionButton.appendChild(number);
      optionButton.appendChild(labelText);
      optionButton.addEventListener("click", async () => {
        lockNotificationActionArea(optionButton);
        showAgentProcessing(optionButton, "정책 변경 선택을 처리하고 있습니다.");
        const formData = new FormData();
        formData.set("message", policyChoicePayload(option, index));
        await postFormAction(`/notifications/${notification.id}/reply`, formData);
        const refreshedNotification = await fetchNotification(notification.id);
        if (refreshedNotification) {
          updatePopup(toast, refreshedNotification);
        }
        refreshPanels();
      });
      actions.appendChild(optionButton);
    });

    panel.appendChild(label);
    panel.appendChild(note);
    if (candidates.length > 0) {
      panel.appendChild(createPolicyChangeTable(candidates));
    }
    panel.appendChild(actions);
    return panel;
  }

  return { createPolicyConfirmationForm };
}


function policyOptionNumber(option, index) {
  if (option && typeof option === "object" && option.number) {
    return option.number;
  }
  return index + 1;
}


function policyOptionLabel(option) {
  if (typeof option === "string") {
    return option.replace(/^\d+\.\s*/, "");
  }
  return option.label || option.text || option.value || "선택";
}


function policyChoicePayload(option, index) {
  if (typeof option === "string") {
    return JSON.stringify({
      prompt_type: "policy_confirmation",
      action: option,
      number: index + 1,
      label: policyOptionLabel(option),
    });
  }
  return JSON.stringify({
    prompt_type: "policy_confirmation",
    action: option.value || option.text || option.label,
    number: option.number || index + 1,
    label: option.label || option.text || option.value,
  });
}


function createPolicyChangeTable(candidates) {
  const wrapper = document.createElement("div");
  wrapper.className = "policy-change-table-wrap";

  const table = document.createElement("table");
  table.className = "policy-change-table";

  const thead = document.createElement("thead");
  const headRow = document.createElement("tr");
  for (const label of ["시간대", "현재", "변경 후"]) {
    const th = document.createElement("th");
    th.textContent = label;
    headRow.appendChild(th);
  }
  thead.appendChild(headRow);
  table.appendChild(thead);

  const tbody = document.createElement("tbody");
  for (const candidate of candidates) {
    const row = document.createElement("tr");
    for (const value of [
      candidate.slot_label || "",
      policyCandidateSummary(candidate, "current"),
      policyCandidateSummary(candidate, "proposed"),
    ]) {
      const cell = document.createElement("td");
      cell.textContent = value;
      row.appendChild(cell);
    }
    tbody.appendChild(row);
  }
  table.appendChild(tbody);
  wrapper.appendChild(table);
  return wrapper;
}


function policyCandidateSummary(candidate, key) {
  const values = candidate && typeof candidate === "object" ? candidate[key] : null;
  if (values && typeof values === "object" && values.summary) {
    return values.summary;
  }
  return "";
}
