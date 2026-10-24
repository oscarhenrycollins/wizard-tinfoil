"""Pinned official SDK only. stdin and stdout are private subprocess pipes."""
import datetime
import json
import logging
import os
import sys
logging.disable(logging.CRITICAL)

def proxy_client(api_key, timeout):
    """Pinned SDK 0.14.0 adapter: only its HTTP transport gains CONNECT support.

    Bundle mode avoids random router / direct AMD / GitHub lookups. Sigstore
    verification and trust-root refresh remain enabled and unmodified.
    SDK's custom httpx.HTTPTransport ignores environment proxies by default.
    """
    import httpx
    from ehbp import EHBPTransport
    from openai import OpenAI
    from tinfoil import TinfoilAI
    from tinfoil.client import SecureClient

    class ProxySecureClient(SecureClient):
        def _build_ehbp_sync_transport(self):
            key = self._require_hpke_public_key()
            transport = httpx.HTTPTransport(proxy=os.environ.get('HTTPS_PROXY') or os.environ['https_proxy'])
            return EHBPTransport.from_public_key_hex(key, inner=transport)

    secure = ProxySecureClient(enclave='inference.tinfoil.sh', repo='tinfoilsh/confidential-model-router', attestation_bundle_url='https://atc.tinfoil.sh')
    http = secure.make_secure_http_client()  # official verification, fail closed
    result = TinfoilAI.__new__(TinfoilAI)
    result._secure_client = secure
    result.api_key, result.enclave = api_key, secure.enclave
    result.client = OpenAI(api_key=api_key, timeout=timeout,
                           base_url=f'https://{secure.enclave}/v1/', http_client=http)
    result.chat, result.embeddings, result.audio = result.client.chat, result.client.embeddings, result.client.audio
    return result


def run(action, request, factory=None):
    if factory is None:
        from tinfoil import TinfoilAI
        factory = proxy_client if os.environ.get('HTTPS_PROXY') or os.environ.get('https_proxy') else TinfoilAI
    # No direct OpenAI fallback, custom host, transport override or measurement bypass.
    client = factory(api_key=request['key'], timeout=30)
    try:
        client.client.max_retries = 0
        if action == 'verify':
            models = sorted({m.id for m in client.client.models.list().data})
            if not models: raise ValueError('No models')
            # Listing models alone does not prove the key; a one-token attested call does.
            probe = 'deepseek-v4-1-flash' if 'deepseek-v4-1-flash' in models else next((m for m in models if not any(x in m for x in ('embed','whisper','voxtral','tts','realtime','audio'))), None)
            if probe is None: raise ValueError('No chat model')
            client.chat.completions.create(model=probe, messages=[{'role':'user','content':'ok'}], max_tokens=1)
            return {'models':models, 'authenticated':True, 'verifiedAt':datetime.datetime.now(datetime.timezone.utc).isoformat()}
        if action != 'infer': raise ValueError('Invalid action')
        payload = dict(request['payload'])
        # Kimi K3 on Tinfoil accepts only low/high/max; agents default to medium. Map to the nearest supported level.
        if str(payload.get('model','')).startswith('kimi-k3') and payload.get('reasoning_effort') not in (None,'low','high','max'):
            payload['reasoning_effort'] = {'none':'low','minimal':'low','medium':'high','xhigh':'max'}.get(payload['reasoning_effort'],'high')
        # DeepSeek V4.1 Flash on Tinfoil rejects a reasoning effort; send none.
        if payload.get('model') == 'deepseek-v4-1-flash': payload.pop('reasoning_effort', None)
        extra = {k: payload.pop(k) for k in ('thinking','reasoning','chat_template_kwargs','top_k','min_p','repetition_penalty') if k in payload}
        result = client.chat.completions.create(**payload, **({'extra_body': extra} if extra else {}))
        if request['payload'].get('stream'):
            return {'sse': ''.join('data: '+chunk.model_dump_json()+'\n\n' for chunk in result)+'data: [DONE]\n\n'}
        return result.model_dump(mode='json')
    finally:
        client.client.close()

def error_code(error):
    status = getattr(error, 'status_code', None)
    if status in (401, 403): return 'signed_out'
    if status == 429: return 'limit'
    if isinstance(error, (TimeoutError, ConnectionError, OSError)) or type(error).__name__ in ('APIConnectionError', 'APITimeoutError'):
        return 'unreachable'
    return 'provider_error'


def router_infer(request, emit, factory=None):
    """Official Python SDK's attested transport; no buffered synthetic SSE."""
    client = None
    try:
        if factory is None:
            from tinfoil import TinfoilAI
            factory = proxy_client if os.environ.get('HTTPS_PROXY') or os.environ.get('https_proxy') else TinfoilAI
        client = factory(api_key=request['key'], timeout=30)
        client.client.max_retries = 0
        expected = request['payload']['model']
        effective, usage, finished = None, {}, False
        for chunk in client.chat.completions.create(**request['payload']):
            data = chunk.model_dump(mode='json')
            if data.get('model'):
                effective = data['model']
                if effective != expected: raise ValueError('Effective model differs')
            if isinstance(data.get('usage'), dict): usage = data['usage']
            for choice in data.get('choices', []):
                text = (choice.get('delta') or {}).get('content')
                if isinstance(text, str) and text: emit({'type': 'delta', 'text': text})
                if choice.get('finish_reason') is not None: finished = True
        if effective != expected or not finished: raise ValueError('Incomplete stream')
        emit({'type': 'done', 'model': effective, 'usage': usage})
    except Exception as error:
        emit({'type': 'error', 'code': error_code(error)})
    finally:
        if client is not None: client.client.close()


if __name__ == '__main__':
    try:
        if sys.argv[1] == 'infer-router':
            router_infer(json.load(sys.stdin), lambda event: print(json.dumps(event), flush=True))
        else:
            result=run(sys.argv[1],json.load(sys.stdin))
            sys.stdout.write(json.dumps(result))
    except BaseException:
        # Upstream errors may contain authorization headers or response bodies.
        sys.exit(1)
