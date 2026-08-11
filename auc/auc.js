(() => {
  "use strict";

  const API_BASE = "/api/auc";
  const POLL_INTERVAL_MS = 1500;
  const TIMER_INTERVAL_MS = 250;
  const OPTION_COLORS = ["#a9ddea", "#ffd96a", "#ffb69f", "#b9dfa5", "#cec5ed"];
  const STATUS_LABELS = {
    draft: "черновик",
    open: "идёт",
    closing: "закрывается",
    finished: "завершён",
    cancelled: "отменён",
  };
  const MODE_LABELS = {
    leader: "Лидерный аукцион",
    "weighted-wheel": "Взвешенное колесо",
  };

  const elements = Object.fromEntries(
    [
      "connection-dot",
      "connection-label",
      "offline-banner",
      "public-empty",
      "auction-view",
      "auction-status",
      "auction-mode",
      "auction-title",
      "auction-description",
      "auction-timer",
      "auction-total",
      "seed-preview",
      "seed-commitment",
      "options-kicker",
      "option-count",
      "option-list",
      "options-empty",
      "wheel-panel",
      "auction-wheel",
      "contribution-list",
      "contributions-empty",
      "result-panel",
      "result-title",
      "winner-name",
      "result-message",
      "verification-details",
      "verification-list",
      "copy-verification",
      "admin-toggle",
      "admin-panel",
      "admin-title",
      "admin-session-label",
      "login-form",
      "admin-password",
      "admin-workspace",
      "admin-context",
      "logout-button",
      "auction-form-title",
      "auction-form-hint",
      "auction-form",
      "admin-auction-title",
      "admin-auction-description",
      "admin-auction-mode",
      "admin-auction-duration",
      "auction-submit",
      "add-option-form",
      "new-option-name",
      "admin-option-list",
      "admin-options-empty",
      "contribution-form",
      "contribution-option",
      "contribution-amount",
      "admin-contribution-list",
      "admin-contributions-empty",
      "load-more-contributions",
      "admin-state-label",
      "start-button",
      "close-button",
      "cancel-reason",
      "cancel-button",
      "dispute-option",
      "dispute-reason",
      "dispute-button",
      "refresh-audit",
      "audit-list",
      "audit-empty",
      "toast",
    ].map((id) => [id, document.getElementById(id)]),
  );

  const moneyFormatter = new Intl.NumberFormat("ru-RU", {
    style: "currency",
    currency: "RUB",
    minimumFractionDigits: 0,
    maximumFractionDigits: 2,
  });
  const dateFormatter = new Intl.DateTimeFormat("ru-RU", {
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });

  const state = {
    auction: null,
    contributions: [],
    adminContributions: null,
    adminContributionsHasMore: false,
    result: null,
    session: { authenticated: false, csrfToken: null },
    auditEvents: [],
    serverOffsetMs: 0,
    hasServerClock: false,
    requestSequence: 0,
    appliedSequence: 0,
    pollTimer: null,
    toastTimer: null,
    adminOpen: false,
    hydratedAuctionKey: null,
    adminOptionsSignature: null,
    adminContributionsSignature: null,
    adminSelectsSignature: null,
    verificationPayload: null,
    pendingContribution: null,
  };

  class ApiError extends Error {
    constructor(message, status, payload) {
      super(message);
      this.name = "ApiError";
      this.status = status;
      this.payload = payload;
    }
  }

  function firstValue(object, keys, fallback = undefined) {
    for (const key of keys) {
      if (object && object[key] !== undefined && object[key] !== null) return object[key];
    }
    return fallback;
  }

  function objectBody(payload) {
    if (!payload || typeof payload !== "object") return {};
    return payload.data && typeof payload.data === "object" && !Array.isArray(payload.data)
      ? payload.data
      : payload;
  }

  function integer(value, fallback = 0) {
    const number = Number(value);
    return Number.isSafeInteger(number) ? number : fallback;
  }

  function nonNegativeInteger(value, fallback = 0) {
    const number = integer(value, fallback);
    return number >= 0 ? number : fallback;
  }

  function stringId(value) {
    return value === undefined || value === null ? "" : String(value);
  }

  function normalizeRawStatus(value) {
    return String(value || "DRAFT").trim().toUpperCase().replaceAll("-", "_");
  }

  function publicStatus(auction, now = serverNow()) {
    if (!auction) return "draft";
    const status = auction.rawStatus;
    if (["LOCKED", "RESOLVING", "CLOSING"].includes(status)) return "closing";
    if (status === "FINISHED" || status === "COMPLETED") return "finished";
    if (status === "CANCELLED" || status === "CANCELED") return "cancelled";
    if (status === "OPEN" || status === "RUNNING") {
      const endsAt = parseTimestamp(auction.endsAt);
      return endsAt !== null && endsAt <= now ? "closing" : "open";
    }
    return "draft";
  }

  function normalizeMode(value) {
    const mode = String(value || "leader").trim().toLowerCase().replaceAll("_", "-");
    return mode === "weighted" || mode === "wheel" || mode === "weighted-wheel"
      ? "weighted-wheel"
      : "leader";
  }

  function normalizeOption(raw, totalKopecks) {
    const amountKopecks = nonNegativeInteger(
      firstValue(raw, ["amountKopecks", "amount_kopecks", "totalKopecks", "total_kopecks", "amount"]),
    );
    const suppliedShare = firstValue(raw, ["shareBasisPoints", "share_basis_points", "shareBps", "share_bps"]);
    const shareBasisPoints = Number.isFinite(Number(suppliedShare))
      ? Math.max(0, Math.min(10000, Math.round(Number(suppliedShare))))
      : totalKopecks > 0
        ? Math.round((amountKopecks / totalKopecks) * 10000)
        : 0;
    return {
      id: stringId(firstValue(raw, ["id", "optionId", "option_id"])),
      name: String(firstValue(raw, ["name", "title", "label"], "Без названия")),
      sortOrder: integer(firstValue(raw, ["sortOrder", "sort_order", "position", "order"]), 0),
      amountKopecks,
      shareBasisPoints,
      suppliedRank: integer(firstValue(raw, ["rank", "place"]), 0),
      raw,
    };
  }

  function extractAuction(payload) {
    const body = objectBody(payload);
    const candidate = firstValue(body, ["auction", "currentAuction", "current_auction", "current"]);
    if (candidate === null) return null;
    if (candidate && typeof candidate === "object") return candidate;
    return firstValue(body, ["id", "auctionId", "auction_id"]) !== undefined ? body : null;
  }

  function normalizeAuction(raw) {
    if (!raw || typeof raw !== "object") return null;
    const rawOptions = firstValue(raw, ["options", "auctionOptions", "auction_options"], []);
    const suppliedTotal = firstValue(raw, ["totalKopecks", "total_kopecks", "totalAmountKopecks", "total_amount_kopecks"]);
    const initialOptions = Array.isArray(rawOptions)
      ? rawOptions.map((option) => normalizeOption(option, 0))
      : [];
    const calculatedTotal = initialOptions.reduce((sum, option) => sum + option.amountKopecks, 0);
    const totalKopecks = nonNegativeInteger(suppliedTotal, calculatedTotal);
    const options = (Array.isArray(rawOptions) ? rawOptions : [])
      .map((option) => normalizeOption(option, totalKopecks))
      .sort((left, right) => left.sortOrder - right.sortOrder || left.name.localeCompare(right.name, "ru"));

    options.forEach((option) => {
      option.rank = option.suppliedRank > 0
        ? option.suppliedRank
        : 1 + options.filter((candidate) => candidate.amountKopecks > option.amountKopecks).length;
    });

    return {
      id: stringId(firstValue(raw, ["id", "auctionId", "auction_id"])),
      title: String(firstValue(raw, ["title", "name"], "Аукцион")),
      description: String(firstValue(raw, ["description", "rules"], "")),
      mode: normalizeMode(firstValue(raw, ["mode", "auctionMode", "auction_mode"])),
      rawStatus: normalizeRawStatus(firstValue(raw, ["status", "state"])),
      durationSeconds: nonNegativeInteger(firstValue(raw, ["durationSeconds", "duration_seconds", "duration"]), 0),
      startsAt: firstValue(raw, ["startsAt", "starts_at", "startedAt", "started_at"], null),
      endsAt: firstValue(raw, ["endsAt", "ends_at", "expiresAt", "expires_at"], null),
      totalKopecks,
      seedCommitment: String(firstValue(raw, ["seedCommitment", "seed_commitment", "commitment"], "")),
      cancelReason: String(firstValue(raw, ["cancelReason", "cancel_reason", "cancellationReason", "cancellation_reason"], "")),
      updatedAt: firstValue(raw, ["updatedAt", "updated_at"], null),
      options,
      inlineResult: firstValue(raw, ["result", "auctionResult", "auction_result"], null),
      raw,
    };
  }

  function extractArray(payload, keys) {
    if (Array.isArray(payload)) return payload;
    const body = objectBody(payload);
    for (const key of keys) {
      if (Array.isArray(body[key])) return body[key];
    }
    return [];
  }

  function normalizeContribution(raw) {
    const kind = String(firstValue(raw, ["kind", "type", "status"], "contribution")).toLowerCase();
    return {
      id: stringId(firstValue(raw, ["id", "contributionId", "contribution_id"])),
      optionId: stringId(firstValue(raw, ["optionId", "option_id"])),
      optionName: String(firstValue(raw, ["optionName", "option_name", "name"], "Вариант")),
      amountKopecks: integer(firstValue(raw, ["amountKopecks", "amount_kopecks", "amount"]), 0),
      kind: kind.includes("void") || kind.includes("cancel") || kind.includes("reversal") ? "void" : "contribution",
      createdAt: firstValue(raw, ["createdAt", "created_at", "timestamp"], null),
      reason: String(firstValue(raw, ["reason", "voidReason", "void_reason"], "")),
      voided: Boolean(firstValue(raw, ["voided", "isVoided", "is_voided"], false)),
      raw,
    };
  }

  function normalizeResult(payload) {
    if (!payload) return null;
    const body = objectBody(payload);
    const result = body.result === null
      ? null
      : body.result && typeof body.result === "object"
        ? body.result
        : body;
    if (!result || typeof result !== "object" || Array.isArray(result)) return null;

    const winner = firstValue(result, ["winner", "winnerOption", "winner_option"], {});
    return {
      winnerOptionId: stringId(
        firstValue(result, ["winnerOptionId", "winner_option_id", "winnerId", "winner_id", "optionId", "option_id"],
          firstValue(winner, ["id", "optionId", "option_id"])),
      ),
      winnerName: String(
        firstValue(result, ["winnerName", "winner_name", "optionName", "option_name"],
          firstValue(winner, ["name", "title", "label"], "")),
      ),
      originalWinnerOptionId: stringId(firstValue(result, ["originalWinnerOptionId", "original_winner_option_id"])),
      originalWinnerName: String(firstValue(result, ["originalWinnerName", "original_winner_name"], "")),
      originalReason: String(firstValue(result, ["originalReason", "original_reason"], "")),
      moderatorResolution: firstValue(result, ["moderatorResolution", "moderator_resolution"], null),
      reason: String(firstValue(result, ["reason", "message", "resolutionReason", "resolution_reason"], "")),
      seed: String(firstValue(result, ["seed", "seedReveal", "seed_reveal", "revealedSeed", "revealed_seed"], "")),
      seedCommitment: String(firstValue(result, ["seedCommitment", "seed_commitment", "commitment"], "")),
      snapshotHash: String(firstValue(result, ["snapshotHash", "snapshot_hash"], "")),
      algorithm: String(firstValue(result, ["algorithm", "algorithmVersion", "algorithm_version"], "")),
      selectionRule: String(firstValue(result, ["selectionRule", "selection_rule"], "")),
      drawValue: firstValue(result, ["drawValue", "draw_value", "winningTicket", "winning_ticket", "selectionKopecks", "selection_kopecks", "selectedOffset", "selected_offset"], null),
      totalWeight: firstValue(result, ["totalWeight", "total_weight", "totalKopecks", "total_kopecks"], null),
      snapshot: firstValue(result, ["snapshot", "canonicalSnapshot", "canonical_snapshot"], null),
      canonicalSnapshot: firstValue(result, ["canonicalSnapshot", "canonical_snapshot"], null),
      hmacCounter: firstValue(result, ["hmacCounter", "hmac_counter"], null),
      hmacDigest: firstValue(result, ["hmacDigest", "hmac_digest", "hmacDigestHex", "hmac_digest_hex"], null),
      rejectionLimit: firstValue(result, ["rejectionLimit", "rejection_limit", "rejectionLimitDecimal", "rejection_limit_decimal"], null),
      drawSpace: firstValue(result, ["drawSpace", "draw_space"], null),
      forced: Boolean(firstValue(result, ["forced", "isForced", "is_forced"], false)),
      raw: result,
    };
  }

  function serverTimeFrom(payload) {
    const body = objectBody(payload);
    return firstValue(payload, ["serverTime", "server_time"], firstValue(body, ["serverTime", "server_time"], null));
  }

  function updateServerClock(serverTime, requestStartedAt, responseReceivedAt) {
    const parsed = parseTimestamp(serverTime);
    if (parsed === null) return;
    state.serverOffsetMs = parsed - (requestStartedAt + responseReceivedAt) / 2;
    state.hasServerClock = true;
  }

  function serverNow() {
    return Date.now() + (state.hasServerClock ? state.serverOffsetMs : 0);
  }

  function parseTimestamp(value) {
    if (value === null || value === undefined || value === "") return null;
    if (typeof value === "number" && Number.isFinite(value)) {
      return value < 10_000_000_000 ? value * 1000 : value;
    }
    const parsed = Date.parse(String(value));
    return Number.isFinite(parsed) ? parsed : null;
  }

  function formatMoney(kopecks) {
    const amount = Number(kopecks);
    return moneyFormatter.format(Number.isFinite(amount) ? amount / 100 : 0);
  }

  function formatPercent(basisPoints) {
    const percent = Math.max(0, Number(basisPoints) || 0) / 100;
    return `${new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 2 }).format(percent)}%`;
  }

  function formatDate(value) {
    const timestamp = parseTimestamp(value);
    return timestamp === null ? "время не указано" : dateFormatter.format(new Date(timestamp));
  }

  function pluralizeOptions(count) {
    const mod10 = count % 10;
    const mod100 = count % 100;
    if (mod10 === 1 && mod100 !== 11) return `${count} вариант`;
    if ([2, 3, 4].includes(mod10) && ![12, 13, 14].includes(mod100)) return `${count} варианта`;
    return `${count} вариантов`;
  }

  function parseRublesToKopecks(rawValue) {
    const value = String(rawValue ?? "").trim();
    const match = /^(0|[1-9]\d*|[1-9]\d{0,2}(?:[ \u00a0\u202f]\d{3})+)(?:[.,](\d{1,2}))?$/.exec(value);
    if (!match) {
      throw new Error("Введите сумму в формате 1000 или 1000,50.");
    }
    const whole = BigInt(match[1].replace(/[ \u00a0\u202f]/g, ""));
    const fraction = BigInt((match[2] || "").padEnd(2, "0") || "0");
    const kopecks = whole * 100n + fraction;
    if (kopecks <= 0n) throw new Error("Сумма должна быть больше нуля.");
    if (kopecks > BigInt(Number.MAX_SAFE_INTEGER)) throw new Error("Сумма слишком большая.");
    return Number(kopecks);
  }

  function makeRequestId() {
    if (typeof crypto.randomUUID === "function") return crypto.randomUUID();
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0"));
    return `${hex.slice(0, 4).join("")}-${hex.slice(4, 6).join("")}-${hex.slice(6, 8).join("")}-${hex.slice(8, 10).join("")}-${hex.slice(10).join("")}`;
  }

  async function requestJson(path, { method = "GET", body, admin = false } = {}) {
    const headers = { Accept: "application/json" };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (admin && state.session.csrfToken) headers["X-CSRF-Token"] = state.session.csrfToken;

    const response = await fetch(`${API_BASE}${path}`, {
      method,
      headers,
      credentials: "same-origin",
      cache: "no-store",
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const rawText = await response.text();
    let payload = {};
    if (rawText) {
      try {
        payload = JSON.parse(rawText);
      } catch {
        payload = { message: rawText.slice(0, 300) };
      }
    }
    if (!response.ok) {
      const errorBody = objectBody(payload);
      const errorValue = firstValue(errorBody, ["error", "message", "detail"]);
      const errorMessage = typeof errorValue === "object" && errorValue !== null
        ? String(firstValue(errorValue, ["message", "detail", "code"], `Ошибка HTTP ${response.status}`))
        : String(errorValue || `Ошибка HTTP ${response.status}`);
      throw new ApiError(errorMessage, response.status, payload);
    }
    return payload;
  }

  function showToast(message, kind = "info") {
    clearTimeout(state.toastTimer);
    elements.toast.textContent = String(message);
    elements.toast.dataset.kind = kind;
    elements.toast.hidden = false;
    state.toastTimer = window.setTimeout(() => {
      elements.toast.hidden = true;
    }, kind === "error" ? 6000 : 3500);
  }

  function humanError(error) {
    if (error instanceof ApiError) {
      if (error.status === 401) return "Сессия завершилась. Войдите снова.";
      if (error.status === 403) return "Действие отклонено: обновите сессию и попробуйте ещё раз.";
      if (error.status === 409) return error.message || "Состояние аукциона уже изменилось.";
      if (error.status === 429) return "Слишком много запросов. Немного подождите.";
      return error.message;
    }
    return error instanceof Error ? error.message : "Неизвестная ошибка";
  }

  function setConnection(online) {
    elements["connection-dot"].dataset.state = online ? "online" : "offline";
    elements["connection-label"].textContent = online ? "в сети" : "нет связи";
    elements["offline-banner"].hidden = online;
  }

  function schedulePoll(delay = POLL_INTERVAL_MS) {
    clearTimeout(state.pollTimer);
    state.pollTimer = window.setTimeout(pollPublic, delay);
  }

  async function pollPublic() {
    const sequence = ++state.requestSequence;
    const requestStartedAt = Date.now();
    try {
      const payload = await requestJson("/current");
      const responseReceivedAt = Date.now();
      if (sequence !== state.requestSequence) return;
      state.appliedSequence = sequence;
      updateServerClock(serverTimeFrom(payload), requestStartedAt, responseReceivedAt);
      setConnection(true);

      const previousAuctionId = state.auction?.id || null;
      const auction = normalizeAuction(extractAuction(payload));
      const nextAuctionId = auction?.id || null;
      if (previousAuctionId !== nextAuctionId) {
        state.contributions = [];
        state.adminContributions = null;
        state.adminContributionsHasMore = false;
        state.result = null;
        state.hydratedAuctionKey = null;
        state.adminOptionsSignature = null;
        state.adminContributionsSignature = null;
      }
      state.auction = auction;
      if (auction?.inlineResult) state.result = normalizeResult(auction.inlineResult);
      renderAll();

      if (auction?.id) {
        const expectedId = auction.id;
        const status = publicStatus(auction);
        const requests = [requestJson(`/${encodeURIComponent(expectedId)}/contributions`)];
        const shouldLoadResult = Boolean(auction.inlineResult) || ["closing", "finished"].includes(status);
        if (shouldLoadResult) requests.push(requestJson(`/${encodeURIComponent(expectedId)}/result`));
        const settled = await Promise.allSettled(requests);
        if (sequence !== state.requestSequence || state.auction?.id !== expectedId) return;

        if (settled[0]?.status === "fulfilled") {
          state.contributions = extractArray(settled[0].value, ["contributions", "items", "entries"])
            .map(normalizeContribution);
        }
        if (shouldLoadResult && settled[1]?.status === "fulfilled") {
          state.result = normalizeResult(settled[1].value) || state.result;
        }
        renderAll();
      }
    } catch (error) {
      if (sequence === state.requestSequence) setConnection(false);
      if (!state.auction) renderAll();
    } finally {
      if (sequence === state.requestSequence) schedulePoll();
    }
  }

  function renderAll() {
    renderPublic();
    if (state.adminOpen) renderAdmin();
  }

  function renderPublic() {
    const auction = state.auction;
    elements["public-empty"].hidden = Boolean(auction);
    elements["auction-view"].hidden = !auction;
    if (!auction) {
      if (window.AuctionWheel) window.AuctionWheel.clear(elements["auction-wheel"]);
      return;
    }

    elements["auction-title"].textContent = auction.title;
    elements["auction-description"].textContent = auction.description;
    elements["auction-description"].hidden = !auction.description;
    elements["auction-mode"].textContent = MODE_LABELS[auction.mode] || auction.mode;
    elements["auction-total"].textContent = formatMoney(auction.totalKopecks);
    elements["option-count"].textContent = pluralizeOptions(auction.options.length);
    elements["options-kicker"].textContent = auction.mode === "leader" ? "рейтинг по сумме" : "точные доли колеса";
    elements["seed-preview"].hidden = !auction.seedCommitment;
    elements["seed-commitment"].textContent = auction.seedCommitment;
    renderStatusAndTimer();
    renderOptions(auction);
    renderContributions();
    renderWheel(auction);
    renderResult(auction);
  }

  function renderStatusAndTimer() {
    const auction = state.auction;
    if (!auction) return;
    const status = publicStatus(auction);
    elements["auction-status"].dataset.status = status;
    elements["auction-status"].textContent = STATUS_LABELS[status] || status;

    if (status === "draft") {
      elements["auction-timer"].textContent = auction.durationSeconds > 0
        ? `${Math.ceil(auction.durationSeconds / 60)} мин`
        : "не запущен";
      return;
    }
    if (status === "closing") {
      elements["auction-timer"].textContent = "закрываем…";
      return;
    }
    if (status === "finished") {
      elements["auction-timer"].textContent = "завершён";
      return;
    }
    if (status === "cancelled") {
      elements["auction-timer"].textContent = "отменён";
      return;
    }

    const endsAt = parseTimestamp(auction.endsAt);
    if (endsAt === null) {
      elements["auction-timer"].textContent = "идёт";
      return;
    }
    const remaining = Math.max(0, endsAt - serverNow());
    const totalSeconds = Math.ceil(remaining / 1000);
    const hours = Math.floor(totalSeconds / 3600);
    const minutes = Math.floor((totalSeconds % 3600) / 60);
    const seconds = totalSeconds % 60;
    elements["auction-timer"].textContent = hours > 0
      ? `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`
      : `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
  }

  function renderOptions(auction) {
    const options = auction.mode === "leader"
      ? [...auction.options].sort((left, right) => left.rank - right.rank || right.amountKopecks - left.amountKopecks || left.sortOrder - right.sortOrder)
      : [...auction.options].sort((left, right) => left.sortOrder - right.sortOrder);
    elements["option-list"].replaceChildren();
    elements["options-empty"].hidden = options.length > 0;

    options.forEach((option, index) => {
      const item = document.createElement("li");
      item.className = "option-item";
      item.dataset.leading = String(auction.mode === "leader" && option.rank === 1 && option.amountKopecks > 0);

      const row = document.createElement("div");
      row.className = "option-row";
      const rank = document.createElement("span");
      rank.className = "option-rank";
      rank.textContent = auction.mode === "leader" ? String(option.rank) : String(index + 1);
      const name = document.createElement("span");
      name.className = "option-name";
      name.textContent = option.name;
      const money = document.createElement("strong");
      money.className = "option-money";
      money.textContent = formatMoney(option.amountKopecks);
      row.append(rank, name, money);

      const progress = document.createElement("div");
      progress.className = "option-progress";
      progress.setAttribute("role", "progressbar");
      progress.setAttribute("aria-label", `Доля варианта ${option.name}`);
      progress.setAttribute("aria-valuemin", "0");
      progress.setAttribute("aria-valuemax", "100");
      progress.setAttribute("aria-valuenow", String(option.shareBasisPoints / 100));
      const fill = document.createElement("div");
      fill.className = "option-progress-fill";
      fill.style.width = `${Math.max(0, Math.min(100, option.shareBasisPoints / 100))}%`;
      fill.style.setProperty("--option-color", OPTION_COLORS[index % OPTION_COLORS.length]);
      progress.append(fill);

      const share = document.createElement("p");
      share.className = "option-share";
      share.textContent = `${formatPercent(option.shareBasisPoints)} от общей суммы`;
      item.append(row, progress, share);
      elements["option-list"].append(item);
    });
  }

  function renderContributions() {
    const contributions = state.contributions.slice(0, 20);
    elements["contribution-list"].replaceChildren();
    elements["contributions-empty"].hidden = contributions.length > 0;
    contributions.forEach((contribution) => {
      const item = document.createElement("li");
      item.className = "contribution-item";
      item.dataset.kind = contribution.kind;
      const row = document.createElement("div");
      row.className = "contribution-row";
      const name = document.createElement("span");
      name.className = "contribution-name";
      name.textContent = contribution.optionName;
      const amount = document.createElement("strong");
      amount.className = "contribution-amount";
      const prefix = contribution.kind === "void" && contribution.amountKopecks > 0 ? "−" : "";
      amount.textContent = `${prefix}${formatMoney(Math.abs(contribution.amountKopecks))}`;
      row.append(name, amount);
      const meta = document.createElement("p");
      meta.className = "contribution-meta";
      meta.textContent = contribution.kind === "void"
        ? `отменяющая запись · ${formatDate(contribution.createdAt)}`
        : formatDate(contribution.createdAt);
      item.append(row, meta);
      elements["contribution-list"].append(item);
    });
  }

  function renderWheel(auction) {
    const isWheel = auction.mode === "weighted-wheel";
    elements["wheel-panel"].hidden = !isWheel;
    if (!window.AuctionWheel) return;
    if (!isWheel) {
      window.AuctionWheel.clear(elements["auction-wheel"]);
      return;
    }
    window.AuctionWheel.render(elements["auction-wheel"], auction.options);
    if (publicStatus(auction) === "finished" && state.result?.winnerOptionId) {
      const wheelResult = state.result.forced && state.result.originalWinnerOptionId
        ? { ...state.result.raw, winnerOptionId: state.result.originalWinnerOptionId }
        : state.result.raw;
      window.AuctionWheel.animateToResult(elements["auction-wheel"], wheelResult, {
        options: auction.options,
      });
    }
  }

  function resultVerificationEntries(auction, result) {
    const entries = [];
    const add = (label, value) => {
      if (value === undefined || value === null || value === "") return;
      let display = value;
      if (typeof value === "object") {
        try {
          display = JSON.stringify(value);
        } catch {
          display = "[не удалось сериализовать]";
        }
      }
      entries.push([label, String(display)]);
    };
    add("Коммитмент seed", result?.seedCommitment || auction.seedCommitment);
    add("Раскрытый seed", result?.seed);
    add("Хеш снимка", result?.snapshotHash);
    add("Алгоритм", result?.algorithm);
    add("Правило выбора", result?.selectionRule);
    add("Исходный победитель", result?.originalWinnerName);
    add("Исходное основание", result?.originalReason);
    add("Решение модератора", result?.moderatorResolution);
    add("Формула", "commitment = SHA-256('moralsqd-auction-commitment-v1\\0' || seed); draw = HMAC-SHA-256(seed, 'moralsqd-auction-draw-v1\\0' || snapshotHash || uint64be(counter)); значения выше rejection limit отбрасываются; offset = digest mod drawSpace");
    add("Счётчик HMAC", result?.hmacCounter);
    add("HMAC digest", result?.hmacDigest);
    add("Граница rejection sampling", result?.rejectionLimit);
    add("Число жеребьёвки", result?.drawValue);
    add("Размер пространства", result?.drawSpace);
    add("Общий вес", result?.totalWeight);
    add("Канонический снимок", result?.canonicalSnapshot || result?.snapshot);
    return entries;
  }

  function renderResult(auction) {
    const status = publicStatus(auction);
    const result = state.result;
    const isCancelled = status === "cancelled";
    const shouldShow = isCancelled || (status === "finished" && Boolean(result));
    elements["result-panel"].hidden = !shouldShow;
    if (!shouldShow) return;

    if (isCancelled) {
      elements["result-title"].textContent = "Аукцион отменён";
      elements["winner-name"].textContent = "Без результата";
      elements["result-message"].textContent = auction.cancelReason || "Причина отмены не указана.";
    } else {
      const option = auction.options.find((candidate) => candidate.id === result.winnerOptionId);
      const winnerName = result.winnerName || option?.name || "Победитель не определён";
      elements["result-title"].textContent = result.forced
        ? "Решение модератора"
        : result.winnerOptionId ? "Победитель" : "Аукцион завершён";
      elements["winner-name"].textContent = winnerName;
      elements["result-message"].textContent = result.forced
        ? `Исходная жеребьёвка: ${result.originalWinnerName || "без победителя"}. Решение модератора: ${result.reason || "причина не указана"}. Исходный результат сохранён в данных проверки.`
        : result.reason || (result.winnerOptionId
          ? "Результат сохранён до запуска анимации."
          : "Общая сумма равна нулю, поэтому победитель не определён.");
    }

    const entries = resultVerificationEntries(auction, result);
    elements["verification-list"].replaceChildren();
    entries.forEach(([label, value]) => {
      const term = document.createElement("dt");
      term.textContent = label;
      const description = document.createElement("dd");
      description.textContent = value;
      elements["verification-list"].append(term, description);
    });
    elements["verification-details"].hidden = entries.length === 0;
    state.verificationPayload = entries.length > 0
      ? { auctionId: auction.id, result: result?.raw || null, seedCommitment: auction.seedCommitment }
      : null;
  }

  function sessionFrom(payload) {
    const body = objectBody(payload);
    const session = body.session && typeof body.session === "object" ? body.session : body;
    return {
      authenticated: Boolean(firstValue(session, ["authenticated", "isAuthenticated", "is_authenticated", "loggedIn", "logged_in"], false)),
      csrfToken: firstValue(session, ["csrfToken", "csrf_token"], firstValue(body, ["csrfToken", "csrf_token"], null)),
    };
  }

  async function loadSession({ quiet = false } = {}) {
    try {
      const payload = await requestJson("/admin/session");
      state.session = sessionFrom(payload);
      if (state.session.authenticated) {
        await Promise.all([
          refreshAudit({ quiet: true }),
          refreshAdminContributions({ quiet: true }),
        ]);
      }
    } catch (error) {
      state.session = { authenticated: false, csrfToken: null };
      if (!quiet) showToast(humanError(error), "error");
    }
    renderAdmin();
  }

  function renderAdmin() {
    const authenticated = state.session.authenticated;
    elements["login-form"].hidden = authenticated;
    elements["admin-workspace"].hidden = !authenticated;
    elements["admin-session-label"].textContent = authenticated ? "сессия активна" : "не авторизован";
    if (!authenticated) return;

    const auction = state.auction;
    const status = auction ? publicStatus(auction) : null;
    const isDraft = status === "draft";
    const isOpen = status === "open";
    const ended = status === "finished" || status === "cancelled";
    const canCreate = !auction || ended;
    const canEdit = Boolean(auction && isDraft);
    const formEnabled = canCreate || canEdit;

    elements["admin-context"].textContent = auction
      ? `Текущий аукцион: ${auction.title}. Статус: ${STATUS_LABELS[status] || status}.`
      : "Активного аукциона нет — можно создать черновик.";
    elements["admin-state-label"].textContent = status ? STATUS_LABELS[status] || status : "нет аукциона";
    elements["auction-form-title"].textContent = canEdit ? "Настройки черновика" : "Новый аукцион";
    elements["auction-form-hint"].textContent = formEnabled ? "один активный одновременно" : "настройки уже зафиксированы";
    elements["auction-submit"].textContent = canEdit ? "сохранить настройки" : "создать черновик";

    const formControls = elements["auction-form"].querySelectorAll("input, textarea, select, button");
    formControls.forEach((control) => {
      control.disabled = !formEnabled;
    });
    hydrateAuctionForm(auction, canEdit, canCreate);

    elements["add-option-form"].querySelectorAll("input, button").forEach((control) => {
      control.disabled = !canEdit;
    });
    renderAdminOptions(auction, canEdit);
    populateAdminOptionSelects(auction);

    elements["contribution-form"].querySelectorAll("input, select, button").forEach((control) => {
      control.disabled = !isOpen || !auction?.options.length;
    });
    renderAdminContributions(isOpen);

    elements["start-button"].disabled = !isDraft || (auction?.options.length || 0) < 2;
    elements["close-button"].disabled = !isOpen;
    elements["cancel-reason"].disabled = !(isDraft || isOpen);
    elements["cancel-button"].disabled = !(isDraft || isOpen);
    const canResolveDispute = Boolean(auction && status === "finished" && auction.options.length);
    elements["dispute-option"].disabled = !canResolveDispute;
    elements["dispute-reason"].disabled = !canResolveDispute;
    elements["dispute-button"].disabled = !canResolveDispute;
    renderAudit();
  }

  function hydrateAuctionForm(auction, canEdit, canCreate) {
    const key = canEdit ? `edit:${auction.id}:${auction.rawStatus}:${auction.updatedAt || ""}` : canCreate ? "create" : `locked:${auction?.id}`;
    if (state.hydratedAuctionKey === key) return;
    state.hydratedAuctionKey = key;
    if (canEdit) {
      elements["admin-auction-title"].value = auction.title;
      elements["admin-auction-description"].value = auction.description;
      elements["admin-auction-mode"].value = auction.mode;
      elements["admin-auction-duration"].value = String(Math.max(1, Math.ceil(auction.durationSeconds / 60)));
    } else if (canCreate) {
      elements["auction-form"].reset();
      elements["admin-auction-mode"].value = "leader";
      elements["admin-auction-duration"].value = "10";
    }
  }

  function renderAdminOptions(auction, enabled) {
    const options = auction?.options || [];
    const signature = `${enabled}:${options.map((option) => `${option.id}:${option.name}:${option.sortOrder}`).join("|")}`;
    if (state.adminOptionsSignature === signature) return;
    state.adminOptionsSignature = signature;
    elements["admin-option-list"].replaceChildren();
    elements["admin-options-empty"].hidden = options.length > 0;

    options.forEach((option) => {
      const item = document.createElement("li");
      item.className = "admin-option-item";
      const editRow = document.createElement("div");
      editRow.className = "admin-option-edit";
      const input = document.createElement("input");
      input.className = "admin-option-input";
      input.value = option.name;
      input.maxLength = 120;
      input.disabled = !enabled;
      input.setAttribute("aria-label", `Название варианта ${option.name}`);
      const save = document.createElement("button");
      save.className = "button button-small button-secondary";
      save.type = "button";
      save.textContent = "сохранить";
      save.disabled = !enabled;
      save.addEventListener("click", () => updateOption(option, input.value, save));
      const remove = document.createElement("button");
      remove.className = "button button-small button-danger-outline";
      remove.type = "button";
      remove.textContent = "удалить";
      remove.disabled = !enabled;
      remove.addEventListener("click", () => deleteOption(option, remove));
      editRow.append(input, save, remove);

      const mergeRow = document.createElement("div");
      mergeRow.className = "admin-option-merge";
      const target = document.createElement("select");
      target.className = "admin-option-select";
      target.disabled = !enabled || options.length < 2;
      target.setAttribute("aria-label", `Куда объединить вариант ${option.name}`);
      const placeholder = document.createElement("option");
      placeholder.value = "";
      placeholder.textContent = "объединить с…";
      target.append(placeholder);
      options.filter((candidate) => candidate.id !== option.id).forEach((candidate) => {
        const targetOption = document.createElement("option");
        targetOption.value = candidate.id;
        targetOption.textContent = candidate.name;
        target.append(targetOption);
      });
      const merge = document.createElement("button");
      merge.className = "button button-small button-quiet";
      merge.type = "button";
      merge.textContent = "объединить";
      merge.disabled = !enabled || options.length < 2;
      merge.addEventListener("click", () => mergeOption(option, target.value, merge));
      mergeRow.append(target, merge);
      item.append(editRow, mergeRow);
      elements["admin-option-list"].append(item);
    });
  }

  function populateSelect(select, options, placeholderText) {
    const previous = select.value;
    select.replaceChildren();
    if (placeholderText) {
      const placeholder = document.createElement("option");
      placeholder.value = "";
      placeholder.textContent = placeholderText;
      select.append(placeholder);
    }
    options.forEach((option) => {
      const item = document.createElement("option");
      item.value = option.id;
      item.textContent = option.name;
      select.append(item);
    });
    if (options.some((option) => option.id === previous)) select.value = previous;
  }

  function populateAdminOptionSelects(auction) {
    const options = auction?.options || [];
    const signature = options.map((option) => `${option.id}:${option.name}`).join("|");
    if (state.adminSelectsSignature === signature) return;
    state.adminSelectsSignature = signature;
    populateSelect(elements["contribution-option"], options, "выберите вариант");
    populateSelect(elements["dispute-option"], options, "выберите победителя");
  }

  function renderAdminContributions(canVoid) {
    const contributions = state.adminContributions || state.contributions;
    const signature = `${canVoid}:${state.adminContributionsHasMore}:${contributions.map((item) => `${item.id}:${item.kind}:${item.amountKopecks}:${item.voided}`).join("|")}`;
    if (state.adminContributionsSignature === signature) return;
    state.adminContributionsSignature = signature;
    elements["admin-contribution-list"].replaceChildren();
    elements["admin-contributions-empty"].hidden = contributions.length > 0;
    elements["load-more-contributions"].hidden = !state.adminContributionsHasMore;

    contributions.forEach((contribution) => {
      const item = document.createElement("li");
      item.className = "admin-contribution-item";
      const row = document.createElement("div");
      row.className = "admin-contribution-row";
      const main = document.createElement("div");
      main.className = "admin-contribution-main";
      const title = document.createElement("strong");
      title.textContent = `${contribution.optionName} · ${formatMoney(Math.abs(contribution.amountKopecks))}`;
      const meta = document.createElement("p");
      meta.className = "contribution-meta";
      meta.textContent = `${contribution.kind === "void" ? "отмена" : "ставка"} · ${formatDate(contribution.createdAt)}`;
      main.append(title, meta);
      const voidButton = document.createElement("button");
      voidButton.className = "button button-small button-danger-outline";
      voidButton.type = "button";
      voidButton.textContent = "отменить запись";
      voidButton.disabled = !canVoid || contribution.kind === "void" || contribution.voided || !contribution.id;
      voidButton.addEventListener("click", () => voidContribution(contribution, voidButton));
      row.append(main, voidButton);
      item.append(row);
      elements["admin-contribution-list"].append(item);
    });
  }

  function normalizeAuditEvent(raw) {
    const details = firstValue(raw, ["details", "payload", "metadata", "data"], "");
    let detailsText = details;
    if (typeof details === "object" && details !== null) {
      try {
        detailsText = JSON.stringify(details);
      } catch {
        detailsText = "[не удалось сериализовать]";
      }
    }
    return {
      id: stringId(firstValue(raw, ["id", "eventId", "event_id"])),
      action: String(firstValue(raw, ["action", "eventType", "event_type", "type"], "событие")),
      reason: String(firstValue(raw, ["reason"], "")),
      actor: String(firstValue(raw, ["actor", "admin", "createdBy", "created_by"], "")),
      createdAt: firstValue(raw, ["createdAt", "created_at", "timestamp"], null),
      details: String(detailsText || ""),
    };
  }

  async function refreshAudit({ quiet = false } = {}) {
    if (!state.session.authenticated) return;
    try {
      const query = state.auction?.id ? `?auctionId=${encodeURIComponent(state.auction.id)}` : "";
      const payload = await requestJson(`/admin/audit${query}`, { admin: true });
      state.auditEvents = extractArray(payload, ["events", "auditEvents", "audit_events", "items"])
        .map(normalizeAuditEvent);
      renderAudit();
    } catch (error) {
      if (!quiet) showToast(humanError(error), "error");
      if (error instanceof ApiError && error.status === 401) {
        state.session = { authenticated: false, csrfToken: null };
        renderAdmin();
      }
    }
  }

  async function refreshAdminContributions({ quiet = false, append = false } = {}) {
    if (!state.session.authenticated || !state.auction?.id) {
      state.adminContributions = null;
      state.adminContributionsHasMore = false;
      state.adminContributionsSignature = null;
      return;
    }
    const auctionId = state.auction.id;
    try {
      const query = new URLSearchParams({
        auctionId,
        limit: "200",
      });
      const lastContributionId = append
        ? state.adminContributions?.at(-1)?.id
        : null;
      if (lastContributionId) query.set("beforeId", lastContributionId);
      const payload = await requestJson(`/admin/contributions?${query}`, { admin: true });
      if (state.auction?.id !== auctionId) return;
      const page = extractArray(payload, ["contributions", "items", "entries"])
        .map(normalizeContribution);
      if (append && state.adminContributions) {
        const knownIds = new Set(state.adminContributions.map((item) => item.id));
        state.adminContributions = state.adminContributions.concat(
          page.filter((item) => !knownIds.has(item.id)),
        );
      } else {
        state.adminContributions = page;
      }
      state.adminContributionsHasMore = page.length === 200;
      state.adminContributionsSignature = null;
      if (state.adminOpen) renderAdminContributions(publicStatus(state.auction) === "open");
    } catch (error) {
      if (!quiet) showToast(humanError(error), "error");
      if (error instanceof ApiError && error.status === 401) {
        state.session = { authenticated: false, csrfToken: null };
        state.adminContributions = null;
        state.adminContributionsHasMore = false;
        renderAdmin();
      }
    }
  }

  async function loadMoreAdminContributions() {
    const button = elements["load-more-contributions"];
    button.disabled = true;
    try {
      await refreshAdminContributions({ append: true });
    } finally {
      button.disabled = false;
    }
  }

  function renderAudit() {
    elements["audit-list"].replaceChildren();
    elements["audit-empty"].hidden = state.auditEvents.length > 0;
    state.auditEvents.forEach((event) => {
      const item = document.createElement("li");
      item.className = "audit-item";
      const title = document.createElement("strong");
      title.textContent = event.action;
      const meta = document.createElement("p");
      meta.className = "audit-meta";
      meta.textContent = [formatDate(event.createdAt), event.actor].filter(Boolean).join(" · ");
      item.append(title, meta);
      if (event.reason) {
        const reason = document.createElement("p");
        reason.className = "audit-details";
        reason.textContent = `Причина: ${event.reason}`;
        item.append(reason);
      }
      if (event.details) {
        const details = document.createElement("p");
        details.className = "audit-details";
        details.textContent = event.details;
        item.append(details);
      }
      elements["audit-list"].append(item);
    });
  }

  async function runButton(button, operation) {
    const wasDisabled = button.disabled;
    button.disabled = true;
    try {
      await operation();
      return { ok: true, error: null };
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) {
        state.session = { authenticated: false, csrfToken: null };
        renderAdmin();
      }
      showToast(humanError(error), "error");
      return { ok: false, error };
    } finally {
      button.disabled = wasDisabled;
      if (state.adminOpen) renderAdmin();
    }
  }

  async function refreshAfterMutation(message) {
    showToast(message);
    await pollPublic();
    await Promise.all([
      refreshAudit({ quiet: true }),
      refreshAdminContributions({ quiet: true }),
    ]);
  }

  async function updateOption(option, rawName, button) {
    const name = String(rawName).trim();
    if (!name) {
      showToast("Название варианта не может быть пустым.", "error");
      return;
    }
    await runButton(button, async () => {
      await requestJson(`/admin/options/${encodeURIComponent(option.id)}`, {
        method: "PATCH",
        body: { name },
        admin: true,
      });
      state.adminOptionsSignature = null;
      await refreshAfterMutation("Вариант обновлён.");
    });
  }

  async function deleteOption(option, button) {
    if (!window.confirm(`Удалить вариант «${option.name}»? Это действие нельзя отменить.`)) return;
    await runButton(button, async () => {
      await requestJson(`/admin/options/${encodeURIComponent(option.id)}`, {
        method: "DELETE",
        admin: true,
      });
      state.adminOptionsSignature = null;
      await refreshAfterMutation("Вариант удалён.");
    });
  }

  async function mergeOption(option, targetOptionId, button) {
    const target = state.auction?.options.find((candidate) => candidate.id === targetOptionId);
    if (!target) {
      showToast("Выберите вариант, в который нужно объединить текущий.", "error");
      return;
    }
    if (!window.confirm(`Объединить «${option.name}» с «${target.name}»?`)) return;
    await runButton(button, async () => {
      await requestJson(`/admin/options/${encodeURIComponent(option.id)}/merge`, {
        method: "POST",
        body: { targetOptionId: target.id },
        admin: true,
      });
      state.adminOptionsSignature = null;
      await refreshAfterMutation("Варианты объединены.");
    });
  }

  async function voidContribution(contribution, button) {
    const reason = window.prompt("Укажите причину отмены записи:", "");
    if (reason === null) return;
    const normalizedReason = reason.trim();
    if (!normalizedReason) {
      showToast("Для отмены записи нужна причина.", "error");
      return;
    }
    await runButton(button, async () => {
      await requestJson(`/admin/contributions/${encodeURIComponent(contribution.id)}/void`, {
        method: "POST",
        body: { reason: normalizedReason, requestId: makeRequestId() },
        admin: true,
      });
      state.adminContributionsSignature = null;
      await refreshAfterMutation("Отменяющая запись создана.");
    });
  }

  async function handleLogin(event) {
    event.preventDefault();
    const submit = event.submitter || elements["login-form"].querySelector("button[type=submit]");
    await runButton(submit, async () => {
      const password = elements["admin-password"].value;
      const payload = await requestJson("/admin/login", {
        method: "POST",
        body: { password },
      });
      state.session = sessionFrom(payload);
      if (!state.session.authenticated || !state.session.csrfToken) await loadSession({ quiet: true });
      if (!state.session.authenticated) throw new Error("Сервер не подтвердил административную сессию.");
      elements["login-form"].reset();
      showToast("Вход выполнен.");
      renderAdmin();
      await Promise.all([
        refreshAudit({ quiet: true }),
        refreshAdminContributions({ quiet: true }),
      ]);
    });
  }

  async function handleLogout() {
    await runButton(elements["logout-button"], async () => {
      await requestJson("/admin/logout", { method: "POST", body: {}, admin: true });
      state.session = { authenticated: false, csrfToken: null };
      state.auditEvents = [];
      state.adminContributions = null;
      state.adminContributionsHasMore = false;
      state.hydratedAuctionKey = null;
      showToast("Вы вышли из админки.");
      renderAdmin();
    });
  }

  async function handleAuctionSubmit(event) {
    event.preventDefault();
    const auction = state.auction;
    const canEdit = auction && publicStatus(auction) === "draft";
    const durationMinutes = Number(elements["admin-auction-duration"].value);
    if (!Number.isSafeInteger(durationMinutes) || durationMinutes < 1 || durationMinutes > 1440) {
      showToast("Длительность должна быть целым числом от 1 до 1440 минут.", "error");
      return;
    }
    const body = {
      title: elements["admin-auction-title"].value.trim(),
      description: elements["admin-auction-description"].value.trim(),
      mode: elements["admin-auction-mode"].value,
      durationSeconds: durationMinutes * 60,
    };
    if (!body.title) {
      showToast("Укажите название аукциона.", "error");
      return;
    }

    const submit = event.submitter || elements["auction-submit"];
    await runButton(submit, async () => {
      await requestJson(canEdit
        ? `/admin/auctions/${encodeURIComponent(auction.id)}`
        : "/admin/auctions", {
        method: canEdit ? "PATCH" : "POST",
        body,
        admin: true,
      });
      state.hydratedAuctionKey = null;
      await refreshAfterMutation(canEdit ? "Настройки сохранены." : "Черновик создан.");
    });
  }

  async function handleAddOption(event) {
    event.preventDefault();
    const auction = state.auction;
    const name = elements["new-option-name"].value.trim();
    if (!auction || publicStatus(auction) !== "draft" || !name) return;
    const submit = event.submitter || elements["add-option-form"].querySelector("button[type=submit]");
    await runButton(submit, async () => {
      await requestJson(`/admin/auctions/${encodeURIComponent(auction.id)}/options`, {
        method: "POST",
        body: { name },
        admin: true,
      });
      elements["add-option-form"].reset();
      state.adminOptionsSignature = null;
      await refreshAfterMutation("Вариант добавлен.");
    });
  }

  async function handleContribution(event) {
    event.preventDefault();
    const auction = state.auction;
    if (!auction || publicStatus(auction) !== "open") return;
    let amountKopecks;
    try {
      amountKopecks = parseRublesToKopecks(elements["contribution-amount"].value);
    } catch (error) {
      showToast(humanError(error), "error");
      return;
    }
    const optionId = elements["contribution-option"].value;
    if (!auction.options.some((option) => option.id === optionId)) {
      showToast("Выберите вариант.", "error");
      return;
    }
    const submit = event.submitter || elements["contribution-form"].querySelector("button[type=submit]");
    const pendingKey = `${auction.id}:${optionId}:${amountKopecks}`;
    if (state.pendingContribution?.key !== pendingKey) {
      state.pendingContribution = { key: pendingKey, requestId: makeRequestId() };
    }
    const outcome = await runButton(submit, async () => {
      await requestJson(`/admin/auctions/${encodeURIComponent(auction.id)}/contributions`, {
        method: "POST",
        body: { optionId, amountKopecks, requestId: state.pendingContribution.requestId },
        admin: true,
      });
      elements["contribution-amount"].value = "";
      state.adminContributionsSignature = null;
      await refreshAfterMutation("Ставка принята.");
    });
    const isDefinitiveClientError = outcome.error instanceof ApiError
      && outcome.error.status >= 400
      && outcome.error.status < 500
      && ![408, 425, 429].includes(outcome.error.status);
    if (outcome.ok || isDefinitiveClientError) state.pendingContribution = null;
  }

  async function performAuctionAction(action, body, message, button) {
    const auction = state.auction;
    if (!auction) return { ok: false, error: null };
    return runButton(button, async () => {
      await requestJson(`/admin/auctions/${encodeURIComponent(auction.id)}/${action}`, {
        method: "POST",
        body,
        admin: true,
      });
      state.hydratedAuctionKey = null;
      await refreshAfterMutation(message);
    });
  }

  async function handleStart() {
    if (!state.auction || !window.confirm("Запустить аукцион? После запуска варианты и настройки нельзя будет изменить.")) return;
    await performAuctionAction("start", {}, "Аукцион запущен.", elements["start-button"]);
  }

  async function handleClose() {
    if (!state.auction || !window.confirm("Закрыть приём ставок досрочно и определить результат сейчас?")) return;
    await performAuctionAction("close", {}, "Приём ставок закрыт.", elements["close-button"]);
  }

  async function handleCancel() {
    const reason = elements["cancel-reason"].value.trim();
    if (!reason) {
      showToast("Для отмены нужна причина.", "error");
      return;
    }
    if (!window.confirm("Отменить аукцион без определения победителя?")) return;
    const outcome = await performAuctionAction("cancel", { reason }, "Аукцион отменён.", elements["cancel-button"]);
    if (outcome.ok) elements["cancel-reason"].value = "";
  }

  async function handleDispute() {
    const auction = state.auction;
    const optionId = elements["dispute-option"].value;
    const reason = elements["dispute-reason"].value.trim();
    if (!auction || !auction.options.some((option) => option.id === optionId)) {
      showToast("Выберите победителя.", "error");
      return;
    }
    if (!reason) {
      showToast("Для ручного решения нужна причина.", "error");
      return;
    }
    if (!window.confirm("Зафиксировать ручное решение? Оно попадёт в публичный результат и журнал.")) return;
    const outcome = await performAuctionAction("resolve-dispute", { optionId, reason }, "Ручное решение зафиксировано.", elements["dispute-button"]);
    if (outcome.ok) elements["dispute-reason"].value = "";
  }

  async function copyVerification() {
    if (!state.verificationPayload) return;
    try {
      await navigator.clipboard.writeText(JSON.stringify(state.verificationPayload, null, 2));
      showToast("Данные проверки скопированы.");
    } catch {
      showToast("Не удалось скопировать данные автоматически.", "error");
    }
  }

  async function toggleAdmin() {
    state.adminOpen = !state.adminOpen;
    elements["admin-panel"].hidden = !state.adminOpen;
    elements["admin-toggle"].setAttribute("aria-expanded", String(state.adminOpen));
    elements["admin-toggle"].textContent = state.adminOpen ? "закрыть админку" : "админка";
    if (state.adminOpen) {
      await loadSession({ quiet: true });
      const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      elements["admin-panel"].scrollIntoView({ block: "start", behavior: reducedMotion ? "auto" : "smooth" });
      window.requestAnimationFrame(() => {
        (state.session.authenticated ? elements["admin-title"] : elements["admin-password"]).focus({ preventScroll: true });
      });
    }
  }

  function bindEvents() {
    elements["admin-toggle"].addEventListener("click", toggleAdmin);
    elements["login-form"].addEventListener("submit", handleLogin);
    elements["logout-button"].addEventListener("click", handleLogout);
    elements["auction-form"].addEventListener("submit", handleAuctionSubmit);
    elements["add-option-form"].addEventListener("submit", handleAddOption);
    elements["contribution-form"].addEventListener("submit", handleContribution);
    elements["start-button"].addEventListener("click", handleStart);
    elements["close-button"].addEventListener("click", handleClose);
    elements["cancel-button"].addEventListener("click", handleCancel);
    elements["dispute-button"].addEventListener("click", handleDispute);
    elements["refresh-audit"].addEventListener("click", () => Promise.all([
      refreshAudit(),
      refreshAdminContributions(),
    ]));
    elements["load-more-contributions"].addEventListener("click", loadMoreAdminContributions);
    elements["copy-verification"].addEventListener("click", copyVerification);

    window.addEventListener("online", () => schedulePoll(0));
    window.addEventListener("offline", () => setConnection(false));
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") schedulePoll(0);
    });
  }

  function initialize() {
    const search = new URLSearchParams(window.location.search);
    if (search.get("obs") === "1") document.body.classList.add("obs-mode");
    bindEvents();
    renderAll();
    window.setInterval(renderStatusAndTimer, TIMER_INTERVAL_MS);
    pollPublic();
  }

  window.AuctionUI = Object.freeze({ formatMoney, parseRublesToKopecks });
  initialize();
})();
