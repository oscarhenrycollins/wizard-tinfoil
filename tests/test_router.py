"""Router contract tests; fake SDK is not an attestation proof."""
import hashlib
import http.client
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch
from ws_client import Client

ROOT = Path(__file__).resolve().parents[1]
def load(name, path):
    spec=importlib.util.spec_from_file_location(name,path); module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module); return module
s=load('router_test_server', ROOT/'app/server.py')
p=load('router_test_provider', ROOT/'app/provider.py')
MODEL='deepseek-v4-1-flash'
KEY='synthetic-test-key-not-a-real-secret'
GRANT='synthetic-router-grant'

class RouterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory(); s.DATA=Path(cls.temp.name); s.initialize()
        cls.grants=s.DATA/'grants.json'
        cls.grants.write_text(json.dumps({'enforce':True,'incoming':[
            {'from':'wizard-connection-router','sha256':hashlib.sha256(GRANT.encode()).hexdigest()},
            {'from':'another-app','sha256':hashlib.sha256(b'other').hexdigest()}]}))
        cls.env=patch.dict(os.environ,{'WIZARD_GRANTS_FILE':str(cls.grants)}); cls.env.start()
        cls.http=s.ThreadingHTTPServer(('127.0.0.1',0),s.Handler); cls.port=cls.http.server_address[1]
        s.HOSTS.add(f'127.0.0.1:{cls.port}')
        threading.Thread(target=cls.http.serve_forever,daemon=True).start()
    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown(); cls.http.server_close(); cls.env.stop(); cls.temp.cleanup()
    def setUp(self):
        s.atomic('connection.json',{'key':KEY,'models':[MODEL,'glm-5-3','kimi-k3','voxtral-mini-4b-realtime'],'verifiedAt':'test','authenticated':True})
    def call(self, path, body=None, headers=None):
        conn=http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        conn.request('GET' if body is None else 'POST',path,body=None if body is None else json.dumps(body),
                     headers={'Content-Type':'application/json',**({'X-Wizard-Grant':GRANT} if headers is None else headers)})
        response=conn.getresponse(); status,kind,raw=response.status,response.getheader('Content-Type'),response.read()
        conn.close(); return status,kind,raw
    def test_only_router_grant_admitted(self):
        for path,body in [('/api/infer',{}),('/v1/live',None)]:
            for h in [{},{'X-Wizard-App':'wizard-connection-router'},{'X-Wizard-Grant':'bad'},
                      {'X-Wizard-Grant':'other'}, {'Authorization':'Bearer '+json.loads((s.DATA/'gateway-token.json').read_text())['token']},
                      {'X-Wizard-Grant':GRANT,'Origin':f'http://127.0.0.1:{self.port}'},
                      {'X-Wizard-Grant':GRANT,'Cookie':'owner=session'},
                      {'X-Wizard-Grant':GRANT,'Forwarded':'for=owner'}]:
                self.assertEqual(self.call(path,body,h)[0],403)
        with patch.dict(os.environ,{'WIZARD_GRANTS_FILE':str(s.DATA/'missing')}):
            self.assertEqual(self.call('/api/infer',{})[0],403)
    def test_router_grant_rotation_and_report_only_are_fail_closed(self):
        path=s.DATA/'rotation.json'
        original={'enforce':False,'incoming':[{'from':'wizard-connection-router','sha256':hashlib.sha256(GRANT.encode()).hexdigest()}]}
        with patch.dict(os.environ,{'WIZARD_GRANTS_FILE':str(path)}):
            path.write_text(json.dumps(original))
            self.assertEqual(self.call('/v1/live',headers={'X-Wizard-Grant':'unknown'})[0],403)
            self.assertEqual(self.call('/v1/live')[0],400)  # valid auth; upgrade missing
            original['incoming'][0]['sha256']=hashlib.sha256(b'rotated').hexdigest()
            path.write_text(json.dumps(original))
            self.assertEqual(self.call('/v1/live')[0],403)
            self.assertEqual(self.call('/v1/live',headers={'X-Wizard-Grant':'rotated'})[0],400)
            path.write_text('{invalid')
            self.assertEqual(self.call('/v1/live',headers={'X-Wizard-Grant':'rotated'})[0],403)

    def test_v2_models_keep_v1_status_and_gateway(self):
        status,_,raw=self.call('/api/connection'); self.assertEqual(status,200)
        data=json.loads(raw); self.assertEqual(data['contract'],'wizard.ai-connection/2')
        self.assertEqual([m['id'] for m in data['models']],[MODEL,'glm-5-3','kimi-k3'])
        self.assertEqual(data['models'][0],{'id':MODEL,'label':'DeepSeek V4.1 Flash','tier':'fast','abilities':['text','streaming'],'context':None})
        self.assertTrue(all(m['tier']!='frontier' for m in data['models']))
        self.assertEqual(json.loads(self.call('/api/status')[2])['models'][0],MODEL)
        self.assertNotIn(KEY,raw.decode()); self.assertTrue(data['credentialVerified'])
    def test_infer_sse_shape_system_usage(self):
        seen=[]
        def events(key,payload):
            self.assertEqual(key,KEY); seen.append(payload)
            yield {'type':'delta','text':'Hello'}
            yield {'type':'done','model':MODEL,'usage':{'total_tokens':5}}
        with patch.object(s,'infer_events',side_effect=events):
            code,kind,raw=self.call('/api/infer',{'model':MODEL,'system':'Be brief','messages':[{'role':'user','content':'hi'}],'max_tokens':16,'stream':True,'abilities':['text','streaming']})
        self.assertEqual((code,kind),(200,'text/event-stream'))
        self.assertEqual(raw.decode(),'event: delta\ndata: {"type": "delta", "text": "Hello"}\n\nevent: done\ndata: {"type": "done", "model": "'+MODEL+'", "usage": {"total_tokens": 5}}\n\n')
        self.assertEqual(seen[0]['messages'][0],{'role':'system','content':'Be brief'})
        self.assertEqual(seen[0]['stream_options'],{'include_usage':True})
    def test_infer_errors_and_payload_refusal(self):
        base={'model':MODEL,'messages':[{'role':'user','content':'hi'}]}
        with patch.object(s,'infer_events') as worker:
            for bad in [{'base_url':'https://evil.invalid'},{'model':'not-listed'},{'abilities':['web_search']},{'max_tokens':True},{'messages':[]},{'messages':[{'role':'system','content':'x'}]}]:
                code,kind,raw=self.call('/api/infer',{**base,**bad}); self.assertEqual(code,200)
                self.assertEqual(json.loads(raw.split(b'data: ')[1])['code'],'provider_error')
            worker.assert_not_called()
            (s.DATA/'connection.json').unlink()
            self.assertIn(b'"code": "signed_out"',self.call('/api/infer',base)[2]); worker.assert_not_called()
    def test_ws_unsupported_format_and_signed_out(self):
        client=Client(self.port,GRANT)
        try:
            client.send({'type':'start','format':'wav','sample_rate':16000,'channels':1})
            self.assertEqual(client.receive()['code'],'unsupported_format')
            self.assertEqual(client.receive(),{'socket_closed':1000})
        finally: client.close()
        (s.DATA/'connection.json').unlink(); client=Client(self.port,GRANT)
        try: self.assertEqual(client.receive()['code'],'signed_out')
        finally: client.close()
    def test_ws_first_frame_and_binary_bounds(self):
        for frame,code in [({'type':'commit'},'provider_error'),(bytes(65538),'unsupported_format')]:
            client=Client(self.port,GRANT)
            try: client.send(frame); self.assertEqual(client.receive()['code'],code)
            finally: client.close()

class SDKTests(unittest.TestCase):
    def client(self,chunks,seen):
        def factory(**kw):
            seen.append('attested')
            def create(**kw):
                seen.append(kw)
                return iter(NS(model_dump=lambda mode,c=c:c) for c in chunks)
            return NS(client=NS(max_retries=0,close=lambda:seen.append('closed')),chat=NS(completions=NS(create=create)))
        return factory
    def test_sdk_stream_mapping(self):
        seen=[]; out=[]
        chunks=[{'model':MODEL,'choices':[{'delta':{'content':'Hello'},'finish_reason':None}]},
                {'model':MODEL,'choices':[{'delta':{},'finish_reason':'stop'}],'usage':{'total_tokens':3}}]
        p.router_infer({'key':KEY,'payload':{'model':MODEL,'stream':True}},out.append,self.client(chunks,seen))
        self.assertEqual(out,[{'type':'delta','text':'Hello'},{'type':'done','model':MODEL,'usage':{'total_tokens':3}}])
        self.assertEqual(seen[0],'attested'); self.assertEqual(seen[-1],'closed')
    def test_missing_finish_or_wrong_effective_model_fail_closed(self):
        for chunks in [[],[{'model':'wrong','choices':[]}],[{'model':MODEL,'choices':[]}]]:
            out=[]; p.router_infer({'key':KEY,'payload':{'model':MODEL}},out.append,self.client(chunks,[]))
            self.assertEqual(out[-1],{'type':'error','code':'provider_error'})
            self.assertFalse(any(e['type']=='done' for e in out))
    def test_sdk_errors_are_sanitized(self):
        for status,code in [(401,'signed_out'),(403,'signed_out'),(429,'limit'),(500,'provider_error')]:
            def blocked(**kw):
                e=RuntimeError(KEY); e.status_code=status; raise e
            out=[]; p.router_infer({'key':KEY},out.append,blocked)
            self.assertEqual(out,[{'type':'error','code':code}]); self.assertNotIn(KEY,json.dumps(out))
        self.assertEqual(p.error_code(TimeoutError(KEY)),'unreachable')
