// ============================================================================
// SSK DRAMA Telegram Bot — Cloudflare Worker (ফ্রি, always-on, ইনস্ট্যান্ট উত্তর)
// Cloudflare → Workers → Settings → Variables and Secrets এখানে বসান:
//   TELEGRAM_BOT_TOKEN, GEMINI_API_KEY, GITHUB_TOKEN, GITHUB_REPO,
//   TELEGRAM_CHAT_ID (ঐচ্ছিক), BOT_SECRET (ঐচ্ছিক), GEMINI_MODEL (ঐচ্ছিক)
// Cron Trigger: * * * * *  (প্রতি মিনিটে — LIVE মোড চালু থাকলে আপডেট পাঠায়)
// ============================================================================
const TG = 'https://api.telegram.org';
const GHAPI = 'https://api.github.com';
const RAW = 'https://raw.githubusercontent.com';

function esc(s) {
  return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
function b64e(s) {
  const b = new TextEncoder().encode(s); let o = '';
  for (const x of b) { o += String.fromCharCode(x); }
  return btoa(o);
}
async function tg(env, method, payload) {
  const r = await fetch(TG + '/bot' + env.TELEGRAM_BOT_TOKEN + '/' + method, {
    method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(payload || {})
  });
  return await r.json();
}
async function rawJson(env, path) {
  try {
    const r = await fetch(RAW + '/' + env.GITHUB_REPO + '/main/' + path + '?t=' + Date.now());
    if (!r.ok) { return null; }
    return await r.json();
  } catch (e) { return null; }
}
async function apiJson(env, path, init) {
  const opt = Object.assign({}, init || {});
  opt.headers = Object.assign({
    'accept': 'application/vnd.github+json',
    'authorization': 'Bearer ' + env.GITHUB_TOKEN,
    'x-github-api-version': '2022-11-28',
    'user-agent': 'ssk-drama-bot'
  }, (init && init.headers) || {});
  return await fetch(GHAPI + '/repos/' + env.GITHUB_REPO + '/' + path, opt);
}
async function writeJson(env, path, data, message) {
  let sha = null;
  const cur = await apiJson(env, 'contents/' + path + '?ref=main', { method: 'GET' });
  if (cur.ok) { try { sha = (await cur.json()).sha; } catch (e) { sha = null; } }
  const body = { message: message || ('SSK bot: ' + path), branch: 'main', content: b64e(JSON.stringify(data, null, 2) + '\n') };
  if (sha) { body.sha = sha; }
  const r = await apiJson(env, 'contents/' + path, { method: 'PUT', body: JSON.stringify(body) });
  return r.ok;
}
async function gemini(env, prompt) {
  const model = env.GEMINI_MODEL || 'gemini-3.1-flash-lite';
  const r = await fetch('https://generativelanguage.googleapis.com/v1beta/models/' + model + ':generateContent?key=' + env.GEMINI_API_KEY, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ contents: [{ parts: [{ text: prompt }] }], generationConfig: { temperature: 0.4, maxOutputTokens: 900 } })
  });
  const j = await r.json();
  const cand = (j.candidates || [])[0] || {};
  const parts = (cand.content || {}).parts || [];
  const text = parts.map(function (p) { return p.text || ''; }).join('').trim();
  if (!text) { throw new Error('Gemini উত্তর দেয়নি: ' + JSON.stringify(j).slice(0, 200)); }
  return text;
}

async function liveData(env) {
  const got = await Promise.all([
    rawJson(env, 'queue.json'), rawJson(env, 'config.json'),
    rawJson(env, 'log.json'), rawJson(env, 'status.json'), rawJson(env, 'progress.json')
  ]);
  const queue = got[0] || {}, cfg = got[1] || {}, log = got[2] || {}, status = got[3] || {}, prog = got[4] || {};
  const jobs = Object.entries(queue).filter(function (e) { return e[1] && e[1].platform; });
  const count = {};
  jobs.forEach(function (e) { const st = e[1].state || 'pending'; count[st] = (count[st] || 0) + 1; });
  return { queue: queue, cfg: cfg, log: log, status: status, prog: prog, jobs: jobs, count: count };
}
function bdNow() {
  try { return new Date().toLocaleString('en-GB', { timeZone: 'Asia/Dhaka' }); }
  catch (e) { return new Date().toISOString(); }
}
function liveReport(d) {
  const L = [];
  L.push('📊 <b>SSK DRAMA LIVE</b>');
  L.push('🕒 ' + bdNow() + ' (বাংলাদেশ)');
  L.push((d.cfg.paused ? '⏸ পজ করা' : '▶️ চালু') + ' · এডিট উইন্ডো: রাত ১টা–সকাল ৬টা');
  L.push('📦 কাজ: মোট ' + d.jobs.length + ' · সম্পন্ন ' + (d.count.done || 0) + ' · ব্যর্থ ' + (d.count.failed || 0) +
         ' · রেডি ' + (d.count.ready || 0) + ' · এডিট/আপলোড চলছে ' + ((d.count.preparing || 0) + (d.count.uploading || 0)));
  const recent = d.jobs.slice().sort(function (a, b) { return String(b[1].updated_at || '').localeCompare(String(a[1].updated_at || '')); }).slice(0, 6);
  if (recent.length) {
    L.push('🗂 সর্বশেষ কাজ:');
    recent.forEach(function (e) {
      const v = e[1];
      L.push('• ' + esc(e[0]) + ' — ' + esc(v.state || '?') + (v.error ? ' → ' + esc(String(v.error).slice(0, 110)) : ''));
    });
  } else { L.push('🗂 এখনো কোনো কাজের রেকর্ড নেই।'); }
  if (d.prog && d.prog.stage) { L.push('⚙️ ধাপ: ' + esc(d.prog.stage) + (d.prog.updated_at ? ' (' + esc(d.prog.updated_at) + ')' : '')); }
  const items = (d.log.items || []).slice(-4).reverse();
  if (items.length) {
    L.push('🧾 শেষ লগ:');
    items.forEach(function (i) { L.push('• ' + esc(String(i.text || '').slice(0, 130))); });
  }
  L.push('ℹ️ বন্ধ করতে: <b>SSK DRAMA UPDATE</b> লিখুন, অথবা যেকোনো সাধারণ প্রশ্ন লিখুন।');
  return L.join('\n');
}
function updateKeyboard(cfg) {
  const music = cfg.music_enabled ? 'চালু' : 'বন্ধ';
  const day = (cfg.day_filter === 0 || cfg.allow_any_age) ? 'যেকোনো বয়স' : (cfg.day_filter + ' দিন');
  const ratio = String(cfg.keep_ratio || 0.8125);
  return { inline_keyboard: [
    [{ text: (cfg.paused ? '▶️ চালু করুন' : '⏸ পজ করুন'), callback_data: 'pause' }, { text: '🔄 রিফ্রেশ', callback_data: 'refresh' }],
    [{ text: '🕒 সময় বদল', callback_data: 'times' }, { text: '📅 বয়স: ' + day, callback_data: 'day' }],
    [{ text: '🎚 রেশিও: ' + ratio, callback_data: 'ratio' }, { text: '🎵 মিউজিক: ' + music, callback_data: 'music' }],
    [{ text: '⚙️ বিশেষ অপশন', callback_data: 'special' }, { text: '🌙 এখনই এডিট', callback_data: 'editnow' }],
    [{ text: '⬆️ আপলোড চেক', callback_data: 'upload' }, { text: '📊 লাইভ দেখুন', callback_data: 'dash' }],
    [{ text: '❌ বন্ধ করুন', callback_data: 'close' }]
  ] };
}
function specialKeyboard(cfg) {
  const anyAge = cfg.allow_any_age ? 'চালু' : 'বন্ধ';
  const trim = cfg.trim_long_sources ? 'চালু' : 'বন্ধ';
  const probe = String(cfg.metadata_probe_limit || 15);
  return { inline_keyboard: [
    [{ text: '🧩 যেকোনো বয়সের ভিডিও: ' + anyAge, callback_data: 'any_age' }],
    [{ text: '✂️ লম্বা ভিডিও কেটে নেওয়া: ' + trim, callback_data: 'trim' }],
    [{ text: '🔎 সোর্স স্ক্যান গভীরতা: ' + probe, callback_data: 'probe' }],
    [{ text: '⬆️ yt-dlp আপডেট + স্ক্যান টেস্ট', callback_data: 'scan' }],
    [{ text: '⬅️ ফিরে যান', callback_data: 'back' }]
  ] };
}
function updateText(d) {
  const cfg = d.cfg || {};
  const L = [];
  L.push('⚙️ <b>SSK DRAMA UPDATE</b> — অ্যাপের সেটিংস এখান থেকেই বদলান');
  L.push('▶️ সিস্টেম: ' + (cfg.paused ? 'পজ' : 'চালু'));
  const fbc = cfg.facebook_channels || [], ytc = cfg.youtube_channels || [];
  const ft = cfg.facebook_times_bd || [], tt = cfg.youtube_times_bd || [];
  L.push('');
  L.push('<b>Facebook (১:১)</b>');
  [0, 1, 2].forEach(function (i) { L.push('• ' + esc((fbc[i] || '—').replace('https://youtube.com/', '')) + ' → ' + esc(ft[i] || '—')); });
  L.push('<b>YouTube (১৬:৯)</b>');
  [0, 1, 2].forEach(function (i) { L.push('• ' + esc((ytc[i] || '—').replace('https://youtube.com/', '')) + ' → ' + esc(tt[i] || '—')); });
  L.push('');
  L.push('📅 বয়স ফিল্টার: ' + ((cfg.day_filter === 0 || cfg.allow_any_age) ? 'যেকোনো' : (cfg.day_filter + ' দিন')) +
         ' · 🎚 রেশিও: ' + String(cfg.keep_ratio || 0.8125) +
         ' · 🎵 মিউজিক: ' + (cfg.music_enabled ? 'চালু' : 'বন্ধ'));
  L.push('⏱ সর্বোচ্চ দৈর্ঘ্য: ' + String(cfg.max_duration_minutes || 80) + ' মিনিট · Whisper: ' + esc(cfg.whisper_model || 'base'));
  L.push('🧩 যেকোনো বয়স: ' + (cfg.allow_any_age ? 'চালু' : 'বন্ধ') + ' · ✂️ লম্বা কাটা: ' + (cfg.trim_long_sources ? 'চালু' : 'বন্ধ') +
         ' · 🔎 স্ক্যান: ' + String(cfg.metadata_probe_limit || 15));
  L.push('');
  L.push('নিচের বাটনে চাপ দিন। অন্য যেকোনো কথা লিখলে এই মোড বন্ধ হয়ে সাধারণ উত্তর আসবে।');
  return L.join('\n');
}

async function dispatch(env, mode, extra) {
  const r = await apiJson(env, 'dispatches', {
    method: 'POST',
    body: JSON.stringify({ event_type: 'ssk', client_payload: Object.assign({ mode: mode }, extra || {}) })
  });
  if (r.status === 204) { return true; }
  await writeJson(env, 'bot_command.json',
    { mode: mode, extra: extra || {}, state: 'pending', at: new Date().toISOString(), note: 'GitHub dispatch ব্যর্থ: HTTP ' + r.status },
    'SSK bot command');
  return false;
}
async function patchConfig(env, patch) {
  const cfg = (await rawJson(env, 'config.json')) || {};
  Object.assign(cfg, patch);
  return await writeJson(env, 'config.json', cfg, 'SSK bot: settings');
}
async function setMode(env, chatId, mode, messageId) {
  const st = (await rawJson(env, 'bot_state.json')) || {};
  st.mode = mode; st.chat_id = chatId; st.updated_at = new Date().toISOString();
  if (messageId) { st.message_id = messageId; }
  if (mode === 'live') { st.live_message_id = messageId || st.live_message_id || null; }
  await writeJson(env, 'bot_state.json', st, 'SSK bot: mode ' + mode);
  return st;
}

async function handleUpdate(env, update) {
  try {
    if (update.callback_query) { return await handleCallback(env, update.callback_query); }
    const msg = update.message || update.channel_post;
    if (!msg || !msg.text) { return; }
    const chatId = String((msg.chat && msg.chat.id) || env.TELEGRAM_CHAT_ID || '');
    if (!chatId) { return; }
    if (env.TELEGRAM_CHAT_ID && String(env.TELEGRAM_CHAT_ID) !== chatId) {
      await tg(env, 'sendMessage', { chat_id: chatId, text: '⛔ এই বট শুধু অনুমোদিত চ্যাটে কাজ করে।' });
      return;
    }
    const raw = msg.text.trim();
    const key = raw.toUpperCase().replace(/\s+/g, ' ');
    const st = (await rawJson(env, 'bot_state.json')) || {};

    if (key === 'SSK DRAMA LIVE' || key === 'SSK DRAMA LIVE.' || key === '/LIVE') {
      const d = await liveData(env);
      const sent = await tg(env, 'sendMessage', { chat_id: chatId, text: liveReport(d), parse_mode: 'HTML' });
      const mid = (sent.result && sent.result.message_id) || null;
      await setMode(env, chatId, 'live', mid);
      await tg(env, 'sendMessage', { chat_id: chatId, text: '🔴 LIVE মোড চালু — প্রতি মিনিটে এই মেসেজ নিজে থেকেই আপডেট হবে। বন্ধ করতে <b>SSK DRAMA UPDATE</b> লিখুন।', parse_mode: 'HTML' });
      return;
    }
    if (key === 'SSK DRAMA UPDATE' || key === '/UPDATE') {
      const d = await liveData(env);
      await setMode(env, chatId, 'update', null);
      await tg(env, 'sendMessage', { chat_id: chatId, text: updateText(d), parse_mode: 'HTML', reply_markup: updateKeyboard(d.cfg) });
      return;
    }
    if (st.pending_action === 'times') {
      const fb = (raw.match(/FB\s*=?\s*([0-9:, ]+)/i) || [])[1] || '';
      const yt = (raw.match(/YT\s*=?\s*([0-9:, ]+)/i) || [])[1] || '';
      const clean = function (s) { return String(s).split(',').map(function (x) { return x.trim(); }).filter(function (x) { return /^([01]\d|2[0-3]):[0-5]\d$/.test(x); }).slice(0, 3); };
      const fbTimes = clean(fb), ytTimes = clean(yt);
      if (fbTimes.length !== 3 || ytTimes.length !== 3) {
        await tg(env, 'sendMessage', { chat_id: chatId, text: '❌ ঠিক ৩টি করে সময় লাগবে, ফরম্যাট: FB=10:00,15:00,20:00 | YT=08:00,15:00,22:00 (রাত ১টা–সকাল ৬টা নিষিদ্ধ)' });
        return;
      }
      const bad = fbTimes.concat(ytTimes).some(function (t) { const h = parseInt(t.slice(0, 2), 10); return h >= 1 && h < 6; });
      if (bad) { await tg(env, 'sendMessage', { chat_id: chatId, text: '❌ রাত ১টা থেকে সকাল ৬টার মধ্যে আপলোড সময় দেওয়া যাবে না।' }); return; }
      const ok = await patchConfig(env, { facebook_times_bd: fbTimes, youtube_times_bd: ytTimes });
      await setMode(env, chatId, 'update', null);
      const d2 = await liveData(env);
      await tg(env, 'sendMessage', { chat_id: chatId, text: (ok ? '✅ সময় সংরক্ষিত হয়েছে।\n\n' : '⚠️ সংরক্ষণ ব্যর্থ (টোকেন চেক করুন)।\n\n') + updateText(d2), parse_mode: 'HTML', reply_markup: updateKeyboard(d2.cfg) });
      return;
    }
    if (st.pending_action === 'probe') {
      const n = parseInt(raw.replace(/[^0-9]/g, ''), 10);
      if (!n || n < 3 || n > 60) { await tg(env, 'sendMessage', { chat_id: chatId, text: '❌ ৩ থেকে ৬০ এর মধ্যে একটা সংখ্যা লিখুন।' }); return; }
      await patchConfig(env, { metadata_probe_limit: n });
      await setMode(env, chatId, 'update', null);
      await tg(env, 'sendMessage', { chat_id: chatId, text: '✅ স্ক্যান গভীরতা ' + n + ' করা হয়েছে।' });
      return;
    }
    const d = await liveData(env);
    await setMode(env, chatId, 'ai', null);
    const context = 'সিস্টেম স্টেট (JSON): ' + JSON.stringify({
      paused: d.cfg.paused, facebook_times_bd: d.cfg.facebook_times_bd, youtube_times_bd: d.cfg.youtube_times_bd,
      day_filter: d.cfg.day_filter, keep_ratio: d.cfg.keep_ratio, counts: d.count,
      recent_jobs: d.jobs.slice(0, 6).map(function (e) { return { key: e[0], state: e[1].state, error: String(e[1].error || '').slice(0, 160) }; }),
      last_log: (d.log.items || []).slice(-4).map(function (i) { return i.text; }),
      stage: (d.prog || {}).stage
    });
    const prompt = 'তুমি "SSK DRAMA" অটোমেশন সিস্টেমের সহকারী। বাংলায় সংক্ষেপে (সর্বোচ্চ ৮ লাইন) উত্তর দাও। ' +
      'নিয়ম: কোনো কোড লিখবে না, কোনো সিক্রেট/টোকেন দেখাবে না, বানানো তথ্য দেবে না; স্টেট থেকে যা জানা যায় শুধু তাই বলবে। ' +
      'ব্যবহারকারী LIVE আপডেট চাইলে বলবে "SSK DRAMA LIVE" লিখতে, সেটিংস বদলাতে চাইলে "SSK DRAMA UPDATE" লিখতে।\n' +
      context + '\nব্যবহারকারীর প্রশ্ন: ' + raw;
    let reply;
    try { reply = await gemini(env, prompt); }
    catch (e) { reply = '⚠️ উত্তর দিতে পারিনি: ' + String(e.message || e).slice(0, 200); }
    await tg(env, 'sendMessage', { chat_id: chatId, text: reply });
  } catch (e) {
    // চুপচাপ ব্যর্থ হলে Telegram আবার পাঠাবে
  }
}

async function handleCallback(env, cb) {
  const chatId = String((cb.message && cb.message.chat && cb.message.chat.id) || '');
  const msgId = cb.message && cb.message.message_id;
  const data = cb.data || '';
  await tg(env, 'answerCallbackQuery', { callback_query_id: cb.id, text: 'হচ্ছে…' });
  const cfg = (await rawJson(env, 'config.json')) || {};

  if (data === 'pause') {
    await patchConfig(env, { paused: !cfg.paused });
  } else if (data === 'day') {
    const order = [1, 6, 8, 30, 60, 90, 0];
    const cur = Number(cfg.day_filter || 60);
    const next = order[(order.indexOf(cur) + 1) % order.length];
    await patchConfig(env, { day_filter: next, allow_any_age: false });
  } else if (data === 'ratio') {
    const order = [0.8125, 0.875, 0.75];
    const cur = Number(cfg.keep_ratio || 0.8125);
    const next = order[(order.indexOf(cur) + 1) % order.length];
    await patchConfig(env, { keep_ratio: next });
  } else if (data === 'music') {
    await patchConfig(env, { music_enabled: !cfg.music_enabled });
  } else if (data === 'any_age') {
    await patchConfig(env, { allow_any_age: !cfg.allow_any_age });
  } else if (data === 'trim') {
    await patchConfig(env, { trim_long_sources: !cfg.trim_long_sources });
  } else if (data === 'times') {
    const st = (await rawJson(env, 'bot_state.json')) || {};
    st.pending_action = 'times'; st.chat_id = chatId;
    await writeJson(env, 'bot_state.json', st, 'SSK bot: waiting times');
    await tg(env, 'sendMessage', { chat_id: chatId, text: '🕒 নতুন সময় লিখুন এক লাইনে, ঠিক এই ফরম্যাটে:\n\nFB=10:00,15:00,20:00 | YT=08:00,15:00,22:00' });
    return;
  } else if (data === 'probe') {
    const st = (await rawJson(env, 'bot_state.json')) || {};
    st.pending_action = 'probe'; st.chat_id = chatId;
    await writeJson(env, 'bot_state.json', st, 'SSK bot: waiting probe');
    await tg(env, 'sendMessage', { chat_id: chatId, text: '🔎 সোর্স স্ক্যান গভীরতা কত ভিডিও পর্যন্ত পরীক্ষা করবে? (৩–৬০; এখন: ' + String(cfg.metadata_probe_limit || 15) + ')' });
    return;
  } else if (data === 'editnow') {
    const ok = await dispatch(env, 'prepare', { force: '1' });
    await tg(env, 'sendMessage', { chat_id: chatId, text: ok ? '🌙 রাতের এডিটিং এখনই চালু হয়েছে (Actions-এ দেখুন)।' : '⚠️ সরাসরি চালু করা যায়নি — কমান্ড সেভ হয়েছে, পরের মনিটর রানে চলবে।' });
    return;
  } else if (data === 'upload') {
    const ok = await dispatch(env, 'upload', {});
    await tg(env, 'sendMessage', { chat_id: chatId, text: ok ? '⬆️ আপলোড চেক চালু হয়েছে।' : '⚠️ কমান্ড সেভ হয়েছে, পরের মনিটর রানে চলবে।' });
    return;
  } else if (data === 'scan') {
    const ok = await dispatch(env, 'scan', {});
    await tg(env, 'sendMessage', { chat_id: chatId, text: ok ? '🔎 yt-dlp আপডেট + স্ক্যান টেস্ট চালু, ফলাফল Telegram-এ আসবে।' : '⚠️ কমান্ড সেভ হয়েছে, পরের মনিটর রানে চলবে।' });
    return;
  } else if (data === 'dash') {
    const d = await liveData(env);
    await tg(env, 'sendMessage', { chat_id: chatId, text: liveReport(d), parse_mode: 'HTML' });
    return;
  } else if (data === 'special') {
    const d = await liveData(env);
    await tg(env, 'editMessageText', { chat_id: chatId, message_id: msgId, text: '⚙️ <b>বিশেষ অপশন</b>\nএখান থেকে সোর্স স্ক্যান আর yt-dlp কন্ট্রোল করুন।', parse_mode: 'HTML', reply_markup: specialKeyboard(d.cfg) });
    return;
  } else if (data === 'back') {
    const d = await liveData(env);
    await tg(env, 'editMessageText', { chat_id: chatId, message_id: msgId, text: updateText(d), parse_mode: 'HTML', reply_markup: updateKeyboard(d.cfg) });
    return;
  } else if (data === 'close') {
    await setMode(env, chatId, 'ai', null);
    await tg(env, 'editMessageText', { chat_id: chatId, message_id: msgId, text: '❌ UPDATE মোড বন্ধ। সাধারণ প্রশ্ন লিখলে সাথে সাথে উত্তর দেব; LIVE আপডেট চাইলে লিখুন SSK DRAMA LIVE।', parse_mode: 'HTML' });
    return;
  }
  const d = await liveData(env);
  await tg(env, 'editMessageText', { chat_id: chatId, message_id: msgId, text: updateText(d), parse_mode: 'HTML', reply_markup: updateKeyboard(d.cfg) });
}

async function cronTick(env) {
  const st = (await rawJson(env, 'bot_state.json')) || {};
  if (st.mode !== 'live') { return; }
  const chatId = st.chat_id || env.TELEGRAM_CHAT_ID;
  if (!chatId) { return; }
  const d = await liveData(env);
  const text = liveReport(d);
  const mid = st.live_message_id || st.message_id;
  if (mid) {
    const r = await tg(env, 'editMessageText', { chat_id: chatId, message_id: mid, text: text, parse_mode: 'HTML' });
    if (r.ok) { return; }
  }
  const s = await tg(env, 'sendMessage', { chat_id: chatId, text: text, parse_mode: 'HTML' });
  if (s.ok && s.result) {
    st.live_message_id = s.result.message_id; st.chat_id = chatId; st.mode = 'live';
    await writeJson(env, 'bot_state.json', st, 'SSK bot: live message');
  }
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (url.pathname === '/set-webhook') {
      if (env.BOT_SECRET && url.searchParams.get('s') !== env.BOT_SECRET) {
        return new Response('forbidden', { status: 403 });
      }
      const hook = url.origin + '/telegram';
      const payload = { url: hook, allowed_updates: ['message', 'callback_query'] };
      if (env.BOT_SECRET) { payload.secret_token = env.BOT_SECRET; }
      const set = await tg(env, 'setWebhook', payload);
      const info = await tg(env, 'getWebhookInfo', {});
      return new Response(JSON.stringify({ setWebhook: set, getWebhookInfo: info }, null, 2), {
        status: 200, headers: { 'content-type': 'application/json' }
      });
    }
    if (request.method !== 'POST') {
      return new Response('SSK DRAMA bot is running.', { status: 200 });
    }
    if (env.BOT_SECRET) {
      const token = request.headers.get('x-telegram-bot-api-secret-token');
      if (token !== env.BOT_SECRET) { return new Response('forbidden', { status: 403 }); }
    }
    let update = null;
    try { update = await request.json(); } catch (e) { return new Response('ok'); }
    ctx.waitUntil(handleUpdate(env, update));
    return new Response('ok');
  },
  async scheduled(event, env, ctx) {
    ctx.waitUntil(cronTick(env));
  }
};
