import argparse
import logging
import re
import shutil
import subprocess
import sys
import time
from typing import IO, Callable, Dict, Iterable, List, Optional

from pathlib import Path

logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(
        description = 'Compile out-of-tree QMK keyboard in a Docker container.',
        epilog = "Additional arguments are passed to QMK's make command."
    )

    parser.add_argument('keyboard',
        metavar='BOARD', help='QMK keyboard name')
    parser.add_argument('layout', nargs='?', default='default',
        metavar='LAYOUT', help='QMK layout/keymap name')

    group = parser.add_mutually_exclusive_group()
    group.add_argument('--flash', nargs='?', default=False, const=True, choices=['l','r'],
        help='flash firmware to device after compilation')
    group2 = group.add_argument_group()
    group2.add_argument('--into',
        metavar='DEST', help='directory or filename to copy compiled artefacts to')
    group2.add_argument('-f', nargs='*', dest='extensions', default=['hex'],
        metavar='EXT', help="extension of the artefact(s) to retrieve (default: hex)")
    group2.add_argument('--name',
        metavar='NAME', help="basename used to rename the artefact(s)")

    parser.add_argument('-v', '--verbose', action='store_true',
        help="print more")

    args, extra_args = parser.parse_known_args()

    logging.basicConfig(level=logging.WARNING, format='%(message)s')
    logger.setLevel(logging.DEBUG if args.verbose else logging.INFO)

    QMK_HOME = Path(__file__).parent


    keyboard_rules = parse_keyboard_rules(QMK_HOME / 'keyboards' / args.keyboard)

    make_targets, make_args = partition(lambda a:a.startswith(':'), extra_args)

    def make_target():
        yield args.keyboard
        yield args.layout

        if args.flash in ('l', 'r'):
            bootloader = keyboard_rules.get('BOOTLOADER', '').lower()
            if 'dfu' in bootloader:
                if args.flash=='r': yield 'dfu-split-right'
                else              : yield 'dfu-split-left'
            elif 'caterina' in bootloader:
                if args.flash=='r': yield 'avrdude-split-right'
                else              : yield 'avrdude-split-left'
            else:
                logger.warning('no left/right specific flash target for `%s` bootloader', bootloader)
                yield 'flash'
        elif args.flash:
            yield 'flash'

        yield from (target.lstrip(':') for target in make_targets)

    make_cmd = ['make', ':'.join(make_target()), *make_args]

    logger.debug(f'make command: {subprocess.list2cmdline(make_cmd)}')

    process = subprocess.Popen(make_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
    if process.stdout:
        for line in force_buffering(process.stdout):
            if not line.endswith('\n'):
                line = '\x1b[A\x1b[J' + line + '\n'
            sys.stdout.write(line)
            sys.stdout.flush()
        
    process.wait()
    if process.returncode != 0:
        logger.error('build failed')
        return process.returncode
    
    if args.into:
        build_dir = QMK_HOME / '.build'
        into_dir = Path(args.into)
        output_basename = f'{args.keyboard}_{args.layout}'.replace('/', '_')
        final_basename = args.name if args.name else output_basename
        for ext in args.extensions:
            temp_output = build_dir / f'{output_basename}.{ext}'
            final_output = into_dir / f'{final_basename}.{ext}'

            logger.info(f'copy `{temp_output}` to `{final_output}`')
            if temp_output.is_file():
                shutil.copy(temp_output, final_output)
            else:
                logger.warning(f'`{temp_output}` is not a file')



def parse_keyboard_rules(keyboard_dir: Path):
    def find_rules():
        for parent in reversed([keyboard_dir, *keyboard_dir.parents]):
            rules = parent / 'rules.mk'
            if rules.is_file():
                yield rules

    parsed_rules: Dict[str,str] = {}
    for path in find_rules():
        parse_rulesmk(open(path), parsed_rules)

    return parsed_rules



def parse_rulesmk(lines: Iterable[str], initial: Optional[Dict[str,str]]=None):
    parsed = initial if initial != None else {}

    ops: Dict[str, Callable[[str,str], str]] = {
        '=' : lambda old,new: new,
        ':=': lambda old,new: new,
        '+=': lambda old,new: (old + ' ' + new) if old else new,
        '?=': lambda old,new: new if old else old,
    }

    regex = re.compile(r'(\w+)\s*(' + '|'.join(map(re.escape, ops)) + r')\s*(.+)')
    for line in makefile_lines(lines):
        m = regex.match(line)
        if m:
            name, op, val = m.groups()
            parsed[name] = ops[op](parsed[name], val) if name in parsed else val

    return parsed


def makefile_lines(lines: Iterable[str]):
    def stripped_lines(lines: Iterable[str]):
        for line in lines:
            if '#' in line:
                line = line[:line.index('#')]
            yield line.rstrip()
    
    def continued_lines(lines: Iterable[str]):
        lines = iter(lines)
        try:
            while True:
                line = next(lines)
                cont_line = line
                while line.endswith('\\'):
                    cont_line = line.rstrip('\\')
                    line = next(lines)
                    cont_line += '\n' + line
                yield line
        except StopIteration:
            pass

    return continued_lines(stripped_lines(lines))


def force_buffering(stream: IO[str]):
    t0 = 0
    line = ''
    for c in iter(lambda: stream.read(1), ''):
        if c:
            line += c
            if c == '\n':
                yield line
                line = ''
                t0 = time.perf_counter()
            elif line.endswith('..') or line.endswith('##'):
                if time.perf_counter()-t0 > .5:
                    yield line
        else:
            break


def partition(pred: Callable[[str], bool], xs: Iterable[str]):
    ts: List[str] = []
    fs: List[str] = []
    for x in xs:
        (ts if pred(x) else fs).append(x)
    return ts,fs


if __name__ == '__main__':
    exit(main())