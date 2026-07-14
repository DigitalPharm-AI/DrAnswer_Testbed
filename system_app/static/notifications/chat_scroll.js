function getChatLogElement() {
  return document.querySelector("[data-chat-scroll-region]");
}

function isNearChatBottom(element) {
  return element.scrollHeight - element.scrollTop - element.clientHeight < 48;
}

function captureTopVisibleMessage(chatLog) {
  const chatLogTop = chatLog.getBoundingClientRect().top;
  const messages = chatLog.querySelectorAll("[data-chat-message-id]");
  for (const message of messages) {
    const bounds = message.getBoundingClientRect();
    if (bounds.bottom > chatLogTop) {
      return {
        messageId: message.dataset.chatMessageId,
        offsetTop: bounds.top - chatLogTop,
      };
    }
  }
  return null;
}

function restoreTopVisibleMessage(chatLog, anchor) {
  if (!anchor || !anchor.messageId) {
    return false;
  }
  const message = Array.from(chatLog.querySelectorAll("[data-chat-message-id]")).find(
    (candidate) => candidate.dataset.chatMessageId === anchor.messageId,
  );
  if (!message) {
    return false;
  }
  const chatLogTop = chatLog.getBoundingClientRect().top;
  const currentOffsetTop = message.getBoundingClientRect().top - chatLogTop;
  chatLog.scrollTop += currentOffsetTop - anchor.offsetTop;
  return true;
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
    wasAtBottom: isNearChatBottom(chatLog),
    hadOverflow,
    anchor: captureTopVisibleMessage(chatLog),
  };
}

function restoreChatLogScroll(state) {
  const chatLog = getChatLogElement();
  if (!chatLog) {
    return;
  }
  if (state.chatScrollForceBottom) {
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

  if (restoreTopVisibleMessage(chatLog, state.chatScroll.anchor)) {
    return;
  }

  const maxScrollTop = Math.max(0, chatLog.scrollHeight - chatLog.clientHeight);
  chatLog.scrollTop = Math.min(Math.max(0, state.chatScroll.scrollTop), maxScrollTop);
}

function scrollChatLogToBottom() {
  const chatLog = getChatLogElement();
  if (!chatLog) {
    return;
  }
  chatLog.scrollTop = chatLog.scrollHeight;
}

function restoreChatLogScrollAfterLayout(state) {
  const shouldStickToBottom = Boolean(
    state.chatScrollForceBottom ||
      !state.chatScroll ||
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
  if (shouldStickToBottom) {
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
