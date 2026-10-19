"""Authenticated gateway draft synthesis; no writes to the voice registry."""
import json
import urllib.request


def request_preview(gateway_url, api_key, prompt, text):
    request=urllib.request.Request(gateway_url.rstrip('/')+'/synthesize-draft',
        data=json.dumps({'prompt_path':str(prompt),'text':text}).encode('utf-8'),
        headers={'Authorization':'Bearer '+api_key,'Content-Type':'application/json'},method='POST')
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request,timeout=180) as response:return response.read()
