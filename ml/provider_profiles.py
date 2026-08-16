"""ML provider profiles (added 2026-07-27).

WHY THIS EXISTS
---------------
The decision engine used to hard-code the OpenAI wire format (Bearer auth,
`/chat/completions`, and a `choices[0].message.content` reply). That only works
for OpenAI-compatible endpoints. The default `api.freemodel.dev` endpoint is
DEAD, so the whole ML layer silently fell back to the local model and never
fired. This module abstracts the provider-specific bits (auth header, chat
path, request body shape, and where the reply text lives) behind a small
`Profile` object so the SAME engine can talk to OpenAI, any OpenAI-compatible
gateway (Together, Groq, OpenRouter, Fireworks, LM Studio, vLLM...), Anthropic,
Google Gemini, Cohere, or a local Ollama server just by selecting a profile.

SAFETY
------
This module is PURE + defensive. Every parse is wrapped so a malformed reply
returns '' instead of raising -- the engine treats '' as "no answer" and
gracefully falls back to the local model. Nothing here can make the bot
collapse; the worst case is a clean fall-back to rules.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


def _dig(data: Any, path: str) -> Any:
    """Walk a dotted path with numeric indices, e.g. 'choices.0.message.content'.
    Returns None on any miss instead of raising."""
    cur = data
    for part in path.split('.'):
        if cur is None:
            return None
        try:
            if isinstance(cur, list):
                cur = cur[int(part)]
            elif isinstance(cur, dict):
                cur = cur.get(part)
            else:
                return None
        except (ValueError, IndexError, KeyError, TypeError):
            return None
    return cur


@dataclass
class Profile:
    """Describes how to talk to one provider family."""
    name: str
    label: str                                   # human label for Telegram
    chat_path: str = '/chat/completions'         # appended to base_url
    models_path: Optional[str] = '/models'       # for key/model discovery (GET)
    auth_header: str = 'Authorization'
    auth_prefix: str = 'Bearer '                 # header value = prefix + key
    key_in_query: Optional[str] = None           # gemini: ?key=API_KEY instead
    extra_headers: Dict[str, str] = field(default_factory=dict)
    system_mode: str = 'message'                 # 'message' | 'top' | 'prepend'
    response_path: str = 'choices.0.message.content'
    usage_path: Optional[str] = 'usage.total_tokens'
    models_path_json: str = 'data'               # where the model list lives
    models_id_key: str = 'id'
    default_base_url: str = ''
    default_model: str = ''
    # builder/parser overrides (set for non-openai families)
    _body: Optional[Callable] = None
    _parse: Optional[Callable] = None

    # -- HTTP shaping ------------------------------------------------- #
    def url(self, base_url: str) -> str:
        return (base_url or self.default_base_url).rstrip('/') + self.chat_path

    def models_url(self, base_url: str, api_key: str = '') -> Optional[str]:
        if not self.models_path:
            return None
        u = (base_url or self.default_base_url).rstrip('/') + self.models_path
        if self.key_in_query and api_key:
            u += ('&' if '?' in u else '?') + f'{self.key_in_query}={api_key}'
        return u

    def headers(self, api_key: str) -> Dict[str, str]:
        h = {'Content-Type': 'application/json'}
        h.update(self.extra_headers or {})
        if api_key and self.auth_header and not self.key_in_query:
            h[self.auth_header] = f'{self.auth_prefix}{api_key}'
        return h

    def full_url(self, base_url: str, api_key: str = '') -> str:
        u = self.url(base_url)
        if self.key_in_query and api_key:
            u += ('&' if '?' in u else '?') + f'{self.key_in_query}={api_key}'
        return u

    # -- request body ------------------------------------------------- #
    def body(self, model: str, system: str, user: str,
             max_tokens: int, temperature: float) -> Dict[str, Any]:
        if self._body is not None:
            return self._body(self, model, system, user, max_tokens, temperature)
        # default: OpenAI chat schema
        msgs = []
        if system and self.system_mode == 'message':
            msgs.append({'role': 'system', 'content': system})
        content = user if self.system_mode != 'prepend' else f'{system}\n\n{user}'
        msgs.append({'role': 'user', 'content': content})
        return {'model': model, 'messages': msgs,
                'max_tokens': max_tokens, 'temperature': temperature}

    # -- reply parsing ------------------------------------------------ #
    def parse(self, data: Any) -> str:
        try:
            if self._parse is not None:
                return (self._parse(self, data) or '').strip()
            val = _dig(data, self.response_path)
            return (val or '').strip() if isinstance(val, str) else ''
        except Exception:
            return ''

    def tokens(self, data: Any) -> int:
        if not self.usage_path:
            return 0
        try:
            v = _dig(data, self.usage_path)
            return int(v) if v else 0
        except Exception:
            return 0

    def parse_models(self, data: Any) -> List[str]:
        try:
            lst = _dig(data, self.models_path_json)
            if not isinstance(lst, list):
                return []
            out = []
            for item in lst:
                if isinstance(item, dict):
                    mid = item.get(self.models_id_key) or item.get('name') or item.get('id')
                    if mid:
                        # gemini returns 'models/gemini-1.5-pro'
                        out.append(str(mid).split('/')[-1] if self.name == 'google_gemini' else str(mid))
                elif isinstance(item, str):
                    out.append(item)
            return out
        except Exception:
            return []


# ---- family-specific builders / parsers ----------------------------- #
def _anthropic_body(p, model, system, user, max_tokens, temperature):
    b = {'model': model,
         'messages': [{'role': 'user', 'content': user}],
         'max_tokens': max_tokens, 'temperature': temperature}
    if system:
        b['system'] = system  # Anthropic takes system as a TOP-LEVEL field
    return b


def _gemini_body(p, model, system, user, max_tokens, temperature):
    text = user if not system else f'{system}\n\n{user}'
    return {'contents': [{'role': 'user', 'parts': [{'text': text}]}],
            'generationConfig': {'maxOutputTokens': max_tokens,
                                 'temperature': temperature}}


def _cohere_body(p, model, system, user, max_tokens, temperature):
    b = {'model': model, 'message': user,
         'max_tokens': max_tokens, 'temperature': temperature}
    if system:
        b['preamble'] = system
    return b


# ---- the registry --------------------------------------------------- #
PROFILES: Dict[str, Profile] = {
    'openai': Profile(
        name='openai', label='OpenAI',
        default_base_url='https://api.openai.com/v1',
        default_model='gpt-4o-mini',
    ),
    'openai_compatible': Profile(
        name='openai_compatible', label='OpenAI-compatible (Groq/Together/OpenRouter/vLLM/LM Studio)',
        default_base_url='',
        default_model='',
    ),
    'anthropic': Profile(
        name='anthropic', label='Anthropic (Claude)',
        chat_path='/v1/messages', models_path='/v1/models',
        auth_header='x-api-key', auth_prefix='',
        extra_headers={'anthropic-version': '2023-06-01'},
        system_mode='top', response_path='content.0.text',
        usage_path='usage.output_tokens',
        default_base_url='https://api.anthropic.com',
        default_model='claude-3-5-haiku-latest',
        _body=_anthropic_body,
    ),
    'google_gemini': Profile(
        name='google_gemini', label='Google Gemini',
        chat_path='/v1beta/models/{model}:generateContent',
        models_path='/v1beta/models',
        auth_header='', auth_prefix='', key_in_query='key',
        response_path='candidates.0.content.parts.0.text',
        usage_path='usageMetadata.totalTokenCount',
        models_path_json='models', models_id_key='name',
        default_base_url='https://generativelanguage.googleapis.com',
        default_model='gemini-1.5-flash',
        _body=_gemini_body,
    ),
    'cohere': Profile(
        name='cohere', label='Cohere',
        chat_path='/v1/chat', models_path='/v1/models',
        response_path='text', usage_path='meta.tokens.output_tokens',
        models_path_json='models', models_id_key='name',
        default_base_url='https://api.cohere.ai',
        default_model='command-r',
        _body=_cohere_body,
    ),
    'ollama': Profile(
        name='ollama', label='Ollama (local, no key)',
        chat_path='/v1/chat/completions', models_path='/v1/models',
        auth_prefix='Bearer ',
        default_base_url='http://localhost:11434',
        default_model='llama3.1',
    ),
}

DEFAULT_PROFILE = 'openai_compatible'


def get_profile(name: Optional[str]) -> Profile:
    """Return the named profile, defaulting safely to openai_compatible."""
    if not name:
        return PROFILES[DEFAULT_PROFILE]
    return PROFILES.get(str(name).strip().lower(), PROFILES[DEFAULT_PROFILE])


def list_profiles() -> List[Profile]:
    return list(PROFILES.values())


def gemini_url(base_url: str, model: str, api_key: str) -> str:
    """Gemini needs the model baked into the path; helper for the engine."""
    p = PROFILES['google_gemini']
    path = p.chat_path.replace('{model}', model or p.default_model)
    u = (base_url or p.default_base_url).rstrip('/') + path
    if api_key:
        u += ('&' if '?' in u else '?') + f'key={api_key}'
    return u


# ---- live model discovery (added 2026-08-03 for the /mlsetup wizard) ---- #
def discover_models(base_url, api_key='', profile_name=None, timeout=8):
    """GET a provider's models endpoint; return (ok, models, error).
    Fail-open: any network/parse issue returns (False, [], reason). Uses the
    profile's models_url()/parse_models() so it works for OpenAI, any
    OpenAI-compatible gateway, Anthropic, Gemini, Cohere and Ollama."""
    prof = get_profile(profile_name)
    try:
        import requests  # lazy import keeps this module import-light + pure
    except Exception as e:  # pragma: no cover
        return (False, [], 'requests unavailable: %s' % e)
    url = prof.models_url(base_url, api_key)
    if not url:
        return (False, [], 'profile %s exposes no models endpoint' % prof.name)
    try:
        r = requests.get(url, headers=prof.headers(api_key), timeout=timeout)
        if r.status_code != 200:
            return (False, [], 'HTTP %s: %s' % (r.status_code, (r.text or '')[:80]))
        models = prof.parse_models(r.json())
        if models:
            return (True, models, '')
        return (False, [], 'no models in response')
    except Exception as e:
        return (False, [], str(e)[:100])


def autodetect_profile(base_url, api_key='', timeout=8):
    """Probe every known profile's models endpoint one-by-one and return the
    first that answers. Returns (profile_name, models, tried) where tried is a
    list of (name, ok, note) for a readable report; profile_name is '' if none
    worked. Backs the wizard's 'I don't know -- try all' option."""
    tried = []
    for name in PROFILES:
        ok, models, err = discover_models(base_url, api_key, name, timeout=timeout)
        note = ('%d models' % len(models)) if ok else (err or 'no')
        tried.append((name, ok, note))
        if ok and models:
            return (name, models, tried)
    return ('', [], tried)
