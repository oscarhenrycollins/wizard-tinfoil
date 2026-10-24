"""Opt-in real enclave proof: python3 tests/live_relay.py --key-env PATH.

Build the branch image as pp-test-tinfoil-relay and tests/relay-client.Dockerfile
with context tests/ as pp-test-relay-client first. All pp-test resources
are disposable. The key enters only the app's own key-save HTTP route.
No provider credential is passed to the client container or capture process.

SDKs require HTTPS for bundle fetching and omit proxy auth there.
The test's in-client TLS adapter supplies auth/transport, not verification:
base_url is the real HTTP relay; attestation_bundle_url is local HTTPS, backed
by an ephemeral trusted CA. This accommodation is reported, not hidden.
"""
import argparse
import http.client
import json
import os
from pathlib import Path
import secrets
import subprocess
import time

IMAGE = 'pp-test-tinfoil-relay'
APP = 'pp-test-tinfoil-relay'
CLIENT = 'pp-test-relay-client'
CAPTURE = 'pp-test-relay-capture'
NETWORK = 'pp-test-relay'
EGRESS = 'pp-test-relay-egress'

CAPTURE_CODE = r'''
import json, socket, sys
from pathlib import Path
probe=json.loads(sys.stdin.readline())['codeword'].encode()
sock=socket.socket(socket.AF_PACKET,socket.SOCK_RAW,socket.htons(3)); sock.settimeout(0.5)
packets=[]
print('READY',flush=True)
while not Path('/work/stop').exists():
    try:
        packet=sock.recv(262144)
        # Ethernet IPv4 TCP traffic on relay port; no provider-side TLS traffic.
        if len(packet)<54 or packet[12:14]!=b'\x08\x00' or packet[23]!=6: continue
        start=14+(packet[14]&15)*4
        if int.from_bytes(packet[start:start+2],'big')!=8090 and int.from_bytes(packet[start+2:start+4],'big')!=8090: continue
        tcp_end=start+(packet[start+12]>>4)*4
        packets.append(packet[tcp_end:])
    except TimeoutError: continue
wire=b''.join(packets)
print(json.dumps({'packets':len(packets),'payload_bytes':len(wire),'ehbp_request_header':b'ehbp-encapsulated-key:' in wire.lower(),'ehbp_response_header':b'ehbp-response-nonce:' in wire.lower(),'codeword_absent':probe not in wire}),flush=True)
'''


def docker(*args, input=None, check=True):
    result = subprocess.run(['docker', *args], input=input, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if check and result.returncode:
        # Commands, env, stdout/stderr can contain auth or response material.
        raise RuntimeError('docker operation failed: '+args[0])
    return result


def main():
    parser=argparse.ArgumentParser(); parser.add_argument('--key-env',required=True)
    parser.add_argument('--subnet',default='10.252.241.0/24')
    parser.add_argument('--egress-subnet',default='10.253.241.0/24',help='Management listener binds this runtime-pool interface; component test only, not runtime egress-policy proof')
    args=parser.parse_args()
    codeword='PRIVPROBE-'+secrets.token_hex(16)
    capture=None
    try:
        docker('network','create','--internal','--subnet',args.subnet,NETWORK)
        docker('network','create','--subnet',args.egress_subnet,EGRESS)
        docker('run','-d','--name',APP,'--network',EGRESS,'--read-only','--cap-drop','ALL','--security-opt','no-new-privileges','--tmpfs','/data:rw,uid=1000,gid=1000,mode=700','--tmpfs','/tmp:rw,uid=1000,gid=1000','-p','127.0.0.1::18804',IMAGE)
        docker('network','connect','--alias','wizard-tinfoil-relay',NETWORK,APP)
        port=int(docker('port',APP,'18804/tcp').stdout.strip().rsplit(':',1)[1])
        def api(method,path,body=None):
            conn=http.client.HTTPConnection('127.0.0.1',port,timeout=60)
            headers={'Host':'localhost:18804','Origin':'http://localhost:18804','X-Wizard-Request':'1','Content-Type':'application/json'}
            conn.request(method,path,body=None if body is None else json.dumps(body),headers=headers)
            response=conn.getresponse(); data=response.read(); status=response.status; conn.close()
            return status,json.loads(data) if data else None
        for attempt in range(30):
            try:
                if api('GET','/healthz')[0]==200: break
            except OSError: time.sleep(0.2)
        else: raise RuntimeError('app never became ready')
        # No env file bind/copy, no shell expansion, and no credential stdout.
        env={}
        for line in Path(args.key_env).read_text().splitlines():
            if '=' in line and not line.lstrip().startswith('#'):
                name,value=line.split('=',1); env[name.strip()]=value.strip().strip('\"\'')
        key=env.get('TINFOIL_API_KEY')
        if not key: raise RuntimeError('key env does not contain TINFOIL_API_KEY')
        status,connected=api('POST','/api/connect',{'apiKey':key})
        del key,env
        assert status==200 and connected['credentialStored'], 'app key-save verification failed'
        status,connection=api('GET','/api/connection')
        assert status==200 and connection['models'], 'no chat models'
        ids=[m['id'] for m in connection['models']]
        model=next((m for m in ('llama3-3-70b','gpt-oss-120b','kimi-k3') if m in ids),ids[0])
        status,client=api('POST','/api/private-clients',{'agent':'live-relay-probe'})
        assert status==200
        # Real peer refusal from an unrelated (egress) network, not a mock.
        refusal=docker('run','--rm','--name','pp-test-relay-outsider','--network',EGRESS,IMAGE,'python','-c',"import http.client; c=http.client.HTTPConnection('pp-test-tinfoil-relay',8090); c.request('GET','/healthz'); assert c.getresponse().status==403; print('outsider=403')").stdout.strip()
        print(refusal)
        capture=subprocess.Popen(['docker','run','--rm','-i','--name',CAPTURE,'--network','container:'+APP,'--user','0:0','--cap-drop','ALL','--cap-add','NET_RAW','--read-only','--tmpfs','/work',IMAGE,'python','-u','-c',CAPTURE_CODE],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        capture.stdin.write(json.dumps({'codeword':codeword})+'\n'); capture.stdin.flush()
        assert capture.stdout.readline().strip()=='READY', 'capture did not become ready'
        tls=subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout','/dev/stdout','-out','/dev/stdout','-days','1','-subj','/CN=localhost','-addext','subjectAltName=DNS:localhost','-addext','basicConstraints=critical,CA:TRUE'],capture_output=True,text=True,check=True).stdout
        private_key=tls[:tls.index('-----BEGIN CERTIFICATE-----')]
        certificate=tls[tls.index('-----BEGIN CERTIFICATE-----'):]
        result=docker('run','--rm','-i','--name',CLIENT,'--network',NETWORK,'--read-only','--cap-drop','ALL','--security-opt','no-new-privileges','--tmpfs','/work:uid=1000,gid=1000','pp-test-relay-client','node','/probe/relay-client.cjs',input=json.dumps({'token':client['token'],'codeword':codeword,'model':model,'tlsKey':private_key,'tlsCert':certificate,'cacheSecret':secrets.token_urlsafe(48)})+'\n',check=False)
        if result.returncode:
            print('SDK client result:',result.stdout.strip(), result.stderr.strip()[-500:])
            raise RuntimeError('live SDK client failed')
        print(result.stdout.strip())
        docker('exec',CAPTURE,'python','-c',"from pathlib import Path; Path('/work/stop').touch()")
        stdout,stderr=capture.communicate(timeout=10)
        assert capture.returncode==0, 'capture failed'
        wire=json.loads(stdout.strip()); print(json.dumps(wire))
        assert wire['ehbp_request_header'] and wire['ehbp_response_header'] and wire['codeword_absent']
        # No plaintext probe in app output or any /data file (including SDK cache).
        logs=docker('logs',APP)
        assert codeword not in logs.stdout and codeword not in logs.stderr
        scan=docker('exec','-i',APP,'python','-c',"import json,sys; from pathlib import Path; p=json.loads(sys.stdin.readline()); files=[f for f in Path('/data').rglob('*') if f.is_file()]; assert all(p['codeword'].encode() not in f.read_bytes() for f in files); print('app_data_files='+str(len(files))+'; codeword_absent=true')",input=json.dumps({'codeword':codeword})+'\n')
        print(scan.stdout.strip()); print('app_logs_codeword_absent=true')
        old=client['token']
        status,client=api('POST','/api/private-clients',{'agent':'live-relay-probe'}); assert status==200 and client['token']!=old
        def rejected(token):
            return docker('run','--rm','-i','--name','pp-test-relay-auth','--network',NETWORK,IMAGE,'python','-c',"import http.client,json,sys; t=json.loads(sys.stdin.readline())['token']; c=http.client.HTTPConnection('wizard-tinfoil-relay',8090); c.request('GET','/attestation',headers={'Authorization':'Bearer '+t}); assert c.getresponse().status==401",input=json.dumps({'token':token})+'\n')
        rejected(old)
        assert api('DELETE','/api/private-clients/live-relay-probe')[0]==204
        rejected(client['token'])
        print('live_rotation_revocation=PASS; model='+model)
    finally:
        for name in (CLIENT,CAPTURE,'pp-test-relay-auth','pp-test-relay-outsider',APP): docker('rm','-f',name,check=False)
        if capture:
            try: capture.communicate(timeout=5)
            except subprocess.TimeoutExpired: capture.kill(); capture.communicate()
        for network in (NETWORK,EGRESS): docker('network','rm',network,check=False)
        print('test_resources_cleanup=complete')


if __name__=='__main__': main()
