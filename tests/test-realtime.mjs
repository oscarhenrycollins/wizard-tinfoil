import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import test from 'node:test';
import { LiveSession, MODEL, errorCode } from '../app/realtime.mjs';
const start = {type:'start',format:'pcm_s16le',sample_rate:16000,channels:1,language:null};
const tick = () => new Promise(resolve => setImmediate(resolve));
function rig() {
  const streams = [], out = [];
  const client = { realtime: async (props) => {
    assert.deepEqual(props, {model:MODEL});
    const stream = new EventEmitter(); stream.sent = []; stream.close = () => { stream.closed = true; stream.emit('close'); };
    stream.send = frame => stream.sent.push(frame); streams.push(stream); return stream;
  }};
  const session = new LiveSession(client, e => out.push(e));
  return {session, streams, out};
}
const audio = n => ({type:'audio',audio:Buffer.from([n,0,n,0]).toString('base64')});

test('maps start/PCM/delta/final and sends non-final bootstrap commit', async () => {
  const {session:s, streams, out} = rig();
  s.receive(start); s.receive(audio(1)); s.receive({type:'commit'});
  await tick(); const a=streams[0]; a.emit('session.created', {});
  assert.deepEqual(a.sent, [{type:'session.update',model:MODEL},{type:'input_audio_buffer.commit'},
    {type:'input_audio_buffer.append',audio:audio(1).audio},{type:'input_audio_buffer.commit',final:true}]);
  a.emit('transcription.delta',{delta:'hello'}); a.emit('transcription.done',{text:'hello world'});
  assert.deepEqual(out,[{type:'ready',provider:'wizard-tinfoil',model:MODEL},{type:'delta',text:'hello'},{type:'final',text:'hello world'}]);
  assert.equal(a.closed,true); s.receive({type:'stop'}); assert.equal(out.at(-1).type,'closed');
});

test('rotation queues multiple utterances and preserves every commit in order', async () => {
  const {session:s,streams,out}=rig(); s.receive(start); await tick();
  const a=streams[0]; a.emit('session.created',{});
  s.receive(audio(1)); s.receive({type:'commit'});
  s.receive(audio(2)); s.receive({type:'commit'});
  s.receive(audio(3)); s.receive({type:'stop'});
  assert.equal(streams.length,1);
  a.emit('transcription.done',{text:'one'}); await tick();
  const b=streams[1]; assert.equal(b.sent.length,0); b.emit('session.created',{});
  assert.equal(b.sent.filter(x=>x.type==='input_audio_buffer.append').length,1);
  assert.equal(b.sent[2].audio,audio(2).audio);
  // Late events from the old connection must not produce duplicate final/delta.
  a.emit('transcription.done',{text:'stale'}); a.emit('transcription.delta',{delta:'stale'});
  b.emit('transcription.done',{text:'two'}); await tick();
  const c=streams[2]; c.emit('session.created',{});
  assert.equal(c.sent[2].audio,audio(3).audio); c.emit('transcription.done',{text:'three'});
  assert.deepEqual(out.filter(x=>x.type==='final').map(x=>x.text),['one','two','three']);
  assert.equal(out.filter(x=>x.type==='ready').length,1); assert.equal(out.at(-1).type,'closed');
  assert.equal(streams.length,3);
});

test('multiple commits before ready, and stop during attestation, retain audio', async () => {
  const {session:s,streams,out}=rig(); s.receive(start);
  s.receive(audio(1)); s.receive({type:'commit'}); s.receive(audio(2)); s.receive({type:'stop'});
  await tick(); streams[0].emit('session.created',{}); streams[0].emit('transcription.done',{text:'first'});
  await tick(); streams[1].emit('session.created',{}); streams[1].emit('transcription.done',{text:'second'});
  assert.deepEqual(out.map(x=>x.type),['ready','final','final','closed']);
});

test('empty commits do not invent finals, empty stop closes', async () => {
  const {session:s,out}=rig(); s.receive(start); s.receive({type:'commit'}); s.receive({type:'stop'}); await tick();
  assert.deepEqual(out,[{type:'closed'}]);
});

test('unsupported format fails closed without SDK call', () => {
  for (const bad of [{format:'wav'},{sample_rate:48000},{channels:2},{language:'en'}]) {
    const {session:s,streams,out}=rig(); s.receive({...start,...bad});
    assert.equal(out[0].code,'unsupported_format'); assert.equal(streams.length,0); assert.equal(s.closed,true);
  }
});

test('first-frame and frame bounds are enforced', async () => {
  let r=rig(); r.session.receive(audio(1)); assert.equal(r.out[0].code,'provider_error');
  r=rig(); r.session.receive(start); r.session.receive({type:'audio',audio:Buffer.alloc(65538).toString('base64')});
  await tick(); assert.equal(r.out[0].code,'unsupported_format');
  r=rig(); r.session.receive(start); for(let i=0;i<65;i++)r.session.receive({type:'audio',audio:Buffer.alloc(65536).toString('base64')});
  await tick(); assert.equal(r.out[0].code,'limit');
});

test('SDK failure and attestation rejection never fall back or expose raw errors', async () => {
  const out=[]; const s=new LiveSession({realtime:async()=>{throw Object.assign(new Error('secret credential'),{status:401});}}, e=>out.push(e));
  s.receive(start); await tick(); assert.equal(out[0].code,'signed_out'); assert.ok(!JSON.stringify(out).includes('secret'));
  assert.equal(errorCode({status:429}),'limit'); assert.equal(errorCode({code:'ENOTFOUND'}),'unreachable');
  assert.equal(errorCode(new Error('attestation mismatch')),'provider_error');
});

test('official JS SDK attestation failure blocks realtime before any socket request', async () => {
  const { TinfoilAI } = await import('../app/node_modules/tinfoil/dist/index.js');
  const client = new TinfoilAI({ apiKey: 'synthetic-invalid-key' });
  let requests = 0;
  client.secureClient.getPinnedWebSocketOptions = async () => { throw new Error('attestation mismatch'); };
  client.ensureReady = async () => { requests++; throw new Error('must not reach HTTP'); };
  const out=[]; const session=new LiveSession(client,e=>out.push(e));
  session.receive(start); await tick();
  assert.equal(requests,0); assert.equal(session.closed,true);
  assert.deepEqual(out,[{type:'error',code:'provider_error',message:'Private transcription unavailable.'}]);
});

test('unexpected upstream close terminates, timeout reaps opening session', async () => {
  const r=rig(); r.session.receive(start); await tick(); r.streams[0].emit('close');
  assert.equal(r.out[0].code,'unreachable');
  const out=[]; const s=new LiveSession({realtime:()=>new Promise(()=>{})},e=>out.push(e),10);
  s.receive(start); await new Promise(resolve=>setTimeout(resolve,25)); assert.equal(out[0].code,'unreachable'); assert.equal(s.closed,true);
});
