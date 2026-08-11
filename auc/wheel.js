(() => {
  "use strict";

  const SVG_NS = "http://www.w3.org/2000/svg";
  const CENTER = 210;
  const RADIUS = 190;
  const LABEL_RADIUS = 125;
  const COLORS = [
    "#a9ddea",
    "#ffd96a",
    "#ffb69f",
    "#b9dfa5",
    "#cec5ed",
    "#8fc8d6",
    "#f4c95d",
    "#f49b84",
    "#9dce88",
    "#b6a8df",
  ];
  const wheelStates = new WeakMap();

  function finiteNonNegative(value) {
    const number = Number(value);
    return Number.isFinite(number) && number > 0 ? number : 0;
  }

  function optionId(option) {
    return String(option?.id ?? option?.optionId ?? option?.option_id ?? "");
  }

  function optionName(option) {
    return String(option?.name ?? option?.title ?? option?.label ?? "Вариант");
  }

  function optionAmount(option) {
    return finiteNonNegative(
      option?.amountKopecks ??
        option?.amount_kopecks ??
        option?.totalKopecks ??
        option?.total_kopecks ??
        option?.amount ??
        0,
    );
  }

  function calculateSectors(options) {
    const normalized = Array.isArray(options)
      ? options.map((option) => ({
          id: optionId(option),
          name: optionName(option),
          amountKopecks: optionAmount(option),
          source: option,
        }))
      : [];
    const positive = normalized.filter((option) => option.amountKopecks > 0);
    const totalKopecks = positive.reduce((sum, option) => sum + option.amountKopecks, 0);
    let cursor = 0;

    const sectors = positive.map((option, index) => {
      const startAngle = cursor;
      const endAngle = index === positive.length - 1
        ? 360
        : cursor + (option.amountKopecks / totalKopecks) * 360;
      cursor = endAngle;
      return {
        ...option,
        startAngle,
        endAngle,
        sweepAngle: endAngle - startAngle,
        share: option.amountKopecks / totalKopecks,
        color: COLORS[index % COLORS.length],
      };
    });

    return { sectors, totalKopecks };
  }

  function svgElement(name, attributes = {}) {
    const element = document.createElementNS(SVG_NS, name);
    Object.entries(attributes).forEach(([key, value]) => {
      element.setAttribute(key, String(value));
    });
    return element;
  }

  function pointAt(angle, radius = RADIUS) {
    const radians = ((angle - 90) * Math.PI) / 180;
    return {
      x: CENTER + radius * Math.cos(radians),
      y: CENTER + radius * Math.sin(radians),
    };
  }

  function sectorPath(sector) {
    const start = pointAt(sector.startAngle);
    const end = pointAt(sector.endAngle);
    const largeArc = sector.sweepAngle > 180 ? 1 : 0;
    return [
      `M ${CENTER} ${CENTER}`,
      `L ${start.x.toFixed(4)} ${start.y.toFixed(4)}`,
      `A ${RADIUS} ${RADIUS} 0 ${largeArc} 1 ${end.x.toFixed(4)} ${end.y.toFixed(4)}`,
      "Z",
    ].join(" ");
  }

  function shortLabel(name) {
    const chars = Array.from(name);
    return chars.length > 18 ? `${chars.slice(0, 17).join("")}…` : name;
  }

  function buildSector(sector, index) {
    const shape = sector.sweepAngle >= 359.999
      ? svgElement("circle", { cx: CENTER, cy: CENTER, r: RADIUS })
      : svgElement("path", { d: sectorPath(sector) });
    shape.setAttribute("class", "wheel-sector");
    shape.setAttribute("fill", sector.color);
    shape.dataset.optionId = sector.id;

    const title = svgElement("title");
    title.textContent = `${sector.name}: ${(sector.share * 100).toFixed(2)}%`;
    shape.append(title);

    const nodes = [shape];
    if (sector.sweepAngle >= 9) {
      const middleAngle = sector.startAngle + sector.sweepAngle / 2;
      const labelPoint = pointAt(middleAngle, sector.sweepAngle < 22 ? 148 : LABEL_RADIUS);
      const label = svgElement("text", {
        x: labelPoint.x.toFixed(3),
        y: labelPoint.y.toFixed(3),
        class: "wheel-label",
        "dominant-baseline": "middle",
      });
      label.dataset.optionId = sector.id;
      label.textContent = shortLabel(sector.name);
      if (sector.sweepAngle < 18) {
        label.setAttribute("font-size", "10");
      }
      nodes.push(label);
    }

    return nodes;
  }

  function renderEmpty(svg, message) {
    const circle = svgElement("circle", {
      cx: CENTER,
      cy: CENTER,
      r: RADIUS,
      fill: "#eef0eb",
      stroke: "#2f4348",
      "stroke-width": 3,
      "stroke-dasharray": "8 9",
    });
    const label = svgElement("text", {
      x: CENTER,
      y: CENTER,
      class: "wheel-empty-label",
      "dominant-baseline": "middle",
    });
    label.textContent = message;
    svg.replaceChildren(circle, label);
  }

  function signatureFor(options) {
    return (Array.isArray(options) ? options : [])
      .map((option) => `${optionId(option)}:${optionAmount(option)}:${optionName(option)}`)
      .join("|");
  }

  function render(svg, options) {
    if (!(svg instanceof SVGElement)) return { sectors: [], totalKopecks: 0 };

    const signature = signatureFor(options);
    const existing = wheelStates.get(svg);
    if (existing?.signature === signature) {
      return existing.geometry;
    }

    if (existing?.animation) existing.animation.cancel();
    const geometry = calculateSectors(options);
    if (geometry.totalKopecks <= 0) {
      renderEmpty(svg, "Ставок пока нет");
      wheelStates.set(svg, {
        signature,
        geometry,
        rotor: null,
        rotation: 0,
        resultKey: null,
        animation: null,
      });
      return geometry;
    }

    const rotor = svgElement("g", { class: "wheel-rotor" });
    rotor.style.transformBox = "view-box";
    rotor.style.transformOrigin = `${CENTER}px ${CENTER}px`;
    rotor.style.transform = "rotate(0deg)";

    geometry.sectors.forEach((sector, index) => {
      rotor.append(...buildSector(sector, index));
    });

    const hub = svgElement("circle", {
      cx: CENTER,
      cy: CENTER,
      r: 24,
      class: "wheel-hub",
    });
    const hubMark = svgElement("text", {
      x: CENTER,
      y: CENTER + 1,
      class: "wheel-label",
      "dominant-baseline": "middle",
      "font-size": 18,
    });
    hubMark.textContent = "✿";
    rotor.append(hub, hubMark);
    svg.replaceChildren(rotor);
    wheelStates.set(svg, {
      signature,
      geometry,
      rotor,
      rotation: 0,
      resultKey: null,
      animation: null,
    });
    return geometry;
  }

  function firstValue(object, keys) {
    for (const key of keys) {
      if (object && object[key] !== undefined && object[key] !== null) return object[key];
    }
    return undefined;
  }

  function resultBody(result) {
    return result?.result && typeof result.result === "object" ? result.result : result;
  }

  function winnerId(result) {
    const body = resultBody(result) || {};
    const winner = firstValue(body, ["winner", "winnerOption", "winner_option"]);
    return String(
      firstValue(body, [
        "winnerOptionId",
        "winner_option_id",
        "winnerId",
        "winner_id",
        "optionId",
        "option_id",
      ]) ?? firstValue(winner, ["id", "optionId", "option_id"]) ?? "",
    );
  }

  function drawPosition(result) {
    const body = resultBody(result) || {};
    const draw = firstValue(body, ["draw", "selection", "verification"]);
    const candidate = firstValue(body, [
      "selectionKopecks",
      "selection_kopecks",
      "selectedOffset",
      "selected_offset",
      "winningTicket",
      "winning_ticket",
      "drawPosition",
      "draw_position",
      "drawValue",
      "draw_value",
      "selectedOffset",
      "selected_offset",
      "selectedIndex",
      "selected_index",
    ]) ?? firstValue(draw, [
      "selectionKopecks",
      "selection_kopecks",
      "selectedOffset",
      "selected_offset",
      "winningTicket",
      "winning_ticket",
      "position",
      "index",
      "drawValue",
      "draw_value",
      "selectedOffset",
      "selected_offset",
    ]);
    const number = Number(candidate);
    return Number.isSafeInteger(number) && number >= 0 ? number : null;
  }

  function selectedAngle(result, geometry) {
    const winningId = winnerId(result);
    const winningSector = geometry.sectors.find((sector) => sector.id === winningId);
    if (!winningSector) return null;

    const position = drawPosition(result);
    if (position !== null && position < geometry.totalKopecks) {
      const angle = ((position + 0.5) / geometry.totalKopecks) * 360;
      if (angle >= winningSector.startAngle && angle <= winningSector.endAngle) return angle;
    }
    return winningSector.startAngle + winningSector.sweepAngle / 2;
  }

  function resultKey(result) {
    const body = resultBody(result) || {};
    return [
      firstValue(body, ["id", "resultId", "result_id"]) ?? "result",
      winnerId(body),
      firstValue(body, ["snapshotHash", "snapshot_hash"]) ?? "",
      firstValue(body, ["seed", "seedReveal", "seed_reveal"]) ?? "",
    ].join(":");
  }

  function animateToResult(svg, result, options = {}) {
    if (!(svg instanceof SVGElement) || !result) return false;
    let state = wheelStates.get(svg);
    if (!state) {
      render(svg, options.options || []);
      state = wheelStates.get(svg);
    }
    if (!state?.rotor || state.geometry.totalKopecks <= 0) return false;

    const key = resultKey(result);
    if (state.resultKey === key) return true;
    const angle = selectedAngle(result, state.geometry);
    if (angle === null) return false;

    if (state.animation) state.animation.cancel();
    state.resultKey = key;
    const reducedMotion = options.reducedMotion ?? window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const current = state.rotation;
    const currentNormalized = ((current % 360) + 360) % 360;
    const desired = ((-angle % 360) + 360) % 360;
    const delta = (desired - currentNormalized + 360) % 360;
    const target = current + delta + (reducedMotion ? 0 : 5 * 360);
    state.rotation = target;

    if (reducedMotion || typeof state.rotor.animate !== "function") {
      state.rotor.style.transform = `rotate(${target}deg)`;
      return true;
    }

    const animation = state.rotor.animate(
      [
        { transform: `rotate(${current}deg)` },
        { transform: `rotate(${target}deg)` },
      ],
      {
        duration: 5200,
        easing: "cubic-bezier(0.12, 0.72, 0.12, 1)",
        fill: "forwards",
      },
    );
    state.animation = animation;
    animation.addEventListener("finish", () => {
      state.rotor.style.transform = `rotate(${target}deg)`;
      state.animation = null;
    }, { once: true });
    animation.addEventListener("cancel", () => {
      state.animation = null;
    }, { once: true });
    return true;
  }

  function clear(svg) {
    if (!(svg instanceof SVGElement)) return;
    const state = wheelStates.get(svg);
    if (state?.animation) state.animation.cancel();
    svg.replaceChildren();
    wheelStates.delete(svg);
  }

  window.AuctionWheel = Object.freeze({
    animateToResult,
    calculateSectors,
    clear,
    render,
    winnerId,
  });
})();
