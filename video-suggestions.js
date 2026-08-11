"use strict";

(() => {
  const API_URL = "/api/video-suggestions";
  const IDENTITY_URL = "/api/video-suggestions/identity";
  const SUBMISSION_LOCK_NAME = "moralsqd-video-suggestion-submit-v1";
  const STORAGE_LEASE_KEY = "moralsqd:video-suggestion-submit-lease:v1";
  const LOCK_WAIT_TIMEOUT_MS = 20_000;
  const STORAGE_LEASE_DURATION_MS = 60_000;
  const STORAGE_HEARTBEAT_MS = 5_000;
  const STORAGE_CONFIRM_DELAY_MS = 120;
  const YOUTUBE_HOSTS = new Set([
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
    "www.youtu.be",
  ]);

  let initialized = false;
  let rootObserver = null;

  function init() {
    if (initialized) {
      return;
    }

    initialized = true;
    rootObserver?.disconnect();
    document.addEventListener("click", handleClick);
    document.addEventListener("submit", handleSubmit);
    document.addEventListener("input", handleInput);
    document.addEventListener("cancel", handleDialogCancel, true);
  }

  function waitForRenderedPage() {
    if (document.querySelector("#dc-root")) {
      init();
      return;
    }

    rootObserver = new MutationObserver(() => {
      if (document.querySelector("#dc-root")) {
        init();
      }
    });
    rootObserver.observe(document.documentElement, { childList: true, subtree: true });
  }

  function handleClick(event) {
    const target = event.target instanceof Element ? event.target : null;
    if (!target) {
      return;
    }

    if (target.closest("[data-video-suggestion-open]")) {
      openDialog();
      return;
    }

    if (target.closest("[data-video-suggestion-close]")) {
      closeDialog();
      return;
    }

    if (target.matches("[data-video-suggestion-dialog]")) {
      closeDialog();
    }
  }

  function handleInput(event) {
    const changedInput = event.target instanceof HTMLInputElement ? event.target : null;
    if (!changedInput) {
      return;
    }

    const consent = changedInput.closest("[data-video-suggestion-consent]");
    if (consent instanceof HTMLInputElement) {
      const form = consent.closest("[data-video-suggestion-form]");
      const submitButton = form?.querySelector("[data-video-suggestion-submit]");
      if (form instanceof HTMLFormElement && submitButton instanceof HTMLButtonElement) {
        submitButton.disabled = !consent.checked || form.getAttribute("aria-busy") === "true";
        setStatus(form, "", "");
      }
      return;
    }

    const input = changedInput.closest("[data-video-suggestion-form] input[name='url']");

    if (!input) {
      return;
    }

    input.setCustomValidity("");
    input.removeAttribute("aria-invalid");

    const form = input.closest("[data-video-suggestion-form]");
    if (form) {
      setStatus(form, "", "");
    }
  }

  function openDialog() {
    const dialog = document.querySelector("[data-video-suggestion-dialog]");
    if (!(dialog instanceof HTMLDialogElement) || dialog.open) {
      return;
    }

    const form = dialog.querySelector("[data-video-suggestion-form]");
    if (form instanceof HTMLFormElement) {
      setStatus(form, "", "");
      const input = form.elements.namedItem("url");
      if (input instanceof HTMLInputElement) {
        input.setCustomValidity("");
        input.removeAttribute("aria-invalid");
      }
    }

    dialog.showModal();
    window.requestAnimationFrame(() => {
      const input = dialog.querySelector("input[name='url']");
      if (input instanceof HTMLInputElement) {
        input.focus();
      }
    });
  }

  function closeDialog() {
    const dialog = document.querySelector("[data-video-suggestion-dialog]");
    if (
      dialog instanceof HTMLDialogElement
      && dialog.open
      && !isDialogBusy(dialog)
    ) {
      dialog.close();
    }
  }

  function handleDialogCancel(event) {
    const dialog = event.target instanceof HTMLDialogElement ? event.target : null;
    if (dialog?.matches("[data-video-suggestion-dialog]") && isDialogBusy(dialog)) {
      event.preventDefault();
    }
  }

  function isDialogBusy(dialog) {
    const form = dialog.querySelector("[data-video-suggestion-form]");
    return form?.getAttribute("aria-busy") === "true";
  }

  async function handleSubmit(event) {
    const form = event.target instanceof HTMLFormElement
      ? event.target.closest("[data-video-suggestion-form]")
      : null;

    if (!(form instanceof HTMLFormElement)) {
      return;
    }

    event.preventDefault();

    if (form.getAttribute("aria-busy") === "true") {
      return;
    }

    const input = form.elements.namedItem("url");
    const consent = form.elements.namedItem("policyAccepted");
    const submitButton = form.querySelector("[data-video-suggestion-submit]");
    if (
      !(input instanceof HTMLInputElement)
      || !(consent instanceof HTMLInputElement)
      || !(submitButton instanceof HTMLButtonElement)
    ) {
      return;
    }

    const submittedUrl = input.value.trim();
    input.setCustomValidity("");
    input.removeAttribute("aria-invalid");

    if (!submittedUrl) {
      showInputError(form, input, "Вставь ссылку на видео YouTube.");
      return;
    }

    if (!isAllowedYouTubeUrl(submittedUrl)) {
      showInputError(form, input, "Нужна HTTPS-ссылка на youtube.com или youtu.be.");
      return;
    }

    if (!consent.checked) {
      setStatus(form, "Подтверди согласие с правилами перед отправкой.", "error");
      consent.focus();
      return;
    }

    setBusy(form, submitButton, input, consent, true);
    setStatus(form, "Проверяем отправки в других вкладках…", "loading");

    try {
      const { identity, submission } = await withSubmissionLock(async (assertLockOwner) => {
        assertLockOwner();
        setStatus(form, "Готовим анонимную отправку…", "loading");
        const identityResult = await prepareVisitorIdentity();
        assertLockOwner();

        if (!identityResult.response.ok || identityResult.payload?.ready !== true) {
          return { identity: identityResult, submission: null };
        }

        setStatus(form, "Проверяем видео и отправляем просьбу…", "loading");
        const submissionResult = await sendSuggestion(submittedUrl);
        assertLockOwner();
        return { identity: identityResult, submission: submissionResult };
      });

      if (!identity.response.ok || identity.payload?.ready !== true) {
        setStatus(form, friendlyError(identity.payload, identity.response.status), "error");
        return;
      }

      if (!submission) {
        setStatus(form, "Не получилось отправить ссылку. Попробуй ещё раз чуть позже.", "error");
        return;
      }

      const { response, payload } = submission;

      if (payload?.duplicate === true || payload?.error === "duplicate") {
        setStatus(form, "Ты уже предлагал этот видос. Можно отправить другой.", "error");
        input.setAttribute("aria-invalid", "true");
        input.focus();
        input.select();
        return;
      }

      if (!response.ok || payload?.ok !== true) {
        setStatus(form, friendlyError(payload, response.status), "error");
        input.setAttribute("aria-invalid", "true");
        input.focus();
        return;
      }

      form.reset();
      setStatus(form, "Готово, передали ДК. Можно сразу предложить другой видос.", "success");
      input.focus();
    } catch (error) {
      const message = error instanceof SubmissionLockError
        ? error.message
        : "Не получилось связаться с сервером. Проверь интернет и попробуй ещё раз.";
      setStatus(form, message, "error");
    } finally {
      setBusy(form, submitButton, input, consent, false);
    }
  }

  class SubmissionLockError extends Error {
    constructor(message) {
      super(message);
      this.name = "SubmissionLockError";
    }
  }

  async function withSubmissionLock(task) {
    if (navigator.locks && typeof navigator.locks.request === "function") {
      return withNavigatorLock(task);
    }
    return withStorageLease(task);
  }

  async function withNavigatorLock(task) {
    const controller = new AbortController();
    let acquired = false;
    const timeout = window.setTimeout(() => {
      if (!acquired) {
        controller.abort();
      }
    }, LOCK_WAIT_TIMEOUT_MS);

    try {
      return await navigator.locks.request(
        SUBMISSION_LOCK_NAME,
        { mode: "exclusive", signal: controller.signal },
        async () => {
          acquired = true;
          window.clearTimeout(timeout);
          return task(() => {});
        },
      );
    } catch (error) {
      if (!acquired && error?.name === "AbortError") {
        throw new SubmissionLockError(
          "Другая вкладка слишком долго отправляет видео. Дождись её завершения и попробуй ещё раз.",
        );
      }
      throw error;
    } finally {
      window.clearTimeout(timeout);
    }
  }

  async function withStorageLease(task) {
    const owner = createLeaseOwner();
    const deadline = Date.now() + LOCK_WAIT_TIMEOUT_MS;
    let heartbeat = null;
    let leaseLost = false;
    let ownedUntil = 0;
    let ownedSince = 0;

    try {
      while (Date.now() < deadline) {
        if (await tryAcquireStorageLease(owner)) {
          break;
        }
        await waitForStorageLeaseChange(deadline);
      }

      if (!isOwnActiveLease(owner)) {
        throw new SubmissionLockError(
          "Другая вкладка слишком долго отправляет видео. Дождись её завершения и попробуй ещё раз.",
        );
      }
      const acquiredLease = readStorageLease();
      ownedUntil = acquiredLease?.expiresAt ?? 0;
      ownedSince = acquiredLease?.acquiredAt ?? 0;

      const handleStorageChange = (event) => {
        if (event.key !== STORAGE_LEASE_KEY) {
          return;
        }

        try {
          const current = readStorageLease();
          if (current?.owner === owner && current.expiresAt > Date.now()) {
            return;
          }

          if (
            ownedUntil > Date.now()
            && storageLeaseHasPriority(ownedSince, owner, current)
          ) {
            ownedUntil = Date.now() + STORAGE_LEASE_DURATION_MS;
            writeStorageLease(owner, ownedUntil, ownedSince);
          }
          if (!isOwnActiveLease(owner)) {
            leaseLost = true;
          }
        } catch {
          leaseLost = true;
        }
      };
      window.addEventListener("storage", handleStorageChange);

      heartbeat = window.setInterval(() => {
        try {
          const renewedUntil = renewStorageLease(owner);
          if (renewedUntil === null) {
            leaseLost = true;
          } else {
            ownedUntil = renewedUntil;
          }
        } catch {
          leaseLost = true;
        }
      }, STORAGE_HEARTBEAT_MS);

      const assertLockOwner = () => {
        if (leaseLost || !isOwnActiveLease(owner)) {
          throw new SubmissionLockError(
            "Безопасная отправка между вкладками прервалась. Попробуй ещё раз.",
          );
        }
      };

      try {
        assertLockOwner();
        const result = await task(assertLockOwner);
        assertLockOwner();
        return result;
      } finally {
        window.removeEventListener("storage", handleStorageChange);
      }
    } catch (error) {
      if (error instanceof SubmissionLockError) {
        throw error;
      }
      throw new SubmissionLockError(
        "Не удалось безопасно согласовать отправку между вкладками. Обнови страницу и попробуй ещё раз.",
      );
    } finally {
      if (heartbeat !== null) {
        window.clearInterval(heartbeat);
      }
      releaseStorageLease(owner);
    }
  }

  async function tryAcquireStorageLease(owner) {
    const current = readStorageLease();
    if (current && current.owner !== owner && current.expiresAt > Date.now()) {
      return false;
    }

    const acquiredAt = Date.now();
    writeStorageLease(owner, acquiredAt + STORAGE_LEASE_DURATION_MS, acquiredAt);
    await delay(STORAGE_CONFIRM_DELAY_MS + Math.floor(Math.random() * 80));

    if (!isOwnActiveLease(owner)) {
      return false;
    }

    await delay(STORAGE_CONFIRM_DELAY_MS);
    return isOwnActiveLease(owner);
  }

  function renewStorageLease(owner) {
    const current = readStorageLease();
    if (current?.owner !== owner || current.expiresAt <= Date.now()) {
      return null;
    }
    const expiresAt = Date.now() + STORAGE_LEASE_DURATION_MS;
    writeStorageLease(owner, expiresAt, current.acquiredAt);
    return isOwnActiveLease(owner) ? expiresAt : null;
  }

  function storageLeaseHasPriority(ownedSince, owner, current) {
    if (!current || current.expiresAt <= Date.now()) {
      return true;
    }
    if (ownedSince !== current.acquiredAt) {
      return ownedSince < current.acquiredAt;
    }
    return owner < current.owner;
  }

  function releaseStorageLease(owner) {
    try {
      const current = readStorageLease();
      if (current?.owner === owner) {
        window.localStorage.removeItem(STORAGE_LEASE_KEY);
      }
    } catch {
      // The lease expires by itself if storage becomes unavailable during release.
    }
  }

  function isOwnActiveLease(owner) {
    const current = readStorageLease();
    return current?.owner === owner && current.expiresAt > Date.now();
  }

  function readStorageLease() {
    const raw = window.localStorage.getItem(STORAGE_LEASE_KEY);
    if (!raw) {
      return null;
    }

    try {
      const parsed = JSON.parse(raw);
      if (
        typeof parsed?.owner === "string"
        && parsed.owner.length > 0
        && Number.isFinite(parsed.expiresAt)
      ) {
        return {
          owner: parsed.owner,
          expiresAt: parsed.expiresAt,
          acquiredAt: Number.isFinite(parsed.acquiredAt) ? parsed.acquiredAt : 0,
        };
      }
      return null;
    } catch {
      return null;
    }
  }

  function writeStorageLease(owner, expiresAt, acquiredAt) {
    window.localStorage.setItem(
      STORAGE_LEASE_KEY,
      JSON.stringify({ owner, expiresAt, acquiredAt }),
    );
  }

  function createLeaseOwner() {
    if (typeof crypto.randomUUID === "function") {
      return crypto.randomUUID();
    }
    const random = new Uint32Array(4);
    crypto.getRandomValues(random);
    return `${Date.now()}-${Array.from(random, (value) => value.toString(16)).join("-")}`;
  }

  function waitForStorageLeaseChange(deadline) {
    const remaining = Math.max(0, deadline - Date.now());
    const timeoutMs = Math.min(remaining, 180 + Math.floor(Math.random() * 180));

    return new Promise((resolve) => {
      let timer = null;
      const finish = () => {
        if (timer !== null) {
          window.clearTimeout(timer);
        }
        window.removeEventListener("storage", handleStorage);
        resolve();
      };
      const handleStorage = (event) => {
        if (event.key === STORAGE_LEASE_KEY) {
          finish();
        }
      };
      timer = window.setTimeout(finish, timeoutMs);
      window.addEventListener("storage", handleStorage);
    });
  }

  function delay(milliseconds) {
    return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
  }

  function isAllowedYouTubeUrl(value) {
    try {
      const parsedUrl = new URL(value);
      return parsedUrl.protocol === "https:"
        && parsedUrl.username === ""
        && parsedUrl.password === ""
        && (parsedUrl.port === "" || parsedUrl.port === "443")
        && YOUTUBE_HOSTS.has(parsedUrl.hostname.toLowerCase());
    } catch {
      return false;
    }
  }

  async function prepareVisitorIdentity() {
    return postJson(IDENTITY_URL, { policyAccepted: true });
  }

  async function sendSuggestion(url) {
    return postJson(API_URL, { url, policyAccepted: true });
  }

  async function postJson(url, body) {
    const response = await fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
      },
      body: JSON.stringify(body),
    });
    const payload = await readJson(response);
    return { response, payload };
  }

  function showInputError(form, input, message) {
    input.setCustomValidity(message);
    input.setAttribute("aria-invalid", "true");
    setStatus(form, message, "error");
    input.reportValidity();
    input.focus();
  }

  function setBusy(form, submitButton, input, consent, isBusy) {
    form.setAttribute("aria-busy", String(isBusy));
    submitButton.disabled = isBusy || !consent.checked;
    submitButton.textContent = isBusy ? "отправляем…" : "отправить";
    input.readOnly = isBusy;
    consent.disabled = isBusy;

    const dialog = form.closest("[data-video-suggestion-dialog]");
    if (dialog instanceof HTMLDialogElement) {
      for (const closeButton of dialog.querySelectorAll("[data-video-suggestion-close]")) {
        if (closeButton instanceof HTMLButtonElement) {
          closeButton.disabled = isBusy;
        }
      }
    }
  }

  function setStatus(form, message, state) {
    const status = form.querySelector("[data-video-suggestion-status]");
    if (!(status instanceof HTMLElement)) {
      return;
    }

    status.textContent = message;
    if (state) {
      status.dataset.state = state;
    } else {
      delete status.dataset.state;
    }
  }

  async function readJson(response) {
    try {
      return await response.json();
    } catch {
      return null;
    }
  }

  function friendlyError(payload, status) {
    const messages = {
      invalid_url: "Не получилось распознать ссылку. Нужна ссылка на конкретное видео YouTube.",
      invalid_youtube_url: "Нужна ссылка на конкретное видео YouTube.",
      video_not_found: "Такое видео не найдено или оно недоступно.",
      video_unavailable: "Видео недоступно для предложения.",
      livestream_not_finished: "Дождись окончания трансляции и предложи её ещё раз.",
      rate_limited: "Слишком много попыток подряд. Подожди немного и попробуй снова.",
      youtube_unavailable: "YouTube сейчас не отвечает. Попробуй чуть позже.",
      metadata_unavailable: "Не удалось проверить видео. Попробуй чуть позже.",
      policy_required: "Подтверди согласие с правилами перед отправкой.",
      visitor_identity_required: "Браузер не сохранил анонимный идентификатор. Разреши cookie для сайта и попробуй ещё раз.",
    };
    const code = typeof payload?.error === "string" ? payload.error : "";

    if (messages[code]) {
      return messages[code];
    }
    if (status === 429) {
      return messages.rate_limited;
    }
    if (typeof payload?.message === "string" && payload.message.trim()) {
      return payload.message.trim();
    }
    return "Не получилось отправить ссылку. Попробуй ещё раз чуть позже.";
  }

  waitForRenderedPage();
})();
