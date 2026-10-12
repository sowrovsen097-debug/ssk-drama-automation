// SSK DRAMA — তাৎক্ষণিক টেলিগ্রাম বট (Cloudflare Worker)
// এনভায়রনমেন্ট ভেরিয়েবল: TELEGRAM_BOT_TOKEN, GEMINI_API_KEY, GITHUB_REPO
// ঐচ্ছিক: TELEGRAM_CHAT_ID, GITHUB_TOKEN, BOT_SECRET, GEMINI_MODEL
// ওয়েবহুক: https://<worker>/set-webhook?s=<BOT_SECRET>

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (url.pathname === "/set-webhook") {
      if (!env.BOT_SECRET || url.searchParams.get("s") !== env.BOT_SECRET) {
        return json({ ok: false, error: "BOT_SECRET ভুল" }, 403);
      }
      const hook = url.origin + "/telegram";
      const set = await tg(env, "setWebhook", {
        url: hook,
        secret_token: env.BOT_SECRET,
        allowed_updates: ["message", "callback_query"],
      });
      const info = await tg(env, "getWebhookInfo", {});
      return json({ set, info });
    }
    if (url.pathname === "/health") {
      return json({ ok: true, repo: env.GITHUB_REPO, at: new Date().toISOString() });
    }
    if (request.method !== "POST") return new Response("SSK DRAMA bot is running.", { status: 200 });
    if (env.BOT_SECRET && request.headers.get("x-telegram-bot-api-secret-token") !== env.BOT_SECRET) {
      return json({ ok: false }, 403);
    }
    let update = {};
    try { update = await request.json(); } catch (e) {}
    ctx.waitUntil(handle(update, env));
    return new Response("ok");
  },

  async scheduled(event, env, ctx) {
    ctx.waitUntil(cronTick(env));
  },
};

function json(obj, status = 200) {
  return new Response(JSON.stringify(obj, null, 2), {
    status, headers: { "content-type": "application/json; charset=utf-8" },
  });
}

async function tg(env, method, body) {
  const r = await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/${method}`, {
    method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body),
  });
  return r.json().catch(() => ({}));
}

async function send(env, chatId, text, keyboard) {
  const payload = { chat_id: chatId, text: String(text).slice(0, 4000), disable_web_page_preview: true };
  if (keyboard) payload.reply_markup = keyboard;
  return tg(env, "sendMessage", payload);
}

async function rawJson(env, path) {
  try {
    const r = await fetch(`https://raw.githubusercontent.com/${env.GITHUB_REPO}/main/${path}?t=${Date.now()}`);
    if (!r.ok) return {};
    return await r.json();
  } catch (e) { return {}; }
}

function b64(str) {
  return btoa(unescape(encodeURIComponent(str)));
}

async function putJson(env, path, obj, message) {
  if (!env.GITHUB_TOKEN) throw new Error("GITHUB_TOKEN নেই");
  const api = `https://api.github.com/repos/${env.GITHUB_REPO}/contents/${path}`;
  const head = { Authorization: `Bearer ${env.GITHUB_TOKEN}`, "User-Agent": "ssk-bot",
                 Accept: "application/vnd.github+json" };
  const cur = await fetch(api, { headers: head });
  let sha = null;
  if (cur.ok) sha = (await cur.json()).sha;
  const body = { message, branch: "main", content: b64(JSON.stringify(obj, null, 2) + "\n") };
  if (sha) body.sha = sha;
  const r = await fetch(api, { method: "PUT", headers: { ...head, "content-type": "application/json" },
                               body: JSON.stringify(body) });
  if (!r.ok) throw new Error(`GitHub ${r.status}: ${(await r.text()).slice(0, 160)}`);
  return true;
}

async function dispatch(env, mode, payload = {}) {
  if (!env.GITHUB_TOKEN) throw new Error("GITHUB_TOKEN নেই — কাজ চালানো যাবে না");
  const r = await fetch(`https://api.github.com/repos/${env.GITHUB_REPO}/dispatches`, {
    method: "POST",
    headers: { Authorization: `Bearer ${env.GITHUB_TOKEN}`, "User-Agent": "ssk-bot",
               Accept: "application/vnd.github+json", "content-type": "application/json" },
    body: JSON.stringify({ event_type: "ssk", client_payload: { mode, ...payload } }),
  });
  if (!r.ok) throw new Error(`dispatch ${r.status}: ${(await r.text()).slice(0, 160)}`);
  return true;
}

async function gemini(env, prompt) {
  const key = env.GEMINI_API_KEY;
  if (!key) throw new Error("GEMINI_API_KEY নেই");
  const models = [env.GEMINI_MODEL || "gemini-3.1-flash-lite", "gemini-2.5-flash", "gemini-2.0-flash"];
  for (const m of models) {
    try {
      const r = await fetch(`https://generativelanguage.googleapis.com/v1beta/models/${m}:generateContent?key=${key}`, {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ contents: [{ parts: [{ text: prompt }] }],
                               generationConfig: { temperature: 0.5, maxOutputTokens: 900 } }),
      });
      const j = await r.json();
      const parts = (((j.candidates || [{}])[0].content || {}).parts) || [];
      const text = parts.map((p) => p.text || "").join("").trim();
      if (text) return text;
    } catch (e) {}
  }
  throw new Error("Gemini উত্তর দেয়নি");
}

async function stateMini(env) {
  const [status, queue, log, progress] = await Promise.all([
    rawJson(env, "status.json"), rawJson(env, "queue.json"),
    rawJson(env, "log.json"), rawJson(env, "progress.json"),
  ]);
  return { status, queue, log, progress };
}

function jobsText(queue) {
  return Object.entries(queue || {}).slice(-8).map(([k, v]) =>
    `• ${k}: ${v.state}${v.error ? " — " + String(v.error).slice(0, 120) : ""}`).join("\n") || "কোনো কাজ নেই";
}

async function liveText(env) {
  const { status, queue, progress, log } = await stateMini(env);
  const c = status.counts || {};
  const done = Object.values(queue || {}).filter((j) => j.state === "done").length;
  const failed = Object.values(queue || {}).filter((j) => j.state === "failed").length;
  const last = (log.items || []).slice(-4).map((x) => `• ${x.at} ${x.text}`.slice(0, 160)).join("\n");
  const drive = status.drive || {};
  const free = drive.free ? (drive.free / 1073741824).toFixed(1) + " GB" : (drive.error || "অজানা");
  return [
    `🎬 SSK DRAMA LIVE — ${new Date().toLocaleString("bn-BD", { timeZone: "Asia/Dhaka" })}`,
    `ধাপ: ${(progress || {}).stage || "অপেক্ষায়"}`,
    `তৈরি হয়েছে: ${c.ready || 0} · আপলোড হয়েছে: ${c.done || done} · ব্যর্থ: ${failed}`,
    `বর্তমানে চলছে: ${c.preparing || 0} তৈরি, ${c.uploading || 0} আপলোড`,
    `পজ: ${status.paused ? "হ্যাঁ" : "না"} · Drive খালি: ${free}`,
    `এডিট উইন্ডো: ${(status.edit_window || ["01:00", "06:00"]).join("–")} (BD) · ন্যূনতম আগে: ${status.min_lead_hours || 6} ঘণ্টা`,
    "", "কাজগুলোর অবস্থা:", jobsText(queue), "", last ? "সর্বশেষ লগ:\n" + last : "",
  ].join("\n");
}

function settingsText(cfg) {
  const f = (cfg.facebook_times_bd || []).join(", ");
  const y = (cfg.youtube_times_bd || []).join(", ");
  return [
    "⚙️ SSK DRAMA SETTINGS (এখান থেকে সব পরিবর্তন করবে)",
    `এডিট উইন্ডো: ${(cfg.edit_window_bd || []).join("–")} (BD)`,
    `ন্যূনতম কত আগে সময় দিতে হবে: ${cfg.min_lead_hours || 6} ঘণ্টা`,
    `ট্রিম অনুপাত: ${cfg.keep_ratio} · সর্বোচ্চ দৈর্ঘ্য: ${cfg.max_duration_minutes} মিনিট`,
    `পুরোনো কত দিনের ভিডিও: ${cfg.day_filter} দিন · Whisper: ${cfg.whisper_model}`,
    `Facebook সময়: ${f} · YouTube সময়: ${y}`,
    `পজ: ${cfg.paused ? "হ্যাঁ" : "না"}`,
    "",
    "পরিবর্তন করতে: /set key value",
    "উদাহরণ: /set min_lead_hours 8 | /set edit_window 02:00-05:30 | /set keep_ratio 0.82",
    "সময় বদলাতে: /times facebook 10:00,15:00,20:00 | /times youtube 08:00,15:00,22:00",
    "পজ/চালু: /pause অথবা /resume",
  ].join("\n");
}

async function applySet(env, args) {
  const cfg = await rawJson(env, "config.json");
  const key = (args[0] || "").trim(), value = args.slice(1).join(" ").trim();
  if (!key || !value) return "ব্যবহার: /set key value\n\n" + settingsText(cfg);
  const numeric = ["min_lead_hours", "keep_ratio", "max_duration_minutes", "day_filter",
                   "min_duration_seconds", "micro_speed", "storage_retention_days", "pitch_ratio"];
  if (numeric.includes(key)) {
    const num = Number(value);
    if (!isFinite(num)) return `${key}: সংখ্যা দাও`;
    if (key === "keep_ratio" && (num < 0.5 || num > 0.95)) return "keep_ratio ০.৫–০.৯৫ এর মধ্যে হতে হবে";
    if (key === "min_lead_hours" && num < 6) return "ন্যূনতম ৬ ঘণ্টার নিয়ম ভাঙা যাবে না";
    cfg[key] = num;
  } else if (key === "edit_window") {
    const m = /^(\d{2}:\d{2})-(\d{2}:\d{2})$/.exec(value);
    if (!m) return "ব্যবহার: /set edit_window 01:00-06:00";
    cfg.edit_window_bd = [m[1], m[2]];
  } else if (key === "whisper_model") {
    if (!["tiny", "base", "small", "medium"].includes(value)) return "tiny/base/small/medium এর একটি দাও";
    cfg.whisper_model = value;
  } else if (key === "paused") {
    cfg.paused = ["1", "true", "হ্যাঁ", "yes", "on"].includes(value.toLowerCase());
  } else {
    cfg[key] = value;
  }
  await putJson(env, "config.json", cfg, `SSK config: ${key}=${value}`);
  return `✅ সেভ হয়েছে: ${key} = ${key === "edit_window" ? cfg.edit_window_bd.join("-") : cfg[key]}\n\n` + settingsText(cfg);
}

async function applyTimes(env, args) {
  const cfg = await rawJson(env, "config.json");
  const platform = (args[0] || "").toLowerCase();
  const list = (args[1] || "").split(",").map((s) => s.trim()).filter(Boolean);
  if (!["facebook", "youtube"].includes(platform) || !list.length)
    return "ব্যবহার: /times facebook 10:00,15:00,20:00";
  for (const t of list)
    if (!/^([01]\d|2[0-3]):[0-5]\d$/.test(t)) return `সময় ভুল: ${t} (HH:MM)`;
  cfg[`${platform}_times_bd`] = list;
  await putJson(env, "config.json", cfg, `SSK times ${platform}`);
  return `✅ ${platform} সময় সেভ: ${list.join(", ")}\n\n` + settingsText(cfg);
}

async function answer(env, chatId, question) {
  const { status, queue, progress, log } = await stateMini(env);
  const context = JSON.stringify({
    paused: status.paused, counts: status.counts, edit_window: status.edit_window,
    min_lead_hours: status.min_lead_hours, drive: status.drive, progress,
    jobs: Object.entries(queue || {}).slice(-8).map(([k, v]) => ({
      key: k, platform: v.platform, state: v.state, error: String(v.error || "").slice(0, 140) })),
    log: (log.items || []).slice(-6).map((x) => x.text),
  }).slice(0, 6000);
  const prompt = "তুমি SSK DRAMA অটোমেশনের সহকারী। বাংলায় ছোট করে (সর্বোচ্চ ৮ লাইন) উত্তর দাও। " +
    "কোনো কোড বা টোকেন দেবে না। শুধু এই স্টেট থেকেই উত্তর দাও, বানানো তথ্য নয়।\n" +
    `স্টেট: ${context}\nপ্রশ্ন: ${question}`;
  const reply = await gemini(env, prompt);
  await send(env, chatId, reply);
}

async function handle(update, env) {
  const msg = update.message || (update.callback_query || {}).message;
  if (!msg || !msg.chat) return;
  const chatId = msg.chat.id;
  const text = (update.message ? msg.text : (update.callback_query || {}).data) || "";
  const t = text.trim();
  const low = t.toLowerCase();

  if (env.TELEGRAM_CHAT_ID && String(chatId) !== String(env.TELEGRAM_CHAT_ID)) {
    await send(env, chatId, "দুঃখিত, এই বট শুধু মালিকের জন্য।");
    return;
  }
  try {
    if (!t || ["/start", "/help", "hi", "হ্যালো"].includes(low)) {
      return await send(env, chatId,
        "🎬 SSK DRAMA বট প্রস্তুত\n\nSSK DRAMA LIVE — এখনো পর্যন্ত কী হয়েছে\nSSK DRAMA UPDATE — সেটিংস বদলাও\nSSK DRAMA SETTINGS — সব সেটিংস দেখো\n\nযেকোনো প্রশ্ন বাংলায় লিখো — সাথে সাথে উত্তর দেব।",
        { keyboard: [[{ text: "SSK DRAMA LIVE" }, { text: "SSK DRAMA UPDATE" }]], resize_keyboard: true });
    }
    if (t === "SSK DRAMA LIVE" || low === "/live" || low === "live") {
      return await send(env, chatId, await liveText(env));
    }
    if (t === "SSK DRAMA UPDATE" || low === "/settings" || low === "update") {
      const cfg = await rawJson(env, "config.json");
      return await send(env, chatId, settingsText(cfg),
        { keyboard: [[{ text: "/run prepare" }, { text: "/run scan" }],
                     [{ text: "/pause" }, { text: "/resume" }]], resize_keyboard: true });
    }
    if (low === "/pause" || low === "/resume") {
      const cfg = await rawJson(env, "config.json");
      cfg.paused = low === "/pause";
      await putJson(env, "config.json", cfg, "SSK pause toggle");
      return await send(env, chatId, cfg.paused ? "⏸ সিস্টেম পজ করা হলো" : "▶️ সিস্টেম চালু করা হলো");
    }
    if (low.startsWith("/times")) return await send(env, chatId, await applyTimes(env, t.split(/\s+/).slice(1)));
    if (low.startsWith("/set")) return await send(env, chatId, await applySet(env, t.split(/\s+/).slice(1)));
    if (low.startsWith("/run")) {
      const mode = (t.split(/\s+/)[1] || "service").toLowerCase();
      const allowed = ["service", "scan", "upload", "prepare"];
      if (!allowed.includes(mode)) return await send(env, chatId, "হবে: " + allowed.join(", "));
      await dispatch(env, mode, mode === "prepare" ? { force: "1" } : {});
      return await send(env, chatId, `🚀 ${mode} কাজ চালু করা হলো — কিছুক্ষণ পর SSK DRAMA LIVE লিখে দেখো।`);
    }
    if (low.startsWith("/status")) {
      const { status } = await stateMini(env);
      return await send(env, chatId, "📊 " + JSON.stringify(status.counts || {}) + " · পজ: " + (status.paused ? "হ্যাঁ" : "না"));
    }
    await answer(env, chatId, t);
  } catch (e) {
    await send(env, chatId, "⚠️ সমস্যা: " + String(e.message || e).slice(0, 300));
  }
}

async function cronTick(env) {
  // প্রতি মিনিটে চলবে: GitHub Actions-এর রেখে যাওয়া কমান্ড/চ্যাট থাকলে সাথে সাথে উত্তর দিয়ে দেয়।
  try {
    const chat = await rawJson(env, "chat/index.json");
    const pending = (chat && chat.pending) || [];
    for (const name of pending.slice(0, 3)) {
      const item = await rawJson(env, "chat/" + name);
      if (!item.message) continue;
      try {
        const reply = await gemini(env, "তুমি SSK DRAMA-র সহকারী। বাংলায় ছোট উত্তর দাও।\nপ্রশ্ন: " + item.message);
        item.reply = reply; item.state = "done"; item.answered_at = new Date().toISOString();
        await putJson(env, "chat/" + name, item, "SSK chat reply");
        if (env.TELEGRAM_CHAT_ID) await send(env, env.TELEGRAM_CHAT_ID, "💬 " + reply);
      } catch (e) {}
    }
  } catch (e) {}
}
