(() => {
  const state = {
    activeTab: "home-page",
    simulatedTime: new Date("2026-07-26T09:30:00+09:00"),
    simulationRunning: true,
    toastTimer: null,
    generatedMessageCount: 0,
  };

  const toast = document.querySelector("#prototype-toast");
  const chatLog = document.querySelector("#chat-log");
  const chatForm = document.querySelector("#chat-form");
  const chatInput = document.querySelector("#chat-message-input");
  const timeDisplay = document.querySelector(".top-time-value");

  function showToast(message, kind = "success") {
    window.clearTimeout(state.toastTimer);
    toast.textContent = message;
    toast.dataset.kind = kind;
    toast.hidden = false;
    state.toastTimer = window.setTimeout(() => {
      toast.hidden = true;
    }, 2800);
  }

  function setActiveTab(targetId, options = {}) {
    const { focusComposer = false } = options;
    state.activeTab = targetId;

    document.querySelectorAll("[data-tab-target]").forEach((button) => {
      const isActive = button.dataset.tabTarget === targetId;
      button.classList.toggle("is-active", isActive);
      button.setAttribute("aria-selected", String(isActive));
    });

    document.querySelectorAll(".page-tab-panel").forEach((panel) => {
      const isActive = panel.id === targetId;
      panel.classList.toggle("is-active", isActive);
      panel.hidden = !isActive;
    });

    document.body.dataset.activeTab = targetId === "chat-page" ? "chat" : "home";

    if (targetId === "chat-page") {
      window.requestAnimationFrame(() => {
        chatLog.scrollTop = chatLog.scrollHeight;
        if (focusComposer) {
          chatInput.focus();
        }
      });
    }
  }

  function formatTime(date) {
    const year = date.getFullYear();
    const month = String(date.getMonth() + 1).padStart(2, "0");
    const day = String(date.getDate()).padStart(2, "0");
    const hour = String(date.getHours()).padStart(2, "0");
    const minute = String(date.getMinutes()).padStart(2, "0");
    return `${year}-${month}-${day} ${hour}:${minute}`;
  }

  function updateSimulationTime(minutes, announce = true) {
    state.simulatedTime = new Date(state.simulatedTime.getTime() + minutes * 60 * 1000);
    const formatted = formatTime(state.simulatedTime);
    timeDisplay.textContent = formatted;
    timeDisplay.dateTime = state.simulatedTime.toISOString();
    const clock = document.querySelector(".simulation-clock strong");
    clock.textContent = formatted.slice(-5);
    if (announce) {
      showToast(`시뮬레이션 시간을 ${minutes}분 진행했습니다.`);
    }
  }

  function getMessageArticle(target) {
    return target.closest(".chat-message.assistant");
  }

  function closeFeedbackForm(article) {
    const form = article.querySelector(".inline-feedback-form");
    if (form) {
      form.hidden = true;
    }
  }

  function openFeedbackForm(article) {
    const form = article.querySelector(".inline-feedback-form");
    if (!form) {
      return;
    }
    form.hidden = false;
    form.querySelector("textarea").focus();
  }

  function handleReaction(button) {
    const article = getMessageArticle(button);
    if (!article) {
      return;
    }

    const rating = button.dataset.rating;
    const previous = article.querySelector(".reaction-button.is-selected");

    if (previous === button) {
      showToast("이미 같은 평가가 전송되었습니다.", "neutral");
      return;
    }

    article.querySelectorAll(".reaction-button").forEach((candidate) => {
      const selected = candidate === button;
      candidate.classList.toggle("is-selected", selected);
      candidate.setAttribute("aria-pressed", String(selected));
    });

    const status = article.querySelector(".feedback-status");
    status.textContent = "평가 전송됨";

    if (rating === "dislike") {
      openFeedbackForm(article);
      showToast("싫어요가 반영되었습니다. 개선 의견도 남길 수 있어요.");
    } else {
      closeFeedbackForm(article);
      showToast(previous ? "평가를 좋아요로 변경했습니다." : "좋아요가 전송되었습니다.");
    }
  }

  function createFeedbackControls() {
    const wrapper = document.createElement("div");
    wrapper.innerHTML = `
      <div class="message-feedback" aria-label="AI 답변 평가">
        <button class="reaction-button" type="button" data-rating="like" aria-pressed="false">
          <span aria-hidden="true">👍</span><span>좋아요</span>
        </button>
        <button class="reaction-button" type="button" data-rating="dislike" aria-pressed="false">
          <span aria-hidden="true">👎</span><span>싫어요</span>
        </button>
        <button class="feedback-link" type="button" data-feedback-toggle>의견 남기기</button>
        <span class="feedback-status" aria-live="polite"></span>
      </div>
      <form class="inline-feedback-form" hidden>
        <label>
          <span>이 답변에 대한 의견을 알려주세요 <small>선택 입력</small></span>
          <textarea maxlength="4000" rows="3" placeholder="답변에서 좋았던 점이나 개선이 필요한 내용을 적어주세요."></textarea>
        </label>
        <div class="feedback-form-footer">
          <span class="character-count">0 / 4,000</span>
          <div>
            <button class="compact-button ghost" type="button" data-feedback-cancel>취소</button>
            <button class="compact-button" type="submit">의견 보내기</button>
          </div>
        </div>
      </form>`;
    return [...wrapper.children];
  }

  function appendUserMessage(text) {
    const article = document.createElement("article");
    article.className = "chat-message user";
    article.innerHTML = `
      <div class="message-column">
        <div class="message-meta user-meta">
          <span>방금</span>
          <strong>나</strong>
        </div>
        <div class="message-bubble"><p></p></div>
      </div>`;
    article.querySelector("p").textContent = text;
    chatLog.append(article);
  }

  function appendPendingMessage() {
    const article = document.createElement("article");
    article.className = "chat-message assistant chat-pending";
    article.innerHTML = `
      <div class="avatar ai-avatar" aria-hidden="true">AI</div>
      <div class="message-column">
        <div class="message-meta">
          <strong>닥터앤서 AI</strong>
          <span>응답 생성 중</span>
        </div>
        <div class="message-bubble">
          <span class="typing-dots" aria-hidden="true"><span></span><span></span><span></span></span>
          <span>필요한 건강 정보를 확인하고 있어요.</span>
        </div>
      </div>`;
    chatLog.append(article);
    return article;
  }

  function resolvePendingMessage(article, userText) {
    state.generatedMessageCount += 1;
    article.classList.remove("chat-pending");
    article.dataset.messageId = `assistant_demo_${state.generatedMessageCount}`;
    article.dataset.messageType = "text";
    article.querySelector(".message-meta").innerHTML = `
      <strong>닥터앤서 AI</strong>
      <span>방금</span>`;
    article.querySelector(".message-bubble").innerHTML = `
      <div class="contract-message">
        <h3 class="contract-message-title">질문 확인</h3>
        <div class="contract-message-text">
          <p>말씀하신 내용을 확인했어요.</p>
          <p></p>
        </div>
      </div>`;
    article.querySelector(".contract-message-text p:last-child").textContent =
      `"${userText}"에 대해 등록된 복약 일정과 최근 기록을 기준으로 확인하고 있어요.`;

    const column = article.querySelector(".message-column");
    createFeedbackControls().forEach((node) => column.append(node));
    chatLog.scrollTop = chatLog.scrollHeight;
  }

  document.addEventListener("click", (event) => {
    const tabButton = event.target.closest("[data-tab-target]");
    if (tabButton) {
      setActiveTab(tabButton.dataset.tabTarget);
      return;
    }

    const demoButton = event.target.closest("[data-demo-message]");
    if (demoButton) {
      showToast(demoButton.dataset.demoMessage, "neutral");
      return;
    }

    const advanceButton = event.target.closest("[data-advance-minutes]");
    if (advanceButton) {
      updateSimulationTime(Number(advanceButton.dataset.advanceMinutes));
      return;
    }

    const simulationButton = event.target.closest("[data-simulation-action]");
    if (simulationButton) {
      document.querySelectorAll("[data-simulation-action]").forEach((button) => {
        button.classList.toggle("is-active", button === simulationButton);
        button.classList.toggle("ghost", button !== simulationButton);
      });
      const running = simulationButton.dataset.simulationAction === "start";
      state.simulationRunning = running;
      document.querySelector(".top-time-controls .status-pill").textContent = running ? "실행 중" : "일시 정지";
      const panelBadge = document.querySelector(".simulation-panel .badge");
      panelBadge.textContent = running ? "실행 중" : "일시 정지";
      panelBadge.classList.toggle("success", running);
      panelBadge.classList.toggle("neutral", !running);
      showToast(running ? "시뮬레이션을 시작했습니다." : "시뮬레이션을 일시 정지했습니다.");
      return;
    }

    const openChatButton = event.target.closest("[data-open-chat-message]");
    if (openChatButton) {
      chatInput.value = openChatButton.dataset.openChatMessage;
      setActiveTab("chat-page", { focusComposer: true });
      resizeComposer();
      return;
    }

    const quickPrompt = event.target.closest("[data-quick-prompt]");
    if (quickPrompt) {
      chatInput.value = quickPrompt.dataset.quickPrompt;
      chatInput.focus();
      resizeComposer();
      return;
    }

    const reactionButton = event.target.closest(".reaction-button");
    if (reactionButton) {
      handleReaction(reactionButton);
      return;
    }

    const feedbackToggle = event.target.closest("[data-feedback-toggle]");
    if (feedbackToggle) {
      const article = getMessageArticle(feedbackToggle);
      const form = article.querySelector(".inline-feedback-form");
      if (form.hidden) {
        openFeedbackForm(article);
      } else {
        closeFeedbackForm(article);
      }
      return;
    }

    const feedbackCancel = event.target.closest("[data-feedback-cancel]");
    if (feedbackCancel) {
      closeFeedbackForm(getMessageArticle(feedbackCancel));
      return;
    }

    const selectionButton = event.target.closest("[data-selection-value]");
    if (selectionButton) {
      const card = selectionButton.closest(".result-selection");
      card.querySelectorAll("[data-selection-value]").forEach((button) => {
        button.classList.toggle("is-selected", button === selectionButton);
        button.disabled = true;
      });
      card.querySelector(".selection-result").textContent =
        `${selectionButton.dataset.selectionValue}으로 응답했습니다.`;
      showToast("선택 응답이 반영되었습니다.");
      return;
    }
  });

  document.addEventListener("submit", (event) => {
    if (event.target.matches(".result-input-form")) {
      event.preventDefault();
      const formData = new FormData(event.target);
      const status = event.target.querySelector(".input-result-status");
      status.textContent = [...formData.entries()]
        .map(([label, value]) => `${label}: ${value}`)
        .join(" · ");
      event.target.querySelectorAll("input, select, button").forEach((control) => {
        control.disabled = true;
      });
      showToast("입력 응답이 반영되었습니다.");
      return;
    }

    if (event.target.matches(".inline-feedback-form")) {
      event.preventDefault();
      const article = getMessageArticle(event.target);
      const text = event.target.querySelector("textarea").value.trim();
      const status = article.querySelector(".feedback-status");
      status.textContent = text ? "의견 전송됨" : "평가 전송됨";
      closeFeedbackForm(article);
      showToast(text ? "소중한 의견을 전송했습니다." : "평가를 전송했습니다.");
      return;
    }
  });

  document.addEventListener("input", (event) => {
    if (event.target.matches(".inline-feedback-form textarea")) {
      const form = event.target.closest(".inline-feedback-form");
      form.querySelector(".character-count").textContent =
        `${event.target.value.length.toLocaleString("ko-KR")} / 4,000`;
    }

    if (event.target === chatInput) {
      resizeComposer();
    }
  });

  chatForm.addEventListener("submit", (event) => {
    event.preventDefault();
    const text = chatInput.value.trim();
    if (!text) {
      return;
    }

    appendUserMessage(text);
    const pending = appendPendingMessage();
    chatInput.value = "";
    resizeComposer();
    chatLog.scrollTop = chatLog.scrollHeight;

    window.setTimeout(() => {
      resolvePendingMessage(pending, text);
    }, 850);
  });

  chatInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      chatForm.requestSubmit();
    }
  });

  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") {
      return;
    }
    const openForm = document.querySelector(".inline-feedback-form:not([hidden])");
    if (openForm) {
      openForm.hidden = true;
      openForm.closest(".chat-message").querySelector("[data-feedback-toggle]").focus();
    }
  });

  document.querySelectorAll(".switch input").forEach((input) => {
    input.addEventListener("change", () => {
      showToast(input.checked ? "알림 정책을 켰습니다." : "알림 정책을 껐습니다.");
    });
  });

  function resizeComposer() {
    chatInput.style.height = "auto";
    chatInput.style.height = `${Math.min(chatInput.scrollHeight, 150)}px`;
  }

  window.setInterval(() => {
    if (state.simulationRunning) {
      updateSimulationTime(60, false);
    }
  }, 1000);

  setActiveTab("home-page");
})();
