(() => {
  const LOAD_MARK = "data-hx-lite-load-bound";
  const EVERY_MARK = "data-hx-lite-every-bound";
  const DEFAULT_REQUEST_TIMEOUT_MS = 15000;

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
    const target = targetFor(source, options.target);
    const beforePayload = detail(source, target, null);
    if (!dispatch("htmx:beforeRequest", source, beforePayload)) {
      return null;
    }

    source.classList.add("htmx-request");
    document.body.classList.add("htmx-request");
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
      const afterPayload = detail(source, target, xhr);
      dispatch("htmx:afterRequest", source, afterPayload);

      if ((response.headers.get("HX-Refresh") || "").toLowerCase() === "true") {
        window.location.reload();
        return response;
      }

      if (response.status === 204) {
        return response;
      }

      const html = await response.text();
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
      throw error;
    } finally {
      window.clearTimeout(timeoutId);
      source.classList.remove("htmx-request");
      document.body.classList.remove("htmx-request");
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

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => bindTriggers(document));
  } else {
    bindTriggers(document);
  }
})();
