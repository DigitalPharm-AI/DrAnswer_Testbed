export function createButton(label, className, onClick) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = className;
  button.textContent = label;
  button.addEventListener("click", onClick);
  return button;
}

export async function postAction(url) {
  await fetch(url, {
    method: "POST",
    headers: {
      "HX-Request": "true",
    },
  });
}

export async function postFormAction(url, formData) {
  await fetch(url, {
    method: "POST",
    body: formData,
    headers: {
      "HX-Request": "true",
    },
  });
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
