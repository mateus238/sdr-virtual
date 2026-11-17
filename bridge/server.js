// Bridge NÃO OFICIAL: mantém uma sessão do WhatsApp Web (Baileys) conectada por QR Code.
// Conversa com o servidor Python por HTTP. Sem nenhum recurso de disfarce: sem digitação simulada,
// sem aleatorização de tempos, sem troca de identidade de dispositivo.
import http from 'node:http';
import path from 'node:path';
import fs from 'node:fs';
import pino from 'pino';
import QRCode from 'qrcode';
import makeWASocket, { useMultiFileAuthState, DisconnectReason, fetchLatestBaileysVersion } from '@whiskeysockets/baileys';

const PORT = +(process.env.BRIDGE_PORT || 3100);
const HOST = process.env.BRIDGE_HOST || '127.0.0.1';          // só local: nunca exponha na internet
const TOKEN = process.env.QR_BRIDGE_TOKEN || '';
const APP_URL = (process.env.APP_URL || 'http://127.0.0.1:8080').replace(/\/$/, '');
const AUTH_DIR = process.env.BRIDGE_AUTH_DIR || './auth';      // credenciais da sessão: trate como senha
if (!TOKEN) { console.error('Defina QR_BRIDGE_TOKEN (o mesmo valor do servidor Python).'); process.exit(1); }
const log = pino({ level: process.env.LOG_LEVEL || 'info' });
const sessions = new Map(); // id -> {sock, state, qr, me, stopped}

const safeId = id => /^[\w-]{1,40}$/.test(id) ? id : null;
async function notify(body) {
  try {
    await fetch(APP_URL + '/webhooks/qr', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Bridge-Token': TOKEN }, body: JSON.stringify(body) });
  } catch (e) { log.warn({ err: e.message }, 'falha ao avisar o servidor'); }
}
const digits = jid => (jid || '').split('@')[0].split(':')[0].replace(/\D/g, '');

async function start(id) {
  let s = sessions.get(id);
  if (s && !s.stopped && s.sock) return s;
  s = s || { state: 'connecting', qr: null, me: null, stopped: false };
  s.stopped = false; sessions.set(id, s);
  const { state, saveCreds } = await useMultiFileAuthState(path.join(AUTH_DIR, id));
  const { version } = await fetchLatestBaileysVersion();
  const sock = makeWASocket({ version, auth: state, printQRInTerminal: false, logger: pino({ level: 'warn' }), markOnlineOnConnect: false, syncFullHistory: false });
  s.sock = sock; s.state = 'connecting';
  sock.ev.on('creds.update', saveCreds);
  sock.ev.on('connection.update', async u => {
    const { connection, lastDisconnect, qr } = u;
    if (qr) { s.qr = await QRCode.toDataURL(qr, { margin: 1, width: 320 }); s.state = 'qr'; }
    if (connection === 'open') { s.state = 'open'; s.qr = null; s.me = digits(sock.user?.id); log.info({ id, me: s.me }, 'conectado'); notify({ session: id, type: 'connection', state: 'open', me: s.me ? '+' + s.me : null }); }
    if (connection === 'close') {
      const code = lastDisconnect?.error?.output?.statusCode;
      s.sock = null; s.qr = null;
      if (code === DisconnectReason.loggedOut) { s.state = 'logged_out'; s.stopped = true; fs.rmSync(path.join(AUTH_DIR, id), { recursive: true, force: true }); notify({ session: id, type: 'connection', state: 'logged_out' }); }
      else if (code === DisconnectReason.forbidden) { s.state = 'banned'; s.stopped = true; notify({ session: id, type: 'connection', state: 'banned' }); }
      else if (code === DisconnectReason.connectionReplaced) { s.state = 'replaced'; s.stopped = true; notify({ session: id, type: 'connection', state: 'replaced' }); }
      else { s.state = 'close'; notify({ session: id, type: 'connection', state: 'close' }); if (!s.stopped) setTimeout(() => start(id).catch(e => log.error(e)), 5000); }
    }
  });
  sock.ev.on('messages.upsert', ({ messages, type }) => {
    if (type !== 'notify') return;
    for (const m of messages) {
      if (m.key.fromMe || !m.message) continue;
      const jid = m.key.remoteJidAlt || m.key.remoteJid || '';
      if (jid.endsWith('@g.us') || jid.endsWith('@broadcast')) continue;           // ignora grupos e listas
      if (jid.endsWith('@lid')) { log.warn({ id }, 'mensagem de contato com ID oculto (LID) sem número: ignorada'); continue; }
      const msg = m.message;
      const text = msg.conversation || msg.extendedTextMessage?.text || msg.buttonsResponseMessage?.selectedDisplayText || msg.listResponseMessage?.title;
      const kind = Object.keys(msg)[0];
      notify({ session: id, type: 'message', from: '+' + digits(jid), id: m.key.id, ts: Number(m.messageTimestamp) || Math.floor(Date.now() / 1000),
               text: text ?? `[mensagem do tipo '${kind}' — conteúdo não suportado; peça ao lead para escrever]` });
    }
  });
  sock.ev.on('messages.update', ups => {
    for (const u of ups) {
      const st = u.update?.status; // 2=servidor, 3=entregue, 4=lida
      const map = { 2: 'sent', 3: 'delivered', 4: 'read', 5: 'read' };
      if (u.key?.fromMe && map[st]) notify({ session: id, type: 'status', id: u.key.id, status: map[st] });
    }
  });
  return s;
}

function json(res, code, body) { res.writeHead(code, { 'Content-Type': 'application/json' }); res.end(JSON.stringify(body)); }
const readBody = req => new Promise(r => { let b = ''; req.on('data', c => b += c); req.on('end', () => { try { r(JSON.parse(b || '{}')); } catch { r({}); } }); });

http.createServer(async (req, res) => {
  if (req.headers['x-bridge-token'] !== TOKEN) return json(res, 403, { error: 'forbidden' });
  const m = req.url.match(/^\/sessions\/([^/]+)\/(start|status|send|logout)$/);
  if (!m) return json(res, 404, { error: 'not_found' });
  const id = safeId(m[1]); if (!id) return json(res, 400, { error: 'bad_session' });
  const s = sessions.get(id);
  try {
    if (m[2] === 'start' && req.method === 'POST') { const x = await start(id); return json(res, 200, { state: x.state, qr: x.qr }); }
    if (m[2] === 'status') return json(res, 200, { state: s?.state || 'not_started', qr: s?.qr || null, me: s?.me ? '+' + s.me : null });
    if (m[2] === 'logout' && req.method === 'POST') { if (s?.sock) { s.stopped = true; await s.sock.logout().catch(() => {}); } sessions.delete(id); fs.rmSync(path.join(AUTH_DIR, id), { recursive: true, force: true }); return json(res, 200, { ok: true }); }
    if (m[2] === 'send' && req.method === 'POST') {
      if (!s || s.state !== 'open' || !s.sock) return json(res, 409, { error: 'not_connected' });
      const { to, text } = await readBody(req); const num = String(to || '').replace(/\D/g, '');
      if (!num || !text) return json(res, 400, { error: 'bad_request' });
      const [r] = await s.sock.onWhatsApp(num + '@s.whatsapp.net');
      if (!r?.exists) return json(res, 422, { error: 'not_on_whatsapp' });
      const sent = await s.sock.sendMessage(r.jid, { text });
      return json(res, 200, { id: sent.key.id });
    }
    return json(res, 405, { error: 'method' });
  } catch (e) { log.error({ err: e.message }, 'erro'); return json(res, 503, { error: e.message }); }
}).listen(PORT, HOST, () => {
  log.info(`bridge em http://${HOST}:${PORT}`);
  if (fs.existsSync(AUTH_DIR)) for (const d of fs.readdirSync(AUTH_DIR)) if (safeId(d)) start(d).catch(e => log.error(e)); // retoma sessões salvas
});
