"use strict";

const { assert, check } = require("../validation_helpers");
async function runProCtcaeStructuredRegression(page, report, dependencies) {
  const { sendDeterministicChat, submitStructuredSelection, screenshot } = dependencies;
  const first = await sendDeterministicChat(
    page,
    "어제 약을 먹고 속이 메스꺼웠어",
  );
  assert(
    (await first.assistant_card.getAttribute("data-message-type")) ===
      "selection_box",
    "PRO_CTCAE_FIRST_QUESTION_TYPE_INVALID",
    "The symptom Tool result did not render the first PRO-CTCAE selection box.",
    { assistant_text: first.assistant_text, events: first.events },
  );
  assert(
    first.assistant_text.includes("(1/2)") &&
      first.assistant_text.includes("얼마나 자주"),
    "PRO_CTCAE_FIRST_QUESTION_MISSING",
    "The first original PRO-CTCAE question was not visible.",
    { assistant_text: first.assistant_text },
  );

  const second = await submitStructuredSelection(
    page,
    first.assistant_card,
    "자주 있다",
  );
  assert(
    (await second.assistant_card.getAttribute("data-message-type")) ===
      "selection_box" &&
      second.assistant_text.includes("(2/2)") &&
      second.assistant_text.includes("가장 심할 때"),
    "PRO_CTCAE_SECOND_QUESTION_MISSING",
    "The first survey answer did not advance to the second original question.",
    { assistant_text: second.assistant_text, events: second.events },
  );

  const approval = await submitStructuredSelection(
    page,
    second.assistant_card,
    "심하다",
  );
  assert(
    (await approval.assistant_card.getAttribute("data-message-type")) ===
      "selection_box" &&
      approval.assistant_text.includes("부작용 평가 기록") &&
      approval.assistant_text.includes("빈도(Frequency)") &&
      approval.assistant_text.includes("자주 있다") &&
      approval.assistant_text.includes("정도(Severity)") &&
      approval.assistant_text.includes("심하다"),
    "PRO_CTCAE_APPROVAL_SUMMARY_INVALID",
    "The completed survey did not render a record approval with the original answers.",
    { assistant_text: approval.assistant_text, events: approval.events },
  );

  const completed = await submitStructuredSelection(
    page,
    approval.assistant_card,
    "기록",
  );
  assert(
    (await completed.assistant_card.getAttribute("data-message-type")) ===
      "text" &&
      completed.assistant_text.includes("기록"),
    "PRO_CTCAE_WRITE_COMPLETION_MISSING",
    "The approved side-effect assessment did not finish with a text result.",
    { assistant_text: completed.assistant_text, events: completed.events },
  );

  report.pro_ctcae = {
    first_question_message_id: first.completed.assistant_message_id,
    second_question_message_id: second.completed.assistant_message_id,
    approval_message_id: approval.completed.assistant_message_id,
    completion_message_id: completed.completed.assistant_message_id,
    answers: ["자주 있다", "심하다"],
    event_types: completed.events.map((event) => event.type),
  };
  check(report, "pro_ctcae_two_question_approval_write", report.pro_ctcae);
  await screenshot(page, "05-v13-pro-ctcae-completed.png");
}

module.exports = { runProCtcaeStructuredRegression };
