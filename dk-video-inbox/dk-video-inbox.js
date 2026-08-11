"use strict";

(() => {
  const API = {
    session: "/api/video-suggestions/admin/session",
    login: "/api/video-suggestions/admin/login",
    logout: "/api/video-suggestions/admin/logout",
    videos: "/api/video-suggestions/admin/videos",
  };

  const elements = {
    sessionCheck: document.querySelector("[data-session-check]"),
    authView: document.querySelector("[data-auth-view]"),
    dashboardView: document.querySelector("[data-dashboard-view]"),
    loginForm: document.querySelector("[data-login-form]"),
    loginSubmit: document.querySelector("[data-login-submit]"),
    loginStatus: document.querySelector("[data-login-status]"),
    refreshButton: document.querySelector("[data-refresh-videos]"),
    logoutButton: document.querySelector("[data-logout]"),
    dashboardStatus: document.querySelector("[data-dashboard-status]"),
    videoCount: document.querySelector("[data-video-count]"),
    updatedAt: document.querySelector("[data-updated-at]"),
    videoList: document.querySelector("[data-video-list]"),
  };

  const state = {
    csrfToken: null,
    loadingVideos: false,
  };

  elements.loginForm?.addEventListener("submit", handleLogin);
  elements.refreshButton?.addEventListener("click", loadVideos);
  elements.logoutButton?.addEventListener("click", handleLogout);

  checkSession();

  async function checkSession() {
    showOnly("checking");

    try {
      const { response, payload } = await request(API.session);
      if (response.ok && payload?.authenticated === true) {
        state.csrfToken = typeof payload.csrfToken === "string" ? payload.csrfToken : null;
        showOnly("dashboard");
        await loadVideos();
        return;
      }

      showAuth();
    } catch {
      showAuth("Не получилось связаться с сервером. Обнови страницу и попробуй ещё раз.");
    }
  }

  async function handleLogin(event) {
    event.preventDefault();
    if (!(elements.loginForm instanceof HTMLFormElement) || !(elements.loginSubmit instanceof HTMLButtonElement)) {
      return;
    }

    if (!elements.loginForm.reportValidity()) {
      return;
    }

    const usernameInput = elements.loginForm.elements.namedItem("username");
    const passwordInput = elements.loginForm.elements.namedItem("password");
    if (!(usernameInput instanceof HTMLInputElement) || !(passwordInput instanceof HTMLInputElement)) {
      return;
    }

    setLoginStatus("");
    setButtonBusy(elements.loginSubmit, true, "входим…", "войти");

    try {
      const { response, payload } = await request(API.login, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          username: usernameInput.value.trim(),
          password: passwordInput.value,
        }),
      });

      passwordInput.value = "";

      if (!response.ok || payload?.authenticated !== true) {
        setLoginStatus(loginError(payload, response.status));
        passwordInput.focus();
        return;
      }

      state.csrfToken = typeof payload.csrfToken === "string" ? payload.csrfToken : null;
      showOnly("dashboard");
      await loadVideos();
    } catch {
      setLoginStatus("Не получилось связаться с сервером. Попробуй ещё раз.");
    } finally {
      setButtonBusy(elements.loginSubmit, false, "входим…", "войти");
    }
  }

  async function loadVideos() {
    if (state.loadingVideos || !(elements.refreshButton instanceof HTMLButtonElement)) {
      return;
    }

    state.loadingVideos = true;
    setButtonBusy(elements.refreshButton, true, "обновляем…", "обновить");
    setDashboardStatus("Обновляем список…", "loading");

    try {
      const { response, payload } = await request(API.videos);

      if (response.status === 401) {
        showAuth("Сессия закончилась. Войди ещё раз.");
        return;
      }
      if (!response.ok || !Array.isArray(payload?.videos)) {
        setDashboardStatus(adminError(payload), "error");
        return;
      }

      const videos = sortVideos(payload.videos);
      renderVideos(videos);
      renderSummary(videos, payload.serverTime);
      setDashboardStatus("", "");
    } catch {
      setDashboardStatus("Не получилось обновить список. Проверь соединение и попробуй ещё раз.", "error");
    } finally {
      state.loadingVideos = false;
      setButtonBusy(elements.refreshButton, false, "обновляем…", "обновить");
    }
  }

  async function handleLogout() {
    if (!(elements.logoutButton instanceof HTMLButtonElement)) {
      return;
    }

    setButtonBusy(elements.logoutButton, true, "выходим…", "выйти");

    try {
      const headers = {};
      if (state.csrfToken) {
        headers["X-CSRF-Token"] = state.csrfToken;
      }
      const { response, payload } = await request(API.logout, { method: "POST", headers });
      if (!response.ok && response.status !== 401) {
        setDashboardStatus(adminError(payload), "error");
        return;
      }
      showAuth();
    } catch {
      setDashboardStatus("Не получилось выйти. Попробуй ещё раз.", "error");
    } finally {
      setButtonBusy(elements.logoutButton, false, "выходим…", "выйти");
    }
  }

  function showOnly(view) {
    if (elements.sessionCheck instanceof HTMLElement) {
      elements.sessionCheck.hidden = view !== "checking";
    }
    if (elements.authView instanceof HTMLElement) {
      elements.authView.hidden = view !== "auth";
    }
    if (elements.dashboardView instanceof HTMLElement) {
      elements.dashboardView.hidden = view !== "dashboard";
    }
  }

  function showAuth(message = "") {
    state.csrfToken = null;
    showOnly("auth");
    setLoginStatus(message);
    clearDashboard();

    const passwordInput = elements.loginForm instanceof HTMLFormElement
      ? elements.loginForm.elements.namedItem("password")
      : null;
    if (passwordInput instanceof HTMLInputElement) {
      passwordInput.value = "";
      passwordInput.focus();
    }
  }

  function clearDashboard() {
    elements.videoList?.replaceChildren();
    if (elements.videoCount instanceof HTMLElement) {
      elements.videoCount.textContent = "загружаем список…";
    }
    if (elements.updatedAt instanceof HTMLElement) {
      elements.updatedAt.textContent = "";
    }
    setDashboardStatus("", "");
  }

  function renderVideos(videos) {
    if (!(elements.videoList instanceof HTMLElement)) {
      return;
    }

    elements.videoList.replaceChildren();

    if (videos.length === 0) {
      const row = document.createElement("tr");
      const cell = document.createElement("td");
      cell.colSpan = 6;
      cell.className = "empty-cell";
      cell.textContent = "Пока никто ничего не предложил.";
      row.append(cell);
      elements.videoList.append(row);
      return;
    }

    const fragment = document.createDocumentFragment();
    for (const video of videos) {
      fragment.append(createVideoRow(video));
    }
    elements.videoList.append(fragment);
  }

  function createVideoRow(video) {
    const row = document.createElement("tr");

    const requestsCell = createCell("просьбы");
    const requestCount = document.createElement("span");
    requestCount.className = "request-count";
    requestCount.textContent = String(safeCount(video.requestCount));
    requestsCell.append(requestCount);

    const freshnessCell = createCell("свежесть");
    const freshness = normalizeFreshness(video.freshness);
    const freshnessBadge = document.createElement("span");
    freshnessBadge.className = `freshness-badge freshness-${freshness}`;
    freshnessBadge.textContent = freshnessLabel(video.freshnessLabel, freshness);
    freshnessCell.append(freshnessBadge);

    const dateCell = createCell("дата", "video-date");
    dateCell.textContent = formatDate(video.publishedAt);

    const titleCell = createCell("название", "video-title");
    titleCell.textContent = safeText(video.title, "Без названия");

    const durationCell = createCell("длительность", "video-duration");
    durationCell.textContent = formatDuration(video.durationSeconds);

    const linkCell = createCell("ссылка");
    const safeUrl = safeYouTubeUrl(video.url, video.youtubeId);
    if (safeUrl) {
      const link = document.createElement("a");
      link.className = "youtube-link";
      link.href = safeUrl;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.textContent = "открыть ↗";
      linkCell.append(link);
    } else {
      linkCell.textContent = "недоступна";
    }

    row.append(requestsCell, freshnessCell, dateCell, titleCell, durationCell, linkCell);
    return row;
  }

  function createCell(label, className = "") {
    const cell = document.createElement("td");
    cell.dataset.label = label;
    if (className) {
      cell.className = className;
    }
    return cell;
  }

  function renderSummary(videos, serverTime) {
    if (elements.videoCount instanceof HTMLElement) {
      const totalRequests = videos.reduce((sum, video) => sum + safeCount(video.requestCount), 0);
      elements.videoCount.textContent = `${plural(videos.length, "видео", "видео", "видео")} · ${plural(totalRequests, "просьба", "просьбы", "просьб")}`;
    }
    if (elements.updatedAt instanceof HTMLElement) {
      const date = parseDate(serverTime) || new Date();
      elements.updatedAt.textContent = `обновлено ${new Intl.DateTimeFormat("ru-RU", {
        day: "numeric",
        month: "long",
        hour: "2-digit",
        minute: "2-digit",
      }).format(date)}`;
    }
  }

  function sortVideos(videos) {
    return [...videos].sort((left, right) => {
      const requestDifference = safeCount(right?.requestCount) - safeCount(left?.requestCount);
      if (requestDifference !== 0) {
        return requestDifference;
      }
      return dateTimestamp(right?.publishedAt) - dateTimestamp(left?.publishedAt);
    });
  }

  function safeYouTubeUrl(value, youtubeId) {
    if (typeof youtubeId === "string" && /^[A-Za-z0-9_-]{11}$/.test(youtubeId)) {
      return `https://www.youtube.com/watch?v=${youtubeId}`;
    }

    try {
      const parsed = new URL(typeof value === "string" ? value : "");
      const allowedHosts = new Set([
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtu.be",
        "www.youtu.be",
      ]);
      if (
        parsed.protocol === "https:"
        && parsed.username === ""
        && parsed.password === ""
        && (parsed.port === "" || parsed.port === "443")
        && allowedHosts.has(parsed.hostname.toLowerCase())
      ) {
        return parsed.href;
      }
    } catch {
      // A canonical fallback is built below when the API returned a valid video ID.
    }

    return null;
  }

  function normalizeFreshness(value) {
    if (value === "fresh" || value === "moderate" || value === "old") {
      return value;
    }
    return "old";
  }

  function freshnessLabel(value, freshness) {
    if (typeof value === "string" && value.trim()) {
      return value.trim();
    }
    return {
      fresh: "Свежее",
      moderate: "Не очень свежее",
      old: "Старенькое",
    }[freshness];
  }

  function formatDate(value) {
    const date = parseDate(value);
    if (!date) {
      return "—";
    }
    return new Intl.DateTimeFormat("ru-RU", {
      day: "2-digit",
      month: "2-digit",
      year: "numeric",
    }).format(date);
  }

  function formatDuration(value) {
    const totalSeconds = Math.max(0, Math.floor(Number(value)));
    if (!Number.isFinite(totalSeconds)) {
      return "—";
    }
    const hours = Math.floor(totalSeconds / 3600);
    const minutes = Math.floor((totalSeconds % 3600) / 60);
    const seconds = totalSeconds % 60;
    if (hours > 0) {
      return `${hours}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
    }
    return `${minutes}:${String(seconds).padStart(2, "0")}`;
  }

  function parseDate(value) {
    if (typeof value !== "string" || !value) {
      return null;
    }
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? null : date;
  }

  function dateTimestamp(value) {
    return parseDate(value)?.getTime() ?? 0;
  }

  function safeCount(value) {
    const count = Math.floor(Number(value));
    return Number.isFinite(count) && count > 0 ? count : 0;
  }

  function safeText(value, fallback) {
    return typeof value === "string" && value.trim() ? value.trim() : fallback;
  }

  function plural(count, one, few, many) {
    const mod100 = count % 100;
    const mod10 = count % 10;
    const word = mod100 >= 11 && mod100 <= 19
      ? many
      : mod10 === 1
        ? one
        : mod10 >= 2 && mod10 <= 4
          ? few
          : many;
    return `${count} ${word}`;
  }

  function setLoginStatus(message) {
    if (elements.loginStatus instanceof HTMLElement) {
      elements.loginStatus.textContent = message;
    }
  }

  function setDashboardStatus(message, status) {
    if (!(elements.dashboardStatus instanceof HTMLElement)) {
      return;
    }
    elements.dashboardStatus.textContent = message;
    if (status) {
      elements.dashboardStatus.dataset.state = status;
    } else {
      delete elements.dashboardStatus.dataset.state;
    }
  }

  function setButtonBusy(button, busy, busyLabel, idleLabel) {
    button.disabled = busy;
    button.textContent = busy ? busyLabel : idleLabel;
  }

  function loginError(payload, status) {
    if (payload?.error === "rate_limited" || status === 429) {
      return "Слишком много попыток. Подожди немного и попробуй снова.";
    }
    if (payload?.error === "invalid_credentials" || status === 401) {
      return "Неверный логин или пароль.";
    }
    return adminError(payload);
  }

  function adminError(payload) {
    if (typeof payload?.message === "string" && payload.message.trim()) {
      return payload.message.trim();
    }
    return "Что-то пошло не так. Попробуй ещё раз.";
  }

  async function request(url, options = {}) {
    const headers = new Headers(options.headers || {});
    headers.set("Accept", "application/json");
    const response = await fetch(url, {
      ...options,
      credentials: "same-origin",
      headers,
    });
    let payload = null;
    try {
      payload = await response.json();
    } catch {
      // Non-JSON errors are converted to the generic user-facing message.
    }
    return { response, payload };
  }
})();
