"""Runner 连通性示例：仅验证参数、进程和日志，不进行模型训练。"""

import argparse
import json
from pathlib import Path
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    parameters = json.loads(Path(args.config).read_text(encoding='utf-8'))
    print('Runner smoke demo; no model training or paid calls.', flush=True)
    for step in range(1, parameters.get('epochs', 3) + 1):
        print(json.dumps({'step': step, 'message': 'demo step finished'}), flush=True)
        time.sleep(parameters.get('delay_seconds', .2))
    print('Demo completed.', flush=True)


if __name__ == '__main__':
    main()
