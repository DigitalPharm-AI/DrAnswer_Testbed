const ACTION_REQUEST_TIMEOUT_MS = 10000;

export function createButton(label, className, onClick) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = className;
  button.textContent = label;
  button.addEventListener("click", async (event) => {
    if (button.dataset.requestPending === "true") {
      return;
    }
    const wasDisabled = button.disabled;
    button.dataset.requestPending = "true";
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    try {
      await onClick(event);
    } catch (error) {
      if (!error || error.feedbackReported !== true) {
        showRequestError("요청을 처리하지 못했습니다. 다시 시도해주세요.");
      }
    } finally {
      delete button.dataset.requestPending;
      button.removeAttribute("aria-busy");
      if (button.isConnected) {
        button.disabled = wasDisabled;
      }
    }
  });
  return button;
}

export async function postAction(url) {
  return postRequest(url);
}

export async function postFormAction(url, formData) {
  return postRequest(url, formData);
}

async function postRequest(url, body = undefined) {
  const controller = new AbortController();
  const timeoutId = window.setTimeout(
    () => controller.abort(),
    ACTION_REQUEST_TIMEOUT_MS,
  );
  try {
    const response = await fetch(url, {
      method: "POST",
      body,
      signal: controller.signal,
      headers: {
        "HX-Request": "true",
      },
    });
    if (!response.ok) {
      const error = new Error(actionErrorMessage(response.status));
      error.feedbackReported = true;
      showRequestError(error.message);
      throw error;
    }
    if (window.appFeedback && typeof window.appFeedback.clear === "function") {
      window.appFeedback.clear();
    }
    return response;
  } catch (error) {
    if (error && error.feedbackReported === true) {
      throw error;
    }
    const message =
      error && error.name === "AbortError"
        ? "요청 시간이 초과되었습니다. 다시 시도해주세요."
        : "서버에 연결하지 못했습니다. 연결 상태를 확인해주세요.";
    const reportedError = new Error(message);
    reportedError.feedbackReported = true;
    showRequestError(message);
    throw reportedError;
  } finally {
    window.clearTimeout(timeoutId);
  }
}

function actionErrorMessage(status) {
  if (status === 404) {
    return "대상이 변경되었거나 존재하지 않습니다. 화면을 새로 확인해주세요.";
  }
  if (status === 409) {
    return "화면 상태가 변경되었습니다. 최신 상태를 확인한 뒤 다시 시도해주세요.";
  }
  if (status === 400 || status === 422) {
    return "입력값을 확인해주세요.";
  }
  if (status >= 500) {
    return "서버에서 요청을 처리하지 못했습니다. 잠시 후 다시 시도해주세요.";
  }
  return "요청을 처리하지 못했습니다. 다시 시도해주세요.";
}

function showRequestError(message) {
  if (window.appFeedback && typeof window.appFeedback.error === "function") {
    window.appFeedback.error(message);
  }
}

export async function fetchNotification(notificationId) {
  const response = await fetch(`/api/notifications/${notificationId}`, {
    headers: { Accept: "application/json" },
  });
  if (!response.ok) {
    return null;
  }
  const payload = await response.json();
  return payload.notification || null;
}

export function showNativeNotification(notification) {
  if (!("Notification" in window) || Notification.permission !== "granted") {
    return;
  }
  new Notification(notification.title, {
    body: notification.body,
    tag: `medication-notification-${notification.id}`,
  });
}

export function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}
