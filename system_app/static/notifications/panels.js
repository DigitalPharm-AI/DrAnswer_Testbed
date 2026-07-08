import { fetchNotification } from "./shared.js";

function cacheBustedUrl(path) {
  const separator = path.includes("?") ? "&" : "?";
  return `${path}${separator}_=${Date.now()}`;
}

function htmxDetail(source, target, response, extra = {}) {
  return {
    elt: source,
    target,
    xhr: {
      status: response.status,
      getResponseHeader: (name) => response.headers.get(name),
    },
    failed: !response.ok,
    successful: response.ok,
    ...extra,
  };
}

function dispatchLifecycleEvent(name, element, detail) {
  if (!element) {
    return true;
  }
  return element.dispatchEvent(new CustomEvent(name, { bubbles: true, cancelable: true, detail }));
}

export function createPanelRefresher({ stack, updatePopup }) {
  function chatComposerSnapshot(targetSelector) {
    if (targetSelector !== "#chat-panel") {
      return null;
    }
    const input = document.getElementById("agent-chat-message");
    if (!(input instanceof HTMLTextAreaElement) || !input.value) {
      return null;
    }
    return {
      value: input.value,
      name: input.name,
      active: document.activeElement === input,
      selectionStart: input.selectionStart,
      selectionEnd: input.selectionEnd,
    };
  }

  function restoreChatComposer(snapshot) {
    if (!snapshot) {
      return;
    }
    const input = document.getElementById("agent-chat-message");
    if (!(input instanceof HTMLTextAreaElement) || input.name !== snapshot.name) {
      return;
    }
    input.value = snapshot.value;
    input.dispatchEvent(new Event("input", { bubbles: true }));
    if (snapshot.active) {
      input.focus({ preventScroll: true });
      input.setSelectionRange(snapshot.selectionStart, snapshot.selectionEnd);
    }
  }

  async function replacePanel(path, targetSelector) {
    const target = document.querySelector(targetSelector);
    if (!target) {
      return false;
    }
    const url = cacheBustedUrl(path);
    if (window.htmx && targetSelector !== "#chat-panel") {
      const composerSnapshot = chatComposerSnapshot(targetSelector);
      await window.htmx.ajax("GET", url, { target: targetSelector, swap: "outerHTML" });
      restoreChatComposer(composerSnapshot);
      return true;
    }
    const response = await fetch(url, { headers: { "HX-Request": "true" } });
    if (!response.ok) {
      return false;
    }
    const html = await response.text();
    const composerSnapshot = chatComposerSnapshot(targetSelector);
    const beforeDetail = htmxDetail(target, target, response, { serverResponse: html });
    if (!dispatchLifecycleEvent("htmx:beforeSwap", target, beforeDetail)) {
      restoreChatComposer(composerSnapshot);
      return false;
    }
    target.outerHTML = html;
    const replacement = document.querySelector(targetSelector);
    dispatchLifecycleEvent(
      "htmx:afterSwap",
      replacement || document.body,
      htmxDetail(target, replacement || target, response, { serverResponse: html }),
    );
    restoreChatComposer(composerSnapshot);
    return true;
  }

  function refreshPanels() {
    return Promise.all([
      refreshNotificationsPanel(),
      refreshTimelinePanel(),
      refreshNutritionPanel(),
      refreshActivePoliciesPanel(),
      refreshChatLogPanel(),
      refreshChatHistoryPanel(),
    ]);
  }

  function refreshNotificationsPanel() {
    return replacePanel("/partials/notifications", "#notifications-panel").catch(() => false);
  }

  function refreshTimelinePanel() {
    return replacePanel("/partials/timeline", "#timeline-panel").catch(() => false);
  }

  function refreshActivePoliciesPanel() {
    return replacePanel("/partials/active-policies", "#active-policies-panel").catch(() => false);
  }

  function refreshNutritionPanel() {
    return replacePanel("/partials/nutrition", "#nutrition-panel").catch(() => false);
  }

  function refreshChatLogPanel() {
    return replacePanel("/partials/chat-log", "#chat-log-region").catch(() => false);
  }

  function refreshChatPanel() {
    return replacePanel("/partials/chat", "#chat-panel").catch(() => false);
  }

  function refreshChatHistoryPanel() {
    return replacePanel("/partials/chat-history", "#conversation-history-region").catch(() => false);
  }

  function refreshChangedNotifications() {
    refreshActiveConversationPopups();
    if (isReplyingInAlert()) {
      return;
    }
    refreshPanels();
  }

  async function refreshActiveConversationPopups() {
    const toasts = Array.from(stack.querySelectorAll(".popup-toast.conversation_alert[data-notification-id]"));
    for (const toast of toasts) {
      if (isReplyingInElement(toast)) {
        continue;
      }
      const notificationId = toast.getAttribute("data-notification-id");
      if (!notificationId) {
        continue;
      }
      try {
        const notification = await fetchNotification(notificationId);
        if (notification) {
          updatePopup(toast, notification);
        }
      } catch (_error) {
        // Existing popup refresh is best-effort.
      }
    }
  }

  function isReplyingInAlert() {
    const focused = document.activeElement;
    return Boolean(
      (focused && focused.classList && focused.classList.contains("notification-reply-input") && !focused.readOnly) ||
        document.querySelector(".alert-reply-form.is-submitted, .popup-reply-form.is-submitted, [data-agent-processing='true']"),
    );
  }

  function isReplyingInElement(element) {
    const focused = document.activeElement;
    return Boolean(focused && element && element.contains(focused) && focused.classList.contains("notification-reply-input") && !focused.readOnly);
  }

  return {
    refreshPanels,
    refreshNotificationsPanel,
    refreshTimelinePanel,
    refreshNutritionPanel,
    refreshActivePoliciesPanel,
    refreshChatLogPanel,
    refreshChatPanel,
    refreshChatHistoryPanel,
    refreshChangedNotifications,
    refreshActiveConversationPopups,
    isReplyingInAlert,
    isReplyingInElement,
  };
}
