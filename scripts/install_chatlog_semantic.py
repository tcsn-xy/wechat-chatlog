#!/usr/bin/env python3
"""Prepare explicit local MiniMax text consent and credential configuration."""
from __future__ import annotations

import argparse
from pathlib import Path

from chatlog_core.common import QueryError, load_config
from chatlog_core.semantic import (CONFIG, DIMENSIONS, ENDPOINT, MODEL, atomic_json,
                                   dependencies, read_private, settings)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--key-file', type=Path, required=True,
                        help='当前用户所有、0600 JSON 文件，含 api_key；不把 Key 放入命令行')
    parser.add_argument('--consent-all-history', action='store_true', required=True,
                        help='明确允许将两平台全部历史及新增可读文字发送给 MiniMax 生成向量')
    args = parser.parse_args()
    try:
        load_config(); dependencies()
        source = read_private(args.key_file)
        key = source.get('api_key')
        if not isinstance(key, str) or not key.strip():
            raise QueryError('semantic_config_required', '密钥文件缺少 api_key')
        value = dict(endpoint=ENDPOINT, model=MODEL, dimensions=DIMENSIONS, api_key=key,
                     text_external_consent=True, scope='all_history_and_new')
        if CONFIG.exists():
            if settings() != value:
                raise QueryError('semantic_config_conflict', '已有不同配置；未覆盖')
        else:
            atomic_json(CONFIG, value)
        print('语义配置已就绪；运行 chatlog semantic start --json 启动全历史索引。')
        return 0
    except QueryError as exc:
        print(exc.detail['code'] + ': ' + exc.detail['message'])
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
