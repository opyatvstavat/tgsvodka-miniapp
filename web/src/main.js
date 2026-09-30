import "./style.css";

const tg = window.Telegram?.WebApp;
const app = document.getElementById("app");
const PAGE_SIZE = 8;
const PREVIEW_AHEAD = 3;
const SNAP_MS = 380;
const GRADIENTS = 6;

const feed = [];
const seenInSession = new Set();
const warmedMedia = new Set();
// Only the previous, current and next posts live in the DOM.
const slideNodes = new Map();
let stage = null;
let currentIndex = 0;
let nextCursor = null;
let hasMore = true;
let pageRequest = null;
let liking = false;
let animating = false;
let pointerStart = null;
let lastTap = null;
let tapTimer = null;
const commentStates = new Map();
const restoredContent = new Set();
const restoreQueue = [];
let restoringContent = false;
let lottiePlayer = null;
// Who is reading: channel count, limit, whether comments may be sent.
let me = null;
// The post on screen and since when; its dwell time is sent when it leaves.
let viewing = null;
let channelsPoll = null;

const ICONS = {
  heart: `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 21.35l-1.45-1.32C5.4 15.36 2 12.28 2 8.5 2 5.42 4.42 3 7.5 3c1.74 0 3.41.81 4.5 2.09C13.09 3.81 14.76 3 16.5 3 19.58 3 22 5.42 22 8.5c0 3.78-3.4 6.86-8.55 11.54L12 21.35z"/></svg>`,
  comment: `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3C6.5 3 2 6.8 2 11.5c0 2.4 1.2 4.6 3.1 6.1-.2 1.3-.8 2.6-1.8 3.6 2.1-.1 3.9-.9 5.2-1.9 1.1.3 2.3.5 3.5.5 5.5 0 10-3.8 10-8.5S17.5 3 12 3z"/></svg>`,
  share: `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M14 8.5V4l8 7.5-8 7.5v-4.6c-5.4 0-9 1.7-11.5 5.6 1-5.6 4-10.4 11.5-11.5z"/></svg>`,
  play: `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 5.14v13.72a1 1 0 0 0 1.52.85l11-6.86a1 1 0 0 0 0-1.7l-11-6.86A1 1 0 0 0 8 5.14z"/></svg>`,
  channels: `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 6h11v2H4zm0 5h11v2H4zm0 5h7v2H4zm13-3h2v3h3v2h-3v3h-2v-3h-3v-2h3z"/></svg>`,
  muted: `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M16.5 12c0-1.77-1.02-3.29-2.5-4.03v2.21l2.45 2.45c.03-.2.05-.41.05-.63zm2.5 0c0 .94-.2 1.82-.54 2.64l1.51 1.51C20.63 14.91 21 13.5 21 12c0-4.28-2.99-7.86-7-8.77v2.06c2.89.86 5 3.54 5 6.71zM4.27 3L3 4.27 7.73 9H3v6h4l5 5v-6.73l4.25 4.25c-.67.52-1.42.93-2.25 1.18v2.06c1.38-.31 2.63-.95 3.69-1.81L19.73 21 21 19.73l-9-9L4.27 3zM12 4L9.91 6.09 12 8.18V4z"/></svg>`,
};

function applyViewportHeight() {
  const height = window.visualViewport?.height || window.innerHeight;
  document.documentElement.style.setProperty("--viewport-height", `${height}px`);
  slideNodes.forEach(refreshClamp);
}

applyViewportHeight();
window.visualViewport?.addEventListener("resize", applyViewportHeight);

if (tg) {
  tg.ready();
  tg.expand();
  tg.setHeaderColor?.("#000000");
  tg.setBackgroundColor?.("#000000");
  tg.disableVerticalSwipes?.();
  // Older Telegram clients return no Promise here. Calling `.catch` on that
  // value aborts the entire Mini App before it can render.
  try {
    const fullscreenRequest = tg.requestFullscreen?.();
    if (fullscreenRequest?.catch) fullscreenRequest.catch(() => {});
  } catch (_) {
    // Fullscreen is optional; the feed works in the regular Mini App viewport.
  }

  const applyInsets = () => {
    // In fullscreen the content inset (Telegram's Close and menu buttons) is
    // measured from the device safe area, so the two add up.
    const safe = tg.safeAreaInset || {};
    const content = tg.contentSafeAreaInset || {};
    document.documentElement.style.setProperty("--top-inset", `${(safe.top || 0) + (content.top || 0)}px`);
    document.documentElement.style.setProperty("--bottom-inset", `${(safe.bottom || 0) + (content.bottom || 0)}px`);
  };
  applyInsets();
  tg.onEvent?.("safeAreaChanged", applyInsets);
  tg.onEvent?.("contentSafeAreaChanged", applyInsets);
  tg.onEvent?.("viewportChanged", applyViewportHeight);
}

function authHeader() {
  return tg?.initData ? { Authorization: `tma ${tg.initData}` } : {};
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      "ngrok-skip-browser-warning": "1",
      ...authHeader(),
      ...(options.headers || {}),
    },
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || `Request failed: ${response.status}`);
  }
  return response.json();
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function mediaUrl(postId, index, preview = false) {
  const params = new URLSearchParams({ init_data: tg?.initData || "" });
  if (preview) params.set("preview", "1");
  return `/api/posts/${postId}/media/${index}?${params}`;
}

function avatarUrl(postId) {
  return `/api/posts/${postId}/avatar?${new URLSearchParams({ init_data: tg?.initData || "" })}`;
}

function customEmojiUrl(id) {
  return `/api/custom-emoji/${id}?${new URLSearchParams({ init_data: tg?.initData || "" })}`;
}

function userAvatarUrl(userId) {
  return `/api/users/${userId}/avatar?${new URLSearchParams({ init_data: tg?.initData || "" })}`;
}

function commentMediaUrl(postId, commentId, preview = false) {
  const params = new URLSearchParams({ init_data: tg?.initData || "" });
  if (preview) params.set("preview", "1");
  return `/api/posts/${postId}/comments/${commentId}/media?${params}`;
}

function postSource(post) {
  return post.source_link || (post.channel_username ? `https://t.me/${post.channel_username}` : "");
}

function hashIndex(value, size) {
  let hash = 0;
  for (const char of String(value)) hash = (hash * 31 + char.charCodeAt(0)) | 0;
  return Math.abs(hash) % size;
}

function formatCount(value) {
  if (!value) return "";
  if (value < 1000) return String(value);
  if (value < 1_000_000) return `${(value / 1000).toFixed(value < 10_000 ? 1 : 0).replace(".0", "")}K`;
  return `${(value / 1_000_000).toFixed(1).replace(".0", "")}M`;
}

function setCommentsCount(post, count) {
  post.comments_count = count;
  const button = slideNodes.get(post.id)?.querySelector(".rail-comments");
  if (!button) return;
  button.classList.toggle("empty", !count);
  button.querySelector(".rail-count").textContent = formatCount(count);
}

function setReactions(post, reactions) {
  post.reactions = reactions;
  const button = slideNodes.get(post.id)?.querySelector(".rail-like");
  if (!button) return;
  button.classList.toggle("empty", !reactions?.total);
  button.querySelector(".rail-count").textContent = formatCount(reactions?.total);
}

function setForwards(post, count) {
  post.forwards = count;
  const button = slideNodes.get(post.id)?.querySelector(".rail-share");
  if (!button) return;
  button.classList.toggle("empty", !count);
  button.querySelector(".rail-count").textContent = formatCount(count);
}

function isLongText(post) {
  return String(post.text || "").replace(/<[^>]*>/g, "").length > 420;
}

function warmPreview(post) {
  (post.media_items || []).forEach((item, index) => {
    if (item.type === "video" || item.type === "voice" || item.type === "audio") return;
    const key = `${post.id}:${index}`;
    if (warmedMedia.has(key)) return;
    warmedMedia.add(key);
    fetch(mediaUrl(post.id, index, true), { headers: authHeader() }).catch(() => warmedMedia.delete(key));
  });
}

function loadPage() {
  if (!hasMore) return Promise.resolve();
  // A swipe past the end joins the prefetch that is already running instead
  // of giving up because a request is in flight.
  pageRequest ??= fetchPage().finally(() => { pageRequest = null; });
  return pageRequest;
}

async function fetchPage() {
  const query = new URLSearchParams({ limit: String(PAGE_SIZE) });
  if (nextCursor !== null) query.set("before", String(nextCursor));
  const page = await api(`/api/queue?${query}`);
  const known = new Set(feed.map((post) => post.id));
  const fresh = (page.items || []).filter((post) => !known.has(post.id));
  feed.push(...fresh);
  nextCursor = page.next_cursor;
  hasMore = Boolean(page.next_cursor && fresh.length === PAGE_SIZE);
  for (const post of fresh.slice(0, PREVIEW_AHEAD)) warmPreview(post);
}

function formatDuration(seconds) {
  const total = Math.round(seconds || 0);
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
}

function formatSize(bytes) {
  if (!bytes) return "";
  const units = ["Б", "КБ", "МБ", "ГБ"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(unit && value < 10 ? 1 : 0)} ${units[unit]}`;
}

function pauseMedia(node) {
  node?.querySelectorAll("video, audio").forEach((media) => media.pause());
}

function disposeMedia(node) {
  node?.querySelectorAll("video, audio").forEach((media) => {
    media.pause();
    media.removeAttribute("src");
    media.load();
  });
}

function renderMedia(post) {
  const items = post.media_items || [];
  if (!items.length) return "";
  return `<div class="media-carousel" aria-label="Медиа поста">${items.map((item, index) => {
    const full = mediaUrl(post.id, index);
    if (item.type === "video") {
      return `<video class="media media-video" src="${full}" poster="${mediaUrl(post.id, index, true)}" loop playsinline preload="metadata"></video>`;
    }
    if (item.type === "voice" || item.type === "audio") {
      return `<div class="media media-audio"><div class="audio-disc">♪</div><span>Аудио</span><audio src="${full}" preload="none"></audio></div>`;
    }
    return `<img class="media media-photo" src="${mediaUrl(post.id, index, true)}" data-full="${full}" alt="" draggable="false" />`;
  }).join("")}</div>`;
}

function renderEmbeds(embeds) {
  return (embeds || []).map((embed) => {
    if (embed.type !== "youtube" || !embed.video_id) return "";
    const src = `https://www.youtube-nocookie.com/embed/${encodeURIComponent(embed.video_id)}`;
    return `<div class="yt-embed"><iframe src="${src}" title="YouTube" loading="lazy" allow="autoplay; encrypted-media; picture-in-picture" allowfullscreen></iframe></div>`;
  }).join("");
}

function renderEmpty() {
  slideNodes.forEach(disposeMedia);
  slideNodes.clear();
  stage = null;
  const waiting = me && me.channels_count > 0;
  app.innerHTML = `<main class="empty-state"><div class="empty-icon">✓</div><h1>${waiting ? "Лента просмотрена" : "Пока пусто"}</h1><p>${waiting ? "Новые посты из твоих каналов появятся здесь. Только что добавленные каналы подключаются несколько минут." : "Добавь каналы, чтобы посты появились в ленте."}</p><button class="primary-btn" type="button">Мои каналы</button></main>`;
  app.querySelector(".primary-btn").onclick = () => openChannels();
}

function slideClass(post) {
  const items = post.media_items || [];
  const hasMedia = items.length || renderEmbeds(post.embeds);
  const kind = hasMedia ? "has-media" : `text-only gradient-${hashIndex(post.id, GRADIENTS)}`;
  return `slide ${kind}${items.some((item) => item.type === "video") ? " has-video" : ""}`;
}

function slideMarkup(post) {
  const title = post.channel_title || post.channel_username || "Канал";
  const source = postSource(post);
  const channel = post.channel_label_html || escapeHtml(title);
  const channelMarkup = source ? `<a href="${escapeHtml(source)}" target="_blank" rel="noopener">${channel}</a>` : channel;
  const initial = escapeHtml((title.trim()[0] || "T").toUpperCase());
  const items = post.media_items || [];
  const embeds = renderEmbeds(post.embeds);
  const avatarInner = `<span>${initial}</span><img src="${avatarUrl(post.id)}" alt="" draggable="false" />`;
  const avatar = source
    ? `<a class="rail-avatar" href="${escapeHtml(source)}" target="_blank" rel="noopener" aria-label="Открыть канал">${avatarInner}</a>`
    : `<span class="rail-avatar">${avatarInner}</span>`;
  const rail = `<div class="slide-rail">${avatar}
    <button class="rail-btn rail-like${post.liked ? " liked" : ""}${post.reactions?.total ? "" : " empty"}" type="button" aria-label="Нравится">${ICONS.heart}<span class="rail-count">${formatCount(post.reactions?.total)}</span></button>
    <button class="rail-btn rail-comments${post.comments_count ? "" : " empty"}" type="button" aria-label="Комментарии">${ICONS.comment}<span class="rail-count">${formatCount(post.comments_count)}</span></button>
    ${source ? `<button class="rail-btn rail-share${post.forwards ? "" : " empty"}" type="button" aria-label="Поделиться">${ICONS.share}<span class="rail-count">${formatCount(post.forwards)}</span></button>` : ""}
  </div>`;

  if (!items.length && !embeds) {
    const text = post.text ? `<div class="post-text${isLongText(post) ? " long" : ""}">${post.text}</div>` : "";
    return `<div class="slide-content">${text}</div>
      <div class="slide-info"><div class="slide-channel">${channelMarkup}</div></div>
      ${rail}`;
  }

  const bgIndex = items.findIndex((item) => item.type !== "voice" && item.type !== "audio");
  const background = bgIndex >= 0
    ? `<div class="slide-bg" style="background-image:url('${escapeHtml(mediaUrl(post.id, bgIndex, true))}')"></div>`
    : "";
  const dots = items.length > 1
    ? `<div class="media-dots">${items.map((_, index) => `<span${index ? "" : ` class="active"`}></span>`).join("")}</div>`
    : "";
  const caption = post.text
    ? `<div class="post-text">${post.text}</div><button class="caption-more" type="button">ещё</button>`
    : "";
  return `${background}<div class="slide-media">${renderMedia(post)}${embeds}</div>${dots}
    <div class="slide-info"><div class="slide-channel">${channelMarkup}</div>${caption}</div>
    ${rail}
    <div class="play-indicator">${ICONS.play}</div>
    <div class="speed-badge">2x ▸▸</div>
    <button class="unmute-hint" type="button">${ICONS.muted}<span>Включить звук</span></button>
    <div class="video-progress"><span></span></div>`;
}

function createSlide(post) {
  const node = document.createElement("article");
  node.className = slideClass(post);
  node.dataset.postId = post.id;
  node.dataset.mediaIndex = "0";
  node.innerHTML = slideMarkup(post);

  node.querySelectorAll(".rail-avatar img").forEach((image) => { image.onerror = () => image.remove(); });
  bindLikeButton(node.querySelector(".rail-like"), post);
  node.querySelector(".rail-comments").onclick = () => openComments(post);
  const share = node.querySelector(".rail-share");
  if (share) share.onclick = () => sharePost(post);
  node.querySelector(".caption-more")?.addEventListener("click", () => toggleExpand(node));
  node.querySelector(".slide-info .post-text")?.addEventListener("click", (event) => {
    if (event.target.closest("a") || node.classList.contains("expanded") || !node.classList.contains("clampable")) return;
    toggleExpand(node, true);
  });
  node.querySelector(".unmute-hint")?.addEventListener("click", () => {
    const media = visibleMedia(node);
    if (media) media.muted = false;
    node.classList.remove("muted");
  });
  const carousel = node.querySelector(".media-carousel");
  carousel?.addEventListener("scroll", () => onCarouselScroll(node, carousel), { passive: true });
  const progress = node.querySelector(".video-progress span");
  node.querySelectorAll("video").forEach((video) => {
    video.addEventListener("timeupdate", () => {
      if (video.duration) progress.style.transform = `scaleX(${video.currentTime / video.duration})`;
    });
  });
  return node;
}

function upgradePhotos(node) {
  // The preview is a small Telegram thumbnail that shows instantly but looks
  // soft full-screen. Swap in the original once it has loaded, so the photo
  // never blanks while it downloads.
  node.querySelectorAll("img[data-full]").forEach((image) => {
    const full = image.dataset.full;
    delete image.dataset.full;
    const loader = new Image();
    loader.onload = () => { image.src = full; };
    loader.src = full;
  });
}

function refreshClamp(node) {
  const text = node.querySelector(".slide-info .post-text");
  if (!text) return;
  node.classList.toggle("clampable", node.classList.contains("expanded") || text.scrollHeight > text.clientHeight + 2);
}

function toggleExpand(node, expanded = !node.classList.contains("expanded")) {
  node.classList.toggle("expanded", expanded);
  const more = node.querySelector(".caption-more");
  if (more) more.textContent = expanded ? "свернуть" : "ещё";
  if (!expanded) {
    const text = node.querySelector(".slide-info .post-text");
    if (text) text.scrollTop = 0;
  }
}

function carouselIndex(carousel) {
  return Math.round(carousel.scrollLeft / Math.max(1, carousel.clientWidth));
}

function visibleMedia(node) {
  const carousel = node?.querySelector(".media-carousel");
  const item = carousel?.children[carouselIndex(carousel)];
  if (!item) return null;
  return item.matches("video, audio") ? item : item.querySelector("video, audio");
}

function onCarouselScroll(node, carousel) {
  const index = String(carouselIndex(carousel));
  if (node.dataset.mediaIndex === index) return;
  node.dataset.mediaIndex = index;
  node.querySelectorAll(".media-dots span").forEach((dot, i) => dot.classList.toggle("active", String(i) === index));
  if (!node.classList.contains("is-active")) return;
  pauseMedia(node);
  playSlideMedia(node);
}

function playSlideMedia(node) {
  node?.classList.remove("paused");
  const media = visibleMedia(node);
  if (!media || !media.paused) return;
  node.classList.remove("muted");
  media.muted = false;
  // The swipe is a user gesture in Telegram, so playback normally starts with
  // sound. Before the first interaction the browser refuses sound: play
  // muted then, and offer a button to turn it on.
  media.play().catch((error) => {
    if (error.name !== "NotAllowedError" || media.tagName !== "VIDEO") return;
    media.muted = true;
    node.classList.add("muted");
    media.play().catch(() => {});
  });
}

function activeSlide() {
  const post = feed[currentIndex];
  return post ? slideNodes.get(post.id) : null;
}

function renderShell() {
  app.innerHTML = `
    <main class="feed-shell">
      <div class="feed-stage"></div>
      <header class="feed-header"><span class="feed-brand">TGSvodka</span><span class="feed-header-end"><span class="feed-position"></span><button class="header-btn" type="button" aria-label="Мои каналы">${ICONS.channels}</button></span></header>
    </main>`;
  stage = app.querySelector(".feed-stage");
  app.querySelector(".header-btn").onclick = () => openChannels();
}

function updatePosition() {
  const label = app.querySelector(".feed-position");
  if (label) label.textContent = `${currentIndex + 1}${hasMore ? "+" : ` / ${feed.length}`}`;
}

function layoutSlides() {
  if (!stage) return;
  const wanted = new Map();
  for (const offset of [-1, 0, 1]) {
    const post = feed[currentIndex + offset];
    if (post) wanted.set(post.id, [offset, post]);
  }
  for (const [id, node] of slideNodes) {
    if (wanted.has(id)) continue;
    disposeMedia(node);
    node.remove();
    slideNodes.delete(id);
  }
  for (const [id, [offset, post]] of wanted) {
    let node = slideNodes.get(id);
    if (!node) {
      node = createSlide(post);
      slideNodes.set(id, node);
      stage.append(node);
      refreshClamp(node);
    }
    node.style.setProperty("--slot", offset);
    node.classList.toggle("is-active", offset === 0);
    node.inert = offset !== 0;
    upgradePhotos(node);
    if (offset !== 0 && node.classList.contains("expanded")) toggleExpand(node, false);
  }
  updatePosition();
}

function activate() {
  const post = feed[currentIndex];
  const node = activeSlide();
  if (!post || !node) return;
  for (const other of slideNodes.values()) {
    if (other === node) continue;
    pauseMedia(other);
    other.classList.remove("paused", "muted");
  }
  playSlideMedia(node);
  markSeen(post);
  restoreContent(post);
  if (feed[currentIndex + 1]) restoreContent(feed[currentIndex + 1]);
  for (const upcoming of feed.slice(currentIndex + 1, currentIndex + 1 + PREVIEW_AHEAD)) warmPreview(upcoming);
  if (hasMore && feed.length - currentIndex <= PREVIEW_AHEAD) {
    loadPage().then(() => { if (!animating) layoutSlides(); }).catch(() => {});
  }
}

function setDrag(px) {
  stage.style.setProperty("--drag", `${px}px`);
  stage.style.setProperty("--progress", Math.min(1, Math.abs(px) / (stage.clientHeight || 1)).toFixed(3));
}

function animateDrag(px) {
  return new Promise((resolve) => {
    const active = activeSlide();
    let done = false;
    const finish = () => {
      if (done) return;
      done = true;
      active?.removeEventListener("transitionend", onEnd);
      resolve();
    };
    const onEnd = (event) => {
      if (event.target === active && event.propertyName === "transform") finish();
    };
    active?.addEventListener("transitionend", onEnd);
    setTimeout(finish, SNAP_MS + 80);
    stage.classList.add("animating");
    setDrag(px);
  });
}

async function settle() {
  if (!stage) return;
  animating = true;
  await animateDrag(0);
  stage.classList.remove("animating");
  animating = false;
}

async function move(direction) {
  if (animating || !stage) return;
  const target = currentIndex + direction;
  if (target < 0) return settle();
  if (target >= feed.length) {
    animating = true;
    await loadPage().catch(() => {});
    animating = false;
    if (target >= feed.length) {
      showToast("Лента просмотрена");
      return settle();
    }
    layoutSlides();
  }
  animating = true;
  const incoming = slideNodes.get(feed[target].id);
  pauseMedia(activeSlide());
  // Start the next video while it slides in and while we are still inside
  // the swipe gesture, so the browser allows sound.
  playSlideMedia(incoming);
  await animateDrag(-direction * stage.clientHeight);
  currentIndex = target;
  // Removing the transition and shifting every slot by one lands each slide
  // exactly where the animation left it, so the swap is invisible.
  stage.classList.remove("animating");
  setDrag(0);
  layoutSlides();
  animating = false;
  activate();
}

// A post counts as read once it leaves the screen, with how long it stayed:
// that time is the first signal for recommendations.
function flushView(keepalive = false) {
  if (!viewing) return;
  const { post, since } = viewing;
  viewing = null;
  seenInSession.add(post.id);
  const dwell = Math.round(performance.now() - since);
  api(`/api/posts/${post.id}/seen`, { method: "POST", body: JSON.stringify({ dwell_ms: dwell }), keepalive }).catch(() => {});
}

function markSeen(post) {
  if (viewing?.post === post) return;
  flushView();
  viewing = { post, since: performance.now() };
}

document.addEventListener("visibilitychange", () => {
  if (document.hidden) flushView(true);
  else if (stage && feed[currentIndex] && !overlayOpen()) markSeen(feed[currentIndex]);
});

function restoreContent(post) {
  if (restoredContent.has(post.id)) return;
  restoredContent.add(post.id);
  restoreQueue.push(post);
  processContentRestoreQueue();
}

async function processContentRestoreQueue() {
  if (restoringContent) return;
  restoringContent = true;
  while (restoreQueue.length) {
    const post = restoreQueue.shift();
    try {
      const content = await api(`/api/posts/${post.id}/content`);
      const embeds = content.embeds || post.embeds;
      const embedsChanged = JSON.stringify(embeds || []) !== JSON.stringify(post.embeds || []);
      const textChanged = content.text !== post.text;
      post.text = content.text;
      post.embeds = embeds;
      if (content.comments_count != null) post.comments_count = content.comments_count;
      if (textChanged || embedsChanged) refreshSlide(post, embedsChanged);
      setCommentsCount(post, post.comments_count);
      if (content.reactions != null) setReactions(post, content.reactions);
      if (content.forwards != null) setForwards(post, content.forwards);
    } catch (_) {
      // A deleted or private original keeps its readable saved copy.
    }
  }
  restoringContent = false;
}

function refreshSlide(post, rebuild) {
  const node = slideNodes.get(post.id);
  if (!node) return;
  const text = node.querySelector(".post-text");
  if (!rebuild && text && post.text) {
    // Swapping only the text keeps a playing video and an open caption intact.
    text.innerHTML = post.text;
    if (node.classList.contains("text-only")) text.classList.toggle("long", isLongText(post));
    refreshClamp(node);
    return;
  }
  const fresh = createSlide(post);
  fresh.style.setProperty("--slot", node.style.getPropertyValue("--slot"));
  fresh.classList.toggle("is-active", node.classList.contains("is-active"));
  fresh.inert = node.inert;
  disposeMedia(node);
  node.replaceWith(fresh);
  slideNodes.set(post.id, fresh);
  refreshClamp(fresh);
  upgradePhotos(fresh);
  if (fresh.classList.contains("is-active") && !animating) playSlideMedia(fresh);
}

function commentState(post) {
  if (!commentStates.has(post.id)) {
    commentStates.set(post.id, { items: [], cursor: null, available: true, message: null, loading: false, sending: false, draft: "", replyTo: null, expanded: new Set() });
  }
  return commentStates.get(post.id);
}

function openSheet(post) {
  const sheet = document.querySelector(".comments-sheet:not(.closing)");
  return sheet && (!post || sheet.dataset.postId === String(post.id)) ? sheet : null;
}

function closeComments() {
  const sheet = openSheet();
  if (!sheet) return;
  const backdrop = document.querySelector(".comments-backdrop:not(.closing)");
  pauseMedia(sheet);
  sheet._lottieObserver?.disconnect();
  sheet.querySelectorAll(".comment-lottie").forEach((node) => node._lottie?.destroy());
  sheet.classList.add("closing");
  backdrop?.classList.add("closing");
  setTimeout(() => {
    sheet.remove();
    backdrop?.remove();
  }, 240);
}

function bindSheetDrag(panel) {
  let drag = null;
  panel.addEventListener("touchstart", (event) => {
    const target = event.target;
    if (!target.closest(".comments-grabber, .comments-header") || target.closest("button")) return;
    drag = { y: event.touches[0].clientY, dy: 0 };
    panel.classList.add("dragging");
  }, { passive: true });
  panel.addEventListener("touchmove", (event) => {
    if (!drag) return;
    drag.dy = Math.max(0, event.touches[0].clientY - drag.y);
    panel.style.transform = `translateY(${drag.dy}px)`;
  }, { passive: true });
  const end = () => {
    if (!drag) return;
    const { dy } = drag;
    drag = null;
    panel.classList.remove("dragging");
    panel.style.transform = "";
    if (dy > 90) closeComments();
  };
  panel.addEventListener("touchend", end);
  panel.addEventListener("touchcancel", end);
}

function formatCommentDate(value) {
  if (!value) return "";
  return new Intl.DateTimeFormat("ru", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }).format(new Date(value));
}

function pluralRu(count, one, few, many) {
  const tens = count % 100;
  const units = count % 10;
  if (tens >= 11 && tens <= 14) return many;
  if (units === 1) return one;
  if (units >= 2 && units <= 4) return few;
  return many;
}

// Telegram replies form chains; like TikTok, every chain is flattened under
// the top-level comment it started from.  Pages load oldest first, so a
// parent is always loaded before its replies.
function commentThreads(items) {
  const byId = new Map(items.map((comment) => [comment.id, comment]));
  // An album is one comment, but a reply may point at any of its files.
  for (const comment of items) {
    for (const media of comment.media || []) if (!byId.has(media.id)) byId.set(media.id, comment);
  }
  const rootOf = (comment) => {
    let current = comment;
    const seen = new Set();
    while (current.reply_to_id && byId.has(current.reply_to_id) && !seen.has(current.id)) {
      seen.add(current.id);
      current = byId.get(current.reply_to_id);
    }
    return current;
  };
  const threads = new Map();
  for (const comment of items) {
    const root = rootOf(comment);
    if (!threads.has(root.id)) threads.set(root.id, { root, replies: [] });
    if (root !== comment) threads.get(root.id).replies.push(comment);
  }
  for (const thread of threads.values()) thread.replies.sort((a, b) => a.id - b.id);
  return { byId, threads: items.filter((comment) => threads.has(comment.id)).map((comment) => threads.get(comment.id)) };
}

const VISUAL_MEDIA = new Set(["photo", "gif", "video"]);

function commentMediaItemHtml(post, media) {
  const full = commentMediaUrl(post.id, media.id);
  const preview = commentMediaUrl(post.id, media.id, true);
  const ratio = media.width && media.height ? `aspect-ratio:${media.width}/${media.height}` : "";
  const box = (kind, inner, { style = "", attrs = "" } = {}) =>
    `<div class="comment-media ${kind}" data-key="${post.id}:${media.id}" style="${style}" ${attrs}>${inner}</div>`;
  switch (media.type) {
    case "photo":
      return box("comment-photo", `<img src="${preview}" data-full="${full}" alt="" />`, { style: ratio });
    case "gif":
      return box("comment-gif", `<video src="${full}" poster="${preview}" autoplay loop muted playsinline></video>`, { style: ratio });
    case "video":
      return box("comment-video", `<video src="${full}" poster="${preview}" controls playsinline preload="none"></video>${media.duration ? `<span class="comment-media-badge">${formatDuration(media.duration)}</span>` : ""}`, { style: ratio });
    case "sticker":
      return box("comment-sticker", `<img src="${full}" alt="${escapeHtml(media.emoji || "")}" />`);
    case "sticker_video":
      return box("comment-sticker", `<video src="${full}" data-fallback="${preview}" autoplay loop muted playsinline></video>`);
    case "sticker_lottie":
      return box("comment-sticker comment-lottie", `<img src="${preview}" alt="${escapeHtml(media.emoji || "")}" />`, { attrs: `data-src="${full}"` });
    case "round":
      return box("comment-round", `<video src="${full}" poster="${preview}" playsinline preload="metadata"></video>`);
    case "voice":
    case "audio": {
      const title = media.type === "audio"
        ? [media.performer, media.title].filter(Boolean).join(" — ") || "Аудио"
        : "Голосовое сообщение";
      return box("comment-audio", `<span>${escapeHtml(title)}${media.duration ? ` · ${formatDuration(media.duration)}` : ""}</span><audio src="${full}" controls preload="none"></audio>`);
    }
    case "file":
      return box("comment-file", `<i><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8zm0 1.5L18.5 8H14z"/></svg></i><span><b>${escapeHtml(media.name || "Файл")}</b><small>${formatSize(media.size)}</small></span>`);
    default:
      return media.label ? `<div class="comment-media-label">${escapeHtml(media.label)}</div>` : "";
  }
}

function commentMediaHtml(post, comment) {
  const media = comment.media || [];
  if (!media.length) return "";
  const visual = media.filter((item) => VISUAL_MEDIA.has(item.type));
  const rest = media.filter((item) => !VISUAL_MEDIA.has(item.type));
  const grid = visual.length > 1
    ? `<div class="comment-media-grid">${visual.map((item) => commentMediaItemHtml(post, item)).join("")}</div>`
    : visual.map((item) => commentMediaItemHtml(post, item)).join("");
  return grid + rest.map((item) => commentMediaItemHtml(post, item)).join("");
}

function loadLottie() {
  lottiePlayer ??= import("lottie-web/build/player/lottie_light").then((module) => module.default);
  return lottiePlayer;
}

async function startLottie(node) {
  if (node._lottie) {
    node._lottie.play();
    return;
  }
  if (node.dataset.lottie) return;
  node.dataset.lottie = "loading";
  try {
    // .tgs is gzipped Lottie JSON; without DecompressionStream the static preview stays.
    if (!("DecompressionStream" in window)) throw new Error("No DecompressionStream");
    const [lottie, response] = await Promise.all([loadLottie(), fetch(node.dataset.src)]);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await new Response(response.body.pipeThrough(new DecompressionStream("gzip"))).json();
    if (!node.isConnected) {
      delete node.dataset.lottie;
      return;
    }
    node._lottie = lottie.loadAnimation({ container: node, renderer: "svg", loop: true, autoplay: true, animationData: data });
    node.classList.add("playing");
  } catch {
    node.dataset.lottie = "failed";
  }
}

function watchLotties(panel, list) {
  panel._lottieObserver?.disconnect();
  const nodes = list.querySelectorAll(".comment-lottie, .reaction-lottie");
  if (!nodes.length || !("IntersectionObserver" in window)) {
    nodes.forEach(startLottie);
    return;
  }
  panel._lottieObserver = new IntersectionObserver((entries) => {
    for (const entry of entries) {
      if (entry.isIntersecting) startLottie(entry.target);
      else entry.target._lottie?.pause();
    }
  }, { root: list, rootMargin: "120px 0px" });
  nodes.forEach((node) => panel._lottieObserver.observe(node));
}

function bindCommentMedia(node) {
  node.dataset.bound = "1";
  node.querySelectorAll("img").forEach((image) => {
    image.onerror = () => node.classList.add("broken");
  });
  const video = node.querySelector("video");
  if (!video) return;
  // Telegram video stickers are WebM, which not every WebView plays.
  if (video.dataset.fallback) video.onerror = () => video.replaceWith(Object.assign(new Image(), { src: video.dataset.fallback, alt: "" }));
  if (node.classList.contains("comment-round")) {
    node.addEventListener("click", () => {
      if (video.paused) video.play().catch(() => {});
      else video.pause();
    });
    video.addEventListener("play", () => node.classList.add("playing"));
    video.addEventListener("pause", () => node.classList.remove("playing"));
  }
}

function commentReactionsHtml(comment) {
  const top = comment.reactions?.top || [];
  if (!top.length) return "";
  const chips = top.map((reaction) => `<span class="comment-reaction">${reactionIcon(reaction)}<span>${formatCount(reaction.count)}</span></span>`).join("");
  return `<div class="comment-reactions">${chips}</div>`;
}

function commentHtml(post, comment, { byId, rootId }) {
  const parent = comment.reply_to_id ? byId.get(comment.reply_to_id) : null;
  // Inside a thread only a reply to someone other than the thread's author
  // needs to name its addressee; a reply to an unloaded comment quotes it.
  const target = parent && parent.id !== rootId
    ? `<span class="comment-arrow">▸</span><button class="comment-target-link" type="button" data-comment-id="${parent.id}">${escapeHtml(parent.author)}</button>`
    : "";
  const quote = !parent && comment.reply_to
    ? `<div class="comment-quote"><b>${escapeHtml(comment.reply_to.author)}</b><span>${escapeHtml(comment.reply_to.text)}</span></div>`
    : "";
  return `<article class="comment${comment.id === rootId ? "" : " comment-reply"}" id="comment-${comment.id}">
    <span class="comment-avatar"><span>${escapeHtml((comment.author || "П")[0].toUpperCase())}</span>${comment.author_id ? `<img src="${userAvatarUrl(comment.author_id)}" alt="" />` : ""}</span>
    <div class="comment-body">
      <div class="comment-author"><b>${escapeHtml(comment.author)}</b>${comment.mine ? `<em>вы</em>` : ""}${target}</div>
      ${quote}${commentMediaHtml(post, comment)}${comment.text ? `<div class="comment-text">${comment.text}</div>` : ""}${commentReactionsHtml(comment)}
      <div class="comment-actions"><time>${formatCommentDate(comment.date)}</time><button class="comment-reply-btn" type="button" data-reply-id="${comment.id}" data-root-id="${rootId}">Ответить</button></div>
    </div>
  </article>`;
}

function threadHtml(post, thread, state, byId) {
  const rootId = thread.root.id;
  const count = thread.replies.length;
  const open = state.expanded.has(rootId);
  const replies = open ? `<div class="comment-replies">${thread.replies.map((reply) => commentHtml(post, reply, { byId, rootId })).join("")}</div>` : "";
  const toggle = count
    ? `<button class="thread-toggle" type="button" data-thread-id="${rootId}"><span></span>${open ? "Скрыть" : `Посмотреть ${count} ${pluralRu(count, "ответ", "ответа", "ответов")}`}<i class="${open ? "up" : ""}"></i></button>`
    : "";
  return `<div class="comment-thread">${commentHtml(post, thread.root, { byId, rootId })}${replies}${toggle}</div>`;
}

function highlightComment(node) {
  node.scrollIntoView({ behavior: "smooth", block: "center" });
  node.classList.remove("comment-target");
  requestAnimationFrame(() => node.classList.add("comment-target"));
  setTimeout(() => node.classList.remove("comment-target"), 900);
}

function renderComments(post) {
  const state = commentState(post);
  const existing = openSheet();
  const previousScroll = existing?.querySelector(".comments-list")?.scrollTop || 0;
  const panel = existing || document.createElement("section");
  panel.className = `comments-sheet${me && !me.can_comment ? " readonly" : ""}`;
  panel.dataset.postId = post.id;
  const { byId, threads } = commentThreads(state.items);
  const comments = threads.map((thread) => threadHtml(post, thread, state, byId)).join("");
  const content = state.loading && !state.items.length
    ? `<p class="comments-status">Загружаю обсуждение…</p>`
    : !state.available
      ? `<p class="comments-status">${escapeHtml(state.message || "Обсуждение недоступно")}</p>`
      : comments || `<p class="comments-status">Комментариев пока нет. Можно начать обсуждение.</p>`;
  const total = Math.max(post.comments_count || 0, state.items.length);
  const title = total ? `${formatCount(total)} ${pluralRu(total, "комментарий", "комментария", "комментариев")}` : "Комментарии";
  const replyChip = state.replyTo
    ? `<div class="reply-chip"><span>Ответ для <b>${escapeHtml(state.replyTo.author)}</b></span><button type="button" class="reply-cancel" aria-label="Отменить ответ">×</button></div>`
    : "";
  const placeholder = state.replyTo ? `Ответить ${escapeHtml(state.replyTo.author)}` : "Добавить комментарий";
  const readOnly = state.available && me && !me.can_comment;
  const form = readOnly ? `<p class="comments-readonly">Писать комментарии можно в самом канале</p>` : state.available ? `
    <div class="comment-composer">${replyChip}<form class="comment-form"><textarea maxlength="4096" rows="1" placeholder="${placeholder}" ${state.sending ? "disabled" : ""}>${escapeHtml(state.draft)}</textarea><button type="submit" aria-label="Отправить" ${state.sending ? "disabled" : ""}><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3.4 20.4 21 12 3.4 3.6 3.4 10l12.6 2-12.6 2z"/></svg></button></form></div>` : "";
  // The sheet is rebuilt on every change; carry over media nodes so a playing
  // video or voice message is not restarted by loading more comments.
  const keptMedia = new Map([...panel.querySelectorAll(".comment-media[data-key]")].map((node) => [node.dataset.key, node]));
  panel.innerHTML = `<div class="comments-grabber"></div><header class="comments-header"><h2>${title}</h2><button type="button" class="comments-close" aria-label="Закрыть">×</button></header><div class="comments-list">${content}</div>${form}`;
  if (!existing) {
    const backdrop = document.createElement("div");
    backdrop.className = "comments-backdrop";
    backdrop.addEventListener("click", closeComments);
    bindSheetDrag(panel);
    document.body.append(backdrop, panel);
  }
  panel.querySelector(".comments-close").onclick = closeComments;
  panel.querySelector(".comment-form")?.addEventListener("submit", (event) => submitComment(event, post));
  panel.querySelector("textarea")?.addEventListener("input", (event) => { state.draft = event.target.value; });
  panel.querySelector(".reply-cancel")?.addEventListener("click", () => {
    state.replyTo = null;
    renderComments(post);
  });
  panel.querySelectorAll(".comment-avatar img").forEach((image) => { image.onerror = () => image.remove(); });
  panel.querySelectorAll(".comment-reaction video").forEach((video) => {
    video.onerror = () => { video.parentElement.textContent = "🙂"; };
  });
  const list = panel.querySelector(".comments-list");
  list.querySelectorAll(".comment-media[data-key]").forEach((node) => {
    const kept = keptMedia.get(node.dataset.key);
    if (kept) node.replaceWith(kept);
    else bindCommentMedia(node);
  });
  upgradePhotos(list);
  watchLotties(panel, list);
  panel.querySelectorAll(".thread-toggle").forEach((toggle) => {
    toggle.addEventListener("click", () => {
      const id = Number(toggle.dataset.threadId);
      if (state.expanded.has(id)) state.expanded.delete(id);
      else state.expanded.add(id);
      renderComments(post);
    });
  });
  panel.querySelectorAll(".comment-reply-btn").forEach((button) => {
    button.addEventListener("click", () => {
      const comment = byId.get(Number(button.dataset.replyId));
      if (!comment) return;
      state.replyTo = { id: comment.id, author: comment.author, rootId: Number(button.dataset.rootId) };
      renderComments(post);
      const textarea = openSheet(post)?.querySelector("textarea");
      textarea?.focus();
      textarea?.setSelectionRange(textarea.value.length, textarea.value.length);
    });
  });
  panel.querySelectorAll(".comment-target-link").forEach((link) => {
    link.addEventListener("click", () => {
      const original = list.querySelector(`#comment-${link.dataset.commentId}`);
      if (original) highlightComment(original);
    });
  });
  list.scrollTop = previousScroll;
  list.addEventListener("scroll", () => {
    if (list.scrollTop + list.clientHeight >= list.scrollHeight - 160) loadComments(post, true);
  }, { passive: true });
}

async function loadComments(post, append = false) {
  const state = commentState(post);
  if (state.loading || (append && !state.cursor)) return;
  state.loading = true;
  if (openSheet(post)) renderComments(post);
  try {
    const query = new URLSearchParams({ limit: "20" });
    if (append && state.cursor) query.set("after", String(state.cursor));
    const page = await api(`/api/posts/${post.id}/comments?${query}`);
    state.available = page.available;
    state.message = page.message;
    const known = new Set(state.items.map((comment) => comment.id));
    const fresh = (page.items || []).filter((comment) => !known.has(comment.id));
    state.items = append ? [...state.items, ...fresh] : fresh;
    state.cursor = page.next_cursor;
  } catch (error) {
    state.available = false;
    state.message = error.message;
  } finally {
    state.loading = false;
    // The sheet may have been closed, or reopened for another post, meanwhile.
    if (openSheet(post)) renderComments(post);
  }
}

function openComments(post) {
  closeComments();
  renderComments(post);
  const state = commentState(post);
  if (!state.items.length && !state.message) loadComments(post);
}

async function submitComment(event, post) {
  event.preventDefault();
  const state = commentState(post);
  const textarea = event.currentTarget.querySelector("textarea");
  const text = textarea.value.trim();
  if (!text || state.sending) return;
  const replyTo = state.replyTo;
  state.sending = true;
  state.draft = text;
  renderComments(post);
  let sent = null;
  try {
    sent = await api(`/api/posts/${post.id}/comments`, { method: "POST", body: JSON.stringify({ text, reply_to: replyTo?.id ?? null }) });
    if (replyTo) {
      sent.reply_to_id ??= replyTo.id;
      state.items.push(sent);
      state.expanded.add(replyTo.rootId);
    } else {
      state.items.unshift(sent);
    }
    state.draft = "";
    state.replyTo = null;
    setCommentsCount(post, (post.comments_count || 0) + 1);
    tg?.HapticFeedback?.notificationOccurred?.("success");
  } catch (error) {
    showToast(error.message);
  } finally {
    state.sending = false;
    if (openSheet(post)) renderComments(post);
  }
  const node = sent && openSheet(post)?.querySelector(`#comment-${sent.id}`);
  if (node) highlightComment(node);
}

function showToast(text) {
  document.querySelector(".toast")?.remove();
  const toast = document.createElement("div");
  toast.className = "toast";
  toast.textContent = text;
  document.body.append(toast);
  setTimeout(() => toast.remove(), 1800);
}

function showLikeHeart(x, y) {
  const heart = document.createElement("span");
  heart.className = "like-heart";
  heart.innerHTML = ICONS.heart;
  heart.style.left = `${x}px`;
  heart.style.top = `${y}px`;
  heart.style.setProperty("--tilt", `${Math.round(Math.random() * 36 - 18)}deg`);
  document.body.appendChild(heart);
  setTimeout(() => heart.remove(), 820);
}

function setLiked(post, liked) {
  post.liked = liked;
  slideNodes.get(post.id)?.querySelector(".rail-like")?.classList.toggle("liked", liked);
}

function popLike(post) {
  const button = slideNodes.get(post.id)?.querySelector(".rail-like");
  if (!button) return;
  button.classList.remove("pop");
  void button.offsetWidth;
  button.classList.add("pop");
}

const LONG_PRESS_MS = 450;

// A tap likes; holding the heart shows the post's top reactions instead.
function bindLikeButton(button, post) {
  let timer = null;
  let start = null;
  let held = false;
  const begin = (x, y) => {
    clearTimeout(timer);
    held = false;
    start = { x, y };
    timer = setTimeout(() => {
      held = true;
      tg?.HapticFeedback?.impactOccurred?.("medium");
      showReactionsPanel(post, button);
    }, LONG_PRESS_MS);
  };
  const moveTo = (x, y) => {
    if (start && Math.hypot(x - start.x, y - start.y) > 10) clearTimeout(timer);
  };
  const end = () => {
    clearTimeout(timer);
    start = null;
  };
  if ("ontouchstart" in window) {
    button.addEventListener("touchstart", (event) => {
      const touch = event.touches[0];
      if (touch) begin(touch.clientX, touch.clientY);
    }, { passive: true });
    button.addEventListener("touchmove", (event) => {
      const touch = event.touches[0];
      if (touch) moveTo(touch.clientX, touch.clientY);
    }, { passive: true });
    button.addEventListener("touchend", end, { passive: true });
    button.addEventListener("touchcancel", end, { passive: true });
  } else {
    button.addEventListener("pointerdown", (event) => {
      if (event.button === 0) begin(event.clientX, event.clientY);
    });
    button.addEventListener("pointermove", (event) => moveTo(event.clientX, event.clientY));
    button.addEventListener("pointerup", end);
    button.addEventListener("pointerleave", end);
    button.addEventListener("pointercancel", end);
  }
  button.addEventListener("contextmenu", (event) => event.preventDefault());
  button.addEventListener("click", () => {
    if (held) {
      held = false;
      return;
    }
    likePost(post);
  });
}

// Telegram sends ❤ without U+FE0F, and iOS then draws it as a white text glyph.
function colorEmoji(text) {
  return text.replace(/(\p{Extended_Pictographic})(?![️\u{1F3FB}-\u{1F3FF}])/gu, "$1️");
}

function reactionIcon(reaction) {
  if (reaction.type === "paid") return `<span class="reaction-icon">⭐</span>`;
  if (reaction.type === "emoji") return `<span class="reaction-icon">${escapeHtml(colorEmoji(reaction.emoji))}</span>`;
  const alt = escapeHtml(colorEmoji(reaction.emoji || "🙂"));
  const url = escapeHtml(customEmojiUrl(reaction.id));
  switch (reaction.format) {
    case "static":
      return `<span class="reaction-icon"><img src="${url}" alt="${alt}" /></span>`;
    case "video":
      return `<span class="reaction-icon"><video src="${url}" autoplay loop muted playsinline></video></span>`;
    case "lottie":
      return `<span class="reaction-icon reaction-lottie" data-src="${url}"><span class="reaction-alt">${alt}</span></span>`;
    default:
      return `<span class="reaction-icon">${alt}</span>`;
  }
}

function closeReactionsPanel() {
  const layer = document.querySelector(".reactions-layer");
  if (!layer) return;
  layer.querySelectorAll(".reaction-lottie").forEach((node) => node._lottie?.destroy());
  layer.remove();
}

function showReactionsPanel(post, button) {
  closeReactionsPanel();
  const top = post.reactions?.top || [];
  const layer = document.createElement("div");
  layer.className = "reactions-layer";
  const chips = top.map((reaction) => `<div class="reaction-chip">${reactionIcon(reaction)}<span class="reaction-count">${formatCount(reaction.count)}</span></div>`).join("");
  layer.innerHTML = `<div class="reactions-panel" role="dialog" aria-label="Реакции">${chips || `<span class="reactions-empty">Реакций пока нет</span>`}</div>`;
  const panel = layer.firstElementChild;
  // Any touch outside the panel only closes it, so it can't flip or pause the post.
  const dismiss = (event) => {
    if (panel.contains(event.target)) return;
    event.preventDefault();
    closeReactionsPanel();
  };
  layer.addEventListener("touchstart", dismiss, { passive: false });
  layer.addEventListener("pointerdown", dismiss);
  layer.addEventListener("wheel", closeReactionsPanel, { passive: true });
  document.body.append(layer);

  const rect = button.getBoundingClientRect();
  const right = Math.max(8, window.innerWidth - rect.left + 6);
  panel.style.right = `${right}px`;
  panel.style.top = `${Math.max(8, rect.top + rect.height / 2 - panel.offsetHeight / 2)}px`;

  panel.querySelectorAll("video").forEach((video) => {
    video.onerror = () => { video.parentElement.textContent = "🙂"; };
  });
  panel.querySelectorAll(".reaction-lottie").forEach(startLottie);
}

async function likePost(post, x, y) {
  if (!post) return;
  if (x !== undefined) showLikeHeart(x, y);
  popLike(post);
  if (liking || post.liked) return;
  liking = true;
  setLiked(post, true);
  try {
    await api(`/api/posts/${post.id}/like`, { method: "POST" });
    tg?.HapticFeedback?.notificationOccurred?.("success");
  } catch (error) {
    setLiked(post, false);
    tg?.HapticFeedback?.notificationOccurred?.("error");
    showToast(error.message);
  } finally {
    liking = false;
  }
}

function shareLink(post) {
  const source = postSource(post);
  if (!source) return;
  const url = `https://t.me/share/url?url=${encodeURIComponent(source)}`;
  if (tg?.openTelegramLink) tg.openTelegramLink(url);
  else window.open(url, "_blank", "noopener");
}

// Sends the post itself — media and text — through Telegram's chat picker.
// Older clients without shareMessage fall back to sharing the link.
async function sharePost(post) {
  if (!tg?.shareMessage || !tg.isVersionAtLeast?.("8.0")) return shareLink(post);
  const button = slideNodes.get(post.id)?.querySelector(".rail-share");
  if (button?.classList.contains("loading")) return;
  button?.classList.add("loading");
  try {
    const { prepared_id: preparedId } = await api(`/api/posts/${post.id}/share`, { method: "POST" });
    tg.shareMessage(preparedId, (sent) => {
      if (sent) tg?.HapticFeedback?.notificationOccurred?.("success");
    });
  } catch (error) {
    showToast(error.message);
    shareLink(post);
  } finally {
    button?.classList.remove("loading");
  }
}

function singleTap() {
  const node = activeSlide();
  if (!node) return;
  if (node.classList.contains("expanded")) return toggleExpand(node, false);
  const media = visibleMedia(node);
  if (!media) return;
  if (media.muted) {
    media.muted = false;
    node.classList.remove("muted");
    return;
  }
  if (media.paused) {
    media.play().catch(() => {});
    node.classList.remove("paused");
  } else {
    media.pause();
    node.classList.add("paused");
  }
}

// Holding the right side of playing video or audio speeds it up, as in TikTok.
function fastForwardTarget(element, x) {
  if (x < window.innerWidth * 2 / 3 || !element?.closest(".slide.has-media") || element.closest(".slide-info")) return null;
  const media = visibleMedia(activeSlide());
  return media && !media.paused ? media : null;
}

function startFastForward(start, media) {
  if (pointerStart !== start || start.dragging || start.rejected) return;
  start.fast = media;
  media.playbackRate = 2;
  activeSlide()?.classList.add("fast");
  tg?.HapticFeedback?.impactOccurred?.("light");
}

function stopFastForward(start) {
  clearTimeout(start.fastTimer);
  if (!start.fast) return false;
  start.fast.playbackRate = 1;
  start.fast.closest(".slide")?.classList.remove("fast");
  return true;
}

function scrollableText(element) {
  const text = element?.closest(".post-text");
  if (!text) return null;
  const overflow = getComputedStyle(text).overflowY;
  // A collapsed caption clips its text but cannot scroll; treating it as a
  // scroll area would block the swipe to the next post.
  return (overflow === "auto" || overflow === "scroll") && text.scrollHeight > text.clientHeight + 2 ? text : null;
}

function release(start) {
  const height = stage.clientHeight;
  // A finger that stopped before lifting has no velocity left.
  const velocity = performance.now() - start.lastT > 120 ? 0 : start.vy;
  let direction = 0;
  if (start.dy < 0 && (start.dy < -height * 0.18 || velocity < -0.45)) direction = 1;
  if (start.dy > 0 && (start.dy > height * 0.18 || velocity > 0.45)) direction = -1;
  if (!direction) return settle();
  tg?.HapticFeedback?.impactOccurred?.("light");
  move(direction);
}

function bindGestures() {
  const isControl = (target) => target instanceof Element
    && target.closest("a, button, .comments-sheet, .channels-screen, iframe");
  const begin = (target, x, y) => {
    if (animating || !stage || isControl(target)) return;
    const element = target instanceof Element ? target : target.parentElement;
    pointerStart = {
      x,
      y,
      dy: 0,
      vy: 0,
      lastY: y,
      lastT: performance.now(),
      dragging: false,
      rejected: false,
      inInfo: Boolean(element?.closest(".slide-info")),
      // Text has its own scroll area. A swipe inside it scrolls the text and
      // only flips the post once the text is at its edge.
      scrollContainer: scrollableText(element),
    };
    const media = fastForwardTarget(element, x);
    if (media) {
      const start = pointerStart;
      start.fastTimer = setTimeout(() => startFastForward(start, media), 350);
    }
  };
  const drag = (x, y, event) => {
    const start = pointerStart;
    if (start?.fast) {
      if (event?.cancelable) event.preventDefault();
      return;
    }
    if (!start || start.rejected || animating) return;
    const dx = x - start.x;
    const dy = y - start.y;
    if (!start.dragging) {
      if (Math.abs(dx) < 8 && Math.abs(dy) < 8) return;
      clearTimeout(start.fastTimer);
      if (Math.abs(dx) > Math.abs(dy)) {
        start.rejected = true;
        return;
      }
      const scroller = start.scrollContainer;
      if (scroller) {
        const atTop = scroller.scrollTop <= 2;
        const atBottom = scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 2;
        if ((dy < 0 && !atBottom) || (dy > 0 && !atTop)) {
          start.rejected = true;
          return;
        }
      }
      start.dragging = true;
    }
    if (event?.cancelable) event.preventDefault();
    const now = performance.now();
    start.vy = start.vy * 0.2 + ((y - start.lastY) / Math.max(1, now - start.lastT)) * 0.8;
    start.lastY = y;
    start.lastT = now;
    start.dy = dy;
    const blocked = (dy > 0 && currentIndex === 0) || (dy < 0 && currentIndex >= feed.length - 1);
    setDrag(blocked ? dy * 0.3 : dy);
  };
  const registerTap = (start, x, y) => {
    if (Math.abs(x - start.x) > 14 || Math.abs(y - start.y) > 14) return;
    const now = Date.now();
    clearTimeout(tapTimer);
    if (lastTap && now - lastTap.at < 300 && Math.hypot(x - lastTap.x, y - lastTap.y) < 54) {
      lastTap = null;
      likePost(feed[currentIndex], x, y);
      return;
    }
    lastTap = { at: now, x, y };
    tapTimer = setTimeout(singleTap, 300);
  };
  const finish = (x, y) => {
    const start = pointerStart;
    if (!start) return;
    pointerStart = null;
    if (stopFastForward(start)) return;
    if (start.dragging) return release(start);
    if (!start.rejected && !start.inInfo) registerTap(start, x, y);
  };
  const cancel = () => {
    const start = pointerStart;
    pointerStart = null;
    if (start && stopFastForward(start)) return;
    if (start?.dragging) settle();
  };

  // Pinching zooms the post's media and lets it spring back on release, as in
  // TikTok. The whole media layer scales, so the carousel keeps its position.
  let pinch = null;
  const midpoint = (a, b) => ({ x: (a.clientX + b.clientX) / 2, y: (a.clientY + b.clientY) / 2 });
  const spread = (a, b) => Math.max(1, Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY));
  const startPinch = (event) => {
    const target = event.target instanceof Element ? event.target : null;
    const layer = activeSlide()?.querySelector(".slide-media");
    if (!layer || animating || pointerStart?.dragging || !target?.closest(".slide.has-media") || isControl(target)) return;
    const [a, b] = event.touches;
    const center = midpoint(a, b);
    const rect = layer.getBoundingClientRect();
    layer.style.transition = "none";
    layer.style.transformOrigin = `${center.x - rect.left}px ${center.y - rect.top}px`;
    layer.closest(".slide").classList.add("zooming");
    pinch = { layer, center, distance: spread(a, b) };
  };
  const movePinch = (event) => {
    const [a, b] = event.touches;
    if (event.cancelable) event.preventDefault();
    if (!b) return;
    const center = midpoint(a, b);
    const scale = Math.min(4, Math.max(1, spread(a, b) / pinch.distance));
    pinch.layer.style.transform = `translate(${center.x - pinch.center.x}px, ${center.y - pinch.center.y}px) scale(${scale})`;
  };
  const endPinch = () => {
    const { layer } = pinch;
    pinch = null;
    layer.style.transition = "transform .25s ease-out";
    layer.style.transform = "";
    layer.closest(".slide")?.classList.remove("zooming");
  };

  // Telegram Android WebView reliably exposes Touch Events. Pointer Events are
  // retained as a fallback for desktop and clients without touch support.
  if ("ontouchstart" in window) {
    app.addEventListener("touchstart", (event) => {
      if (event.touches.length > 1) {
        if (event.touches.length === 2 && !pinch) startPinch(event);
        return cancel();
      }
      const touch = event.touches[0];
      if (touch) begin(event.target, touch.clientX, touch.clientY);
    }, { passive: true });
    app.addEventListener("touchmove", (event) => {
      if (pinch) return movePinch(event);
      const touch = event.touches[0];
      if (touch) drag(touch.clientX, touch.clientY, event);
    }, { passive: false });
    app.addEventListener("touchend", (event) => {
      if (pinch) {
        if (event.touches.length < 2) endPinch();
        return;
      }
      const touch = event.changedTouches[0];
      if (touch) finish(touch.clientX, touch.clientY);
    }, { passive: true });
    app.addEventListener("touchcancel", () => {
      if (pinch) endPinch();
      cancel();
    }, { passive: true });
  } else {
    app.addEventListener("pointerdown", (event) => {
      if (event.button === 0) begin(event.target, event.clientX, event.clientY);
    });
    window.addEventListener("pointermove", (event) => drag(event.clientX, event.clientY, event));
    window.addEventListener("pointerup", (event) => finish(event.clientX, event.clientY));
    window.addEventListener("pointercancel", cancel);
  }

  let wheelLockedUntil = 0;
  window.addEventListener("wheel", (event) => {
    if (!(event.target instanceof Element) || overlayOpen() || event.target.closest(".comments-sheet") || scrollableText(event.target)) return;
    if (Math.abs(event.deltaY) < 30 || Date.now() < wheelLockedUntil) return;
    wheelLockedUntil = Date.now() + 700;
    move(event.deltaY > 0 ? 1 : -1);
  }, { passive: true });

  document.addEventListener("keydown", (event) => {
    if (overlayOpen() || (event.target instanceof Element && event.target.closest("textarea, input"))) return;
    if (event.key === "ArrowDown" || event.key === "PageDown") move(1);
    if (event.key === "ArrowUp" || event.key === "PageUp") move(-1);
    if (event.key === " ") singleTap();
  });
}

function resetFeed() {
  flushView();
  slideNodes.forEach(disposeMedia);
  slideNodes.clear();
  feed.length = 0;
  seenInSession.clear();
  currentIndex = 0;
  nextCursor = null;
  hasMore = true;
  pageRequest = null;
  stage = null;
}

async function startFeed() {
  resetFeed();
  app.innerHTML = `<main class="loading-state"><span class="spinner"></span>Загружаю ленту…</main>`;
  try {
    await loadPage();
    if (!feed.length) return renderEmpty();
    renderShell();
    layoutSlides();
    activate();
  } catch (error) {
    app.innerHTML = `<main class="empty-state"><h1>Не удалось загрузить ленту</h1><p>${escapeHtml(error.message)}</p></main>`;
  }
}

/* My channels: list, add by @username or link, starter packs */

const CHANNEL_STATUS = {
  active: "",
  pending_join: "подключается…",
  failed: "не удалось подключить",
  left: "отключён",
};
let catalog = null;
let channelsState = null;

function overlayOpen() {
  return Boolean(document.querySelector(".channels-screen"));
}

async function refreshMe() {
  me = await api("/api/me");
  return me;
}

function channelItemHtml(channel) {
  const status = CHANNEL_STATUS[channel.status] ?? "";
  const detail = channel.status === "failed" && channel.last_error ? channel.last_error : status;
  const initial = escapeHtml((channel.title.replace(/^@/, "").trim()[0] || "К").toUpperCase());
  return `<li class="channel-item ${escapeHtml(channel.status)}" data-id="${channel.id}">
    <span class="channel-avatar">${initial}</span>
    <span class="channel-meta"><span class="channel-title">${escapeHtml(channel.title)}${channel.is_private ? ` <span class="channel-lock" title="Приватный">🔒</span>` : ""}</span>${detail ? `<span class="channel-status">${escapeHtml(detail)}</span>` : channel.username ? `<span class="channel-status">@${escapeHtml(channel.username)}</span>` : ""}</span>
    <button class="channel-remove" type="button" aria-label="Удалить">×</button>
  </li>`;
}

function packHtml(pack, subscribed) {
  const done = pack.channels.every((username) => subscribed.has(username.toLowerCase()));
  return `<li class="pack${done ? " added" : ""}" data-pack="${escapeHtml(pack.id)}">
    <span class="pack-emoji">${escapeHtml(pack.emoji)}</span>
    <span class="pack-meta"><span class="pack-title">${escapeHtml(pack.title)}</span><span class="pack-channels">${pack.channels.map((c) => `@${escapeHtml(c)}`).join(" · ")}</span></span>
    <button class="pack-add" type="button" ${done ? "disabled" : ""}>${done ? "Добавлено" : "Добавить"}</button>
  </li>`;
}

function renderChannels() {
  const screen = document.querySelector(".channels-screen");
  if (!screen || !channelsState) return;
  const { items, onboarding, busy } = channelsState;
  const subscribed = new Set(items.filter((c) => c.username).map((c) => c.username.toLowerCase()));
  const limit = me?.channel_limit;
  const counter = limit ? `<span class="channels-counter">${items.length} / ${limit}</span>` : "";
  const list = items.length
    ? `<ul class="channel-list">${items.map(channelItemHtml).join("")}</ul>`
    : `<p class="channels-empty">${onboarding ? "Выбери подборку или добавь свои каналы." : "Каналов пока нет."}</p>`;
  const packs = catalog?.length ? `<h3>Подборки</h3><ul class="pack-list">${catalog.map((pack) => packHtml(pack, subscribed)).join("")}</ul>` : "";
  const scroll = screen.querySelector(".channels-body")?.scrollTop || 0;
  screen.innerHTML = `
    <header class="channels-header"><h2>${onboarding ? "Что будем читать?" : "Мои каналы"}</h2>${onboarding ? "" : `<button class="channels-close" type="button" aria-label="Закрыть">×</button>`}</header>
    <div class="channels-body">
      <form class="channel-form"><input type="text" inputmode="url" autocomplete="off" autocapitalize="off" spellcheck="false" placeholder="@канал или ссылка t.me/…" value="${escapeHtml(channelsState.draft)}" ${busy ? "disabled" : ""}/><button type="submit" ${busy ? "disabled" : ""}>Добавить</button></form>
      <p class="channels-hint">Ещё можно переслать боту любой пост из канала. Для приватного канала пришли ссылку-приглашение.</p>
      <div class="channels-section-title"><h3>Мои каналы</h3>${counter}</div>
      ${list}
      ${packs}
    </div>
    ${onboarding ? `<footer class="channels-footer"><button class="primary-btn" type="button" ${items.length ? "" : "disabled"}>Смотреть ленту</button></footer>` : ""}`;
  screen.querySelector(".channels-body").scrollTop = scroll;
  screen.querySelector(".channels-close")?.addEventListener("click", closeChannels);
  screen.querySelector(".channels-footer .primary-btn")?.addEventListener("click", closeChannels);
  const input = screen.querySelector(".channel-form input");
  input.addEventListener("input", () => { channelsState.draft = input.value; });
  screen.querySelector(".channel-form").addEventListener("submit", (event) => {
    event.preventDefault();
    addChannel(input.value);
  });
  screen.querySelectorAll(".channel-remove").forEach((button) => {
    button.addEventListener("click", () => removeChannel(Number(button.closest(".channel-item").dataset.id)));
  });
  screen.querySelectorAll(".pack-add").forEach((button) => {
    button.addEventListener("click", () => addPack(button.closest(".pack").dataset.pack));
  });
}

async function loadChannels() {
  const [{ items }] = await Promise.all([
    api("/api/channels"),
    catalog ? null : api("/api/catalog").then((data) => { catalog = data.items; }),
  ]);
  channelsState.items = items;
  renderChannels();
  // Queued channels turn active within minutes; keep the list current while it is open.
  clearTimeout(channelsPoll);
  if (items.some((c) => c.status === "pending_join")) {
    channelsPoll = setTimeout(() => { if (overlayOpen()) loadChannels().catch(() => {}); }, 10000);
  }
}

async function withBusy(action) {
  if (channelsState.busy) return;
  channelsState.busy = true;
  renderChannels();
  try {
    await action();
    channelsState.changed = true;
    tg?.HapticFeedback?.notificationOccurred?.("success");
  } catch (error) {
    tg?.HapticFeedback?.notificationOccurred?.("error");
    showToast(error.message);
  } finally {
    channelsState.busy = false;
    await loadChannels().catch(() => renderChannels());
  }
}

function addChannel(ref) {
  if (!ref.trim()) return;
  return withBusy(async () => {
    const result = await api("/api/channels", { method: "POST", body: JSON.stringify({ ref }) });
    channelsState.draft = "";
    const title = result.channel.title;
    showToast(result.result === "already" ? `${title} уже в ленте` : result.result === "queued" ? `${title} подключается` : `${title} добавлен`);
  });
}

function addPack(packId) {
  return withBusy(async () => {
    const result = await api(`/api/catalog/${encodeURIComponent(packId)}/subscribe`, { method: "POST" });
    if (result.error) showToast(result.error);
  });
}

function removeChannel(id) {
  return withBusy(() => api(`/api/channels/${id}`, { method: "DELETE" }));
}

async function openChannels({ onboarding = false } = {}) {
  if (overlayOpen()) return;
  flushView();
  pauseMedia(activeSlide());
  channelsState = { items: [], onboarding, busy: false, changed: false, draft: "" };
  const screen = document.createElement("section");
  screen.className = "channels-screen";
  document.body.append(screen);
  screen.innerHTML = `<div class="loading-state"><span class="spinner"></span></div>`;
  try {
    await loadChannels();
  } catch (error) {
    screen.innerHTML = `<div class="empty-state"><h1>Не удалось загрузить каналы</h1><p>${escapeHtml(error.message)}</p></div>`;
  }
}

async function closeChannels() {
  clearTimeout(channelsPoll);
  document.querySelector(".channels-screen")?.remove();
  const { changed, onboarding } = channelsState || {};
  channelsState = null;
  await refreshMe().catch(() => {});
  if (changed || onboarding || !stage) return startFeed();
  const node = activeSlide();
  if (node) playSlideMedia(node);
  if (feed[currentIndex]) markSeen(feed[currentIndex]);
}

async function start() {
  if (!tg?.initData) {
    app.innerHTML = `<main class="empty-state"><h1>Открой ленту через бота</h1></main>`;
    return;
  }
  app.innerHTML = `<main class="loading-state"><span class="spinner"></span>Загружаю ленту…</main>`;
  try {
    await refreshMe();
  } catch (error) {
    app.innerHTML = `<main class="empty-state"><h1>Не удалось загрузить ленту</h1><p>${escapeHtml(error.message)}</p></main>`;
    return;
  }
  if (!me.channels_count) {
    app.innerHTML = "";
    return openChannels({ onboarding: true });
  }
  return startFeed();
}

bindGestures();
start();
