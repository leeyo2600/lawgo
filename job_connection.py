"""Build an OpenAI-only web run without persisting credentials in job files."""
import copy
import re

from llm_providers import resolve_provider


def run_config(config, options, allow_endpoint=False):
    if options.get('provider', 'openai') != 'openai':
        raise ValueError('OpenAI API 키를 입력해주세요.')
    if options.get('api_url') not in (None, '', 'https://api.openai.com/v1/chat/completions'):
        raise ValueError('OpenAI API 주소만 사용할 수 있습니다.')
    cfg = copy.deepcopy(config)
    cfg.pop('bizrouter_optional', None)
    cfg['llm_provider'] = 'openai'
    # The web page takes only a key; the installed model setting remains internal.
    model = cfg.get('llm_model') or 'gpt-6-luna'
    if isinstance(model, str) and model.startswith('openai/'):
        model = model.removeprefix('openai/')
    if not isinstance(model, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,149}', model):
        raise ValueError('서버의 OpenAI 모델 설정을 확인해주세요.')
    cfg['llm_model'] = model
    if 'api_key' in options:
        key = options['api_key']
        if not isinstance(key, str) or not key.strip() or not key.isascii() or re.search(r'\s', key.strip()):
            raise ValueError('OpenAI API 키를 입력해주세요.')
        cfg['llm_api_key'] = key.strip()
    return resolve_provider(cfg)
