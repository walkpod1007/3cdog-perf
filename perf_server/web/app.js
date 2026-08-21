"use strict";

// Browser-only, dependency-free client for src/server.py and src/schema.py.
const state = {
  name: "",
  records: [],
  offset: 0,
  fileSize: 0,
  mode: "live",
  pollTimer: null,
  replayTimer: null,
  replayIndex: 0,
  replayPlaying: false,
  replaySpeed: 1,
  failures: 0,
  generation: 0,
  warnings: [],
  status: null,
  view: "overview",
  devices: [],
  packages: [],
  record: { running: false, session_id: null, output: null },
  // Flips true the moment the user manually touches deviceSelect/packageSelect
  // (real UI interaction only — programmatic `.value =` assignment from
  // restoreRecordSelection() does not fire a change event, so this can't
  // self-trigger). Gates the 4s background status poll from clawing back a
  // deliberate target change once recording is idle. See restoreRecordSelection().
  userPickedTarget: false,
  // Phase 2: device source toggle. "android" = adb path, "ios" = pymobiledevice3
  // path. The "自動偵測" button is the primary UX (per the 08-21 brief);
  // the manual package/bundle dropdown is collapsed into an "advanced" section.
  platform: "android",
};

// Mirror of the BUNDLE_FRIENDLY_NAMES table on the server side. Kept in sync
// by hand because the front-end is single-file and the server is zero-dep
// stdlib — the brief's "pikmin 級別的常見款 + fallback 顯示 bundle id" allows
// this set to be small. Anything not listed falls back to the raw bundle id
// in the UI; the server has the canonical table in src/server.py.
const BUNDLE_FRIENDLY_NAMES = {
  "com.nianticlabs.pikmin": "Pikmin",
  "com.nianticlabs.pikminbloom": "Pikmin Bloom",
  "com.google.ios.youtube": "YouTube",
  "com.netflix.Netflix": "Netflix",
  "com.amazon.avod.thirdpartyclient": "Prime Video",
  "com.apple.mobilesafari": "Safari",
  "com.hbo.hboMax": "Max",
  "com.disney.disneyplus": "Disney+",
  "com.spotify.client": "Spotify",
};

const $ = (id) => document.getElementById(id);
const sessionSelect = $("sessionSelect");
const cards = Object.fromEntries(
  [...document.querySelectorAll(".chart-card[data-chart]")].map((card) => [card.dataset.chart, card]),
);

// Onboarding wizard state. Kept off the global `state` object because it has a
// very different lifecycle (one-shot on landing, then hidden) and we don't want
// it to leak into session/recording renders.
const onboardingState = {
  android: null, // raw snapshot from /api/onboarding/state
  ios: null,
  fetchError: null,
  refreshTimer: null,
};

// Server-side device_state -> human-readable 繁中 label. Kept in sync with
// the python `_detect_adb_state` / `_detect_ios_state` enums; adding a new
// state on the server without updating this map degrades to the fallback at
// the bottom of `describeAndroidState` / `describeIosState`.
const ANDROID_STATE_LABELS = {
  missing_tool: "找不到 adb",
  adb_timeout: "adb 沒回應",
  disconnected: "尚未偵測到手機",
  unauthorized: "裝置未授權——請看手機螢幕按「允許 USB 偵錯」",
  offline: "裝置離線——把線拔掉重接一次",
  multiple_devices: "接了多台已授權裝置",
  no_permissions: "macOS 沒給 adb 權限",
  device: "已連線",
};
const IOS_REASON_LABELS = {
  missing_tool: "尚未安裝 pymobiledevice3",
  detect_failed: "pymobiledevice3 偵測失敗",
  no_device: "尚未偵測到 iPhone",
  ready: "已偵測到 iPhone",
};

// Steps that light up when the corresponding prerequisite is satisfied. The
// order matches the ol[data-platform] children in index.html — keep them in
// sync if the copy changes.
const ANDROID_STEP_ORDER = [
  "developer-options",
  "usb-debugging",
  "plug-cable",
  "allow-prompt",
  "adb-detected",
];
const IOS_STEP_ORDER = [
  "install-pmd",
  "plug-cable",
  "trust",
  "developer-mode",
  "tunneld",
  "tunneld-consent",
  "pmd-detected",
];

function describeAndroidState(snapshot) {
  if (!snapshot) return { kind: "checking", title: "狀態偵測中…", hint: "讀取 adb 中，請稍等" };
  if (!snapshot.adb_installed) {
    return {
      kind: "error",
      title: ANDROID_STATE_LABELS.missing_tool,
      hint: snapshot.next_step || "請先用 Homebrew 裝一次 android-platform-tools",
    };
  }
  const state = snapshot.device_state;
  if (state === "device") {
    return {
      kind: "ready",
      title: `${ANDROID_STATE_LABELS.device} · ${snapshot.device_serial || "Android"}`,
      hint: snapshot.next_step,
    };
  }
  if (state === "multiple_devices") {
    return {
      kind: "error",
      title: ANDROID_STATE_LABELS.multiple_devices,
      hint: snapshot.next_step || "請只留一台，或稍後在 Lab 選序號",
    };
  }
  // Any non-ready Android state — unauthorized, offline, adb_timeout,
  // disconnected, no_permissions, etc. — maps to a 繁中 label rather than
  // spraying the raw token into the title.
  const label = ANDROID_STATE_LABELS[state] || "Android 狀態待確認";
  const note = snapshot.note ? `（${snapshot.note}）` : "";
  return {
    kind: state === "disconnected" ? "waiting" : "error",
    title: label,
    hint: `${snapshot.next_step || ""}${note}`.trim(),
  };
}

function describeIosState(snapshot) {
  if (!snapshot) return { kind: "checking", title: "狀態偵測中…", hint: "讀取 pymobiledevice3 中，請稍等" };
  const reason = snapshot.reason || (snapshot.pymobiledevice3_installed ? (snapshot.device_count > 0 ? "ready" : "no_device") : "missing_tool");
  if (reason === "ready") {
    return {
      kind: "ready",
      title: `${IOS_REASON_LABELS.ready}（${snapshot.device_count || 0} 部）`,
      hint: snapshot.next_step,
    };
  }
  const label = IOS_REASON_LABELS[reason] || "iPhone 狀態待確認";
  // The server intentionally surfaces ``note`` on the detect_failed branch so
  // the user can see the real reason (tunneld, developer mode, sysmon change)
  // instead of being told to replug the cable.
  const note = snapshot.note ? `（${snapshot.note}）` : "";
  return {
    kind: reason === "missing_tool" ? "waiting" : "error",
    title: label,
    hint: `${snapshot.next_step || ""}${note}`.trim(),
  };
}

function paintPlatformCard(platform, snapshot) {
  const card = $(platform === "android" ? "onboardingAndroid" : "onboardingiOS");
  const status = $(platform === "android" ? "onboardingAndroidStatus" : "onboardingiOSStatus");
  const stepsId = platform === "android" ? "onboardingAndroidSteps" : "onboardingiOSSteps";
  const stepsEl = $(stepsId);
  const desc = platform === "android" ? describeAndroidState(snapshot) : describeIosState(snapshot);

  if (status) {
    status.dataset.state = desc.kind;
    const title = status.querySelector(".status-title");
    const hint = status.querySelector(".status-hint");
    if (title) title.textContent = desc.title;
    if (hint) hint.textContent = desc.hint;
  }
  if (stepsEl) {
    const order = platform === "android" ? ANDROID_STEP_ORDER : IOS_STEP_ORDER;
    // For Android we map the current device_state to the *most relevant*
    // prerequisite step instead of falling back to step 0. Otherwise a phone
    // that's already past "developer-options" would see the wizard tell them
    // to "tap build number 7 times" again, which is misleading.
    let activeIdx = -1;
    if (platform === "android" && snapshot) {
      const state = snapshot.device_state;
      if (state === "device") {
        activeIdx = order.indexOf("adb-detected");
      } else if (state === "unauthorized") {
        activeIdx = order.indexOf("allow-prompt");
      } else if (state === "offline" || state === "disconnected" || state === "adb_timeout") {
        activeIdx = order.indexOf("plug-cable");
      } else if (state === "multiple_devices") {
        // No single step resolves this; keep the highlight on the last
        // action that actually moved the wizard forward (plug-cable) rather
        // than regressing to step 0.
        activeIdx = order.indexOf("plug-cable");
      } else if (state === "missing_tool" || !snapshot.adb_installed) {
        activeIdx = order.indexOf("usb-debugging");
      } else {
        // Unknown future state: highlight the last concrete step instead
        // of regressing to developer-options.
        activeIdx = order.indexOf("plug-cable");
      }
    } else if (platform === "ios" && snapshot) {
      const reason = snapshot.reason || (snapshot.pymobiledevice3_installed ? "no_device" : "missing_tool");
      if (reason === "ready") {
        activeIdx = order.indexOf("pmd-detected");
      } else if (reason === "missing_tool") {
        activeIdx = order.indexOf("install-pmd");
      } else if (reason === "detect_failed") {
        // tunneld / developer-mode / dvt issues — point at the tunneld step,
        // which is the most common day-one failure that doesn't blame the cable.
        activeIdx = order.indexOf("tunneld");
      } else if (reason === "no_device") {
        activeIdx = order.indexOf("trust");
      } else {
        activeIdx = order.indexOf("plug-cable");
      }
    }
    Array.from(stepsEl.children).forEach((li) => {
      const step = li.getAttribute("data-step");
      const idx = order.indexOf(step);
      if (idx === -1) return;
      li.removeAttribute("data-step-state");
      if (activeIdx === -1) {
        // No snapshot / unknown snapshot: nothing is "active" yet, leave the
        // list visually neutral. Previously we fell back to idx === 0, which
        // re-highlighted step 0 every time the wizard re-rendered with an
        // empty snapshot.
        return;
      }
      if (idx < activeIdx) {
        li.setAttribute("data-step-state", "done");
      } else if (idx === activeIdx) {
        li.setAttribute("data-step-state", "active");
      }
    });
  }
  if (card) {
    card.dataset.state = desc.kind;
  }
}

function renderOnboarding(payload) {
  onboardingState.android = payload && payload.android ? payload.android : null;
  onboardingState.ios = payload && payload.ios ? payload.ios : null;
  paintPlatformCard("android", onboardingState.android);
  paintPlatformCard("ios", onboardingState.ios);
}

function hideOnboardingOverlay() {
  const overlay = $("onboardingOverlay");
  if (overlay) overlay.classList.add("is-hidden");
  if (onboardingState.refreshTimer) {
    clearInterval(onboardingState.refreshTimer);
    onboardingState.refreshTimer = null;
  }
}

async function refreshOnboardingState() {
  try {
    const res = await fetch("/api/onboarding/state", { cache: "no-store" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const body = await res.json();
    onboardingState.fetchError = null;
    renderOnboarding(body);
  } catch (err) {
    onboardingState.fetchError = String(err);
    paintPlatformCard("android", null);
    paintPlatformCard("ios", null);
    const meta = $("onboardingMeta");
    if (meta) {
      meta.textContent = `本機引導（onboarding）— 連不上 server（${err.message || err}），按「進到 Lab」仍可使用`;
      meta.dataset.error = "true";
    }
  }
}

function initOnboarding() {
  const overlay = $("onboardingOverlay");
  if (!overlay) return;
  // Allow opting out via ?skip-onboarding=1 or sessionStorage flag set by the
  // "先看看" button. Refresh resets the flag so users can always re-run the
  // wizard by reloading the page.
  const params = new URLSearchParams(window.location.search);
  const skip =
    params.get("skip-onboarding") === "1" ||
    (window.sessionStorage && window.sessionStorage.getItem("3cdog-perf:onboarding-skip") === "1");
  if (skip) {
    overlay.classList.add("is-hidden");
    return;
  }
  const enter = $("onboardingEnterLab");
  const skipBtn = $("onboardingSkip");
  if (enter) {
    enter.addEventListener("click", hideOnboardingOverlay);
  }
  if (skipBtn) {
    skipBtn.addEventListener("click", () => {
      try {
        if (window.sessionStorage) {
          window.sessionStorage.setItem("3cdog-perf:onboarding-skip", "1");
        }
      } catch (e) {
        // sessionStorage may be unavailable in some privacy modes; failing to
        // remember the skip preference is fine — the user just sees the wizard
        // next time, which is the safer default.
      }
      hideOnboardingOverlay();
    });
  }
  refreshOnboardingState();
  // Refresh once a second while the overlay is visible so users see the state
  // change the moment they plug in / tap "trust" / start tunneld. The timer
  // dies the first time the overlay is hidden.
  onboardingState.refreshTimer = setInterval(refreshOnboardingState, 1000);
}

// Mirror the server's BUNDLE_FRIENDLY_NAMES for the session list badge. We do
// not require the table to match exactly — the brief explicitly allows
// fallback to the raw bundle id for unmapped packages.
function friendlyBundleName(bundleId) {
  if (!bundleId) return null;
  return Object.prototype.hasOwnProperty.call(BUNDLE_FRIENDLY_NAMES, bundleId)
    ? BUNDLE_FRIENDLY_NAMES[bundleId]
    : null;
}

const COLORS = {
  fps: "#74f5c3",
  jank: "#ff9b73",
  cpu: "#72b7ff",
  gpu: "#b59cff",
  memory: "#f4d47c",
  power: "#ff86aa",
  temperature: "#ff9b73",
  core: ["#b59cff", "#f4d47c", "#72b7ff", "#ff86aa", "#56e39f", "#e9d758", "#718cff", "#f28482"],
};

const STATUS_LABELS = {
  ok: "採集中",
  app_background: "App 不在前景",
  device_error: "裝置錯誤",
  device_unauthorized: "裝置未授權",
  device_offline: "裝置離線",
  device_disconnected: "裝置已拔除",
  adb_timeout: "ADB 逾時",
  collector_error: "採集錯誤",
};

const METRICS = {
  fps: { value: "fps", reason: "fpsReason", field: "fps", suffix: " fps" },
  cpu: { value: "cpuNow", reason: "cpuReason", field: "cpu_total", suffix: "%" },
  gpu: { value: "gpuNow", reason: "gpuReason", field: "gpu", suffix: "%" },
  memory: { value: "memNow", reason: "memReason", field: "mem_pss_mb", suffix: " MB" },
  power: { value: "powerNow", reason: "powerReason", field: "battery_power_w", suffix: " W" },
  temperature: { value: "tempNow", reason: "tempReason", field: "temp_c", suffix: " °C" },
};
METRICS.fps.value = "fpsNow";

const EXPORT_FIELDS = [
  "schema_version", "session_id", "ts", "elapsed_s", "package", "device_serial", "status",
  "fps", "jank", "jank_pct", "frame_times_ms", "cpu_total", "cpu_per_core", "gpu", "mem_pss_mb",
  "battery_power_w", "temp_c", "sources", "errors",
];

const FPS_LAB_METRICS = [
  { tile: "avg", field: "avg_fps", valueId: "fpsLabAvg", reasonId: "fpsLabAvgReason", digits: 1, suffix: "" },
  { tile: "variance", field: "fps_variance", valueId: "fpsLabVar", reasonId: "fpsLabVarReason", digits: 2, suffix: "" },
  { tile: "jank", field: "jank", valueId: "fpsLabJank", reasonId: "fpsLabJankReason", digits: 0, suffix: "" },
  { tile: "bigjank", field: "bigjank", valueId: "fpsLabBigJank", reasonId: "fpsLabBigJankReason", digits: 0, suffix: "" },
  { tile: "stutter", field: "stutter_pct", valueId: "fpsLabStutter", reasonId: "fpsLabStutterReason", digits: 2, suffix: " %" },
  { tile: "low", field: "one_percent_low", valueId: "fpsLabLow", reasonId: "fpsLabLowReason", digits: 1, suffix: " fps" },
  { tile: "drop", field: "drop_count", valueId: "fpsLabDrop", reasonId: "fpsLabDropReason", digits: 0, suffix: "" },
];

const FPS_LAB_EXTRA = [
  { field: "max_fps", valueId: "fpsLabMax", digits: 1, suffix: " fps" },
  { field: "min_fps", valueId: "fpsLabMin", digits: 1, suffix: " fps" },
  { field: "one_tenth_low", valueId: "fpsLabTenthLow", digits: 1, suffix: " fps" },
  { field: "jank_per_10min", valueId: "fpsLabJankPer10", digits: 1, suffix: "" },
  { field: "bigjank_per_10min", valueId: "fpsLabBigJankPer10", digits: 1, suffix: "" },
  { field: "frames", valueId: "fpsLabFrames", digits: 0, suffix: "" },
  { field: "duration_s", valueId: "fpsLabDuration", digits: 1, suffix: " s" },
];

const FPS_LAB_DEFINITION_LABELS = {
  jank_count: "Jank 判定",
  bigjank_count: "BigJank 判定",
  jank_per_10min: "Jank / 10min",
  bigjank_per_10min: "BigJank / 10min",
  stutter_pct: "Stutter %",
  avg_fps: "Avg FPS",
  max_fps: "Max FPS",
  min_fps: "Min FPS",
  fps_variance: "FPS 方差",
  drop_count: "Drop (FPS)",
  one_percent_low: "1% low FPS",
  one_tenth_low: "0.1% low FPS",
};

function finite(value) {
  return typeof value === "number" && Number.isFinite(value);
}

function elapsed(record, fallback = 0) {
  return finite(record && record.elapsed_s) ? record.elapsed_s : fallback;
}

function formatDuration(seconds) {
  const total = Math.max(0, Math.floor(finite(seconds) ? seconds : 0));
  const hours = String(Math.floor(total / 3600)).padStart(2, "0");
  const minutes = String(Math.floor((total % 3600) / 60)).padStart(2, "0");
  const secs = String(total % 60).padStart(2, "0");
  return `${hours}:${minutes}:${secs}`;
}

function aliasesFor(field) {
  return {
    fps: ["fps", "frame", "jank"],
    cpu_total: ["cpu_total", "cpu", "proc/stat"],
    gpu: ["gpu"],
    mem_pss_mb: ["mem_pss_mb", "memory", "meminfo", "pss"],
    battery_power_w: ["battery_power_w", "battery", "power", "current", "voltage"],
    temp_c: ["temp_c", "temperature", "thermal", "skin"],
  }[field] || [field];
}

function unavailableReason(field, record) {
  const row = record || {};
  const aliases = aliasesFor(field);
  const errors = Array.isArray(row.errors) ? row.errors : [];
  const direct = errors.find((item) => (
    typeof item === "string" && aliases.some((alias) => item.toLowerCase().includes(alias))
  ));
  if (direct) return direct;

  const sources = row.sources && typeof row.sources === "object" ? row.sources : {};
  const sourceEntry = Object.entries(sources).find(([name]) => (
    aliases.some((alias) => name.toLowerCase().includes(alias))
  ));
  if (sourceEntry && sourceEntry[1]) return `來源 ${sourceEntry[1]} 暫無數值`;
  if (row.status && row.status !== "ok") return STATUS_LABELS[row.status] || `狀態：${row.status}`;
  if (state.status && state.status.reason) return state.status.reason;
  return state.records.length ? "來源尚未提供數值" : "等待 session 資料";
}

function sourceDetail(field, record) {
  const row = record || {};
  const sources = row.sources && typeof row.sources === "object" ? row.sources : {};
  const aliases = aliasesFor(field);
  const source = Object.entries(sources).find(([name]) => (
    aliases.some((alias) => name.toLowerCase().includes(alias))
  ));
  return source && source[1] ? String(source[1]) : "來源未標示";
}

function setConnection(kind, message) {
  const connection = $("connection");
  connection.classList.remove("connected", "error");
  if (kind === "connected") connection.classList.add("connected");
  if (kind === "error") connection.classList.add("error");
  connection.dataset.state = kind;
  $("connectionText").textContent = message;
}

function setNotice(message, error = false) {
  const alert = $("alert");
  alert.hidden = !message;
  alert.textContent = message || "";
  alert.classList.toggle("error", error);
}

async function api(path) {
  const response = await fetch(path, { cache: "no-store" });
  let body;
  try {
    body = await response.json();
  } catch (_) {
    body = { error: `HTTP ${response.status}` };
  }
  if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`);
  return body;
}

function sessionQuery(endpoint, name, params = {}) {
  const query = new URLSearchParams({ name, ...params });
  return `${endpoint}?${query.toString()}`;
}

function clearPollTimer() {
  clearTimeout(state.pollTimer);
  state.pollTimer = null;
}

function stopReplay() {
  clearTimeout(state.replayTimer);
  state.replayTimer = null;
  state.replayPlaying = false;
  $("playButton").textContent = "▶";
  $("playButton").setAttribute("aria-label", "播放重播");
}

async function listSessions(preserve = true) {
  try {
    const data = await api("/api/sessions");
    const sessions = Array.isArray(data.sessions) ? data.sessions : [];
    const previous = preserve ? state.name : "";
    sessionSelect.replaceChildren(new Option(sessions.length ? "選擇 session" : "沒有 session JSONL", ""));
    sessions.forEach((item) => {
      const identity = item.session_id || item.name;
      const target = item.package || "unknown package";
      // Phase 2: prepend a platform badge so the user can tell iOS captures
      // apart from Android ones in the same list. Default to Android when
      // the server hasn't stamped the field (legacy rows from older
      // collectors).
      const badge = item.platform === "ios" ? "[iOS]" : "[Android]";
      const friendly = item.platform === "ios" ? friendlyBundleName(item.package) : null;
      const targetLabel = friendly ? `${friendly} (${target})` : target;
      sessionSelect.add(new Option(`${badge} ${identity} · ${targetLabel}`, item.name));
    });
    if (previous && sessions.some((item) => item.name === previous)) {
      sessionSelect.value = previous;
    } else if (sessions.length) {
      sessionSelect.value = sessions[0].name;
      await loadSession(sessionSelect.value);
    } else {
      await loadSession("");
      setConnection("idle", "等待 session");
      setNotice("尚無 session 資料。先啟動 collector 後再重新掃描。");
    }
  } catch (error) {
    setConnection("error", "本機服務斷線");
    setNotice(`無法取得 session：${error.message}`, true);
  }
}

async function refreshStatus(generation) {
  if (!state.name) return;
  try {
    const status = await api(sessionQuery("/api/status", state.name));
    if (generation !== state.generation) return;
    state.status = status;
    const healthy = status.connected && !["device_error", "device_offline", "device_disconnected", "device_unauthorized"].includes(status.status);
    setConnection(healthy ? "connected" : "error", STATUS_LABELS[status.status] || status.status || "狀態未知");
  } catch (error) {
    if (generation !== state.generation) return;
    setConnection("error", `狀態讀取失敗：${error.message}`);
  }
}

async function fetchChunk(generation) {
  if (!state.name || generation !== state.generation) return { eof: true, progressed: false };
  const before = state.offset;
  const data = await api(sessionQuery("/api/session", state.name, { offset: before, limit: 5000 }));
  if (generation !== state.generation) return { eof: true, progressed: false };

  const nextOffset = Number(data.next_offset);
  const fileSize = Number(data.file_size);
  if (!Number.isFinite(nextOffset) || !Number.isFinite(fileSize) || !Array.isArray(data.records)) {
    throw new Error("session API response shape 不符");
  }
  if (before && nextOffset < before) state.records = [];
  state.records.push(...data.records);
  state.offset = nextOffset;
  state.fileSize = fileSize;
  state.warnings = Array.isArray(data.warnings) ? data.warnings : [];
  state.failures = 0;
  return { eof: Boolean(data.eof), progressed: nextOffset > before };
}

function updateNotice() {
  const warning = state.warnings.length ? ` · ${state.warnings.join("；")}` : "";
  setNotice(
    `${state.records.length} 筆 · 已讀 ${state.offset}/${state.fileSize} bytes${warning}`,
    state.warnings.length > 0,
  );
}

function schedulePoll(delay = 1000) {
  clearPollTimer();
  if (state.mode === "live" && state.name) {
    state.pollTimer = setTimeout(pollLive, delay);
  }
}

async function pollLive() {
  clearPollTimer();
  if (state.mode !== "live" || !state.name) return;
  const generation = state.generation;
  try {
    const chunk = await fetchChunk(generation);
    if (generation !== state.generation) return;
    await refreshStatus(generation);
    updateNotice();
    render();
    schedulePoll(chunk.eof || !chunk.progressed ? 1000 : 0);
  } catch (error) {
    if (generation !== state.generation) return;
    state.failures += 1;
    if (/offset is outside session file/i.test(error.message) && state.offset > 0) {
      state.records = [];
      state.offset = 0;
      state.fileSize = 0;
      schedulePoll(0);
      return;
    }
    setConnection("error", "資料流斷線");
    setNotice(`讀取中斷：${error.message} · 將自動重試`, true);
    render();
    schedulePoll(Math.min(5000, 1000 * state.failures));
  }
}

async function loadSnapshot(generation) {
  try {
    let chunk;
    do {
      chunk = await fetchChunk(generation);
    } while (generation === state.generation && !chunk.eof && chunk.progressed);
    if (generation !== state.generation) return;
    await refreshStatus(generation);
    state.replayIndex = 0;
    updateNotice();
    render();
  } catch (error) {
    if (generation !== state.generation) return;
    setConnection("error", "Session 載入失敗");
    setNotice(`無法載入 session：${error.message}`, true);
    render();
  }
}

async function loadSession(name) {
  clearPollTimer();
  stopReplay();
  state.generation += 1;
  state.name = name;
  state.records = [];
  state.offset = 0;
  state.fileSize = 0;
  state.failures = 0;
  state.warnings = [];
  state.status = null;
  state.replayIndex = 0;
  render();
  if (!name) return;
  setNotice(`載入 ${name}…`);
  const generation = state.generation;
  if (state.mode === "live") await pollLive();
  else await loadSnapshot(generation);
}

function displayRecords() {
  if (state.mode === "live") return state.records;
  return state.records.slice(0, Math.min(state.records.length, state.replayIndex + 1));
}

function series(records, field) {
  return records
    .map((row, index) => ({ x: elapsed(row, index), y: row[field] }))
    .filter((point) => finite(point.y));
}

function drawChart(canvas, lineSets, domain) {
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  canvas.width = Math.max(1, Math.round(rect.width * dpr));
  canvas.height = Math.max(1, Math.round(rect.height * dpr));
  const context = canvas.getContext("2d");
  context.setTransform(dpr, 0, 0, dpr, 0, 0);
  const width = rect.width;
  const height = rect.height;
  const padding = { left: 38, right: 9, top: 10, bottom: 22 };
  context.clearRect(0, 0, width, height);
  const points = lineSets.flatMap((item) => item.points);
  if (!points.length) return;

  let [xMin, xMax] = domain;
  const ys = points.map((point) => point.y);
  let yMin = Math.min(0, ...ys);
  let yMax = Math.max(...ys);
  if (xMax === xMin) xMax = xMin + 1;
  if (yMax === yMin) yMax = yMin + 1;
  const plotWidth = Math.max(1, width - padding.left - padding.right);
  const plotHeight = Math.max(1, height - padding.top - padding.bottom);
  const xPixel = (x) => padding.left + ((x - xMin) / (xMax - xMin)) * plotWidth;
  const yPixel = (y) => padding.top + (1 - (y - yMin) / (yMax - yMin)) * plotHeight;

  context.font = "9px ui-monospace, monospace";
  context.fillStyle = "#718097";
  context.strokeStyle = "#263247";
  context.lineWidth = 1;
  for (let index = 0; index < 4; index += 1) {
    const y = padding.top + (index * plotHeight) / 3;
    context.beginPath();
    context.moveTo(padding.left, y);
    context.lineTo(width - padding.right, y);
    context.stroke();
    const value = yMax - (index * (yMax - yMin)) / 3;
    context.fillText(value >= 100 ? value.toFixed(0) : value.toFixed(1), 1, y + 3);
  }
  context.fillText(`${xMin.toFixed(1)}s`, padding.left, height - 4);
  const endLabel = `${xMax.toFixed(1)}s`;
  context.fillText(endLabel, width - padding.right - context.measureText(endLabel).width, height - 4);

  lineSets.forEach((line) => {
    if (!line.points.length) return;
    context.beginPath();
    context.strokeStyle = line.color;
    context.lineWidth = line.width || 1.8;
    context.globalAlpha = line.alpha || 1;
    line.points.forEach((point, index) => {
      const x = xPixel(point.x);
      const y = yPixel(point.y);
      if (index) context.lineTo(x, y);
      else context.moveTo(x, y);
    });
    context.stroke();
  });
  context.globalAlpha = 1;
}

function formatMetric(value, suffix) {
  if (!finite(value)) return "N/A";
  const precision = Math.abs(value) >= 100 ? 0 : 1;
  return `${value.toFixed(precision)}${suffix}`;
}

function updateSummary(key, record) {
  const metric = METRICS[key];
  const value = record[metric.field];
  $(metric.value).textContent = formatMetric(value, metric.suffix);
  $(metric.reason).textContent = finite(value)
    ? sourceDetail(metric.field, record)
    : unavailableReason(metric.field, record);
}

function updateCard(key, latest, lines, detail, missingField, domain) {
  const card = cards[key];
  const visible = displayRecords();
  const reasonRecord = visible[visible.length - 1];
  const hasHistory = lines.some((line) => line.points.length);
  card.classList.toggle("has-data", hasHistory);
  card.querySelector("[data-instant]").textContent = finite(latest)
    ? (Math.abs(latest) >= 100 ? latest.toFixed(0) : latest.toFixed(1))
    : "N/A";
  card.querySelector("[data-detail]").textContent = finite(latest)
    ? detail
    : `N/A · ${unavailableReason(missingField, reasonRecord)}`;
  card.querySelector(".empty").textContent = hasHistory ? "" : `N/A · ${unavailableReason(missingField, reasonRecord)}`;
  drawChart(card.querySelector("canvas"), lines, domain);
}

function renderTimeline(records) {
  const replay = state.mode === "replay";
  const max = Math.max(0, state.records.length - 1);
  const index = replay ? Math.min(state.replayIndex, max) : max;
  const scrubber = $("scrubber");
  scrubber.max = String(max);
  scrubber.value = String(index);
  scrubber.disabled = !replay || state.records.length < 2;
  $("playButton").disabled = !replay || state.records.length < 2;
  $("speedSelect").disabled = !replay;
  const latest = records[records.length - 1];
  $("timelineLabel").textContent = `${state.records.length ? index + 1 : 0} / ${state.records.length} samples · ${elapsed(latest).toFixed(1)}s`;
}

// Identity strip (device/package/session/elapsed) and the REC clock live
// outside both view sections (they're in the always-visible header/sidebar),
// but their previous home inside render() only ran while the Overview view
// was active. Switching to FPS Lab called renderFpsLab() instead, so the REC
// dot/timer froze mid-recording — looking exactly like recording had
// stopped even though the collector subprocess and status polling kept
// running untouched. Both view render paths must call this so the clock
// keeps ticking regardless of which view is on screen.
function updateIdentityStrip() {
  const records = displayRecords();
  const last = records[records.length - 1] || {};
  $("deviceValue").textContent = last.device_serial || (state.status && state.status.device) || "—";
  $("packageValue").textContent = last.package || (state.status && state.status.package) || "—";
  $("sessionValue").textContent = last.session_id || (state.status && state.status.session) || state.name || "—";
  $("elapsedValue").textContent = formatDuration(elapsed(last));
  $("recordTimer").textContent = formatDuration(elapsed(last));
  $("statusValue").textContent = STATUS_LABELS[last.status] || last.status || "NO DATA";
  $("recordClock").dataset.active = String(state.mode === "live" && last.status === "ok");
}

function render() {
  const records = displayRecords();
  const last = records[records.length - 1] || {};
  const xStart = records.length ? elapsed(records[0], 0) : 0;
  const xEnd = records.length ? elapsed(last, records.length - 1) : 1;
  const domain = [xStart, xEnd];

  updateIdentityStrip();

  const enabled = state.records.length > 0;
  $("jsonButton").disabled = !enabled;
  $("csvButton").disabled = !enabled;
  $("fpsSummaryButton").disabled = !enabled;
  Object.keys(METRICS).forEach((key) => updateSummary(key, last));
  renderTimeline(records);

  updateCard(
    "frame",
    last.fps,
    [
      { points: series(records, "fps"), color: COLORS.fps, width: 2.2 },
      { points: series(records, "jank_pct"), color: COLORS.jank },
    ],
    `Jank ${finite(last.jank) ? last.jank : "N/A"} · ${finite(last.jank_pct) ? `${last.jank_pct.toFixed(1)}%` : "N/A"}`,
    "fps",
    domain,
  );

  const coreNames = new Set();
  records.forEach((row) => Object.keys(row.cpu_per_core || {}).forEach((name) => coreNames.add(name)));
  const sortedCores = [...coreNames].sort((a, b) => a.localeCompare(b, undefined, { numeric: true }));
  const coreLines = sortedCores.map((name, index) => ({
    points: records
      .map((row, recordIndex) => ({ x: elapsed(row, recordIndex), y: (row.cpu_per_core || {})[name] }))
      .filter((point) => finite(point.y)),
    color: COLORS.core[index % COLORS.core.length],
    alpha: 0.65,
    width: 1,
  }));
  $("cpuLegend").replaceChildren(...sortedCores.map((name, index) => {
    const chip = document.createElement("span");
    chip.style.setProperty("--c", COLORS.core[index % COLORS.core.length]);
    chip.textContent = name;
    return chip;
  }));
  updateCard("cpu", last.cpu_total, [
    { points: series(records, "cpu_total"), color: COLORS.cpu, width: 2.3 },
    ...coreLines,
  ], `${sortedCores.length} 核心 · ${sourceDetail("cpu_total", last)}`, "cpu_total", domain);
  updateCard("gpu", last.gpu, [{ points: series(records, "gpu"), color: COLORS.gpu }], sourceDetail("gpu", last), "gpu", domain);
  updateCard("memory", last.mem_pss_mb, [{ points: series(records, "mem_pss_mb"), color: COLORS.memory }], sourceDetail("mem_pss_mb", last), "mem_pss_mb", domain);
  updateCard("power", last.battery_power_w, [{ points: series(records, "battery_power_w"), color: COLORS.power }], `battery-side net · ${sourceDetail("battery_power_w", last)}`, "battery_power_w", domain);
  updateCard("temperature", last.temp_c, [{ points: series(records, "temp_c"), color: COLORS.temperature }], sourceDetail("temp_c", last), "temp_c", domain);
  if (state.view === "fpsLab") renderFpsLab();
}

async function setMode(mode) {
  if (mode === state.mode) return;
  clearPollTimer();
  stopReplay();
  state.mode = mode;
  $("liveButton").classList.toggle("active", mode === "live");
  $("replayButton").classList.toggle("active", mode === "replay");
  $("liveButton").setAttribute("aria-pressed", String(mode === "live"));
  $("replayButton").setAttribute("aria-pressed", String(mode === "replay"));
  if (mode === "live") {
    render();
    await pollLive();
  } else {
    state.replayIndex = 0;
    await loadSnapshot(state.generation);
  }
}

function replayStep() {
  clearTimeout(state.replayTimer);
  if (!state.replayPlaying || state.mode !== "replay") return;
  if (state.replayIndex >= state.records.length - 1) {
    stopReplay();
    render();
    return;
  }
  const current = state.records[state.replayIndex];
  const next = state.records[state.replayIndex + 1];
  const delay = Math.max(50, Math.min(2000, ((elapsed(next) - elapsed(current)) * 1000) / state.replaySpeed));
  state.replayTimer = setTimeout(() => {
    state.replayIndex += 1;
    render();
    replayStep();
  }, finite(delay) ? delay : 1000 / state.replaySpeed);
}

function toggleReplay() {
  if (state.mode !== "replay" || state.records.length < 2) return;
  if (state.replayPlaying) {
    stopReplay();
  } else {
    if (state.replayIndex >= state.records.length - 1) state.replayIndex = 0;
    state.replayPlaying = true;
    $("playButton").textContent = "❚❚";
    $("playButton").setAttribute("aria-label", "暫停重播");
    render();
    replayStep();
  }
}

function escapeCsv(value) {
  const text = value == null ? "" : typeof value === "object" ? JSON.stringify(value) : String(value);
  return /[",\n]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text;
}

function download(type) {
  const content = type === "json"
    ? JSON.stringify(state.records, null, 2)
    : [EXPORT_FIELDS.join(","), ...state.records.map((row) => EXPORT_FIELDS.map((key) => escapeCsv(row[key])).join(","))].join("\n");
  const mime = type === "json" ? "application/json" : "text/csv;charset=utf-8";
  const blob = new Blob([content], { type: mime });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `${(state.name || "session").replace(/\.jsonl$/i, "")}.${type}`;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}

// ----- FPS Lab -----

function flattenFrameTimes(records) {
  const result = [];
  for (const row of records) {
    const frames = Array.isArray(row.frame_times_ms) ? row.frame_times_ms : [];
    for (const value of frames) {
      if (typeof value === "number" && Number.isFinite(value) && value > 0) {
        result.push(value);
      }
    }
  }
  return result;
}

function perSecondFpsFromFrames(records) {
  const out = [];
  for (const row of records) {
    const frames = Array.isArray(row.frame_times_ms) ? row.frame_times_ms : [];
    const clean = frames.filter((value) => typeof value === "number" && Number.isFinite(value) && value > 0);
    if (!clean.length) continue;
    const total = clean.reduce((acc, value) => acc + value, 0);
    if (total <= 0) continue;
    out.push({ x: elapsed(row, out.length), y: 1000 * clean.length / total, frames: clean });
  }
  return out;
}

function fpsSummary(records) {
  const intervals = flattenFrameTimes(records);
  const perSecond = perSecondFpsFromFrames(records);
  const durations = records
    .map((row) => (typeof row.elapsed_s === "number" ? row.elapsed_s : null))
    .filter((value) => value !== null);
  const observedDuration = durations.length ? Math.max(...durations) - Math.min(...durations) : null;
  const totalIntervalsMs = intervals.reduce((acc, value) => acc + value, 0);
  const totalDuration = observedDuration !== null ? observedDuration : totalIntervalsMs / 1000;

  // PerfDog-style Jank classification: frame > 2× previous-three-frames avg
  // AND > 83.33 ms (jank) / > 125 ms (bigjank).
  const JANK_MS = 83.33;
  const BIGJANK_MS = 125.0;
  let jank = 0;
  let bigjank = 0;
  let jankMs = 0;
  for (let index = 3; index < intervals.length; index += 1) {
    const baseline = (intervals[index - 3] + intervals[index - 2] + intervals[index - 1]) / 3;
    const frameMs = intervals[index];
    if (frameMs > baseline * 2 && frameMs > JANK_MS) {
      jank += 1;
      jankMs += frameMs;
      if (frameMs > BIGJANK_MS) bigjank += 1;
    }
  }

  const fpsValues = perSecond.map((point) => point.y);
  const avgFps = totalIntervalsMs > 0 ? (1000 * intervals.length) / totalIntervalsMs : null;
  const maxFps = fpsValues.length ? Math.max(...fpsValues) : null;
  const minFps = fpsValues.length ? Math.min(...fpsValues) : null;
  const variance = fpsValues.length > 1
    ? fpsValues.reduce((acc, value) => acc + Math.pow(value - avgFps, 2), 0) / fpsValues.length
    : null;

  // Drop count: adjacent-second FPS drop >= 8.
  let dropCount = 0;
  for (let index = 1; index < fpsValues.length; index += 1) {
    if (fpsValues[index - 1] - fpsValues[index] >= 8) dropCount += 1;
  }

  // 1% / 0.1% low FPS (slowest frame-time tail → FPS).
  function percentileLow(pct) {
    if (!intervals.length) return null;
    const sorted = [...intervals].sort((a, b) => a - b);
    const tailSize = Math.max(1, Math.ceil(sorted.length * pct));
    const tail = sorted.slice(-tailSize);
    const avg = tail.reduce((acc, value) => acc + value, 0) / tail.length;
    return avg > 0 ? 1000 / avg : null;
  }
  const onePctLow = percentileLow(0.01);
  const tenthPctLow = percentileLow(0.001);

  const scale = totalDuration > 0 ? 600 / totalDuration : null;
  const jankPer10min = scale !== null ? jank * scale : null;
  const bigjankPer10min = scale !== null ? bigjank * scale : null;
  const stutterPct = totalIntervalsMs > 0 ? (100 * jankMs) / totalIntervalsMs : null;

  return {
    frames: intervals.length,
    duration_s: Number.isFinite(totalDuration) ? totalDuration : null,
    avg_fps: avgFps,
    max_fps: maxFps,
    min_fps: minFps,
    fps_variance: variance,
    jank,
    bigjank,
    jank_per_10min: jankPer10min,
    bigjank_per_10min: bigjankPer10min,
    stutter_pct: stutterPct,
    one_percent_low: onePctLow,
    one_tenth_low: tenthPctLow,
    drop_count: dropCount,
    has_frames: intervals.length > 0,
  };
}

function fpsLabDefinitions() {
  return {
    jank_count: "單�耗時 > 前三幀平均×2 且 > 83.33 ms（兩個電影幀）",
    bigjank_count: "單幀耗時 > 前三幀平均×2 且 > 125 ms（三個電影幀）",
    jank_per_10min: "jank 次數按 600 秒比例換算（jank / duration_s × 600）",
    bigjank_per_10min: "bigjank 次數按 600 秒比例換算（bigjank / duration_s × 600）",
    stutter_pct: "jank 幀累計耗時 ÷ 總時長",
    avg_fps: "平均 FPS（總幀數 / 總時長）",
    max_fps: "最高每秒 FPS（由 frame_times_ms 反推）",
    min_fps: "最低每秒 FPS（由 frame_times_ms 反推）",
    fps_variance: "FPS 樣本方差（n<2 回傳 N/A）",
    drop_count: "相鄰秒 FPS 下降 ≥ 8 的次數",
    one_percent_low: "1% low FPS（幀耗時排序尾部 1% 換算）",
    one_tenth_low: "0.1% low FPS（幀耗時排序尾部 0.1% 換算）",
  };
}

function renderFpsLabTiles(summary, hasRecords) {
  if (!hasRecords) {
    for (const item of FPS_LAB_METRICS) {
      $(item.valueId).textContent = "—";
      $(item.reasonId).textContent = "等待資料";
    }
    for (const item of FPS_LAB_EXTRA) {
      $(item.valueId).textContent = "—";
    }
    return;
  }
  if (!summary.has_frames) {
    for (const item of FPS_LAB_METRICS) {
      $(item.valueId).textContent = "—";
      $(item.reasonId).textContent = "此 session 無逐幀資料";
    }
    for (const item of FPS_LAB_EXTRA) {
      $(item.valueId).textContent = "—";
    }
    return;
  }
  for (const item of FPS_LAB_METRICS) {
    const value = summary[item.field];
    if (value === null || value === undefined || !Number.isFinite(value)) {
      $(item.valueId).textContent = "N/A";
      $(item.reasonId).textContent = "資料不足";
    } else {
      const fixed = value.toFixed(item.digits);
      $(item.valueId).textContent = `${fixed}${item.suffix}`;
      $(item.reasonId).textContent = "本工具口徑";
    }
  }
  for (const item of FPS_LAB_EXTRA) {
    const value = summary[item.field];
    if (value === null || value === undefined || !Number.isFinite(value)) {
      $(item.valueId).textContent = "—";
    } else {
      const fixed = value.toFixed(item.digits);
      $(item.valueId).textContent = `${fixed}${item.suffix}`;
    }
  }
}

function drawFpsCurve(canvas, perSecond, avgFps) {
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  canvas.width = Math.max(1, Math.round(rect.width * dpr));
  canvas.height = Math.max(1, Math.round(rect.height * dpr));
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  const width = rect.width;
  const height = rect.height;
  const padding = { left: 38, right: 9, top: 10, bottom: 22 };
  ctx.clearRect(0, 0, width, height);
  if (!perSecond.length) return;
  const xs = perSecond.map((point) => point.x);
  const xMin = Math.min(...xs);
  const xMax = Math.max(...xs);
  const ys = perSecond.map((point) => point.y);
  let yMin = 0;
  let yMax = Math.max(60, ...ys) * 1.05;
  if (xMax === xMin) xMax = xMin + 1;
  if (yMax === yMin) yMax = yMin + 1;
  const plotWidth = Math.max(1, width - padding.left - padding.right);
  const plotHeight = Math.max(1, height - padding.top - padding.bottom);
  const xPixel = (x) => padding.left + ((x - xMin) / (xMax - xMin)) * plotWidth;
  const yPixel = (y) => padding.top + (1 - (y - yMin) / (yMax - yMin)) * plotHeight;

  ctx.font = "9px ui-monospace, monospace";
  ctx.fillStyle = "#718097";
  ctx.strokeStyle = "#263247";
  ctx.lineWidth = 1;
  for (let index = 0; index < 4; index += 1) {
    const y = padding.top + (index * plotHeight) / 3;
    ctx.beginPath();
    ctx.moveTo(padding.left, y);
    ctx.lineTo(width - padding.right, y);
    ctx.stroke();
    const value = yMax - (index * (yMax - yMin)) / 3;
    ctx.fillText(value >= 100 ? value.toFixed(0) : value.toFixed(1), 1, y + 3);
  }
  ctx.fillText(`${xMin.toFixed(1)}s`, padding.left, height - 4);
  const endLabel = `${xMax.toFixed(1)}s`;
  ctx.fillText(endLabel, width - padding.right - ctx.measureText(endLabel).width, height - 4);

  // Jank frames as red dots.
  for (const point of perSecond) {
    const jankFrames = point.frames.filter((value) => value > 83.33);
    if (!jankFrames.length) continue;
    const worst = Math.max(...jankFrames);
    if (worst <= 83.33) continue;
    ctx.fillStyle = "#ff9b73";
    ctx.beginPath();
    ctx.arc(xPixel(point.x), yPixel(point.y), 3, 0, Math.PI * 2);
    ctx.fill();
  }
  // Avg dashed reference line.
  if (avgFps !== null && Number.isFinite(avgFps) && avgFps >= yMin && avgFps <= yMax) {
    ctx.strokeStyle = "#ff86aa";
    ctx.setLineDash([4, 4]);
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(padding.left, yPixel(avgFps));
    ctx.lineTo(width - padding.right, yPixel(avgFps));
    ctx.stroke();
    ctx.setLineDash([]);
  }
  // FPS curve.
  ctx.strokeStyle = "#74f5c3";
  ctx.lineWidth = 2;
  ctx.beginPath();
  perSecond.forEach((point, index) => {
    const x = xPixel(point.x);
    const y = yPixel(point.y);
    if (index) ctx.lineTo(x, y);
    else ctx.moveTo(x, y);
  });
  ctx.stroke();
}

function drawFrameTime(canvas, perSecond) {
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  canvas.width = Math.max(1, Math.round(rect.width * dpr));
  canvas.height = Math.max(1, Math.round(rect.height * dpr));
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  const width = rect.width;
  const height = rect.height;
  const padding = { left: 38, right: 9, top: 10, bottom: 22 };
  ctx.clearRect(0, 0, width, height);
  if (!perSecond.length) return;
  const xs = [];
  for (const point of perSecond) {
    for (let index = 0; index < point.frames.length; index += 1) {
      xs.push(point.x + (index / point.frames.length));
    }
  }
  if (!xs.length) return;
  const xMin = Math.min(...xs);
  const xMax = Math.max(...xs);
  const allFrames = perSecond.flatMap((point) => point.frames);
  let yMin = 0;
  let yMax = Math.max(125, ...allFrames) * 1.05;
  if (xMax === xMin) xMax = xMin + 1;
  if (yMax === yMin) yMax = yMin + 1;
  const plotWidth = Math.max(1, width - padding.left - padding.right);
  const plotHeight = Math.max(1, height - padding.top - padding.bottom);
  const xPixel = (x) => padding.left + ((x - xMin) / (xMax - xMin)) * plotWidth;
  const yPixel = (y) => padding.top + (1 - (y - yMin) / (yMax - yMin)) * plotHeight;

  ctx.font = "9px ui-monospace, monospace";
  ctx.fillStyle = "#718097";
  ctx.strokeStyle = "#263247";
  ctx.lineWidth = 1;
  for (let index = 0; index < 4; index += 1) {
    const y = padding.top + (index * plotHeight) / 3;
    ctx.beginPath();
    ctx.moveTo(padding.left, y);
    ctx.lineTo(width - padding.right, y);
    ctx.stroke();
    const value = yMax - (index * (yMax - yMin)) / 3;
    ctx.fillText(value >= 100 ? value.toFixed(0) : value.toFixed(1) + " ms", 1, y + 3);
  }
  ctx.fillText(`${xMin.toFixed(1)}s`, padding.left, height - 4);
  const endLabel = `${xMax.toFixed(1)}s`;
  ctx.fillText(endLabel, width - padding.right - ctx.measureText(endLabel).width, height - 4);

  // 125 ms threshold (dashed pink).
  if (125 >= yMin && 125 <= yMax) {
    ctx.strokeStyle = "#ff86aa";
    ctx.setLineDash([5, 3]);
    ctx.beginPath();
    ctx.moveTo(padding.left, yPixel(125));
    ctx.lineTo(width - padding.right, yPixel(125));
    ctx.stroke();
    ctx.setLineDash([]);
  }
  // 83.33 ms threshold (dashed orange).
  if (83.33 >= yMin && 83.33 <= yMax) {
    ctx.strokeStyle = "#ff9b73";
    ctx.setLineDash([3, 3]);
    ctx.beginPath();
    ctx.moveTo(padding.left, yPixel(83.33));
    ctx.lineTo(width - padding.right, yPixel(83.33));
    ctx.stroke();
    ctx.setLineDash([]);
  }

  // Frame time bars per frame (one pixel wide), color-coded.
  const barWidth = Math.max(0.5, (plotWidth / xs.length) * 0.85);
  for (const point of perSecond) {
    point.frames.forEach((frameMs, index) => {
      const x = xPixel(point.x + (index / point.frames.length));
      const y = yPixel(frameMs);
      ctx.fillStyle = frameMs > 125 ? "#ff86aa" : frameMs > 83.33 ? "#ff9b73" : "#74f5c3";
      ctx.fillRect(x, y, barWidth, Math.max(1, height - padding.bottom - y));
    });
  }
}

function drawHistogram(canvas, frames, bins) {
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  canvas.width = Math.max(1, Math.round(rect.width * dpr));
  canvas.height = Math.max(1, Math.round(rect.height * dpr));
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  const width = rect.width;
  const height = rect.height;
  const padding = { left: 38, right: 9, top: 10, bottom: 22 };
  ctx.clearRect(0, 0, width, height);
  if (!frames.length) return;
  const maxMs = Math.max(125, ...frames);
  const step = maxMs / bins;
  const counts = new Array(bins).fill(0);
  for (const frame of frames) {
    let index = Math.floor(frame / step);
    if (index >= bins) index = bins - 1;
    counts[index] += 1;
  }
  const countMax = Math.max(...counts, 1);
  const plotWidth = Math.max(1, width - padding.left - padding.right);
  const plotHeight = Math.max(1, height - padding.top - padding.bottom);
  const barWidth = plotWidth / bins;

  ctx.font = "9px ui-monospace, monospace";
  ctx.fillStyle = "#718097";
  ctx.strokeStyle = "#263247";
  ctx.lineWidth = 1;
  for (let index = 0; index < 4; index += 1) {
    const y = padding.top + (index * plotHeight) / 3;
    ctx.beginPath();
    ctx.moveTo(padding.left, y);
    ctx.lineTo(width - padding.right, y);
    ctx.stroke();
    const value = countMax - (index * countMax) / 3;
    ctx.fillText(value.toFixed(0), 1, y + 3);
  }
  ctx.fillText("0 ms", padding.left, height - 4);
  ctx.fillText(`${maxMs.toFixed(0)} ms`, width - padding.right - ctx.measureText(`${maxMs.toFixed(0)} ms`).width, height - 4);

  for (let index = 0; index < bins; index += 1) {
    const count = counts[index];
    const x = padding.left + index * barWidth;
    const h = (count / countMax) * plotHeight;
    const msEnd = (index + 1) * step;
    ctx.fillStyle = msEnd > 125 ? "#ff86aa" : msEnd > 83.33 ? "#ff9b73" : "#74f5c3";
    ctx.fillRect(x + 1, padding.top + plotHeight - h, Math.max(0.5, barWidth - 2), h);
  }
}

function renderFpsLabCharts(summary, perSecond, frames) {
  const cardFps = document.querySelector('[data-fps-chart="fps"]');
  const cardFrame = document.querySelector('[data-fps-chart="frametime"]');
  const cardHist = document.querySelector('[data-fps-chart="histogram"]');
  if (cardFps) {
    cardFps.classList.toggle("has-data", perSecond.length > 0);
    const empty = cardFps.querySelector(".empty");
    if (empty) empty.textContent = perSecond.length ? "" : "N/A · 此 session 無逐幀資料";
    drawFpsCurve(cardFps.querySelector("canvas"), perSecond, summary ? summary.avg_fps : null);
  }
  if (cardFrame) {
    cardFrame.classList.toggle("has-data", perSecond.length > 0);
    const empty = cardFrame.querySelector(".empty");
    if (empty) empty.textContent = perSecond.length ? "" : "N/A · 此 session 無逐幀資料";
    drawFrameTime(cardFrame.querySelector("canvas"), perSecond);
  }
  if (cardHist) {
    cardHist.classList.toggle("has-data", frames.length > 0);
    const empty = cardHist.querySelector(".empty");
    if (empty) empty.textContent = frames.length ? "" : "N/A · 此 session 無逐幀資料";
    drawHistogram(cardHist.querySelector("canvas"), frames, 40);
  }
}

function renderFpsLabDefinitions() {
  const list = $("fpsLabDefinitions");
  if (!list) return;
  list.replaceChildren();
  const defs = fpsLabDefinitions();
  for (const [key, label] of Object.entries(FPS_LAB_DEFINITION_LABELS)) {
    const item = document.createElement("li");
    const code = document.createElement("code");
    code.textContent = label;
    const text = document.createElement("span");
    text.textContent = defs[key] || "—";
    item.appendChild(code);
    item.appendChild(text);
    list.appendChild(item);
  }
}

function renderFpsLabAlert(records) {
  const alert = $("fpsLabAlert");
  if (!alert) return;
  const hasAny = records.some((row) => Array.isArray(row.frame_times_ms) && row.frame_times_ms.length);
  if (!records.length) {
    alert.hidden = true;
    alert.textContent = "";
    return;
  }
  if (!hasAny) {
    alert.hidden = false;
    alert.textContent = "此 session 無逐幀資料（frame_times_ms 缺欄位），FPS Lab 圖表以空態呈現。";
    return;
  }
  alert.hidden = true;
  alert.textContent = "";
}

function copyFpsSummaryToClipboard() {
  if (!state.records.length) return;
  const summary = fpsSummary(state.records);
  const defs = fpsLabDefinitions();
  const payload = {
    session: state.name || null,
    package: state.records[0].package || null,
    device: state.records[0].device_serial || null,
    metrics: summary,
    definitions: defs,
    note: "本工具口�；不宣稱與 PerfDog 數字逐位相等",
  };
  const text = JSON.stringify(payload, null, 2);
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(
      () => setNotice("FPS Summary 已複製到剪貼簿"),
      () => setNotice("複製失敗；請改用�覽器開發者工具", true),
    );
  } else {
    const area = document.createElement("textarea");
    area.value = text;
    document.body.appendChild(area);
    area.select();
    try {
      document.execCommand("copy");
      setNotice("FPS Summary 已複製到剪貼簿");
    } catch (_) {
      setNotice("複製失敗", true);
    } finally {
      area.remove();
    }
  }
}

function renderFpsLab() {
  updateIdentityStrip();
  const records = displayRecords();
  const summary = fpsSummary(records);
  const perSecond = perSecondFpsFromFrames(records);
  const frames = flattenFrameTimes(records);
  renderFpsLabAlert(records);
  renderFpsLabTiles(summary, records.length > 0);
  renderFpsLabCharts(summary, perSecond, frames);
}

// ----- View switcher -----

// NOTE: this function must never call /api/record/stop or apiPost(...stop...)
// in any form. Switching views is not a stop action — recording, the live
// poll loop (keyed off state.mode, not state.view), and the 4s
// refreshRecordStatus() interval all keep running untouched underneath.
function setView(view) {
  if (view === state.view) return;
  state.view = view;
  const overviewTab = $("overviewTab");
  const fpsLabTab = $("fpsLabTab");
  overviewTab.classList.toggle("active", view === "overview");
  fpsLabTab.classList.toggle("active", view === "fpsLab");
  overviewTab.setAttribute("aria-selected", String(view === "overview"));
  fpsLabTab.setAttribute("aria-selected", String(view === "fpsLab"));
  $("overviewView").classList.toggle("active", view === "overview");
  $("overviewView").hidden = view !== "overview";
  $("fpsLabView").classList.toggle("active", view === "fpsLab");
  $("fpsLabView").hidden = view !== "fpsLab";
  if (view === "fpsLab") {
    renderFpsLab();
  } else {
    render();
    // Re-pull /api/record/status immediately on return so the record
    // button/state/selection reflect reality without waiting up to 4s for
    // the next background poll tick.
    refreshRecordStatus();
  }
}

// ----- Record controls -----

async function apiPost(path, body) {
  const response = await fetch(path, {
    method: "POST",
    cache: "no-store",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  let payload;
  try {
    payload = await response.json();
  } catch (_) {
    payload = { error: `HTTP ${response.status}` };
  }
  if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
  return payload;
}

function setRecordState(label, active = false, error = false) {
  const node = $("recordState");
  node.textContent = label;
  node.classList.toggle("active", active);
  node.classList.toggle("error", error);
}

function applyRecordStatus(status) {
  state.record = status;
  const running = Boolean(status && status.running);
  $("recordStartButton").disabled = running;
  $("recordStopButton").disabled = !running;
  $("deviceSelect").disabled = running;
  $("packageSelect").disabled = running;
  $("intervalInput").disabled = running;
  if (running) {
    setRecordState(`錄製中 · ${status.output || ""}`, true, false);
    if (status.output && (!state.name || state.name !== status.output)) {
      // Auto-tail to live file while recording.
      state.name = status.output;
      sessionSelect.value = status.output;
      if (state.mode !== "live") {
        setMode("live");
      } else {
        loadSession(status.output);
      }
    }
  } else if (status && status.session_id) {
    setRecordState("已完成 · " + (status.output || ""), false, false);
  } else {
    setRecordState("待機", false, false);
  }
  restoreRecordSelection();
}

// Pre-select the device/package dropdowns to whatever /api/record/status last
// recorded (the server keeps package/serial on the RecordController even
// after a stop, so this survives page reloads and view switches alike — the
// user should never have to re-pick them on load, or while a recording is
// actually running). Called after every status refresh, and again once the
// device/package option lists finish loading in case status arrived first.
//
// agy red-team HIGH-01 (2026-08-20): the unconditional version of this
// function fought the user — /api/record/status keeps reporting the *last*
// session's serial/package forever after a stop, and the 4s background
// refreshRecordStatus() poll called this on every tick. So a user who
// stopped a recording and then picked a new device/package to start a fresh
// one would get clawed back to the old target within 4 seconds flat,
// whenever they hadn't yet hit "start". Fix: only auto-follow status while
// bootstrapping (state.userPickedTarget still false, i.e. the user hasn't
// touched either dropdown since page load) or while a recording is actually
// running (status.running — the dropdowns are disabled then anyway, so
// syncing them to the live session is harmless and keeps a reloaded page
// honest). Once idle and the user has manually chosen a target, leave it
// alone until they reload or start a new recording.
function restoreRecordSelection() {
  const status = state.record;
  if (!status) return;
  const running = Boolean(status.running);
  if (running || !state.userPickedTarget) {
    const deviceSelect = $("deviceSelect");
    const packageSelect = $("packageSelect");
    if (status.serial && deviceSelect.value !== status.serial) {
      const hasDevice = [...deviceSelect.options].some((option) => option.value === status.serial);
      if (hasDevice) {
        deviceSelect.value = status.serial;
        // Package list is per-device; refetch for the restored device, then
        // re-run to apply the package half once that list is in.
        refreshPackages(status.serial).then(() => restoreRecordSelection());
        return;
      }
    }
    if (status.package && packageSelect.value !== status.package) {
      const hasPackage = [...packageSelect.options].some((option) => option.value === status.package);
      if (hasPackage) packageSelect.value = status.package;
    }
  }
  updateRecordButtons();
}

async function refreshRecordStatus() {
  try {
    const status = await api("/api/record/status");
    applyRecordStatus(status);
  } catch (error) {
    setRecordState("狀態查詢失敗", false, true);
  }
}

async function refreshDevices(preserve = true) {
  const select = $("deviceSelect");
  const platform = state.platform;
  const endpoint = platform === "ios" ? "/api/ios/devices" : "/api/devices";
  try {
    const data = await api(endpoint);
    const devices = Array.isArray(data.devices) ? data.devices : [];
    const previous = preserve ? select.value : "";
    const emptyLabel = platform === "ios" ? "沒有 iPhone（未接／未信任）" : "沒有裝置";
    select.replaceChildren(new Option(devices.length ? "選擇裝置" : emptyLabel, ""));
    devices.forEach((device) => {
      const option = document.createElement("option");
      option.value = device.serial;
      option.textContent = `${device.serial} · ${device.state || "?"}`;
      select.appendChild(option);
    });
    state.devices = devices;
    if (previous && devices.some((device) => device.serial === previous)) {
      select.value = previous;
    } else if (devices.length) {
      select.value = devices[0].serial;
    }
    if (select.value) {
      await refreshPackages(select.value);
    } else {
      const pkg = $("packageSelect");
      const placeholder = platform === "ios"
        ? "自動偵測前景 App 或在進階面板手動輸入"
        : "選擇裝置後載入";
      pkg.replaceChildren(new Option(placeholder, ""));
    }
    restoreRecordSelection();
  } catch (error) {
    setRecordState("裝置掃描失敗", false, true);
  }
}

async function refreshPackages(serial) {
  const select = $("packageSelect");
  const platform = state.platform;
  if (!serial) {
    const placeholder = platform === "ios"
      ? "自動偵測前景 App 或在進階面板手動輸入"
      : "選擇裝置後載入";
    select.replaceChildren(new Option(placeholder, ""));
    state.packages = [];
    updateRecordButtons();
    return;
  }
  // iOS has no public "list installed packages" probe — the brief routes the
  // user through the auto-detect button (which reads the foreground bundle
  // via pymobiledevice3 sysmon). The advanced panel stays empty and accepts
  // free-form bundle ids.
  if (platform === "ios") {
    const previous = select.value;
    const placeholder = "自動偵測前景 App 或在進階面板手動輸入";
    select.replaceChildren(new Option(placeholder, ""));
    if (previous && previous.includes(".")) select.value = previous;
    state.packages = [];
    restoreRecordSelection();
    return;
  }
  try {
    const data = await api(`/api/packages?serial=${encodeURIComponent(serial)}`);
    const packages = Array.isArray(data.packages) ? data.packages : [];
    const previous = select.value;
    select.replaceChildren(new Option("選擇 package", ""));
    packages.forEach((name) => {
      const option = document.createElement("option");
      option.value = name;
      option.textContent = name;
      select.appendChild(option);
    });
    state.packages = packages;
    if (previous && packages.includes(previous)) {
      select.value = previous;
    }
    restoreRecordSelection();
  } catch (error) {
    setRecordState("Package 掃描失敗", false, true);
  }
}

function updateRecordButtons() {
  const running = state.record && state.record.running;
  const noPackage = !$("packageSelect").value;
  // iPhone capture is intentionally not implemented in this release —
  // disable the start button so the user gets the message from the server
  // response (501) instead of a silent UI hang.
  const iosBlocked = state.platform === "ios";
  $("recordStartButton").disabled = running || noPackage || iosBlocked;
  $("recordStopButton").disabled = !running;
}

// Phase 2: switch the active device source. Android hits /api/devices +
// /api/packages + /api/foreground (the existing adb path). iPhone hits
// /api/ios/devices + /api/ios/foreground (pymobiledevice3 path). The
// advanced bundle panel collapses on iOS too (we don't have a list of
// installed apps on the public surface, only the foreground snapshot).
//
// iPhone capture itself is still not wired into the server (see the
// onboarding card and RecordController.start's iOS rejection), so the
// record button is disabled on the iOS source until that lands.
function setSource(platform) {
  if (platform !== "ios" && platform !== "android") platform = "android";
  state.platform = platform;
  const androidBtn = $("sourceAndroid");
  const iosBtn = $("sourceiPhone");
  androidBtn.classList.toggle("active", platform === "android");
  iosBtn.classList.toggle("active", platform === "ios");
  androidBtn.setAttribute("aria-pressed", platform === "android" ? "true" : "false");
  iosBtn.setAttribute("aria-pressed", platform === "ios" ? "true" : "false");
  $("deviceLabel").textContent = platform === "ios" ? "DEVICE (UDID)" : "DEVICE";
  $("packageLabel").textContent = platform === "ios" ? "BUNDLE ID" : "PACKAGE";
  $("sourceHint").textContent = platform === "ios"
    ? "iPhone 主流程：自動偵測前景 App（讀 DVT sysmon；錄製本輪尚未整合）"
    : "Android 主流程：自動偵測前景 App（讀 dumpsys）";
  // Reset the device/package dropdowns when switching source so the user
  // doesn't see stale Android packages under an iPhone source label (or
  // vice versa).
  $("deviceSelect").replaceChildren(new Option("掃描中…", ""));
  $("packageSelect").replaceChildren(new Option("選擇裝置後載入", ""));
  state.devices = [];
  state.packages = [];
  state.userPickedTarget = false;
  updateRecordButtons();
  refreshDevices(false);
}

// Phase 2: advanced panel toggle. Collapsed by default per the 08-21 brief
// ("一般人不知道 package name"). Expand only if the user explicitly opens it.
function toggleAdvancedPanel() {
  const panel = $("advancedBundlePanel");
  const button = $("advancedToggle");
  const expanded = button.getAttribute("aria-expanded") === "true";
  const next = !expanded;
  button.setAttribute("aria-expanded", next ? "true" : "false");
  button.textContent = next ? "⚙ 收合進階手動選 App" : "⚙ 進階手動選 App";
  if (next) {
    panel.removeAttribute("hidden");
  } else {
    panel.setAttribute("hidden", "");
  }
}

// "自動偵測前景 App": the primary UX per the 08-21 brief. The user opens
// the game on the phone, clicks this once, and we ask the device which
// package currently owns the foreground activity. Android uses
// ``adb shell dumpsys activity activities`` (read-only). iOS uses
// ``pymobiledevice3 developer dvt sysmon process single`` (also read-only;
// we degrade to "the first bundle id we see in the snapshot" because DVT
// does not expose a clean foreground bit on the public surface). The
// advanced bundle dropdown is collapsed by default — a normal user never
// has to know a bundle id.
async function detectForeground() {
  const button = $("detectForegroundButton");
  const serial = $("deviceSelect").value || "";
  const platform = state.platform;
  const originalLabel = button.textContent;
  button.disabled = true;
  button.textContent = "偵測中…";
  try {
    const query = serial ? `?serial=${encodeURIComponent(serial)}` : "";
    const endpoint = platform === "ios" ? `/api/ios/foreground${query}` : `/api/foreground${query}`;
    const data = await api(endpoint);
    const id = (data && (data.package || data.bundle)) || "";
    if (!id) {
      setNotice("自動偵測失敗：找不到目前前景 App", true);
      return;
    }
    const select = $("packageSelect");
    const hasOption = [...select.options].some((option) => option.value === id);
    if (!hasOption) {
      const option = document.createElement("option");
      option.value = id;
      const friendly = platform === "ios" ? friendlyBundleName(id) : null;
      option.textContent = friendly ? `${friendly} (${id})` : id;
      select.appendChild(option);
      if (!state.packages.includes(id)) state.packages = [...state.packages, id];
    }
    select.value = id;
    // Same deliberate-target-change class as a manual dropdown pick (see
    // HIGH-01 fix in restoreRecordSelection) — without this the next 4s
    // background status poll would claw the selection back to whatever the
    // *previous* stopped session used.
    state.userPickedTarget = true;
    updateRecordButtons();
    const friendly = platform === "ios" ? friendlyBundleName(id) : null;
    const label = friendly || id;
    setNotice(`已自動偵測前景 App → ${label}`);
  } catch (error) {
    setNotice(`自動偵測失敗：${error.message}`, true);
  } finally {
    button.disabled = false;
    button.textContent = originalLabel;
  }
}

async function startRecording() {
  const pkg = $("packageSelect").value;
  if (!pkg) {
    setNotice("請先自動偵測前景或到進階面板手選 App", true);
    return;
  }
  const serial = $("deviceSelect").value || null;
  const interval = Number($("intervalInput").value) || 1;
  try {
    setRecordState("啟動中…", true);
    // Phase 2: forward the chosen source so the server can dispatch the
    // matching collector. iPhone capture is gated in the wizard and the
    // server returns 501 until a recorder ships.
    const platform = state.platform;
    const status = await apiPost("/api/record/start", {
      package: pkg,
      serial,
      interval,
      platform,
    });
    applyRecordStatus(status);
    setNotice(`錄製已啟動 → ${status.output || ""}`);
    await listSessions(true);
  } catch (error) {
    setRecordState("啟動失敗", false, true);
    setNotice(`錄製啟動失敗：${error.message}`, true);
  }
}

// Only the actions that truly end a recording (this button, or closing the
// tab mid-recording via beforeunload below) confirm. Switching between
// Overview/FPS Lab is not a stop action and must never reach this function.
async function stopRecording() {
  if (state.record && state.record.running) {
    if (!window.confirm("錄製進行中，確定停止？")) return;
  }
  try {
    setRecordState("停止中…", false);
    const status = await apiPost("/api/record/stop", {});
    applyRecordStatus(status);
    setNotice(`錄製已停止 · ${status.output || ""}`);
    await listSessions(true);
  } catch (error) {
    setRecordState("停止失敗", false, true);
    setNotice(`錄製停止失敗：${error.message}`, true);
  }
}

sessionSelect.addEventListener("change", () => loadSession(sessionSelect.value));
$("refreshButton").addEventListener("click", () => listSessions(true));
$("liveButton").addEventListener("click", () => setMode("live"));
$("replayButton").addEventListener("click", () => setMode("replay"));
$("playButton").addEventListener("click", toggleReplay);
$("speedSelect").addEventListener("change", (event) => {
  state.replaySpeed = Number(event.target.value) || 1;
  if (state.replayPlaying) replayStep();
});
$("scrubber").addEventListener("input", (event) => {
  stopReplay();
  state.replayIndex = Number(event.target.value) || 0;
  render();
});
$("jsonButton").addEventListener("click", () => download("json"));
$("csvButton").addEventListener("click", () => download("csv"));
$("fpsSummaryButton").addEventListener("click", copyFpsSummaryToClipboard);
$("overviewTab").addEventListener("click", () => setView("overview"));
$("fpsLabTab").addEventListener("click", () => setView("fpsLab"));
// Real user interaction only — restoreRecordSelection()'s own `.value = ...`
// assignments don't fire "change", so this can't be set by our own restore
// logic re-triggering itself.
$("deviceSelect").addEventListener("change", () => {
  state.userPickedTarget = true;
  refreshPackages($("deviceSelect").value);
});
$("packageSelect").addEventListener("change", () => {
  state.userPickedTarget = true;
  updateRecordButtons();
});
$("recordStartButton").addEventListener("click", startRecording);
$("recordStopButton").addEventListener("click", stopRecording);
$("detectForegroundButton").addEventListener("click", detectForeground);
$("sourceAndroid").addEventListener("click", () => setSource("android"));
$("sourceiPhone").addEventListener("click", () => setSource("ios"));
$("advancedToggle").addEventListener("click", toggleAdvancedPanel);
window.addEventListener("resize", render);

// The only other action (besides the ■ stop button) that actually ends a
// recording: closing/reloading/navigating away from the tab mid-recording.
// Switching Overview <-> FPS Lab never reaches here — it's a same-page DOM
// toggle, not a navigation/unload event.
window.addEventListener("beforeunload", (event) => {
  if (!(state.record && state.record.running)) return;
  event.preventDefault();
  event.returnValue = "";
});

renderFpsLabDefinitions();
render();
listSessions(false);
refreshDevices(false);
refreshRecordStatus();
setInterval(refreshRecordStatus, 4000);
initOnboarding();
