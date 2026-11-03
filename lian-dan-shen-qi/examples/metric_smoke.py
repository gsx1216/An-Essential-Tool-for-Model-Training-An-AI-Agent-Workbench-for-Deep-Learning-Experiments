"""仅演示结构化指标输出；数值是示例公式，不是实际模型训练结果。"""

import argparse
import json
from pathlib import Path
import time


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--config',required=True)
    args=parser.parse_args()
    parameters=json.loads(Path(args.config).read_text(encoding='utf-8'))
    print('Metric protocol demo; synthetic values, no model training.',flush=True)
    for step in range(parameters.get('steps',3)):
        print(json.dumps({'type':'metric','split':'train','step':step,'epoch':step,'loss':1/(step+1)}),flush=True)
        print(json.dumps({'type':'metric','split':'validation','step':step,'loss':1/(step+1)+.1}),flush=True)
        time.sleep(parameters.get('delay_seconds',.2))


if __name__=='__main__':
    main()
