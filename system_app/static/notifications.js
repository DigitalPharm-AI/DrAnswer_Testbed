import { installChatLogScrollPreserver } from "./notifications/chat_scroll.js?v=20260714c";
import { createPanelRefresher } from "./notifications/panels.js?v=20260714d";
import { createPolicyConfirmationRenderer } from "./notifications/policy_confirmation.js?v=20260619b";
import { createButton, escapeHtml, fetchNotification, postAction, postFormAction, showNativeNotification } from "./notifications/shared.js?v=20260619b";

(function () {
  const stack = document.getElementById("popup-stack");
  if (!stack) {
    return;
  }

  const state = {
    lastSeenId: getInitialLastSeenId(),
    activeIds: new Set(),
    isPolling: false,
    chatScroll: null,
  };
  let panelRefresher = null;
  let policyConfirmationRenderer = null;

  function refreshPanels() {
    return panelRefresher.refreshPanels();
  }

  function refreshChangedNotifications() {
    return panelRefresher.refreshChangedNotifications();
  }

  function refreshChatPanel() {
    return panelRefresher.refreshChatPanel();
  }

  function refreshChatHistoryPanel() {
    return panelRefresher.refreshChatHistoryPanel();
  }

  async function refreshChangedNotificationsLoop() {
    try {
      await refreshChangedNotifications();
    } catch (_error) {
      // Periodic panel refresh is best-effort and retries after the delay.
    }
    window.setTimeout(refreshChangedNotificationsLoop, 3000);
  }

  function openPage(targetId, updateHash = true) {
    const panels = document.querySelectorAll(".page-tab-panel");
    const tabs = document.querySelectorAll(".app-tab");
    for (const panel of panels) {
      panel.classList.toggle("is-active", panel.id === targetId);
    }
    for (const tab of tabs) {
      tab.classList.toggle("is-active", tab.dataset.tabTarget === targetId);
    }
    document.body.dataset.activeTab = targetId.replace("-page", "");
    if (updateHash) {
      const hash = targetId === "chat-page" ? "#chat" : targetId === "logs-page" ? "#logs" : "#home";
      window.history.replaceState(null, "", hash);
    }
    if (targetId === "chat-page") {
      window.setTimeout(() => {
        const input = document.getElementById("agent-chat-message");
        if (input) {
          input.focus({ preventScroll: true });
        }
      }, 60);
    }
  }

  async function openChatPage(updateHash = true, prefill = "") {
    openPage("chat-page", updateHash);
    state.chatScrollForceBottom = true;
    applyChatPrefill(prefill);
    await Promise.all([refreshChatPanel(), refreshChatHistoryPanel()]);
    window.requestAnimationFrame(() => applyChatPrefill(prefill));
  }

  function applyChatPrefill(prefill = "") {
    const input = document.getElementById("agent-chat-message");
    if (!input) {
      return;
    }
    if (prefill) {
      input.value = prefill;
      input.dispatchEvent(new Event("input", { bubbles: true }));
    }
    input.focus({ preventScroll: true });
  }

  function installAppTabs() {
    for (const tab of document.querySelectorAll(".app-tab[data-tab-target]")) {
      tab.addEventListener("click", () => {
        if (tab.dataset.tabTarget === "chat-page") {
          openChatPage();
          return;
        }
        openPage(tab.dataset.tabTarget);
      });
    }
    document.body.addEventListener("click", (event) => {
      const trigger = event.target.closest("[data-open-chat]");
      if (!trigger) {
        return;
      }
      event.preventDefault();
      openChatPage(true, trigger.dataset.chatPrefill || "");
    });
    document.body.addEventListener("click", (event) => {
      const trigger = event.target.closest("[data-scroll-target]");
      if (!trigger) {
        return;
      }
      const targetSelector = trigger.dataset.scrollTarget || "";
      const target = targetSelector ? document.querySelector(targetSelector) : null;
      if (!(target instanceof HTMLElement)) {
        return;
      }
      event.preventDefault();
      target.scrollIntoView({ behavior: "auto", block: "start" });
      target.focus({ preventScroll: true });
    });
    if (window.location.hash === "#chat") {
      openChatPage(false);
    } else if (window.location.hash === "#logs") {
      openPage("logs-page", false);
    } else {
      openPage("home-page", false);
    }
  }

  function installChatComposerShortcuts() {
    document.body.addEventListener("keydown", (event) => {
      const input = event.target;
      if (!(input instanceof HTMLTextAreaElement) || input.id !== "agent-chat-message") {
        return;
      }
      if (event.key !== "Enter" || event.isComposing || event.keyCode === 229) {
        return;
      }
      if (event.altKey) {
        event.preventDefault();
        const start = input.selectionStart;
        const end = input.selectionEnd;
        input.value = `${input.value.slice(0, start)}\n${input.value.slice(end)}`;
        input.selectionStart = start + 1;
        input.selectionEnd = start + 1;
        input.dispatchEvent(new Event("input", { bubbles: true }));
        return;
      }
      event.preventDefault();
      const form = input.closest("form");
      if (form) {
        form.requestSubmit();
      }
    });
  }

  function installChatDraftPersistence() {
    const storageKey = "daDrug.chatComposerDraft";

    function readDraft() {
      try {
        const value = window.sessionStorage.getItem(storageKey);
        return value ? JSON.parse(value) : null;
      } catch (_error) {
        return null;
      }
    }

    function writeDraft(input) {
      if (!input.value) {
        window.sessionStorage.removeItem(storageKey);
        return;
      }
      window.sessionStorage.setItem(
        storageKey,
        JSON.stringify({
          name: input.name,
          value: input.value,
          selectionStart: input.selectionStart,
          selectionEnd: input.selectionEnd,
        }),
      );
    }

    function restoreDraft() {
      const input = document.getElementById("agent-chat-message");
      const draft = readDraft();
      if (!(input instanceof HTMLTextAreaElement) || !draft || input.value || input.name !== draft.name) {
        return;
      }
      input.value = draft.value || "";
      input.dispatchEvent(new Event("input", { bubbles: true }));
      if (document.activeElement === input) {
        input.setSelectionRange(draft.selectionStart || input.value.length, draft.selectionEnd || input.value.length);
      }
    }

    document.body.addEventListener("input", (event) => {
      const input = event.target;
      if (input instanceof HTMLTextAreaElement && input.id === "agent-chat-message") {
        writeDraft(input);
      }
    });
    document.body.addEventListener("submit", (event) => {
      const form = event.target;
      if (form instanceof HTMLFormElement && form.classList.contains("chat-composer")) {
        window.sessionStorage.removeItem(storageKey);
      }
    });
    new MutationObserver(() => restoreDraft()).observe(document.body, { childList: true, subtree: true });
    restoreDraft();
  }

  function installChatComposerPendingIndicator() {
    function isSystemChatForm(form) {
      const action = form.getAttribute("action") || form.getAttribute("hx-post") || "";
      return action.includes("/chat/system");
    }

    function removeLocalPending() {
      for (const row of document.querySelectorAll("[data-local-pending-agent='true']")) {
        row.remove();
      }
    }

    function appendLocalPending(force = false) {
      const chatLog = document.querySelector("[data-chat-scroll-region]");
      if (!(chatLog instanceof HTMLElement) || (!force && !chatLogNeedsPending(chatLog))) {
        return;
      }
      if (chatLog.querySelector("[data-local-pending-agent='true']")) {
        return;
      }
      const article = document.createElement("article");
      article.className = "chat-message assistant multiturn_chat from-ai multi-turn-bubble pending-response local-pending-response";
      article.dataset.localPendingAgent = "true";
      article.innerHTML = `
        <div class="chat-role">
          <span>AI</span>
          <span class="chat-category">대화</span>
        </div>
        <div class="chat-content chat-pending-content" aria-live="polite">
          <span class="progress-round" aria-hidden="true"></span>
          <span>답변 생성 중</span>
        </div>
      `;
      chatLog.appendChild(article);
      state.chatScrollForceBottom = true;
      chatLog.scrollTop = chatLog.scrollHeight;
      window.requestAnimationFrame(() => {
        chatLog.scrollTop = chatLog.scrollHeight;
      });
    }

    function chatLogNeedsPending(chatLog) {
      if (chatLog.querySelector(".pending-response:not([data-local-pending-agent='true'])")) {
        return false;
      }
      const messages = chatLog.querySelectorAll(".chat-message:not([data-local-pending-agent='true'])");
      const lastMessage = messages[messages.length - 1];
      return lastMessage instanceof HTMLElement && !lastMessage.classList.contains("from-ai");
    }

    function hasAssistantAfterLatestUser(chatLog) {
      const messages = Array.from(chatLog.querySelectorAll(".chat-message:not([data-local-pending-agent='true'])"));
      let latestUserIndex = -1;
      messages.forEach((message, index) => {
        if (message.classList.contains("from-user")) {
          latestUserIndex = index;
        }
      });
      if (latestUserIndex < 0) {
        return false;
      }
      return messages
        .slice(latestUserIndex + 1)
        .some((message) => message.classList.contains("from-ai") && !message.classList.contains("pending-response"));
    }

    function syncLocalPending() {
      const chatLog = document.querySelector("[data-chat-scroll-region]");
      if (!(chatLog instanceof HTMLElement)) {
        return;
      }
      if (hasAssistantAfterLatestUser(chatLog)) {
        state.localChatPendingActive = false;
        removeLocalPending();
        return;
      }
      if (chatLog.querySelector(".pending-response:not([data-local-pending-agent='true'])")) {
        removeLocalPending();
        state.chatScrollForceBottom = true;
        return;
      }
      if (state.localChatPendingActive) {
        appendLocalPending();
      }
    }

    document.body.addEventListener("submit", (event) => {
      const form = event.target;
      if (form instanceof HTMLFormElement && form.classList.contains("chat-composer") && isSystemChatForm(form)) {
        state.localChatPendingActive = true;
        appendLocalPending(true);
      }
    });

    document.body.addEventListener("htmx:beforeRequest", (event) => {
      const form = event.detail && event.detail.elt;
      if (form instanceof HTMLFormElement && form.classList.contains("chat-composer") && isSystemChatForm(form)) {
        state.localChatPendingActive = true;
        appendLocalPending(true);
      }
    });

    document.body.addEventListener("htmx:afterSwap", (event) => {
      syncLocalPending();
    });

    document.body.addEventListener("htmx:afterRequest", (event) => {
      if (event.detail && event.detail.failed) {
        state.localChatPendingActive = false;
        removeLocalPending();
      }
    });
  }

  function installNutritionScenarioRefresh() {
    document.body.addEventListener("htmx:afterRequest", (event) => {
      const source = event.detail && event.detail.elt;
      if (!(source instanceof HTMLFormElement)) {
        return;
      }
      const action = source.getAttribute("action") || source.getAttribute("hx-post") || "";
      const isNutritionScenario = action.includes("/nutrition/scenarios/");
      const isFoodConfirm = action.includes("/chat/food-confirm");
      if (!isNutritionScenario && !isFoodConfirm) {
        return;
      }
      if (isFoodConfirm) {
        panelRefresher.refreshNutritionPanel();
      }
      panelRefresher.refreshNotificationsPanel();
      panelRefresher.refreshChatLogPanel();
      panelRefresher.refreshChatHistoryPanel();
    });
  }

  function isReplyingInAlert() {
    return panelRefresher.isReplyingInAlert();
  }

  function isReplyingInElement(element) {
    return panelRefresher.isReplyingInElement(element);
  }

  function getInitialLastSeenId() {
    const elements = document.querySelectorAll("[data-notification-id]");
    let maxId = 0;
    for (const element of elements) {
      const parsed = Number(element.getAttribute("data-notification-id") || "0");
      if (parsed > maxId) {
        maxId = parsed;
      }
    }
    return maxId;
  }

  async function pollNotifications() {
    if (state.isPolling) {
      return;
    }
    state.isPolling = true;

    try {
      const response = await fetch(`/api/notifications/feed?after_id=${state.lastSeenId}&_=${Date.now()}`, {
        headers: { Accept: "application/json" },
      });
      if (!response.ok) {
        return;
      }

      const payload = await response.json();
      let hasNewNotification = false;
      let hasSuppressedNotification = false;
      let hasNewChatPromptNotification = false;
      for (const notification of payload.notifications || []) {
        state.lastSeenId = Math.max(state.lastSeenId, notification.id);
        const metadata = notification.metadata || {};
        if (
          notification.notification_type === "conversation_alert" &&
          metadata.status === "agent_ready" &&
          ["missed_dose", "policy_confirmation", "side_effect_reminder_safety"].includes(metadata.category)
        ) {
          hasNewChatPromptNotification = true;
        }
        if (notification.metadata && notification.metadata.suppress_popup) {
          hasSuppressedNotification = true;
          continue;
        }
        hasNewNotification = true;
        showPopup(notification);
        showNativeNotification(notification);
      }
      if (hasNewNotification && !isReplyingInAlert()) {
        if (hasNewChatPromptNotification) {
          state.chatScrollForceBottom = true;
        }
        await refreshPanels();
        if (hasNewChatPromptNotification) {
          await refreshChatPanel();
        }
      } else if (hasSuppressedNotification) {
        state.chatScrollForceBottom = true;
        await refreshChatPanel();
      }
    } catch (_error) {
      // Popup polling is best-effort and should not interrupt the page.
    } finally {
      state.isPolling = false;
    }
  }

  function showPopup(notification) {
    const existingToast = findToast(notification.id);
    if (existingToast) {
      updatePopup(existingToast, notification);
      return;
    }
    state.activeIds.add(notification.id);

    const toast = document.createElement("article");
    toast.dataset.notificationId = String(notification.id);
    renderPopup(toast, notification);
    stack.prepend(toast);

    if (!["conversation_alert", "agent_error"].includes(notification.notification_type)) {
      window.setTimeout(() => dismissPopup(toast, notification.id), 8000);
    }
  }

  function findToast(notificationId) {
    return stack.querySelector(`.popup-toast[data-notification-id="${notificationId}"]`);
  }

  function conversationResultClass(notification) {
    const metadata = (notification && notification.metadata) || {};
    if (!notification || notification.notification_type !== "conversation_alert") {
      return "";
    }
    if (metadata.status === "reply_completed") {
      return "reply-success";
    }
    if (["reply_error", "agent_error"].includes(metadata.status)) {
      return "reply-error";
    }
    return "";
  }

  function updatePopup(toast, notification) {
    if (!toast || isReplyingInElement(toast)) {
      return;
    }
    if (toast.dataset.agentProcessing === "true" && !isConversationReplyFinished(notification) && !isConversationReplyFailed(notification)) {
      return;
    }
    if (notification.acknowledged && !isConversationReplyFinished(notification)) {
      dismissPopup(toast, notification.id);
      return;
    }
    renderPopup(toast, notification);
  }

  function renderPopup(toast, notification) {
    delete toast.dataset.agentProcessing;
    toast.className = `popup-toast ${notification.notification_type} ${conversationResultClass(notification)}`;
    toast.dataset.notificationId = String(notification.id);
    toast.dataset.notificationStatus = notification.metadata && notification.metadata.status ? notification.metadata.status : "";
    const metadata = notification.metadata || {};

    const closeButton = createButton("닫기", "popup-toast-close ghost", () => dismissPopup(toast, notification.id));
    const readButton = createButton("읽음", "ghost", async () => {
      await postAction(`/notifications/${notification.id}/ack`);
      dismissPopup(toast, notification.id);
      refreshPanels();
    });

    const actions = document.createElement("div");
    actions.className = "popup-actions";

    if (
      notification.notification_type === "medication_alert" &&
      notification.related_dose_event_id &&
      notification.dose_status !== "taken"
    ) {
      const takeButton = createButton("복약 체크", "", async () => {
        await postAction(`/doses/${notification.related_dose_event_id}/take`);
        dismissPopup(toast, notification.id);
        refreshPanels();
      });
      actions.appendChild(takeButton);
    }

    if (notification.notification_type === "agent_error" && notification.agent_job_id) {
      const retryButton = createButton("다시 시도", "", async () => {
        await postAction(`/agent-jobs/${notification.agent_job_id}/retry?notification_id=${notification.id}`);
        dismissPopup(toast, notification.id);
        refreshPanels();
      });
      actions.appendChild(retryButton);
    }

    if (notification.notification_type === "nutrition_alert") {
      const chatButton = createButton("채팅 상담", "", () => {
        openChatPage(true, `${notification.body} 다음 식사는 어떻게 조정하면 좋을까요?`);
      });
      actions.appendChild(chatButton);
    }

    if (!notification.acknowledged && notification.notification_type !== "conversation_alert") {
      actions.appendChild(readButton);
    }

    const isConversationReady =
      notification.notification_type === "conversation_alert" && metadata.status === "agent_ready";
    const bodyHtml = isConversationReady
      ? `<button class="popup-toast-body popup-chat-jump" type="button" data-open-chat>${escapeHtml(notification.body)}</button>`
      : `<p class="popup-toast-body">${escapeHtml(notification.body)}</p>`;
    toast.innerHTML = `
      <div class="popup-toast-head">
        <div>
          <div class="popup-toast-title">${escapeHtml(notification.title)}</div>
          <span class="tiny-badge">${escapeHtml(notification.notification_type)}</span>
        </div>
      </div>
      ${bodyHtml}
      <div class="popup-toast-meta">${escapeHtml(notification.visible_at_label)}</div>
    `;

    toast.querySelector(".popup-toast-head").appendChild(closeButton);
    if (notification.notification_type === "conversation_alert") {
      const status = notification.metadata && notification.metadata.status;
      if (isConversationReplyFinished(notification)) {
        toast.appendChild(createCompletedReplyPanel(notification));
      } else if (!notification.acknowledged) {
        if (["awaiting_agent", "reply_submitted"].includes(status)) {
          toast.appendChild(createPendingAgentPanel(notification, toast));
        } else if (status === "agent_error") {
          toast.appendChild(createAgentFailurePanel(notification, toast));
        } else if (status === "agent_ready") {
          actions.appendChild(createButton("채팅 열기", "", () => openChatPage()));
        }
      }
    }
    if (actions.children.length > 0) {
      toast.appendChild(actions);
    }
  }

  function dismissPopup(toast, notificationId) {
    if (!toast || toast.classList.contains("fade-out")) {
      return;
    }
    toast.classList.add("fade-out");
    window.setTimeout(() => {
      toast.remove();
      state.activeIds.delete(notificationId);
    }, 200);
  }

  function lockSubmittedReplyForm(form) {
    if (!form || form.classList.contains("is-submitted")) {
      return;
    }
    const input = form.querySelector(".notification-reply-input");
    form.classList.add("is-submitted");
    if (input) {
      input.classList.add("is-submitted");
      input.readOnly = true;
      input.setAttribute("aria-readonly", "true");
    }
    for (const button of form.querySelectorAll("button")) {
      button.disabled = true;
    }
    const externalSubmitButton = form.id ? document.querySelector(`button[form="${CSS.escape(form.id)}"]`) : null;
    if (externalSubmitButton) {
      externalSubmitButton.disabled = true;
    }
  }

  function installReplyFormSubmitLock() {
    document.body.addEventListener(
      "submit",
      (event) => {
        const form = event.target;
        if (!(form instanceof HTMLFormElement)) {
          return;
        }
        const action = form.getAttribute("action") || "";
        const isAlertReply = form.classList.contains("alert-reply-form");
        const isPolicyChoice = Boolean(form.closest(".notification.conversation_alert") && action.includes("/reply"));
        if (!isAlertReply && !isPolicyChoice) {
          return;
        }
        if (isAlertReply) {
          const input = form.querySelector(".notification-reply-input");
          if (!input || !input.value.trim()) {
            return;
          }
          lockSubmittedReplyForm(form);
          showAgentProcessing(form, "답변을 처리하고 있습니다.");
          return;
        }
        lockNotificationActionArea(form);
        showAgentProcessing(form, "정책 변경 선택을 처리하고 있습니다.");
      },
      true,
    );
  }

  function createPendingAgentPanel(notification, toast) {
    const panel = createAgentProgressPanel("미복용 상황을 agent가 확인하고 있습니다.");
    return panel;
  }

  function createAgentFailurePanel(notification, toast) {
    const panel = document.createElement("div");
    panel.className = "prompt-preview alert-agent-reply is-error";

    const label = document.createElement("strong");
    label.textContent = "AI 처리 실패";

    const note = document.createElement("p");
    note.className = "muted inline-note";
    note.textContent = "AI 에이전트 오류 알림에서 다시 시도할 수 있습니다.";

    panel.appendChild(label);
    panel.appendChild(note);
    return panel;
  }

  function createAgentProgressPanel(message) {
    const panel = document.createElement("div");
    panel.className = "prompt-preview alert-agent-progress";
    panel.setAttribute("aria-live", "polite");

    const line = document.createElement("div");
    line.className = "alert-progress-line";

    const spinner = document.createElement("span");
    spinner.className = "progress-round";
    spinner.setAttribute("aria-hidden", "true");

    const label = document.createElement("strong");
    label.textContent = "AI 처리 중";

    const note = document.createElement("p");
    note.textContent = message;

    line.appendChild(spinner);
    line.appendChild(label);
    panel.appendChild(line);
    panel.appendChild(note);
    return panel;
  }

  function showAgentProcessing(element, message) {
    const container = element && element.closest ? element.closest(".notification, .popup-toast") : null;
    if (!container || container.dataset.agentProcessing === "true") {
      return;
    }
    container.dataset.agentProcessing = "true";
    const existing = container.querySelector(".alert-agent-progress");
    if (existing) {
      existing.remove();
    }
    const panel = createAgentProgressPanel(message);
    const notificationActions = container.querySelector(".notification-actions");
    if (notificationActions) {
      container.insertBefore(panel, notificationActions);
    } else {
      container.appendChild(panel);
    }
  }

  function lockNotificationActionArea(element) {
    const container = element && element.closest ? element.closest(".notification, .popup-toast") : null;
    if (!container) {
      return;
    }
    for (const button of container.querySelectorAll(".notification-response-actions button, .popup-actions button")) {
      button.disabled = true;
    }
  }

  function isConversationReplyFinished(notification) {
    const metadata = (notification && notification.metadata) || {};
    return Boolean(
      notification &&
        notification.notification_type === "conversation_alert" &&
        (["reply_completed", "reply_error"].includes(metadata.status) || metadata.agent_reply),
    );
  }

  function isConversationReplyFailed(notification) {
    const metadata = (notification && notification.metadata) || {};
    return Boolean(notification && notification.notification_type === "conversation_alert" && metadata.status === "agent_error");
  }

  function createCompletedReplyPanel(notification) {
    const metadata = notification.metadata || {};
    const isError = metadata.status === "reply_error";
    const panel = document.createElement("div");
    panel.className = `popup-reply-form popup-reply-completed ${isError ? "is-error" : "is-success"}`;

    const label = document.createElement("label");
    label.textContent = isError ? "AI 처리 실패" : "답변 완료";
    panel.appendChild(label);

    const submittedForm = document.createElement("form");
    submittedForm.className = "popup-reply-form is-submitted";

    const submittedLabel = document.createElement("label");
    submittedLabel.textContent = "제출한 답변";

    const textarea = document.createElement("textarea");
    textarea.className = "notification-reply-input popup-reply-input is-submitted";
    textarea.rows = 3;
    textarea.readOnly = true;
    textarea.setAttribute("aria-readonly", "true");
    textarea.value = metadata.patient_reply || "답변이 처리되었습니다.";

    submittedForm.appendChild(submittedLabel);
    submittedForm.appendChild(textarea);
    panel.appendChild(submittedForm);

    const reply = document.createElement("div");
    reply.className = `prompt-preview alert-agent-reply ${isError ? "is-error" : "is-success"}`;

    const replyTitle = document.createElement("strong");
    replyTitle.textContent = isError ? "AI 처리 실패" : "AI 응답 완료";

    const replyBody = document.createElement("p");
    replyBody.textContent = metadata.agent_reply || "응답을 기록했습니다.";

    reply.appendChild(replyTitle);
    reply.appendChild(replyBody);
    panel.appendChild(reply);
    return panel;
  }

  policyConfirmationRenderer = createPolicyConfirmationRenderer({
    createButton,
    fetchNotification,
    postAction,
    postFormAction,
    refreshPanels,
    updatePopup,
    dismissPopup,
    showAgentProcessing,
    lockNotificationActionArea,
  });
  panelRefresher = createPanelRefresher({ stack, updatePopup });
  installAppTabs();
  installChatComposerShortcuts();
  installChatDraftPersistence();
  installChatComposerPendingIndicator();
  installNutritionScenarioRefresh();
  installChatLogScrollPreserver(state);
  installReplyFormSubmitLock();
  pollNotifications();
  window.setInterval(pollNotifications, 1000);
  window.setTimeout(refreshChangedNotificationsLoop, 3000);
})();
