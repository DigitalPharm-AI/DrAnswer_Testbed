function getChatLogElement() {
  return document.querySelector("[data-chat-scroll-region]");
}

function getChatLogRegion() {
  return document.getElementById("chat-log-region");
}

function getChatLogSignature() {
  const region = getChatLogRegion();
  if (!region) {
    return "";
  }
  return `${region.dataset.chatMessageCount || "0"}:${region.dataset.chatLastMessageId || ""}`;
}

function isNearChatBottom(element) {
  return element.scrollHeight - element.scrollTop - element.clientHeight < 48;
}

function captureChatLogScroll(state) {
  const chatLog = getChatLogElement();
  if (!chatLog) {
    state.chatScroll = null;
    return;
  }
  const hadOverflow = chatLog.scrollHeight > chatLog.clientHeight;
  state.chatScroll = {
    scrollTop: chatLog.scrollTop,
    bottomOffset: chatLog.scrollHeight - chatLog.scrollTop,
    wasAtBottom: isNearChatBottom(chatLog),
    hadOverflow,
    signature: getChatLogSignature(),
  };
}

function restoreChatLogScroll(state) {
  const chatLog = getChatLogElement();
  if (!chatLog) {
    return;
  }
  const currentSignature = getChatLogSignature();
  const chatLogChanged = Boolean(state.chatScroll && state.chatScroll.signature && currentSignature !== state.chatScroll.signature);
  if (state.chatScrollForceBottom || chatLogChanged) {
    state.chatScrollForceBottom = false;
    chatLog.scrollTop = chatLog.scrollHeight;
    return;
  }
  if (!state.chatScroll) {
    chatLog.scrollTop = chatLog.scrollHeight;
    return;
  }

  if (state.chatScroll.wasAtBottom || !state.chatScroll.hadOverflow) {
    chatLog.scrollTop = chatLog.scrollHeight;
    return;
  }

  const restoredTop = chatLog.scrollHeight - state.chatScroll.bottomOffset;
  chatLog.scrollTop = Math.max(0, restoredTop || state.chatScroll.scrollTop);
}

function scrollChatLogToBottom() {
  const chatLog = getChatLogElement();
  if (!chatLog) {
    return;
  }
  chatLog.scrollTop = chatLog.scrollHeight;
}

function restoreChatLogScrollAfterLayout(state) {
  const before = getChatLogElement();
  const currentSignature = getChatLogSignature();
  const shouldStickToBottom = Boolean(
    state.chatScrollForceBottom ||
      !state.chatScroll ||
      (state.chatScroll.signature && currentSignature !== state.chatScroll.signature) ||
      state.chatScroll.wasAtBottom ||
      !state.chatScroll.hadOverflow,
  );
  restoreChatLogScroll(state);
  window.requestAnimationFrame(() => {
    if (shouldStickToBottom || state.chatScrollForceBottom) {
      scrollChatLogToBottom();
      state.chatScrollForceBottom = false;
      return;
    }
    restoreChatLogScroll(state);
  });
  if (shouldStickToBottom || (before && before.isConnected === false)) {
    window.setTimeout(scrollChatLogToBottom, 80);
  }
}

function isChatComposerSubmitEvent(event) {
  return Boolean(event.target && event.target.classList && event.target.classList.contains("chat-composer"));
}

function getChatComposerFromRequestEvent(event) {
  const detail = event.detail || {};
  const element = detail.elt;
  if (element && element.classList && element.classList.contains("chat-composer")) {
    return element;
  }
  return null;
}

function wasHtmxRequestSuccessful(event) {
  const detail = event.detail || {};
  if (detail.failed || detail.successful === false) {
    return false;
  }
  const xhr = detail.xhr;
  return !xhr || (xhr.status >= 200 && xhr.status < 400);
}

function isChatLogRefreshEvent(event) {
  const detail = event.detail || {};
  return Boolean(
    (detail.elt && detail.elt.id === "chat-log-region") ||
      (detail.elt && detail.elt.id === "chat-panel") ||
      (detail.target && detail.target.id === "chat-log-region") ||
      (detail.target && detail.target.id === "chat-panel") ||
      (event.target && event.target.id === "chat-log-region") ||
      (event.target && event.target.id === "chat-panel"),
  );
}

export function installChatLogScrollPreserver(state) {
  document.body.addEventListener("submit", (event) => {
    if (isChatComposerSubmitEvent(event)) {
      state.chatScrollForceBottom = true;
    }
  });

  document.body.addEventListener("htmx:afterRequest", (event) => {
    const form = getChatComposerFromRequestEvent(event);
    if (form && wasHtmxRequestSuccessful(event)) {
      form.reset();
    }
  });

  document.body.addEventListener("htmx:beforeRequest", (event) => {
    if (isChatLogRefreshEvent(event)) {
      captureChatLogScroll(state);
    }
  });

  document.body.addEventListener("htmx:beforeSwap", (event) => {
    if (isChatLogRefreshEvent(event)) {
      captureChatLogScroll(state);
    }
  });

  document.body.addEventListener("htmx:afterSwap", (event) => {
    if (isChatLogRefreshEvent(event)) {
      restoreChatLogScrollAfterLayout(state);
    }
  });

  window.requestAnimationFrame(() => restoreChatLogScrollAfterLayout(state));
}
