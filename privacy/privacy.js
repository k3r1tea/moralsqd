"use strict";

(() => {
  const button = document.querySelector("[data-delete-video-suggestions]");
  const status = document.querySelector("[data-delete-video-suggestions-status]");
  if (!(button instanceof HTMLButtonElement) || !(status instanceof HTMLElement)) {
    return;
  }

  const originalLabel = button.textContent;
  let confirmationTimer = null;
  let armed = false;

  button.addEventListener("click", async () => {
    if (!armed) {
      armed = true;
      button.textContent = "Подтвердить удаление";
      status.textContent = "Нажми кнопку ещё раз: действие удалит все предложения этого браузера.";
      confirmationTimer = window.setTimeout(() => {
        resetConfirmation();
        status.textContent = "";
      }, 10_000);
      return;
    }

    if (confirmationTimer !== null) {
      window.clearTimeout(confirmationTimer);
      confirmationTimer = null;
    }

    button.disabled = true;
    button.textContent = "Удаляем…";
    status.textContent = "";

    try {
      const response = await fetch("/api/video-suggestions/delete-mine", {
        method: "POST",
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      });
      const payload = await readJson(response);

      if (response.status === 428 && payload?.error === "visitor_identity_required") {
        status.textContent = "В этом браузере нет сохранённого идентификатора предложений.";
        resetConfirmation();
        return;
      }

      if (!response.ok || payload?.ok !== true) {
        status.textContent = typeof payload?.message === "string"
          ? payload.message
          : "Не получилось удалить данные. Попробуй ещё раз.";
        resetConfirmation();
        return;
      }

      const deletedCount = Number.isInteger(payload.deletedCount) ? payload.deletedCount : 0;
      status.textContent = deletedCount > 0
        ? `Удалено предложений: ${deletedCount}. Анонимная cookie тоже удалена.`
        : "Связанных предложений не найдено. Анонимная cookie удалена.";
      button.textContent = "Данные удалены";
      button.disabled = true;
      armed = false;
    } catch {
      status.textContent = "Не получилось связаться с сервером. Попробуй ещё раз.";
      resetConfirmation();
    }
  });

  async function readJson(response) {
    try {
      return await response.json();
    } catch {
      return null;
    }
  }

  function resetConfirmation() {
    if (confirmationTimer !== null) {
      window.clearTimeout(confirmationTimer);
      confirmationTimer = null;
    }
    armed = false;
    button.disabled = false;
    button.textContent = originalLabel;
  }
})();
