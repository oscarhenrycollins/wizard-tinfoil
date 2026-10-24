"""Opt-in paid smoke: local disposable server, real attested SDKs, synthetic speech only.
Usage: python tools/smoke_router.py --key-file /private/key --wav /synthetic.wav --output /private/logdir
Never run with real recordings. Never logs credentials or SDK exception strings.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import secrets
import sys
import tempfile
import threading
import time
from urllib.request import Request, urlopen
import wave

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tests'))
from ws_client import Client

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--key-file',required=True,type=Path); parser.add_argument('--wav',required=True,type=Path)
    parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args(); args.output.mkdir(parents=True,exist_ok=True)
    key=args.key_file.read_text().strip()
    if not key: raise ValueError('Key file is empty')
    with wave.open(str(args.wav),'rb') as audio:
        assert (audio.getframerate(),audio.getnchannels(),audio.getsampwidth())==(16000,1,2)
        pcm=audio.readframes(audio.getnframes())
    with tempfile.TemporaryDirectory(prefix='tinfoil-smoke-',dir=args.key_file.parent) as temp:
        os.environ['DATA_DIR']=temp
        grants=Path(temp)/'grants.json'; grant=secrets.token_urlsafe(32)
        grants.write_text(json.dumps({'enforce':True,'incoming':[{'from':'wizard-connection-router','sha256':hashlib.sha256(grant.encode()).hexdigest()}]})); grants.chmod(0o600)
        os.environ['WIZARD_GRANTS_FILE']=str(grants)
        spec=importlib.util.spec_from_file_location('smoke_server',ROOT/'app/server.py')
        s=importlib.util.module_from_spec(spec); spec.loader.exec_module(s); s.initialize()
        # Verify catalog with the unchanged official Python SDK, not a fabricated list.
        catalog=s.worker('verify',key)
        assert 'deepseek-v4-1-flash' in catalog['models']
        s.atomic('connection.json',{'key':key,**catalog,'authenticated':False})
        http=s.ThreadingHTTPServer(('127.0.0.1',0),s.Handler); port=http.server_address[1]
        s.HOSTS.add(f'127.0.0.1:{port}'); threading.Thread(target=http.serve_forever,daemon=True).start()
        try:
            payload={'model':'deepseek-v4-1-flash','system':'Be concise.','messages':[{'role':'user','content':'Reply with exactly: Private inference works.'}],'max_tokens':64,'stream':True,'abilities':['text','streaming']}
            request=Request(f'http://127.0.0.1:{port}/api/infer',data=json.dumps(payload).encode(),headers={'X-Wizard-Grant':grant,'Content-Type':'application/json'})
            with urlopen(request,timeout=180) as response:
                assert response.headers['Content-Type']=='text/event-stream'
                raw=response.read().decode()
            assert key not in raw and grant not in raw
            (args.output/'tinfoil-infer.log').write_text(raw)
            events=[json.loads(line[6:]) for line in raw.splitlines() if line.startswith('data: ')]
            text=''.join(e.get('text','') for e in events if e['type']=='delta')
            assert text and events[-1]['type']=='done', 'Inference did not complete'
            assert events[-1]['model']=='deepseek-v4-1-flash' and isinstance(events[-1]['usage'],dict)
            print('INFER',json.dumps({'text':text,'done':events[-1]}),flush=True)
            client=Client(port,grant); live=[]
            try:
                client.send({'type':'start','format':'pcm_s16le','sample_rate':16000,'channels':1,'language':None})
                # Send while attestation is starting, including audio before ready.
                for offset in range(0,len(pcm),8000):
                    client.send(pcm[offset:offset+8000]); time.sleep(0.25)
                client.send({'type':'commit'}); client.send({'type':'stop'})
                while True:
                    event=client.receive(); live.append(event)
                    if event.get('type') in ('closed','error'): break
                finals=[e['text'] for e in live if e.get('type')=='final']
                result={'events':live,'transcript':' '.join(finals),'pcm_bytes':len(pcm),'frame_ms':250,'sdk':'tinfoil npm 1.2.1 / realtime / attested pinned TLS'}
                serialized=json.dumps(result,indent=2)
                assert key not in serialized and grant not in serialized
                (args.output/'tinfoil-live.log').write_text(serialized+'\n')
                assert live[0].get('type')=='ready' and live[-1].get('type')=='closed' and len(finals)==1, 'Live stream did not complete'
                normalized=' '.join(finals).lower()
                assert all(word in normalized for word in ('purple','wizard','private','transcription','test')), 'Spoken words missing'
                print('LIVE',json.dumps(result),flush=True)
            finally: client.close()
        finally: http.shutdown(); http.server_close()

if __name__=='__main__':
    try: main()
    except Exception as error:
        print('SMOKE FAILED:',type(error).__name__,flush=True)
        sys.exit(1)
