import argparse
import hashlib
import logging
import os
import re
import subprocess
from collections import Counter
from pathlib import Path
from time import time
from typing import List, Optional

from .dockerstuff import VolumesMapping
from .dockerstuff import logger as docker_logger
from .dockerstuff import run_in_container

logger = logging.getLogger(__name__)


def main():

    parser = argparse.ArgumentParser(
        description = "Compile out-of-tree QMK keyboard in a Docker container.",
        epilog = "Additional arguments are passed to QMK's make command."
    )

    parser.add_argument('keyboard',
        metavar='BOARD', help='out-of-tree keyboard primary C file or QMK keyboard name')
    parser.add_argument('layout', nargs='?', default='default',
        metavar='LAYOUT', help='out-of-tree layout directory or QMK layout/keymap name')

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

    parser.add_argument('--build-dir',
        metavar='DIR', help='directory to store build files into')
    
    group = parser.add_argument_group(title='container and source')
    ex = group.add_mutually_exclusive_group()
    ex.add_argument('--qmk-git',
        metavar='URL', help='QMK git repository url')

    parser.add_argument('-n', '--dry-run', action='store_true',
        help="just print build commands; don't run them")
    parser.add_argument('-v', '--verbose', action='store_true',
        help="print more")

    args, extra_args = parser.parse_known_args()

    logging.basicConfig(level=logging.WARNING, format='%(message)s')
    for l in (logger, docker_logger):
        l.setLevel(logging.DEBUG if args.verbose else logging.INFO)

    try:
        return run_build(args, extra_args)
    except ValueError as e:
        logger.error(f'error: {e}')
        return 2
    except KeyboardInterrupt:
        return 1



QMKUSER = 'qmkuser'
QMKUSER_HOME = Path('/home') / QMKUSER
ARTEFACTS = Path('/artefacts')
DIR = Path(__file__).parent


def path_hash(path: Path, length: int=8):
    return hashlib.shake_128(str(path.resolve()).encode()).hexdigest(length//2)


def run_build(args: argparse.Namespace, extra_args: List[str]):
    volumes: VolumesMapping = {}
    device_cgroup_rules: List[str] = []

    QMK_DIR = Path(args.qmk_git).stem if args.qmk_git else 'qmk_firmware'
    QMK_HOME = QMKUSER_HOME / QMK_DIR
    

    keyboard_path = Path(args.keyboard)
    if keyboard_path.is_file():
        logger.info(f'using out-of-tree keyboard `{keyboard_path}`')
        tmp_keyboard_name = f'hermit{path_hash(keyboard_path.parent)}'
        tmp_keyboard_path = QMK_HOME / 'keyboards' / tmp_keyboard_name
        for src,dst in user_keyboard_files(keyboard_path.parent, keyboard_path.stem, tmp_keyboard_name):
            volumes[tmp_keyboard_path / dst] = src, 'ro'
    elif keyboard_path.is_dir():
        raise ValueError('out-of-tree keyboard must be a file')
    else:
        tmp_keyboard_name = str(args.keyboard)
        tmp_keyboard_path = QMK_HOME / 'keyboards' / tmp_keyboard_name

    if args.layout:
        layout_path = Path(args.layout)
        if layout_path.is_dir():
            logger.info(f'using out-of-tree layout `{layout_path}`')

            layout_type = guess_layout_type(layout_path)
            if not layout_type:
                raise ValueError('could not guess layout type')
            logger.info(f'guessed `{layout_type}` layout type')

            tmp_layout_name = f'hermit{path_hash(layout_path)}'
            tmp_layout_path = QMK_HOME / 'layouts' / 'community' / layout_type / tmp_layout_name
            for src in layout_path.glob('*'):
                volumes[tmp_layout_path / src.name] = src, 'ro'
        elif keyboard_path.is_dir():
            raise ValueError('out-of-tree layout must be a directory')
        else:
            tmp_layout_name = str(args.layout)
    else:
        tmp_layout_name = 'default'
    
    if args.build_dir:
        build_path = Path(args.build_dir)
        if build_path.is_dir():
            volumes[QMK_HOME / '.build'] = build_path, 'rw'
        else:
            raise ValueError('build directory not a directory')
    
    if args.into:
        into_path = Path(args.into)
        if into_path.is_dir():
            volumes[ARTEFACTS] = into_path, 'rw'
        else:
            raise ValueError('output directory not a directory')

    if args.flash:
        volumes[Path('/dev')] = Path('/dev'),'ro'
        device_cgroup_rules = ['c *:* rmw']
    else:
        device_cgroup_rules = []
    
    if args.name:
        output_basename = str(args.name)
    else:
        output_basename = f'{tmp_keyboard_name}_{tmp_layout_name}'.replace('/', '_')

    volumes[QMK_HOME / 'build.py'] = DIR / 'build.py', 'ro'

    def container_args():
        yield 'python3'
        yield QMK_HOME / 'build.py'
        yield tmp_keyboard_name
        yield tmp_layout_name
         
        if args.into:
            yield from ('--name', output_basename)
            yield from ('-f', *args.extensions)
            yield from ('--into', ARTEFACTS)

        if args.flash==True:
            yield '--flash'
        elif args.flash:
            yield from ('--flash', args.flash)
        
        if args.verbose:
            yield '--verbose'
        
        yield from extra_args
    
    start_time = time()
    exit_code = run_in_container(DIR / 'Dockerfile',
        image_args(args.qmk_git),
        map(str, container_args()),
        volumes = volumes,
        tag = 'qmk-hermit',
        device_cgroup_rules = device_cgroup_rules,
    )
    if exit_code:
        logger.warning('failed.')
    elif args.into:
        for fn in Path(args.into).glob(f'{output_basename}.*'):
            if fn.suffix.lstrip('.') in args.extensions and fn.stat().st_mtime > start_time:
                logger.info(f'retrieved `{fn}`')

    return exit_code


def user_keyboard_files(src_dir: Path, src_name: str, dst_name: str):

    def renames(fn: str):
        if fn == f'{src_name}.c':
            yield f'{dst_name}.c'
            yield fn
        elif fn == f'{src_name}.h':
            yield f'{dst_name}.h'
            yield fn
        else:
            m = re.match(r'keymap(?:[-_ ]+(.+))?\.(c|h)', str(fn))
            if m:
                name,ext = m.group(1) or 'default', m.group(2)
                yield Path('keymaps') / name / f'keymap.{ext}'
            else:
                yield fn
    
    for src_fn in src_dir.glob('*'):
        for dst_fn in renames(src_fn.name):
            yield src_fn, dst_fn



def guess_layout_type(src_dir: Path):
    def find_occurences():
        for f in src_dir.glob('keymap.*'):
            proc = subprocess.Popen(['cpp', f], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            proc.wait()
            if proc.stdout:
                for line in proc.stdout.readlines():
                    yield from re.findall(r'LAYOUT_([^(]+)\s*\(', line)
    
    for k,_ in Counter(find_occurences()).most_common(1):
        return k


def image_args(qmk_git: Optional[str]=None):
    if qmk_git:
        yield 'QMK_GIT', qmk_git
        yield 'QMK_DIR', Path(qmk_git).stem
    yield 'UID', str(os.getuid())
    yield 'GID', str(os.getgid())
    yield 'USER', QMKUSER



if __name__ == '__main__':
    exit(main())
