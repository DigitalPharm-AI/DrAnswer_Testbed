(() => {
  const LOAD_MARK = "data-hx-lite-load-bound";
  const EVERY_MARK = "data-hx-lite-every-bound";
  const DEFAULT_REQUEST_TIMEOUT_MS = 15000;
  const inFlightSources = new WeakSet();
  const busyStateBySource = new WeakMap();
  let activeRequestCount = 0;

  const ERROR_CODE_MESSAGES = {
    invalid_date_format: "날짜 형식을 확인해주세요.",
    invalid_date_range: "종료일은 시작일보다 빠를 수 없습니다.",
    invalid_schedule_time_format: "복약 시간을 확인해주세요.",
    required_choice_missing: "필수 선택 항목을 확인해주세요.",
    required_schedule_missing: "복약 시간 조합을 선택해주세요.",
    medication_form_invalid: "복약 입력값을 확인해주세요.",
    phr_medication_required: "복약 계획을 먼저 등록해주세요.",
    medication_name_too_long: "약 이름은 255자 이하로 입력해주세요.",
    medication_dosage_too_long: "용량은 255자 이하로 입력해주세요.",
    medication_instructions_too_long: "추가 지시는 2,000자 이하로 입력해주세요.",
    medication_submission_id_invalid: "복약 등록 요청 식별자가 올바르지 않습니다. 화면을 새로고침해주세요.",
    medication_submission_conflict: "동일한 복약 등록 요청의 입력값이 변경되었습니다. 화면을 새로고침 후 다시 시도해주세요.",
    clock_advance_minutes_invalid: "지원하는 시간 진행 단위를 선택해주세요.",
    clock_speed_multiplier_invalid: "지원하는 재생 속도를 선택해주세요.",
    mutation_confirmation_execution_in_progress: "변경 작업이 처리 중입니다. 완료 후 다시 시도해주세요.",
    mutation_confirmation_not_found: "변경 확인 요청이 만료되었거나 존재하지 않습니다.",
    stale_food_selection: "이전 음식 선택 단계가 만료되었습니다. 최신 대화를 확인해주세요.",
    stale_contract_response: "이 응답 요청은 더 이상 유효하지 않습니다. 최신 대화를 확인해주세요.",
    pending_response_already_answered: "이미 응답한 요청입니다.",
    pending_response_type_mismatch: "요청된 입력 형식과 응답 형식이 일치하지 않습니다.",
    chat_message_not_found: "대화 항목이 만료되었거나 존재하지 않습니다.",
    notification_not_found: "알림이 만료되었거나 존재하지 않습니다.",
    dose_event_not_found: "복약 일정이 만료되었거나 존재하지 않습니다.",
    medication_plan_not_found: "복약 계획이 이미 삭제되었거나 존재하지 않습니다.",
    agent_job_not_found: "재시도할 작업이 이미 종료되었거나 존재하지 않습니다.",
    notification_agent_job_mismatch: "알림과 재시도 작업이 일치하지 않습니다. 화면을 새로 확인해주세요.",
    agent_job_retry_in_progress: "AI 작업 재시도 요청을 처리 중입니다.",
    agent_job_retry_stale: "AI 작업 상태가 이미 변경되었습니다. 최신 알림을 확인해주세요.",
    agent_task_retry_failed: "AI 작업 재시도 요청을 전달하지 못했습니다. 알림은 유지되므로 다시 시도할 수 있습니다.",
    chat_message_required: "메시지를 입력해주세요.",
    chat_message_too_long: "메시지는 4,000자 이하로 입력해주세요.",
    chat_event_type_invalid: "지원하지 않는 채팅 요청입니다.",
    chat_contract_version_invalid: "지원하지 않는 채팅 계약 버전입니다.",
    ae_response_required: "문항 응답을 입력해주세요.",
    ae_response_too_long: "문항 응답은 4,000자 이하로 입력해주세요.",
    ae_question_invalid: "응답할 문항이 유효하지 않습니다.",
    ae_question_already_answered: "이미 응답한 문항입니다.",
    invalid_food_portion: "섭취량은 1g 이상 2,000g 이하로 입력해주세요.",
    invalid_meal_type: "식사 종류를 다시 선택해주세요.",
  };

  function detail(elt, target, xhr, extra = {}) {
    return {
      elt,
      target,
      xhr,
      failed: xhr ? xhr.status >= 400 : false,
      successful: xhr ? xhr.status >= 200 && xhr.status < 400 : true,
      ...extra,
    };
  }

  function dispatch(name, elt, payload) {
    const event = new CustomEvent(name, {
      bubbles: true,
      cancelable: true,
      detail: payload,
    });
    elt.dispatchEvent(event);
    return !event.defaultPrevented;
  }

  function feedbackElement() {
    return document.getElementById("app-request-feedback");
  }

  function showFeedback(message, kind = "error") {
    const element = feedbackElement();
    if (!element) {
      return;
    }
    element.textContent = String(message || "요청을 처리하지 못했습니다. 잠시 후 다시 시도해주세요.");
    element.dataset.kind = kind;
    element.hidden = false;
    if (kind === "error") {
      element.setAttribute("role", "alert");
    } else {
      element.setAttribute("role", "status");
    }
  }

  function clearFeedback() {
    const element = feedbackElement();
    if (!element) {
      return;
    }
    element.hidden = true;
    element.textContent = "";
    delete element.dataset.kind;
  }

  function errorCodeFromBody(rawBody) {
    if (!rawBody) {
      return "";
    }
    try {
      const payload = JSON.parse(rawBody);
      if (typeof payload.detail === "string") {
        return payload.detail.split(":", 1)[0].trim();
      }
      if (payload.error && typeof payload.error.code === "string") {
        return payload.error.code.trim();
      }
    } catch (_error) {
      return "";
    }
    return "";
  }

  function responseErrorMessage(status, rawBody) {
    const code = errorCodeFromBody(rawBody);
    if (code && ERROR_CODE_MESSAGES[code]) {
      return ERROR_CODE_MESSAGES[code];
    }
    if (status === 400 || status === 422) {
      return "입력값을 확인해주세요.";
    }
    if (status === 401 || status === 403) {
      return "요청 권한을 확인해주세요.";
    }
    if (status === 404) {
      return "대상이 변경되었거나 존재하지 않습니다. 화면을 새로 확인해주세요.";
    }
    if (status === 409) {
      return "화면 상태가 변경되었습니다. 최신 상태를 확인한 뒤 다시 시도해주세요.";
    }
    if (status >= 500) {
      return "서버에서 요청을 처리하지 못했습니다. 잠시 후 다시 시도해주세요.";
    }
    return "요청을 처리하지 못했습니다. 다시 시도해주세요.";
  }

  function setSourceBusy(source, busy) {
    if (!(source instanceof Element)) {
      return;
    }
    if (busy) {
      const controls = [];
      if (source instanceof HTMLButtonElement || source instanceof HTMLInputElement) {
        controls.push([source, source.disabled]);
        source.disabled = true;
      } else {
        for (const control of source.querySelectorAll("button[type='submit'], input[type='submit']")) {
          controls.push([control, control.disabled]);
          control.disabled = true;
        }
      }
      busyStateBySource.set(source, {
        ariaBusy: source.getAttribute("aria-busy"),
        controls,
      });
      source.setAttribute("aria-busy", "true");
      source.classList.add("htmx-request");
      activeRequestCount += 1;
      document.body.classList.add("htmx-request");
      return;
    }

    const state = busyStateBySource.get(source);
    if (state) {
      for (const [control, wasDisabled] of state.controls) {
        if (control.isConnected) {
          control.disabled = wasDisabled;
        }
      }
      if (state.ariaBusy === null) {
        source.removeAttribute("aria-busy");
      } else {
        source.setAttribute("aria-busy", state.ariaBusy);
      }
      busyStateBySource.delete(source);
    }
    source.classList.remove("htmx-request");
    activeRequestCount = Math.max(0, activeRequestCount - 1);
    if (activeRequestCount === 0) {
      document.body.classList.remove("htmx-request");
    }
  }

  function targetFor(elt, explicitTarget) {
    const selector = explicitTarget || elt.getAttribute("hx-target");
    if (selector) {
      return document.querySelector(selector);
    }
    return elt;
  }

  function swap(target, html, mode) {
    if (!target || !html) {
      return target;
    }
    if ((mode || "").toLowerCase().includes("outerhtml")) {
      const template = document.createElement("template");
      template.innerHTML = html.trim();
      const replacement = template.content.firstElementChild;
      if (!replacement) {
        target.outerHTML = html;
        return document.body;
      }
      target.replaceWith(replacement);
      return replacement;
    }
    target.innerHTML = html;
    return target;
  }

  async function request(method, url, options = {}) {
    const source = options.source || document.body;
    if (inFlightSources.has(source)) {
      return null;
    }
    const target = targetFor(source, options.target);
    const beforePayload = detail(source, target, null);
    if (!dispatch("htmx:beforeRequest", source, beforePayload)) {
      return null;
    }

    inFlightSources.add(source);
    setSourceBusy(source, true);
    const timeoutMs = Number(options.timeoutMs) > 0 ? Number(options.timeoutMs) : DEFAULT_REQUEST_TIMEOUT_MS;
    const controller = new AbortController();
    const timeoutId = window.setTimeout(() => controller.abort(), timeoutMs);
    try {
      const fetchOptions = {
        method,
        signal: controller.signal,
        headers: {
          "HX-Request": "true",
          ...(options.headers || {}),
        },
      };
      if (options.body) {
        fetchOptions.body = options.body;
      }

      const response = await fetch(url, fetchOptions);
      const xhr = {
        status: response.status,
        getResponseHeader: (name) => response.headers.get(name),
      };

      if ((response.headers.get("HX-Refresh") || "").toLowerCase() === "true") {
        dispatch("htmx:afterRequest", source, detail(source, target, xhr));
        if (method !== "GET") {
          clearFeedback();
        }
        window.location.reload();
        return response;
      }

      if (response.status === 204) {
        dispatch("htmx:afterRequest", source, detail(source, target, xhr));
        if (method !== "GET") {
          clearFeedback();
        }
        return response;
      }

      const html = await response.text();
      const afterPayload = detail(source, target, xhr, { serverResponse: html });
      dispatch("htmx:afterRequest", source, afterPayload);
      if (!response.ok) {
        const errorPayload = detail(source, target, xhr, {
          failed: true,
          successful: false,
          serverResponse: html,
        });
        dispatch("htmx:responseError", source, errorPayload);
        if (method !== "GET") {
          showFeedback(responseErrorMessage(response.status, html));
        }
        return response;
      }
      if (method !== "GET") {
        clearFeedback();
      }
      const swapPayload = detail(source, target, xhr, { serverResponse: html });
      if (dispatch("htmx:beforeSwap", target || source, swapPayload)) {
        const swappedTarget = swap(target, html, options.swap || source.getAttribute("hx-swap") || "innerHTML");
        dispatch("htmx:afterSwap", swappedTarget || target || source, detail(source, swappedTarget || target, xhr, { serverResponse: html }));
        bindTriggers(document, { runLoad: false });
      }
      return response;
    } catch (error) {
      const xhr = {
        status: 0,
        getResponseHeader: () => null,
      };
      dispatch("htmx:afterRequest", source, detail(source, target, xhr, { failed: true, successful: false, error }));
      dispatch("htmx:sendError", source, detail(source, target, xhr, { failed: true, successful: false, error }));
      if (method !== "GET") {
        showFeedback(
          error && error.name === "AbortError"
            ? "요청 시간이 초과되었습니다. 다시 시도해주세요."
            : "서버에 연결하지 못했습니다. 연결 상태를 확인한 뒤 다시 시도해주세요.",
        );
      }
      throw error;
    } finally {
      window.clearTimeout(timeoutId);
      inFlightSources.delete(source);
      setSourceBusy(source, false);
    }
  }

  function formBody(form, submitter) {
    const data = new FormData(form);
    if (submitter && submitter.name) {
      data.set(submitter.name, submitter.value || "");
    }
    return new URLSearchParams(data);
  }

  function requestFromElement(elt, submitter = null) {
    const submitterPost = submitter && submitter.getAttribute("hx-post");
    const submitterGet = submitter && submitter.getAttribute("hx-get");
    const submitterAction = submitter && submitter.getAttribute("formaction");
    const method = submitterPost || elt.getAttribute("hx-post") ? "POST" : "GET";
    const url =
      submitterPost ||
      submitterGet ||
      submitterAction ||
      elt.getAttribute("hx-post") ||
      elt.getAttribute("hx-get") ||
      elt.getAttribute("action") ||
      window.location.pathname;
    const target = (submitter && submitter.getAttribute("hx-target")) || elt.getAttribute("hx-target") || "";
    const swapMode = (submitter && submitter.getAttribute("hx-swap")) || elt.getAttribute("hx-swap") || "innerHTML";
    const body = method === "POST" && elt.tagName === "FORM" ? formBody(elt, submitter) : null;
    const headers = body ? { "Content-Type": "application/x-www-form-urlencoded" } : {};
    return request(method, url, { source: elt, target, swap: swapMode, body, headers });
  }

  function clickTriggerFor(target) {
    if (!(target instanceof Element)) {
      return null;
    }
    return target.closest("button[hx-post], button[hx-get], a[hx-post], a[hx-get], [role='button'][hx-post], [role='button'][hx-get]");
  }

  function confirmIfNeeded(elt) {
    const message = elt.getAttribute("hx-confirm");
    return !message || window.confirm(message);
  }

  function parseEvery(trigger) {
    const match = String(trigger || "").match(/every\s+(\d+(?:\.\d+)?)s/i);
    return match ? Number(match[1]) * 1000 : 0;
  }

  function isPollingEligible(elt) {
    if (document.hidden || !elt.isConnected) {
      return false;
    }
    const tabPanel = elt.closest(".page-tab-panel");
    return !tabPanel || tabPanel.classList.contains("is-active");
  }

  function bindTriggers(root = document, options = {}) {
    const runLoad = options.runLoad !== false;
    root.querySelectorAll("[hx-get][hx-trigger]").forEach((elt) => {
      const trigger = elt.getAttribute("hx-trigger") || "";
      let loadRequest = Promise.resolve();
      if (runLoad && trigger.includes("load") && !elt.hasAttribute(LOAD_MARK) && isPollingEligible(elt)) {
        elt.setAttribute(LOAD_MARK, "true");
        loadRequest = requestFromElement(elt).catch(() => {});
      }
      const everyMs = parseEvery(trigger);
      if (everyMs > 0 && !elt.hasAttribute(EVERY_MARK)) {
        elt.setAttribute(EVERY_MARK, "true");
        const pollAfterCompletion = () => {
          window.setTimeout(async () => {
            if (!elt.isConnected) {
              return;
            }
            if (isPollingEligible(elt)) {
              await requestFromElement(elt).catch(() => {});
            }
            pollAfterCompletion();
          }, everyMs);
        };
        loadRequest.finally(() => {
          if (elt.isConnected) {
            pollAfterCompletion();
          }
        });
      }
    });
  }

  document.addEventListener("submit", (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement) || !form.hasAttribute("hx-post")) {
      return;
    }
    if (!confirmIfNeeded(form)) {
      event.preventDefault();
      return;
    }
    event.preventDefault();
    requestFromElement(form, event.submitter).then(() => bindTriggers(document)).catch(() => {});
  });

  document.addEventListener("click", (event) => {
    const elt = clickTriggerFor(event.target);
    if (!elt || elt.tagName === "FORM") {
      return;
    }
    if (elt.tagName === "BUTTON" && elt.form) {
      return;
    }
    if (!confirmIfNeeded(elt)) {
      event.preventDefault();
      return;
    }
    event.preventDefault();
    requestFromElement(elt).then(() => bindTriggers(document)).catch(() => {});
  });

  window.htmx = {
    ajax(method, url, options = {}) {
      const source = options.source || targetFor(document.body, options.target) || document.body;
      return request(String(method || "GET").toUpperCase(), url, {
        source,
        target: options.target,
        swap: options.swap,
        timeoutMs: options.timeoutMs,
      });
    },
  };
  window.appFeedback = {
    error(message) {
      showFeedback(message, "error");
    },
    clear: clearFeedback,
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => bindTriggers(document));
  } else {
    bindTriggers(document);
  }
})();
