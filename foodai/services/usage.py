"""Provider-reported usage only; no keys, addresses or message contents in logs."""
from contextvars import ContextVar
from datetime import datetime, timezone
import json
import logging
from pathlib import Path

request_id = ContextVar('foodai_request_id', default=None)


def record_usage(path, response, model, structured):
    usage = response.json().get('usage') if response.status_code == 200 else None
    counters = None
    if isinstance(usage, dict):
        counters = {key: int(usage.get(key, 0)) for key in ('prompt_tokens', 'completion_tokens', 'total_tokens')}
        counters['cached_tokens'] = int((usage.get('prompt_tokens_details') or {}).get('cached_tokens', 0))
    if path:
        target = Path(path)
        row = {'at': datetime.now(timezone.utc).isoformat(), 'request_id': request_id.get(),
               'model': model, 'status': response.status_code, 'structured': structured,
               'request_bytes': len(response.request.content), 'usage': counters}
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open('a', encoding='utf8') as stream:
                stream.write(json.dumps(row, ensure_ascii=False, separators=(',', ':')) + '\n')
        except OSError:
            logging.getLogger(__name__).warning('Could not save model usage metrics')
    return counters
