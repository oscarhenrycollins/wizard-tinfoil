"""Router-only grants, ai-connection/2 and private worker streams."""
import hashlib
import hmac
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

APP = Path(__file__).resolve().parent


def router_allowed(headers):
    """Fail closed even if grants are absent or report-only. Identity hints are not auth."""
    if any(k.lower() in {'origin', 'authorization', 'cookie', 'tailscale-user-login',
                        'x-forwarded-for', 'forwarded', 'x-real-ip'} or k.lower().startswith('cf-') for k in headers):
        return False
    token = headers.get('X-Wizard-Grant', '')
    if not token or len(headers.get_all('X-Wizard-Grant', [])) != 1:
        return False
    try:
        grants = json.loads(Path(os.environ['WIZARD_GRANTS_FILE']).read_text())
        rows = grants['incoming']
        if not isinstance(rows, list): return False
        digest = hashlib.sha256(token.encode()).hexdigest()
        return any(isinstance(row, dict) and row.get('from') == 'wizard-connection-router'
                   and isinstance(row.get('sha256'), str) and hmac.compare_digest(digest, row['sha256']) for row in rows)
    except (OSError, ValueError, KeyError, TypeError):
        return False


def models(catalog):
    # Catalog-backed: never advertise an unavailable model or non-chat endpoint.
    excluded = ('embed', 'tts', 'whisper', 'voxtral', 'doc-upload', 'pii-filter', 'websearch')
    labels = {'deepseek-v4-1-flash': 'DeepSeek V4.1 Flash', 'glm-5-3': 'GLM 5.3',
              'kimi-k3': 'Kimi K3', 'gpt-oss-120b': 'GPT OSS 120B', 'llama3-3-70b': 'Llama 3.3 70B'}
    return [{'id': m, 'label': labels.get(m, m),
             'tier': 'fast' if m == 'deepseek-v4-1-flash' else 'standard',
             'abilities': ['text', 'streaming'], 'context': None}
            for m in catalog if not any(x in m for x in excluded)]


def infer_payload(body, catalog):
    allowed = {'model', 'system', 'messages', 'max_tokens', 'stream', 'abilities'}
    messages = body.get('messages')
    abilities = body.get('abilities', [])
    if (set(body) - allowed or body.get('model') not in {m['id'] for m in models(catalog)}
            or not isinstance(body.get('system', ''), str)
            or type(body.get('stream', True)) is not bool
            or not isinstance(abilities, list) or any(a not in ('text', 'streaming') for a in abilities)
            or not isinstance(messages, list) or not 1 <= len(messages) <= 128
            or any(not isinstance(m, dict) or set(m) != {'role', 'content'}
                   or m['role'] not in ('user', 'assistant') or not isinstance(m['content'], str) for m in messages)):
        raise ValueError('Invalid inference request')
    maximum = body.get('max_tokens', 2048)
    if type(maximum) is not int or not 1 <= maximum <= 32768: raise ValueError('Invalid token limit')
    return {'model': body['model'], 'messages': ([{'role': 'system', 'content': body['system']}] if body.get('system') else []) + messages,
            'max_tokens': maximum, 'stream': True, 'stream_options': {'include_usage': True}}


def infer_events(key, payload):
    """JSON-line private pipe; emit text as it arrives, reap worker on disconnect/timeout."""
    process = subprocess.Popen([sys.executable, str(APP / 'provider.py'), 'infer-router'],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    timer = threading.Timer(180, process.kill)
    timer.daemon = True; timer.start()
    terminal = False
    try:
        process.stdin.write(json.dumps({'key': key, 'payload': payload}) + '\n'); process.stdin.close()
        for line in process.stdout:
            event = json.loads(line)
            if event.get('type') not in ('delta', 'done', 'error'): raise ValueError()
            yield event
            if event['type'] in ('done', 'error'):
                terminal = True; break
        if not terminal: yield {'type': 'error', 'code': 'unreachable'}
    except (OSError, ValueError):
        yield {'type': 'error', 'code': 'provider_error'}
    finally:
        timer.cancel()
        if process.poll() is None: process.kill()
        process.wait(); process.stdout.close()
