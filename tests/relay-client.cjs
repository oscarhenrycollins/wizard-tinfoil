// Test-only transport adapter; the SDK's verifier and EHBP remain unmodified.
const fs = require('node:fs');
const https = require('node:https');
const { Readable } = require('node:stream');
const { spawnSync } = require('node:child_process');

async function probe(p) {
  const relay = 'http://wizard-tinfoil-relay:8090';
  const server = https.createServer({key:p.tlsKey, cert:p.tlsCert}, async (req,res) => {
    if (req.method !== 'GET' || req.url !== '/attestation') { res.writeHead(404).end(); return; }
    try {
      const response = await fetch(relay+'/attestation', {headers:{Authorization:'Bearer '+p.token}});
      res.writeHead(response.status, {'Content-Type':'application/json'});
      Readable.fromWeb(response.body).pipe(res);
    } catch { res.writeHead(502).end(); }
  });
  await new Promise(resolve => server.listen(0,'127.0.0.1',resolve));
  try {
    let internet = false;
    try { await fetch('https://example.com', {signal:AbortSignal.timeout(3000)}); internet=true; } catch {}
    if (internet) throw new Error('client has internet');
    const { TinfoilAI } = await import('tinfoil');
    const client = new TinfoilAI({bearerToken:p.token,baseURL:relay+'/v1/',attestationBundleURL:'https://localhost:'+server.address().port,userCacheSecret:p.cacheSecret,maxRetries:0});
    await client.ready();
    const doc = await client.getVerificationDocument();
    if (!doc.securityVerified) throw new Error('attestation not verified');
    const request = {model:p.model,messages:[{role:'user',content:'Confidential codeword: '+p.codeword+'. Reply with exactly OK and nothing else.'}],max_tokens:16};
    const answer = await client.chat.completions.create(request);
    if (answer.choices[0].message.content.trim() !== 'OK') throw new Error('nonstream response not OK');
    const stream = await client.chat.completions.create({...request,stream:true});
    let text='';
    for await (const chunk of stream) text += chunk.choices[0]?.delta.content || '';
    if (text.trim() !== 'OK') throw new Error('stream response not OK');
    console.log(JSON.stringify({nonstream:'OK',stream:'OK',client_internet:'blocked',sdk:'tinfoil@1.2.2',securityVerified:doc.securityVerified,attestation_transport:'in-client HTTPS adapter; crypto verification unchanged'}));
  } catch(error) {
    // Attestation errors precede any prompt. Never print completion errors/bodies.
    const message = String(error.message || '').replaceAll(p.token,'[redacted]').replaceAll(p.codeword,'[redacted]');
    console.error(JSON.stringify({failure_type:error.constructor.name,diagnostic:message.slice(0,250)}));
    process.exitCode=1;
  } finally { await new Promise(resolve => server.close(resolve)); }
}
(async () => {
  const p=JSON.parse(fs.readFileSync(0,'utf8'));
  if (!process.env.PP_PROBE_CHILD) {
    fs.writeFileSync('/work/ca.pem',p.tlsCert,{mode:0o600});
    const child=spawnSync(process.execPath,[__filename],{input:JSON.stringify(p),encoding:'utf8',env:{...process.env,PP_PROBE_CHILD:'1',NODE_EXTRA_CA_CERTS:'/work/ca.pem'},timeout:180000});
    process.stdout.write(child.stdout || ''); process.stderr.write(child.stderr || '');
    process.exitCode=child.status ?? 1;
  } else await probe(p);
})().catch(error => { console.error(error.constructor.name); process.exitCode=1; });
