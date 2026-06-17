import { fetchNotification } from "./shared.js";

function cacheBustedUrl(path) {
  const separator = path.includes("?") ? "&" : "?";
  return `${path}${separator}_=${Date.now()}`;
}

export function createPanelRefresher({ stack, updatePopup }) {
  async function replacePanel(path, targetSelector) {
    const target = document.querySelector(targetSelector);
    if (!target) {
      return false;
    }
    const url = cacheBustedUrl(path);
    if (window.htmx) {
      await window.htmx.ajax("GET", url, { target: targetSelector, swap: "outerHTML" });
      return true;
    }
    const response = await fetch(url, { headers: { "HX-Request": "true" } });
    if (!response.ok) {
      return false;
    }
    const html = await response.text();
    target.outerHTML = html;
    return true;
  }

  function refreshPanels() {
    return Promise.all([
      refreshNotificationsPanel(),
      refreshTimelinePanel(),
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
