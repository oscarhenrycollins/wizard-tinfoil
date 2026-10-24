// Official SDK only: no custom socket, host, measurement or attestation bypass.
import { createInterface } from 'node:readline';
import { pathToFileURL } from 'node:url';

export const MODEL = 'voxtral-mini-4b-realtime';
const MAX_QUEUED = 4 * 1024 * 1024;
export function errorCode(error) {
  const status = error?.status ?? error?.statusCode ?? error?.error?.status;
  if (status === 401 || status === 403) return 'signed_out';
  if (status === 429) return 'limit';
  if (['ECONNRESET', 'ECONNREFUSED', 'ENOTFOUND', 'ETIMEDOUT'].includes(error?.code)) return 'unreachable';
  return 'provider_error';
}

// Each queue entry is one utterance; commit boundaries survive startup and rotation.
export class LiveSession {
  constructor(client, emit, timeout = 30000) {
    this.client = client; this.emit = emit; this.timeout = timeout;
    this.queue = [{ audio: [], committed: false }]; this.bytes = 0;
    this.started = false; this.stopping = false; this.closed = false;
    this.stream = null; this.opening = false; this.ready = false;
    this.hasAudio = false; this.finalizing = false; this.announced = false;
  }
  arm() { clearTimeout(this.timer); this.timer = setTimeout(() => this.fail('unreachable'), this.timeout); }
  disarm() { clearTimeout(this.timer); }
  fail(code = 'provider_error') {
    if (this.closed) return;
    this.emit({ type: 'error', code, message: code === 'unsupported_format' ? 'Use PCM16, 16000 Hz, mono.' : 'Private transcription unavailable.' });
    this.dispose();
  }
  dispose() {
    this.closed = true; this.disarm();
    try { this.stream?.close(); } catch {}
    this.stream = null; this.queue = []; this.bytes = 0;
  }
  finish() { if (!this.closed) { this.emit({ type: 'closed' }); this.dispose(); } }
  receive(frame) {
    if (this.closed) return;
    try {
      if (!this.started) {
        if (frame.type !== 'start') return this.fail();
        if (frame.format !== 'pcm_s16le' || frame.sample_rate !== 16000 || frame.channels !== 1 || frame.language != null) return this.fail('unsupported_format');
        this.started = true; void this.open(); return;
      }
      if (frame.type === 'audio' && !this.stopping) {
        if (typeof frame.audio !== 'string' || !/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(frame.audio)) return this.fail();
        const n = Buffer.from(frame.audio, 'base64').length;
        if (!n) return;
        if (n > 65536 || n % 2) return this.fail('unsupported_format');
        if (this.bytes + n > MAX_QUEUED) return this.fail('limit');
        this.queue.at(-1).audio.push(frame.audio); this.bytes += n; this.pump();
      } else if (frame.type === 'commit' && !this.stopping) {
        this.commit();
      } else if (frame.type === 'stop' && !this.stopping) {
        this.stopping = true; this.commit(); this.pump();
      } else return this.fail();
    } catch { this.fail(); }
  }
  commit() {
    const tail = this.queue.at(-1);
    // Audio already sent belongs to the head even when its pending list is empty.
    if (tail.audio.length || (this.queue.length === 1 && this.hasAudio && !tail.committed)) {
      tail.committed = true;
      if (this.queue.length >= 128) return this.fail('limit');
      this.queue.push({ audio: [], committed: false });
    }
    this.pump();
  }
  async open() {
    if (this.closed || this.stream || this.opening) return;
    this.opening = true; this.arm();
    try {
      const stream = await this.client.realtime({ model: MODEL });
      this.opening = false;
      if (this.closed) { stream.close(); return; }
      this.stream = stream;
      stream.on('session.created', () => {
        if (this.closed || this.stream !== stream || this.ready) return;
        try {
          this.disarm(); this.ready = true;
          stream.send({ type: 'session.update', model: MODEL });
          stream.send({ type: 'input_audio_buffer.commit' });
          if (!this.announced) { this.announced = true; this.emit({ type: 'ready', provider: 'wizard-tinfoil', model: MODEL }); }
          this.pump();
        } catch { this.fail(); }
      });
      stream.on('transcription.delta', event => {
        if (!this.closed && this.stream === stream && typeof event.delta === 'string') this.emit({ type: 'delta', text: event.delta });
      });
      stream.on('transcription.done', event => {
        if (this.closed || this.stream !== stream) return;
        if (!this.finalizing || typeof event.text !== 'string') return this.fail();
        this.disarm(); this.stream = null; this.ready = false;
        this.finalizing = false; this.hasAudio = false;
        stream.close(); this.queue.shift();
        this.emit({ type: 'final', text: event.text }); this.pump();
      });
      stream.on('error', error => { if (this.stream === stream) this.fail(errorCode(error)); });
      stream.on('close', () => { if (!this.closed && this.stream === stream) this.fail('unreachable'); });
    } catch (error) { this.opening = false; this.fail(errorCode(error)); }
  }
  pump() {
    if (this.closed || this.finalizing) return;
    const head = this.queue[0];
    if (this.stopping && this.queue.length === 1 && !head.audio.length && !this.hasAudio) return this.finish();
    if (!this.stream) { if (head.audio.length) void this.open(); return; }
    if (!this.ready) return;
    try {
      for (const audio of head.audio.splice(0)) {
        this.bytes -= Buffer.from(audio, 'base64').length;
        this.stream.send({ type: 'input_audio_buffer.append', audio }); this.hasAudio = true;
      }
      if (head.committed && this.hasAudio) {
        this.finalizing = true; this.arm();
        this.stream.send({ type: 'input_audio_buffer.commit', final: true });
      }
    } catch { this.fail(); }
  }
}

async function main() {
  // Key is supplied once through a private pipe, never argv or environment.
  let session;
  const lines = createInterface({ input: process.stdin, crlfDelay: Infinity });
  const emit = event => {
    process.stdout.write(JSON.stringify(event) + '\n');
    if (event.type === 'closed' || event.type === 'error') { lines.close(); process.stdin.destroy(); }
  };
  for await (const line of lines) {
    try {
      const frame = JSON.parse(line);
      if (!session) {
        if (frame.type !== 'init' || typeof frame.key !== 'string') throw new Error();
        const { TinfoilAI } = await import('tinfoil');
        session = new LiveSession(new TinfoilAI({ apiKey: frame.key }), emit);
      } else session.receive(frame);
    } catch { if (session) session.fail(); else { emit({ type: 'error', code: 'provider_error', message: 'Private transcription unavailable.' }); break; } }
  }
  session?.dispose();
}
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch(() => { process.exitCode = 1; });
}
