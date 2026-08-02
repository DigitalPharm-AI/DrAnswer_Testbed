"use strict";

const {
  assert,
  backendNutritionEvidence,
  diagnosticError,
  isMealApprovalCard,
  isStructuredChatRequest,
  publicEnvelopeData,
  requestBody,
} = require("../validation_helpers");

const llmTimeoutMs = Number(process.env.REAL_LLM_CHAT_TIMEOUT_MS || 135_000);
async function advanceMealInteractionToApproval(
  page,
  initialData,
  initialBackendState,
  report,
) {
  let current = initialData;
  const steps = [];
  for (let index = 0; index < 8; index += 1) {
    if (isMealApprovalCard(current)) {
      report.structured_chat.intermediate_steps = steps;
      return current;
    }
    assert(
      current?.message_type === "selection_box" ||
        current?.message_type === "input_box",
      "MEAL_INTERACTION_STOPPED_AS_TEXT",
      "The meal interaction stopped before the 기록/취소 approval card.",
      {
        step: index,
        current,
        steps,
      },
    );
    const sourceCard = page.locator(
      `.chat-message.assistant[data-message-id=${JSON.stringify(
        current.assistant_message_id,
      )}]`,
    );
    await sourceCard.waitFor({ state: "visible" });

    const beforeStepState = await backendNutritionEvidence(page);
    assert(
      beforeStepState.meal_count === initialBackendState.meal_count,
      "MEAL_SAVED_DURING_INTERMEDIATE_INTERACTION",
      "A meal was saved while resolving food candidates or inputs.",
      {
        step: index,
        initial: initialBackendState,
        current: beforeStepState,
      },
    );

    const responsePromise = page.waitForResponse(
      (response) => isStructuredChatRequest(response.request()),
      { timeout: llmTimeoutMs },
    );
    let submittedValue;
    if (current.message_type === "selection_box") {
      const selections = current.message?.selections ?? [];
      submittedValue = selections.find((value) => value !== "취소");
      assert(
        typeof submittedValue === "string" && submittedValue.length > 0,
        "MEAL_CANDIDATE_SELECTION_MISSING",
        "The meal candidate card did not contain a selectable value.",
        { current },
      );
      await sourceCard
        .getByRole("button", { name: submittedValue, exact: true })
        .click();
    } else {
      const inputs = current.message?.inputs ?? [];
      assert(
        inputs.length > 0,
        "MEAL_INPUT_DEFINITION_MISSING",
        "The meal input card did not contain input definitions.",
        { current },
      );
      const submitted = {};
      for (const input of inputs) {
        const control = sourceCard.getByLabel(input.label, {
          exact: true,
        });
        if (input.type === "dropdown") {
          const value =
            input.value ?? input.options?.selections?.[0] ?? "";
          await control.selectOption(String(value));
          submitted[input.label] = String(value);
        } else {
          const lower = Number(input.options?.lower ?? 1);
          const upper = Number(input.options?.upper ?? 1000);
          const value = Number(
            input.value ?? Math.min(Math.max(100, lower), upper),
          );
          await control.fill(String(value));
          submitted[input.label] = value;
        }
      }
      submittedValue = JSON.stringify(submitted);
      await sourceCard
        .getByRole("button", { name: "입력값 보내기", exact: true })
        .click();
    }

    const response = await responsePromise;
    const request = requestBody(response.request());
    const payload = await response.json();
    const data = publicEnvelopeData(payload);
    assert(
      response.status() === 200 && data,
      "MEAL_INTERMEDIATE_RESPONSE_FAILED",
      "A food candidate or input response did not complete successfully.",
      {
        step: index,
        status: response.status(),
        request,
        payload,
      },
    );
    assert(
      request?.source_message_id === current.assistant_message_id &&
        request?.requested_return_type === current.message_type,
      "MEAL_INTERMEDIATE_SOURCE_MISMATCH",
      "The intermediate response was not bound to its exact source card.",
      {
        step: index,
        expected_source_message_id: current.assistant_message_id,
        expected_type: current.message_type,
        request,
      },
    );
    steps.push({
      step: index + 1,
      source_message_id: current.assistant_message_id,
      source_message_type: current.message_type,
      submitted_value: submittedValue,
      response_message_id: data.assistant_message_id,
      response_message_type: data.message_type,
      response_selections: data.message?.selections ?? null,
      response_inputs: data.message?.inputs ?? null,
    });
    current = data;
  }
  throw diagnosticError(
    "MEAL_INTERACTION_STEP_LIMIT",
    "The meal interaction did not reach approval within eight structured steps.",
    { steps, current },
  );
}

module.exports = { advanceMealInteractionToApproval };
