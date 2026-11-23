import argparse
import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from foodai.application import MODES
from foodai.bootstrap import build
from foodai.config import Settings


async def demo(args):
    settings = Settings.load()
    if args.catalog:
        settings.catalog_path = args.catalog
    if args.llm:
        settings.llm_mode = args.llm
    with TemporaryDirectory() as temp:
        settings.database_path = str(Path(temp)/'demo.sqlite')
        app = build(settings)
        await app.handle(1, args.zone)
        await app.handle(1, MODES['basket'])
        records = []
        for query in [args.query] + ([args.followup] if args.followup else []):
            result = await app.handle(1, query)
            print(result.text + '\n')
            records.append({'user': query, **result.to_dict()})
        if args.trace:
            trace = Path(args.trace)
            trace.parent.mkdir(parents=True, exist_ok=True)
            trace.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding='utf8')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--query', default='молоко 1 л, яйца 10 шт')
    parser.add_argument('--followup')
    parser.add_argument('--zone', default='msk_center')
    parser.add_argument('--catalog')
    parser.add_argument('--llm', choices=('mock', 'groq'))
    parser.add_argument('--trace')
    asyncio.run(demo(parser.parse_args()))


if __name__ == '__main__':
    main()
