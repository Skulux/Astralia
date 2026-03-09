const STREAM_UPTIME_ENDPOINT = "/api/live/uptime/";
const STREAM_TITLE_ENDPOINT = "/api/live/title/";
const STREAM_GAME_ENDPOINT = "/api/live/game/";
const statusCache = new Map();
const titleCache = new Map();
const gameCache = new Map();

const isOfflineTitle = (title) => {
  if (typeof title !== "string") {
    return false;
  }

  return title.trim().toLowerCase().includes("aktuell offline");
};

const normalizeStatusResponse = (rawText) => {
  if (typeof rawText !== "string") {
    return { state: "error", message: "Status derzeit nicht verfügbar." };
  }

  const text = rawText.trim();
  if (!text) {
    return { state: "error", message: "Status derzeit nicht verfügbar." };
  }

  const normalized = text.toLowerCase();

  if (
    normalized.includes("could not find") ||
    normalized.includes("invalid channel") ||
    normalized.includes("no user with the name")
  ) {
    return { state: "error", message: "Kanal nicht gefunden." };
  }

  if (normalized.includes("too many requests")) {
    return { state: "error", message: "Status derzeit nicht verfügbar." };
  }

  if (normalized.includes("is offline")) {
    return { state: "offline", message: "Offline" };
  }

  if (text === "Stream has not started") {
    return { state: "offline", message: "Offline" };
  }

  // Any other response from the uptime endpoint indicates the channel is live and the
  // returned text represents the uptime.
  return {
    state: "live",
    message: "Live",
    detail: text,
  };
};

const fetchStreamerStatus = async (slug) => {
  const normalizedSlug = slug.trim().toLowerCase();
  if (!normalizedSlug) {
    return { state: "error", message: "Kein Kanal angegeben." };
  }

  if (statusCache.has(normalizedSlug)) {
    return statusCache.get(normalizedSlug);
  }

  const controller = new AbortController();
  const timeoutId = window.setTimeout(() => controller.abort(), 6000);

  const request = fetch(`${STREAM_UPTIME_ENDPOINT}${encodeURIComponent(normalizedSlug)}`, {
    headers: { Accept: "text/plain" },
    signal: controller.signal,
  })
    .then(async (response) => {
      if (!response.ok) {
        throw new Error(`HTTP ${response.status}`);
      }
      return response.text();
    })
    .then((text) => normalizeStatusResponse(text))
    .catch((error) => {
      console.warn(`Unable to load live status for ${normalizedSlug}:`, error);
      return { state: "error", message: "Status derzeit nicht verfügbar." };
    })
    .finally(() => {
      window.clearTimeout(timeoutId);
    });

  statusCache.set(normalizedSlug, request);
  return request;
};

const fetchStreamTitle = async (slug) => {
  const normalizedSlug = slug.trim().toLowerCase();
  if (!normalizedSlug) {
    return "";
  }

  if (titleCache.has(normalizedSlug)) {
    return titleCache.get(normalizedSlug);
  }

  const controller = new AbortController();
  const timeoutId = window.setTimeout(() => controller.abort(), 6000);

  const request = fetch(`${STREAM_TITLE_ENDPOINT}${encodeURIComponent(normalizedSlug)}`, {
    headers: { Accept: "text/plain" },
    signal: controller.signal,
  })
    .then(async (response) => {
      if (!response.ok) {
        throw new Error(`HTTP ${response.status}`);
      }
      return response.text();
    })
    .then((text) => text.trim())
    .catch((error) => {
      console.warn(`Unable to load stream title for ${normalizedSlug}:`, error);
      return "";
    })
    .finally(() => {
      window.clearTimeout(timeoutId);
    });

  titleCache.set(normalizedSlug, request);
  return request;
};

const fetchStreamGame = async (slug) => {
  const normalizedSlug = slug.trim().toLowerCase();
  if (!normalizedSlug) {
    return "";
  }

  if (gameCache.has(normalizedSlug)) {
    return gameCache.get(normalizedSlug);
  }

  const controller = new AbortController();
  const timeoutId = window.setTimeout(() => controller.abort(), 6000);

  const request = fetch(`${STREAM_GAME_ENDPOINT}${encodeURIComponent(normalizedSlug)}`, {
    headers: { Accept: "text/plain" },
    signal: controller.signal,
  })
    .then(async (response) => {
      if (!response.ok) {
        throw new Error(`HTTP ${response.status}`);
      }
      return response.text();
    })
    .then((text) => text.trim())
    .catch((error) => {
      console.warn(`Unable to load stream game for ${normalizedSlug}:`, error);
      return "";
    })
    .finally(() => {
      window.clearTimeout(timeoutId);
    });

  gameCache.set(normalizedSlug, request);
  return request;
};

const escapeForSelector = (value) => {
  if (window.CSS && typeof window.CSS.escape === "function") {
    return window.CSS.escape(value);
  }

  return value.replace(/[^a-zA-Z0-9_-]/g, "\\$&");
};

const findEmbedContainer = (slug) => {
  const escapedSlug = escapeForSelector(slug);
  return document.querySelector(
    `[data-live-embed][data-streamer-slug="${escapedSlug}"]`
  );
};

const getEmbedFrame = (container) =>
  container.querySelector("[data-live-iframe]");

const buildEmbedSrc = (slug) => {
  const parentHost = window.location.hostname || "localhost";
  const params = new URLSearchParams({
    channel: slug,
    parent: parentHost,
    muted: "true",
  });

  return `https://player.twitch.tv/?${params.toString()}`;
};

const toggleLiveEmbed = (slug, isLive) => {
  const container = findEmbedContainer(slug);
  if (!container) {
    return;
  }

  const frame = getEmbedFrame(container);

  if (!isLive) {
    if (frame && container.dataset.embedLoaded) {
      frame.removeAttribute("src");
      delete container.dataset.embedLoaded;
    }

    container.hidden = true;
    return;
  }

  if (!frame) {
    console.warn("Missing Twitch embed frame for", slug);
    return;
  }

  if (!container.dataset.embedLoaded) {
    frame.src = buildEmbedSrc(slug);
    frame.setAttribute("title", `Twitch Livestream von ${slug}`);
    container.dataset.embedLoaded = "true";
  }

  container.hidden = false;
};

const updateLiveScroller = (grid, visibleCards) => {
  if (!grid) {
    return;
  }

  const viewport = grid.querySelector("[data-live-viewport]");
  const track = grid.querySelector("[data-live-track]");
  if (!viewport || !track) {
    return;
  }

  const cards = Array.isArray(visibleCards)
    ? visibleCards
    : Array.from(track.querySelectorAll("[data-live-card]")).filter(
        (card) => !card.hidden
      );

  const hasVisible = cards.length > 0;
  const maxScroll = Math.max(
    viewport.scrollWidth - viewport.clientWidth,
    0
  );
  const atStart = viewport.scrollLeft <= 4;
  const atEnd = viewport.scrollLeft >= maxScroll - 4;
  const scrollable = hasVisible && maxScroll > 4;

  const toggleButton = (selector, shouldShow, isDisabled) => {
    const button = grid.querySelector(selector);
    if (!button) {
      return;
    }

    button.hidden = !shouldShow;
    button.disabled = Boolean(isDisabled);
    button.setAttribute("aria-disabled", isDisabled ? "true" : "false");
  };

  toggleButton('[data-live-nav="prev"]', scrollable, atStart);
  toggleButton('[data-live-nav="next"]', scrollable, atEnd);

  grid.classList.toggle("is-scrollable", scrollable);
};

const updateLiveGridState = (grid) => {
  if (!grid) {
    return;
  }

  const emptyNotice = grid.querySelector("[data-live-empty]");
  const cards = Array.from(grid.querySelectorAll("[data-live-card]"));
  const visibleCards = cards.filter((card) => !card.hidden);
  const liveSection = grid.closest("[data-live-section]");

  if (liveSection) {
    liveSection.hidden = visibleCards.length === 0;
  }

  if (emptyNotice) {
    if (visibleCards.length === 0) {
      const fallbackText = emptyNotice.dataset.emptyText;
      if (fallbackText) {
        emptyNotice.textContent = fallbackText;
      }
      emptyNotice.hidden = false;
    } else {
      emptyNotice.hidden = true;
    }
  }

  updateLiveScroller(grid, visibleCards);
};

const removeLiveCard = (card) => {
  if (!card) {
    return;
  }

  const grid = card.closest("[data-live-grid]");

  if (card.parentElement) {
    card.parentElement.removeChild(card);
  }

  if (grid) {
    updateLiveGridState(grid);
  }
};

const finaliseLiveGrid = (grid) => {
  updateLiveGridState(grid);
};

const updateLiveCard = (card, status) => {
  const grid = card.closest("[data-live-grid]");
  const slug = card.getAttribute("data-streamer-slug") || "";
  const titleElement = card.querySelector("[data-live-title]");
  const defaultTitle = titleElement?.dataset.defaultText || "Live auf Twitch";
  const gameElement = card.querySelector("[data-live-game]");

  card.classList.remove("is-loading", "is-live", "is-offline", "is-error");

  if (status.state === "live") {
    card.hidden = false;
    card.classList.add("is-live");

    if (titleElement) {
      titleElement.textContent = defaultTitle;
      fetchStreamTitle(slug).then((title) => {
        if (!card.isConnected) {
          return;
        }
        if ((card.getAttribute("data-streamer-slug") || "") !== slug) {
          return;
        }
        if (isOfflineTitle(title)) {
          removeLiveCard(card);
          return;
        }

        titleElement.textContent = title || defaultTitle;
      });
    }

    if (gameElement) {
      gameElement.textContent = "";
      gameElement.hidden = true;
      fetchStreamGame(slug).then((game) => {
        if (!card.isConnected) {
          return;
        }
        if ((card.getAttribute("data-streamer-slug") || "") !== slug) {
          return;
        }
        if (card.hidden || card.classList.contains("is-offline")) {
          return;
        }
        if (game) {
          gameElement.textContent = game;
          gameElement.hidden = false;
        } else {
          gameElement.textContent = "";
          gameElement.hidden = true;
        }
      });
    }

    updateLiveGridState(grid);
    return;
  }

  removeLiveCard(card);
};

const updateStatusElement = (element, status) => {
  const indicator = element.querySelector("[data-live-status-indicator]");
  const textNode = element.querySelector("[data-live-status-text]");

  element.classList.remove("is-loading", "is-live", "is-offline", "is-error");

  const stateClass =
    status.state === "live"
      ? "is-live"
      : status.state === "offline"
      ? "is-offline"
      : "is-error";

  element.classList.add(stateClass);

  if (textNode) {
    textNode.textContent = status.message;
  }

  if (status.detail) {
    element.setAttribute("title", status.detail);
  } else {
    element.removeAttribute("title");
  }

  if (indicator) {
    indicator.setAttribute("aria-hidden", "true");
  }
};

const initialiseLiveStatus = () => {
  const statusElements = document.querySelectorAll("[data-live-status]");
  if (!statusElements.length) {
    return;
  }

  statusElements.forEach((element) => {
    const slug = element.getAttribute("data-streamer-slug");
    if (!slug) {
      updateStatusElement(element, { state: "error", message: "Kein Kanal angegeben." });
      return;
    }

    const normalizedSlug = slug.trim().toLowerCase();

    element.setAttribute("data-streamer-slug", normalizedSlug);

    fetchStreamerStatus(normalizedSlug).then((status) => {
      updateStatusElement(element, status);
      toggleLiveEmbed(normalizedSlug, status.state === "live");
    });
  });
};

const initialiseLiveCards = () => {
  const grids = new Set(
    Array.from(document.querySelectorAll("[data-live-grid]"))
  );
  const cards = Array.from(document.querySelectorAll("[data-live-card]"));

  grids.forEach((grid) => {
    if (grid.dataset.liveNavReady === "true") {
      return;
    }

    const viewport = grid.querySelector("[data-live-viewport]");
    if (viewport) {
      viewport.addEventListener("scroll", () => updateLiveScroller(grid));

      if ("ResizeObserver" in window) {
        const observer = new ResizeObserver(() => updateLiveScroller(grid));
        observer.observe(viewport);
        grid._liveResizeObserver = observer;
      }
    }

    grid.querySelectorAll("[data-live-nav]").forEach((button) => {
      button.addEventListener("click", () => {
        if (!viewport) {
          return;
        }

        const direction = button.getAttribute("data-live-nav");
        const distance = viewport.clientWidth * 0.85 || 240;
        const offset = direction === "next" ? distance : -distance;

        viewport.scrollBy({ left: offset, behavior: "smooth" });
      });
    });

    grid.dataset.liveNavReady = "true";
    updateLiveGridState(grid);
  });

  if (!cards.length) {
    grids.forEach((grid) => finaliseLiveGrid(grid));
    return;
  }

  let pending = cards.length;

  cards.forEach((card) => {
    const slug = card.getAttribute("data-streamer-slug");
    if (!slug) {
      updateLiveCard(card, { state: "error", message: "Kein Kanal angegeben." });
      pending -= 1;
      if (pending === 0) {
        grids.forEach((grid) => finaliseLiveGrid(grid));
      }
      return;
    }

    const normalizedSlug = slug.trim().toLowerCase();
    card.setAttribute("data-streamer-slug", normalizedSlug);
    card.hidden = true;
    card.classList.add("is-loading");

    fetchStreamerStatus(normalizedSlug)
      .then((status) => {
        updateLiveCard(card, status);
      })
      .catch((error) => {
        console.warn(`Unable to resolve live card for ${normalizedSlug}:`, error);
        updateLiveCard(card, {
          state: "error",
          message: "Status derzeit nicht verfügbar.",
        });
      })
      .finally(() => {
        pending -= 1;
        if (pending === 0) {
          grids.forEach((grid) => finaliseLiveGrid(grid));
        }
      });
  });
};

const initialiseLiveFeatures = () => {
  initialiseLiveStatus();
  initialiseLiveCards();
};

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", initialiseLiveFeatures);
} else {
  initialiseLiveFeatures();
}
